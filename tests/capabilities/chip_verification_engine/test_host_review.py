"""Native-host failure/recovery probes; no model or EDA evidence is fabricated."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from loopx.capabilities.chip_verification import host_work
from loopx.capabilities.chip_verification.engine.models import ConflictError
from loopx.capabilities.chip_verification.engine.workflow import DVProject


class HostRecoveryReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.spec = self.root / "spec.json"
        self.project = DVProject(self.root)
        self.import_revision("The FIFO shall preserve accepted word order.")
        host_work.initialize(self.project)
        self.rtl = self.root / "dut.sv"
        self.rtl.write_text("module dut(input logic clk, rst_n); endmodule\n", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def import_revision(self, statement):
        self.spec.write_text(json.dumps({"behavior": statement}), encoding="utf-8")
        self.project.import_spec(self.spec, chunk_chars=1000, context_chars=16000)

    def claim_model(self):
        claim = host_work.claim(self.project, {"kind": "model", "session_id": "worker-execution-label"})
        task = self.project.store.get_task(claim["task_id"])
        response = {
            "requirements": [{"local_id": "order", "statement": "Accepted FIFO words retain order",
                "source_ids": task["payload"]["source_ids"], "modules": ["fifo"], "features": ["ordering"],
                "interfaces": ["stream"], "conditions": ["outside reset"], "expected": "Accepted words retain order",
                "exceptions": [], "applicability": "in_scope"}],
            "dispositions": [{"source_id": source, "classification": "requirements", "reason": "Explicit FIFO behavior"}
                for source in task["payload"]["source_ids"]],
            "unresolved": [], "cross_references": [],
        }
        return claim, response

    def accept_plan(self):
        claim, response = self.claim_model()
        host_work.submit(self.project, claim["attempt_id"], response)
        plan = host_work.claim(self.project, {"kind": "plan", "session_id": "independent-planning-worker"})
        task = self.project.store.get_task(plan["task_id"])
        response = {"testpoints": [{"id": "TP-order", "name": "Observe FIFO ordering",
            "requirement_ids": [requirement["id"] for requirement in task["payload"]["requirements"]],
            "level": "ip", "stimulus": "Enqueue distinct words", "observations": ["monitor accepted words"],
            "checkers": ["compare against reference queue"], "coverage": ["observed occupancy boundaries"],
            "uvm_components": ["fifo_agent", "fifo_scoreboard"], "configurations": ["depth=4"], "priority": "critical"}],
            "unresolved": []}
        host_work.submit(self.project, plan["attempt_id"], response)

    def generation_request(self):
        return {"rtl_files": [str(self.rtl)], "top": "dut", "session_id": "architecture-worker"}

    def architecture_response(self):
        plan = self.project.store.list_tasks(status="done", kind="plan")[0]
        point = plan["id"] + ":" + plan["result"]["testpoints"][0]["id"]
        return {"top": "dut", "uvm_version": "1.2", "interfaces": [], "shared_contracts": [],
            "components": [{"id": identifier, "kind": "env", "purpose": "Independent fixture contract",
                "responsibilities": ["interface", "monitor", "agent", "coverage", "env", "test", "tb_top", "scoreboard"],
                "depends_on": [], "dependency_files": [], "testpoint_ids": [point],
                "output_paths": [identifier + ".sv"], "contract": "Unit fixture only; never simulated"}
                for identifier in ("first", "second")], "omitted_optional": [], "unresolved": []}

    def component_response(self, task_id):
        component = self.project.store.get_task(task_id)["payload"]["component"]
        return {"files": [{"path": name, "content": "package fixture_" + component["id"] + "; endpackage\n"}
                          for name in component["output_paths"]], "notes": ["Structural fixture only"], "unresolved": []}

    def test_accepted_submission_recovers_after_ticket_receipt_crash(self):
        claim, response = self.claim_model()
        with patch.object(host_work, "_receipt", side_effect=OSError("simulated crash after state commit")):
            with self.assertRaises(OSError):
                host_work.submit(self.project, claim["attempt_id"], response)
        self.assertEqual("done", self.project.store.get_task(claim["task_id"])["status"])
        replay = host_work.submit(DVProject(self.root), claim["attempt_id"], response)
        self.assertEqual("accepted", replay["status"])
        self.assertTrue(replay["receipt"]["replayed"])
        with self.assertRaises(ConflictError):
            host_work.submit(self.project, claim["attempt_id"], {**response, "unresolved": [{"different": True}]})

    def test_same_generation_request_is_available_after_new_spec_baseline(self):
        self.accept_plan()
        request = self.generation_request()
        first = host_work.generate(self.project, request)
        self.assertEqual("awaiting_host", first["status"])
        self.import_revision("The FIFO shall preserve accepted word order across output stalls.")
        self.accept_plan()
        second = host_work.generate(self.project, request)
        self.assertEqual("awaiting_host", second["status"])
        with self.project.store._db() as db:
            available = db.execute("SELECT COUNT(*) FROM host_generations WHERE baseline_id=?",
                                   (self.project.store.baseline()["id"],)).fetchone()[0]
        self.assertGreater(available, 0)

    def test_explicit_generation_task_selection_cannot_claim_another_top(self):
        self.accept_plan()
        first = host_work.generate(self.project, self.generation_request())
        other_rtl = self.root / "second.sv"
        other_rtl.write_text("module second(input logic clk); endmodule\n", encoding="utf-8")
        second = host_work.generate(self.project, {"rtl_files": [str(other_rtl)], "top": "second", "session_id": "second-worker"})
        for claim in (first, second):
            self.project.store.fail(claim["task_id"], claim["attempt_id"], "worker interrupted", retryable=True)
        selected = host_work.claim(self.project, {"task_id": first["task_id"], "session_id": "replacement-worker"})
        self.assertEqual(first["task_id"], selected["task_id"])
        self.assertEqual("pending", self.project.store.get_task(second["task_id"])["status"])

    def test_expired_worker_cannot_submit_after_replacement_claim(self):
        claim, response = self.claim_model()
        with self.project.store._db(write=True) as db:
            db.execute("UPDATE attempts SET lease_until=0 WHERE id=?", (claim["attempt_id"],))
        replacement = host_work.claim(self.project, {"kind": "model", "session_id": "replacement-worker"})
        self.assertNotEqual(claim["attempt_id"], replacement["attempt_id"])
        with self.assertRaises(ConflictError):
            host_work.submit(self.project, claim["attempt_id"], response)
        self.assertEqual(replacement["attempt_id"], self.project.store.get_task(claim["task_id"])["attempt_id"])

    def test_independent_component_submission_does_not_execute_an_earlier_requeued_component(self):
        self.accept_plan()
        architecture = host_work.generate(self.project, self.generation_request())
        host_work.submit(self.project, architecture["attempt_id"], self.architecture_response())
        first = host_work.claim(self.project, {"kind": "implement", "session_id": "first-worker"})
        second = host_work.claim(self.project, {"kind": "implement", "session_id": "second-worker"})
        self.project.store.fail(first["task_id"], first["attempt_id"], "worker interrupted", retryable=True)
        accepted = host_work.submit(self.project, second["attempt_id"], self.component_response(second["task_id"]))
        self.assertEqual("accepted", accepted["status"])
        self.assertEqual("pending", self.project.store.get_task(first["task_id"])["status"])

    def test_component_files_written_before_commit_can_be_replayed_after_crash(self):
        self.accept_plan()
        architecture = host_work.generate(self.project, self.generation_request())
        host_work.submit(self.project, architecture["attempt_id"], self.architecture_response())
        claimed = host_work.claim(self.project, {"kind": "implement", "session_id": "component-worker"})
        response = self.component_response(claimed["task_id"])
        with patch.object(self.project.store, "finish", side_effect=OSError("simulated crash before state commit")):
            with self.assertRaises(OSError):
                host_work.submit(self.project, claimed["attempt_id"], response)
        accepted = host_work.submit(self.project, claimed["attempt_id"], response)
        self.assertEqual("accepted", accepted["status"])

    def test_architecture_receipt_crash_recovers_dependent_component_graph(self):
        self.accept_plan()
        architecture = host_work.generate(self.project, self.generation_request())
        response = self.architecture_response()
        with patch.object(host_work, "_receipt", side_effect=OSError("simulated crash after architecture commit")):
            with self.assertRaises(OSError):
                host_work.submit(self.project, architecture["attempt_id"], response)
        host_work.submit(DVProject(self.root), architecture["attempt_id"], response)
        claimed = host_work.claim(self.project, {"kind": "implement", "session_id": "component-worker"})
        self.assertEqual("awaiting_host", claimed["status"])

    def test_changed_baseline_fences_prior_worker_context_and_submission(self):
        claim, response = self.claim_model()
        self.import_revision("Reset shall empty the FIFO.")
        with self.assertRaises(ConflictError):
            host_work.context(self.project, claim["attempt_id"])
        with self.assertRaises(ConflictError):
            host_work.submit(self.project, claim["attempt_id"], response)
        self.assertEqual([], self.project.store.list_requirements())

    def test_native_host_protocol_completes_candidate_graph_without_simulation_claim(self):
        self.accept_plan()
        architecture = host_work.generate(self.project, self.generation_request())
        architecture_context = host_work.context(DVProject(self.root, initialize=False), architecture["attempt_id"])
        self.assertEqual("architecture", architecture_context["kind"])
        self.assertIsNone(architecture_context["context_budget_chars"])
        self.assertIn("accepted_testpoint_catalog", architecture_context["prompt"])
        accepted = host_work.submit(DVProject(self.root), architecture["attempt_id"], self.architecture_response())
        self.assertEqual("accepted", accepted["status"])
        seen = set()
        for number in range(2):
            project = DVProject(self.root)
            claim = host_work.claim(project, {"kind": "implement", "session_id": "fresh-worker-" + str(number)})
            self.assertEqual("awaiting_host", claim["status"])
            self.assertNotIn(claim["attempt_id"], seen)
            seen.add(claim["attempt_id"])
            bounded = host_work.context(DVProject(self.root, initialize=False), claim["attempt_id"])
            self.assertEqual("implement", bounded["kind"])
            self.assertIsNone(bounded["context_budget_chars"])
            component_context = json.loads(bounded["prompt"].splitlines()[-1])
            self.assertIn("required_dependency_files", component_context)
            self.assertTrue(component_context["requirements"])
            accepted = host_work.submit(DVProject(self.root), claim["attempt_id"], self.component_response(claim["task_id"]))
            self.assertEqual("accepted", accepted["status"])
        manifest = accepted["generation"]
        self.assertEqual("candidate_generated", manifest["status"])
        self.assertFalse(manifest["compiled"])
        self.assertFalse(manifest["simulated"])
        self.assertFalse(manifest["coverage_verified"])
        self.assertFalse(manifest["cross_scope_integration_verified"])
        persisted = json.loads(Path(manifest["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual("candidate_generated", persisted["status"])
        self.assertEqual([], self.project.store.list_runs())
        self.assertEqual("no_ready_work", host_work.claim(DVProject(self.root), {"kind": "implement", "session_id": "final-readback"})["status"])


if __name__ == "__main__":
    unittest.main()
