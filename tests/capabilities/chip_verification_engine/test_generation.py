"""UVM generation control-plane tests; runtime doubles never claim EDA success."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from loopx.capabilities.chip_verification.engine import generation
from loopx.capabilities.chip_verification.engine.generation import generation_scopes, run_generation
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

    def test_large_required_dependency_is_preserved_and_does_not_split_component(self):
        def huge_shared(kind, context, response):
            if kind == "component" and context["component"]["id"] == "shared":
                response["files"][0]["content"] += "// exact shared content " + "x" * 120000 + "\n"
            return response
        adapter = CandidateRuntime(mutate=huge_shared)
        result = self.generate(adapter, context_chars=1)
        self.assertEqual("candidate_generated", result["status"])
        self.assertEqual(5, len(adapter.calls))
        self.assertEqual(4, len(self.project.store.list_tasks(status="done", kind="implement")))
        self.assertEqual([], self.project.store.list_issues())
        self.assertIn("x" * 120000, adapter.calls[2]["prompt"])
        self.assertEqual([], self.project.store.list_tasks(status="blocked"))
        second = CandidateRuntime()
        resumed = self.generate(second, context_chars=64000)
        self.assertEqual([], second.calls)
        self.assertEqual(result["generation_id"], resumed["generation_id"])

    def test_legacy_small_context_setting_does_not_block_architecture(self):
        save_json(self.project.config_path, {**self.project.config, "context_chars": 1})
        adapter = CandidateRuntime()
        result = self.generate(adapter)
        self.assertEqual("candidate_generated", result["status"])
        self.assertEqual(5, len(adapter.calls))
        self.assertIsNone(result["context_chars"])

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
        self.assertEqual(4, inventory["scope_count"])
        references = [identifier for scope in inventory["scopes"] for identifier in scope["testpoint_ids"]]
        self.assertEqual(61, len(references))
        self.assertEqual(len(references), len(set(references)))
        self.assertNotIn("TP_DETAIL_ONLY", json.dumps(inventory))
        for scope in inventory["scopes"]:
            self.assertTrue(scope["ready"])
            self.assertIsNone(scope["testpoint_budget"])
            self.assertEqual(1, len(scope["tags"]["modules"]))
        first_runtime = CandidateRuntime()
        first = self.generate(first_runtime)
        second_runtime = CandidateRuntime()
        second = self.generate(second_runtime)
        self.assertNotEqual(first["generation_scope"]["id"], second["generation_scope"]["id"])
        self.assertFalse(second["cross_scope_integration_verified"])
        repeated_runtime = CandidateRuntime()
        repeated = self.generate(repeated_runtime, scope_id=first["generation_scope"]["id"])
        self.assertEqual([], repeated_runtime.calls)
        self.assertEqual(first["generation_id"], repeated["generation_id"])

    def test_retired_budget_metadata_does_not_regenerate_identical_accepted_scope(self):
        scopes = generation._scope_entries
        def historical(project, points):
            result = scopes(project, points)
            for item in result:
                item["testpoint_budget"] = 4200
            return result
        with patch.object(generation, "_scope_entries", side_effect=historical):
            first = self.generate(CandidateRuntime())
        tasks = self.project.store.list_tasks(kind="architecture")
        resumed = CandidateRuntime()
        second = self.generate(resumed, context_chars=1)
        self.assertEqual([], resumed.calls)
        self.assertEqual(first["generation_id"], second["generation_id"])
        self.assertEqual(tasks, self.project.store.list_tasks(kind="architecture"))

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

    def test_large_profile_keeps_complete_facts_and_dispatches_normally(self):
        path, profile, _ = self.make_profile()
        profile["documented_design_facts"][0]["statement"] += "a" * 40000
        save_json(path, profile)
        adapter = CandidateRuntime()
        result = self.generate(adapter, profile_path=path, context_chars=1, max_components=0)
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, len(adapter.calls))
        self.assertIn("a" * 40000, adapter.calls[0]["prompt"])
        self.assertEqual(1, len(self.project.store.list_tasks(kind="architecture", status="done")))
        repeated = CandidateRuntime()
        resumed = self.generate(repeated, profile_path=path, context_chars=32000, max_components=0)
        self.assertEqual([], repeated.calls)
        self.assertEqual(result["generation_id"], resumed["generation_id"])
        self.assertEqual([], self.project.store.list_issues())

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

    def test_component_work_order_uses_dependencies_not_prose_length(self):
        independent = blueprint()
        independent["components"][0]["contract"] += "b" * 40000
        independent["components"][1]["depends_on"] = []
        independent["components"][1]["dependency_files"] = []
        adapter = CandidateRuntime(independent)
        result = self.generate(adapter, max_components=1)
        self.assertEqual(["architecture", "component"], [call["kind"] for call in adapter.calls])
        self.assertEqual("shared", adapter.calls[1]["context"]["component"]["id"])
        self.assertIn("b" * 40000, adapter.calls[1]["prompt"])
        self.assertEqual(1, result["components_dispatched_this_call"])
        self.assertEqual(1, result["components_generated_this_call"])
        self.assertEqual([], self.project.store.list_tasks(kind="implement", status="blocked"))

    def test_legacy_context_hold_recovers_without_budget_change_or_blueprint_rewrite(self):
        blocked, _ = self._context_blocked_component()
        architecture = self.project.store.list_tasks(status="done", kind="architecture")[0]
        original_config = self.project.config_path.read_bytes()
        architecture_path = next((self.root / "generated_uvm").rglob("architecture.json"))
        architecture_snapshot = architecture_path.read_bytes(), architecture_path.stat().st_mtime_ns
        adapter = CandidateRuntime()
        self.generate(adapter, max_components=1)
        self.assertEqual(architecture, self.project.store.get_task(architecture["id"]))
        self.assertEqual(architecture_snapshot, (architecture_path.read_bytes(), architecture_path.stat().st_mtime_ns))
        self.assertEqual(original_config, self.project.config_path.read_bytes())
        self.assertEqual(1, len(adapter.calls))
        self.assertEqual("done", self.project.store.get_task(blocked["id"])["status"])
        self.assertEqual([], self.project.store.list_issues())
        resolved = self.project.store.list_issues(status="resolved")
        self.assertEqual(1, len(resolved))
        resolution = json.loads(resolved[0]["resolution"])
        self.assertIsNone(resolution["context_chars"])
        path = self.root / ".loopx/chip-verification/attempts" / adapter.calls[0]["attempt_id"] / "generation-context.json"
        context = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsNone(context["context_chars"])
        self.assertIn("k" * 14000, context["prompt"])

    def test_positive_legacy_budget_does_not_limit_complete_component(self):
        large_contract = blueprint()
        large_contract["shared_contracts"][0]["description"] += "k" * 14000
        adapter = CandidateRuntime(large_contract)
        result = self.generate(adapter, context_chars=1, max_components=1)
        self.assertEqual(2, len(adapter.calls))
        self.assertEqual(1, result["components_done"])
        self.assertIn("k" * 14000, adapter.calls[1]["prompt"])
        self.assertEqual([], self.project.store.list_issues())

    def test_targeted_budget_recovery_leaves_sibling_blocker_unchanged(self):
        independent = blueprint()
        independent["shared_contracts"][0]["description"] += "k" * 14000
        independent["components"][1]["depends_on"] = []
        independent["components"][1]["dependency_files"] = []
        self.generate(CandidateRuntime(independent), max_components=0)
        for task in self.project.store.ready_tasks(100):
            if task["kind"] == "implement":
                self.record_legacy_context_hold(task)
        blocked = self.project.store.list_tasks(kind="implement", status="blocked")
        self.assertEqual(2, len(blocked))
        by_component = {task["payload"]["component"]["id"]: task for task in blocked}
        sibling, target = by_component["shared"], by_component["uvc"]
        sibling_issues = [issue for issue in self.project.store.list_issues()
                          if issue["task_id"] == sibling["id"]]
        dispatched = []
        dispatcher = SimpleNamespace(expected_task_id=target["id"], completed=False,
                                     owns=lambda task: False)

        def dispatch(project, task, prompt, schema, agent, timeout, validate, accept, budget):
            validate()
            attempt = project.store.claim(task["id"], "targeted-connection")
            dispatched.append((task["id"], attempt["id"]))
            dispatcher.completed = True
            return project.store.get_task(task["id"]), {"status": "claimed"}

        dispatcher.dispatch = dispatch
        run_generation(self.project, [self.rtl], "dut", context_chars=64000, dispatcher=dispatcher)
        self.assertEqual([target["id"]], [task_id for task_id, _ in dispatched])
        self.assertEqual(sibling, self.project.store.get_task(sibling["id"]))
        self.assertEqual(sibling_issues, self.project.store.list_issues())
        self.assertEqual("running", self.project.store.get_task(target["id"])["status"])

    def record_legacy_context_hold(self, task):
        path = self.root / (task["id"] + "-old-context.json")
        details = {"input_fingerprint": task["payload"]["input_fingerprint"], "context_chars": 14000,
                   "required_chars": 24000, "evidence_path": str(path), "agent": "codex"}
        save_json(path, {**details, "prompt": "Historical complete input", "output_schema": {}})
        blocked, _ = self.project.store.block_context(task["id"], task["revision"],
            "uvm_context_blocker: old fixed character limit", json.dumps(details))
        return blocked, json.loads(path.read_text(encoding="utf-8"))

    def _context_blocked_component(self):
        large_contract = blueprint()
        large_contract["shared_contracts"][0]["description"] += "k" * 14000
        self.generate(CandidateRuntime(large_contract), max_components=0)
        task = next(task for task in self.project.store.ready_tasks(100) if task["kind"] == "implement")
        return self.record_legacy_context_hold(task)

    def test_two_connections_recover_same_snapshot_once_and_only_one_claims(self):
        task, evidence = self._context_blocked_component()
        projects = [DVProject(self.root), DVProject(self.root)]
        ready = Barrier(2)

        def recover(project):
            return generation._prepare_dispatch_context(
                project, task, evidence["prompt"], {}, 64000, evidence["agent"],
                evidence["output_schema"], 64000, lambda: ready.wait(timeout=10))

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(recover, projects))
        self.assertTrue(all(current["status"] == "pending" and prompt is not None
                            for current, prompt, _ in results))
        self.assertEqual(task["revision"] + 1, self.project.store.get_task(task["id"])["revision"])
        self.assertEqual([], self.project.store.list_issues())
        self.assertEqual(1, len(self.project.store.list_issues(status="resolved")))
        events = self.project.store.events(limit=1000)
        self.assertEqual(1, len([event for event in events if event["type"] == "task_requeued"
                                and event["task_id"] == task["id"]]))
        ready = Barrier(2)

        def claim(index):
            ready.wait(timeout=10)
            try:
                return projects[index].store.claim(task["id"], "connection-" + str(index))
            except ConflictError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = list(pool.map(claim, range(2)))
        accepted = [attempt for attempt in attempts if attempt]
        self.assertEqual(1, len(accepted))
        self.assertEqual(accepted[0]["id"], self.project.store.get_task(task["id"])["attempt_id"])

    def test_stale_budget_recovery_cannot_reset_running_lease_or_resolve_issues(self):
        task, evidence = self._context_blocked_component()
        other = DVProject(self.root)
        running = []

        def claimed_before_recovery():
            other.store.requeue(task["id"])
            other.store.claim(task["id"], "other-connection")
            running.append(other.store.get_task(task["id"]))

        issues = self.project.store.list_issues()
        current, prompt, receipt = generation._prepare_dispatch_context(
            self.project, task, evidence["prompt"], {}, 64000, evidence["agent"],
            evidence["output_schema"], 64000, claimed_before_recovery)
        self.assertIsNone(prompt)
        self.assertIsNone(receipt)
        self.assertEqual(running[0], current)
        self.assertEqual(running[0], other.store.get_task(task["id"]))
        self.assertEqual(issues, other.store.list_issues())
        self.assertEqual([], other.store.list_issues(status="resolved"))

    def test_stale_budget_recovery_cannot_clear_a_new_blocker_revision(self):
        task, evidence = self._context_blocked_component()
        other = DVProject(self.root)
        issues = self.project.store.list_issues()
        current, prompt, _ = generation._prepare_dispatch_context(
            self.project, task, evidence["prompt"], {}, 64000, evidence["agent"],
            evidence["output_schema"], 64000,
            lambda: other.store.block(task["id"], "A separately discovered contract is missing"))
        self.assertIsNone(prompt)
        self.assertEqual("blocked", current["status"])
        self.assertEqual("A separately discovered contract is missing", current["reason"])
        self.assertEqual(issues, other.store.list_issues())

    def test_pending_retry_resolves_same_input_context_issues_after_admission(self):
        task, _ = self._context_blocked_component()
        pending = self.project.store.requeue(task["id"])
        self.assertEqual(1, len(self.project.store.list_issues()))
        adapter = CandidateRuntime()
        self.generate(adapter, context_chars=64000, max_components=1)
        self.assertEqual("done", self.project.store.get_task(task["id"])["status"])
        self.assertEqual([], self.project.store.list_issues())
        resolved = self.project.store.list_issues(status="resolved")
        self.assertEqual(1, len(resolved))
        self.assertIsNone(json.loads(resolved[0]["resolution"])["context_chars"])
        requeued = [event for event in self.project.store.events(limit=1000)
                    if event["type"] == "task_requeued" and event["task_id"] == task["id"]]
        self.assertEqual(1, len(requeued), "Budget admission must not requeue an already pending retry")
        self.assertGreater(self.project.store.get_task(task["id"])["revision"], pending["revision"])

    def test_context_preparation_does_not_change_another_sessions_lease(self):
        self.generate(CandidateRuntime(), max_components=0)
        task = next(task for task in self.project.store.ready_tasks(100) if task["kind"] == "implement")
        attempt = self.project.store.claim(task["id"], "concurrent-connection")
        running = self.project.store.get_task(task["id"])
        current, prompt, receipt = generation._prepare_dispatch_context(self.project, running,
            "Complete context " * 10000, {}, None, "host", {}, None, lambda: None)
        self.assertEqual(running, current)
        self.assertIsNone(prompt)
        self.assertIsNone(receipt)
        self.assertEqual(attempt["id"], self.project.store.get_task(task["id"])["attempt_id"])
        self.assertEqual([], self.project.store.list_issues())

    def test_context_preparation_never_creates_a_length_hold(self):
        self.generate(CandidateRuntime(), max_components=0)
        task = next(task for task in self.project.store.ready_tasks(100) if task["kind"] == "implement")
        with patch.object(self.project.store, "block_context", side_effect=AssertionError("No new length hold")):
            current, prompt, receipt = generation._prepare_dispatch_context(self.project, task,
                "Complete context " * 10000, {}, 1, "host", {}, None, lambda: None)
        self.assertEqual(task, current)
        self.assertIn("Complete context " * 10000, prompt)
        self.assertIsNone(receipt)
        self.assertEqual([], self.project.store.list_issues())

    def test_pi_schema_is_measured_without_a_runtime_limit(self):
        adapter = CandidateRuntime()
        with patch("loopx.capabilities.chip_verification.engine.generation.runtime", return_value=adapter) as factory:
            result = run_generation(self.project, [self.rtl], "dut", agent="pi", context_chars=18000, max_components=1)
        self.assertIsNone(result["context_chars"])
        self.assertEqual(2, len(adapter.calls))
        for invocation, call in zip(factory.call_args_list, adapter.calls):
            self.assertIsNone(invocation.kwargs["max_prompt_chars"])
            extra = len("\nReturn only JSON conforming to this schema:\n" + json.dumps(call["schema"]))
            path = self.root / ".loopx" / "chip-verification" / "attempts" / call["attempt_id"] / "generation-context.json"
            recorded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(extra, recorded["schema_chars"])
            self.assertEqual(len(call["prompt"]) + extra, recorded["required_chars"])
            self.assertIsNone(recorded["context_chars"])

    def test_invalid_execution_budget_is_rejected_without_mutating_project(self):
        original = self.project.config_path.read_bytes()
        with patch("loopx.capabilities.chip_verification.engine.generation.runtime") as adapter:
            for budget in (0, -1, True, 3.5):
                with self.subTest(budget=budget), self.assertRaises(ValueError):
                    run_generation(self.project, [self.rtl], "dut", context_chars=budget)
            adapter.assert_not_called()
        self.assertEqual(original, self.project.config_path.read_bytes())

    def test_runtime_contexts_distinguish_dut_from_xsim_testbench_top(self):
        adapter = CandidateRuntime()
        self.generate(adapter, max_components=1)
        self.assertEqual(2, len(adapter.calls))
        for call in adapter.calls:
            contract = call["context"]["build_manifest_contract"]
            self.assertEqual("dut", contract["dut_top"])
            self.assertEqual("vivado_xsim", contract["simulator"])
            self.assertEqual("tb_top", contract["template"]["top"])
            self.assertIn(".json", contract["output_suffixes"])
            self.assertNotIn(".do", contract["output_suffixes"])
            self.assertEqual([str(self.rtl)], contract["template"]["rtl_files"])
        self.assertIn("DUT module identifier", adapter.calls[0]["schema"]["properties"]["top"]["description"])

    def test_late_bound_manifest_example_matches_real_backend_and_observed_activity_contract(self):
        from loopx.capabilities.chip_verification.engine.generation import _build_manifest_contract
        from loopx.capabilities.chip_verification.engine.simulators.generic import (
            ManifestError, load_build_manifest, parse_smoke_log,
        )
        from loopx.capabilities.chip_verification.engine.simulators.xsim import SimulationError

        contract = _build_manifest_contract({"top": "dut", "rtl": [],
                                             "shared_specification": {"task_id": "fixture-contract"}}, None)
        self.assertEqual([], contract["template"]["rtl_files"])
        self.assertIn("not a runnable build", contract["rtl_binding"])
        self.assertIn("never create a fake DUT", contract["rtl_binding"])
        candidate = self.root / "manifest-example"
        candidate.mkdir()
        (candidate / "tb_top.sv").write_text("module tb_top; dut dut_instance(); endmodule\n", encoding="utf-8")
        manifest = candidate / "build.json"
        value = copy.deepcopy(contract["template"])
        save_json(manifest, value)
        with self.assertRaisesRegex(ManifestError, "rtl_files"):
            load_build_manifest(manifest)
        # Emulate only the join's path binding, without claiming simulation.
        value["rtl_files"] = [str(self.rtl)]
        value["compile_order"].insert(0, str(self.rtl))
        save_json(manifest, value)
        parsed = load_build_manifest(manifest)
        self.assertEqual("tb_top", parsed["top"])
        self.assertEqual([str(self.rtl)], parsed["rtl_files"])
        self.assertEqual("smoke_test", parsed["testname"])
        log = "UVM Report Summary\nUVM_ERROR : 0\nUVM_FATAL : 0\nLOOPX_ACTIVITY checks=7 transactions=4\n"
        observed = parse_smoke_log(log, parsed["activity_contract"])
        self.assertEqual({"checks": 7, "transactions": 4}, observed["activity"]["counters"])
        with self.assertRaises(SimulationError):
            parse_smoke_log(log.replace("transactions=4", "transactions=0"), parsed["activity_contract"])

    def test_execution_target_conflict_is_reported_without_overriding_sourced_profile(self):
        profile_path, profile, _ = self.make_profile()
        profile["testbench_choices"].append({"id": "execution_target", "statement": "Use the supplied Questa simulator choice",
            "rationale": "Explicit profile runtime selection", "source_refs": [], "value": {"simulator": "questa"}})
        save_json(profile_path, profile)
        original = profile_path.read_bytes()
        adapter = CandidateRuntime()
        with self.assertRaisesRegex(ValidationError, "unsupported simulator"):
            self.generate(adapter, profile_path=profile_path)
        self.assertEqual([], adapter.calls)
        self.assertEqual(original, profile_path.read_bytes())

    def test_path_error_identifies_unsupported_script_without_unbounded_echo(self):
        from loopx.capabilities.chip_verification.engine.generation import _safe_path

        with self.assertRaises(ValidationError) as raised:
            _safe_path("scripts/questa.do")
        self.assertIn("scripts/questa.do", str(raised.exception))
        self.assertIn("allowed suffixes", str(raised.exception))
        self.assertIn(".json", str(raised.exception))
        with self.assertRaises(ValidationError) as long_error:
            _safe_path("x" * 10000 + ".do")
        self.assertLess(len(str(long_error.exception)), 400)


if __name__ == "__main__":
    unittest.main()
