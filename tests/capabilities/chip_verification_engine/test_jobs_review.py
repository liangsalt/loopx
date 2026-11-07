"""Durable EDA job lifecycle tests; no subprocess here runs a simulator."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_generic_simulator as simulator_fixture

from loopx.capabilities.chip_verification import jobs
from loopx.capabilities.chip_verification.engine.workflow import DVProject


class JobLifecycleReviewTests(unittest.TestCase):
    def setUp(self):
        simulator_fixture.GenericSimulatorTests.setUp(self)
        self.spec = self.root / "spec.json"
        self.spec.write_text('{"behavior":"Accepted words preserve FIFO order."}', encoding="utf-8")
        self.project = DVProject(self.root)
        self.project.import_spec(self.spec)

    def save(self):
        simulator_fixture.GenericSimulatorTests.save(self)

    def launch(self):
        with patch.object(jobs.subprocess, "Popen", return_value=SimpleNamespace(pid=123456789)):
            return jobs.launch(self.project, {"manifest": str(self.manifest), "run_smoke": True, "timeout": 30})

    def test_unknown_job_without_job_table_has_actionable_error(self):
        with self.assertRaisesRegex(ValueError, "Unknown.*job"):
            jobs.status(DVProject(self.root, initialize=False), "job-absent")

    def test_spawn_failure_is_durable_and_does_not_claim_success(self):
        with patch.object(jobs.subprocess, "Popen", side_effect=OSError("fixture spawn failure")):
            with self.assertRaisesRegex(OSError, "spawn failure"):
                jobs.launch(self.project, {"manifest": str(self.manifest), "run_smoke": True, "timeout": 30})
        with self.project.store._db() as db:
            identifier = db.execute("SELECT id FROM host_jobs").fetchone()[0]
        result = jobs.status(DVProject(self.root, initialize=False), identifier)
        self.assertEqual("failed", result["status"])
        self.assertIn("spawn failure", result["result"]["error"])

    def test_crash_before_pid_registration_becomes_actionable_after_grace_period(self):
        with patch.object(jobs.subprocess, "Popen", side_effect=KeyboardInterrupt("fixture launch crash")):
            with self.assertRaises(KeyboardInterrupt):
                jobs.launch(self.project, {"manifest": str(self.manifest), "run_smoke": True, "timeout": 30})
        with self.project.store._db(write=True) as db:
            identifier = db.execute("SELECT id FROM host_jobs").fetchone()[0]
            db.execute("UPDATE host_jobs SET created=0 WHERE id=?", (identifier,))
        before = hashlib.sha256(self.project.store.db_path.read_bytes()).hexdigest()
        result = jobs.status(DVProject(self.root, initialize=False), identifier)
        self.assertEqual("interrupted", result["status"])
        self.assertIn("new build", result["instruction"])
        self.assertEqual(before, hashlib.sha256(self.project.store.db_path.read_bytes()).hexdigest())

    def test_dead_registered_process_is_reported_without_restarting_eda(self):
        job = self.launch()
        with patch("loopx.file_lock.process_is_alive", return_value=False), \
             patch.object(jobs.subprocess, "Popen", side_effect=AssertionError("Readback cannot restart EDA")):
            result = jobs.status(DVProject(self.root, initialize=False), job["job_id"])
        self.assertEqual("interrupted", result["status"])

    def test_changed_baseline_rejects_job_before_calling_build(self):
        job = self.launch()
        self.spec.write_text('{"behavior":"Reset clears the FIFO."}', encoding="utf-8")
        self.project.import_spec(self.spec)
        with patch("loopx.capabilities.chip_verification.engine.build.run_build", side_effect=AssertionError("Old baseline cannot run")):
            result = jobs.run(self.root, job["job_id"])
        self.assertEqual("failed", result["status"])
        self.assertIn("Specification changed", result["error"])
        self.assertFalse(jobs.status(DVProject(self.root, initialize=False), job["job_id"])["baseline_current"])

    def test_changed_input_rejects_job_before_calling_build(self):
        job = self.launch()
        self.rtl.write_text("module changed; endmodule\n", encoding="utf-8")
        with patch("loopx.capabilities.chip_verification.engine.build.run_build", side_effect=AssertionError("Changed input cannot run")):
            result = jobs.run(self.root, job["job_id"])
        self.assertEqual("failed", result["status"])
        self.assertIn("input changed", result["error"])

    def test_run_once_preserves_result_and_rejects_second_execution(self):
        job = self.launch()
        with patch("loopx.capabilities.chip_verification.engine.build.run_build", return_value={"status": "built", "fixture_only": True}) as build:
            self.assertEqual("built", jobs.run(self.root, job["job_id"])["status"])
            with self.assertRaisesRegex(ValueError, "already started"):
                jobs.run(self.root, job["job_id"])
        self.assertEqual(1, build.call_count)
        self.assertTrue(jobs.status(DVProject(self.root, initialize=False), job["job_id"])["result"]["fixture_only"])


if __name__ == "__main__":
    unittest.main()
