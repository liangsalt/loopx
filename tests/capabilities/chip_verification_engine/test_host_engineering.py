"""Native host recovery across simultaneous shared-contract UVM scopes."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import test_generation as uvm_fixture

from loopx.capabilities.chip_verification import host_work
from loopx.capabilities.chip_verification.engine import engineering
from loopx.capabilities.chip_verification.engine.workflow import DVProject, save_json


class SharedHostRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = DVProject(self.root)
        spec = self.root / "spec.json"
        save_json(spec, {name: name + " is a four-bit enabled counter with active-low asynchronous reset."
                         for name in ("counter_a", "counter_b")})
        self.project.import_spec(spec, chunk_chars=1000, context_chars=24000)
        engineering.enable(self.project)
        host_work.initialize(self.project)
        for task in self.project.store.list_tasks(kind="model"):
            sources = {source["id"]: source for source in self.project.store.list_sources(include_text=True)}
            requirements = []
            for sid in task["payload"]["source_ids"]:
                name = "counter_a" if "counter_a" in sources[sid]["text"] else "counter_b"
                requirements.append({"local_id": sid, "statement": sources[sid]["text"], "source_ids": [sid],
                    "modules": [name], "features": ["count"], "interfaces": [], "conditions": ["enabled clock edge"],
                    "expected": "Increment modulo 16; asynchronous reset clears", "exceptions": [], "applicability": "in_scope"})
            response = {"requirements": requirements, "dispositions": [
                {"source_id": sid, "classification": "requirements", "reason": "Explicit module behavior"}
                for sid in task["payload"]["source_ids"]], "unresolved": [], "cross_references": []}
            self.finish(task, response, self.project._validate_model)
        self.project.reconcile()
        self.contracts = {}
        for task in self.project.store.list_tasks(kind="spec-contract"):
            name = task["payload"]["module_name"]
            refs = [requirement["id"] for requirement in task["payload"]["requirements"]]
            provenance = {"origin": "supplied", "requirement_ids": refs}
            contract = {"entry_views": ["microarchitecture"], "top": name,
                "modules": [{"id": task["payload"]["module_id"], "name": name, "role": "Counter IP"}],
                "ports": [{"name": port, "direction": direction, "width": width, "clock_domain": "core", "reset": "reset", **provenance}
                          for port, direction, width in (("clk", "input", 1), ("rst_n", "input", 1), ("en", "input", 1), ("count", "output", 4))],
                "parameters": [], "clocks": [{"name": "core", "port": "clk", "edge": "rising", "period_ns": None, **provenance}],
                "resets": [{"name": "reset", "port": "rst_n", "active": "low", "clock_domain": "core", "asynchronous": True, **provenance}],
                "relations": [], "decisions": [], "unresolved": [],
                "requirements": [{"requirement_id": rid, "module_ids": [task["payload"]["module_id"]], "relation_ids": []} for rid in refs]}
            self.finish(task, contract, lambda t, r, a: engineering.validate(self.project, t, r, a))
            self.contracts[name] = task["id"]
        engineering.reconcile(self.project)
        for task in self.project.store.list_tasks(kind="contract-plan"):
            plan = {"testpoints": [{"id": "count", "name": "Observed count sequence", "requirement_ids": [item["id"] for item in task["payload"]["requirements"]],
                    "level": "ip", "stimulus": "Toggle enable across reset and wrap", "observations": ["Actual output count"],
                    "checkers": ["Compare count to independent modulo-16 model"], "coverage": ["Reset, hold, increment and wrap"],
                    "uvm_components": ["counter_agent", "counter_scoreboard"], "configurations": ["four-bit counter"], "priority": "critical"}],
                    "unresolved": []}
            self.finish(task, plan, lambda t, r, a: engineering.validate(self.project, t, r, a))

    def finish(self, task, response, validator):
        attempt = self.project.store.claim(task["id"], "fixture-session")
        path = self.project.store.state_dir / "attempts" / attempt["id"] / "fixture-response.json"
        save_json(path, response)
        self.project.store.finish(task["id"], attempt["id"], response, artifacts=[path], validator=validator)

    def prepare(self, name):
        return host_work.generate(self.project, {"top": name, "rtl_files": [],
            "contract_task_id": self.contracts[name], "session_id": "prepare-" + name})

    def assert_reconnect_claims(self, task_id, kind):
        restarted = DVProject(self.root)
        result = host_work.claim(restarted, {"task_id": task_id, "session_id": "reconnected-runtime"})
        self.assertEqual("awaiting_host", result["status"])
        self.assertEqual(task_id, result["task_id"])
        self.assertEqual(kind, result["kind"])
        context = host_work.context(restarted, result["attempt_id"])
        pack = json.loads(context["prompt"].splitlines()[-1])
        self.assertEqual(self.contracts["counter_a"], pack["shared_specification"]["task_id"])
        self.assertEqual("counter_a", pack["top_declaration"]["module"])
        self.assertIsNone(context["context_budget_chars"])
        self.assertGreater(context["context_chars"], 0)

    def test_newer_unscoped_request_cannot_steal_earlier_architecture_claim(self):
        first = self.prepare("counter_a")
        self.project.store.fail(first["task_id"], first["attempt_id"], "Switch session before architecture generation", retryable=True)
        self.prepare("counter_b")
        self.assert_reconnect_claims(first["task_id"], "architecture")

    def test_newer_unscoped_request_cannot_steal_earlier_component_claim(self):
        first = self.prepare("counter_a")
        context = host_work.context(self.project, first["attempt_id"])
        pack = json.loads(context["prompt"].splitlines()[-1])
        response = uvm_fixture.blueprint(pack["accepted_testpoint_catalog"][0]["id"])
        response["top"] = "counter_a"
        host_work.submit(self.project, first["attempt_id"], response)
        task = next(item for item in self.project.store.ready_tasks(100)
                    if item["kind"] == "implement" and item["payload"]["contract_task_id"] == self.contracts["counter_a"])
        self.prepare("counter_b")
        self.assert_reconnect_claims(task["id"], "implement")

    def test_large_contract_reconnects_without_a_context_budget_change(self):
        first = self.prepare("counter_a")
        context = host_work.context(self.project, first["attempt_id"])
        pack = json.loads(context["prompt"].splitlines()[-1])
        response = uvm_fixture.blueprint(pack["accepted_testpoint_catalog"][0]["id"])
        response["top"] = "counter_a"
        response["shared_contracts"][0]["description"] += " retained contract" * 6000
        host_work.submit(self.project, first["attempt_id"], response)
        task = next(item for item in self.project.store.ready_tasks(100)
                    if item["kind"] == "implement" and item["payload"]["contract_task_id"] == self.contracts["counter_a"])
        result = host_work.generate(self.project, {"top": "counter_a", "rtl_files": [],
            "contract_task_id": self.contracts["counter_a"], "session_id": "legacy-small-context",
            "context_chars": 1}, expected_task_id=task["id"])
        self.assertEqual("awaiting_host", result["status"])
        self.assertEqual(task["id"], result["task_id"])
        full = host_work.context(self.project, result["attempt_id"])
        self.assertIsNone(full["context_budget_chars"])
        self.assertIn(response["shared_contracts"][0]["description"], full["prompt"])
        self.assertEqual([], self.project.store.list_issues())
        self.project.store.fail(task["id"], result["attempt_id"], "Reconnect selected semantic work", retryable=True)
        restarted = DVProject(self.root)
        result = host_work.claim(restarted, {"task_id": task["id"], "session_id": "new-connection"})
        self.assertEqual("awaiting_host", result["status"])
        self.assertEqual(task["id"], result["task_id"])
        self.assertIn(response["shared_contracts"][0]["description"], host_work.context(restarted, result["attempt_id"])["prompt"])
        self.assertEqual([], restarted.store.list_issues())

    def component_response(self, context, *, marker=""):
        prompt = context["prompt"].split("\nRevision feedback (preserve the source contract):\n", 1)[0]
        pack = json.loads(prompt.splitlines()[-1])
        component = pack["component"]
        files = []
        for path in component["output_paths"]:
            if path.endswith(".f"):
                content = "shared.sv\nagent.sv\nenvironment.sv\ntest.sv\ntb_top.sv\n"
            elif path == "tb_top.sv":
                content = "module tb_top; counter_a dut(); endmodule\n"
            else:
                content = "class " + component["id"] + "_candidate; int observed; endclass\n"
            files.append({"path": path, "content": content + ("// " + marker + "\n" if marker else "")})
        return {"files": files, "notes": ["Transport candidate fixture; not compiled or simulated"], "unresolved": []}

    def test_revision_rebuilds_only_dependency_descendants_after_new_parent_acceptance(self):
        first = self.prepare("counter_a")
        context = host_work.context(self.project, first["attempt_id"])
        pack = json.loads(context["prompt"].splitlines()[-1])
        blueprint = uvm_fixture.blueprint(pack["accepted_testpoint_catalog"][0]["id"])
        blueprint["top"] = "counter_a"
        host_work.submit(self.project, first["attempt_id"], blueprint)
        for _ in range(4):
            task = next(item for item in self.project.store.ready_tasks(100)
                        if item["kind"] == "implement" and item["payload"]["contract_task_id"] == self.contracts["counter_a"])
            ticket = host_work.claim(self.project, {"task_id": task["id"], "session_id": "initial-components"})
            context = host_work.context(self.project, ticket["attempt_id"])
            host_work.submit(self.project, ticket["attempt_id"], self.component_response(context))
        tasks = {item["payload"]["component"]["id"]: item for item in self.project.store.list_tasks(kind="implement")}
        self.assertTrue(all(task["status"] == "done" for task in tasks.values()))
        original = tasks["shared"]
        old_file = self.root / original["result"]["files"][0]["artifact_path"]
        old_bytes = old_file.read_bytes()
        reason = "Repair the shared transaction representation while preserving the specification."
        result = host_work.revise(self.project, original["id"], reason)
        self.assertEqual({task["id"] for task in tasks.values()}, set(result["invalidated_tasks"]))
        self.assertEqual("done", self.project.store.get_task(first["task_id"])["status"])
        self.assertEqual("done", self.project.store.get_task(self.contracts["counter_a"])["status"])
        self.assertEqual("stale", self.project.store.get_task(tasks["uvc"]["id"])["status"])
        restarted = DVProject(self.root)
        ticket = host_work.claim(restarted, {"task_id": original["id"], "session_id": "repair-parent"})
        context = host_work.context(restarted, ticket["attempt_id"])
        self.assertIn(reason, context["prompt"])
        host_work.submit(restarted, ticket["attempt_id"], self.component_response(context, marker="REVISED_SHARED_CONTRACT_FIXTURE"))
        self.assertEqual("pending", restarted.store.get_task(tasks["uvc"]["id"])["status"])
        self.assertEqual("stale", restarted.store.get_task(tasks["environment"]["id"])["status"])
        self.assertEqual(old_bytes, old_file.read_bytes())
        self.assertEqual(original["payload"], restarted.store.get_task(original["id"])["payload"])
        child = host_work.claim(restarted, {"task_id": tasks["uvc"]["id"], "session_id": "repair-dependent"})
        child_context = host_work.context(restarted, child["attempt_id"])
        self.assertIn("REVISED_SHARED_CONTRACT_FIXTURE", child_context["prompt"])
        host_work.submit(restarted, child["attempt_id"], self.component_response(child_context, marker="DEPENDENT_REBUILT_FIXTURE"))
        self.assertEqual("pending", restarted.store.get_task(tasks["environment"]["id"])["status"])
        self.assertEqual("stale", restarted.store.get_task(tasks["test"]["id"])["status"])


if __name__ == "__main__":
    unittest.main()
