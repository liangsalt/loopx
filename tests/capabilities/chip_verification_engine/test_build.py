"""State-orchestration fixtures only; these are never simulator evidence."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from loopx.capabilities.chip_verification.engine.build import run_build
from loopx.capabilities.chip_verification.engine.workflow import DVProject


class BuildLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        spec = self.root / "spec.json"
        spec.write_text('{"requirement":"Check observed FIFO order"}', encoding="utf-8")
        self.project = DVProject(self.root)
        self.project.import_spec(spec, name="Lifecycle fixture", scope="FIFO")
        self.source = self.root / "fixture.sv"
        self.source.write_text("module fixture; endmodule", encoding="utf-8")
        self.manifest = self.root / "build.json"
        self.manifest.write_text('{"fixture_only":true}', encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, status="built", smoke=False, mutate=False):
        source, manifest = self.source, self.manifest
        inputs = [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                  for path in [manifest, source]]

        class BackendFixture:
            def __init__(self, *_args):
                pass

            def plan(self, _path):
                return {"top": "fixture", "compile_order": [str(source)]}

            def snapshot_inputs(self, _path):
                return inputs

            def run(self, _manifest, directory, **_kwargs):
                assert not any(directory.iterdir()), "Backend needs a new empty directory"
                evidence = {"status": status, "inputs": inputs, "commands": [], "artifacts": [],
                            "unit_test_fixture": True}
                (directory / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
                if mutate:
                    source.write_text("module changed; endmodule", encoding="utf-8")
                return evidence

        def validate(evidence, _manifest, phase):
            accepted = status != "compile_failed"
            return {"accepted": accepted, "verified": status == "smoke_verified",
                    "reason": "fixture rejected compilation" if not accepted else "",
                    "phase": phase, "unit_test_fixture": True}

        with patch("loopx.capabilities.chip_verification.engine.simulators.generic.GenericXSimBackend", BackendFixture), \
             patch("loopx.capabilities.chip_verification.engine.simulators.generic.validate_generic_evidence", validate):
            return run_build(self.project, self.manifest, run_smoke=smoke)

    def test_build_acceptance_is_not_coverage(self):
        result = self.execute()
        self.assertEqual("built", result["status"])
        self.assertEqual({"status": "not_evaluated"}, result["coverage"])
        self.assertEqual("done", self.project.store.get_task(result["build_task_id"])["status"])
        self.assertEqual("passed", self.project.store.get_run(result["run_id"])["status"])
        self.assertEqual([], self.project.store.list_tasks(kind="smoke"))

    def test_no_activity_preserves_build_but_rejects_smoke(self):
        result = self.execute("smoke_completed", smoke=True)
        self.assertEqual("failed", result["status"])
        self.assertEqual("done", self.project.store.get_task(result["build_task_id"])["status"])
        self.assertEqual("failed", self.project.store.get_task(result["smoke_task_id"])["status"])

    def test_tool_capability_has_persistent_blocker_and_retains_build(self):
        result = self.execute("smoke_unqualified", smoke=True)
        self.assertEqual("blocked", result["status"])
        self.assertEqual("done", self.project.store.get_task(result["build_task_id"])["status"])
        self.assertEqual("blocked", self.project.store.get_task(result["smoke_task_id"])["status"])
        self.assertEqual("tool_capability", self.project.store.list_issues()[0]["category"])
        self.assertEqual("failed", self.project.store.get_run(result["run_id"])["status"])

    def test_observed_smoke_has_separate_dependent_acceptance(self):
        result = self.execute("smoke_verified", smoke=True)
        self.assertEqual("smoke_verified", result["status"])
        smoke = self.project.store.get_task(result["smoke_task_id"])
        self.assertEqual("done", smoke["status"])
        self.assertEqual([result["build_task_id"]], smoke["depends_on"])
        self.assertFalse(smoke["result"]["coverage_verified"])

    def test_compile_failure_never_creates_smoke(self):
        result = self.execute("compile_failed", smoke=True)
        self.assertEqual("failed", result["status"])
        self.assertEqual("failed", self.project.store.get_task(result["build_task_id"])["status"])
        self.assertEqual([], self.project.store.list_tasks(kind="smoke"))

    def test_changed_input_cannot_accept_build(self):
        result = self.execute(mutate=True)
        self.assertEqual("failed", result["status"])
        self.assertIn("changed", result["error"])
        self.assertEqual([], self.project.store.list_tasks(kind="build", status="done"))


if __name__ == "__main__":
    unittest.main()
