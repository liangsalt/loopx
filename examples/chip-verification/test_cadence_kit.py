"""Portable file/dispatch contracts, not Cadence or hardware qualification."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from cadence_case_kit import stage_execution_kit, validate_execution_kit


spec = importlib.util.spec_from_file_location("cadence_runner_under_test", Path(__file__).with_name("cadence") / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def candidate(tmp_path):
    (tmp_path / "rtl").mkdir()
    (tmp_path / "tb").mkdir()
    (tmp_path / "rtl/types.svh").write_text("typedef logic [31:0] word_t;\n", encoding="utf-8")
    (tmp_path / "rtl/dut.sv").write_text('`include "types.svh"\nmodule dut(input logic clk_i); endmodule\n', encoding="utf-8")
    (tmp_path / "tb/tb_top.sv").write_text('`include "uvm_macros.svh"\nmodule tb_top; endmodule\n', encoding="utf-8")
    write_json(tmp_path / "coverage.json", {"schema_version": 1, "metric": "functional", "target_percent": 90,
               "bins": [{"id": "reset_during_backpressure", "requirement_ids": ["R-reset"]}]})
    value = {"schema_version": 1, "simulator": "cadence_xcelium", "execution": "external",
             "candidate_dir": ".", "dut_top": "dut", "top": "tb_top", "testname": "exercise_test",
             "rtl_files": ["rtl/dut.sv"], "tb_files": ["tb/tb_top.sv"],
             "compile_order": ["rtl/dut.sv", "tb/tb_top.sv"], "include_dirs": ["rtl"], "defines": [],
             "seed": 7, "timescale": "1ns/1ps", "coverage_model": "coverage.json"}
    path = tmp_path / "build.json"
    write_json(path, value)
    return path, value


def test_complete_file_inventory_is_not_elaboration_or_coverage(candidate, tmp_path):
    path, _ = candidate
    result = runner.inspect(path, tmp_path)
    assert set(result["inputs"]) == {"rtl/dut.sv", "rtl/types.svh", "tb/tb_top.sv", "coverage.json"}
    assert result["ready"] and not result["hdl_elaborated"] and not result["coverage_verified"]
    old = result["input_digest"]
    (tmp_path / "rtl/types.svh").write_text("typedef logic [63:0] word_t;", encoding="utf-8")
    assert runner.inspect(path, tmp_path)["input_digest"] != old


@pytest.mark.parametrize("change,message", [
    ({"simulator": "vivado_xsim"}, "no simulator fallback"),
    ({"rtl_files": []}, "not yet supplied"),
    ({"compile_order": ["tb/tb_top.sv"]}, "every RTL/TB"),
    ({"defines": ["UVM_NO_DPI"]}, "native UVM"),
    ({"extra_inputs": ["../outside.txt"]}, "escapes"),
    ({"candidate_dir": ".."}, "escapes execution"),
    ({"seed": True}, "Seed"),
])
def test_invalid_or_foreign_contracts_rejected(candidate, tmp_path, change, message):
    path, value = candidate
    write_json(path, {**value, **change})
    with pytest.raises(ValueError, match=message):
        runner.inspect(path, tmp_path)


def test_missing_include_and_unlisted_memory_are_not_hidden(candidate, tmp_path):
    path, _ = candidate
    source = tmp_path / "rtl/dut.sv"
    source.write_text('`include "unshipped.svh"\nmodule dut; endmodule', encoding="utf-8")
    with pytest.raises(ValueError, match="transitive include"):
        runner.inspect(path, tmp_path)
    source.write_text('module dut; initial $readmemh("program.hex", memory); endmodule', encoding="utf-8")
    with pytest.raises(ValueError, match="extra_inputs"):
        runner.inspect(path, tmp_path)
    source.write_text('import "DPI-C" function int upstream_model();', encoding="utf-8")
    with pytest.raises(ValueError, match="DPI"):
        runner.inspect(path, tmp_path)


def test_question_inputs_are_frozen_and_historical_sources_cannot_be_compiled(candidate, tmp_path):
    path, value = candidate
    contract = tmp_path / "cadence_contract.md"
    contract.write_text("Require ordered retirement", encoding="utf-8")
    before = runner.inspect(path, tmp_path)
    contract.write_text("Require new reset behavior", encoding="utf-8")
    after = runner.inspect(path, tmp_path)
    assert before["input_digest"] != after["input_digest"]
    write_json(tmp_path / "integration_profile.json", {"forbidden_execution_roots": ["rtl"]})
    with pytest.raises(ValueError, match="Historical/reference"):
        runner.inspect(path, tmp_path)


def test_native_plan_is_explicit_and_rejects_reuse(candidate, tmp_path):
    path, _ = candidate
    output = tmp_path / "run with spaces"
    plan = runner.make_plan(path, tmp_path, output)
    assert plan["command"][:6] == ["xrun", "-64bit", "-sv", "-uvm", "-uvmhome", "CDNS-1.2"]
    assert plan["command"][-3:] == ["-coverage", "A", "+UVM_TESTNAME=exercise_test"]
    assert not plan["tool_execution_performed"] and not plan["coverage_verified"]
    assert '"' in (output / "files.f").read_text()
    assert "tb_top" not in (output / "rtl_files.f").read_text()
    with pytest.raises(ValueError, match="must be new"):
        runner.make_plan(path, tmp_path, output)


def test_site_stage_requires_native_recipe_and_flags(candidate, tmp_path):
    path, value = candidate
    result = runner.make_plan(path, tmp_path, tmp_path / "cdc-missing", "cdc")
    assert not result["ready"] and result["status"] == "site_recipe_required"
    assert not (tmp_path / "cdc-missing").exists()
    (tmp_path / "cdc.tcl").write_text('# Site-owned recipe fixture; no actual CDC run\n', encoding="utf-8")
    write_json(path, {**value, "scripts": {"cdc": "cdc.tcl"}})
    site = tmp_path / "site.json"
    write_json(site, {"launch_flags": {"cdc": ["-batch", "{script}"]}})
    plan = runner.make_plan(path, tmp_path, tmp_path / "cdc", "cdc", site)
    assert plan["command"] == ["jg", "-batch", str(tmp_path / "cdc.tcl")]
    assert "cdc.tcl" in plan["inputs"]
    assert not plan["tool_execution_performed"]


@pytest.mark.parametrize("text,clean", [
    ("coverage 99.9%", False),
    ("UVM_ERROR : 0\nUVM_FATAL : 0", False),
    ("UVM_ERROR : 0\nUVM_FATAL : 0\nLOOPX_ACTIVITY checks=0 transactions=5", False),
    ("UVM_ERROR : 1\nUVM_FATAL : 0\nLOOPX_ACTIVITY checks=5 transactions=5", False),
    ("UVM_ERROR : 0\nUVM_FATAL : 0\nLOOPX_ACTIVITY checks=5 transactions=5", True),
    ("xmelab: *E,FAIL: unresolved\nUVM_ERROR : 0\nUVM_FATAL : 0\nLOOPX_ACTIVITY checks=5 transactions=5", False),
])
def test_process_exit_or_percentage_is_not_acceptance(text, clean):
    result = runner.smoke_observation(text)
    assert result["clean_uvm_report_and_activity_observed"] == clean
    assert not result["coverage_verified"] and not result["checker_correctness_verified"]


def test_record_real_fixture_subprocess_without_claiming_native_acceptance(candidate, tmp_path):
    path, _ = candidate
    output = tmp_path / "fixture-run"
    plan = runner.make_plan(path, tmp_path, output)
    # This is deliberately a Python fixture, not a fake claim of installed xrun.
    plan["command"] = [sys.executable, "-c", "print('UVM_ERROR : 0\\nUVM_FATAL : 0\\nLOOPX_ACTIVITY checks=3 transactions=4')"]
    plan["version_command"] = [sys.executable, "--version"]
    receipt = runner.execute(plan, timeout=20)
    assert receipt["status"] == "tool_completed" and receipt["returncode"] == 0
    assert not receipt["task_acceptance"] and not receipt["coverage_verified"]
    assert receipt["coverage_percent"] is None
    assert {a["path"] for a in receipt["artifacts"]} >= {"tool-version.txt", "tool.log", "files.f", "inputs.json"}
    with pytest.raises(ValueError, match="already has a receipt"):
        runner.execute(plan)


def test_changed_input_cannot_start_a_planned_process(candidate, tmp_path):
    path, _ = candidate
    plan = runner.make_plan(path, tmp_path, tmp_path / "changed")
    (tmp_path / "rtl/dut.sv").write_text("module changed; endmodule", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after planning"):
        runner.execute(plan)


def test_kit_generation_profile_and_integrity(tmp_path):
    contract = tmp_path / "cadence_contract.md"
    contract.write_text("# Independent digital subsystem\nUse Cadence and ordinary SV.\n", encoding="utf-8")
    profile = {"case_id": "fixture", "root_module": "fixture_subsystem"}
    write_json(tmp_path / "integration_profile.json", profile)
    report = stage_execution_kit(tmp_path, profile)
    assert len(validate_execution_kit(tmp_path)) == 5
    assert not report["native_tool_execution_performed"]
    generation = json.loads((tmp_path / "eda/verification_profile.json").read_text())
    assert generation["top"] == profile["root_module"]
    assert generation["sources"][0]["sha256"] == runner.sha(contract.read_bytes())
    assert generation["testbench_choices"][0]["value"]["simulator"] == "cadence_xcelium"
    result = subprocess.run([sys.executable, str(tmp_path / "eda/cadence_runner.py"), "inspect",
                             "--project", str(tmp_path / "eda/project.example.json"), "--workspace", str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 2 and "not yet supplied" in result.stdout
    (tmp_path / "eda/cadence_runner.py").write_text("print('changed')", encoding="utf-8")
    with pytest.raises(ValueError, match="content changed"):
        validate_execution_kit(tmp_path)


def test_coverage_denominator_cannot_be_empty_or_duplicate(candidate, tmp_path):
    path, _ = candidate
    coverage = tmp_path / "coverage.json"
    for bins in ([], [{"id": "same", "requirement_ids": ["R"]}] * 2):
        write_json(coverage, {"schema_version": 1, "metric": "functional", "target_percent": 90, "bins": bins})
        with pytest.raises(ValueError):
            runner.inspect(path, tmp_path)


@pytest.mark.parametrize("stage", ["lint", "cdc", "formal", "synthesis"])
def test_design_stages_run_before_dv_exists(candidate, tmp_path, stage):
    path, value = candidate
    # A shared manifest may already declare planned TB paths; design checks
    # must neither open those paths nor put them on the design file list.
    (tmp_path / "tb/tb_top.sv").unlink()
    (tmp_path / "coverage.json").unlink()
    for key in ("top", "testname", "seed", "timescale"):
        value.pop(key)
    (tmp_path / "stage.tcl").write_text("# Target installation recipe fixture\n", encoding="utf-8")
    value["scripts"] = {stage: "stage.tcl"}
    write_json(path, value)
    site = tmp_path / "site.json"
    write_json(site, {"launch_flags": {stage: ["-batch", "{script}"]}})
    output = tmp_path / "design-run"
    plan = runner.make_plan(path, tmp_path, output, stage, site)
    assert plan["validation_scope"] == "rtl_and_declared_constraints"
    assert "tb_files" in plan["unvalidated_fields"] and "coverage_model" in plan["unvalidated_fields"]
    assert "tb/tb_top.sv" not in plan["inputs"] and "coverage.json" not in plan["inputs"]
    assert "LOOPX_COVERAGE_MODEL" not in plan["environment"]
    assert (output / "files.f").read_bytes() == (output / "rtl_files.f").read_bytes()
    plan["command"] = [sys.executable, "-c", "print('Design stage fixture only')"]
    plan["version_command"] = [sys.executable, "--version"]
    result = runner.execute(plan, timeout=20)
    assert result["status"] == "tool_completed" and not result["task_acceptance"]
    assert result["validation_scope"] == "rtl_and_declared_constraints"
    for verification_stage in ("simulation", "coverage"):
        with pytest.raises(ValueError):
            runner.inspect(path, tmp_path, verification_stage)


def test_rtl_only_manifest_can_prepare_design_recipe(candidate, tmp_path):
    path, value = candidate
    for key in ("tb_files", "top", "testname", "seed", "timescale", "coverage_model"):
        value.pop(key)
    value["compile_order"] = value["rtl_files"]
    write_json(path, value)
    result = runner.inspect(path, tmp_path, "cdc")
    assert result["functional_bin_count"] is None and result["compile_order"] == ["rtl/dut.sv"]
    assert runner.make_plan(path, tmp_path, tmp_path / "missing-recipe", "cdc")["status"] == "site_recipe_required"


def test_generated_cadence_manifest_is_directly_consumable(candidate, tmp_path):
    from loopx.capabilities.chip_verification.engine.generation import _build_manifest_contract

    path, _ = candidate
    contract = _build_manifest_contract({"top": "dut", "rtl": [{"path": "rtl/dut.sv"}]},
        {"testbench_choices": [{"id": "execution", "value": {"simulator": "cadence_xcelium"}}]})
    value = contract["template"]
    (tmp_path / "tb_top.sv").write_bytes((tmp_path / "tb/tb_top.sv").read_bytes())
    write_json(path, value)
    # The generated coverage model is a required artifact, not implicit credit.
    with pytest.raises(ValueError, match="Missing input: coverage_model.json"):
        runner.inspect(path, tmp_path)
    (tmp_path / "coverage_model.json").write_bytes((tmp_path / "coverage.json").read_bytes())
    observed = runner.inspect(path, tmp_path)
    assert observed["ready"] and observed["activity_contract"] == value["activity_contract"]
    assert not observed["hdl_elaborated"] and not observed["coverage_verified"]
    plan = runner.make_plan(path, tmp_path, tmp_path / "generated-plan")
    assert plan["tool"] == "xrun" and plan["validation_scope"] == "rtl_tb_coverage"


def memory_candidate(candidate, tmp_path, name="data/program.hex"):
    path, value = candidate
    (tmp_path / "rtl/dut.sv").write_text('module dut; logic [7:0] m[1]; initial $readmemh("' + name + '", m); endmodule', encoding="utf-8")
    data = tmp_path / name
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(b"ab\n")
    (tmp_path / "unused.txt").write_text("Not a runtime memory input", encoding="utf-8")
    value["extra_inputs"] = [name, "unused.txt"]
    write_json(path, value)
    return path, value


def test_runtime_memory_is_available_to_real_process_in_run_directory(candidate, tmp_path):
    path, _ = memory_candidate(candidate, tmp_path)
    output = tmp_path / "memory-run"
    plan = runner.make_plan(path, tmp_path, output)
    assert (output / "data/program.hex").read_text() == "ab\n"
    assert not (output / "unused.txt").exists()
    assert plan["runtime_inputs"] == {"data/program.hex": {"sha256": runner.sha(b"ab\n"), "bytes": 3}}
    plan["command"] = [sys.executable, "-c", "from pathlib import Path; assert Path('data/program.hex').read_bytes() == b'ab\\n'; print('UVM_ERROR : 0\\nUVM_FATAL : 0\\nLOOPX_ACTIVITY checks=2 transactions=3')"]
    plan["version_command"] = [sys.executable, "--version"]
    result = runner.execute(plan, timeout=20)
    assert result["status"] == "tool_completed" and result["inputs_unchanged"]
    assert any(item["path"] == "data/program.hex" for item in result["artifacts"])
    assert not result["coverage_verified"]


@pytest.mark.parametrize("name", ["plan.json", "tool.log", "files.f/nested.hex", "xrun.log", "cov_work/program.hex"])
def test_runtime_memory_cannot_overwrite_runner_or_native_output(candidate, tmp_path, name):
    path, _ = memory_candidate(candidate, tmp_path, name)
    output = tmp_path / "collision-run"
    with pytest.raises(ValueError, match="collides"):
        runner.make_plan(path, tmp_path, output)
    assert not output.exists()


def test_changed_staged_memory_prevents_execution(candidate, tmp_path):
    path, _ = memory_candidate(candidate, tmp_path)
    output = tmp_path / "memory-run"
    plan = runner.make_plan(path, tmp_path, output)
    (output / "data/program.hex").write_text("00\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Staged runtime memory input changed"):
        runner.execute(plan)
    assert not (output / "tool-version.txt").exists()


@pytest.mark.parametrize("kind", ["include", "extra_inputs", "constraints", "script", "coverage_model"])
def test_forbidden_sources_apply_to_entire_dependency_inventory(candidate, tmp_path, kind):
    path, value = candidate
    directory = tmp_path / "historical"
    directory.mkdir()
    write_json(tmp_path / "integration_profile.json", {"forbidden_execution_roots": ["historical"]})
    (directory / "old.svh").write_text("typedef logic old_t;", encoding="utf-8")
    if kind == "include":
        (tmp_path / "rtl/dut.sv").write_text('`include "../historical/old.svh"\nmodule dut; endmodule', encoding="utf-8")
    elif kind == "script":
        (directory / "stage.tcl").write_text("# forbidden script", encoding="utf-8")
        value["scripts"] = {"formal": "historical/stage.tcl"}
    elif kind == "coverage_model":
        (directory / "coverage.json").write_bytes((tmp_path / "coverage.json").read_bytes())
        value[kind] = "historical/coverage.json"
    else:
        value[kind] = ["historical/old.svh"]
    write_json(path, value)
    with pytest.raises(ValueError, match="Historical/reference"):
        runner.inspect(path, tmp_path)


def test_comments_and_ordinary_uvm_package_do_not_create_false_dependencies(candidate, tmp_path):
    path, _ = candidate
    (tmp_path / "tb/tb_top.sv").write_text('''// import "DPI-C" is mentioned in provenance only.
/* `include "absent.svh"
$readmemh("unshipped.hex", memory);
export "DPI-C" function old_entry;
*/
package example_pkg;
  import uvm_pkg::*;
  `include "uvm_macros.svh"
  class example_test extends uvm_test;
    `uvm_component_utils(example_test)
    string url = "https://example.test/with/*literal*/text";
    string quote_text = "escaped \\" // still in string";
  endclass
endpackage
module tb_top; endmodule
''', encoding="utf-8")
    result = runner.inspect(path, tmp_path)
    assert result["ready"] and result["runtime_inputs"] == {}
    stripped = runner.source_without_comments('string s = "http://x/*y*/"; /* remove */\n')
    assert '"http://x/*y*/"' in stripped and "remove" not in stripped
    # Removing comments must not hide a real declaration around them.
    (tmp_path / "tb/tb_top.sv").write_text('import /* explanation */ "DPI-C" function int real_dpi();', encoding="utf-8")
    with pytest.raises(ValueError, match="DPI"):
        runner.inspect(path, tmp_path)


@pytest.mark.parametrize("contract", [
    {"pattern": "arbitrary success", "minimums": {"checks": 1, "transactions": 1}},
    {"pattern": runner.ACTIVITY_PATTERN, "minimums": {"checks": 0, "transactions": 1}},
    {"pattern": runner.ACTIVITY_PATTERN, "minimums": {"checks": True, "transactions": 1}},
    {"pattern": runner.ACTIVITY_PATTERN, "minimums": {"checks": 10}},
])
def test_unsupported_activity_contract_is_not_silently_ignored(candidate, tmp_path, contract):
    path, value = candidate
    write_json(path, {**value, "activity_contract": contract})
    with pytest.raises(ValueError, match="activity_contract|Activity minimums"):
        runner.inspect(path, tmp_path)


def test_activity_minimums_use_final_counters_and_reach_execute(candidate, tmp_path):
    path, value = candidate
    contract = {"pattern": runner.ACTIVITY_PATTERN, "minimums": {"checks": 100, "transactions": 5}}
    write_json(path, {**value, "activity_contract": contract})
    log = "UVM_ERROR : 0\nUVM_FATAL : 0\nLOOPX_ACTIVITY checks=100 transactions=5\n"
    assert runner.smoke_observation(log, contract)["clean_uvm_report_and_activity_observed"]
    final_reset = runner.smoke_observation(log + "LOOPX_ACTIVITY checks=1 transactions=1\n", contract)
    assert final_reset["activity_counters"] == {"checks": 1, "transactions": 1}
    assert not final_reset["clean_uvm_report_and_activity_observed"]
    output = tmp_path / "activity-run"
    plan = runner.make_plan(path, tmp_path, output)
    plan["command"] = [sys.executable, "-c", "print('UVM_ERROR : 0\\nUVM_FATAL : 0\\nLOOPX_ACTIVITY checks=1 transactions=1')"]
    plan["version_command"] = [sys.executable, "--version"]
    result = runner.execute(plan, timeout=20)
    assert result["status"] == "simulation_evidence_incomplete"
    assert not result["observation"]["activity_minimums_satisfied"] and not result["task_acceptance"]


@pytest.mark.parametrize("group_gone", [False, True])
def test_posix_timeout_escalates_before_reaping_leader(monkeypatch, group_gone):
    calls = []

    class OwnedProcess:
        pid = 12345

        def wait(self):
            calls.append("reap")

    def killpg(group, sig):
        assert group == OwnedProcess.pid
        calls.append(sig)
        if group_gone and sig == signal.SIGKILL:
            raise ProcessLookupError

    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(runner.os, "getpgrp", lambda: 54321, raising=False)
    monkeypatch.setattr(runner.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(runner.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(runner.time, "sleep", lambda grace: calls.append(("grace", grace)))
    runner.terminate_owned_posix_group(OwnedProcess(), grace_seconds=0.1)
    assert calls == [signal.SIGTERM, ("grace", 0.1), signal.SIGKILL, "reap"]


def test_timeout_does_not_signal_unowned_process_group(monkeypatch):
    class UnownedProcess:
        pid = 12345

    monkeypatch.setattr(runner.os, "getpgrp", lambda: 54321, raising=False)
    monkeypatch.setattr(runner.os, "getpgid", lambda pid: 54321, raising=False)
    monkeypatch.setattr(runner.os, "killpg", lambda *args: pytest.fail("Unowned group signalled"), raising=False)
    with pytest.raises(ValueError, match="not owned"):
        runner.terminate_owned_posix_group(UnownedProcess(), grace_seconds=0)


@pytest.mark.skipif(os.name != "posix", reason="Requires real POSIX sessions and process-group signals")
def test_timeout_stops_child_that_ignores_term_after_parent_exits(candidate, tmp_path):
    path, _ = candidate
    output = tmp_path / "child-tree"
    plan = runner.make_plan(path, tmp_path, output)
    child = """import signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
count = 0
while True:
    count += 1
    Path('child-heartbeat.txt').write_text(str(count))
    time.sleep(0.02)
"""
    parent = ("import os, subprocess, sys, time; from pathlib import Path; "
              "Path('group.txt').write_text(str(os.getpid())); "
              "subprocess.Popen([sys.executable, '-c', " + repr(child) + "]); time.sleep(300)")
    plan["command"] = [sys.executable, "-c", parent]
    plan["version_command"] = [sys.executable, "--version"]
    try:
        result = runner.execute(plan, timeout=2)
        assert result["status"] == "timeout" and not result["task_acceptance"]
        heartbeat = output / "child-heartbeat.txt"
        before = heartbeat.read_text()
        time.sleep(0.15)
        assert heartbeat.read_text() == before, "Term-resistant child survived the tool timeout"
    finally:
        # Only clean a still-running fixture group; normal execution already
        # killed it. The unique fixture heartbeat distinguishes a live leak.
        heartbeat = output / "child-heartbeat.txt"
        if heartbeat.exists() and (output / "group.txt").exists():
            before = heartbeat.read_text()
            time.sleep(0.1)
            if heartbeat.read_text() != before:
                try:
                    os.killpg(int((output / "group.txt").read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
