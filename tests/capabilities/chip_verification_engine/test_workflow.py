"""Deterministic integration tests; no model or simulator claims are mocked true.

The fake runtime supplies candidate documents only. The real workflow validators,
SQLite transitions, context budget, file evidence and restart path are exercised.
"""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from loopx.capabilities.chip_verification.engine.agents import AgentResult
from loopx.capabilities.chip_verification.engine.workflow import DVProject, effective_planning_tasks, superseded_plan_ids


class DocumentRuntime:
    def __init__(self, mutate=None, after_run=None):
        self.mutate = mutate
        self.after_run = after_run
        self.calls = []

    def run(self, prompt, *, cwd, run_dir, schema, attempt_id):
        directory = Path(run_dir)
        context = json.loads((directory / "context.json").read_text(encoding="utf-8"))
        kind = "model" if "requirements" in schema["properties"] else "plan"
        if kind == "model":
            sources = context["sources"]
            response = {
                "requirements": [
                    {"local_id": f"R-{index}", "statement": source["text"],
                     "source_ids": [source["source_id"]], "modules": ["fifo"],
                     "features": ["queue semantics"], "interfaces": ["ready_valid"],
                     "conditions": ["outside reset"], "expected": source["text"],
                     "exceptions": [], "applicability": "in_scope"}
                    for index, source in enumerate(sources)
                ],
                "dispositions": [{"source_id": source["source_id"], "classification": "requirements",
                                  "reason": "The source explicitly describes FIFO behavior"} for source in sources],
                "unresolved": [], "cross_references": [],
            }
        else:
            response = {
                "testpoints": [
                    {"id": f"TP-{index}", "name": "Check FIFO observed transaction order",
                     "requirement_ids": [requirement["id"]], "level": "ip",
                     "stimulus": "Enqueue distinct values while varying downstream ready",
                     "observations": ["monitor captures accepted enqueue and dequeue"],
                     "checkers": ["scoreboard compares dequeued values with reference queue"],
                     "coverage": ["observe occupancy empty, intermediate and full"],
                     "uvm_components": ["fifo_agent", "fifo_scoreboard", "fifo_sequence"],
                     "configurations": ["depth=4"], "priority": "critical"}
                    for index, requirement in enumerate(context["requirements"])
                    if requirement["applicability"] == "in_scope"
                ],
                "unresolved": [],
            }
        if self.mutate:
            response = self.mutate(kind, copy.deepcopy(response))
        session_id = f"independent-test-session-{len(self.calls)}"
        self.calls.append({"kind": kind, "prompt": prompt, "context": context,
                           "attempt_id": attempt_id, "session_id": session_id})
        (directory / "response.json").write_text(json.dumps(response), encoding="utf-8")
        (directory / "receipt.json").write_text(json.dumps({"session_id": session_id,
                                                            "kind": "unit_test_candidate_only"}), encoding="utf-8")
        (directory / "events.jsonl").write_text(json.dumps({"type": "test_candidate_returned"}) + "\n", encoding="utf-8")
        if self.after_run:
            self.after_run(kind)
        return AgentResult("test-double", session_id, attempt_id, 0, 0.0, response,
                           str(directory / "events.jsonl"), "", len(prompt), ["test-double"])


class WorkflowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.spec = self.root / "spec.json"
        self.spec.write_text(json.dumps({"behavior": "The FIFO shall return accepted words in enqueue order."}), encoding="utf-8")
        self.project = DVProject(self.root)
        self.imported = self.project.import_spec(self.spec, name="FIFO", scope="FIFO ordering",
                                                 chunk_chars=800, context_chars=6000)

    def tearDown(self):
        self.temp.cleanup()

    def run_work(self, runtime, **kwargs):
        with patch("loopx.capabilities.chip_verification.engine.workflow.runtime", return_value=runtime):
            return self.project.work(max_tasks=20, **kwargs)

    def assert_rejected_model(self, mutate):
        runtime = DocumentRuntime(mutate)
        results = self.run_work(runtime, kinds=["model"])
        self.assertEqual(1, len(results))
        self.assertEqual("failed", results[0]["status"])
        self.assertEqual([], self.project.store.list_tasks(status="done"))
        self.assertEqual([], self.project.store.list_requirements())
        self.assertEqual([], self.project.store.list_tasks(kind="plan"))
        return results[0]

    def test_import_model_plan_export_and_new_process_restart(self):
        runtime = DocumentRuntime()
        results = self.run_work(runtime)
        self.assertEqual(["done", "done"], [result["status"] for result in results])
        self.assertEqual(["model", "plan"], [call["kind"] for call in runtime.calls])
        self.assertEqual(2, len({call["session_id"] for call in runtime.calls}))
        self.assertNotEqual(runtime.calls[0]["attempt_id"], runtime.calls[1]["attempt_id"])
        self.assertTrue(all(len(call["prompt"]) < 6000 for call in runtime.calls))
        self.assertNotIn("sources", runtime.calls[1]["context"])
        exported = self.project.export_plan()
        plan = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
        self.assertTrue(plan["planning_complete"])
        self.assertEqual(1, len(plan["testpoints"]))
        self.assertEqual(1, len(plan["testpoints"][0]["requirement_ids"]))
        self.assertIn("No simulation", plan["execution_claim"])

        # A real new Python process uses only the project state, not this mock's
        # conversations or Python objects, to recover the completed plan.
        code = "import json,sys; from loopx.capabilities.chip_verification.engine.workflow import DVProject; p=DVProject(sys.argv[1]); print(json.dumps({'status':p.status(),'plan':p.export_plan()}))"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3]) + os.pathsep + env.get("PYTHONPATH", "")
        process = subprocess.run([sys.executable, "-c", code, str(self.root)], env=env,
                                 text=True, capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(0, process.returncode, process.stderr)
        recovered = json.loads(process.stdout)
        self.assertEqual({"done": 2}, recovered["status"]["tasks"])
        self.assertEqual(1, recovered["plan"]["testpoints"])
        self.assertEqual(1, recovered["status"]["requirement_count"])
        self.assertEqual(0, recovered["status"]["ready_count"])

    def test_unknown_model_source_reference_is_rejected(self):
        def mutate(kind, value):
            value["requirements"][0]["source_ids"] = ["not-in-this-task"]
            return value
        result = self.assert_rejected_model(mutate)
        self.assertIn("outside this task", result["error"])

    def test_missing_source_disposition_is_rejected(self):
        def mutate(kind, value):
            value["dispositions"] = []
            return value
        result = self.assert_rejected_model(mutate)
        self.assertIn("exactly one disposition", result["error"])

    def test_requirement_disposition_cannot_silently_discard_all_requirements(self):
        def mutate(kind, value):
            value["requirements"] = []
            return value
        self.assert_rejected_model(mutate)

    def test_whitespace_checker_does_not_pass_uvm_plan_validation(self):
        def mutate(kind, value):
            if kind == "plan":
                value["testpoints"][0]["checkers"] = ["   "]
            return value
        results = self.run_work(DocumentRuntime(mutate))
        self.assertEqual(["done", "failed"], [result["status"] for result in results])
        self.assertEqual([], self.project.store.list_tasks(status="done", kind="plan"))
        export = self.project.export_plan()
        self.assertFalse(export["planning_complete"])
        self.assertEqual(0, export["testpoints"])

    def test_missing_in_scope_requirement_cannot_be_reported_as_planned(self):
        def mutate(kind, value):
            if kind == "plan":
                value["testpoints"] = []
            return value
        results = self.run_work(DocumentRuntime(mutate))
        self.assertEqual(["done", "failed"], [result["status"] for result in results])
        self.assertIn("silently missing", results[-1]["error"])

    def test_modified_accepted_response_invalidates_derived_plan(self):
        self.run_work(DocumentRuntime())
        model = self.project.store.list_tasks(status="done", kind="model")[0]
        artifacts = self.project.store.list_artifacts(task_id=model["id"])
        response = next(artifact for artifact in artifacts if artifact["path"].endswith("response.json"))
        (self.root / response["path"]).write_text('{"tampered":true}', encoding="utf-8")
        status = self.project.status()
        self.assertEqual({"stale": 2}, status["tasks"])
        self.assertEqual(1, len(status["changed_artifacts"]))
        self.assertEqual([], self.project.store.ready_tasks())

    def test_source_mutation_during_execution_cannot_accept_old_candidate(self):
        def mutate_original(kind):
            if kind == "model":
                self.spec.write_text(json.dumps({"behavior": "Changed FIFO reset semantics."}), encoding="utf-8")
        results = self.run_work(DocumentRuntime(after_run=mutate_original), kinds=["model"])
        self.assertEqual("failed", results[0]["status"])
        self.assertEqual([], self.project.store.list_tasks(status="done"))

    def test_repartition_reimport_rejects_before_mutating_existing_state(self):
        before_config = self.project.config_path.read_bytes()
        before_tasks = self.project.store.list_tasks()
        before_sources = self.project.store.list_sources(include_text=True)
        before_manifests = sorted((self.root / ".loopx" / "chip-verification" / "specs").rglob("manifest.json"))
        with self.assertRaisesRegex(ValueError, "partition size"):
            self.project.import_spec(self.spec, name="FIFO", scope="FIFO ordering",
                                     chunk_chars=1000, context_chars=6000)
        self.assertEqual(before_config, self.project.config_path.read_bytes())
        self.assertEqual(before_tasks, self.project.store.list_tasks())
        self.assertEqual(before_sources, self.project.store.list_sources(include_text=True))
        self.assertEqual(before_manifests, sorted((self.root / ".loopx" / "chip-verification" / "specs").rglob("manifest.json")))
        self.assertTrue(all(r["status"] == "done" for r in self.run_work(DocumentRuntime())))

    def test_scope_reimport_of_pending_tasks_is_rejected_without_rebinding(self):
        before = self.project.config_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Changing scope"):
            self.project.import_spec(self.spec, name="FIFO", scope="A different DUT configuration",
                                     chunk_chars=800, context_chars=6000)
        self.assertEqual(before, self.project.config_path.read_bytes())
        self.assertTrue(all(r["status"] == "done" for r in self.run_work(DocumentRuntime())))

    def test_export_revalidates_artifacts_without_prior_status_call(self):
        for kind in ("model", "plan"):
            with self.subTest(kind=kind):
                self.run_work(DocumentRuntime())
                task = self.project.store.list_tasks(status="done", kind=kind)[0]
                artifact = next(a for a in self.project.store.list_artifacts(task_id=task["id"], validity="current")
                                if a["path"].endswith("response.json"))
                (self.root / artifact["path"]).write_text('{"tampered":true}', encoding="utf-8")
                result = self.project.export_plan()
                self.assertFalse(result["planning_complete"])
                self.assertEqual(0, result["testpoints"])
                # Requeue the invalidated tasks to obtain a new, independently
                # accepted response for the other mutation boundary.
                for stale in self.project.store.list_tasks(status="stale"):
                    self.project.store.requeue(stale["id"])

    def test_export_refuses_changed_original_spec(self):
        self.run_work(DocumentRuntime())
        self.spec.write_text('{"behavior":"changed behavior"}', encoding="utf-8")
        with self.assertRaisesRegex(Exception, "Original spec changed"):
            self.project.export_plan()

    def test_reimport_changed_spec_creates_new_work_and_preserves_old_history(self):
        self.run_work(DocumentRuntime())
        old_baseline = self.project.store.baseline()["id"]
        old_tasks = self.project.store.list_tasks()
        self.spec.write_text(json.dumps({"behavior": "The FIFO shall clear its occupancy on reset."}), encoding="utf-8")
        new_import = self.project.import_spec(self.spec, name="FIFO", scope="FIFO ordering",
                                               chunk_chars=800, context_chars=6000)
        self.assertNotEqual(old_baseline, new_import["baseline"]["id"])
        self.assertEqual(1, self.project.store.status()["ready_count"])
        self.assertTrue(all(self.project.store.get_task(task["id"])["status"] == "stale" for task in old_tasks))
        self.assertEqual([], self.project.store.list_requirements())

    def test_single_large_leaf_is_imported_as_multiple_sources(self):
        text = "A valid input handshake enqueues exactly one word.\n" * 80
        self.spec.write_text(json.dumps({"behavior": text}), encoding="utf-8")
        result = self.project.import_spec(self.spec, name="FIFO", scope="FIFO ordering",
                                           chunk_chars=800, context_chars=6000)
        self.assertGreater(result["sources"], 1)
        self.assertEqual(result["sources"], self.project.store.status()["source_count"])
        sources = self.project.store.list_sources(include_text=True)
        sources.sort(key=lambda source: source["metadata"]["start_char"])
        self.assertEqual(text, "".join(source["text"] for source in sources))

    def test_blocking_plan_question_prevents_planning_complete(self):
        def mutate(kind, value):
            if kind == "plan":
                refs = value["testpoints"][0]["requirement_ids"]
                value["testpoints"] = []
                value["unresolved"] = [{"requirement_ids": refs,
                                        "question": "Required reset timing is unspecified", "blocking": True}]
            return value
        results = self.run_work(DocumentRuntime(mutate))
        self.assertTrue(all(result["status"] == "done" for result in results))
        exported = self.project.export_plan()
        self.assertFalse(exported["planning_complete"])
        issues = self.project.store.list_issues()
        self.assertEqual(["plan_blocker"], [issue["category"] for issue in issues])
        self.project.reconcile()
        self.assertEqual(1, len(self.project.store.list_issues()))

    def test_uncertain_applicability_is_a_durable_open_question(self):
        def mutate(kind, value):
            if kind == "model":
                value["requirements"][0]["applicability"] = "uncertain"
            return value
        self.run_work(DocumentRuntime(mutate))
        self.assertFalse(self.project.export_plan()["planning_complete"])
        self.assertEqual("spec_blocker", self.project.store.list_issues()[0]["category"])

    def expanded_requirements(self, count=8, huge_first=False):
        def mutate(kind, value):
            if kind == "model":
                prototype = value["requirements"][0]
                value["requirements"] = []
                for index in range(count):
                    requirement = copy.deepcopy(prototype)
                    requirement["local_id"] = f"R-{index}"
                    requirement["statement"] = f"Scenario {index}: " + "preserve transaction order; " * 30
                    requirement["expected"] = "Each observed output matches the independently tracked input. " * (150 if huge_first and index == 0 else 12)
                    value["requirements"].append(requirement)
            return value
        return mutate

    def make_legacy_plan(self, model, requirements):
        config = self.project.config
        self.project.store.add_task("plan", "Plan UVM tests for " + model["title"],
            {"requirements": requirements, "scope": config["scope"],
             "source_ids": model["payload"]["source_ids"], "spec_sha256": config["spec_sha256"]},
            depends_on=[model["id"]], task_id="plan_" + model["id"])
        return 1

    def test_plan_that_fits_retains_existing_identity_and_payload(self):
        self.run_work(DocumentRuntime(), kinds=["model"])
        model = self.project.store.list_tasks(status="done", kind="model")[0]
        plans = self.project.store.list_tasks(kind="plan")
        self.assertEqual(["plan_" + model["id"]], [task["id"] for task in plans])
        self.assertNotIn("partition_id", plans[0]["payload"])
        before = copy.deepcopy(plans)
        self.project.reconcile()
        self.assertEqual(before, self.project.store.list_tasks(kind="plan"))

    def test_large_requirement_batch_is_losslessly_split_and_all_parts_planned(self):
        adapter = DocumentRuntime(self.expanded_requirements())
        results = self.run_work(adapter)
        self.assertTrue(all(result["status"] == "done" for result in results))
        tasks = self.project.store.list_tasks()
        originals = [task for task in tasks if task["status"] == "cancelled"]
        self.assertEqual(1, len(originals))
        original = originals[0]
        parts = [task for task in tasks if task["payload"].get("supersedes") == original["id"]]
        self.assertGreater(len(parts), 1)
        parts.sort(key=lambda task: task["payload"]["partition_index"])
        self.assertEqual(original["payload"]["requirements"],
                         [requirement for task in parts for requirement in task["payload"]["requirements"]])
        for task in parts:
            self.assertEqual(original["depends_on"], task["depends_on"])
            self.assertEqual(original["payload"]["source_ids"], task["payload"]["source_ids"])
            self.assertLessEqual(len(self.project.task_context(task)[0]), self.project.config["context_chars"])
        self.assertEqual({original["id"]}, superseded_plan_ids(tasks))
        self.assertTrue(all(task["status"] == "done" for task in effective_planning_tasks(tasks)))
        exported = self.project.export_plan()
        self.assertTrue(exported["planning_complete"])
        self.assertEqual(8, exported["testpoints"])
        self.assertTrue(all(len(call["prompt"]) <= 6000 for call in adapter.calls))

    def test_legacy_oversized_pending_plan_is_superseded_and_old_writer_is_compatible(self):
        adapter = DocumentRuntime(self.expanded_requirements())
        with patch.object(self.project, "_materialize_plan", side_effect=self.make_legacy_plan):
            self.run_work(adapter, kinds=["model"])
        old = self.project.store.list_tasks(kind="plan")[0]
        self.assertEqual("pending", old["status"])
        self.assertGreater(len(self.project._plan_context(old["id"], old["payload"]["requirements"])[0]), 6000)
        self.project = DVProject(self.root)
        self.project.reconcile()
        task_count = len(self.project.store.list_tasks())
        # An already running old process can still retry its deterministic add.
        returned = self.project.store.add_task("plan", old["title"], old["payload"],
                                                depends_on=old["depends_on"], task_id=old["id"])
        self.assertEqual("cancelled", returned["status"])
        self.project.reconcile()
        self.assertEqual(task_count, len(self.project.store.list_tasks()))
        self.run_work(DocumentRuntime(), kinds=["plan"])
        self.assertTrue(self.project.export_plan()["planning_complete"])

    def test_crash_during_partition_materialization_recovers_without_missing_requirements(self):
        with patch.object(self.project, "_materialize_plan", side_effect=self.make_legacy_plan):
            self.run_work(DocumentRuntime(self.expanded_requirements()), kinds=["model"])
        old = self.project.store.list_tasks(kind="plan")[0]
        add = self.project.store.add_task
        created = 0

        def crash(*args, **kwargs):
            nonlocal created
            if "__part_" in kwargs.get("task_id", ""):
                created += 1
                if created == 2:
                    raise RuntimeError("Unit fixture crash after first child persisted")
            return add(*args, **kwargs)

        with patch.object(self.project.store, "add_task", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "fixture crash"):
                self.project.reconcile()
        self.assertEqual("pending", self.project.store.get_task(old["id"])["status"])
        self.assertEqual(set(), superseded_plan_ids(self.project.store.list_tasks()))
        self.project = DVProject(self.root)
        self.project.reconcile()
        self.assertEqual({old["id"]}, superseded_plan_ids(self.project.store.list_tasks()))
        self.run_work(DocumentRuntime(), kinds=["plan"])
        self.assertEqual(8, self.project.export_plan()["testpoints"])

    def test_indivisible_requirement_is_blocked_without_truncating_other_work(self):
        adapter = DocumentRuntime(self.expanded_requirements(count=3, huge_first=True))
        self.run_work(adapter)
        blocked = self.project.store.list_tasks(status="blocked", kind="plan")
        self.assertEqual(1, len(blocked))
        self.assertGreater(len(blocked[0]["payload"]["requirements"][0]["expected"]), 8000)
        self.assertIn("no text was truncated", blocked[0]["reason"])
        self.assertTrue(any(task["status"] == "done" for task in self.project.store.list_tasks(kind="plan")))
        self.assertEqual(["context_blocker"], [issue["category"] for issue in self.project.store.list_issues()])
        self.assertFalse(self.project.export_plan()["planning_complete"])
        self.assertTrue(all(len(call["prompt"]) <= 6000 for call in adapter.calls))
        before = len(self.project.store.events(limit=10000))
        self.project.reconcile()
        self.assertEqual(before, len(self.project.store.events(limit=10000)))

    def test_cancelled_plan_is_not_complete_without_every_lossless_replacement(self):
        self.run_work(DocumentRuntime(self.expanded_requirements()))
        tasks = self.project.store.list_tasks()
        parent = next(task for task in tasks if task["status"] == "cancelled")
        parts = [task for task in tasks if task["payload"].get("supersedes") == parent["id"]]
        self.assertEqual({parent["id"]}, superseded_plan_ids(tasks))
        incomplete = [task for task in tasks if task["id"] != parts[0]["id"]]
        self.assertEqual(set(), superseded_plan_ids(incomplete))
        corrupted = copy.deepcopy(tasks)
        child = next(task for task in corrupted if task["id"] == parts[0]["id"])
        child["payload"]["requirements"][0]["expected"] = "A changed requirement is not the original plan scope"
        self.assertEqual(set(), superseded_plan_ids(corrupted))

    def test_explicit_budget_increase_resumes_blocked_part_without_duplicating_completed_parts(self):
        self.run_work(DocumentRuntime(self.expanded_requirements(count=3, huge_first=True)))
        before = self.project.store.list_tasks(kind="plan")
        completed = [task for task in before if task["status"] == "done"]
        self.assertTrue(completed)
        config = self.project.config
        config["context_chars"] = 24000
        self.project.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.project = DVProject(self.root)
        self.project.reconcile()
        self.assertEqual(len(before), len(self.project.store.list_tasks(kind="plan")))
        self.assertEqual([], self.project.store.list_tasks(status="blocked", kind="plan"))
        self.assertEqual([], self.project.store.list_issues())
        for task in completed:
            self.assertEqual(task, self.project.store.get_task(task["id"]))
        results = self.run_work(DocumentRuntime(), kinds=["plan"])
        self.assertEqual(1, len(results))
        self.assertTrue(self.project.export_plan()["planning_complete"])
        self.assertEqual(3, self.project.export_plan()["testpoints"])


if __name__ == "__main__":
    unittest.main()
