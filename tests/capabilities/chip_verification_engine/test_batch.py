"""Deterministic shared-invocation tests; no real model/EDA execution is claimed."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from loopx.capabilities.chip_verification.engine.agents import AgentResult
from loopx.capabilities.chip_verification.engine.batch import _bundle_prompt, _effective_prompt, _schema, work_bundled
from loopx.capabilities.chip_verification.engine.report import write_report
from loopx.capabilities.chip_verification.engine.state import StateStore
from loopx.capabilities.chip_verification.engine.workflow import DVProject, save_json


class CandidateRuntime:
    def __init__(self, agent="codex", mutate=None, after_run=None):
        self.agent, self.mutate, self.after_run = agent, mutate, after_run
        self.calls = []

    @staticmethod
    def candidate(context):
        if "requirements" in context:
            return {"testpoints": [{"id": f"TP-{index}", "name": "Observe FIFO ordering",
                                    "requirement_ids": [requirement["id"]], "level": "ip",
                                    "stimulus": "Enqueue distinct words while varying ready",
                                    "observations": ["monitor observes accepted transactions"],
                                    "checkers": ["scoreboard compares output against independent queue"],
                                    "coverage": ["observed empty and full occupancy"],
                                    "uvm_components": ["fifo_agent", "fifo_scoreboard"],
                                    "configurations": ["depth=4"], "priority": "critical"}
                                   for index, requirement in enumerate(context["requirements"])
                                   if requirement["applicability"] == "in_scope"], "unresolved": []}
        owned = context.get("contract", {}).get("acceptance", {}).get("owned_source_ids")
        sources = [source for source in context["sources"] if owned is None or source["source_id"] in owned]
        return {
            "requirements": [{"local_id": f"R-{index}", "statement": source["text"],
                              "source_ids": [source["source_id"]], "modules": ["fifo"],
                              "features": ["ordering"], "interfaces": ["ready_valid"],
                              "conditions": ["outside reset"], "expected": source["text"],
                              "exceptions": [], "applicability": "in_scope"}
                             for index, source in enumerate(sources)],
            "dispositions": [{"source_id": source["source_id"], "classification": "requirements",
                              "reason": "Unit fixture requirement"} for source in sources],
            "unresolved": [], "cross_references": [],
        }

    def run(self, prompt, *, cwd, run_dir, schema, attempt_id):
        directory = Path(run_dir)
        if "results" in schema["properties"]:
            context = json.loads(prompt.splitlines()[-1])
            response = {"results": [{"task_id": member["task_id"], "candidate": self.candidate(member)}
                                     for member in context["members"]]}
        else:
            context = json.loads((directory / "context.json").read_text(encoding="utf-8"))
            response = self.candidate(context)
        if self.mutate:
            response = self.mutate(copy.deepcopy(response))
        effective = _effective_prompt(prompt, schema, self.agent)
        session = f"unit-shared-session-{len(self.calls)}"
        self.calls.append({"prompt": effective, "context": context, "response": response,
                           "attempt_id": attempt_id, "session_id": session, "directory": directory})
        (directory / "prompt.txt").write_text(effective, encoding="utf-8")
        (directory / "events.jsonl").write_text(json.dumps({"type": "unit_candidate_only", "session_id": session}), encoding="utf-8")
        result = AgentResult(self.agent, session, attempt_id, 0, 0.01, response,
                             str(directory / "events.jsonl"), "", len(effective), ["unit-fixture"],
                             {"input_tokens": 900})
        save_json(directory / "response.json", response)
        save_json(directory / "receipt.json", result.to_dict())
        if self.after_run:
            self.after_run(context)
        return result


class BundledModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.spec = self.root / "spec.json"
        self.spec.write_text(json.dumps({"modules": [{"name": f"fifo_{index}",
            "requirements": "The FIFO shall preserve accepted input order.\n"} for index in range(8)]}), encoding="utf-8")
        self.project = DVProject(self.root)
        self.project.import_spec(self.spec, name="Bundle test fixture", scope="FIFO order",
                                  chunk_chars=800, context_chars=14000)
        self.assertGreater(len(self.project.store.ready_tasks()), 5)

    def tearDown(self):
        self.temp.cleanup()

    def work(self, adapter, **kwargs):
        with patch("loopx.capabilities.chip_verification.engine.batch.runtime", return_value=adapter), patch("loopx.capabilities.chip_verification.engine.workflow.runtime", return_value=adapter):
            return work_bundled(self.project, **kwargs)

    def test_three_tasks_share_one_real_identity_but_keep_independent_attempts(self):
        adapter = CandidateRuntime()
        results = self.work(adapter, max_tasks=3)
        self.assertEqual(["done"] * 3, [result["status"] for result in results])
        self.assertEqual(1, len(adapter.calls))
        self.assertEqual(3, len({result["receipt"]["attempt_id"] for result in results}))
        self.assertEqual(1, len({result["session_id"] for result in results}))
        self.assertEqual(3, len(self.project.store.list_tasks(status="done", kind="model")))
        origin = adapter.calls[0]["directory"] / "receipt.json"
        for result in results:
            artifacts = self.project.store.list_artifacts(task_id=result["task_id"])
            member = next(artifact for artifact in artifacts if Path(artifact["path"]).name == "receipt.json")
            receipt = json.loads((self.root / member["path"]).read_text(encoding="utf-8"))
            self.assertEqual("bundle_member_receipt", receipt["record_type"])
            self.assertEqual(adapter.calls[0]["attempt_id"], receipt["bundle_invocation_id"])
            self.assertEqual(result["receipt"]["attempt_id"], receipt["attempt_id"])
            self.assertEqual(3, len(receipt["member_attempt_ids"]))
            self.assertEqual(origin.read_bytes(), Path(receipt["origin_receipt_copy"]).read_bytes())
        report = write_report(self.project, "report")
        summary = json.loads(Path(report["summary"]).read_text(encoding="utf-8"))
        self.assertEqual(1, summary["unique_agent_sessions"])
        self.assertEqual(3, len(summary["agent_receipts"]))
        self.assertEqual([], summary["excluded_agent_receipts"])

    def test_source_content_is_never_merged_or_truncated(self):
        adapter = CandidateRuntime()
        self.work(adapter, max_tasks=3)
        members = adapter.calls[0]["context"]["members"]
        observed = set()
        for member in members:
            task = self.project.store.get_task(member["task_id"])
            sources = {source["source_id"]: source for source in member["sources"]}
            self.assertEqual(set(task["payload"]["source_ids"]), set(sources))
            for source_id, source in sources.items():
                self.assertEqual(self.project.store.get_source(source_id)["text"], source["text"])
            self.assertFalse(observed & sources.keys())
            observed.update(sources)

    def test_structural_parents_remain_context_without_duplicate_dispositions(self):
        self.spec.write_text(json.dumps({"description": "Reset applies to every module.",
            "modules": [{"name": f"fifo_{index}", "requirements": "Preserve accepted order."}
                        for index in range(3)]}), encoding="utf-8")
        self.project.import_spec(self.spec, name="Bundle test fixture", scope="FIFO order")
        ready = self.project.store.ready_tasks(10000)
        members = [task for task in ready if task["payload"].get("dependency_source_ids")]
        self.assertEqual(3, len(members))
        for task in ready:
            if task not in members:
                self.project.store.block(task["id"], "Fixture selects child module tasks")
        adapter = CandidateRuntime()
        results = self.work(adapter, max_tasks=3)
        self.assertEqual(["done"] * 3, [result["status"] for result in results])
        self.assertIn("Dependency sources supply shared context", adapter.calls[0]["prompt"])
        for member in adapter.calls[0]["context"]["members"]:
            owned = set(member["contract"]["acceptance"]["owned_source_ids"])
            self.assertLess(owned, {source["source_id"] for source in member["sources"]})
            candidate = next(entry["candidate"] for entry in adapter.calls[0]["response"]["results"]
                             if entry["task_id"] == member["task_id"])
            self.assertEqual(owned, {entry["source_id"] for entry in candidate["dispositions"]})

    def test_missing_duplicate_and_unknown_results_reject_every_owned_member(self):
        mutations = [lambda value: {"results": value["results"][:-1]},
                     lambda value: {"results": [value["results"][0]] * len(value["results"])},
                     lambda value: {"results": [{**entry, "task_id": "unrequested"} for entry in value["results"]]}]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                results = self.work(CandidateRuntime(mutate=mutate), max_tasks=3)
                self.assertEqual(["failed"] * 3, [result["status"] for result in results])
                self.assertEqual([], self.project.store.list_tasks(status="done", kind="model"))
                self.assertEqual([], self.project.store.list_tasks(status="running", kind="model"))
        self.assertEqual(["done"] * 3, [result["status"] for result in self.work(CandidateRuntime(), max_tasks=3)])

    def test_one_cross_member_source_rejects_all_before_any_finish(self):
        def mutate(value):
            value["results"][1]["candidate"]["requirements"][0]["source_ids"] = value["results"][0]["candidate"]["requirements"][0]["source_ids"]
            return value
        with patch.object(self.project.store, "finish", wraps=self.project.store.finish) as finish:
            results = self.work(CandidateRuntime(mutate=mutate), max_tasks=3)
        finish.assert_not_called()
        self.assertTrue(all(result["status"] == "failed" for result in results))

    def test_competing_claim_is_untouched_and_remaining_members_continue(self):
        first = self.project.store.ready_tasks()[0]
        other = StateStore(self.root)
        claim = self.project.store.claim
        foreign = {}
        def race(task_id, *args, **kwargs):
            if task_id == first["id"] and not foreign:
                foreign.update(other.claim(task_id, "competing-worker", lease_seconds=600))
            return claim(task_id, *args, **kwargs)
        adapter = CandidateRuntime()
        with patch.object(self.project.store, "claim", side_effect=race):
            results = self.work(adapter, max_tasks=3)
        self.assertEqual(["contended", "done", "done"], [result["status"] for result in results])
        self.assertEqual(2, len(adapter.calls[0]["context"]["members"]))
        current = other.get_task(first["id"])
        self.assertEqual("running", current["status"])
        self.assertEqual(foreign["id"], current["attempt_id"])

    def test_partial_commit_is_recoverable_without_reexecuting_accepted_member(self):
        finish = self.project.store.finish
        calls = 0
        def interrupt(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("Unit fixture: interrupted second acceptance")
            return finish(*args, **kwargs)
        with patch.object(self.project.store, "finish", side_effect=interrupt):
            results = self.work(CandidateRuntime(), max_tasks=3)
        self.assertEqual(["done", "failed", "failed"], [result["status"] for result in results])
        accepted = self.project.store.get_task(results[0]["task_id"])
        self.project = DVProject(self.root)
        next_adapter = CandidateRuntime()
        resumed = self.work(next_adapter, max_tasks=2)
        self.assertEqual(["done", "done"], [result["status"] for result in resumed])
        self.assertEqual(accepted, self.project.store.get_task(accepted["id"]))
        self.assertNotIn(accepted["id"], [member["task_id"] for member in next_adapter.calls[0]["context"]["members"]])

    def test_single_semantic_task_ignores_legacy_budget_without_truncation(self):
        config = self.project.config
        config["context_chars"] = 1
        save_json(self.project.config_path, config)
        adapter = CandidateRuntime()
        with patch.object(self.project, "execute", wraps=self.project.execute) as execute:
            results = self.work(adapter, max_tasks=1)
        execute.assert_called_once()
        self.assertEqual("done", results[0]["status"])
        self.assertNotIn("members", adapter.calls[0]["context"])
        self.assertGreater(len(adapter.calls[0]["prompt"]), config["context_chars"])
        self.assertEqual(len(self.project.store.get_task(results[0]["task_id"])["payload"]["source_ids"]),
                         len(adapter.calls[0]["context"]["sources"]))

    def test_pi_schema_and_semantic_members_are_kept_despite_legacy_budget(self):
        ready = self.project.store.ready_tasks(10000)
        tasks = ready[:3]
        for task in ready[3:]:
            self.project.store.block(task["id"], "Unit fixture reserves exactly three eligible members")
        members = [{"task": task, "context": self.project.task_context(task)[2]} for task in tasks]
        prompt, _ = _bundle_prompt(self.project, members, "bundle_" + "0" * 32)
        schema = _schema([task["id"] for task in tasks])
        config = self.project.config
        config["context_chars"] = len(_effective_prompt(prompt, schema, "pi")) - 20
        save_json(self.project.config_path, config)
        adapter = CandidateRuntime(agent="pi")
        results = self.work(adapter, agent="pi", max_tasks=3)
        self.assertTrue(all(result["status"] == "done" for result in results))
        self.assertEqual(1, len(adapter.calls))
        self.assertGreater(len(adapter.calls[0]["prompt"]), config["context_chars"])
        self.assertEqual(3, len(adapter.calls[0]["context"]["members"]))

    def test_input_change_during_shared_invocation_rejects_all_members(self):
        def change(_):
            self.spec.write_text('{"changed":true}', encoding="utf-8")
        results = self.work(CandidateRuntime(after_run=change), max_tasks=3)
        self.assertTrue(all(result["status"] == "failed" for result in results))
        self.assertEqual([], self.project.store.list_tasks(status="done", kind="model"))
        self.assertEqual([], self.project.store.list_tasks(status="running", kind="model"))

    def test_max_tasks_and_bundle_limit_are_both_respected(self):
        adapter = CandidateRuntime()
        notified = []
        results = self.work(adapter, max_tasks=4, max_bundle_tasks=2, notify=notified.append)
        self.assertEqual(4, len(results))
        self.assertEqual(2, len(adapter.calls))
        self.assertEqual([2, 2], [len(call["context"]["members"]) for call in adapter.calls])
        self.assertEqual(results, notified)
        self.assertEqual([], self.work(adapter, max_tasks=0))

    def test_plan_bundle_preserves_requirements_and_does_not_claim_ready_models(self):
        self.work(CandidateRuntime(), max_tasks=3)
        pending_models = self.project.store.list_tasks(status="pending", kind="model")
        self.assertTrue(pending_models)
        adapter = CandidateRuntime()
        results = self.work(adapter, max_tasks=3, kind="plan")
        self.assertEqual(["done"] * 3, [result["status"] for result in results])
        self.assertEqual(1, len(adapter.calls))
        self.assertEqual(3, len(adapter.calls[0]["context"]["members"]))
        self.assertLessEqual(len(adapter.calls[0]["prompt"]), 14000)
        for member in adapter.calls[0]["context"]["members"]:
            task = self.project.store.get_task(member["task_id"])
            self.assertEqual("plan", task["kind"])
            self.assertEqual(task["payload"]["requirements"], member["requirements"])
            self.assertNotIn("sources", member)
        self.assertEqual(pending_models, self.project.store.list_tasks(status="pending", kind="model"))

    def test_one_plan_cross_member_reference_rejects_all_before_acceptance(self):
        self.work(CandidateRuntime(), max_tasks=3)
        def mutate(value):
            value["results"][1]["candidate"]["testpoints"][0]["requirement_ids"] = value["results"][0]["candidate"]["testpoints"][0]["requirement_ids"]
            return value
        with patch.object(self.project.store, "finish", wraps=self.project.store.finish) as finish:
            results = self.work(CandidateRuntime(mutate=mutate), max_tasks=3, kind="plan")
        finish.assert_not_called()
        self.assertEqual(["failed"] * 3, [result["status"] for result in results])
        self.assertEqual([], self.project.store.list_tasks(status="done", kind="plan"))

    def test_missing_plan_checker_cannot_be_hidden_by_other_valid_members(self):
        self.work(CandidateRuntime(), max_tasks=3)
        def mutate(value):
            value["results"][-1]["candidate"]["testpoints"][0]["checkers"] = []
            return value
        results = self.work(CandidateRuntime(mutate=mutate), max_tasks=3, kind="plan")
        self.assertTrue(all(result["status"] == "failed" for result in results))
        self.assertEqual([], self.project.store.list_tasks(status="done", kind="plan"))
        self.project = DVProject(self.root)
        self.assertEqual(["done"] * 3, [result["status"] for result in self.work(CandidateRuntime(), max_tasks=3, kind="plan")])

    def test_mixed_or_unknown_bundle_kind_is_rejected_before_dispatch(self):
        adapter = CandidateRuntime()
        for kind in ("model,plan", "architecture", None):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.work(adapter, max_tasks=3, kind=kind)
        self.assertEqual([], adapter.calls)


if __name__ == "__main__":
    unittest.main()
