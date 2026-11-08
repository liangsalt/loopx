"""Portable Cadence execution records; no upstream build system or coverage claims.

This file is copied into an input-only exercise as eda/cadence_runner.py.
Only `run --execute` starts a process. Python's standard library is sufficient.
Native reports remain evidence for review, not automatic task/signoff acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid


STAGES = {"simulation": "xrun", "coverage": "imc", "lint": "jg", "cdc": "jg",
          "formal": "jg", "synthesis": "genus"}
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
ACTIVITY_PATTERN = r"LOOPX_ACTIVITY checks=(?P<checks>[0-9]+) transactions=(?P<transactions>[0-9]+)"
RUN_FILES = {"files.f", "rtl_files.f", "inputs.json", "plan.json", "receipt.json", "tool.log", "tool-version.txt"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "Duplicate JSON key: " + key)
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique)


def inside(root, value, *, directory=False):
    require(isinstance(value, str) and bool(value) and not any(c in value for c in '\x00\r\n"'),
            "Input paths must be nonempty and contain no control/quote characters")
    path = root / value
    resolved = path.resolve()
    require(resolved.is_relative_to(root.resolve()), "Input escapes candidate directory: " + value)
    for parent in (path, *path.parents):
        require(not parent.is_symlink() and not getattr(parent, "is_junction", lambda: False)(),
                "Linked input is not a portable dependency: " + value)
        if parent == root:
            break
    require(resolved.is_dir() if directory else resolved.is_file(), "Missing input: " + value)
    return resolved


def source_without_comments(text):
    """Remove SV comments, preserving strings, escapes and line boundaries."""
    result, offset = [], 0
    while offset < len(text):
        if text[offset] == '"':
            start = offset
            offset += 1
            while offset < len(text):
                if text[offset] == "\\":
                    offset += 2
                elif text[offset] == '"':
                    offset += 1
                    break
                else:
                    offset += 1
            result.append(text[start:offset])
        elif text.startswith("//", offset) or text.startswith("/*", offset):
            start = offset
            if text.startswith("//", offset):
                stop = text.find("\n", offset + 2)
                offset = len(text) if stop < 0 else stop
            else:
                stop = text.find("*/", offset + 2)
                require(stop >= 0, "Unterminated SystemVerilog block comment")
                offset = stop + 2
            result.append("".join("\n" if c == "\n" else " " for c in text[start:offset]))
        else:
            result.append(text[offset])
            offset += 1
    return "".join(result)


def activity_contract(value=None):
    if value is None:
        return {"pattern": ACTIVITY_PATTERN, "minimums": {"checks": 1, "transactions": 1}}
    require(isinstance(value, dict) and set(value) == {"pattern", "minimums"}, "Invalid activity_contract fields")
    require(value["pattern"] == ACTIVITY_PATTERN, "Unsupported activity_contract pattern; use the documented LOOPX_ACTIVITY pattern")
    minimums = value["minimums"]
    require(isinstance(minimums, dict) and set(minimums) == {"checks", "transactions"}
            and all(type(n) is int and n > 0 for n in minimums.values()),
            "Activity minimums require positive integer checks and transactions")
    return value


def runtime_name(name):
    require(isinstance(name, str) and name and not any(c in name for c in '\\:\x00\r\n"')
            and all(part not in {"", ".", ".."} for part in name.split("/")),
            "Runtime memory input must use a portable relative path without traversal")
    first = name.split("/")[0].lower()
    require(first not in RUN_FILES | {"xcelium.d", "inca_libs", "cov_work", "waves.shm"}
            and not first.startswith(("xrun.", "xcelium.")), "Runtime memory input collides with run outputs: " + name)
    return name


def inspect(project_path, workspace=None, stage="simulation"):
    require(stage in STAGES, "Unknown Cadence stage")
    verification = stage in {"simulation", "coverage"}
    project_path = Path(project_path).resolve()
    workspace = Path(workspace or Path.cwd()).resolve()
    require(project_path.is_relative_to(workspace), "Project manifest must be inside the execution workspace")
    config = read_json(project_path)
    require(config.get("schema_version") == 1 and config.get("simulator") == "cadence_xcelium",
            "Expected an explicit schema 1 cadence_xcelium manifest; no simulator fallback")
    require(config.get("execution") == "external", "Cadence execution must be explicitly external")
    root = (project_path.parent / config.get("candidate_dir", ".")).resolve()
    require(root.is_relative_to(workspace), "Candidate directory escapes execution workspace")
    for key in (("top", "dut_top", "testname") if verification else ("dut_top",)):
        require(isinstance(config.get(key), str) and IDENTIFIER.fullmatch(config[key]), "Invalid or missing " + key)
    if verification:
        require(type(config.get("seed")) is int and 0 <= config["seed"] < 2**31, "Seed must be explicit and reproducible")
        require(config.get("runtime", "all") == "all", "Finish the UVM test through its own watchdog; runtime must be all")
        require(isinstance(config.get("timescale"), str) and re.fullmatch(r"[1-9][0-9]*(?:s|ms|us|ns|ps|fs)/[1-9][0-9]*(?:s|ms|us|ns|ps|fs)", config["timescale"]),
                "Provide an explicit SystemVerilog timescale")
    contract = activity_contract(config.get("activity_contract")) if verification else None
    rtl, tb, order = config.get("rtl_files"), config.get("tb_files", []), config.get("compile_order")
    require(isinstance(rtl, list) and rtl and isinstance(tb, list) and (tb or not verification),
            "Candidate RTL" + (" and UVM" if verification else "") + " files are not yet supplied; an exercise package is not a solved design")
    require(all(isinstance(name, str) for name in rtl + tb), "Translation units must be paths")
    require(len(set(rtl + tb)) == len(rtl + tb), "Duplicate RTL/TB translation unit")
    require(isinstance(order, list) and all(isinstance(p, str) for p in order)
            and len(order) == len(rtl + tb) and set(order) == set(rtl + tb),
            "compile_order must list every RTL/TB translation unit exactly once")
    selected_order = order if verification else [name for name in order if name in rtl]
    paths = [inside(root, name) for name in selected_order]
    exercise = read_json(workspace / "integration_profile.json") if (workspace / "integration_profile.json").is_file() else {}
    forbidden = exercise.get("forbidden_execution_roots", [])
    require(isinstance(forbidden, list) and all(isinstance(name, str) for name in forbidden), "Invalid exercise execution roots")
    require(all(p.suffix in {".sv", ".v"} for p in paths),
            "Cadence exercises compile ordinary Verilog/SystemVerilog only; headers belong in include_dirs")
    require(isinstance(config.get("include_dirs", []), list), "include_dirs must be a list")
    directories = [inside(root, name, directory=True) for name in config.get("include_dirs", [])]
    defines = config.get("defines", [])
    require(isinstance(defines, list) and all(isinstance(d, str) and re.fullmatch(r"[A-Za-z_]\w*(?:=[A-Za-z0-9_'hbxHXB.+-]+)?", d) for d in defines),
            "Defines must be explicit macro names/values, not simulator command fragments")
    require(config.get("uvm_no_dpi") in (None, False) and "UVM_NO_DPI" not in defines,
            "Use Cadence's native UVM library; do not inherit XSim UVM_NO_DPI")
    for key in ("extra_inputs", "constraints"):
        require(isinstance(config.get(key, []), list) and all(isinstance(p, str) for p in config.get(key, [])), key + " must be a list of paths")
    inventory = {p.relative_to(root).as_posix(): p for p in paths}
    runtime_inputs = set()
    queue = list(paths)
    while queue:
        path = queue.pop()
        text = source_without_comments(path.read_text(encoding="utf-8"))
        require(not re.search(r'\b(?:import|export)\s+"DPI', text),
                "Candidate DPI is outside this standalone SV exercise: " + path.name)
        for name in re.findall(r'^\s*`include\s+"([^"\n]+)"', text, re.M):
            if name == "uvm_macros.svh":
                continue  # Provided by the selected Cadence UVM installation.
            matches = [folder / name for folder in [path.parent, *directories] if (folder / name).is_file()]
            require(bool(matches), "Missing transitive include: " + name)
            selected = inside(root, matches[0].relative_to(root).as_posix())
            relative = selected.relative_to(root).as_posix()
            if relative not in inventory:
                inventory[relative] = selected
                queue.append(selected)
        require(not re.search(r'^\s*`include\s+(?!")\S', text, re.M),
                "Macro-based includes need an explicit resolved SV delivery: " + path.name)
        for name in re.findall(r'\$readmem[hb]\s*\(\s*"([^"\n]+)"', text):
            require(name in config.get("extra_inputs", []), "Memory image must be declared in extra_inputs: " + name)
            runtime_inputs.add(runtime_name(name))
    for key in ("extra_inputs", "constraints"):
        for name in config.get(key, []):
            inventory[name] = inside(root, name)
    scripts = config.get("scripts", {})
    require(isinstance(scripts, dict) and set(scripts) <= set(STAGES) - {"simulation"}, "Unknown Cadence script stage")
    for name in scripts.values():
        path = inside(root, name)
        require(path.suffix == ".tcl", "Native Cadence stage recipes must be Tcl files")
        inventory[name] = path
    bins = None
    if verification:
        coverage_path = config.get("coverage_model")
        require(isinstance(coverage_path, str), "Provide the reviewed frozen functional coverage model")
        coverage_file = inside(root, coverage_path)
        coverage = read_json(coverage_file)
        require(coverage.get("schema_version") == 1 and coverage.get("metric") == "functional",
                "Coverage model must explicitly identify functional coverage")
        bins = coverage.get("bins")
        require(isinstance(bins, list) and bins and all(isinstance(b, dict) and isinstance(b.get("id"), str)
                and b["id"] and isinstance(b.get("requirement_ids"), list) and b["requirement_ids"]
                and all(isinstance(r, str) and r for r in b["requirement_ids"]) for b in bins), "Freeze named requirement-linked functional bins")
        require(len({b["id"] for b in bins}) == len(bins), "Duplicate functional bin identity")
        require(coverage.get("target_percent") == 90, "This exercise fixes the functional target at 90 percent")
        inventory[coverage_path] = coverage_file
    for name in forbidden:
        boundary = (workspace / name).resolve()
        require(boundary.is_relative_to(workspace), "Forbidden-root declaration escapes workspace")
        require(not any(path.is_relative_to(boundary) for path in [*inventory.values(), *directories]),
                "Historical/reference source cannot be a Cadence candidate: " + name)
    require(len({name.lower() for name in runtime_inputs}) == len(runtime_inputs), "Runtime memory input names collide across platforms")
    contents = {name: {"sha256": file_sha(path), "bytes": path.stat().st_size}
                for name, path in sorted(inventory.items())}
    exercise_inputs = {name: file_sha(workspace / name) for name in
                       ("spec.json", "cadence_contract.md", "integration_profile.json", "module_graph.json")
                       if (workspace / name).is_file()}
    identity = {"stage": stage, "manifest_sha256": file_sha(project_path), "inputs": contents, "exercise_inputs": exercise_inputs}
    return {"ready": True, "simulator": "cadence_xcelium", "execution": "external",
            "stage": stage, "validation_scope": "rtl_tb_coverage" if verification else "rtl_and_declared_constraints",
            "unvalidated_fields": [] if verification else ["tb_files", "top", "testname", "seed", "timescale", "coverage_model", "activity_contract"],
            "compile_order": selected_order, "runtime_inputs": {name: contents[name] for name in sorted(runtime_inputs)},
            "activity_contract": contract,
            "candidate_dir": str(root), "project": str(project_path), "config": config,
            "inputs": contents, "exercise_inputs": exercise_inputs, "input_digest": sha(encoded(identity).encode()),
            "functional_bin_count": len(bins) if bins is not None else None, "tool_execution_performed": False,
            "hdl_elaborated": False, "coverage_verified": False,
            "limits": "Declared files and literal includes checked; native elaboration and evidence review remain required"}


def make_plan(project_path, workspace, output, stage="simulation", site_path=None):
    observed = inspect(project_path, workspace, stage)
    config, root = observed["config"], Path(observed["candidate_dir"])
    output = Path(output).resolve()
    require(output.is_relative_to(Path(workspace).resolve()) and not output.exists(), "Run directory must be new and inside workspace")
    site = read_json(Path(site_path)) if site_path else {}
    executable = site.get("tools", {}).get(STAGES[stage], STAGES[stage])
    require(isinstance(executable, str) and executable and not any(c in executable for c in "\r\n\x00"), "Invalid tool executable")
    scripts = config.get("scripts", {})
    if stage != "simulation" and stage not in scripts:
        return {"ready": False, "stage": stage, "status": "site_recipe_required", "tool_execution_performed": False,
                "reason": "Supply a target-version Cadence Tcl recipe and declared constraints for " + stage}
    def quote(path):
        return '"' + path.as_posix() + '"'

    common = ['+incdir+' + quote(inside(root, name, directory=True)) for name in config.get("include_dirs", [])]
    common += ['+define+' + value for value in config.get("defines", [])]
    if stage == "simulation":
        command = [executable, "-64bit", "-sv", "-uvm", "-uvmhome", "CDNS-1.2", "-f", str(output / "files.f"),
                   "-top", config["top"], "-timescale", config["timescale"], "-svseed", str(config["seed"]),
                   "-coverage", "A", "+UVM_TESTNAME=" + config["testname"]]
    else:
        script = str(inside(root, scripts[stage]))
        flags = site.get("launch_flags", {}).get(stage)
        require(isinstance(flags, list) and flags and all(isinstance(item, str) for item in flags),
                "Set target-version launch_flags for " + stage + " with {script} placeholder")
        require(sum(item.count("{script}") for item in flags) == 1, "Stage flags must bind one script")
        command = [executable, *[item.replace("{script}", script) for item in flags]]
    version_args = site.get("version_args", {}).get(STAGES[stage], ["-version"])
    require(isinstance(version_args, list) and all(isinstance(item, str) for item in version_args), "Invalid version query")
    output.mkdir(parents=True)
    for name, metadata in observed["runtime_inputs"].items():
        target = output / runtime_name(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        require(not target.exists(), "Runtime memory input destination already exists: " + name)
        shutil.copyfile(inside(root, name), target)
        require(file_sha(target) == metadata["sha256"], "Runtime memory input changed during planning: " + name)
    for filename, members in (("files.f", observed["compile_order"]), ("rtl_files.f", [p for p in observed["compile_order"] if p in config["rtl_files"]])):
        (output / filename).write_text("\n".join([*common, *[quote(inside(root, name)) for name in members]]) + "\n", encoding="utf-8")
    plan = {"schema_version": 1, "ready": True, "stage": stage, "tool": STAGES[stage],
            "command": command, "version_command": [executable, *version_args], "cwd": str(output),
            "project": str(Path(project_path).resolve()), "workspace": str(Path(workspace).resolve()),
            "input_digest": observed["input_digest"], "inputs": observed["inputs"],
            "runtime_inputs": observed["runtime_inputs"], "activity_contract": observed["activity_contract"],
            "validation_scope": observed["validation_scope"], "unvalidated_fields": observed["unvalidated_fields"],
            "exercise_inputs": observed["exercise_inputs"],
            "manifest_sha256": sha(Path(project_path).read_bytes()), "site_sha256": sha(encoded(site).encode()),
            "tool_execution_performed": False, "coverage_verified": False,
            "environment": {"LOOPX_RTL_FILELIST": str(output / "rtl_files.f"), "LOOPX_DUT_TOP": config["dut_top"],
                            "LOOPX_CANDIDATE_ROOT": str(root), "LOOPX_RUN_DIR": str(output)},
            "native_results": "Keep native databases and reports in this unique run directory; no result is inferred from exit code alone"}
    if stage in {"simulation", "coverage"}:
        plan["environment"]["LOOPX_COVERAGE_MODEL"] = str(inside(root, config["coverage_model"]))
    (output / "inputs.json").write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


def smoke_observation(log, contract=None):
    contract = activity_contract(contract)
    counts = {severity: [int(n) for n in re.findall(r"UVM_" + severity + r"\s*:\s*(\d+)", log)]
              for severity in ("ERROR", "FATAL")}
    activity = list(re.finditer(contract["pattern"], log))
    counters = {name: int(value) for name, value in activity[-1].groupdict().items()} if activity else None
    minimums_met = counters is not None and all(counters[name] >= minimum for name, minimum in contract["minimums"].items())
    clean = (all(counts.values()) and not any(n for values in counts.values() for n in values)
             and not re.search(r"(?m)^\s*UVM_(?:ERROR|FATAL)\s+@|\*[EF],", log)
             and minimums_met)
    return {"clean_uvm_report_and_activity_observed": bool(clean), "uvm_summary_counts": counts,
            "activity_counters": counters, "activity_contract": contract, "activity_minimums_satisfied": minimums_met,
            "coverage_verified": False, "checker_correctness_verified": False}


def check_runtime_inputs(output, expected):
    for name, metadata in expected.items():
        path = inside(output, runtime_name(name))
        require(file_sha(path) == metadata["sha256"] and path.stat().st_size == metadata["bytes"],
                "Staged runtime memory input changed: " + name)


def terminate_owned_posix_group(process, grace_seconds=10):
    """Terminate only the new session created for this tool invocation."""
    import signal

    group = process.pid
    try:
        require(group > 1 and group != os.getpgrp() and os.getpgid(group) == group,
                "Refusing to signal a process group not owned by this invocation")
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    # Do not reap the leader before the final group signal: keeping its PID
    # reserved prevents that group identity from being reused during escalation.
    # A terminated leader does not imply its Cadence children have terminated.
    time.sleep(grace_seconds)
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def execute(plan, timeout=3600):
    require(plan.get("ready") is True, "Cannot execute an incomplete plan")
    output = Path(plan["cwd"])
    require(not (output / "receipt.json").exists(), "Run already has a receipt; use a new run directory")
    before = inspect(plan["project"], plan["workspace"], plan["stage"])
    require(before["input_digest"] == plan["input_digest"], "Inputs changed after planning")
    check_runtime_inputs(output, before["runtime_inputs"])
    tool = plan["command"][0]
    require(shutil.which(tool) or Path(tool).is_file(), "Cadence executable is not available: " + tool)
    environment = {**os.environ, **plan["environment"]}
    version = subprocess.run(plan["version_command"], capture_output=True, timeout=30, env=environment,
                             cwd=output, text=True, errors="replace")
    (output / "tool-version.txt").write_text(version.stdout + version.stderr, encoding="utf-8")
    require(version.returncode == 0, "Tool version query failed; qualify the site profile before execution")
    started = time.monotonic()
    status, code = "failed", None
    with (output / "tool.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(plan["command"], cwd=output, env=environment, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=os.name != "nt",
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            code = process.wait(timeout=timeout)
            status = "tool_completed" if code == 0 else "failed"
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            else:
                terminate_owned_posix_group(process)
            status = "timeout"
    observation = smoke_observation((output / "tool.log").read_text(encoding="utf-8", errors="replace"),
                                    before["activity_contract"]) if plan["stage"] == "simulation" else {}
    if status == "tool_completed" and plan["stage"] == "simulation" and not observation["clean_uvm_report_and_activity_observed"]:
        status = "simulation_evidence_incomplete"
    try:
        same_inputs = inspect(plan["project"], plan["workspace"], plan["stage"])["input_digest"] == plan["input_digest"]
        check_runtime_inputs(output, before["runtime_inputs"])
    except (OSError, ValueError):
        same_inputs = False
    if not same_inputs:
        status = "inputs_changed"
    artifacts = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and not path.is_symlink():
            artifacts.append({"path": path.relative_to(output).as_posix(), "sha256": file_sha(path), "bytes": path.stat().st_size})
    receipt = {"schema_version": 1, "status": status, "stage": plan["stage"], "tool": plan["tool"],
               "input_digest": plan["input_digest"], "command": plan["command"], "returncode": code,
               "validation_scope": before["validation_scope"], "unvalidated_fields": before["unvalidated_fields"],
               "runtime_inputs": before["runtime_inputs"],
               "elapsed_seconds": round(time.monotonic() - started, 3), "inputs_unchanged": same_inputs,
               "tool_execution_performed": True, "tool_identity": "operator_selected_installation",
               "task_acceptance": False, "coverage_verified": False, "coverage_percent": None,
               "native_report_review_required": True, "observation": observation, "artifacts": artifacts}
    (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "plan", "run"])
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--stage", choices=STAGES, default="simulation")
    parser.add_argument("--site", type=Path, help="Target-installation executable paths and native launch flags")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        require(args.timeout > 0, "Timeout must be positive")
        if args.action == "inspect":
            value = inspect(args.project, args.workspace, args.stage)
        else:
            output = args.output or args.workspace / "runs" / ("cadence-" + uuid.uuid4().hex)
            value = make_plan(args.project, args.workspace, output, args.stage, args.site)
            if args.action == "run" and args.execute and value["ready"]:
                value = execute(value, args.timeout)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        failed = value.get("ready") is False or value.get("status") in {"failed", "timeout", "inputs_changed", "simulation_evidence_incomplete"}
        return 2 if failed else 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "not_qualified", "error": str(error), "task_acceptance": False,
                          "coverage_verified": False}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
