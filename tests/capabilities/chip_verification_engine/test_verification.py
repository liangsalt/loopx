"""Adversarial acceptance tests; fixture reports are never benchmark evidence."""

import csv
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import jsonschema

from loopx.capabilities.chip_verification.engine.models import ValidationError
from loopx.capabilities.chip_verification.engine.simulators.xsim import SimulationError, parse_evidence, parse_xcrg_report
from loopx.capabilities.chip_verification.engine.verification import (
    _related_testplan,
    campaign_schema,
    close_coverage,
    digest,
    freeze_inputs,
    run_simulation,
    validate_simulation_evidence,
)
from loopx.capabilities.chip_verification.engine.workflow import DVProject


def native_report(hit_ids=(0, 1, 2), total=3):
    """A deliberately small fixture using XCRG's real text-report syntax."""
    rows = "\n".join(f"      requirement_{index},{int(index in hit_ids)},1,"
                     for index in range(total))
    return (
        "Xcrg Coverage Main Report - UNIT TEST FIXTURE\n"
        f"   Coverage Score            :, {100 * len(hit_ids) / total:.6f}\n"
        "   Total no of Cover Groups  :, 1\n"
        "   Total no of Instances     :, 1\n"
        " Cover Point Table for Inst : native_coverage, Variable :, targets\n"
        "      Name,       Hit Count,  AtLeast,\n" + rows + "\n"
    )


class SimulatorEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hits = self.root / "fixture_hits.csv"
        self.manifest = {
            "metric": "functional", "scope": "unit-test fixture only",
            "target_percent": 90.0,
            "features": [
                {"id": 0, "name": "instruction.ADD"},
                {"id": 1, "name": "branch.BEQ.taken"},
                {"id": 2, "name": "branch.BEQ.not_taken"},
            ],
        }
        self.rows = [{"id": feature["id"], "name": feature["name"], "hit": 1}
                     for feature in self.manifest["features"]]
        self.write_hits()

    def tearDown(self):
        self.temp.cleanup()

    def write_hits(self, rows=None):
        with self.hits.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["id", "name", "hit"])
            writer.writeheader()
            writer.writerows(self.rows if rows is None else rows)

    def log(self, *, checks=30, errors=0, fatals=0, percent="100.000000", hits=3, total=3):
        return (
            f"UVM_INFO reporter [CHECKS] DVFLOW_CHECKS={checks} RETIRED=12 BUS=24 TRAPS=1 RESETS=1\n"
            f"UVM_INFO reporter [COVERAGE] DVFLOW_NATIVE_COVERAGE={percent} HIT_BINS={hits} TOTAL_BINS={total}\n"
            f"UVM_ERROR : {errors}\nUVM_FATAL : {fatals}\n"
        )

    def test_recomputes_functional_coverage_without_claiming_code_coverage(self):
        self.rows[-1]["hit"] = 0
        self.write_hits()
        result = parse_evidence(self.log(percent="66.666667", hits=2), self.hits, self.manifest)
        self.assertEqual("functional", result["coverage"]["metric"])
        self.assertEqual(2, result["coverage"]["hit_bins"])
        self.assertEqual(3, result["coverage"]["total_bins"])
        self.assertAlmostEqual(200 / 3, result["coverage"]["percent"])
        self.assertEqual(["branch.BEQ.not_taken"], result["coverage"]["missing_bins"])
        self.assertNotIn("code_coverage", result)

    def test_zero_exit_style_message_without_real_observer_reports_is_rejected(self):
        for text in ("exit_code=0\nPASS\ncoverage=100", "UVM_ERROR : 0\nUVM_FATAL : 0"):
            with self.subTest(text=text):
                with self.assertRaises(SimulationError):
                    parse_evidence(text, self.hits, self.manifest)

    def test_zero_checker_activity_cannot_pass_even_with_all_bins_hit(self):
        with self.assertRaises(SimulationError):
            parse_evidence(self.log(checks=0), self.hits, self.manifest)

    def test_any_reported_uvm_error_or_fatal_rejects_coverage_closure(self):
        for error, fatal in ((1, 0), (0, 1), (20, 3)):
            with self.subTest(error=error, fatal=fatal):
                with self.assertRaises(SimulationError):
                    parse_evidence(self.log(errors=error, fatals=fatal), self.hits, self.manifest)

    def test_forged_percentage_cannot_override_observed_bin_counts(self):
        self.rows[-1]["hit"] = 0
        self.write_hits()
        with self.assertRaises(SimulationError):
            parse_evidence(self.log(percent="100.000000", hits=2), self.hits, self.manifest)

    def test_reported_denominator_cannot_drop_an_uncovered_bin(self):
        self.rows[-1]["hit"] = 0
        self.write_hits()
        with self.assertRaises(SimulationError):
            parse_evidence(self.log(percent="100.000000", hits=2, total=2), self.hits, self.manifest)

    def test_missing_or_duplicated_bin_inventory_is_rejected(self):
        for rows in (self.rows[:-1], [self.rows[0], self.rows[0], self.rows[2]]):
            with self.subTest(rows=rows):
                self.write_hits(rows)
                with self.assertRaises(SimulationError):
                    parse_evidence(self.log(), self.hits, self.manifest)

    def test_unknown_bin_and_non_boolean_hit_are_rejected(self):
        for field, value in (("name", "unapproved_new_bin"), ("id", 99), ("hit", 2)):
            with self.subTest(field=field):
                rows = [dict(row) for row in self.rows]
                rows[0][field] = value
                self.write_hits(rows)
                with self.assertRaises(SimulationError):
                    parse_evidence(self.log(), self.hits, self.manifest)

    def test_native_report_rejects_duplicate_ids_instead_of_overwriting(self):
        report = native_report() + "      requirement_0,1,1,\n"
        with self.assertRaises(SimulationError):
            parse_xcrg_report(report, 3, [0, 1, 2])

    def test_native_zero_hit_threshold_cannot_turn_unobserved_bins_into_hits(self):
        report = native_report().replace(",1,1,", ",0,0,")
        with self.assertRaises(SimulationError):
            parse_xcrg_report(report, 3, [0, 1, 2])


