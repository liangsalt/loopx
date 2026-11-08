"""Provider report integration; fake tool execution is never real EDA evidence."""
from __future__ import annotations

import hashlib
import json
import unittest
from unittest.mock import patch

import test_generic_simulator as simulator_fixture

from loopx.capabilities.chip_verification import provider
from loopx.capabilities.chip_verification.engine.build import run_build
from loopx.capabilities.chip_verification.engine.simulators.generic import GenericXSimBackend
from loopx.capabilities.chip_verification.engine.workflow import DVProject


class ProviderReportTests(unittest.TestCase):
    def setUp(self):
        simulator_fixture.GenericSimulatorTests.setUp(self)
        self.spec = self.root / "spec.json"
        self.spec.write_text('{"behavior":"The FIFO preserves accepted word order."}', encoding="utf-8")
        self.project = DVProject(self.root)
        self.project.import_spec(self.spec)

    def save(self):
        simulator_fixture.GenericSimulatorTests.save(self)

    def build(self, **options):
        recorder = simulator_fixture.RecordingBackend(**options)
        # Only the simulator process seam is replaced. Manifest resolution,
        # immutable artifacts, parsing, state acceptance and report are real.
        with patch.object(GenericXSimBackend, "_execute", recorder._execute):
            return run_build(self.project, self.manifest, run_smoke=True)

    def report(self):
        before = hashlib.sha256(self.project.store.db_path.read_bytes()).hexdigest()
        result = provider._report(DVProject(self.root, initialize=False))
        self.assertEqual(before, hashlib.sha256(self.project.store.db_path.read_bytes()).hexdigest())
        self.assertEqual({"status": "not_evaluated", "percent": None}, result["coverage"])
        self.assertFalse(result["signoff"])
        return {item["phase"]: item for item in result["executions"]}

    def test_build_and_smoke_report_accept_dict_state_inventory_and_list_tool_inventory(self):
        built = self.build()
        self.assertEqual("smoke_verified", built["status"])
        task = self.project.store.get_task(built["build_task_id"])
        self.assertIsInstance(task["payload"]["inputs"], dict)
        self.assertIsInstance(task["result"]["evidence"]["inputs"], list)
        result = self.report()
        self.assertTrue(result["build"]["accepted"], result)
        self.assertTrue(result["smoke"]["accepted"], result)
        self.assertTrue(result["smoke"]["verified"], result)
        self.assertEqual({"checks": 7, "transactions": 3}, result["smoke"]["activity"]["counters"])
        self.assertFalse(result["smoke"]["meets_coverage_target"])

    def test_changed_rtl_is_rejected_by_report_without_mutating_task_acceptance(self):
        built = self.build()
        self.rtl.write_text("module changed; endmodule\n", encoding="utf-8")
        result = self.report()
        self.assertFalse(result["build"]["accepted"], result)
        self.assertFalse(result["smoke"]["accepted"], result)
        self.assertEqual("done", self.project.store.get_task(built["build_task_id"])["status"])

    def test_changed_raw_simulation_diagnostic_cannot_keep_smoke_accepted(self):
        built = self.build()
        evidence = self.project.store.get_task(built["smoke_task_id"])["result"]["evidence"]
        from pathlib import Path
        log = Path(evidence["commands"][-1]["log"])
        log.write_text(simulator_fixture.CLEAN_LOG.replace("UVM_ERROR : 0", "UVM_ERROR : 1"), encoding="utf-8")
        result = self.report()
        self.assertFalse(result["smoke"]["accepted"], result)
        self.assertFalse(result["smoke"]["verified"])

    def test_ignored_constructs_remain_unqualified_in_report(self):
        result = self.build(tool_warning="WARNING: [XSIM 43-4127] System Verilog Assertion is not supported and will be ignored.\n")
        self.assertEqual("blocked", result["status"])
        report = self.report()
        self.assertTrue(report["build"]["accepted"], report)
        self.assertFalse(report["build"]["verified"])
        self.assertTrue(report["build"]["qualification_limitations"])
        self.assertNotIn("smoke", report)

    def test_changed_recorded_input_set_cannot_be_laundered_through_tool_evidence(self):
        built = self.build()
        task = self.project.store.get_task(built["build_task_id"])
        modified = task["result"]
        modified["evidence"]["inputs"] = modified["evidence"]["inputs"][:-1]
        with self.project.store._db(write=True) as db:
            db.execute("UPDATE tasks SET result=? WHERE id=?", (json.dumps(modified), task["id"]))
        result = self.report()
        self.assertFalse(result["build"]["accepted"], result)
        self.assertIn("accepted domain task", result["build"]["error"])


if __name__ == "__main__":
    unittest.main()
