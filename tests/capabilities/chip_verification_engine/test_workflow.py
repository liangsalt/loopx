"""Deterministic integration tests; no model or simulator claims are mocked true.

The fake runtime supplies candidate documents only. The real workflow validators,
SQLite transitions, semantic scope, file evidence and restart path are exercised.
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

    def test_legacy_length_reimport_preserves_existing_state_and_partition(self):
        before_config = self.project.config_path.read_bytes()
        before_tasks = self.project.store.list_tasks()
        before_sources = self.project.store.list_sources(include_text=True)
        before_manifests = sorted((self.root / ".loopx" / "chip-verification" / "specs").rglob("manifest.json"))
        self.project.import_spec(self.spec, name="FIFO", scope="FIFO ordering",
                                 chunk_chars=1, context_chars=1)
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

    def test_single_large_unstructured_leaf_remains_complete(self):
        text = "A valid input handshake enqueues exactly one word.\n" * 80
        self.spec.write_text(json.dumps({"behavior": text}), encoding="utf-8")
        result = self.project.import_spec(self.spec, name="FIFO", scope="FIFO ordering",
                                           chunk_chars=800, context_chars=6000)
        self.assertEqual(result["sources"], 1)
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
                    requirement["features"] = [f"operation-{index % 2}"]
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

    def test_function_scopes_are_losslessly_partitioned_and_all_planned(self):
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
        def by_id(items):
            return sorted(items, key=lambda item: item["id"])
        self.assertEqual(by_id(original["payload"]["requirements"]),
                         by_id([requirement for task in parts for requirement in task["payload"]["requirements"]]))
        for task in parts:
            self.assertEqual(original["depends_on"], task["depends_on"])
            self.assertEqual(original["payload"]["source_ids"], task["payload"]["source_ids"])
            self.assertEqual(1, len({tuple(req["features"]) for req in task["payload"]["requirements"]}))
        self.assertEqual({original["id"]}, superseded_plan_ids(tasks))
        self.assertTrue(all(task["status"] == "done" for task in effective_planning_tasks(tasks)))
        exported = self.project.export_plan()
        self.assertTrue(exported["planning_complete"])
        self.assertEqual(8, exported["testpoints"])
        self.assertTrue(any(len(call["prompt"]) > 6000 for call in adapter.calls))

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
            if "__scope_" in kwargs.get("task_id", ""):
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

    def test_indivisible_requirement_runs_complete_without_length_blocker(self):
        adapter = DocumentRuntime(self.expanded_requirements(count=3, huge_first=True))
        self.run_work(adapter)
        self.assertEqual([], self.project.store.list_tasks(status="blocked", kind="plan"))
        plans = [task for task in self.project.store.list_tasks(kind="plan") if task["status"] == "done"]
        self.assertEqual(2, len(plans))
        self.assertTrue(any(len(req["expected"]) > 8000 for task in plans for req in task["payload"]["requirements"]))
        self.assertEqual([], self.project.store.list_issues())
        self.assertTrue(self.project.export_plan()["planning_complete"])
        self.assertEqual(3, self.project.export_plan()["testpoints"])
        self.assertTrue(any(len(call["prompt"]) > 6000 for call in adapter.calls))
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

    def test_legacy_length_change_cannot_duplicate_completed_function_scopes(self):
        self.run_work(DocumentRuntime(self.expanded_requirements(count=3, huge_first=True)))
        before = self.project.store.list_tasks(kind="plan")
        baseline = self.project.store.baseline()
        self.project.import_spec(self.spec, name="FIFO", scope="FIFO ordering", chunk_chars=1, context_chars=1)
        self.assertEqual(baseline, self.project.store.baseline())
        self.project.reconcile()
        self.assertEqual(before, self.project.store.list_tasks(kind="plan"))
        self.assertEqual([], self.project.store.list_issues())
        self.assertEqual([], self.run_work(DocumentRuntime(), kinds=["plan"]))
        self.assertTrue(self.project.export_plan()["planning_complete"])
        self.assertEqual(3, self.project.export_plan()["testpoints"])

    def test_legacy_length_hold_recovers_without_resolving_other_issues(self):
        self.run_work(DocumentRuntime(), kinds=["model"])
        plan = self.project.store.list_tasks(kind="plan")[0]
        reason = "Planning context exceeds budget: one complete requirement needs 10000 characters, budget 6000."
        self.project.store.block(plan["id"], reason)
        old_issue = self.project.store.add_issue("context_blocker", reason, task_id=plan["id"])
        older_issue = self.project.store.add_issue("context_blocker",
            "Planning context exceeds budget: previous complete requirement needs 10000 characters, budget 4000.",
            task_id=plan["id"])
        real_issue = self.project.store.add_issue("context_blocker", "Missing clock definition", task_id=plan["id"])
        self.project.reconcile()
        recovered = self.project.store.get_task(plan["id"])
        self.assertEqual("pending", recovered["status"])
        self.assertEqual(plan["payload"], recovered["payload"])
        self.assertEqual([real_issue["id"]], [issue["id"] for issue in self.project.store.list_issues()])
        retired = [issue for issue in self.project.store.list_issues(status=None)
                   if issue["id"] in {old_issue["id"], older_issue["id"]}]
        self.assertEqual(2, len(retired))
        self.assertTrue(all(issue["status"] == "resolved" for issue in retired))
        before = self.project.store.events(limit=10000)
        self.project.reconcile()
        self.assertEqual(before, self.project.store.events(limit=10000))

    def test_legacy_plan_recovery_preserves_real_block_and_concurrent_lease(self):
        with patch.object(self.project, "_materialize_plan", side_effect=self.make_legacy_plan):
            self.run_work(DocumentRuntime(self.expanded_requirements()), kinds=["model"])
        plan = self.project.store.list_tasks(kind="plan")[0]
        reason = "Planning context exceeds budget: legacy complete input."
        blocked = self.project.store.block(plan["id"], reason)
        issue = self.project.store.add_issue("context_blocker", reason, task_id=plan["id"])
        self.project.store.requeue(plan["id"])
        self.project.store.claim(plan["id"], "live-plan-worker")
        current, recovered = self.project.store.recover_context(blocked, [issue], "Length limit retired.")
        self.assertFalse(recovered)
        self.assertEqual("running", current["status"])
        self.project.reconcile()
        self.assertEqual(current, self.project.store.get_task(plan["id"]))
        self.project.store.block(plan["id"], "Missing reset semantics")
        held = self.project.store.get_task(plan["id"])
        self.project.reconcile()
        self.assertEqual(held, self.project.store.get_task(plan["id"]))
        self.assertEqual([issue["id"]], [item["id"] for item in self.project.store.list_issues()])

    def test_legacy_bounded_partition_releases_only_length_blocked_child(self):
        with patch.object(self.project, "_materialize_plan", side_effect=self.make_legacy_plan):
            self.run_work(DocumentRuntime(self.expanded_requirements()), kinds=["model"])
        parent = self.project.store.list_tasks(kind="plan")[0]
        requirements = parent["payload"]["requirements"]
        children = []
        for index, items in enumerate((requirements[:4], requirements[4:])):
            children.append(self.project.store.add_task("plan", f"Legacy part {index}",
                {**parent["payload"], "requirements": items, "supersedes": parent["id"], "partition_id": "legacy",
                 "partition_index": index, "partition_count": 2}, depends_on=parent["depends_on"],
                task_id=parent["id"] + f"__part_legacy_{index}"))
        self.project.store.cancel(parent["id"], "Superseded by bounded plan partition legacy")
        reason = "Planning context exceeds budget: retained requirement batch."
        self.project.store.block(children[0]["id"], reason)
        self.project.store.add_issue("context_blocker", reason, task_id=children[0]["id"])
        self.project.store.block(children[1]["id"], "Missing independent checker definition")
        untouched = self.project.store.get_task(children[1]["id"])
        self.project.reconcile()
        self.assertEqual("pending", self.project.store.get_task(children[0]["id"])["status"])
        self.assertEqual(untouched, self.project.store.get_task(children[1]["id"]))
        self.assertEqual("cancelled", self.project.store.get_task(parent["id"])["status"])
        self.assertEqual(3, len(self.project.store.list_tasks(kind="plan")))
        self.assertEqual([], self.project.store.list_issues())

    def test_function_plans_receive_relevant_environment_without_new_coverage_targets(self):
        def add_environment(kind, value):
            value = self.expanded_requirements(count=2)(kind, value)
            if kind == "model":
                prototype = value["requirements"][0]
                for label, modules in (("clock", []), ("reset", ["fifo"]), ("unrelated", ["other_ip"])):
                    value["requirements"].append({**copy.deepcopy(prototype), "local_id": label,
                        "modules": modules, "features": [label], "statement": label + " environment",
                        "applicability": "context"})
            return value
        adapter = DocumentRuntime(add_environment)
        self.run_work(adapter)
        behavioral = [call for call in adapter.calls if call["kind"] == "plan" and any(
            req["applicability"] == "in_scope" for req in call["context"]["requirements"])]
        self.assertEqual(2, len(behavioral))
        for call in behavioral:
            self.assertEqual({"clock", "reset"}, {req["local_id"] for req in call["context"]["context_requirements"]})
            self.assertIn("not independent coverage targets", call["prompt"])
        self.assertEqual(2, self.project.export_plan()["testpoints"])
        plan = next(task for task in self.project.store.list_tasks(kind="plan", status="done")
                    if any(req["applicability"] == "in_scope" for req in task["payload"]["requirements"]))
        result = copy.deepcopy(plan["result"])
        environment_id = plan["payload"]["context_requirements"][0]["id"]
        result["testpoints"][0]["requirement_ids"].append(environment_id)
        self.project._validate_plan(plan, result, [])
        result["testpoints"][0]["requirement_ids"] = [environment_id]
        with self.assertRaisesRegex(Exception, "owned in-scope"):
            self.project._validate_plan(plan, result, [])
        before = self.project.store.list_tasks(kind="plan")
        self.project.reconcile()
        self.assertEqual(before, self.project.store.list_tasks(kind="plan"))


if __name__ == "__main__":
    unittest.main()