class EvidenceAcceptanceTests(unittest.TestCase):
    """The simulator is not run here; actual file/claim validation is exercised."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.benchmark = self.root / "fixture_benchmark"
        self.benchmark.mkdir()
        self.manifest = {
            "metric": "functional", "scope": "acceptance-test fixtures only", "target_percent": 90,
            "features": [
                {"id": 0, "name": "instruction.ADD", "category": "instruction"},
                {"id": 1, "name": "instruction.SUB", "category": "instruction"},
                {"id": 2, "name": "operand.ADD.zero", "category": "operand"},
            ],
        }
        self.campaign = self.benchmark / "campaign_smoke.json"
        inputs = {
            "spec.json": json.dumps({"behavior": "RV32I arithmetic behavior"}),
            "coverage_manifest.json": json.dumps(self.manifest),
            "programgen.py": "# Test fixture, never executed as a simulator\n",
            "upstream/picorv32.v": "module test_fixture; endmodule\n",
            "tb/cpu_uvm_pkg.sv": "package test_fixture_pkg; endpackage\n",
            "tb/coverage_defs.svh": "// Fixture coverage contract\n",
            "campaign_smoke.json": json.dumps({"instructions": ["ADD"]}),
        }
        for relative, text in inputs.items():
            path = self.benchmark / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.expected = freeze_inputs(self.benchmark, self.campaign)
        self.run_dir = self.root / "fixture_run"
        self.evidence = self.make_evidence(self.run_dir)

    def tearDown(self):
        self.temp.cleanup()

    def make_evidence(self, run_dir, campaign_path=None):
        run = Path(run_dir)
        run.mkdir(parents=True, exist_ok=True)
        campaign = Path(campaign_path) if campaign_path else self.campaign
        frozen = freeze_inputs(self.benchmark, campaign)
        log = (
            "UVM_INFO reporter [CHECKS] DVFLOW_CHECKS=30 RETIRED=12 BUS=24 TRAPS=1 RESETS=1\n"
            "UVM_INFO reporter [COVERAGE] DVFLOW_NATIVE_COVERAGE=100.000000 HIT_BINS=3 TOTAL_BINS=3\n"
            "UVM_ERROR : 0\nUVM_FATAL : 0\n"
        )
        commands = []
        for tool in ("xvlog", "xelab", "xsim", "xcrg"):
            path = run / f"{tool}_console.log"
            path.write_text(log if tool == "xsim" else "Unit-test fixture tool output\n", encoding="utf-8")
            commands.append({"tool": tool, "exit_code": 0, "log": str(path), "log_sha256": digest(path),
                             "argv": [tool], "seconds": 0})
        hits = run / "coverage_hits.csv"
        with hits.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["id", "name", "hit"])
            writer.writeheader()
            writer.writerows({"id": feature["id"], "name": feature["name"], "hit": 1}
                            for feature in self.manifest["features"])
        database = run / "xsim.covdb" / "fixture.cov"
        report = run / "coverage_report" / "xcrg_report.txt"
        for path in (database, report):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Unit-test fixture, not real EDA evidence\n", encoding="utf-8")
        report.write_text(native_report(), encoding="utf-8")
        measured = parse_evidence(log, hits, self.manifest)
        value = {
            **measured, "schema_version": 1, "backend": "vivado_xsim", "mutation": False,
            "benchmark": str(self.benchmark), "run_dir": str(run), "seed": 1,
            "spec_sha256": digest(self.benchmark / "spec.json"),
            "rtl_sha256": digest(self.benchmark / "upstream" / "picorv32.v"),
            "coverage_manifest_sha256": digest(self.benchmark / "coverage_manifest.json"),
            "campaign_sha256": digest(campaign),
            "inputs": [{"path": path, "sha256": sha} for path, sha in frozen.items()],
            "commands": commands,
            "artifacts": [{"path": str(path), "sha256": digest(path)} for path in (hits, report, database)],
            "tool": {"home": "test-only", "version": "fixture", "uvm": "1.2", "dpi": False},
        }
        value["coverage"]["manifest_sha256"] = value["coverage_manifest_sha256"]
        (run / "evidence.json").write_text(json.dumps(value), encoding="utf-8")
        return value

    def test_consistent_report_is_reparsed_and_keeps_functional_metric(self):
        verdict = validate_simulation_evidence(self.evidence, self.benchmark, self.expected)
        self.assertTrue(verdict["passed"])
        self.assertEqual("functional", verdict["measured"]["coverage"]["metric"])
        self.assertEqual(100.0, verdict["measured"]["coverage"]["xcrg_percent"])

    def test_claim_only_success_cannot_replace_build_run_and_report(self):
        evidence = copy.deepcopy(self.evidence)
        evidence["commands"] = []
        with self.assertRaises(ValidationError):
            validate_simulation_evidence(evidence, self.benchmark, self.expected)

    def test_changed_rtl_tb_manifest_and_campaign_are_all_rejected(self):
        for path in (self.benchmark / "upstream" / "picorv32.v",
                     self.benchmark / "tb" / "cpu_uvm_pkg.sv",
                     self.benchmark / "coverage_manifest.json", self.campaign):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b"\nchanged after simulation\n")
                    with self.assertRaises(ValidationError):
                        validate_simulation_evidence(self.evidence, self.benchmark, self.expected)
                finally:
                    path.write_bytes(original)

    def test_a_valid_old_campaign_cannot_satisfy_a_different_expected_campaign(self):
        new_campaign = self.benchmark / "different_campaign.json"
        new_campaign.write_text(json.dumps({"instructions": ["SUB"]}), encoding="utf-8")
        expected_new = freeze_inputs(self.benchmark, new_campaign)
        with self.assertRaises(ValidationError):
            validate_simulation_evidence(self.evidence, self.benchmark, expected_new)

    def test_missing_recorded_input_cannot_be_covered_by_current_files(self):
        evidence = copy.deepcopy(self.evidence)
        evidence["inputs"] = []
        with self.assertRaises(ValidationError):
            validate_simulation_evidence(evidence, self.benchmark, self.expected)

    def test_failed_tool_and_tampered_log_reject_a_claimed_pass(self):
        evidence = copy.deepcopy(self.evidence)
        evidence["commands"][1]["exit_code"] = 1
        with self.assertRaises(ValidationError):
            validate_simulation_evidence(evidence, self.benchmark, self.expected)
        (self.run_dir / "xsim_console.log").write_text("Forged PASS", encoding="utf-8")
        with self.assertRaises(ValidationError):
            validate_simulation_evidence(self.evidence, self.benchmark, self.expected)

    def test_a_different_clean_log_cannot_replace_the_recorded_run_log(self):
        evidence = copy.deepcopy(self.evidence)
        alternate = self.root / "unrelated_clean_log.txt"
        alternate.write_text((self.run_dir / "xsim_console.log").read_text(encoding="utf-8"), encoding="utf-8")
        evidence["commands"][2]["log"] = str(alternate)
        evidence["commands"][2]["log_sha256"] = digest(alternate)
        with self.assertRaises(ValidationError):
            validate_simulation_evidence(evidence, self.benchmark, self.expected)

    def test_functional_bins_cannot_be_relabelled_as_code_coverage(self):
        evidence = copy.deepcopy(self.evidence)
        evidence["coverage"]["metric"] = "code"
        with self.assertRaises(ValidationError):
            validate_simulation_evidence(evidence, self.benchmark, self.expected)

    def test_non_finite_claimed_percentage_is_rejected(self):
        evidence = copy.deepcopy(self.evidence)
        evidence["coverage"]["percent"] = "NaN"
        with self.assertRaises(ValidationError):
            validate_simulation_evidence(evidence, self.benchmark, self.expected)

    def test_raw_data_wins_over_claimed_counts_and_checker_activity(self):
        for field in ("coverage", "checks"):
            with self.subTest(field=field):
                evidence = copy.deepcopy(self.evidence)
                if field == "coverage":
                    evidence["coverage"]["percent"] = 90.5
                else:
                    evidence["checks"]["checks"] = 99999
                with self.assertRaises(ValidationError):
                    validate_simulation_evidence(evidence, self.benchmark, self.expected)

    def test_missing_native_database_or_report_is_rejected(self):
        for relative in ("xsim.covdb/fixture.cov", "coverage_report/xcrg_report.txt"):
            with self.subTest(relative=relative):
                path = self.run_dir / relative
                original = path.read_bytes()
                try:
                    path.unlink()
                    with self.assertRaises(ValidationError):
                        validate_simulation_evidence(self.evidence, self.benchmark, self.expected)
                finally:
                    path.write_bytes(original)

    def test_native_per_bin_tamper_is_rejected_even_with_updated_file_digest(self):
        evidence = copy.deepcopy(self.evidence)
        report = self.run_dir / "coverage_report" / "xcrg_report.txt"
        report.write_text(native_report([0, 1]), encoding="utf-8")
        for artifact in evidence["artifacts"]:
            artifact["sha256"] = digest(artifact["path"])
        with self.assertRaises(SimulationError):
            validate_simulation_evidence(evidence, self.benchmark, self.expected)

    def test_equal_native_percentage_cannot_substitute_different_hit_bins(self):
        evidence = copy.deepcopy(self.evidence)
        hits = self.run_dir / "coverage_hits.csv"
        with hits.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["id", "name", "hit"])
            writer.writeheader()
            writer.writerows({"id": feature["id"], "name": feature["name"],
                              "hit": int(feature["id"] in [0, 1])}
                             for feature in self.manifest["features"])
        log_path = self.run_dir / "xsim_console.log"
        log = log_path.read_text(encoding="utf-8").replace(
            "COVERAGE=100.000000 HIT_BINS=3", "COVERAGE=66.666667 HIT_BINS=2")
        log_path.write_text(log, encoding="utf-8")
        native_path = self.run_dir / "coverage_report" / "xcrg_report.txt"
        native_path.write_text(native_report([0, 2]), encoding="utf-8")
        evidence.update(parse_evidence(log, hits, self.manifest))
        for command in evidence["commands"]:
            command["log_sha256"] = digest(command["log"])
        for artifact in evidence["artifacts"]:
            artifact["sha256"] = digest(artifact["path"])
        with self.assertRaises(SimulationError):
            validate_simulation_evidence(evidence, self.benchmark, self.expected)

    def test_negative_mutation_cannot_contribute_even_if_log_looks_clean(self):
        evidence = copy.deepcopy(self.evidence)
        evidence["mutation"] = True
        with self.assertRaises(ValidationError):
            validate_simulation_evidence(evidence, self.benchmark, self.expected)

    def test_real_state_accepts_only_after_backend_artifact_validation(self):
        project = DVProject(self.root)
        project.import_spec(self.benchmark / "spec.json", scope="fixture arithmetic")
        with patch("loopx.capabilities.chip_verification.engine.simulators.xsim.XSimBackend") as backend:
            backend.return_value.run_benchmark.side_effect = (
                lambda benchmark, run_dir, **kwargs: self.make_evidence(run_dir, kwargs.get("campaign_path"))
            )
            result = run_simulation(project, self.benchmark)
        self.assertEqual("passed", result["status"], result.get("error"))
        self.assertEqual("done", project.store.get_task(result["task_id"])["status"])
        self.assertEqual("passed", project.store.get_run(result["run_id"])["status"])
        self.assertEqual("functional", result["coverage"]["metric"])

    def test_final_state_rejection_cannot_return_a_successful_simulation_task(self):
        project = DVProject(self.root)
        project.import_spec(self.benchmark / "spec.json", scope="fixture arithmetic")
        with patch("loopx.capabilities.chip_verification.engine.simulators.xsim.XSimBackend") as backend, patch.object(
                project.store, "finish", side_effect=ValidationError("Acceptance rejected by final validator")):
            backend.return_value.run_benchmark.side_effect = (
                lambda benchmark, run_dir, **kwargs: self.make_evidence(run_dir, kwargs.get("campaign_path"))
            )
            result = run_simulation(project, self.benchmark)
        self.assertEqual("failed", result["status"])
        self.assertNotEqual("done", project.store.get_task(result["task_id"])["status"])
        self.assertEqual([], project.store.list_artifacts(task_id=result["task_id"]))

    def test_campaign_cannot_add_code_changes_or_unsupported_operations(self):
        schema = campaign_schema(self.manifest)
        proposal = {
            "campaign": {"instructions": ["ADD"], "operand_classes": ["zero"],
                         "both_branch_outcomes": True, "all_memory_offsets": True,
                         "division_exceptions": True, "x0_discard": True,
                         "random_arithmetic_count": 10, "seed": 1},
            "rationale": "Exercise an existing target with new stimulus only",
        }
        jsonschema.validate(proposal, schema)
        for change in ({"instructions": ["EDIT_CHECKER"]}, {"coverage_exclusions": ["uncovered"]},
                       {"random_arithmetic_count": 1001}):
            with self.subTest(change=change):
                candidate = copy.deepcopy(proposal)
                candidate["campaign"].update(change)
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(candidate, schema)


class CoverageResumeTests(unittest.TestCase):
    """Persist/reopen real state with fixture logs; no model or EDA is launched."""

    setUp = EvidenceAcceptanceTests.setUp
    tearDown = EvidenceAcceptanceTests.tearDown
    make_evidence = EvidenceAcceptanceTests.make_evidence

    def project_with_plan(self):
        project = DVProject(self.root)
        project.import_spec(self.benchmark / "spec.json", scope="resume test fixture")
        task = project.store.add_task("plan", "Fixture accepted plan", {})
        attempt = project.store.claim(task["id"], "unit-test")
        artifact = self.root / "accepted_plan_fixture.json"
        plan = {"testpoints": [{"name": "arithmetic", "stimulus": "instruction operands",
                                 "checkers": "independent result comparison", "coverage": "observed operations"}]}
        artifact.write_text(json.dumps(plan), encoding="utf-8")
        project.store.finish(task["id"], attempt["id"], plan, artifacts=[artifact],
                             validator=lambda *_: {"passed": True, "checks": ["unit_test_fixture"]})
        return project

    def measured_fixture(self, directory, hit_ids, campaign_path=None):
        evidence = self.make_evidence(directory, campaign_path)
        run = Path(directory)
        hits = run / "coverage_hits.csv"
        with hits.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["id", "name", "hit"])
            writer.writeheader()
            writer.writerows({"id": feature["id"], "name": feature["name"],
                              "hit": int(feature["id"] in hit_ids)} for feature in self.manifest["features"])
        log_path = run / "xsim_console.log"
        log = log_path.read_text(encoding="utf-8").replace(
            "COVERAGE=100.000000 HIT_BINS=3", f"COVERAGE={100 * len(hit_ids) / 3:.6f} HIT_BINS={len(hit_ids)}")
        log_path.write_text(log, encoding="utf-8")
        (run / "coverage_report/xcrg_report.txt").write_text(native_report(hit_ids), encoding="utf-8")
        evidence.update(parse_evidence(log, hits, self.manifest))
        for command in evidence["commands"]:
            command["log_sha256"] = digest(command["log"])
        for artifact in evidence["artifacts"]:
            artifact["sha256"] = digest(artifact["path"])
        (run / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
        return evidence

    def accepted_history(self, project, name, hit_ids, *, mutation=False, run_status="passed", evidence_status="passed"):
        """Allow intentionally bad history to test independent resume validation."""
        directory = self.root / ".loopx/chip-verification/runs" / name
        evidence = self.measured_fixture(directory, hit_ids)
        evidence.update(mutation=mutation, status=evidence_status)
        inputs = freeze_inputs(self.benchmark, self.campaign)
        (directory / "frozen_inputs.json").write_text(json.dumps(inputs), encoding="utf-8")
        (directory / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
        task = project.store.add_task("regress", "Fixture " + name,
            {"benchmark": str(self.benchmark), "campaign": str(self.campaign), "inputs": inputs}, task_id=name)
        attempt = project.store.claim(task["id"], "unit-test")
        project.store.record_run(name, "vivado_xsim", status=run_status, task_id=task["id"],
                                 attempt_id=attempt["id"], metadata={"run_dir": str(directory), "input_hashes": inputs})
        result = {"run_id": name, "evidence": evidence, "coverage": evidence["coverage"]}
        project.store.finish(task["id"], attempt["id"], result,
                             artifacts=[path for path in directory.rglob("*") if path.is_file()],
                             validator=lambda *_: {"passed": True, "checks": ["unit_test_history_fixture"]})
        return task["id"], evidence

    def accepted_proposal(self, project, previous_id, evidence, *, receipt_changes=None, accept=True):
        task = project.store.add_task("analyze", "Generate stimulus for observed UVM coverage gaps",
            {"run_id": previous_id, "missing_bins": evidence["coverage"]["missing_bins"],
             "coverage_manifest_sha256": digest(self.benchmark / "coverage_manifest.json")},
            depends_on=[previous_id])
        attempt = project.store.claim(task["id"], "unit-campaign-fixture")
        directory = self.root / ".loopx/chip-verification/attempts" / attempt["id"]
        directory.mkdir(parents=True)
        proposal = {"campaign": {"instructions": ["ADD", "SUB"], "operand_classes": ["zero"],
                                  "both_branch_outcomes": True, "all_memory_offsets": True,
                                  "division_exceptions": True, "x0_discard": True,
                                  "random_arithmetic_count": 0, "seed": 2}, "rationale": "Fixture stimulus only"}
        receipt = {"runtime": "codex", "attempt_id": attempt["id"], "returncode": 0,
                   "session_id": "unit-accepted-campaign-session", "response": proposal,
                   "usage": {}, "prompt_chars": 1000, **(receipt_changes or {})}
        for name, value in (("campaign.json", proposal["campaign"]), ("response.json", proposal), ("receipt.json", receipt)):
            (directory / name).write_text(json.dumps(value), encoding="utf-8")
        if accept:
            project.store.finish(task["id"], attempt["id"],
                {"proposal": proposal, "session_id": "unit-accepted-campaign-session", "usage": {}},
                artifacts=[directory / name for name in ("campaign.json", "response.json", "receipt.json")],
                validator=lambda *_: {"passed": True, "checks": ["unit_fixture_only"]})
        else:
            project.store.fail(task["id"], attempt["id"], "Unit fixture rejected before acceptance", retryable=False)
        return project.store.get_task(task["id"]), directory

    def test_accepted_proposal_resumes_after_interruption_without_new_model_or_claim(self):
        project = self.project_with_plan()
        previous, evidence = self.accepted_history(project, "accepted-before-interruption", [0])
        proposal, directory = self.accepted_proposal(project, previous, evidence)
        before_artifacts = project.store.list_artifacts(task_id=proposal["id"])
        with patch("loopx.capabilities.chip_verification.engine.verification.runtime", side_effect=AssertionError("Accepted proposal must not call a model")), patch(
                "loopx.capabilities.chip_verification.engine.simulators.xsim.XSimBackend") as backend:
            backend.return_value.run_benchmark.side_effect = lambda bench, run, **kwargs: self.measured_fixture(
                run, [0, 1, 2], kwargs.get("campaign_path"))
            result = close_coverage(DVProject(self.root), self.benchmark, max_iterations=1)
        self.assertTrue(result["target_met"])
        self.assertTrue(result["runs"][-1]["proposal_reused"])
        self.assertEqual(directory / "campaign.json", backend.return_value.run_benchmark.call_args.kwargs["campaign_path"])
        self.assertEqual(proposal, project.store.get_task(proposal["id"]))
        self.assertEqual(before_artifacts, project.store.list_artifacts(task_id=proposal["id"]))

    def test_failed_post_proposal_simulation_can_retry_same_accepted_campaign(self):
        project = self.project_with_plan()
        previous, evidence = self.accepted_history(project, "before-failed-followup", [0])
        proposal, directory = self.accepted_proposal(project, previous, evidence)
        with patch("loopx.capabilities.chip_verification.engine.verification.runtime", side_effect=AssertionError("No model expected")), patch(
                "loopx.capabilities.chip_verification.engine.simulators.xsim.XSimBackend") as backend:
            backend.return_value.run_benchmark.return_value = {"status": "failed", "error": "Fixture simulator failure"}
            failed = close_coverage(project, self.benchmark, max_iterations=1)
            self.assertFalse(failed["target_met"])
            backend.return_value.run_benchmark.side_effect = lambda bench, run, **kwargs: self.measured_fixture(
                run, [0, 1, 2], kwargs.get("campaign_path"))
            retried = close_coverage(DVProject(self.root), self.benchmark, max_iterations=1)
        self.assertTrue(retried["target_met"])
        self.assertEqual(2, backend.return_value.run_benchmark.call_count)
        self.assertEqual(proposal, project.store.get_task(proposal["id"]))
        self.assertEqual("failed", project.store.get_run(failed["runs"][-1]["run_id"])["status"])
        self.assertNotEqual(failed["runs"][-1]["run_id"], retried["runs"][-1]["run_id"])
        self.assertTrue(all(call.kwargs["campaign_path"] == directory / "campaign.json"
                            for call in backend.return_value.run_benchmark.call_args_list))

    def test_modified_accepted_campaign_is_not_reused(self):
        project = self.project_with_plan()
        previous, evidence = self.accepted_history(project, "before-campaign-tamper", [0])
        proposal, directory = self.accepted_proposal(project, previous, evidence)
        (directory / "campaign.json").write_text('{"instructions":["SUB"]}', encoding="utf-8")
        with patch("loopx.capabilities.chip_verification.engine.verification.runtime", side_effect=AssertionError("No model expected")), patch(
                "loopx.capabilities.chip_verification.engine.verification.run_simulation", side_effect=AssertionError("Stale campaign must not run")):
            result = close_coverage(project, self.benchmark, max_iterations=1)
        self.assertFalse(result["target_met"])
        self.assertEqual("stale", project.store.get_task(proposal["id"])["status"])

    def test_inconsistent_original_receipt_rejects_campaign_even_when_file_hash_was_accepted(self):
        project = self.project_with_plan()
        previous, evidence = self.accepted_history(project, "before-invalid-receipt", [0])
        proposal, _ = self.accepted_proposal(project, previous, evidence,
                                             receipt_changes={"attempt_id": "unrelated-attempt"})
        with patch("loopx.capabilities.chip_verification.engine.verification.runtime", side_effect=AssertionError("No model expected")), patch(
                "loopx.capabilities.chip_verification.engine.verification.run_simulation", side_effect=AssertionError("Unauthenticated campaign must not run")):
            result = close_coverage(project, self.benchmark, max_iterations=1)
        self.assertFalse(result["target_met"])
        self.assertIn("receipt", result["reason"])
        self.assertEqual("stale", project.store.get_task(proposal["id"])["status"])

    def test_unaccepted_failed_proposal_files_cannot_be_reused(self):
        project = self.project_with_plan()
        previous, evidence = self.accepted_history(project, "before-unaccepted-proposal", [0])
        proposal, _ = self.accepted_proposal(project, previous, evidence, accept=False)
        with patch("loopx.capabilities.chip_verification.engine.verification.runtime", side_effect=AssertionError("No silent retry expected")), patch(
                "loopx.capabilities.chip_verification.engine.verification.run_simulation", side_effect=AssertionError("Unaccepted campaign must not run")):
            result = close_coverage(project, self.benchmark, max_iterations=1)
        self.assertFalse(result["target_met"])
        self.assertEqual("failed", project.store.get_task(proposal["id"])["status"])

    def test_active_external_run_is_not_duplicated_when_reusing_accepted_campaign(self):
        project = self.project_with_plan()
        previous, evidence = self.accepted_history(project, "before-active-external", [0])
        proposal, _ = self.accepted_proposal(project, previous, evidence)
        task = project.store.add_task("regress", "Existing external process", {}, depends_on=[proposal["id"]])
        attempt = project.store.claim(task["id"], "other-simulator-host", lease_seconds=600)
        project.store.record_run("still-running", "vivado_xsim", status="building",
                                 task_id=task["id"], attempt_id=attempt["id"])
        with patch("loopx.capabilities.chip_verification.engine.verification.runtime", side_effect=AssertionError("No model expected")), patch(
                "loopx.capabilities.chip_verification.engine.verification.run_simulation", side_effect=AssertionError("No duplicate external run expected")):
            result = close_coverage(DVProject(self.root), self.benchmark, max_iterations=1)
        self.assertEqual(["still-running"], result["pending_run_ids"])
        self.assertEqual("running", project.store.get_task(task["id"])["status"])
        self.assertEqual(proposal, project.store.get_task(proposal["id"]))

    def test_new_session_reuses_current_done_run_without_launching_work(self):
        project = self.project_with_plan()
        run_id, _ = self.accepted_history(project, "accepted-full", [0, 1, 2])
        prior_tasks = len(project.store.list_tasks())
        notifications = []
        reopened = DVProject(self.root)
        with patch("loopx.capabilities.chip_verification.engine.verification.run_simulation", side_effect=AssertionError("No new EDA expected")), patch(
                "loopx.capabilities.chip_verification.engine.verification.runtime", side_effect=AssertionError("No model expected")):
            result = close_coverage(reopened, self.benchmark, max_iterations=1, agent="pi", notify=notifications.append)
        self.assertTrue(result["target_met"])
        self.assertTrue(result["resumed"])
        self.assertEqual(run_id, result["runs"][0]["run_id"])
        self.assertTrue(result["runs"][0]["reused"])
        self.assertTrue(notifications[0]["reused"])
        self.assertEqual(prior_tasks, len(reopened.store.list_tasks()))

    def test_best_valid_coverage_wins_over_newer_lower_coverage(self):
        project = self.project_with_plan()
        best, _ = self.accepted_history(project, "older-full", [0, 1, 2])
        self.accepted_history(project, "newer-low", [0])
        with patch("loopx.capabilities.chip_verification.engine.verification.run_simulation", side_effect=AssertionError("Should reuse best run")):
            result = close_coverage(project, self.benchmark, max_iterations=1)
        self.assertEqual(best, result["runs"][0]["run_id"])
        self.assertEqual(100, result["runs"][0]["coverage"]["percent"])

    def test_tampered_best_run_is_invalidated_and_valid_lower_run_is_reused(self):
        project = self.project_with_plan()
        good, _ = self.accepted_history(project, "valid-low", [0])
        bad, evidence = self.accepted_history(project, "tampered-full", [0, 1, 2])
        Path(evidence["commands"][2]["log"]).write_text("Changed after state acceptance", encoding="utf-8")
        with patch("loopx.capabilities.chip_verification.engine.verification.run_simulation", side_effect=AssertionError("Valid lower run suffices")):
            result = close_coverage(project, self.benchmark, target=30, max_iterations=1)
        self.assertEqual(good, result["runs"][0]["run_id"])
        self.assertNotEqual("done", project.store.get_task(bad)["status"])

    def test_failed_mutation_and_unaccepted_runs_cannot_be_reused(self):
        project = self.project_with_plan()
        good, _ = self.accepted_history(project, "valid-low", [0])
        self.accepted_history(project, "negative-full", [0, 1, 2], mutation=True)
        self.accepted_history(project, "failed-external-job", [0, 1, 2], run_status="failed")
        self.accepted_history(project, "failed-evidence", [0, 1, 2], evidence_status="failed")
        unaccepted, _ = self.accepted_history(project, "stale-full", [0, 1, 2])
        project.store.invalidate(unaccepted, "Unit-test unaccepted history")
        with patch("loopx.capabilities.chip_verification.engine.verification.run_simulation", side_effect=AssertionError("Valid lower run suffices")):
            result = close_coverage(project, self.benchmark, target=30, max_iterations=1)
        self.assertEqual(good, result["runs"][0]["run_id"])
        self.assertNotEqual("done", project.store.get_task("negative-full")["status"])

    def test_changed_input_inventory_forces_smoke_even_when_old_hashes_still_match(self):
        project = self.project_with_plan()
        previous, _ = self.accepted_history(project, "accepted-before-new-header", [0, 1, 2])
        (self.benchmark / "tb/new_checker_contract.svh").write_text("// New input absent from old inventory", encoding="utf-8")
        fallback = {"status": "failed", "error": "Unit-test sentinel initial run"}
        with patch("loopx.capabilities.chip_verification.engine.verification.run_simulation", return_value=fallback) as simulator:
            result = close_coverage(project, self.benchmark, max_iterations=1)
        simulator.assert_called_once()
        self.assertFalse(result["runs"][0]["reused"])
        self.assertNotEqual("done", project.store.get_task(previous)["status"])

    def test_changed_campaign_forces_smoke(self):
        project = self.project_with_plan()
        self.accepted_history(project, "prior-campaign", [0, 1, 2])
        self.campaign.write_text(json.dumps({"instructions": ["SUB"]}), encoding="utf-8")
        with patch("loopx.capabilities.chip_verification.engine.verification.run_simulation", return_value={"status": "failed", "error": "fixture"}) as simulator:
            result = close_coverage(project, self.benchmark, max_iterations=1)
        simulator.assert_called_once()
        self.assertFalse(result["runs"][0]["reused"])

    def test_fresh_true_explicitly_runs_initial_smoke_again(self):
        project = self.project_with_plan()
        old, _ = self.accepted_history(project, "prior-full", [0, 1, 2])
        with patch("loopx.capabilities.chip_verification.engine.simulators.xsim.XSimBackend") as backend:
            backend.return_value.run_benchmark.side_effect = lambda benchmark, directory, **kwargs: self.measured_fixture(
                directory, [0, 1, 2], kwargs.get("campaign_path"))
            result = close_coverage(project, self.benchmark, max_iterations=1, fresh=True)
        backend.return_value.run_benchmark.assert_called_once()
        self.assertTrue(result["target_met"])
        self.assertFalse(result["resumed"])
        self.assertFalse(result["runs"][0]["reused"])
        self.assertNotEqual(old, result["runs"][0]["run_id"])

    def test_repeated_single_iteration_sessions_continue_measured_gaps(self):
        project = self.project_with_plan()
        self.accepted_history(project, "initial-low", [0])
        prompts = []
        proposal = {"campaign": {"instructions": ["ADD", "SUB"], "operand_classes": ["zero"],
                                 "both_branch_outcomes": True, "all_memory_offsets": True,
                                 "division_exceptions": True, "x0_discard": True,
                                 "random_arithmetic_count": 0, "seed": 2}, "rationale": "Fixture stimulus"}

        def model_fixture(prompt, **kwargs):
            prompts.append(json.loads(prompt.split("\n", 1)[1]))
            directory = kwargs["run_dir"]
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "response.json").write_text(json.dumps(proposal), encoding="utf-8")
            (directory / "receipt.json").write_text(json.dumps({"unit_test_fixture": True}), encoding="utf-8")
            return SimpleNamespace(response=proposal, session_id="unit-test-session", usage={})

        hit_sets = iter(([0, 1], [0, 1, 2]))
        with patch("loopx.capabilities.chip_verification.engine.verification.runtime") as runtime_factory, patch("loopx.capabilities.chip_verification.engine.simulators.xsim.XSimBackend") as backend:
            runtime_factory.return_value.run.side_effect = model_fixture
            backend.return_value.run_benchmark.side_effect = lambda benchmark, directory, **kwargs: self.measured_fixture(
                directory, next(hit_sets), kwargs.get("campaign_path"))
            first = close_coverage(project, self.benchmark, max_iterations=1, agent="pi")
            self.assertFalse(first["target_met"])
            self.assertEqual(1, backend.return_value.run_benchmark.call_count)
            second = close_coverage(DVProject(self.root), self.benchmark, max_iterations=1, agent="pi")
        self.assertTrue(second["target_met"])
        self.assertEqual(2, backend.return_value.run_benchmark.call_count)
        self.assertEqual(first["runs"][-1]["run_id"], second["runs"][0]["run_id"])
        self.assertTrue(second["runs"][0]["reused"])
        self.assertFalse(second["runs"][-1]["reused"])
        self.assertEqual([100 / 3, 200 / 3], [prompt["previous_coverage"]["percent"] for prompt in prompts])
        self.assertEqual(["operand.ADD.zero"], prompts[-1]["previous_coverage"]["missing_bins"])
        self.assertEqual(2, runtime_factory.call_count)
        for call in runtime_factory.call_args_list:
            self.assertEqual(("pi",), call.args)
            self.assertIsNone(call.kwargs["max_prompt_chars"])


class CoverageContextTests(unittest.TestCase):
    def project(self, points, requirements=()):
        store = SimpleNamespace(
            list_tasks=lambda **query: [{"id": "accepted-plan", "result": {"testpoints": points}}]
            if query["kind"] == "plan" else [],
            list_requirements=lambda: requirements)
        return SimpleNamespace(store=store)

    def test_gap_relationship_selects_complete_testpoint_beyond_old_length_cutoff(self):
        unrelated = {"id": "unrelated", "name": "other", "stimulus": "x" * 10000, "coverage": ["other"]}
        relevant = {"id": "late", "name": "reset sequencing", "requirement_ids": ["reset-req"],
                    "stimulus": "full timing behavior " * 2000, "checkers": ["independent reset predictor"],
                    "coverage": ["reset transition"], "configurations": ["all domain phases"]}
        project = self.project([unrelated, relevant])
        selected = _related_testplan(project, {"features": [{"name": "reset-gap", "requirement_ids": ["reset-req"]}]}, ["reset-gap"])
        self.assertEqual([{**relevant, "plan_task_id": "accepted-plan"}], selected)

    def test_unknown_gap_preserves_all_accepted_semantics_instead_of_first_text_prefix(self):
        points = [{"id": str(index), "name": "opaque feature " + str(index), "stimulus": "x" * 10000,
                   "coverage": ["unspecified mapping"], "checkers": ["complete checker"], "extra_contract": "preserve"}
                  for index in range(3)]
        selected = _related_testplan(self.project(points), {"features": [{"name": "unknown-gap"}]}, ["unknown-gap"])
        self.assertEqual([{**point, "plan_task_id": "accepted-plan"} for point in points], selected)

    def test_requirement_feature_mapping_selects_gap_and_keeps_cross_module_point_intact(self):
        point = {"id": "cross-module", "requirement_ids": ["a", "b"], "coverage": ["whole relationship"]}
        project = self.project([{"id": "unrelated"}, point], [{"id": "b", "metadata": {"features": ["cross-domain-reset"]}}])
        selected = _related_testplan(project, {"features": [{"name": "cross-domain-reset"}]}, ["cross-domain-reset"])
        self.assertEqual([{**point, "plan_task_id": "accepted-plan"}], selected)


if __name__ == "__main__":
    unittest.main()
