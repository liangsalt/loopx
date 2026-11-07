"""UVM generation control-plane tests; runtime doubles never claim EDA success."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from loopx.capabilities.chip_verification.engine.generation import GenerationBudgetError, generation_scopes, run_generation
from loopx.capabilities.chip_verification.engine.models import ConflictError, ValidationError
from loopx.capabilities.chip_verification.engine.workflow import DVProject, save_json


def blueprint(point_id="accepted_plan:TP-order"):
    def component(identifier, kind, roles, dependencies, files, outputs, mapped=False):
        return {"id": identifier, "kind": kind, "responsibilities": roles,
                "purpose": "Implement " + identifier + " behavior", "depends_on": dependencies,
                "dependency_files": files, "testpoint_ids": [point_id] if mapped else [],
                "output_paths": outputs, "contract": identifier + " contract with ready/valid transactions"}
    return {
        "top": "dut", "uvm_version": "1.2",
        "interfaces": [{"name": "fifo_if", "description": "ready/valid data interface", "signals": ["clk", "rst_n", "valid", "ready", "data"]}],
        "shared_contracts": [{"name": "fifo_transaction", "description": "Observed accepted data item"}],
        "components": [
            component("shared", "package", ["interface", "transaction", "config", "package"], [], [], ["shared.sv"]),
            component("uvc", "agent", ["driver", "monitor", "sequencer", "agent"], ["shared"], ["shared.sv"], ["agent.sv"]),
            component("environment", "env", ["env", "scoreboard", "coverage"], ["uvc"], ["agent.sv"], ["environment.sv"], True),
            component("test", "test", ["sequence", "test", "tb_top", "build_manifest"], ["environment"], ["environment.sv"], ["test.sv", "tb_top.sv", "files.f"], True),
        ],
        "omitted_optional": [{"kind": "ral", "reason": "No register interface is present"}], "unresolved": [],
    }


class CandidateRuntime:
    """Returns deliberately simple uncompiled candidates to exercise contracts."""
    def __init__(self, architecture=None, mutate=None, after_run=None):
        self.architecture = architecture
        self.mutate, self.after_run = mutate, after_run
        self.calls = []

    def run(self, prompt, *, cwd, run_dir, schema, attempt_id):
        context = json.loads(prompt.splitlines()[-1])
        kind = "component" if "component" in context else "architecture"
        if kind == "architecture":
            response = copy.deepcopy(self.architecture or blueprint())
            if self.architecture is None:
                references = [point["id"] for point in context["accepted_testpoint_catalog"]]
                for component in response["components"]:
                    if component["testpoint_ids"]:
                        component["testpoint_ids"] = references
        else:
            component = context["component"]
            response = {"files": [{"path": path, "content": (
                "shared.sv\nagent.sv\nenvironment.sv\ntest.sv\ntb_top.sv\n" if path.endswith(".f") else
                "// Candidate transport fixture: " + component["id"] + "\n" +
                ("// SHARED_CONTRACT_SENTINEL\n" if component["id"] == "shared" else "") +
                ("module tb_top; dut dut_instance(); endmodule\n" if path == "tb_top.sv" else
                 "class " + component["id"] + "_candidate; int observed; endclass\n")
            )} for path in component["output_paths"]], "notes": ["Unit transport fixture only"], "unresolved": []}
        if self.mutate:
            response = self.mutate(kind, copy.deepcopy(context), response)
        self.calls.append({"kind": kind, "context": context, "prompt": prompt, "schema": schema, "attempt_id": attempt_id})
        directory = Path(run_dir)
        save_json(directory / "response.json", response)
        save_json(directory / "receipt.json", {"session_id": "unit-" + attempt_id, "candidate_only": True})
        (directory / "events.jsonl").write_text('{"type":"unit_candidate"}\n', encoding="utf-8")
        if self.after_run:
            self.after_run(kind, context)
        return SimpleNamespace(response=response)


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.spec = self.root / "spec.json"
        self.spec.write_text(json.dumps({"description": "DO_NOT_COPY_WHOLE_SPEC " + "background " * 23000}), encoding="utf-8")
        self.rtl = self.root / "dut.sv"
        self.rtl.write_text("module dut(input logic clk, rst_n, valid, output logic ready, input logic [7:0] data);\n"
                            "// RTL_BODY_SENTINEL\nassign ready = rst_n;\nendmodule\n", encoding="utf-8")
        self.project = DVProject(self.root)
        self.project.import_spec(self.spec, name="FIFO unit fixture", scope="accepted FIFO ordering plan", chunk_chars=8000, context_chars=14000)
        source_id = self.project.store.list_sources(limit=1)[0]["id"]
        self.project.store.add_requirement("Accepted words preserve order", [source_id],
                                           {"modules": ["fifo"], "features": ["ordering"], "interfaces": ["ready_valid"]},
                                           requirement_id="R-order")
        task = self.project.store.add_task("plan", "Accepted fixture plan", task_id="accepted_plan")
        attempt = self.project.store.claim(task["id"], "unit-test")
        result = {"testpoints": [{"id": "TP-order", "name": "FIFO order", "requirement_ids": ["R-order"],
                   "level": "ip", "stimulus": "Send distinct words with backpressure", "observations": ["accepted words"],
                   "checkers": ["compare against reference queue"], "coverage": ["empty and full occupancy"],
                   "uvm_components": ["fifo_agent", "fifo_scoreboard"], "configurations": ["depth=4"], "priority": "critical"}],
                  "unresolved": []}
        artifact = self.root / "plan-fixture.json"
        save_json(artifact, result)
        self.project.store.finish(task["id"], attempt["id"], result, [artifact],
                                  validator=lambda *args: {"passed": True, "checks": ["unit_fixture_only"]})

    def tearDown(self):
        self.temporary.cleanup()

    def generate(self, runtime, **options):
        with patch("loopx.capabilities.chip_verification.engine.generation.runtime", return_value=runtime):
            return run_generation(self.project, [self.rtl], "dut", **options)

    def make_profile(self):
        source = self.root / "interface-description.md"
        source.write_text("# Handshake\nA transfer occurs when ready and valid are both asserted.\n" +
                          "UNRELATED_PROFILE_SOURCE_BODY\n" * 1000, encoding="utf-8")
        profile = {"schema_version": 1, "top": "dut", "uvm_version": "1.2",
                   "sources": [
                       {"id": "interface_doc", "path": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "purpose": "Documented interface protocol"},
                       {"id": "rtl", "path": self.rtl.name, "sha256": hashlib.sha256(self.rtl.read_bytes()).hexdigest(), "purpose": "Actual port binding"}],
                   "documented_design_facts": [{"id": "transfer", "statement": "A transfer occurs when ready and valid are both asserted.",
                                                 "source_refs": ["interface_doc"], "locator": "Handshake"}],
                   "rtl_bindings": [{"id": "ports", "statement": "Bind the declared clk, rst_n, valid, ready and data ports.",
                                     "source_refs": ["rtl"], "locator": "module dut header", "value": {"clock": "clk", "reset": "rst_n"}}],
                   "testbench_choices": [{"id": "clock", "statement": "Use a 10 ns clock period in this bench.",
                                          "rationale": "A bounded simulation choice; no DUT frequency guarantee", "source_refs": [],
                                          "value": {"period_ns": 10}}]}
        path = self.root / "verification-profile.json"
        save_json(path, profile)
        return path, profile, source

    def test_bounded_dependency_contexts_and_candidate_claims(self):
        adapter = CandidateRuntime()
        result = self.generate(adapter)
        self.assertEqual("candidate_generated", result["status"])
        self.assertEqual(4, result["components_done"])
        self.assertEqual(5, len(adapter.calls))
        self.assertEqual(5, len({call["attempt_id"] for call in adapter.calls}))
        self.assertGreater(self.spec.stat().st_size, 200 * 1024)
        for call in adapter.calls:
            self.assertLessEqual(len(call["prompt"]), 14000)
            self.assertNotIn("DO_NOT_COPY_WHOLE_SPEC", call["prompt"])
            self.assertNotIn("RTL_BODY_SENTINEL", call["prompt"])
        environment = adapter.calls[3]["context"]
        self.assertEqual({"shared.sv", "agent.sv"}, {item["path"] for item in environment["required_dependency_files"]})
        self.assertIn("SHARED_CONTRACT_SENTINEL", json.dumps(environment))
        self.assertEqual(["accepted_plan:TP-order"], [point["id"] for point in environment["testpoints"]])
        self.assertFalse(result["compiled"])
        self.assertFalse(result["simulated"])
        self.assertFalse(result["coverage_verified"])
        self.assertFalse(result["planning_scope_complete"])
        for record in result["files"]:
            source = self.root / record["artifact_path"]
            self.assertEqual(record["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(source.read_bytes(), Path(record["view_path"]).read_bytes())

    def test_resume_uses_persisted_state_and_never_regenerates_done_components(self):
        first = CandidateRuntime()
        partial = self.generate(first, max_components=1)
        self.assertEqual("partial", partial["status"])
        self.assertEqual(2, len(first.calls))
        artifact = self.root / partial["files"][0]["artifact_path"]
        before = (artifact.stat().st_mtime_ns, artifact.read_bytes())
        self.project = DVProject(self.root)
        second = CandidateRuntime()
        complete = self.generate(second)
        self.assertEqual(3, len(second.calls))
        self.assertTrue(all(call["kind"] == "component" for call in second.calls))
        self.assertEqual(before, (artifact.stat().st_mtime_ns, artifact.read_bytes()))
        self.assertEqual(partial["generation_id"], complete["generation_id"])
        third = CandidateRuntime()
        self.assertEqual(0, self.generate(third)["components_generated_this_call"])
        self.assertEqual([], third.calls)

    def test_missing_or_wrong_top_is_rejected_before_model_dispatch(self):
        with patch("loopx.capabilities.chip_verification.engine.generation.runtime") as adapter:
            with self.assertRaisesRegex(ValidationError, "exactly one"):
                run_generation(self.project, [self.rtl], "not_the_dut")
            with self.assertRaisesRegex(ValidationError, "actual RTL"):
                run_generation(self.project, [], "dut")
            adapter.assert_not_called()

    def test_blueprint_cannot_change_top(self):
        value = blueprint()
        value["top"] = "replacement_dut"
        with self.assertRaisesRegex(ValidationError, "changes.*top"):
            self.generate(CandidateRuntime(value))
        self.assertEqual([], self.project.store.list_tasks(status="done", kind="architecture"))

    def test_path_escape_and_windows_alias_are_rejected_before_files_are_written(self):
        for path in ("../../escape.sv", "C:/escape.sv", "dir\\escape.sv", "./escape.sv", "CON.sv", "aliased. /escape.sv"):
            with self.subTest(path=path):
                value = blueprint()
                value["components"][0]["output_paths"] = [path]
                with self.assertRaises(ValidationError):
                    self.generate(CandidateRuntime(value))
        self.assertFalse((self.root / "escape.sv").exists())
        self.assertEqual([], list((self.root / "generated_uvm").rglob("shared.sv")))

    def test_case_duplicate_ownership_and_dependency_cycles_are_rejected(self):
        duplicate = blueprint()
        duplicate["components"][1]["output_paths"] = ["SHARED.sv"]
        with self.assertRaisesRegex(ValidationError, "Duplicate output ownership"):
            self.generate(CandidateRuntime(duplicate))
        cyclic = blueprint()
        cyclic["components"][0]["depends_on"] = ["test"]
        with self.assertRaisesRegex(ValidationError, "cycle"):
            self.generate(CandidateRuntime(cyclic))

    def test_testpoint_requires_stimulus_checker_and_observed_coverage_mapping(self):
        value = blueprint()
        value["components"][2]["testpoint_ids"] = []
        with self.assertRaisesRegex(ValidationError, "stimulus, checking and coverage"):
            self.generate(CandidateRuntime(value))

    def test_component_cannot_write_unassigned_path_or_redefine_rtl(self):
        def escape(kind, context, response):
            if kind == "component":
                response["files"][0]["path"] = "../../dut.sv"
            return response
        original = self.rtl.read_bytes()
        with self.assertRaisesRegex(ValidationError, "exclusively owned"):
            self.generate(CandidateRuntime(mutate=escape))
        def redefine(kind, context, response):
            if kind == "component":
                response["files"][0]["content"] = "module automatic dut; endmodule"
            return response
        with self.assertRaisesRegex(ValidationError, "redefine the DUT"):
            self.generate(CandidateRuntime(mutate=redefine))
        self.assertEqual(original, self.rtl.read_bytes())
        self.assertEqual([], self.project.store.list_tasks(status="done", kind="implement"))

    def test_rtl_change_after_ai_response_is_rejected_before_acceptance(self):
        def mutate_rtl(kind, context):
            self.rtl.write_text(self.rtl.read_text(encoding="utf-8") + "// changed in flight\n", encoding="utf-8")
        with self.assertRaisesRegex(ConflictError, "changed during generation"):
            self.generate(CandidateRuntime(after_run=mutate_rtl))
        self.assertEqual([], self.project.store.list_tasks(status="done", kind="architecture"))
        self.assertEqual([], list((self.root / "generated_uvm").rglob("architecture.json")))

    def test_required_dependency_overflow_is_explicit_and_never_truncated(self):
        def huge_shared(kind, context, response):
            if kind == "component" and context["component"]["id"] == "shared":
                response["files"][0]["content"] += "// exact shared content " + "x" * 14000 + "\n"
            return response
        adapter = CandidateRuntime(mutate=huge_shared)
        result = self.generate(adapter)
        self.assertEqual("partial", result["status"])
        self.assertEqual(2, len(adapter.calls))
        self.assertEqual(1, len(self.project.store.list_tasks(status="done", kind="implement")))
        blocked = self.project.store.list_tasks(status="blocked", kind="implement")
        self.assertEqual(1, len(blocked))
        self.assertIn("no required input was truncated", blocked[0]["reason"])
        issues = self.project.store.list_issues()
        self.assertEqual(["uvm_context_blocker"], [issue["category"] for issue in issues])
        details = json.loads(issues[0]["description"])
        self.assertGreater(details["required_chars"], details["context_chars"])
        evidence = json.loads(Path(details["evidence_path"]).read_text(encoding="utf-8"))
        self.assertIn("x" * 14000, evidence["prompt"])
        # A new session reaches the same explicit boundary, without repeating
        # or silently shortening the already accepted shared contract.
        second = CandidateRuntime()
        self.generate(second)
        self.assertEqual([], second.calls)
        self.assertEqual(blocked[0]["revision"], self.project.store.get_task(blocked[0]["id"])["revision"])
        self.assertEqual(1, len(self.project.store.list_issues()))

    def test_full_catalog_overflow_occurs_before_architecture_dispatch(self):
        config = self.project.config
        config["context_chars"] = 1000
        save_json(self.project.config_path, config)
        with patch("loopx.capabilities.chip_verification.engine.generation.runtime") as adapter:
            with self.assertRaises(GenerationBudgetError):
                run_generation(self.project, [self.rtl], "dut")
            adapter.assert_not_called()

    def test_changed_dependency_artifact_invalidates_downstream_readiness(self):
        result = self.generate(CandidateRuntime())
        path = self.root / result["files"][0]["artifact_path"]
        path.write_text("// changed shared interface\n", encoding="utf-8")
        adapter = CandidateRuntime()
        resumed = self.generate(adapter)
        self.assertEqual([], adapter.calls)
        self.assertEqual("partial", resumed["status"])
        self.assertEqual(0, resumed["components_done"])
        self.assertEqual([], resumed["files"])
        self.assertFalse(resumed["compiled"])

    def test_large_plan_automatically_partitions_and_advances_semantic_scopes(self):
        source_id = self.project.store.list_sources(limit=1)[0]["id"]
        points = []
        for index in range(60):
            identifier = "R-extra-" + str(index)
            self.project.store.add_requirement("Observe independent scenario " + str(index), [source_id],
                {"modules": ["dma" if index % 2 else "fifo"],
                 "features": ["backpressure" if index % 3 else "ordering"], "interfaces": ["ready_valid"]},
                requirement_id=identifier)
            point = copy.deepcopy(self.project.store.get_task("accepted_plan")["result"]["testpoints"][0])
            point.update({"id": "TP-extra-" + str(index), "requirement_ids": [identifier],
                          "name": "Independent scenario " + str(index),
                          "stimulus": "TP_DETAIL_ONLY " + "accepted traffic with backpressure; " * 12})
            points.append(point)
        task = self.project.store.add_task("plan", "Large accepted fixture plan", task_id="large_plan")
        attempt = self.project.store.claim(task["id"], "unit-test")
        artifact = self.root / "large-plan-fixture.json"
        save_json(artifact, {"testpoints": points, "unresolved": []})
        self.project.store.finish(task["id"], attempt["id"], {"testpoints": points, "unresolved": []}, [artifact],
                                  validator=lambda *args: {"passed": True, "checks": ["unit_fixture_only"]})
        inventory = generation_scopes(self.project)
        self.assertGreater(inventory["scope_count"], 4)
        references = [identifier for scope in inventory["scopes"] for identifier in scope["testpoint_ids"]]
        self.assertEqual(61, len(references))
        self.assertEqual(len(references), len(set(references)))
        self.assertNotIn("TP_DETAIL_ONLY", json.dumps(inventory))
        for scope in inventory["scopes"]:
            self.assertTrue(scope["ready"])
            self.assertLessEqual(scope["testpoint_characters"], scope["testpoint_budget"])
            self.assertEqual(1, len(scope["tags"]["modules"]))
        first_runtime = CandidateRuntime()
        first = self.generate(first_runtime)
        second_runtime = CandidateRuntime()
        second = self.generate(second_runtime)
        self.assertNotEqual(first["generation_scope"]["id"], second["generation_scope"]["id"])
        self.assertFalse(second["cross_scope_integration_verified"])
        for call in first_runtime.calls + second_runtime.calls:
            self.assertLessEqual(len(call["prompt"]), 14000)
        repeated_runtime = CandidateRuntime()
        repeated = self.generate(repeated_runtime, scope_id=first["generation_scope"]["id"])
        self.assertEqual([], repeated_runtime.calls)
        self.assertEqual(first["generation_id"], repeated["generation_id"])

    def test_unknown_scope_is_rejected_before_dispatch(self):
        with patch("loopx.capabilities.chip_verification.engine.generation.runtime") as adapter:
            with self.assertRaisesRegex(ConflictError, "scope is absent or changed"):
                run_generation(self.project, [self.rtl], "dut", scope_id="scope-does-not-exist")
            adapter.assert_not_called()

    def test_profile_and_complete_behavior_are_in_bounded_architecture_and_component_contexts(self):
        profile_path, profile, _ = self.make_profile()
        adapter = CandidateRuntime()
        result = self.generate(adapter, profile_path=profile_path)
        architecture = adapter.calls[0]["context"]
        self.assertEqual("Send distinct words with backpressure", architecture["testpoints"][0]["stimulus"])
        self.assertEqual("Accepted words preserve order", architecture["requirements"][0]["statement"])
        self.assertEqual(profile["testbench_choices"], architecture["verification_profile"]["testbench_choices"])
        self.assertEqual(profile["documented_design_facts"], architecture["verification_profile"]["documented_design_facts"])
        environment = adapter.calls[3]["context"]
        self.assertEqual("R-order", environment["requirements"][0]["id"])
        self.assertEqual(profile["rtl_bindings"], environment["verification_profile"]["rtl_bindings"])
        for call in adapter.calls:
            self.assertNotIn("UNRELATED_PROFILE_SOURCE_BODY", call["prompt"])
            self.assertNotIn("DO_NOT_COPY_WHOLE_SPEC", call["prompt"])
            self.assertLessEqual(len(call["prompt"]), 14000)
        self.assertEqual(hashlib.sha256(profile_path.read_bytes()).hexdigest(), result["verification_profile"]["sha256"])

    def test_profile_top_source_hash_path_and_references_are_checked_before_dispatch(self):
        profile_path, original, _ = self.make_profile()
        invalid = []
        top = copy.deepcopy(original)
        top["top"] = "other_dut"
        invalid.append(top)
        digest = copy.deepcopy(original)
        digest["sources"][0]["sha256"] = "0" * 64
        invalid.append(digest)
        missing = copy.deepcopy(original)
        missing["sources"][0]["path"] = "missing-document.md"
        invalid.append(missing)
        unknown = copy.deepcopy(original)
        unknown["documented_design_facts"][0]["source_refs"] = ["invented_source"]
        invalid.append(unknown)
        no_source = copy.deepcopy(original)
        no_source["rtl_bindings"][0]["source_refs"] = []
        invalid.append(no_source)
        with patch("loopx.capabilities.chip_verification.engine.generation.runtime") as adapter:
            for profile in invalid:
                with self.subTest(profile=profile):
                    save_json(profile_path, profile)
                    with self.assertRaises(ValidationError):
                        run_generation(self.project, [self.rtl], "dut", profile_path=profile_path, max_components=0)
            adapter.assert_not_called()

    def test_profile_change_after_ai_is_not_accepted(self):
        profile_path, profile, _ = self.make_profile()
        def change_profile(kind, context):
            profile["testbench_choices"][0]["value"]["period_ns"] = 20
            save_json(profile_path, profile)
        with self.assertRaisesRegex(ConflictError, "changed during generation"):
            self.generate(CandidateRuntime(after_run=change_profile), profile_path=profile_path, max_components=0)
        self.assertEqual([], self.project.store.list_tasks(status="done", kind="architecture"))

    def test_profile_source_change_after_ai_is_not_accepted(self):
        profile_path, _, source = self.make_profile()
        def change_source(kind, context):
            source.write_text("Changed protocol", encoding="utf-8")
        with self.assertRaisesRegex(ConflictError, "SHA-256 mismatch"):
            self.generate(CandidateRuntime(after_run=change_source), profile_path=profile_path, max_components=0)
        self.assertEqual([], self.project.store.list_tasks(status="done", kind="architecture"))

    def test_profile_choices_change_input_identity_and_leave_previous_candidate_immutable(self):
        profile_path, profile, _ = self.make_profile()
        first = self.generate(CandidateRuntime(), profile_path=profile_path, max_components=1)
        artifact = self.root / first["files"][0]["artifact_path"]
        original = artifact.read_bytes()
        profile["testbench_choices"][0]["value"]["period_ns"] = 20
        profile["testbench_choices"][0]["statement"] = "Use a 20 ns clock period in this bench."
        save_json(profile_path, profile)
        adapter = CandidateRuntime()
        second = self.generate(adapter, profile_path=profile_path, max_components=0)
        self.assertNotEqual(first["generation_id"], second["generation_id"])
        self.assertEqual(original, artifact.read_bytes())
        self.assertEqual("architecture", adapter.calls[0]["kind"])

    def test_architecture_contract_blocker_persists_and_new_profile_can_advance(self):
        blocked_blueprint = blueprint()
        blocked_blueprint["components"] = []
        blocked_blueprint["unresolved"] = ["Missing documented handshake and explicit bench clock choice"]
        adapter = CandidateRuntime(blocked_blueprint)
        notifications = []
        blocked = self.generate(adapter, max_components=0, notify=notifications.append)
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("blocked", self.project.store.get_task(blocked["task_id"])["status"])
        self.assertEqual("blocked", notifications[0]["status"])
        self.assertEqual(1, len(list((self.root / ".loopx" / "chip-verification" / "attempts").rglob("blocked-contracts.json"))))
        second_adapter = CandidateRuntime()
        self.assertEqual("blocked", self.generate(second_adapter, max_components=0, context_chars=32000)["status"])
        self.assertEqual([], second_adapter.calls)
        profile_path, _, _ = self.make_profile()
        advanced = self.generate(second_adapter, profile_path=profile_path, max_components=0)
        self.assertEqual("partial", advanced["status"])
        self.assertEqual(1, len(second_adapter.calls))
        self.assertEqual("blocked", self.project.store.get_task(blocked["task_id"])["status"])

    def test_component_contract_blocker_does_not_complete_or_regenerate_prerequisite(self):
        def missing_contract(kind, context, response):
            if kind == "component" and context["component"]["id"] == "uvc":
                response["unresolved"] = ["Missing reset deassertion sampling contract"]
            return response
        adapter = CandidateRuntime(mutate=missing_contract)
        result = self.generate(adapter)
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["components_done"])
        self.assertEqual(1, result["components_generated_this_call"])
        self.assertEqual(1, len([item for item in result["pending_tasks"] if item["status"] == "blocked"]))
        second = CandidateRuntime()
        self.generate(second, context_chars=32000)
        self.assertEqual([], second.calls)

    def test_profile_overflow_never_truncates_documented_facts_or_dispatches(self):
        path, profile, _ = self.make_profile()
        profile["documented_design_facts"][0]["statement"] += "a" * 14000
        save_json(path, profile)
        with patch("loopx.capabilities.chip_verification.engine.generation.runtime") as adapter:
            result = run_generation(self.project, [self.rtl], "dut", profile_path=path, max_components=0)
            adapter.assert_not_called()
        self.assertEqual("blocked", result["status"])
        task = self.project.store.get_task(result["task_id"])
        self.assertEqual("architecture", task["kind"])
        self.assertTrue(task["reason"].startswith("uvm_context_blocker:"))
        issue = self.project.store.list_issues()[0]
        details = json.loads(issue["description"])
        evidence = json.loads(Path(details["evidence_path"]).read_text(encoding="utf-8"))
        self.assertIn("a" * 14000, evidence["prompt"])
        repeated = CandidateRuntime()
        self.generate(repeated, profile_path=path, max_components=0)
        self.assertEqual([], repeated.calls)
        self.assertEqual(task["revision"], self.project.store.get_task(task["id"])["revision"])
        resumed = CandidateRuntime()
        self.generate(resumed, profile_path=path, context_chars=32000, max_components=0)
        self.assertEqual(1, len(resumed.calls))
        self.assertEqual("done", self.project.store.get_task(task["id"])["status"])
        self.assertEqual([], self.project.store.list_issues())
        self.assertEqual(1, len(self.project.store.list_issues(status="resolved")))

    def test_component_dispatch_limit_counts_contract_blockers_not_only_successes(self):
        independent = blueprint()
        independent["components"][1]["depends_on"] = []
        independent["components"][1]["dependency_files"] = []
        def blocked_component(kind, context, response):
            if kind == "component":
                response["unresolved"] = ["A precise design contract remains unspecified"]
            return response
        first_adapter = CandidateRuntime(independent, mutate=blocked_component)
        first = self.generate(first_adapter, max_components=1)
        self.assertEqual(["architecture", "component"], [call["kind"] for call in first_adapter.calls])
        self.assertEqual(1, first["components_dispatched_this_call"])
        self.assertEqual(0, first["components_generated_this_call"])
        self.assertEqual(1, len(self.project.store.list_tasks(kind="implement", status="blocked")))
        second_adapter = CandidateRuntime(mutate=blocked_component)
        second = self.generate(second_adapter, max_components=1)
        self.assertEqual(1, len(second_adapter.calls))
        self.assertEqual("uvc", second_adapter.calls[0]["context"]["component"]["id"])
        self.assertEqual(1, second["components_dispatched_this_call"])
        self.assertEqual(0, second["components_generated_this_call"])
        third_adapter = CandidateRuntime()
        third = self.generate(third_adapter, max_components=1)
        self.assertEqual([], third_adapter.calls)
        self.assertEqual(0, third["components_dispatched_this_call"])

    def test_unsent_overflow_does_not_consume_dispatch_allowance_for_independent_component(self):
        independent = blueprint()
        independent["components"][0]["contract"] += "b" * 14000
        independent["components"][1]["depends_on"] = []
        independent["components"][1]["dependency_files"] = []
        adapter = CandidateRuntime(independent)
        result = self.generate(adapter, max_components=1)
        self.assertEqual(["architecture", "component"], [call["kind"] for call in adapter.calls])
        self.assertEqual("uvc", adapter.calls[1]["context"]["component"]["id"])
        self.assertEqual(1, result["components_dispatched_this_call"])
        self.assertEqual(1, result["components_generated_this_call"])
        self.assertEqual(1, len(self.project.store.list_tasks(kind="implement", status="blocked")))

    def test_explicit_larger_budget_unblocks_only_context_and_preserves_blueprint_identity(self):
        large_contract = blueprint()
        large_contract["shared_contracts"][0]["description"] += "k" * 14000
        first_runtime = CandidateRuntime(large_contract)
        original_config = self.project.config_path.read_bytes()
        first = self.generate(first_runtime, max_components=1)
        self.assertEqual(1, len(first_runtime.calls))
        self.assertEqual(0, first["components_done"])
        architecture = self.project.store.list_tasks(status="done", kind="architecture")[0]
        architecture_path = next((self.root / "generated_uvm").rglob("architecture.json"))
        architecture_snapshot = architecture_path.read_bytes(), architecture_path.stat().st_mtime_ns
        blocked = self.project.store.list_tasks(status="blocked", kind="implement")[0]
        second_runtime = CandidateRuntime()
        with patch("loopx.capabilities.chip_verification.engine.generation.runtime", return_value=second_runtime) as factory:
            second = run_generation(self.project, [self.rtl], "dut", context_chars=32000, max_components=1)
        self.assertEqual(first["generation_id"], second["generation_id"])
        self.assertEqual(architecture, self.project.store.get_task(architecture["id"]))
        self.assertEqual(architecture_snapshot, (architecture_path.read_bytes(), architecture_path.stat().st_mtime_ns))
        self.assertEqual(original_config, self.project.config_path.read_bytes())
        self.assertEqual(1, len(second_runtime.calls))
        self.assertEqual("component", second_runtime.calls[0]["kind"])
        self.assertEqual(32000, factory.call_args.kwargs["max_prompt_chars"])
        self.assertEqual("done", self.project.store.get_task(blocked["id"])["status"])
        self.assertEqual([], self.project.store.list_issues())
        resolved = self.project.store.list_issues(status="resolved")
        self.assertEqual(1, len(resolved))
        resolution = json.loads(resolved[0]["resolution"])
        self.assertEqual(32000, resolution["context_chars"])
        self.assertLessEqual(resolution["required_chars"], 32000)
        context_path = self.root / ".loopx" / "chip-verification" / "attempts" / second_runtime.calls[0]["attempt_id"] / "generation-context.json"
        context = json.loads(context_path.read_text(encoding="utf-8"))
        self.assertEqual(32000, context["context_chars"])
        self.assertEqual(len(second_runtime.calls[0]["prompt"]), context["required_chars"])
        self.assertIn("k" * 14000, context["prompt"])

    def test_increased_budget_still_too_small_does_not_dispatch_or_resolve(self):
        large_contract = blueprint()
        large_contract["shared_contracts"][0]["description"] += "k" * 14000
        self.generate(CandidateRuntime(large_contract), max_components=1)
        adapter = CandidateRuntime()
        result = self.generate(adapter, context_chars=14500, max_components=1)
        self.assertEqual([], adapter.calls)
        self.assertEqual(0, result["components_done"])
        self.assertEqual(2, len(self.project.store.list_issues()))
        self.assertEqual([], self.project.store.list_issues(status="resolved"))

    def test_pi_schema_and_runtime_share_the_execution_budget(self):
        adapter = CandidateRuntime()
        with patch("loopx.capabilities.chip_verification.engine.generation.runtime", return_value=adapter) as factory:
            result = run_generation(self.project, [self.rtl], "dut", agent="pi", context_chars=18000, max_components=1)
        self.assertEqual(18000, result["context_chars"])
        self.assertEqual(2, len(adapter.calls))
        for invocation, call in zip(factory.call_args_list, adapter.calls):
            self.assertEqual(18000, invocation.kwargs["max_prompt_chars"])
            extra = len("\nReturn only JSON conforming to this schema:\n" + json.dumps(call["schema"]))
            path = self.root / ".loopx" / "chip-verification" / "attempts" / call["attempt_id"] / "generation-context.json"
            recorded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(extra, recorded["schema_chars"])
            self.assertEqual(len(call["prompt"]) + extra, recorded["required_chars"])
            self.assertLessEqual(recorded["required_chars"], 18000)

    def test_invalid_execution_budget_is_rejected_without_mutating_project(self):
        original = self.project.config_path.read_bytes()
        with patch("loopx.capabilities.chip_verification.engine.generation.runtime") as adapter:
            for budget in (0, -1, True, 3.5):
                with self.subTest(budget=budget), self.assertRaises(ValueError):
                    run_generation(self.project, [self.rtl], "dut", context_chars=budget)
            adapter.assert_not_called()
        self.assertEqual(original, self.project.config_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
