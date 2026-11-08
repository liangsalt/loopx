import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from loopx.capabilities.chip_verification.engine.models import ConflictError, StateError, ValidationError
from loopx.capabilities.chip_verification.engine.state import StateStore, file_hash


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spec = self.root / "spec.json"
        self.spec.write_text('{"requirement":"observe real DUT transactions"}', encoding="utf-8")
        self.store = StateStore(self.root)
        self.baseline = self.store.initialize("test", self.spec, file_hash(self.spec), self.spec.stat().st_size)

    def tearDown(self):
        self.tmp.cleanup()

    def artifact(self, name="evidence.json", value=None):
        path = self.root / name
        path.write_text(json.dumps(value or {"checks": 4, "errors": 0}), encoding="utf-8")
        return path

    def validator(self, task, result, artifacts):
        data = json.loads((self.root / artifacts[0]["path"]).read_text(encoding="utf-8"))
        return {"passed": data["checks"] > 0 and data["errors"] == 0,
                "validator": "observed-checks", "checks": ["checks executed", "no check failures"]}

    def accept(self, task, name="evidence.json"):
        attempt = self.store.claim(task["id"], "worker")
        path = self.artifact(name)
        return self.store.finish(task["id"], attempt["id"], {"report": path.name}, [path], validator=self.validator)

    def test_dependency_gate_and_restart(self):
        parent = self.store.add_task("model", "extract requirements")
        child = self.store.add_task("plan", "test plan", depends_on=[parent["id"]])
        self.assertEqual([parent["id"]], [task["id"] for task in self.store.ready_tasks()])
        with self.assertRaises(StateError):
            self.store.claim(child["id"], "too-early")
        self.accept(parent)
        recovered = StateStore(self.root)
        self.assertEqual([child["id"]], [task["id"] for task in recovered.ready_tasks()])
        self.assertEqual("evidence.json", recovered.get_task(parent["id"])["result"]["report"])

    def test_inspection_reads_existing_state_without_migration_or_mutation(self):
        task = self.store.add_task("model", "extract bounded requirements")
        before = self.store.db_path.read_bytes()
        recovered = StateStore(self.root, initialize=False)
        self.assertEqual([task["id"]], [item["id"] for item in recovered.ready_tasks()])
        self.assertEqual(before, self.store.db_path.read_bytes())
        with self.assertRaisesRegex(StateError, "inspection only"):
            recovered.claim(task["id"], "unexpected-writer")
        with recovered._db() as db, self.assertRaises(sqlite3.OperationalError):
            db.execute("CREATE TABLE unexpected_write(id INTEGER)")
        self.assertEqual("pending", self.store.get_task(task["id"])["status"])

    def test_inspection_of_unknown_project_creates_no_directory(self):
        unknown = self.root / "unimported-project"
        with self.assertRaisesRegex(StateError, "import a specification"):
            StateStore(unknown, initialize=False)
        self.assertFalse(unknown.exists())

    def test_two_process_equivalent_connections_cannot_claim_same_work(self):
        task = self.store.add_task("model", "only one owner")
        stores = [StateStore(self.root), StateStore(self.root)]
        barrier = threading.Barrier(2)

        def claim(index):
            barrier.wait()
            try:
                return stores[index].claim(task["id"], f"session-{index}")["id"]
            except ConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, range(2)))
        self.assertEqual(1, sum(value is not None for value in results))
        self.assertEqual("running", self.store.get_task(task["id"])["status"])

    def test_agent_claim_without_validator_is_not_completion(self):
        task = self.store.add_task("regress", "run simulation")
        attempt = self.store.claim(task["id"], "worker")
        with self.assertRaises(ValidationError):
            self.store.finish(task["id"], attempt["id"], {"passed": True, "coverage": 100}, [self.artifact()])
        self.assertEqual("running", self.store.get_task(task["id"])["status"])

    def test_validator_failure_and_missing_evidence_cannot_complete(self):
        task = self.store.add_task("smoke", "check monitor activity")
        attempt = self.store.claim(task["id"], "worker")
        with self.assertRaises(ValidationError):
            self.store.finish(task["id"], attempt["id"], {}, [], validator=self.validator)
        path = self.artifact(value={"checks": 0, "errors": 0})
        with self.assertRaises(ValidationError):
            self.store.finish(task["id"], attempt["id"], {"passed": True}, [path], validator=self.validator)
        self.assertEqual("running", self.store.get_task(task["id"])["status"])

    def test_idempotent_finish_does_not_duplicate_events_or_artifacts(self):
        task = self.store.add_task("build", "compile")
        attempt = self.store.claim(task["id"], "worker")
        path = self.artifact()
        result = {"report": path.name}
        first = self.store.finish(task["id"], attempt["id"], result, [path], validator=self.validator,
                                  operation_id="submission-1")
        second = self.store.finish(task["id"], attempt["id"], result, [path], operation_id="submission-1")
        self.assertEqual(first["evidence_id"], second["evidence_id"])
        self.assertTrue(second["replayed"])
        self.assertEqual(1, len(self.store.list_artifacts(task_id=task["id"])))
        self.assertEqual(1, sum(event["type"] == "task_accepted" for event in self.store.events()))
        with self.assertRaises(ConflictError):
            self.store.finish(task["id"], attempt["id"], {"different": True}, [path], operation_id="submission-1")

    def test_expired_worker_cannot_write_after_recovery(self):
        task = self.store.add_task("implement", "generate monitor")
        old = self.store.claim(task["id"], "dead-session", lease_seconds=60)
        self.assertEqual([task["id"]], self.store.recover_expired(now=time.time() + 120))
        new = StateStore(self.root).claim(task["id"], "replacement-session")
        with self.assertRaises(ConflictError):
            self.store.finish(task["id"], old["id"], {}, [self.artifact()], validator=self.validator)
        self.assertEqual(new["id"], self.store.get_task(task["id"])["attempt_id"])

    def test_changed_baseline_rejects_old_worker_and_preserves_history(self):
        completed = self.store.add_task("model", "done extraction")
        self.accept(completed)
        pending = self.store.add_task("plan", "old plan")
        old_attempt = self.store.claim(pending["id"], "old-session")
        self.spec.write_text('{"requirement":"changed expected timing"}', encoding="utf-8")
        current = self.store.initialize("test", self.spec, file_hash(self.spec), self.spec.stat().st_size)
        self.assertNotEqual(self.baseline["id"], current["id"])
        self.assertEqual("stale", self.store.get_task(completed["id"])["status"])
        self.assertEqual("stale", self.store.list_artifacts()[0]["validity"])
        self.assertEqual([], self.store.list_tasks())
        with self.assertRaises(ConflictError):
            self.store.finish(pending["id"], old_attempt["id"], {}, [self.artifact()], validator=self.validator)
        with self.assertRaises(ConflictError):
            self.store.requeue(pending["id"])

    def test_change_during_validation_rejects_commit(self):
        task = self.store.add_task("analyze", "evaluate coverage")
        attempt = self.store.claim(task["id"], "worker")
        path = self.artifact()

        def change_then_accept(task, result, artifacts):
            path.write_text('{"checks": 0, "errors": 1}', encoding="utf-8")
            return {"passed": True, "checks": ["pretend to accept previous content"]}

        with self.assertRaises(ConflictError):
            self.store.finish(task["id"], attempt["id"], {}, [path], validator=change_then_accept)
        self.assertEqual([], self.store.list_artifacts())

    def test_baseline_change_during_validator_rejects_commit(self):
        task = self.store.add_task("plan", "plan old behavior")
        attempt = self.store.claim(task["id"], "worker")

        def change_baseline(task, result, artifacts):
            other = StateStore(self.root)
            other.initialize("new", self.spec, "new-fingerprint", 123)
            return {"passed": True, "checks": ["old requirements checked"]}

        with self.assertRaises(ConflictError):
            self.store.finish(task["id"], attempt["id"], {}, [self.artifact()], validator=change_baseline)
        self.assertEqual("stale", self.store.get_task(task["id"])["status"])

    def test_artifact_drift_invalidates_transitive_dependents(self):
        first = self.store.add_task("architecture", "transaction contract")
        second = self.store.add_task("implement", "driver", depends_on=[first["id"]])
        third = self.store.add_task("build", "compile bench", depends_on=[second["id"]])
        self.accept(first, "contract.json")
        self.accept(second, "driver.json")
        running = self.store.claim(third["id"], "compiler")
        (self.root / "contract.json").write_text("modified after acceptance", encoding="utf-8")
        drift = self.store.verify_artifacts()
        self.assertEqual(1, len(drift))
        for task in (first, second, third):
            self.assertEqual("stale", self.store.get_task(task["id"])["status"])
        with self.assertRaises(ConflictError):
            self.store.fail(third["id"], running["id"], "late error")

    def test_sources_and_requirements_preserve_versions_without_full_status_context(self):
        source = self.store.add_source("/features/0", "Old behavior", {"module": "uart"}, source_id="source-1")
        self.store.add_requirement("Old behavior", [source["id"]], requirement_id="REQ-1")
        old_id = self.baseline["id"]
        self.store.initialize("test", self.spec, "next-spec-hash", 77)
        self.store.add_source("/features/0", "New behavior", source_id="source-1")
        self.store.add_requirement("New behavior", ["source-1"], requirement_id="REQ-1")
        self.assertEqual("New behavior", self.store.get_source("source-1")["text"])
        self.assertEqual("Old behavior", self.store.get_source("source-1", baseline_id=old_id)["text"])
        self.assertEqual("New behavior", self.store.get_requirement("REQ-1")["text"])
        self.assertNotIn("text", self.store.list_sources()[0])
        self.assertEqual(1, self.store.status()["requirement_count"])
        self.assertNotIn("New behavior", json.dumps(self.store.status()))

    def test_unknown_requirement_source_and_changed_immutable_source_rejected(self):
        with self.assertRaises(ValidationError):
            self.store.add_requirement("untraceable", ["unknown"])
        self.store.add_source("/feature", "first", source_id="s")
        with self.assertRaises(ConflictError):
            self.store.add_source("/feature", "different", source_id="s")

    def test_fragments_can_share_pointer_without_losing_source_identity(self):
        self.store.add_source("/long_chapter", "first fragment", {"start_char": 0, "end_char": 14}, "fragment-a")
        self.store.add_source("/long_chapter", "second fragment", {"start_char": 14, "end_char": 29}, "fragment-b")
        self.assertEqual(2, self.store.status()["source_count"])
        self.assertEqual("first fragment", self.store.get_source("fragment-a")["text"])
        self.assertEqual("second fragment", self.store.get_source("fragment-b")["text"])

    def test_early_database_migration_preserves_sources_and_requirements(self):
        self.store.add_source("/behavior", "original", source_id="fragment-a")
        self.store.add_requirement("original", ["fragment-a"], requirement_id="REQ-1")
        db = sqlite3.connect(self.store.db_path)
        try:
            db.execute("CREATE UNIQUE INDEX old_pointer_unique ON sources(baseline_id,pointer)")
            db.commit()
        finally:
            db.close()
        reopened = StateStore(self.root)
        reopened.add_source("/behavior", "another fragment", source_id="fragment-b")
        self.assertEqual("original", reopened.get_source("fragment-a")["text"])
        self.assertEqual(["fragment-a"], reopened.get_requirement("REQ-1")["source_ids"])
        self.assertEqual(2, reopened.status()["source_count"])

    def test_external_job_survives_session_recovery_and_cannot_be_resubmitted(self):
        task = self.store.add_task("regress", "long simulation")
        attempt = self.store.claim(task["id"], "session", lease_seconds=30)
        self.store.record_run("job-1", "xsim", task_id=task["id"], attempt_id=attempt["id"],
                              metadata={"pid": 123, "seed": 17})
        self.store.update_run("job-1", "running")
        self.store.recover_expired(now=time.time() + 60)
        resumed = StateStore(self.root)
        self.assertEqual("running", resumed.get_run("job-1")["status"])
        resumed.update_run("job-1", "passed", metadata={"report": "coverage.xml"})
        self.assertEqual("pending", resumed.get_task(task["id"])["status"])
        with self.assertRaises(StateError):
            resumed.update_run("job-1", "running")
        self.assertEqual(1, len(resumed.list_runs()))

    def test_requeue_and_failure_preserve_attempts(self):
        task = self.store.add_task("implement", "checker")
        first = self.store.claim(task["id"], "session-a")
        self.store.fail(task["id"], first["id"], "compiler error", retryable=False)
        self.assertEqual("failed", self.store.get_task(task["id"])["status"])
        self.store.requeue(task["id"], payload={"fix": "type mismatch"})
        second = self.store.claim(task["id"], "session-b")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual({"fix": "type mismatch"}, self.store.get_task(task["id"])["payload"])

    def test_status_before_import_and_issue_lifecycle(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertIsNone(StateStore(empty).status()["baseline"])
        issue = self.store.add_issue("spec_ambiguity", "Timeout unit is unspecified")
        self.assertEqual(1, self.store.status()["open_issues"])
        self.store.resolve_issue(issue["id"], "Owner clarified cycles of peripheral clock")
        self.assertEqual([], self.store.list_issues())
        self.assertEqual("resolved", self.store.list_issues(status=None)[0]["status"])


if __name__ == "__main__":
    unittest.main()
