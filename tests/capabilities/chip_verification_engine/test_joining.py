"""Real design/DV ledger joins; mocked check qualification is not EDA evidence."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_design as design_fixture

from loopx.capabilities.chip_verification.engine import design, engineering, joining
from loopx.capabilities.chip_verification.engine.models import ConflictError, ValidationError
from loopx.capabilities.chip_verification.engine.state import file_hash
from loopx.capabilities.chip_verification.engine.workflow import DVProject, save_json


class JoiningTests(unittest.TestCase):
    def setUp(self):
        # Compose the fixture instead of inheriting its collected test methods.
        self.fixture = design_fixture.DesignTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.project = self.fixture.project
        self.root = self.fixture.root
        self.candidate = None
        self.check = None
        self.tb = self.root / "tb" / "tb_top.sv"
        self.tb.parent.mkdir()
        self.tb.write_text("// Join ownership fixture only; no simulated UVM claim.\n"
                           "module tb_top; logic clk, rst_n, en; logic [3:0] count;\n"
                           "counter dut(.clk(clk), .rst_n(rst_n), .en(en), .count(count)); endmodule\n", encoding="utf-8")
        self.template = self.root / "build-template.json"
        save_json(self.template, {"schema_version": 1, "candidate_dir": "tb", "top": "tb_top",
                                  "rtl_files": [], "tb_files": ["tb_top.sv"],
                                  "compile_order": ["tb_top.sv"], "include_dirs": [], "defines": []})

    def prepare(self, *, content=design_fixture.RTL):
        fixture = self.fixture
        fixture.finish(fixture.micro, fixture.micro_response(), lambda t, r, a: design.validate(self.project, t, r, a))
        design.reconcile(self.project)
        rtl = next(task for task in self.project.store.list_tasks(kind="rtl")
                   if task["payload"]["microarchitecture_task_id"] == fixture.micro["id"])
        self.candidate = fixture.finish_rtl(rtl, fixture.rtl_response([{"path": "rtl/counter.sv", "content": content}]))
        self.tb_task = self.project.store.add_task("implement", "Accepted TB ownership fixture",
            {key: self.candidate[key] for key in ("contract_task_id", "contract_hash")},
            depends_on=[self.candidate["contract_task_id"]])
        attempt = self.project.store.claim(self.tb_task["id"], "tb-fixture-session")
        result = {"files": [{"path": "tb_top.sv", "artifact_path": self.tb.relative_to(self.root).as_posix(),
                              "sha256": file_hash(self.tb)}]}
        self.project.store.finish(self.tb_task["id"], attempt["id"], result, artifacts=[self.tb],
                                  validator=lambda *args: {"passed": True, "checks": ["test_fixture_ownership_only"]})
        check_task = self.project.store.add_task("rtl-check", "Check identity fixture without tool evidence",
            {"rtl_task_id": rtl["id"]}, depends_on=[rtl["id"], self.candidate["contract_task_id"]])
        fixture.finish(check_task, {"fixture_only": True},
                       lambda *args: {"passed": True, "checks": ["test_fixture_not_tool_qualification"]})
        self.check = {"task_id": check_task["id"], "qualified": True, "fixture_only": True}
        return self.candidate

    def joined(self):
        if self.candidate is None:
            self.prepare()
        with patch("loopx.capabilities.chip_verification.engine.design_checks.current_check", return_value=self.check):
            return joining.join(self.project, self.candidate["rtl_task_id"], self.template)

    def read_join(self, path, *, check=None, project=None):
        with patch("loopx.capabilities.chip_verification.engine.design_checks.current_check", return_value=check or self.check):
            return joining.validate_join(project or self.project, path, required=True)

    def changed_manifest(self, path, mutate):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        mutate(data)
        changed = self.root / "changed-manifest.json"
        save_json(changed, data)
        return changed

    def test_join_binds_exact_shared_revision_and_current_task_dependencies(self):
        result = self.joined()
        self.assertEqual("joined", result["status"])
        self.assertFalse(result["compiled"])
        self.assertFalse(result["simulated"])
        self.assertFalse(result["coverage_verified"])
        binding = result["binding"]
        self.assertEqual(self.candidate["contract_hash"], binding["contract_hash"])
        self.assertEqual([self.tb_task["id"]], binding["tb_task_ids"])
        dependencies = self.read_join(result["manifest"], project=DVProject(self.root, initialize=False))
        self.assertEqual({self.candidate["contract_task_id"], self.candidate["rtl_task_id"],
                          self.check["task_id"], self.tb_task["id"]}, set(dependencies))
        self.assertEqual(result, self.joined())

    def test_wrong_contract_hash_is_rejected_at_readback(self):
        result = self.joined()
        wrong = self.changed_manifest(result["manifest"], lambda data: data["engineering_binding"].update(contract_hash="0" * 64))
        with self.assertRaisesRegex(ConflictError, "different shared"):
            self.read_join(wrong)

    def test_tb_artifact_drift_is_rejected_without_mutating_read_only_state(self):
        result = self.joined()
        before = self.project.store.get_task(self.tb_task["id"])
        self.tb.write_text(self.tb.read_text(encoding="utf-8") + "// modified\n", encoding="utf-8")
        with self.assertRaises(ConflictError):
            self.read_join(result["manifest"], project=DVProject(self.root, initialize=False))
        self.assertEqual(before, self.project.store.get_task(self.tb_task["id"]))

    def test_changed_rtl_bytes_and_substituted_rtl_inventory_are_rejected(self):
        result = self.joined()
        copied = self.root / "unaccepted-counter.sv"
        copied.write_text(design_fixture.RTL, encoding="utf-8")
        def replace(data):
            old = data["rtl_files"][0]
            data["rtl_files"] = [str(copied)]
            data["compile_order"] = [str(copied) if path == old else path for path in data["compile_order"]]
        changed = self.changed_manifest(result["manifest"], replace)
        with self.assertRaisesRegex(ConflictError, "RTL inventory"):
            self.read_join(changed)
        Path(self.candidate["rtl_files"][0]).write_text(design_fixture.RTL + "// changed\n", encoding="utf-8")
        with self.assertRaises(ConflictError):
            self.read_join(result["manifest"])

    def test_design_preprocessor_configuration_cannot_change_at_join_or_readback(self):
        result = self.joined()
        changed = self.changed_manifest(result["manifest"], lambda data: data.update(defines=["DISABLE_CHECKED_LOGIC"]))
        with self.assertRaisesRegex(ValidationError, "preprocessor"):
            self.read_join(changed)
        template = json.loads(self.template.read_text(encoding="utf-8"))
        template["defines"] = ["DISABLE_CHECKED_LOGIC"]
        save_json(self.template, template)
        with self.assertRaisesRegex(ValidationError, "preprocessor"):
            self.joined()

    def test_parameterized_contract_is_blocked_until_instance_binding_is_supported(self):
        fixture = self.fixture
        identifier = fixture.contract_task["id"]
        self.project.store.invalidate(identifier, "Test a new declared parameter configuration")
        fixture.contract_task = self.project.store.requeue(identifier)
        fixture.contract["parameters"] = [{"name": "WIDTH", "value": "4", "origin": "design_choice",
                                            "requirement_ids": fixture.requirement_ids}]
        fixture.finish(fixture.contract_task, fixture.contract, lambda t, r, a: engineering.validate(self.project, t, r, a))
        engineering.reconcile(self.project)
        fixture.micro = self.project.store.list_tasks(status="pending", kind="microarchitecture")[0]
        self.prepare(content=design_fixture.RTL.replace("module counter(", "module counter #(parameter WIDTH = 4)("))
        with self.assertRaisesRegex(ValidationError, "instance-configuration"):
            self.joined()

    def test_partial_module_cannot_enter_whole_module_simulation(self):
        fixture = self.fixture
        partial = copy.deepcopy(fixture.micro["payload"])
        partial.update(partial_module=True, composition_required=True, scope_index=0, scope_count=2)
        fixture.micro = self.project.store.add_task("microarchitecture", "Partial module fixture",
                                                   partial, depends_on=fixture.micro["depends_on"])
        self.prepare()
        result = self.joined()
        self.assertEqual("blocked", result["status"])
        self.assertTrue(result["composition_required"])

    def test_missing_and_replaced_check_receipts_do_not_qualify_join(self):
        self.prepare()
        with patch("loopx.capabilities.chip_verification.engine.design_checks.current_check",
                   return_value={"qualified": False, "reason": "CDC not available"}):
            self.assertEqual("blocked", joining.join(self.project, self.candidate["rtl_task_id"], self.template)["status"])
        result = self.joined()
        for check in ({"qualified": False, "task_id": self.check["task_id"]},
                      {"qualified": True, "task_id": "another-check"}):
            with self.subTest(check=check), self.assertRaisesRegex(ValidationError, "Current elaboration"):
                self.read_join(result["manifest"], check=check)

    def test_unowned_header_cannot_change_tb_behavior(self):
        self.prepare()
        header = self.tb.parent / "unaccepted.svh"
        header.write_text("`define ALTER_OBSERVATION 1\n", encoding="utf-8")
        template = json.loads(self.template.read_text(encoding="utf-8"))
        template["include_dirs"] = ["."]
        save_json(self.template, template)
        with self.assertRaisesRegex(ValidationError, "Every TB source"):
            self.joined()


if __name__ == "__main__":
    unittest.main()
