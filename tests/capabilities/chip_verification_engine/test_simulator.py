import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from loopx.capabilities.chip_verification.engine.simulators.xsim import (
    SimulationError,
    XSimBackend,
    parse_evidence,
    parse_xcrg_report,
)


class EvidenceParserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "hits.csv"
        self.manifest = {
            "scope": "unit fixture only",
            "features": [{"id": 0, "name": "a"}, {"id": 1, "name": "b"}],
        }
        self.path.write_text("id,name,hit\n0,a,1\n1,b,0\n", encoding="utf-8")
        self.log = "UVM_ERROR : 0\nUVM_FATAL : 0\nDVFLOW_CHECKS=10 RETIRED=2 BUS=4 TRAPS=1 RESETS=1\nDVFLOW_NATIVE_COVERAGE=50.000000 HIT_BINS=1 TOTAL_BINS=2\n"

    def tearDown(self):
        self.tmp.cleanup()

    def test_native_result_crosschecked_with_inventory(self):
        r = parse_evidence(self.log, self.path, self.manifest)
        self.assertEqual(r["coverage"]["hit_ids"], [0])
        self.assertEqual(r["coverage"]["percent"], 50)

    def test_rejects_zero_error_without_activity(self):
        with self.assertRaises(SimulationError):
            parse_evidence(
                self.log.replace("CHECKS=10", "CHECKS=0"), self.path, self.manifest
            )

    def test_rejects_uvm_errors_despite_exit_zero(self):
        with self.assertRaises(SimulationError):
            parse_evidence(
                self.log.replace("UVM_ERROR : 0", "UVM_ERROR : 1"),
                self.path,
                self.manifest,
            )

    def test_rejects_percentage_not_matching_native_bins(self):
        with self.assertRaises(SimulationError):
            parse_evidence(
                self.log.replace("50.000000", "100.000000"), self.path, self.manifest
            )

    def test_rejects_missing_coverage_target(self):
        self.path.write_text("id,name,hit\n0,a,1\n")
        with self.assertRaises(SimulationError):
            parse_evidence(self.log, self.path, self.manifest)

    def test_rejects_renamed_bin(self):
        self.path.write_text("id,name,hit\n0,a,1\n1,cheat,0\n")
        with self.assertRaises(SimulationError):
            parse_evidence(self.log, self.path, self.manifest)

    def test_rejects_no_uvm_summary(self):
        with self.assertRaises(SimulationError):
            parse_evidence("", self.path, self.manifest)

    def test_every_program_needs_observed_completion(self):
        with self.assertRaises(SimulationError):
            parse_evidence(
                self.log.replace("RESETS=1", "RESETS=2"), self.path, self.manifest
            )

    def test_later_clean_summary_does_not_hide_an_earlier_error(self):
        with self.assertRaises(SimulationError):
            parse_evidence("UVM_ERROR : 1\n" + self.log, self.path, self.manifest)

    def test_native_database_crosschecked_bin_by_bin(self):
        report = "Coverage Score :, 50\n requirement_0,4,1,\n requirement_1,0,1,\n"
        self.assertEqual(parse_xcrg_report(report, 2, [0]), 50)
        with self.assertRaises(SimulationError):
            parse_xcrg_report(report, 2, [1])


class ToolDiscoveryTests(unittest.TestCase):
    def test_tool_installation_is_resolved_from_path_without_launching_simulator(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            executable = home / "bin" / ("xvlog.bat" if os.name == "nt" else "xvlog")
            executable.parent.mkdir()
            executable.write_text("test discovery only", encoding="utf-8")
            with (
                patch.dict(os.environ, {"XILINX_VIVADO": ""}),
                patch(
                    "loopx.capabilities.chip_verification.engine.simulators.xsim.shutil.which",
                    return_value=str(executable),
                ),
            ):
                self.assertEqual(home.resolve(), XSimBackend().home)

    def test_missing_installation_has_actionable_error(self):
        with (
            patch.dict(os.environ, {"XILINX_VIVADO": ""}),
            patch(
                "loopx.capabilities.chip_verification.engine.simulators.xsim.shutil.which",
                return_value=None,
            ),
        ):
            with self.assertRaisesRegex(SimulationError, "vivado_home.*PATH"):
                XSimBackend()


if __name__ == "__main__":
    unittest.main()
