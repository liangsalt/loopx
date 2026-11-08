"""Independent static-check evidence guards; fixtures are not EDA results."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from loopx.capabilities.chip_verification.engine import design_checks
from loopx.capabilities.chip_verification.engine.models import ConflictError, ValidationError
from loopx.capabilities.chip_verification.engine.state import file_hash
from loopx.capabilities.chip_verification.engine.workflow import DVProject, save_json


CONSOLE = "LOOPX_ELABORATION_DONE=1\nLOOPX_CLOCK_COUNT=1\nLOOPX_REGISTER_COUNT=1\nLOOPX_UNCLOCKED_PINS=0\nLOOPX_CDC_DONE=1\n"
SAFE_CDC = "CDC Report\n\nAll paths are Safely Timed.\n"


class StaticEvidenceTests(unittest.TestCase):
    def setUp(self):
        # These fixtures never launch EDA. CI's nested temporary directory must
        # not conflate evidence-parser tests with the separate path-limit gate.
        limit = patch.object(design_checks, "_WINDOWS_CWD_LIMIT", 2048)
        limit.start()
        self.addCleanup(limit.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.spec = self.root / "spec.json"
        save_json(self.spec, {"fixture": "Sequential sample"})
        self.project = DVProject(self.root)
        self.project.import_spec(self.spec, chunk_chars=1000, context_chars=24000)
        self.source = self.root / "sample.sv"
        self.source.write_text("module sample(input clk, output q); endmodule\n", encoding="utf-8")
        self.contract = {"ports": [
            {"name": "clk", "direction": "input", "width": 1, "clock_domain": "core"},
            {"name": "q", "direction": "output", "width": 1, "clock_domain": "core"}],
            "clocks": [{"name": "core", "port": "clk", "period_ns": 10}], "resets": [], "parameters": []}
        self.candidate = {"rtl_task_id": "rtl-fixture", "contract_task_id": "contract-fixture", "contract_hash": "a" * 64,
            "top": "sample", "rtl_files": [str(self.source)], "inputs": {str(self.source): file_hash(self.source)},
            "required_checks": ["elaboration", "lint", "cdc"], "contract": self.contract}

    def tearDown(self):
        self.temporary.cleanup()

    def evidence(self, *, lint=True, cdc=SAFE_CDC, console=CONSOLE, version=1):
        run_id = "rtl_check_" + "a" * 32
        directory = self.root if version == 1 else self.root / run_id
        directory.mkdir(exist_ok=True)
        script = directory / "checks.tcl"
        script.write_text(design_checks._script(self.candidate, directory, "xc7a35tcpg236-1")[0], encoding="utf-8")
        report = directory / "cdc.rpt"
        report.write_text(cdc, encoding="utf-8")
        evidence = {key: copy.deepcopy(self.candidate[key]) for key in ("rtl_task_id", "contract_task_id", "contract_hash", "top", "inputs")}
        evidence.update(schema_version=version, target_part="xc7a35tcpg236-1", artifacts=[{"path": str(path), "sha256": file_hash(path)} for path in (script, report)], commands=[])
        if version == 2:
            evidence["run_id"] = run_id
        for label, name in [("vivado", "vivado"), *([("verible", "verible-verilog-lint")] if lint else [])]:
            executable = directory / name
            executable.write_text("UNIT TEST IDENTITY ONLY; NOT EXECUTABLE", encoding="utf-8")
            log = directory / (label + ".log")
            log.write_text(console if label == "vivado" else "", encoding="utf-8")
            args = (["-mode", "batch", "-nojournal", "-nolog", "-source", str(script)] if label == "vivado" else
                    ["--ruleset=default", "--lint_fatal=true", "--parse_fatal=true", str(self.source)])
            evidence["commands"].append({"tool": label, "argv": [str(executable), *args], "executable_sha256": file_hash(executable),
                "exit_code": 0, "timed_out": False, "log": str(log), "log_sha256": file_hash(log)})
            if version == 2:
                evidence["commands"][-1]["cwd"] = str(design_checks._working_directory(self.root, run_id))
        return evidence

    def test_no_crossing_needs_clocks_registers_and_completed_report(self):
        self.assertEqual("passed", design_checks.parse_cdc(SAFE_CDC, CONSOLE, 1)["status"])
        for changed in (CONSOLE.replace("REGISTER_COUNT=1", "REGISTER_COUNT=0"),
                        CONSOLE.replace("UNCLOCKED_PINS=0", "UNCLOCKED_PINS=1"),
                        CONSOLE.replace("CDC_DONE=1", "CDC_DONE=0"), CONSOLE + "LOOPX_CDC_DONE=1\n"):
            self.assertEqual("blocked", design_checks.parse_cdc(SAFE_CDC, changed, 1)["status"])

    def test_cdc_findings_and_unrecognized_output_cannot_pass(self):
        for report in ("CDC Report\n| CDC-1 | Critical | 2 | Unknown CDC circuitry |\n| CDC-3 | Info | 1 | Synchronizer |\n",
                       "CDC Report\nID     Severity  Count  Description\nCDC-1  Critical      2  1-bit unknown CDC circuitry\nCDC-3  Info  1  Synchronizer\n"):
            result = design_checks.parse_cdc(report, CONSOLE, 1)
            self.assertEqual("failed", result["status"])
            self.assertEqual(2, result["findings"])
        self.assertEqual("blocked", design_checks.parse_cdc("successful", CONSOLE, 1)["status"])

    def test_missing_linter_remains_unavailable(self):
        result = design_checks._evaluate(self.candidate, self.evidence(lint=False), self.root)
        self.assertFalse(result["qualified"])
        self.assertEqual("passed", result["checks"]["elaboration"]["status"])
        self.assertEqual("unavailable", result["checks"]["lint"]["status"])

    def test_synthesis_warnings_cannot_hide_unsupported_logic(self):
        evidence = self.evidence(console=CONSOLE + "WARNING: unsupported assertion ignored\n")
        result = design_checks._evaluate(self.candidate, evidence, self.root)
        self.assertFalse(result["qualified"])
        self.assertEqual("failed", result["checks"]["elaboration"]["status"])

    def test_wrong_revision_mutated_source_and_modified_logs_rejected(self):
        evidence = self.evidence()
        changed = copy.deepcopy(evidence)
        changed["contract_hash"] = "b" * 64
        with self.assertRaises(ConflictError):
            design_checks._evaluate(self.candidate, changed, self.root)
        Path(evidence["commands"][0]["log"]).write_text("changed", encoding="utf-8")
        with self.assertRaises(ConflictError):
            design_checks._evaluate(self.candidate, evidence, self.root)
        evidence = self.evidence()
        self.source.write_text("changed", encoding="utf-8")
        with self.assertRaises(ConflictError):
            design_checks._evaluate(self.candidate, evidence, self.root)

    def test_disabling_lint_or_changing_command_is_rejected(self):
        evidence = self.evidence()
        evidence["commands"][1]["argv"][2] = "--lint_fatal=false"
        with self.assertRaisesRegex(ValidationError, "command"):
            design_checks._evaluate(self.candidate, evidence, self.root)
        evidence = self.evidence()
        self.candidate["required_checks"] = ["elaboration"]
        with self.assertRaisesRegex(ValidationError, "waive"):
            design_checks._evaluate(self.candidate, evidence, self.root)

    def test_clockless_missing_period_and_unknown_io_domain_are_blocked(self):
        for mutate in (lambda contract: contract.update(clocks=[]),
                       lambda contract: contract["clocks"][0].update(period_ns=None),
                       lambda contract: contract["ports"][1].update(clock_domain="unknown")):
            contract = copy.deepcopy(self.contract)
            mutate(contract)
            clocks, reason = design_checks._clock_constraints(contract)
            self.assertIsNone(clocks)
            self.assertTrue(reason)

    def test_tcl_quoting_and_port_bindings_are_literal(self):
        self.assertEqual('"a\\$b\\[c\\]\\"d"', design_checks._tcl('a$b[c]"d'))
        text, missing = design_checks._script(self.candidate, self.root, "xc7a35tcpg236-1")
        self.assertFalse(missing)
        self.assertIn("set_output_delay", text)
        self.assertIn("Shared contract port direction differs", text)
        with self.assertRaises(ValidationError):
            design_checks._script(self.candidate, self.root, "xc7;exec malicious")

    def test_readback_never_creates_check_state(self):
        before = self.project.store.status()
        self.assertEqual("not_run", design_checks.current_check(self.project, "missing")["status"])
        self.assertEqual(before, self.project.store.status())

    def test_versioned_working_directory_is_bound_to_run_identity(self):
        evidence = self.evidence(version=2)
        self.assertTrue(design_checks._evaluate(self.candidate, evidence, self.root)["qualified"])
        expected = self.root / ".lx-eda" / ("a" * 12)
        self.assertEqual(str(expected), evidence["commands"][0]["cwd"])
        evidence["commands"][0]["cwd"] = str(self.root)
        with self.assertRaisesRegex(ConflictError, "working directory"):
            design_checks._evaluate(self.candidate, evidence, self.root)
        evidence = self.evidence(version=2)
        del evidence["commands"][0]["cwd"]
        with self.assertRaisesRegex(ConflictError, "working directory"):
            design_checks._evaluate(self.candidate, evidence, self.root)
        evidence = self.evidence(version=2)
        evidence["run_id"] = "rtl_check_" + "b" * 32
        with self.assertRaisesRegex(ConflictError, "different runs"):
            design_checks._evaluate(self.candidate, evidence, self.root)

    def test_only_explicit_legacy_version_allows_missing_cwd(self):
        evidence = self.evidence()
        self.assertTrue(design_checks._evaluate(self.candidate, evidence, self.root)["qualified"])
        del evidence["schema_version"]
        with self.assertRaisesRegex(ValidationError, "version"):
            design_checks._evaluate(self.candidate, evidence, self.root)

    def test_working_directory_rejects_escape_alias_and_long_windows_root(self):
        with self.assertRaisesRegex(ValidationError, "identity"):
            design_checks._working_directory(self.root, "../../escape")
        if design_checks.os.name == "nt":
            with patch.object(design_checks, "_WINDOWS_CWD_LIMIT", 90):
                with self.assertRaisesRegex(ValidationError, "too long"):
                    design_checks._working_directory(self.root / ("x" * 90), "rtl_check_" + "a" * 32)
        link = self.root / ".lx-eda"
        target = self.root / "different-directory"
        target.mkdir()
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            return  # Windows may lack local symlink privileges.
        with self.assertRaisesRegex(ValidationError, "link"):
            design_checks._working_directory(self.root, "rtl_check_" + "a" * 32)

    def test_unavailable_tools_record_blocker_and_survive_restart(self):
        for kind, identifier in (("spec-contract", "contract-fixture"), ("rtl", "rtl-fixture")):
            task = self.project.store.add_task(kind, identifier, task_id=identifier)
            attempt = self.project.store.claim(task["id"], "fixture-session")
            path = self.root / (identifier + ".json")
            save_json(path, {"unit_test_fixture": True})
            self.project.store.finish(task["id"], attempt["id"], {"unit_test_fixture": True}, artifacts=[path],
                validator=lambda *_: {"passed": True, "checks": ["unit_test_fixture_only"]})
        with patch("loopx.capabilities.chip_verification.engine.design.get_candidate", return_value=self.candidate), \
             patch.object(design_checks, "_vivado_executable", return_value=None), \
             patch.object(design_checks, "_executable", return_value=None):
            result = design_checks.run_checks(self.project, "rtl-fixture")
            self.assertEqual("blocked", result["status"])
            self.assertFalse(result["qualified"])
            self.assertEqual("blocked", self.project.store.get_task(result["task_id"])["status"])
            restarted = DVProject(self.root, initialize=False)
            before = restarted.store.status()
            readback = design_checks.current_check(restarted, "rtl-fixture")
            self.assertFalse(readback["qualified"])
            self.assertEqual(result["task_id"], readback["task_id"])
            self.assertEqual(before, restarted.store.status())
            evidence = Path(result["evidence_path"])
            value = json.loads(evidence.read_text(encoding="utf-8"))
            value["inputs"] = {}
            save_json(evidence, value)
            self.assertIn("changed", design_checks.current_check(restarted, "rtl-fixture")["error"])


if __name__ == "__main__":
    unittest.main()
