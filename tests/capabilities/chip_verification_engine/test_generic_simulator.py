"""Contract and evidence tests; these tests never launch EDA or model processes."""
import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from loopx.capabilities.chip_verification.engine.simulators.generic import (
    GenericXSimBackend, ManifestError, load_build_manifest, parse_smoke_log, validate_generic_evidence,
)
from loopx.capabilities.chip_verification.engine.simulators.xsim import SimulationError


CLEAN_LOG = """UVM_INFO @ 0: reporter [RNTST] Running test example_test...
DVFLOW_CHECKS=7 TRANSACTIONS=3
--- UVM Report Summary ---
UVM_INFO : 4
UVM_WARNING : 0
UVM_ERROR : 0
UVM_FATAL : 0
"""
CONTRACT = {"pattern": r"DVFLOW_CHECKS=(?P<checks>\d+) TRANSACTIONS=(?P<transactions>\d+)",
            "minimums": {"checks": 1, "transactions": 1}}


class RecordingBackend(GenericXSimBackend):
    """Execution seam: all parsing, inputs, plans and evidence use production code."""
    def __init__(self, *, text=CLEAN_LOG, fail_tool=None, change_input=None, tool_warning=None):
        super().__init__()
        self.text = text
        self.fail_tool = fail_tool
        self.change_input = change_input
        self.tool_warning = tool_warning

    def _execute(self, tool, arguments, run, manifest, timeout_seconds):
        log = run / (tool + "_console.log")
        log.write_text(self.text if tool == "xsim" else (self.tool_warning or "Tool completed\n"), encoding="utf-8")
        if self.change_input and tool == "xelab":
            self.change_input.write_text("module modified; endmodule\n", encoding="utf-8")
        executable = str(Path(manifest["vivado_home"]) / "bin" / (tool + manifest["executable_suffix"]))
        return {"tool": tool, "argv": [executable, *arguments], "cwd": str(run),
                "log": str(log), "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(), "exit_code": int(tool == self.fail_tool),
                "timed_out": False, "seconds": 0}


class GenericSimulatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.candidate = self.root / "candidate with spaces"
        self.candidate.mkdir()
        self.vivado = self.root / "vivado"
        (self.vivado / "bin").mkdir(parents=True)
        suffix = ".bat" if os.name == "nt" else ""
        for name in ("xvlog", "xelab", "xsim"):
            (self.vivado / "bin" / (name + suffix)).write_text("not executable test fixture", encoding="utf-8")
        self.uvm = self.vivado / "data/system_verilog/uvm_1.2"
        self.uvm.mkdir(parents=True)
        (self.uvm / "uvm_pkg.sv").write_text("package uvm_pkg; endpackage\n", encoding="utf-8")
        (self.uvm / "uvm_macros.svh").write_text("// included vendor macros\n", encoding="utf-8")
        self.rtl = self.root / "dut.v"
        self.rtl.write_text("module dut; endmodule\n", encoding="utf-8")
        for filename in ("types.sv", "tb.sv"):
            (self.candidate / filename).write_text("// test fixture\n", encoding="utf-8")
        (self.candidate / "defs.svh").write_text("`define LIMIT 9\n", encoding="utf-8")
        self.manifest = self.candidate / "build.json"
        self.data = {"schema_version": 1, "top": "tb", "rtl_files": [str(self.rtl)],
                     "tb_files": ["types.sv", "tb.sv"], "include_dirs": ["."],
                     "vivado_home": str(self.vivado), "testname": "example_test", "runtime": "100 us",
                     "seed": 17, "defines": ["WIDTH=32"], "activity_contract": CONTRACT}
        self.save()

    def save(self):
        self.manifest.write_text(json.dumps(self.data), encoding="utf-8")

    def test_manifest_preserves_order_and_records_external_rtl(self):
        resolved = load_build_manifest(self.manifest)
        self.assertEqual(resolved["compile_order"], [str(self.rtl), str(self.candidate / "types.sv"), str(self.candidate / "tb.sv")])
        self.assertEqual(resolved["defines"], ["UVM_NO_DPI", "WIDTH=32"])
        self.data["compile_order"] = ["types.sv", str(self.rtl), "tb.sv"]
        self.save()
        self.assertEqual(load_build_manifest(self.manifest)["compile_order"][0], str(self.candidate / "types.sv"))

    def test_rejects_missing_duplicate_escape_and_shell_options(self):
        cases = [
            ("tb_files", [str(self.rtl)]), ("tb_files", ["../dut.v"]),
            ("rtl_files", [str(self.rtl), str(self.rtl)]),
            ("compile_order", ["tb.sv"]), ("top", "tb;quit"),
            ("runtime", "100 us; exec bad"), ("testname", "test&bad"),
            ("defines", ["X=1&bad"]), ("defines", ["-help"]),
            ("defines", ["WIDTH=1", "WIDTH=2"]), ("seed", True),
            ("rtl_files", ["missing.v"]), ("schema_version", True),
        ]
        original = dict(self.data)
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.data = {**original, field: value}
                self.save()
                with self.assertRaises(ManifestError):
                    load_build_manifest(self.manifest)

    def test_activity_requires_independent_counters_and_positive_thresholds(self):
        for contract in ({"pattern": r"OK=(?P<checks>\d+)"},
                         {**CONTRACT, "minimums": {"checks": 0, "transactions": 1}},
                         {**CONTRACT, "minimums": {"checks": 1}},
                         {"pattern": "["}):
            with self.subTest(contract=contract), self.assertRaises(ManifestError):
                parse_smoke_log(CLEAN_LOG, contract)

    def test_clean_summary_without_activity_is_not_verified(self):
        result = parse_smoke_log(CLEAN_LOG)
        self.assertEqual(result["status"], "smoke_completed")
        self.assertFalse(result["verified"])
        self.assertNotIn("coverage", result)

    def test_checker_activity_establishes_only_smoke_scope(self):
        result = parse_smoke_log(CLEAN_LOG, CONTRACT)
        self.assertEqual(result["status"], "smoke_verified")
        self.assertEqual(result["activity"]["counters"], {"checks": 7, "transactions": 3})
        self.assertIn("smoke only", result["verification_scope"])

    def test_ignored_assertions_block_smoke_acceptance_despite_positive_checks(self):
        warning = "WARNING: [XSIM 43-4127] System Verilog Assertion is not supported and will be ignored.\n"
        evidence = RecordingBackend(tool_warning=warning).run(self.manifest, self.root / "ignored", run_smoke=True)
        self.assertEqual(evidence["status"], "smoke_unqualified")
        self.assertFalse(evidence["verified"])
        self.assertEqual(evidence["activity"]["counters"]["checks"], 7)
        self.assertTrue(validate_generic_evidence(evidence, self.manifest, phase="build")["build_passed"])
        # An old backend/candidate's success field cannot override raw diagnostics.
        evidence.update(status="smoke_verified", verified=True, qualification_limitations=[])
        verdict = validate_generic_evidence(evidence, self.manifest, phase="smoke")
        self.assertTrue(verdict["accepted"])
        self.assertFalse(verdict["verified"])
        self.assertEqual(verdict["smoke_status"], "smoke_unqualified")
        self.assertEqual(len(verdict["qualification_limitations"]), 2)

    def test_rejects_vacuity_incomplete_summary_and_hidden_uvm_errors(self):
        bad_logs = [CLEAN_LOG.replace("CHECKS=7", "CHECKS=0"),
                    CLEAN_LOG.replace("TRANSACTIONS=3", "TRANSACTIONS=0"),
                    CLEAN_LOG.replace("UVM_ERROR : 0", "UVM_ERROR : 2"),
                    CLEAN_LOG.replace("UVM_FATAL : 0", "UVM_FATAL : 1"),
                    CLEAN_LOG.replace("UVM_FATAL : 0", ""),
                    CLEAN_LOG.replace("UVM Report Summary", "end simulation"),
                    "UVM_ERROR /tmp/scoreboard.sv(9) @ 30: checker [MISMATCH] actual differs\n" + CLEAN_LOG,
                    CLEAN_LOG.replace("DVFLOW_CHECKS=7 TRANSACTIONS=3", ""),
                    CLEAN_LOG + "\nDVFLOW_CHECKS=0 TRANSACTIONS=3\n"]
        for log in bad_logs:
            with self.subTest(log=log), self.assertRaises(SimulationError):
                parse_smoke_log(log, CONTRACT)

    def test_build_only_preserves_command_order_hashes_and_no_coverage_claim(self):
        evidence = RecordingBackend().run(self.manifest, self.root / "build")
        self.assertEqual(evidence["status"], "built")
        self.assertFalse(evidence["verified"])
        self.assertEqual([item["tool"] for item in evidence["commands"]], ["xvlog", "xelab"])
        names = {Path(item["path"]).name for item in evidence["inputs"]}
        self.assertTrue({"build.json", "dut.v", "types.sv", "tb.sv", "defs.svh", "uvm_pkg.sv", "uvm_macros.svh"} <= names)
        self.assertEqual(evidence["coverage"]["status"], "not_evaluated")
        self.assertNotIn("percent", evidence["coverage"])
        options = (self.root / "build/xvlog.options").read_text(encoding="utf-8")
        self.assertIn('"WIDTH=32"', options)
        self.assertLess(options.index("types.sv"), options.index("tb.sv"))

    def test_real_run_path_accepts_smoke_only_with_observed_activity(self):
        evidence = RecordingBackend().run(self.manifest, self.root / "smoke", run_smoke=True)
        self.assertEqual(evidence["status"], "smoke_verified")
        self.assertTrue(evidence["verified"])
        self.assertEqual(len(evidence["commands"]), 3)
        self.assertEqual((self.root / "smoke/smoke.tcl").read_text(), "run 100 us\nquit\n")
        self.assertIn('"UVM_TESTNAME=example_test"', (self.root / "smoke/xsim.options").read_text())
        self.assertFalse(evidence["meets_coverage_target"])

    def test_failure_retains_exact_failed_command_and_stops_later_phases(self):
        evidence = RecordingBackend(fail_tool="xvlog").run(self.manifest, self.root / "failure", run_smoke=True)
        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(len(evidence["commands"]), 1)
        self.assertEqual(evidence["commands"][0]["exit_code"], 1)
        stored = json.loads((self.root / "failure/evidence.json").read_text())
        self.assertEqual(stored["commands"], evidence["commands"])

    def test_input_change_invalidates_otherwise_successful_build(self):
        evidence = RecordingBackend(change_input=self.rtl).run(self.manifest, self.root / "changed")
        self.assertEqual(evidence["status"], "failed")
        self.assertIn("inputs changed", evidence["error"])
        self.assertFalse(evidence["verified"])

    def test_existing_evidence_cannot_be_overwritten(self):
        run = self.root / "existing"
        run.mkdir()
        (run / "user-file").write_text("keep")
        with self.assertRaises(ManifestError):
            RecordingBackend().run(self.manifest, run)
        self.assertEqual((run / "user-file").read_text(), "keep")

    def test_independent_validator_reopens_raw_artifacts_and_state_baseline(self):
        backend = RecordingBackend()
        baseline = backend.snapshot_inputs(self.manifest)
        evidence = backend.run(self.manifest, self.root / "validate", run_smoke=True)
        verdict = validate_generic_evidence(evidence, self.manifest, phase="smoke", expected_inputs=baseline)
        self.assertTrue(verdict["accepted"], verdict)
        self.assertTrue(verdict["verified"])
        self.assertFalse(verdict["meets_coverage_target"])
        log = self.root / "validate/xsim_console.log"
        log.write_text(CLEAN_LOG.replace("UVM_ERROR : 0", "UVM_ERROR : 1"))
        self.assertFalse(validate_generic_evidence(evidence, self.manifest, phase="smoke")["accepted"])
        # Re-hashing a changed log still cannot bypass raw UVM checking.
        evidence["commands"][-1]["log_sha256"] = hashlib.sha256(log.read_bytes()).hexdigest()
        self.assertFalse(validate_generic_evidence(evidence, self.manifest, phase="smoke")["accepted"])

    def test_build_phase_can_accept_compile_success_despite_later_smoke_failure(self):
        evidence = RecordingBackend(text=CLEAN_LOG.replace("CHECKS=7", "CHECKS=0")).run(
            self.manifest, self.root / "partial", run_smoke=True)
        self.assertEqual(evidence["status"], "failed")
        verdict = validate_generic_evidence(evidence, self.manifest, phase="build")
        self.assertTrue(verdict["accepted"], verdict)
        self.assertFalse(verdict["verified"])
        self.assertFalse(validate_generic_evidence(evidence, self.manifest, phase="smoke")["accepted"])

    def test_validator_rejects_missing_tool_wrong_order_and_claimed_success(self):
        evidence = RecordingBackend().run(self.manifest, self.root / "tamper", run_smoke=True)
        missing = {**evidence, "commands": evidence["commands"][:1], "verified": True}
        self.assertFalse(validate_generic_evidence(missing, self.manifest, phase="build")["accepted"])
        reordered = {**evidence, "commands": list(reversed(evidence["commands"]))}
        self.assertFalse(validate_generic_evidence(reordered, self.manifest, phase="smoke")["accepted"])
        stale = [dict(row) for row in evidence["inputs"]]
        stale[0]["sha256"] = "0" * 64
        self.assertFalse(validate_generic_evidence(evidence, self.manifest, expected_inputs=stale)["accepted"])

    def test_plan_never_launches_a_process_and_explicit_install_never_falls_back(self):
        with patch("loopx.capabilities.chip_verification.engine.simulators.generic.subprocess.Popen", side_effect=AssertionError("Unexpected EDA launch")):
            self.assertEqual(GenericXSimBackend().plan(self.manifest)["vivado_home"], str(self.vivado))
        other = self.root / "invalid-vivado"
        other.mkdir()
        self.data["vivado_home"] = str(other)
        self.save()
        # XSimBackend discovery can see a real installed version on developer PCs;
        # the generic adapter must nevertheless reject fallback from explicit home.
        with self.assertRaises(SimulationError):
            GenericXSimBackend().plan(self.manifest)


if __name__ == "__main__":
    unittest.main()
