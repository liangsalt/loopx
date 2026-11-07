"""Projection lifecycle tests; no mocked simulator/agent result is real evidence.

These tests use real SQLite acceptance/invalidation, but a deliberately fake
validation oracle so only report aggregation, provenance and refresh are tested.
Actual simulator evidence rejection is covered separately in test_verification.
"""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from loopx.capabilities.chip_verification.engine.models import ValidationError
from loopx.capabilities.chip_verification.engine.report import write_report
from loopx.capabilities.chip_verification.engine.state import StateStore


class ProjectionProject:
    def __init__(self, root):
        self.root = root
        self.store = StateStore(root)
        spec = root / "spec.json"
        spec.write_text('{"unit_test_only": true}', encoding="utf-8")
        # Size is a projection fixture, deliberately not an acceptance result.
        self.store.initialize("Projection fixture", spec,
                              hashlib.sha256(spec.read_bytes()).hexdigest(), 250 * 1024)

    def status(self):
        changed = self.store.verify_artifacts()
        return {**self.store.status(), "scope": "Unit test fixture only", "changed_artifacts": changed}


class ReportProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = ProjectionProject(self.root)
        self.store = self.project.store
        self.sequence = 0
        self.source = self.store.add_source("/behavior", "A unit-test source.")

    def tearDown(self):
        self.temp.cleanup()

    def complete(self, kind, result, *, depends_on=None, receipt=None, payload=None):
        self.sequence += 1
        task = self.store.add_task(kind, f"Fixture {self.sequence}", payload or {},
                                   depends_on=depends_on or [], task_id=f"fixture_{self.sequence}")
        attempt = self.store.claim(task["id"], "fixture-owner")
        directory = self.root / ".loopx" / "chip-verification" / "attempts" / attempt["id"]
        directory.mkdir(parents=True)
        result_path = directory / "result.json"
        result_path.write_text(json.dumps(result), encoding="utf-8")
        artifacts = [result_path]
        if receipt is not None:
            value = {"runtime": "codex", "session_id": f"fixture-session-{self.sequence}",
                     "attempt_id": attempt["id"], "returncode": 0, "prompt_chars": 1000,
                     "usage": {"input_tokens": 300}, **receipt}
            receipt_path = directory / "receipt.json"
            receipt_path.write_text(json.dumps(value), encoding="utf-8")
            artifacts.append(receipt_path)
        self.store.finish(task["id"], attempt["id"], result, artifacts,
                          validator=lambda *_: {"passed": True, "checks": ["unit_test_fixture_only"]})
        return self.store.get_task(task["id"])

    def source_plan(self):
        model = self.complete("model", {"dispositions": [
            {"source_id": self.source["id"], "classification": "requirements"}]}, receipt={})
        plan = self.complete("plan", {"testpoints": []}, depends_on=[model["id"]], receipt={})
        return model, plan

    def simulation(self, percent=100, *, depends_on=None):
        evidence = {"status": "passed", "run_dir": str(self.root / f"run_{self.sequence}"),
                    "coverage": {"metric": "functional", "percent": percent,
                                 "hit_bins": percent, "total_bins": 100}, "checks": 10}
        return self.complete("regress", {"evidence": evidence}, depends_on=depends_on,
                             payload={"benchmark": str(self.root), "inputs": {}})

    def proposal(self, previous, *, receipt=None):
        return self.complete("analyze", {"proposal": {"campaign": "fixture"}},
                             depends_on=[previous["id"]], receipt={} if receipt is None else receipt)

    def report(self, validate=None):
        if validate is None:
            def validate(evidence, *_):
                return {"passed": True, "measured": {"coverage": evidence["coverage"]}}
        with patch("loopx.capabilities.chip_verification.engine.verification.validate_simulation_evidence", side_effect=validate):
            paths = write_report(self.project, "report")
        return json.loads(Path(paths["summary"]).read_text(encoding="utf-8"))

    def generic_build_smoke(self):
        source = self.root / "spec.json"
        inputs = {str(source): hashlib.sha256(source.read_bytes()).hexdigest()}
        evidence = {"backend": "vivado_xsim_generic", "status": "smoke_verified",
                    "inputs": [{"path": path, "sha256": value} for path, value in inputs.items()]}
        payload = {"manifest": str(self.root / "unit-only-manifest.json"), "inputs": inputs}
        build = self.complete("build", {"evidence": evidence}, payload=payload)
        smoke = self.complete("smoke", {"evidence": evidence}, payload=payload, depends_on=[build["id"]])
        return build, smoke

    def test_revalidation_refreshes_status_sessions_and_descendants_in_same_report(self):
        self.source_plan()
        smoke = self.simulation(10)
        proposal = self.proposal(smoke)
        closed = self.simulation(depends_on=[proposal["id"]])
        visited = []

        def validate(evidence, *_):
            visited.append(evidence["run_dir"])
            if evidence["run_dir"] == smoke["result"]["evidence"]["run_dir"]:
                raise ValidationError("Input campaign no longer current")
            return {"passed": True, "measured": {"coverage": evidence["coverage"]}}

        report = self.report(validate)
        self.assertEqual({"done": 2, "stale": 3}, report["status"]["tasks"])
        self.assertEqual(2, report["unique_agent_sessions"])
        self.assertEqual(0, report["accepted_simulations"])
        self.assertFalse(report["agent_stimulus_iteration"])
        self.assertFalse(report["benchmark_acceptance"])
        self.assertIsNone(report["best_run"])
        self.assertEqual([smoke["result"]["evidence"]["run_dir"]], visited)
        self.assertIn(closed["id"], report["invalidated_simulations"][0]["invalidated_tasks"])
        html = (self.root / "report" / "index.html").read_text(encoding="utf-8")
        self.assertIn("stale", html)

    def test_unrelated_agent_proposal_cannot_validate_manual_coverage(self):
        self.source_plan()
        smoke = self.simulation(10)
        self.proposal(smoke)
        self.simulation(100)
        report = self.report()
        self.assertEqual(100, report["best_coverage"]["percent"])
        self.assertFalse(report["agent_stimulus_iteration"])
        self.assertFalse(report["benchmark_acceptance"])
        self.assertIsNone(report["agent_iteration_run"])

    def test_only_coverage_from_an_agent_dependent_run_satisfies_target(self):
        self.source_plan()
        smoke = self.simulation(10)
        proposal = self.proposal(smoke)
        closed = self.simulation(95, depends_on=[proposal["id"]])
        reference = self.simulation(100)
        report = self.report()
        self.assertTrue(report["benchmark_acceptance"])
        self.assertEqual(reference["id"], report["best_run"]["task_id"])
        self.assertEqual(closed["id"], report["agent_iteration_run"])
        self.assertEqual(95, report["agent_iteration_coverage"]["percent"])
        self.assertFalse(report["best_run_agent_stimulus_iteration"])
        self.assertFalse(report["all_spec_signoff"])

    def test_low_coverage_agent_run_and_high_manual_run_cannot_be_combined(self):
        self.source_plan()
        smoke = self.simulation(10)
        proposal = self.proposal(smoke)
        self.simulation(40, depends_on=[proposal["id"]])
        self.simulation(100)
        report = self.report()
        self.assertTrue(report["agent_stimulus_iteration"])
        self.assertFalse(report["benchmark_acceptance"])
        self.assertEqual(40, report["agent_iteration_coverage"]["percent"])

    def test_failed_unaccepted_attempt_and_empty_session_are_not_counted(self):
        self.source_plan()
        self.complete("analyze", {}, receipt={"session_id": ""})
        self.complete("analyze", {}, receipt={"runtime": "transport-fixture"})
        self.complete("analyze", {}, receipt={"attempt_id": "different-attempt"})
        directory = self.root / ".loopx" / "chip-verification" / "attempts" / "unaccepted-attempt"
        directory.mkdir()
        (directory / "receipt.json").write_text('{"runtime":"codex","session_id":"abandoned"}', encoding="utf-8")
        report = self.report()
        self.assertEqual(2, report["unique_agent_sessions"])
        self.assertEqual(2, len(report["agent_receipts"]))
        self.assertEqual(3, len(report["excluded_agent_receipts"]))

    def test_session_identity_is_namespaced_by_runtime_and_deduplicated(self):
        self.source_plan()
        self.complete("analyze", {}, receipt={"runtime": "codex", "session_id": "shared"})
        self.complete("analyze", {}, receipt={"runtime": "codex", "session_id": "shared"})
        self.complete("analyze", {}, receipt={"runtime": "pi", "session_id": "shared"})
        report = self.report()
        self.assertEqual(4, report["unique_agent_sessions"])
        self.assertEqual(5, len(report["agent_receipts"]))

    def test_candidate_code_is_not_reported_as_compiled_tb(self):
        self.source_plan()
        self.complete("implement", {"candidate_only": True, "compiled": False, "simulated": False})
        report = self.report()
        self.assertEqual(1, report["generated_uvm_candidate_components"])
        self.assertIsNone(report["best_coverage"])
        self.assertFalse(report["benchmark_acceptance"])
        self.assertTrue(any("does not establish" in line for line in report["limits"]))

    def test_empty_project_does_not_report_planning_complete(self):
        report = self.report()
        self.assertFalse(report["source_to_plan_tasks_complete"])
        self.assertFalse(report["benchmark_acceptance"])

    def test_generic_build_and_smoke_are_revalidated_without_becoming_coverage(self):
        self.generic_build_smoke()
        def validate(evidence, manifest, *, phase):
            return {"accepted": True, "phase": phase, "build_passed": True,
                    "smoke_passed": phase == "smoke", "verified": phase == "smoke"}
        with patch("loopx.capabilities.chip_verification.engine.simulators.generic.validate_generic_evidence", side_effect=validate) as validator:
            report = self.report()
        self.assertEqual(["build", "smoke"], [call.kwargs["phase"] for call in validator.call_args_list])
        self.assertEqual(1, report["generic_builds"])
        self.assertEqual(1, report["generic_smokes"])
        self.assertEqual(0, report["accepted_simulations"])
        self.assertIsNone(report["best_coverage"])
        self.assertFalse(report["benchmark_acceptance"])

    def test_generic_build_rejection_refreshes_status_and_invalidates_smoke(self):
        build, smoke = self.generic_build_smoke()
        with patch("loopx.capabilities.chip_verification.engine.simulators.generic.validate_generic_evidence",
                   return_value={"accepted": False, "phase": "build", "reason": "unit fixture invalidated"}):
            report = self.report()
        self.assertEqual({"stale": 2}, report["status"]["tasks"])
        self.assertEqual(0, report["generic_builds"])
        self.assertEqual(0, report["generic_smokes"])
        self.assertEqual(build["id"], report["invalidated_simulations"][0]["task_id"])
        self.assertIn(smoke["id"], report["invalidated_simulations"][0]["invalidated_tasks"])

    def test_generic_smoke_without_verified_activity_does_not_count(self):
        self.generic_build_smoke()
        def validate(evidence, manifest, *, phase):
            return {"accepted": True, "phase": phase, "build_passed": True,
                    "smoke_passed": phase == "smoke", "verified": False}
        with patch("loopx.capabilities.chip_verification.engine.simulators.generic.validate_generic_evidence", side_effect=validate):
            report = self.report()
        self.assertEqual({"done": 1, "stale": 1}, report["status"]["tasks"])
        self.assertEqual(1, report["generic_builds"])
        self.assertEqual(0, report["generic_smokes"])

    def test_unrelated_cancelled_plan_is_not_excluded_from_completion(self):
        model, _ = self.source_plan()
        task = self.store.add_task("plan", "Cancelled work is unfinished", depends_on=[model["id"]])
        self.store.cancel(task["id"], "Cancelled by operator")
        report = self.report()
        self.assertFalse(report["source_to_plan_tasks_complete"])
        self.assertEqual([], report["superseded_plan_tasks"])


if __name__ == "__main__":
    unittest.main()
