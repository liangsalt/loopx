"""Tool-backed RTL checks pinned to the accepted shared design contract.

Vivado elaboration/synthesis, Verible syntax/style lint and structural CDC are
reported independently. None is functional verification or ASIC signoff.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from .models import ConflictError, ValidationError
from .state import file_hash
from .workflow import save_json


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_BATCH_META = re.compile(r'[\x00-\x1f&|<>^%!"`()]')
_REQUIRED = ("elaboration", "lint", "cdc")
_CHECK_RUN = re.compile(r"rtl_check_[0-9a-f]{32}\Z")
_WINDOWS_CWD_LIMIT = 90


def _local(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValidationError("Check evidence must be an existing workspace file")
    return path


def _tcl(value):
    """A literal Tcl word, not command interpolation."""
    value = str(value).replace("\\", "/")
    if any(ord(char) < 32 for char in value):
        raise ValidationError("Control characters are not allowed in tool inputs")
    return '"' + re.sub(r'([\\"$\[\]])', r'\\\1', value) + '"'


def _executable(name, env_name=None):
    explicit = os.environ.get(env_name, "") if env_name else ""
    candidate = explicit or shutil.which(name)
    if candidate and Path(candidate).is_file():
        return str(Path(candidate).resolve())
    return None


def _vivado_executable(home):
    home = home or os.environ.get("XILINX_VIVADO")
    if home:
        path = Path(home).resolve() / "bin" / ("vivado.bat" if os.name == "nt" else "vivado")
        if path.is_file():
            return str(path)
    return _executable("vivado.bat" if os.name == "nt" else "vivado")


def _working_directory(root, run_id):
    """Keep legacy Vivado's internal relative paths below Windows path limits."""
    if not isinstance(run_id, str) or not _CHECK_RUN.fullmatch(run_id):
        raise ValidationError("Invalid static-check run identity")
    lexical = root / ".lx-eda" / run_id.removeprefix("rtl_check_")[:12]
    resolved = lexical.resolve()
    if resolved != lexical or not resolved.is_relative_to(root):
        raise ValidationError("Static-check tool directory follows a link or escapes the workspace")
    if os.name == "nt" and len(str(resolved)) > _WINDOWS_CWD_LIMIT:
        raise ValidationError("Workspace path is too long for the qualified Vivado Windows working directory; use a shorter workspace path")
    return resolved


