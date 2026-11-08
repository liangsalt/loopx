"""Watchdog state, host review and recovery fencing without an installed EDA tool."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from loopx.capabilities.chip_verification import host_work
from loopx.capabilities.chip_verification.engine import event_inbox, watchdog
from loopx.capabilities.chip_verification.engine.models import ConflictError, ValidationError
from loopx.capabilities.chip_verification.engine.workflow import DVProject, save_json


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        spec = self.root / "spec.json"
        spec.write_text(json.dumps({"module": "dma", "requirements": ["Descriptors remain ordered across reset and completion"]}), encoding="utf-8")
        self.project = DVProject(self.root)
        self.project.import_spec(spec, scope="Implement and verify descriptor ordering, reset and completion behavior")
        host_work.initialize(self.project)
        self.task = self.project.store.add_task("rtl", "Implement DMA descriptor completion", {"scope": "Preserve descriptor order"})

    def tearDown(self):
        self.tmp.cleanup()

    def failures(self, count=3, *, reason="same ordering violation", session="worker-session"):
        for _ in range(count):
            attempt = self.project.store.claim(self.task["id"], session)
            self.project.store.fail(self.task["id"], attempt["id"], reason)

    def review(self, *, action="resume", strategy="Inspect completion ordering against reset semantics before changing the RTL", session="independent-review", review_id=None):
        if review_id is None:
            review_id = watchdog.status(self.project)["incidents"][0]["review_task_id"]
        claimed = host_work.claim(self.project, {"session_id": session, "task_id": review_id, "kind": "watchdog-review"})
        task = self.project.store.get_task(review_id)
        result = {"diagnosis": "The same candidate failure repeated without new accepted engineering evidence.",
                  "evidence_refs": [task["payload"]["snapshot"]["evidence_refs"][0]],
                  "strategy": strategy, "action": action, "next_check_seconds": 30}
        host_work.submit(self.project, claimed["attempt_id"], result)
        return review_id, result

    def test_repeated_failure_holds_dispatch_and_independent_review_recovers_once(self):
        self.failures()
        first = watchdog.tick(self.project)
        self.assertEqual(1, len(first["review_task_ids"]))
        with self.assertRaisesRegex(ConflictError, "Watchdog|watchdog"):
            self.project.store.claim(self.task["id"], "worker-next")
        with self.assertRaisesRegex(ConflictError, "different runtime session"):
            self.review(session="worker-session")
        review_id, _ = self.review()
        result = watchdog.apply(self.project, review_id)
        self.assertEqual("resumed", result["status"])
        self.assertFalse(result["eda_execution_performed"])
        self.assertTrue(watchdog.apply(self.project, review_id)["replayed"])
        self.assertEqual([], watchdog.status(self.project)["active_holds"])
        self.assertEqual([], watchdog.tick(self.project)["review_task_ids"])
        self.project.store.claim(self.task["id"], "worker-after-review")

    def test_restart_deduplicates_incident_and_wait_schedules_new_bounded_review(self):
        self.failures()
        first = watchdog.tick(self.project)
        recovered = DVProject(self.root)
        self.assertEqual([], watchdog.tick(recovered)["review_task_ids"])
        review_id, _ = self.review(action="wait")
        applied = watchdog.apply(recovered, review_id)
        self.assertEqual("waiting", applied["status"])
        self.assertEqual([], watchdog.tick(recovered, now=applied["next_check"] - 1)["review_task_ids"])
        next_tick = watchdog.tick(recovered, now=applied["next_check"])
        self.assertEqual(1, len(next_tick["review_task_ids"]))
        self.assertNotEqual(first["review_task_ids"], next_tick["review_task_ids"])
        self.assertEqual(1, len(watchdog.status(recovered)["active_holds"]))

    def test_review_is_excluded_from_ordinary_claim_and_uses_immutable_context(self):
        self.failures()
        review_id = watchdog.tick(self.project)["review_task_ids"][0]
        self.assertEqual("no_ready_work", host_work.claim(self.project, {"task_id": review_id, "session_id": "normal-worker"})["status"])
        task = self.project.store.get_task(review_id)
        original = watchdog.task_context(self.project, task)
        self.project.store.add_issue("external_observation", "A separate module now reports a diagnostic")
        self.assertEqual(original, watchdog.task_context(self.project, task))
        self.assertIn(task["payload"]["snapshot"]["evidence_refs"][0], original[0])

    def test_stale_accepted_review_does_not_requeue_cancelled_target(self):
        self.failures()
        watchdog.tick(self.project)
        review_id, _ = self.review()
        self.project.store.cancel(self.task["id"], "User cancelled this design alternative")
        applied = watchdog.apply(self.project, review_id)
        self.assertEqual("superseded", applied["status"])
        self.assertEqual("cancelled", self.project.store.get_task(self.task["id"])["status"])
        self.assertEqual([], watchdog.status(self.project)["active_holds"])

    def test_review_keeps_task_evidence_despite_legacy_context_budget(self):
        save_json(self.project.config_path, {**self.project.config, "context_chars": 1, "host_context_chars": 1})
        self.failures(reason="Known ordering failure with unchanged evidence")
        review_id = watchdog.tick(self.project)["review_task_ids"][0]
        task = self.project.store.get_task(review_id)
        self.assertEqual(3, len(task["payload"]["snapshot"]["attempts"]))
        claimed = host_work.claim(self.project, {"session_id": "independent", "task_id": review_id,
                                                "kind": "watchdog-review"})
        packet = host_work.context(self.project, claimed["attempt_id"])
        self.assertIsNone(packet["context_budget_chars"])
        self.assertIn("Known ordering failure", packet["prompt"])
        self.assertFalse(task["payload"]["snapshot"].get("excerpts_reduced", False))

    def test_review_keeps_complete_goal_scope_and_previous_strategy(self):
        goal = "Preserve descriptor completion across reset. " * 80 + "GOAL TAIL: retain outstanding work."
        scope = "Inspect accepted transaction ordering. " * 60 + "SCOPE TAIL: do not discard retries."
        title = "Implement descriptor ownership " * 30 + "TITLE TAIL"
        strategy = "Trace completion acknowledgements to the specification before editing. " * 40 + "STRATEGY TAIL"
        self.project = DVProject(self.root / "complete-semantics")
        self.project.import_spec(self.root / "spec.json", scope=goal)
        host_work.initialize(self.project)
        self.task = self.project.store.add_task("rtl", title, {"scope": scope})
        self.failures()
        first = watchdog.tick(self.project)["review_task_ids"][0]
        snapshot = self.project.store.get_task(first)["payload"]["snapshot"]
        self.assertEqual(goal, snapshot["goal_excerpt"])
        self.assertEqual(scope, snapshot["scope_excerpt"])
        self.assertEqual(title, snapshot["title"])
        self.review(review_id=first, strategy=strategy)
        self.assertEqual("resumed", watchdog.apply(self.project, first)["status"])
        self.failures()
        second = watchdog.tick(self.project)["review_task_ids"][0]
        task = self.project.store.get_task(second)
        self.assertEqual([strategy], task["payload"]["snapshot"]["previous_strategy_excerpts"])
        prompt, _, _ = watchdog.task_context(self.project, task)
        for value in (goal, scope, title, strategy):
            self.assertIn(value, prompt)

    def test_new_target_revision_supersedes_open_review_and_releases_old_hold(self):
        self.failures()
        review_id = watchdog.tick(self.project)["review_task_ids"][0]
        self.project.store.cancel(self.task["id"], "Replace the obsolete task")
        watchdog.tick(self.project)
        self.assertEqual("cancelled", self.project.store.get_task(review_id)["status"])
        self.assertEqual([], watchdog.status(self.project)["active_holds"])

    def test_expired_worker_is_only_recovered_after_review(self):
        attempt = self.project.store.claim(self.task["id"], "worker", lease_seconds=300)
        with self.project.store._db(write=True) as db:
            db.execute("UPDATE attempts SET lease_until=? WHERE id=?", (time.time() - 1, attempt["id"]))
        watchdog.tick(self.project)
        self.assertEqual("running", self.project.store.get_task(self.task["id"])["status"])
        self.assertNotIn(self.task["id"], self.project.store.recover_expired())
        review_id, _ = self.review()
        self.assertEqual("resumed", watchdog.apply(self.project, review_id)["status"])
        self.assertEqual("pending", self.project.store.get_task(self.task["id"])["status"])
        with self.assertRaises(ConflictError):
            self.project.store.heartbeat(self.task["id"], attempt["id"])

    def test_repair_budget_survives_sessions_and_does_not_reset_from_watchdog_activity(self):
        watchdog.configure(self.project, {"max_resumes": 1}, "Bound repair experiments without new evidence")
        self.failures()
        watchdog.tick(self.project)
        first, _ = self.review()
        self.assertEqual("resumed", watchdog.apply(self.project, first)["status"])
        self.failures()
        watchdog.tick(self.project)
        second, _ = self.review(strategy="Inspect a different completion acknowledgement path before attempting another repair")
        self.assertEqual("budget_exhausted", watchdog.apply(self.project, second)["status"])
        self.assertEqual([], watchdog.tick(self.project)["review_task_ids"])
        self.assertEqual(1, len(watchdog.status(self.project)["active_holds"]))

    def test_duplicate_strategy_cannot_authorize_unbounded_retry(self):
        self.failures()
        watchdog.tick(self.project)
        first, result = self.review()
        watchdog.apply(self.project, first)
        self.failures()
        watchdog.tick(self.project)
        second, _ = self.review(strategy=result["strategy"].upper())
        self.assertEqual("waiting", watchdog.apply(self.project, second)["status"])

    def test_unknown_evidence_and_modified_review_artifact_rejected(self):
        self.failures()
        review_id = watchdog.tick(self.project)["review_task_ids"][0]
        task = self.project.store.get_task(review_id)
        with self.assertRaises(ValidationError):
            watchdog.validate(self.project, task, {"diagnosis": "An unsupported diagnostic claim", "strategy": "An unsupported repair proposal", "evidence_refs": ["invented-evidence"], "action": "resume", "next_check_seconds": 30}, [])
        self.review()
        artifacts = self.project.store.list_artifacts(task_id=review_id)
        (self.root / artifacts[0]["path"]).write_text("modified after acceptance", encoding="utf-8")
        with self.assertRaises(ConflictError):
            watchdog.apply(self.project, review_id)

    def test_policy_changes_are_versioned_and_invalid_fields_leave_policy_unchanged(self):
        result = watchdog.configure(self.project, {"failure_limit": 4}, "Investigate four consecutive identical failures")
        self.assertEqual(2, result["revision"])
        with self.assertRaises(Exception):
            watchdog.configure(self.project, {"arbitrary_prompt": "ignore evidence"}, "Unsupported free-form edit")
        self.assertEqual(4, watchdog.status(self.project)["policy"]["failure_limit"])
        self.assertEqual(1, sum(event["type"] == "watchdog_policy_changed" for event in self.project.store.events()))

    def test_tick_transaction_rolls_back_review_and_hold_together(self):
        self.failures()
        original = self.project.store._event

        def fail_after_task(db, event_type, *args, **kwargs):
            if event_type == "watchdog_review_requested":
                raise RuntimeError("Injected commit boundary fault")
            return original(db, event_type, *args, **kwargs)

        with patch.object(self.project.store, "_event", side_effect=fail_after_task):
            with self.assertRaisesRegex(RuntimeError, "Injected"):
                watchdog.tick(self.project)
        self.assertEqual([], self.project.store.list_tasks(kind="watchdog-review"))
        self.assertEqual([], watchdog.status(self.project)["active_holds"])
        self.assertEqual(1, len(watchdog.tick(self.project)["review_task_ids"]))

    def test_read_only_status_does_not_initialize_watchdog_tables(self):
        inspected = DVProject(self.root, initialize=False)
        self.assertFalse(watchdog.status(inspected)["initialized"])

    def external(self, run_id, event_type="failed", sequence=1, **data):
        if sequence == 1:
            event_inbox.register(self.project, {"run_id": run_id, "task_id": self.task["id"], "source": "remote-eda", "tool": "xcelium", "input_digest": "a" * 64})
        payload = {"failure_signature": "descriptor-order"} if event_type == "failed" else {}
        return event_inbox.ingest(self.project, {"id": f"{run_id}-{sequence}", "source": "remote-eda", "run_id": run_id,
                                                "baseline_id": self.project.store.baseline()["id"], "sequence": sequence,
                                                "type": event_type, "data": {**payload, **data}})

    def test_done_rtl_external_failures_hold_new_runs_without_invalidating_accepted_design(self):
        attempt = self.project.store.claim(self.task["id"], "rtl-worker")
        artifact = self.root / "candidate.sv"
        artifact.write_text("module dma; endmodule", encoding="utf-8")
        self.project.store.finish(self.task["id"], attempt["id"], {"candidate": artifact.name}, [artifact], validator=lambda *args: {"passed": True, "checks": ["fixture acceptance"]})
        for index in range(3):
            self.external(f"run-{index}")
        watchdog.tick(self.project)
        with self.assertRaises(ConflictError):
            self.external("held-run")
        review_id, _ = self.review()
        self.assertEqual("resumed", watchdog.apply(self.project, review_id)["status"])
        self.assertEqual("done", self.project.store.get_task(self.task["id"])["status"])
        self.assertEqual("applied", self.external("next-run")["disposition"])

    def test_late_eda_evidence_fences_previously_accepted_review(self):
        for index in range(3):
            self.external(f"run-{index}")
        watchdog.tick(self.project)
        review_id, _ = self.review()
        self.external("run-2", "evidence", 2, evidence=[{"uri": "artifact:remote-report", "sha256": "b" * 64}])
        receipt = watchdog.apply(self.project, review_id)
        self.assertEqual("superseded", receipt["status"])
        self.assertNotEqual(review_id, receipt["next_review_task_id"])
        self.assertEqual(1, len(watchdog.status(self.project)["active_holds"]))
        with self.assertRaises(ConflictError):
            self.external("no-unreviewed-dispatch")

    def test_attempt_abandon_keeps_hold_and_refreshes_review_outside_scan_page(self):
        watchdog.configure(self.project, {"max_tasks_per_tick": 1}, "Exercise bounded scans with an already held target")
        attempt = self.project.store.claim(self.task["id"], "worker")
        with self.project.store._db(write=True) as db:
            db.execute("UPDATE attempts SET lease_until=? WHERE id=?", (time.time() - 1, attempt["id"]))
        for _ in range(4):
            if watchdog.tick(self.project)["review_task_ids"]:
                break
        incident = watchdog.status(self.project)["incidents"][0]
        # Equivalent to an owner abandonment before the supervisory hold was
        # noticed; failure advances revision but does not accept engineering.
        with self.project.store._db(write=True) as db:
            db.execute("UPDATE attempts SET lease_until=? WHERE id=?", (time.time() + 300, attempt["id"]))
        self.project.store.fail(self.task["id"], attempt["id"], "Owner abandoned the attempt")
        watchdog.tick(self.project)
        current = watchdog.status(self.project)["incidents"][0]
        self.assertEqual(incident["id"], current["id"])
        self.assertNotEqual(incident["review_task_id"], current["review_task_id"])
        with self.assertRaises(ConflictError):
            self.project.store.claim(self.task["id"], "cannot-bypass-review")

    def test_old_live_run_outside_history_window_prevents_recovery(self):
        self.external("old-running", "started")
        with self.project.store._db(write=True) as db:
            original = dict(db.execute("SELECT * FROM runs WHERE id='old-running'").fetchone())
            for index in range(41):
                metadata = json.loads(original["metadata"])
                metadata.update(failure_signature="descriptor-order", completed_at=time.time())
                db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"later-terminal-{index}", original["baseline_id"], self.task["id"], None,
                    original["tool"], "failed", json.dumps(metadata), "[]", "current", original["created"] + index + 1, time.time()))
        watchdog.tick(self.project)
        review_id, _ = self.review()
        snapshot = self.project.store.get_task(review_id)["payload"]["snapshot"]
        self.assertEqual(1, snapshot["active_run_count"])
        self.assertNotIn("old-running", [run["id"] for run in snapshot["runs"]])
        self.assertEqual("waiting", watchdog.apply(self.project, review_id)["status"])

    def test_live_run_from_previous_task_revision_prevents_recovery(self):
        attempt = self.project.store.claim(self.task["id"], "worker")
        self.external("prior-revision-live", "started")
        self.project.store.fail(self.task["id"], attempt["id"], "same ordering violation")
        self.failures(count=2)
        watchdog.tick(self.project)
        review_id, _ = self.review()
        self.assertEqual("waiting", watchdog.apply(self.project, review_id)["status"])

    def test_stale_run_terminal_observation_requires_matching_binding_and_new_review(self):
        attempt = self.project.store.claim(self.task["id"], "worker")
        self.external("old-revision", "started")
        self.project.store.fail(self.task["id"], attempt["id"], "same ordering violation")
        self.failures(count=2)
        watchdog.tick(self.project)
        baseline = self.project.store.baseline()["id"]
        event_inbox.ingest(self.project, {"id": "wrong-baseline-end", "source": "remote-eda", "run_id": "old-revision", "baseline_id": "wrong-baseline", "sequence": 2, "type": "completed", "data": {}})
        watchdog.tick(self.project)
        wrong_review, _ = self.review()
        self.assertEqual("waiting", watchdog.apply(self.project, wrong_review)["status"])
        event_inbox.ingest(self.project, {"id": "bound-physical-end", "source": "remote-eda", "run_id": "old-revision", "baseline_id": baseline, "sequence": 3, "type": "completed", "data": {}})
        refreshed = watchdog.tick(self.project)
        self.assertEqual(1, len(refreshed["review_task_ids"]))
        self.assertEqual(1, len(watchdog.status(self.project)["active_holds"]))
        fresh_review, _ = self.review()
        self.assertEqual(0, self.project.store.get_task(fresh_review)["payload"]["snapshot"]["active_run_count"])
        self.assertEqual("resumed", watchdog.apply(self.project, fresh_review)["status"])

    def test_accepted_old_generation_cannot_override_new_incident_owner(self):
        for index in range(3):
            self.external(f"run-{index}")
        watchdog.tick(self.project)
        old_review, _ = self.review()
        self.external("run-2", "evidence", 2, evidence=[{"uri": "artifact:late-report", "sha256": "c" * 64}])
        # Exercise refresh independently of automatic apply, as when another
        # supervisor observes drift before a manually submitted apply arrives.
        with patch.object(watchdog, "apply", return_value={"status": "deferred"}):
            watchdog.tick(self.project)
        new_owner = watchdog.status(self.project)["incidents"][0]["review_task_id"]
        self.assertNotEqual(old_review, new_owner)
        result = watchdog.apply(self.project, old_review)
        self.assertEqual("superseded", result["status"])
        after = watchdog.status(self.project)
        self.assertEqual(new_owner, after["incidents"][0]["review_task_id"])
        self.assertEqual("open", after["incidents"][0]["status"])
        self.assertEqual(1, len(after["active_holds"]))

    def test_damaged_review_does_not_disable_other_task_monitoring_or_hot_retry(self):
        self.failures()
        watchdog.tick(self.project)
        review_id, _ = self.review()
        artifact = self.project.store.list_artifacts(task_id=review_id)[0]
        (self.root / artifact["path"]).write_text("damaged accepted review", encoding="utf-8")
        self.task = self.project.store.add_task("rtl", "Independent DMA reset issue", {"scope": "Check independent reset progress"})
        self.failures()
        result = watchdog.tick(self.project)
        self.assertEqual("review_error", result["applied"][0]["status"])
        self.assertEqual(1, len(result["review_task_ids"]))
        self.assertEqual([], watchdog.tick(self.project)["applied"])
        broken = next(item for item in watchdog.status(self.project)["incidents"] if item["review_task_id"] == review_id)
        self.assertEqual("waiting", broken["status"])
        self.assertIn("review_error", broken)


if __name__ == "__main__":
    unittest.main()
