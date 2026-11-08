"""Real durable design transitions; synthetic candidates never assert tool success."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from loopx.capabilities.chip_verification import host_work
from loopx.capabilities.chip_verification.engine import design, engineering
from loopx.capabilities.chip_verification.engine.models import ConflictError, ValidationError
from loopx.capabilities.chip_verification.engine.state import file_hash
from loopx.capabilities.chip_verification.engine.workflow import DVProject, save_json


RTL = """module counter(input logic clk, input logic rst_n, input logic en,
               output logic [3:0] count);
always_ff @(posedge clk or negedge rst_n)
  if (!rst_n) count <= 4'b0;
  else if (en) count <= count + 1'b1;
endmodule
"""


class DesignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.spec = self.root / "spec.json"
        save_json(self.spec, {"microarchitecture": "A 4-bit counter increments on enabled rising clock edges and wraps modulo 16. Active-low asynchronous reset clears it."})
        self.project = DVProject(self.root)
        self.project.import_spec(self.spec, chunk_chars=1000, context_chars=24000)
        engineering.enable(self.project)
        for task in self.project.store.list_tasks(kind="model"):
            source_ids = task["payload"]["source_ids"]
            response = {"requirements": [{"local_id": "count", "statement": "Enabled 4-bit counter wraps modulo 16; asynchronous active-low reset clears it.",
                        "source_ids": source_ids, "modules": ["counter"], "features": ["count"], "interfaces": [],
                        "conditions": ["rising clock edge"], "expected": "count increments iff enabled, reset sets zero",
                        "exceptions": [], "applicability": "in_scope"}],
                        "dispositions": [{"source_id": sid, "classification": "requirements", "reason": "Explicit counter behavior"} for sid in source_ids],
                        "unresolved": [], "cross_references": []}
            self.finish(task, response, self.project._validate_model)
        self.project.reconcile()
        self.contract_task = self.project.store.list_tasks(kind="spec-contract")[0]
        self.requirement_ids = [item["id"] for item in self.contract_task["payload"]["requirements"]]
        self.module_id = self.contract_task["payload"]["module_id"]
        provenance = {"origin": "supplied", "requirement_ids": self.requirement_ids}
        self.contract = {"entry_views": ["microarchitecture"], "top": "counter",
            "modules": [{"id": self.module_id, "name": "counter", "role": "counter IP"}],
            "ports": [{"name": name, "direction": direction, "width": width, "clock_domain": "core", "reset": "reset", **provenance}
                      for name, direction, width in (("clk", "input", 1), ("rst_n", "input", 1), ("en", "input", 1), ("count", "output", 4))],
            "parameters": [], "clocks": [{"name": "core", "port": "clk", "edge": "rising", "period_ns": None, **provenance}],
            "resets": [{"name": "reset", "port": "rst_n", "active": "low", "clock_domain": "core", "asynchronous": True, **provenance}],
            "relations": [], "requirements": [{"requirement_id": rid, "module_ids": [self.module_id], "relation_ids": []} for rid in self.requirement_ids],
            "decisions": [], "unresolved": []}
        self.finish(self.contract_task, self.contract, lambda t, r, a: engineering.validate(self.project, t, r, a))
        engineering.reconcile(self.project)
        self.micro = self.project.store.list_tasks(kind="microarchitecture")[0]

    def tearDown(self):
        self.temp.cleanup()

    def finish(self, task, response, validator):
        attempt = self.project.store.claim(task["id"], "test-session")
        path = self.project.store.state_dir / "attempts" / attempt["id"] / "response.json"
        save_json(path, response)
        self.project.store.finish(task["id"], attempt["id"], response, artifacts=[path], validator=validator)
        return path

    def micro_response(self):
        return {"contract_task_id": self.contract_task["id"], "contract_hash": engineering.digest(self.contract), "top": "counter",
            "approach": "adopt_supplied_microarchitecture", "rationale": "Implement the supplied counter directly; no upstream FS is required.",
            "blocks": [{"id": "count_register", "module_id": self.module_id, "purpose": "Retain the observable count",
                        "state_behavior": "Reset to zero, increment on enable, hold otherwise", "datapath": "Four-bit incrementer with natural wrap",
                        "error_behavior": "No errors defined", "clock_reset": "posedge clk, asynchronous negedge rst_n",
                        "requirement_ids": self.requirement_ids, "design_choices": ["Use an always_ff register"]}],
            "rtl_units": [{"path": "rtl/counter.sv", "modules": ["counter"], "purpose": "Counter implementation", "requirement_ids": self.requirement_ids}],
            "ip_bindings": [], "compile_order": ["rtl/counter.sv"], "required_checks": list(design.REQUIRED_CHECKS), "unresolved": []}

    def finish_micro(self, response=None):
        self.finish(self.micro, response or self.micro_response(), lambda t, r, a: design.validate(self.project, t, r, a))
        design.reconcile(self.project)
        return self.project.store.list_tasks(kind="rtl")[0]

    def rtl_response(self, files=None):
        return {"contract_task_id": self.contract_task["id"], "contract_hash": engineering.digest(self.contract), "top": "counter",
                "files": files if files is not None else [{"path": "rtl/counter.sv", "content": RTL}], "notes": [], "unresolved": []}

    def finish_rtl(self, task, response=None):
        attempt = self.project.store.claim(task["id"], "second-session")
        directory = self.project.store.state_dir / "attempts" / attempt["id"]
        raw = response or self.rtl_response()
        save_json(directory / "response.json", raw)
        result, artifacts = design.accept(self.project, task, raw, attempt, directory)
        self.project.store.finish(task["id"], attempt["id"], result,
            artifacts=[directory / "response.json", *artifacts], validator=lambda t, r, a: design.validate(self.project, t, r, a))
        return design.get_candidate(self.project, task["id"])

    def test_microarchitecture_entry_produces_real_candidate_and_survives_process_restart(self):
        rtl_task = self.finish_micro()
        candidate = self.finish_rtl(rtl_task)
        self.assertEqual(RTL, Path(candidate["rtl_files"][0]).read_text(encoding="utf-8"))
        self.assertEqual(self.contract_task["id"], candidate["contract_task_id"])
        self.assertFalse(candidate["compiled"])
        self.assertFalse(candidate["simulated"])
        self.assertFalse(candidate["coverage_verified"])
        code = "import json,sys; from loopx.capabilities.chip_verification.engine.workflow import DVProject; from loopx.capabilities.chip_verification.engine.design import get_candidate; print(json.dumps(get_candidate(DVProject(sys.argv[1], initialize=False),sys.argv[2])))"
        completed = subprocess.run([sys.executable, "-c", code, str(self.root), rtl_task["id"]],
                                   text=True, capture_output=True, timeout=60, check=False)
        self.assertEqual(0, completed.returncode,
                         "Restart readback failed.\nstdout:\n" + completed.stdout + "\nstderr:\n" + completed.stderr)
        resumed = json.loads(completed.stdout)
        self.assertEqual(candidate, resumed)
        self.assertEqual(1, design.reconcile(DVProject(self.root)))
        self.assertEqual(1, len(self.project.store.list_tasks(kind="rtl")))

    def test_design_and_plan_share_exact_contract_dependencies(self):
        plan = self.project.store.list_tasks(kind="contract-plan")[0]
        self.assertEqual(plan["payload"]["contract_hash"], self.micro["payload"]["contract_hash"])
        self.assertEqual(plan["depends_on"], self.micro["depends_on"])
        _, _, context = design.task_context(self.project, self.micro)
        self.assertEqual(["microarchitecture"], context["shared_contract"]["entry_views"])
        self.assertEqual(self.requirement_ids, [item["id"] for item in context["requirements"]])

    def test_wrong_contract_and_missing_requirements_are_rejected(self):
        for mutate in (lambda value: value.update(contract_hash="0" * 64),
                       lambda value: value["blocks"][0].update(requirement_ids=["invented"]),
                       lambda value: value.update(required_checks=["elaboration"])):
            response = self.micro_response()
            mutate(response)
            with self.assertRaises((ValidationError, ConflictError)):
                design.validate(self.project, self.micro, response, [])

    def test_owned_paths_and_module_definitions_cannot_be_changed(self):
        rtl_task = self.finish_micro()
        for files in ([{"path": "../escape.sv", "content": RTL}],
                      [{"path": "rtl/counter.sv", "content": RTL.replace("module counter", "module replacement")}],
                      [{"path": "rtl/counter.sv", "content": "// no implementation"}]):
            with self.assertRaises(ValidationError):
                design.validate(self.project, rtl_task, self.rtl_response(files), [])

    def test_rejected_rtl_can_be_corrected_in_the_same_live_host_attempt(self):
        task = self.finish_micro()
        host_work.initialize(self.project)
        ticket = host_work.claim(self.project, {"task_id": task["id"], "session_id": "correcting-session"})
        incomplete = self.rtl_response([{"path": "rtl/counter.sv", "content": RTL.replace("endmodule", "")}])
        with self.assertRaisesRegex(ValidationError, "no endmodule"):
            host_work.submit(self.project, ticket["attempt_id"], incomplete)
        self.assertEqual("running", self.project.store.get_task(task["id"])["status"])
        rejected_files = list((self.project.store.state_dir / "attempts" / ticket["attempt_id"]).glob("rtl-candidates/*/rtl/rtl/counter.sv"))
        self.assertEqual(1, len(rejected_files))
        rejected_bytes = rejected_files[0].read_bytes()
        response = self.rtl_response()
        receipt = host_work.submit(self.project, ticket["attempt_id"], response)
        self.assertEqual("accepted", receipt["status"])
        candidate = design.get_candidate(self.project, task["id"])
        accepted = Path(candidate["rtl_files"][0])
        self.assertNotEqual(rejected_files[0], accepted)
        self.assertEqual(RTL.encode("utf-8"), accepted.read_bytes())
        self.assertEqual(rejected_bytes, rejected_files[0].read_bytes())
        self.assertTrue(host_work.submit(self.project, ticket["attempt_id"], response)["receipt"]["replayed"])

    def test_blocking_microarchitecture_question_prevents_rtl_creation(self):
        response = self.micro_response()
        response["unresolved"] = [{"question": "The source contains contradictory reset behavior.", "blocking": True, "requirement_ids": self.requirement_ids}]
        self.finish(self.micro, response, lambda t, r, a: design.validate(self.project, t, r, a))
        self.assertEqual(0, design.reconcile(self.project))
        self.assertEqual([], self.project.store.list_tasks(kind="rtl"))
        self.assertTrue(any(issue["category"] == "design_blocker" for issue in self.project.store.list_issues()))

    def test_ip_reuse_is_pinned_and_published_without_rewriting_it(self):
        source = self.root / "ip" / "counter.sv"
        source.parent.mkdir()
        source.write_text(RTL, encoding="utf-8")
        response = self.micro_response()
        response["rtl_units"] = []
        response["ip_bindings"] = [{"path": "ip/counter.sv", "sha256": file_hash(source), "modules": ["counter"],
                                     "requirement_ids": self.requirement_ids, "rationale": "Reuse the supplied counter IP"}]
        response["compile_order"] = ["ip/counter.sv"]
        task = self.finish_micro(response)
        candidate = self.finish_rtl(task, self.rtl_response([]))
        self.assertEqual("ip", candidate["files"][0]["origin"])
        self.assertEqual(source.read_bytes(), Path(candidate["rtl_files"][0]).read_bytes())
        source.write_text(RTL + "// changed\n", encoding="utf-8")
        with self.assertRaises(ConflictError):
            design.get_candidate(self.project, task["id"])

    def test_changed_contract_and_candidate_bytes_fail_readback(self):
        task = self.finish_micro()
        candidate = self.finish_rtl(task)
        path = Path(candidate["rtl_files"][0])
        path.write_text(RTL.replace("+ 1'b1", "+ 2'b10"), encoding="utf-8")
        with self.assertRaises(ConflictError):
            design.get_candidate(self.project, task["id"])
        self.project.store.verify_artifacts()
        self.assertEqual("stale", self.project.store.get_task(task["id"])["status"])

    def test_changed_source_and_stale_microarchitecture_cannot_accept_rtl(self):
        task = self.finish_micro()
        self.spec.write_text('{"description":"changed contract"}', encoding="utf-8")
        with self.assertRaises(ConflictError):
            design.validate(self.project, task, self.rtl_response(), [])

    def test_case_aliased_files_and_duplicate_module_ownership_are_rejected(self):
        response = self.micro_response()
        second = copy.deepcopy(response["rtl_units"][0])
        second["path"] = "rtl/COUNTER.sv"
        response["rtl_units"].append(second)
        response["compile_order"].append(second["path"])
        with self.assertRaises(ValidationError):
            design.validate(self.project, self.micro, response, [])

    def test_revise_rtl_preserves_contract_and_history_but_invalidates_tool_evidence(self):
        task = self.finish_micro()
        old_candidate = self.finish_rtl(task)
        old_path = Path(old_candidate["rtl_files"][0])
        old_bytes = old_path.read_bytes()
        check = self.project.store.add_task("rtl-check", "Dependent check identity fixture", {"rtl_task_id": task["id"]},
                                            depends_on=[task["id"]])
        self.finish(check, {"fixture_only": True}, lambda *args: {"passed": True, "checks": ["fixture_not_tool_evidence"]})
        host_work.initialize(self.project)
        reason = "Lint rejected an implementation detail; preserve the source behavior and fix the RTL."
        result = host_work.revise(self.project, task["id"], reason)
        self.assertEqual({task["id"], check["id"]}, set(result["invalidated_tasks"]))
        self.assertFalse(result["specification_changed"])
        self.assertEqual(task["payload"], self.project.store.get_task(task["id"])["payload"])
        self.assertEqual("done", self.project.store.get_task(self.contract_task["id"])["status"])
        self.assertEqual("done", self.project.store.get_task(self.micro["id"])["status"])
        self.assertEqual("stale", self.project.store.get_task(check["id"])["status"])
        restarted = DVProject(self.root)
        ticket = host_work.claim(restarted, {"task_id": task["id"], "session_id": "revision-session"})
        context = host_work.context(restarted, ticket["attempt_id"])
        self.assertIn(reason, context["prompt"])
        self.assertIn(old_candidate["contract_hash"], context["prompt"])
        self.assertIsNone(context["context_budget_chars"])
        self.assertGreater(context["context_chars"], 0)
        response = self.rtl_response([{"path": "rtl/counter.sv", "content": RTL + "// Revised candidate; no tool pass asserted.\n"}])
        host_work.submit(restarted, ticket["attempt_id"], response)
        revised = design.get_candidate(restarted, task["id"])
        self.assertEqual(old_candidate["contract_hash"], revised["contract_hash"])
        self.assertNotEqual(old_candidate["files"][0]["sha256"], revised["files"][0]["sha256"])
        self.assertNotEqual(old_path, Path(revised["rtl_files"][0]))
        self.assertEqual(old_bytes, old_path.read_bytes())
        self.assertEqual("stale", restarted.store.get_task(check["id"])["status"])

    def test_revision_rejects_live_work_unsupported_stage_and_invalid_feedback(self):
        task = self.finish_micro()
        with self.assertRaisesRegex(ValidationError, "accepted RTL"):
            host_work.revise(self.project, task["id"], "Still pending")
        with self.assertRaisesRegex(ValidationError, "accepted RTL"):
            host_work.revise(self.project, self.micro["id"], "Do not revise a shared upstream design here")
        self.finish_rtl(task)
        before = self.project.store.get_task(task["id"])
        for reason in ("", "   ", "x" * 4001, None):
            with self.subTest(reason_length=len(reason) if isinstance(reason, str) else None):
                with self.assertRaisesRegex(ValidationError, "feedback"):
                    host_work.revise(self.project, task["id"], reason)
                self.assertEqual(before, self.project.store.get_task(task["id"]))

    def test_public_tool_retry_routes_to_owner_without_requeue_or_execution(self):
        from loopx.capabilities.chip_verification.provider import invoke

        repository = Path(__file__).resolve().parents[3]
        bundled = repository / ".local/dv-tools/node-v22.22.3-win-x64"
        path = os.environ.get("PATH", "")
        if (bundled / "node.exe").is_file():
            path = str(bundled) + os.pathsep + path
        elif not shutil.which("node"):
            self.skipTest("Public admission test needs Node")
        with patch.dict(os.environ, {"PATH": path, "LOOPX_USAGE_PING": "0"}):
            for kind, payload, action in (("rtl-check", {"rtl_task_id": "fixture-candidate"}, "check-design"),
                                          ("build", {"manifest": "fixture-build.json"}, "build-uvm"),
                                          ("smoke", {"manifest": "fixture-build.json"}, "build-uvm")):
                with self.subTest(kind=kind):
                    task = self.project.store.add_task(kind, "Retry routing fixture " + kind, payload)
                    self.project.store.block(task["id"], "Tool fixture unavailable")
                    before = self.project.store.get_task(task["id"])
                    with patch("loopx.capabilities.chip_verification.jobs.launch") as launch:
                        result = invoke("retry", workspace=self.root, arguments={"task_id": task["id"]}, execute=True)
                    launch.assert_not_called()
                    self.assertEqual("tool_retry_required", result["status"])
                    self.assertEqual(action, result["next_action"])
                    self.assertEqual(payload, result["arguments"])
                    self.assertEqual(before, self.project.store.get_task(task["id"]))


if __name__ == "__main__":
    unittest.main()