def _command(argv, directory, label, timeout, *, cwd=None):
    if os.name == "nt" and any(_BATCH_META.search(str(value)) for value in argv):
        raise ValidationError("Unsupported shell metacharacter in tool argument")
    log = directory / (label + ".log")
    cwd = Path(cwd or directory).resolve()
    started = time.monotonic()
    timed_out = False
    with log.open("wb") as output:
        process = subprocess.Popen(argv, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                process.kill()
            code = process.wait()
    return {"tool": label, "argv": list(map(str, argv)), "executable_sha256": file_hash(argv[0]),
            "cwd": str(cwd),
            "exit_code": code, "timed_out": timed_out, "seconds": round(time.monotonic() - started, 3),
            "log": str(log), "log_sha256": file_hash(log)}


def _clock_constraints(contract):
    clocks = contract.get("clocks", [])
    if not clocks:
        return None, "No explicit clock applicability/constraints; CDC is not automatically not-applicable"
    ports = {port["name"]: port for port in contract.get("ports", [])}
    names, constrained = set(), set()
    for clock in clocks:
        name, port, period = clock.get("name"), clock.get("port"), clock.get("period_ns")
        if (not isinstance(name, str) or not _IDENTIFIER.fullmatch(name) or name in names
                or port not in ports or ports[port]["direction"] != "input" or ports[port]["width"] != 1
                or port in constrained):
            raise ValidationError("Clock constraints require unique names and scalar input ports")
        if type(period) not in (int, float) or not math.isfinite(period) or period <= 0:
            return None, "Every clock requires an explicit positive period_ns for CDC constraints"
        names.add(name)
        constrained.add(port)
    reset_ports = {reset["port"] for reset in contract.get("resets", [])}
    for name, port in ports.items():
        if name in constrained or name in reset_ports:
            continue
        if port.get("clock_domain") not in names:
            return None, "Every data port requires an explicit clock-domain association for CDC: " + name
        if port["direction"] == "inout":
            return None, "Bidirectional I/O CDC binding requires a dedicated adapter: " + name
    return clocks, ""


def _script(candidate, directory, part):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", part):
        raise ValidationError("Invalid Vivado check target part")
    top = candidate["top"]
    if not _IDENTIFIER.fullmatch(top):
        raise ValidationError("RTL top must be a simple Verilog identifier")
    contract = candidate["contract"]
    clocks, missing = _clock_constraints(contract)
    lines = ["puts \"LOOPX_VIVADO_VERSION=[version -short]\"", "set_param general.maxThreads 2"]
    for path in candidate["rtl_files"]:
        if Path(path).suffix.lower() not in {".sv", ".v"}:
            raise ValidationError("Static-check translation units must be .sv or .v")
        lines.append("read_verilog " + ("-sv " if Path(path).suffix.lower() == ".sv" else "") + _tcl(path))
    parameters = contract.get("parameters", [])
    generic = []
    for parameter in parameters:
        if not _IDENTIFIER.fullmatch(parameter["name"]) or not re.fullmatch(r"[A-Za-z0-9_+'\-.:]+", parameter["value"]):
            raise ValidationError("Unsupported parameter value in Vivado check binding")
        generic.append(parameter["name"] + "=" + parameter["value"])
    synth = "synth_design -top " + _tcl(top) + " -part " + _tcl(part) + " -flatten_hierarchy none"
    if generic:
        synth += " -generic [list " + " ".join(_tcl(value) for value in generic) + "]"
    lines.append(synth)
    ports = contract.get("ports", [])
    lines.append("if {[llength [get_ports]] != " + str(sum(port["width"] for port in ports))
                 + '} {error "Shared contract port inventory differs from elaborated RTL"}')
    for port in ports:
        selector = port["name"] + ("[*]" if port["width"] > 1 else "")
        lines.append("set loopx_bound_ports [get_ports -quiet " + _tcl(selector) + "]")
        lines.append("if {[llength $loopx_bound_ports] != " + str(port["width"])
                     + '} {error "Shared contract port width or name differs from elaborated RTL"}')
        direction = {"input": "IN", "output": "OUT", "inout": "INOUT"}[port["direction"]]
        lines.append("foreach loopx_port $loopx_bound_ports {if {[get_property DIRECTION $loopx_port] ne "
                     + _tcl(direction) + '} {error "Shared contract port direction differs from elaborated RTL"}}')
    lines.append('puts "LOOPX_ELABORATION_DONE=1"')
    if clocks:
        for clock in clocks:
            lines.append("create_clock -name " + _tcl(clock["name"]) + " -period " + str(clock["period_ns"])
                         + " [get_ports " + _tcl(clock["port"]) + "]")
        special_ports = {clock["port"] for clock in clocks} | {reset["port"] for reset in contract.get("resets", [])}
        for port in contract.get("ports", []):
            if port["name"] in special_ports:
                continue
            delay = "set_input_delay" if port["direction"] == "input" else "set_output_delay"
            selector = port["name"] + ("[*]" if port["width"] > 1 else "")
            lines.append(delay + " -clock [get_clocks " + _tcl(port["clock_domain"]) + "] 0 [get_ports " + _tcl(selector) + "]")
        lines += [
            'puts "LOOPX_CLOCK_COUNT=[llength [get_clocks]]"',
            'puts "LOOPX_REGISTER_COUNT=[llength [get_cells -hier -filter {IS_SEQUENTIAL == 1}]]"',
            "set loopx_unclocked 0",
            "foreach pin [get_pins -hier -filter {IS_CLOCK == 1}] {",
            "  if {[llength [get_clocks -quiet -of_objects $pin]] == 0} {incr loopx_unclocked}",
            "}",
            'puts "LOOPX_UNCLOCKED_PINS=$loopx_unclocked"',
            "report_cdc -details -file " + _tcl(directory / "cdc.rpt"),
            'puts "LOOPX_CDC_DONE=1"',
        ]
    lines.append("exit")
    return "\n".join(lines) + "\n", missing


def _marker(text, name):
    matches = re.findall(r"(?m)^" + re.escape(name) + r"=(\d+)\s*$", text)
    return int(matches[0]) if len(matches) == 1 else None


def parse_cdc(report, console, clock_count):
    """A zero-crossing result requires an actual constrained structural run."""
    if (_marker(console, "LOOPX_CDC_DONE") != 1
            or _marker(console, "LOOPX_CLOCK_COUNT") != clock_count
            or _marker(console, "LOOPX_UNCLOCKED_PINS") != 0
            or not (_marker(console, "LOOPX_REGISTER_COUNT") or 0) > 0):
        return {"status": "blocked", "reason": "CDC execution, sequential activity or clock coverage was not established"}
    if "Clock Domain Crossing" not in report and "CDC Report" not in report:
        return {"status": "blocked", "reason": "Unrecognized Vivado CDC report"}
    # Reports have a summary table whose columns are ID, severity, count, description.
    rows = re.findall(r"(?mi)^\s*\|?\s*(CDC-\d+)\s*\|\s*(Critical|Warning|Info)\s*\|\s*(\d+)\s*\|", report)
    rows += re.findall(r"(?mi)^\s*(CDC-\d+)\s+(Critical|Warning|Info)\s+(\d+)\s+", report)
    if not rows:
        if re.search(r"(?m)^All paths are Safely Timed\.\s*$", report):
            return {"status": "passed", "findings": 0, "rules": {},
                    "scope": "Vivado reports no asynchronous crossing in the explicitly constrained scope; not RDC or ASIC signoff"}
        return {"status": "blocked", "reason": "CDC summary counts are absent or unsupported"}
    counts = {rule: {"severity": severity.lower(), "count": int(count)} for rule, severity, count in rows}
    if len(counts) != len(rows):
        return {"status": "blocked", "reason": "Ambiguous duplicate CDC rule summary"}
    failures = sum(entry["count"] for entry in counts.values() if entry["severity"] in {"critical", "warning"})
    return {"status": "failed" if failures else "passed", "findings": failures, "rules": counts,
            "scope": "Vivado structural CDC on the constrained synthesized check target; not RDC or ASIC signoff"}


def _evaluate(candidate, evidence, root):
    if evidence.get("inputs") != candidate["inputs"] or any(
            evidence.get(key) != candidate[key] for key in ("rtl_task_id", "contract_task_id", "contract_hash", "top")):
        raise ConflictError("Static-check evidence names a different RTL/shared contract revision")
    for path, expected in candidate["inputs"].items():
        if file_hash(_local(root, path)) != expected:
            raise ConflictError("RTL input changed after static checks")
    script_records = [item for item in evidence.get("artifacts", []) if Path(item["path"]).name == "checks.tcl"]
    if len(script_records) != 1:
        raise ValidationError("Exactly one generated static-check script is required")
    script = _local(root, script_records[0]["path"])
    version = evidence.get("schema_version")
    if version == 1:
        expected_cwd = script.parent
    elif version == 2:
        expected_cwd = _working_directory(root, evidence.get("run_id"))
        if script.parent.name != evidence["run_id"]:
            raise ConflictError("Static-check script and working directory name different runs")
    else:
        raise ValidationError("Unsupported static-check evidence version")
    expected_script, _ = _script(candidate, script.parent, evidence["target_part"])
    if script.read_text(encoding="utf-8") != expected_script:
        raise ConflictError("Static-check script differs from the bound candidate and constraints")
    records = {}
    for record in evidence.get("commands", []):
        observed_cwd = record.get("cwd", str(script.parent) if version == 1 else None)
        if observed_cwd != str(expected_cwd):
            raise ConflictError("Static-check command ran in a different working directory")
        log = _local(root, record["log"])
        if file_hash(log) != record["log_sha256"]:
            raise ConflictError("Static-check raw log changed")
        if record["tool"] in records:
            raise ValidationError("Duplicate static-check tool evidence")
        argv = record.get("argv", [])
        if not argv or not Path(argv[0]).is_file() or file_hash(argv[0]) != record.get("executable_sha256"):
            raise ConflictError("Static-check executable identity changed or is missing")
        if record["tool"] == "vivado":
            expected = [argv[0], "-mode", "batch", "-nojournal", "-nolog", "-source", str(script)]
            names = {"vivado", "vivado.bat"}
        elif record["tool"] == "verible":
            expected = [argv[0], "--ruleset=default", "--lint_fatal=true", "--parse_fatal=true", *candidate["rtl_files"]]
            names = {"verible-verilog-lint", "verible-verilog-lint.exe"}
        else:
            raise ValidationError("Unknown static-check tool")
        if argv != expected or Path(argv[0]).name not in names:
            raise ValidationError("Tool command does not implement the required static check")
        records[record["tool"]] = record
    for artifact in evidence.get("artifacts", []):
        if file_hash(_local(root, artifact["path"])) != artifact["sha256"]:
            raise ConflictError("Static-check script/report changed")
    checks = {name: {"status": "unavailable", "reason": "Tool not available"} for name in _REQUIRED}
    vivado = records.get("vivado")
    if vivado:
        text = _local(root, vivado["log"]).read_text(encoding="utf-8", errors="replace")
        good = (vivado["exit_code"] == 0 and not vivado["timed_out"]
                and not re.search(r"(?m)^\s*(?:ERROR:|FATAL_ERROR:|CRITICAL WARNING:)", text))
        unsupported = bool(re.search(r"(?mi)^.*(?:WARNING|ERROR).*\b(?:unsupported|not supported|ignored)\b", text))
        elaborated = _marker(text, "LOOPX_ELABORATION_DONE") == 1
        checks["elaboration"] = {"status": "passed" if good and elaborated and not unsupported else "failed",
                                 "scope": "Vivado synthesis on explicit FPGA check target " + evidence["target_part"],
                                 "unsupported_constructs": unsupported}
        clocks, missing = _clock_constraints(candidate["contract"])
        cdc_path = next((item["path"] for item in evidence["artifacts"] if Path(item["path"]).name == "cdc.rpt"), None)
        if missing:
            checks["cdc"] = {"status": "blocked", "reason": missing}
        elif good and elaborated and not unsupported and cdc_path:
            checks["cdc"] = parse_cdc(_local(root, cdc_path).read_text(encoding="utf-8", errors="replace"), text, len(clocks))
        else:
            checks["cdc"] = {"status": "blocked", "reason": "A supported synthesized design and complete CDC report are required"}
    lint = records.get("verible")
    if lint:
        checks["lint"] = {"status": "passed" if lint["exit_code"] == 0 and not lint["timed_out"] else "failed",
                          "scope": "Verible default syntax/style rules; not exhaustive semantic lint"}
    required = candidate.get("required_checks", list(_REQUIRED))
    if set(required) != set(_REQUIRED):
        raise ValidationError("RTL checks cannot silently waive elaboration, lint or CDC")
    qualified = all(checks[name]["status"] == "passed" for name in required)
    return {"qualified": qualified, "checks": checks,
            "composition_required": bool(candidate.get("composition_required", False)),
            "scope_id": candidate.get("scope_id"), "coverage_verified": False, "signoff": False}


def current_check(project, rtl_task_id):
    """Re-read evidence without creating state or changing a task."""
    from .design import get_candidate

    tasks = [task for task in project.store.list_tasks(kind="rtl-check")
             if task["payload"].get("rtl_task_id") == rtl_task_id]
    if not tasks:
        return {"status": "not_run", "qualified": False, "task_id": None, "rtl_task_id": rtl_task_id}
    task = tasks[-1]
    response = {"task_id": task["id"], "rtl_task_id": rtl_task_id, "status": task["status"], "qualified": False}
    try:
        candidate = get_candidate(project, rtl_task_id)
        run = project.store.get_run(task["payload"]["run_id"])
        path = _local(project.root, task["payload"]["evidence_path"])
        if file_hash(path) != run["metadata"].get("evidence_sha256"):
            raise ConflictError("Static-check evidence artifact changed or is incomplete")
        evidence = json.loads(path.read_text(encoding="utf-8"))
        response.update(_evaluate(candidate, evidence, project.root), evidence_path=str(path))
        if task["status"] != "done" or run["validity"] != "current":
            response["qualified"] = False
        if response["qualified"]:
            response["status"] = "passed"
    except (OSError, KeyError, ValueError, RuntimeError) as error:
        response.update(qualified=False, error=str(error))
    return response


def run_checks(project, rtl_task_id, *, timeout=600, vivado_home=None):
    """Execute explicit local checks and commit only independently re-read proof."""
    from .design import get_candidate

    if type(timeout) is not int or not 0 < timeout <= 86400:
        raise ValueError("timeout must be 1..86400 seconds")
    project.assert_inputs_current()
    project.store.verify_artifacts()
    candidate = get_candidate(project, rtl_task_id)
    previous = current_check(project, rtl_task_id)
    if previous["qualified"]:
        return {**previous, "replayed": True}
    run_id = "rtl_check_" + uuid.uuid4().hex
    working_directory = _working_directory(project.root, run_id)
    directory = project.store.state_dir / "runs" / run_id
    if not directory.resolve().is_relative_to(project.root):
        raise ValidationError("Static-check output directory escapes the workspace")
    directory.mkdir(parents=True)
    evidence_path = directory / "evidence.json"
    part = project.config.get("engineering_check_part", "xc7a35tcpg236-1")
    script_text, _ = _script(candidate, directory, part)
    script = directory / "checks.tcl"
    script.write_text(script_text, encoding="utf-8")
    task = project.store.add_task("rtl-check", "Check RTL " + candidate["top"],
        {"rtl_task_id": rtl_task_id, "contract_task_id": candidate["contract_task_id"],
         "contract_hash": candidate["contract_hash"], "inputs": candidate["inputs"], "run_id": run_id,
         "evidence_path": str(evidence_path)}, depends_on=[rtl_task_id, candidate["contract_task_id"]], task_id=run_id)
    attempt = project.store.claim(task["id"], "rtl-check-tools", lease_seconds=timeout * 2 + 180)
    project.store.record_run(run_id, "rtl_static_checks", task_id=task["id"], attempt_id=attempt["id"])
    project.store.update_run(run_id, "running")
    evidence = {key: candidate[key] for key in ("rtl_task_id", "contract_task_id", "contract_hash", "top", "inputs")}
    evidence.update(schema_version=2, run_id=run_id, target_part=part, commands=[], artifacts=[], limitations=[
        "FPGA check target is an analysis binding, not a claimed implementation technology",
        "Zero I/O delays declare source-specified clock associations for CDC only, not timing guarantees",
        "Verible syntax/style checks are not exhaustive RTL lint", "CDC does not include reset-domain crossing or ASIC signoff"])
    error = None
    try:
        working_directory.mkdir(parents=True, exist_ok=False)
        vivado = _vivado_executable(vivado_home)
        if vivado:
            evidence["commands"].append(_command([vivado, "-mode", "batch", "-nojournal", "-nolog", "-source", str(script)],
                                                  directory, "vivado", timeout, cwd=working_directory))
        lint = _executable("verible-verilog-lint", "LOOPX_VERIBLE_LINT")
        if lint:
            evidence["commands"].append(_command([lint, "--ruleset=default", "--lint_fatal=true", "--parse_fatal=true",
                                                  *candidate["rtl_files"]], directory, "verible", timeout, cwd=working_directory))
        project.assert_inputs_current()
        for path in (script, directory / "cdc.rpt"):
            if path.is_file():
                evidence["artifacts"].append({"path": str(path), "sha256": file_hash(path)})
        verdict = _evaluate(get_candidate(project, rtl_task_id), evidence, project.root)
    except (OSError, ValueError, RuntimeError) as caught:
        error = str(caught)
        verdict = {"qualified": False, "checks": {}, "error": error}
    save_json(evidence_path, evidence)
    project.store.update_run(run_id, "running",
        metadata={"evidence_sha256": file_hash(evidence_path), "verdict": verdict}, artifacts=[str(evidence_path)])
    if verdict["qualified"]:
        def validate(_task, result, _artifacts):
            project.assert_inputs_current()
            current = _evaluate(get_candidate(project, rtl_task_id), result["evidence"], project.root)
            if not current["qualified"]:
                raise ValidationError("Required static checks did not pass")
            return {"passed": True, "validator": "rtl_static_checks_v1",
                    "checks": ["exact_candidate_inputs", "raw_tool_logs", "constrained_cdc", "verible_lint"], "measured": current}
        artifacts = [evidence_path] + [item["path"] for item in evidence["artifacts"]] + [item["log"] for item in evidence["commands"]]
        try:
            project.store.finish(task["id"], attempt["id"], {"evidence": evidence, **verdict}, artifacts=artifacts, validator=validate)
        except (OSError, ValueError, RuntimeError) as caught:
            error = str(caught)
            verdict.update(qualified=False, error=error)
    if not verdict["qualified"]:
        reason = error or "; ".join(name + ": " + check["status"] for name, check in verdict["checks"].items() if check["status"] != "passed")
        if project.store.get_task(task["id"])["status"] == "running":
            project.store.fail(task["id"], attempt["id"], reason, retryable=False)
            project.store.block(task["id"], reason)
        project.store.add_issue("rtl_check_blocker", reason, task_id=task["id"])
    project.store.update_run(run_id, "passed" if verdict["qualified"] else "failed", metadata={"verdict": verdict})
    return {"status": "passed" if verdict["qualified"] else "blocked", "task_id": task["id"],
            "rtl_task_id": rtl_task_id, "run_id": run_id, "evidence_path": str(evidence_path), **verdict}
