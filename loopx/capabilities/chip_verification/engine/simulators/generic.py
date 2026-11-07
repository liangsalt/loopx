"""Manifest-driven XSim build and UVM smoke execution, without coverage claims.

The manifest is a reviewed build contract, not an agent completion assertion.
``built`` means compile and elaboration succeeded; ``smoke_verified`` additionally
requires a complete, clean UVM report and observed checking/transaction counters.
It does not establish correctness of the checker or specification coverage.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from .xsim import SimulationError, XSimBackend


class ManifestError(SimulationError):
    """An incomplete, ambiguous or unsafe build contract."""


_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_$]*"
_UNSAFE = re.compile(r'[\x00-\x1f&|<>^%!"`()]')
_HDL_SUFFIXES = {".v", ".sv", ".vh", ".svh"}
_FIELDS = {
    "schema_version", "candidate_dir", "top", "rtl_files", "tb_files",
    "compile_order", "include_dirs", "defines", "uvm_dir", "vivado_home",
    "testname", "runtime", "seed", "timescale", "uvm_no_dpi",
    "activity_contract", "coverage_manifest", "extra_inputs",
}


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _safe_string(value, label: str) -> str:
    if not isinstance(value, str) or not value or _UNSAFE.search(value):
        raise ManifestError(f"Invalid or unsupported shell metacharacter in {label}")
    return value


def _list(value, label: str, *, nonempty=False) -> list:
    if not isinstance(value, list) or (nonempty and not value):
        raise ManifestError(f"{label} must be {'a nonempty' if nonempty else 'an'} array")
    return value


def _path(base: Path, value, label: str, *, directory=False) -> Path:
    raw = _safe_string(value, label)
    result = (base / raw).resolve()
    _safe_string(str(result), label)
    if not (result.is_dir() if directory else result.is_file()):
        raise ManifestError(f"{label} does not exist: {result}")
    return result


def _activity(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"pattern", "minimums"}:
        raise ManifestError("activity_contract requires pattern and optional minimums")
    pattern = value.get("pattern")
    if not isinstance(pattern, str) or not pattern or len(pattern) > 1000 or "\n" in pattern:
        raise ManifestError("Activity pattern must be a single-line regex of at most 1000 characters")
    try:
        compiled = re.compile(pattern)
    except re.error as error:
        raise ManifestError(f"Invalid activity pattern: {error}") from error
    if not {"checks", "transactions"}.issubset(compiled.groupindex):
        raise ManifestError("Activity pattern must contain named checks and transactions counters")
    minimums = value.get("minimums", {"checks": 1, "transactions": 1})
    if not isinstance(minimums, dict) or not {"checks", "transactions"}.issubset(minimums):
        raise ManifestError("Activity minimums must include checks and transactions")
    for key, minimum in minimums.items():
        if key not in compiled.groupindex or type(minimum) is not int or minimum < 1:
            raise ManifestError("Each activity minimum must name a captured counter and be positive")
    return {"pattern": pattern, "minimums": dict(minimums)}


def load_build_manifest(manifest_path) -> dict:
    """Validate a v1 manifest and return absolute paths; never run a tool.

    RTL, include directories and extra data inputs may be explicitly external.
    Every TB translation unit must remain within candidate_dir after resolving
    links. compile_order, when present, must be an exact permutation of RTL+TB.
    """
    manifest_path = Path(manifest_path).resolve()
    _safe_string(str(manifest_path), "manifest_path")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ManifestError(f"Cannot read build manifest: {error}") from error
    if not isinstance(data, dict) or set(data) - _FIELDS:
        raise ManifestError("Unknown manifest fields or manifest is not an object")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ManifestError("Build manifest schema_version must be 1")
    candidate = _path(manifest_path.parent, data.get("candidate_dir", "."), "candidate_dir", directory=True)
    top = data.get("top")
    if not isinstance(top, str) or not re.fullmatch(_IDENTIFIER, top):
        raise ManifestError("top must be an unescaped Verilog identifier")
    resolved = {"manifest_path": str(manifest_path), "candidate_dir": str(candidate), "top": top}
    for role in ("rtl_files", "tb_files"):
        paths = [_path(candidate, item, role) for item in _list(data.get(role), role, nonempty=True)]
        if role == "tb_files" and any(not item.is_relative_to(candidate) for item in paths):
            raise ManifestError("TB source follows a link or path outside candidate_dir")
        if any(item.suffix.lower() not in {".v", ".sv"} for item in paths):
            raise ManifestError("RTL and TB translation units must end in .v or .sv")
        resolved[role] = list(map(str, paths))
    ordered = resolved["rtl_files"] + resolved["tb_files"]
    if len(set(ordered)) != len(ordered):
        raise ManifestError("Translation units must be unique")
    if "compile_order" in data:
        explicit = [str(_path(candidate, item, "compile_order"))
                    for item in _list(data["compile_order"], "compile_order", nonempty=True)]
        if len(explicit) != len(ordered) or set(explicit) != set(ordered):
            raise ManifestError("compile_order must list every RTL and TB unit exactly once")
        ordered = explicit
    resolved["compile_order"] = ordered
    for field, directory in (("include_dirs", True), ("extra_inputs", False)):
        paths = [str(_path(candidate, item, field, directory=directory))
                 for item in _list(data.get(field, []), field)]
        if len(set(paths)) != len(paths):
            raise ManifestError(f"{field} contains duplicate paths")
        resolved[field] = paths
    defines = _list(data.get("defines", []), "defines")
    for define in defines:
        _safe_string(define, "define")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:=[A-Za-z0-9_+./:'-]+)?", define):
            raise ManifestError("Defines require NAME or NAME=simple_literal (no arbitrary options)")
    if len({item.split("=", 1)[0] for item in defines}) != len(defines):
        raise ManifestError("Duplicate preprocessor define")
    resolved["defines"] = list(defines)
    dpi = data.get("uvm_no_dpi", True)
    if type(dpi) is not bool:
        raise ManifestError("uvm_no_dpi must be boolean")
    if dpi and "UVM_NO_DPI" not in {item.split("=", 1)[0] for item in defines}:
        resolved["defines"].insert(0, "UVM_NO_DPI")
    resolved["uvm_no_dpi"] = dpi
    for field in ("uvm_dir", "vivado_home"):
        resolved[field] = str(_path(candidate, data[field], field, directory=True)) if field in data else None
    testname = data.get("testname")
    if testname is not None and (not isinstance(testname, str) or not re.fullmatch(_IDENTIFIER, testname)):
        raise ManifestError("testname must be an unescaped UVM class identifier")
    resolved["testname"] = testname
    runtime = data.get("runtime", "all")
    if not isinstance(runtime, str) or not re.fullmatch(r"all|[1-9][0-9]*(?:\.[0-9]+)?\s*(?:fs|ps|ns|us|ms|s)", runtime):
        raise ManifestError("runtime must be 'all' or a positive time such as '100 us'")
    resolved["runtime"] = runtime
    seed = data.get("seed", 1)
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ManifestError("seed must be an integer between 0 and 2147483647")
    resolved["seed"] = seed
    timescale = data.get("timescale", "1ns/1ps")
    if not isinstance(timescale, str) or not re.fullmatch(r"(?:1|10|100)(?:fs|ps|ns|us|ms|s)/(?:1|10|100)(?:fs|ps|ns|us|ms|s)", timescale):
        raise ManifestError("timescale must be a Verilog time unit/precision")
    resolved["timescale"] = timescale
    resolved["activity_contract"] = _activity(data.get("activity_contract"))
    resolved["coverage_manifest"] = (str(_path(candidate, data["coverage_manifest"], "coverage_manifest"))
                                     if "coverage_manifest" in data else None)
    return resolved


def parse_smoke_log(log_text: str, activity_contract=None) -> dict:
    """Fail closed on UVM reports; checker activity is separate from completion."""
    errors = re.findall(r"(?m)^\s*UVM_ERROR\s*:\s*(\d+)\s*$", log_text)
    fatals = re.findall(r"(?m)^\s*UVM_FATAL\s*:\s*(\d+)\s*$", log_text)
    if not errors or not fatals or "UVM Report Summary" not in log_text:
        raise SimulationError("Complete UVM report summary missing (possibly stopped before check/report phases)")
    if any(map(int, errors + fatals)) or re.search(r"(?m)^\s*UVM_(?:ERROR|FATAL)\s+(?!:)\S", log_text):
        raise SimulationError("UVM error/fatal reported")
    result = {"status": "smoke_completed", "verified": False, "uvm_errors": 0, "uvm_fatals": 0,
              "verification_scope": "UVM smoke only; no specification or coverage closure",
              "activity": {"status": "not_proven", "reason": "No checker activity contract supplied"}}
    contract = _activity(activity_contract)
    if contract is None:
        return result
    # Per-line matching avoids accidental matches spanning unrelated reports.
    matches = [match for line in log_text.splitlines() for match in re.finditer(contract["pattern"], line)]
    if not matches:
        raise SimulationError("Declared checker activity marker missing")
    counts = matches[-1].groupdict()
    if any(value is None or not re.fullmatch(r"[0-9]+", value) for value in counts.values()):
        raise SimulationError("Checker activity counters must be unsigned decimal integers")
    counts = {name: int(value) for name, value in counts.items()}
    if any(counts[name] < minimum for name, minimum in contract["minimums"].items()):
        raise SimulationError("Checker/transaction activity did not meet the declared positive minimums")
    result.update(status="smoke_verified", verified=True,
                  activity={"status": "observed", "counters": counts, "contract": contract,
                            "limitation": "Counters attest activity; checker independence and quality require review"})
    return result


def qualification_limitations(texts: dict[str, str]) -> list[dict]:
    """Retain tool-declared ignored features; a zero exit code cannot waive them."""
    result = []
    for tool, text in texts.items():
        lines = [line.strip() for line in text.splitlines()
                 if re.search(r"(?i)^\s*WARNING:.*(?:43-4127|not supported|unsupported|ignored)", line)]
        if lines:
            result.append({"tool": tool, "kind": "unsupported_or_ignored_construct",
                           "diagnostic_count": len(lines), "examples": list(dict.fromkeys(lines))[:8],
                           "full_diagnostics": "Preserved in the hashed raw tool log"})
    return result


def _qualify_smoke(parsed, limitations):
    if limitations:
        parsed.update(status="smoke_unqualified", verified=False,
                      verification_scope="UVM activity recorded; ignored/unsupported constructs prevent smoke verification acceptance")
    return parsed


def _arguments(manifest: dict, run: Path, run_smoke: bool) -> list[tuple[str, list[str]]]:
    xvlog = ["-sv"]
    for define in manifest["defines"]:
        xvlog.extend(["-d", define])
    for directory in dict.fromkeys([manifest["uvm_dir"], *manifest["include_dirs"]]):
        xvlog.extend(["-i", directory])
    xvlog.extend([str(Path(manifest["uvm_dir"]) / "uvm_pkg.sv"), *manifest["compile_order"]])
    xelab = ["work." + manifest["top"], "-s", "dvflow_candidate", "-debug", "typical",
             "-timescale", manifest["timescale"]]
    steps = [("xvlog", xvlog), ("xelab", xelab)]
    if run_smoke:
        xsim = ["dvflow_candidate", "-tclbatch", str(run / "smoke.tcl"), "-sv_seed", str(manifest["seed"])]
        if manifest["testname"]:
            xsim.extend(["-testplusarg", "UVM_TESTNAME=" + manifest["testname"]])
        steps.append(("xsim", xsim))
    return steps


def _response_text(tokens: list[str]) -> str:
    return "\n".join('"' + token.replace("\\", "/") + '"' for token in tokens) + "\n"


def _script_text(manifest: dict) -> str:
    runtime = manifest["runtime"]
    return ("run -all" if runtime == "all" else "run " + runtime) + "\nquit\n"


class GenericXSimBackend:
    """Compile/elaborate candidates and optionally run a real UVM smoke.

    Constructing the backend and calling plan() do not launch EDA processes.
    Every actual command, including failed/timed-out commands, is preserved.
    """

    def __init__(self, vivado_home=None):
        self.vivado_home = vivado_home

    def plan(self, manifest_path) -> dict:
        manifest = load_build_manifest(manifest_path)
        configured = self.vivado_home or manifest["vivado_home"]
        if self.vivado_home and manifest["vivado_home"] and Path(self.vivado_home).resolve() != Path(manifest["vivado_home"]):
            raise ManifestError("Backend and manifest select different Vivado installations")
        backend = XSimBackend(configured)
        if configured and backend.home != Path(configured).resolve():
            raise ManifestError("Explicit Vivado installation is unavailable; refusing silent fallback")
        home = backend.home
        _safe_string(str(home), "Vivado installation")
        for tool in ("xvlog", "xelab", "xsim"):
            if not (home / "bin" / (tool + backend.suffix)).is_file():
                raise ManifestError(f"Required Vivado executable missing: {tool}")
        uvm = Path(manifest["uvm_dir"]) if manifest["uvm_dir"] else home / "data/system_verilog/uvm_1.2"
        if not (uvm / "uvm_pkg.sv").is_file():
            raise ManifestError("UVM source package missing; provide uvm_dir containing uvm_pkg.sv")
        _safe_string(str(uvm), "UVM source directory")
        manifest.update(vivado_home=str(home), uvm_dir=str(uvm.resolve()), executable_suffix=backend.suffix)
        return manifest

    @staticmethod
    def _inputs(manifest: dict) -> list[dict]:
        paths = {Path(manifest["manifest_path"]), Path(manifest["uvm_dir"]) / "uvm_pkg.sv"}
        paths.update(map(Path, manifest["compile_order"] + manifest["extra_inputs"]))
        if manifest["coverage_manifest"]:
            paths.add(Path(manifest["coverage_manifest"]))
        # Include search directories can affect preprocessing even if a header is
        # shadowed. Hash the complete HDL inventory, including implicit source dirs.
        directories = {Path(manifest["uvm_dir"]), *map(Path, manifest["include_dirs"])}
        directories.update(Path(item).parent for item in manifest["compile_order"])
        for directory in directories:
            for path in directory.rglob("*"):
                if path.is_file() and path.suffix.lower() in _HDL_SUFFIXES:
                    paths.add(path.resolve())
                    if len(paths) > 20000:
                        raise ManifestError("Include inventory exceeds 20000 files; narrow include directories")
        return [{"path": str(path), "sha256": _hash(path), "bytes": path.stat().st_size}
                for path in sorted(paths, key=str)]

    def snapshot_inputs(self, manifest_path) -> list[dict]:
        """Return the pre-launch source inventory for a state-owned baseline."""
        return self._inputs(self.plan(manifest_path))

    @staticmethod
    def _options(path: Path, tokens: list[str]) -> dict:
        # Response files avoid Windows .bat reparsing '=' in defines/plusargs.
        # One quoted token per line; input validation excludes quotes/newlines.
        for token in tokens:
            _safe_string(token, "tool option")
        path.write_text(_response_text(tokens), encoding="utf-8")
        return {"path": str(path), "sha256": _hash(path), "tokens": tokens}

    def _execute(self, tool: str, arguments: list[str], run: Path, manifest: dict,
                 timeout_seconds: int) -> dict:
        argv = [str(Path(manifest["vivado_home"]) / "bin" / (tool + manifest["executable_suffix"])), *arguments]
        for argument in argv:
            _safe_string(argument, "process argument")
        log = run / (tool + "_console.log")
        started = time.monotonic()
        env = os.environ.copy()
        env["XILINX_VIVADO"] = manifest["vivado_home"]
        record = {"tool": tool, "argv": argv, "cwd": str(run), "timeout_seconds": timeout_seconds,
                  "log": str(log), "exit_code": None, "timed_out": False}
        try:
            with log.open("wb") as output:
                process = subprocess.Popen(argv, cwd=str(run), stdout=output, stderr=subprocess.STDOUT,
                                           env=env, shell=False)
                try:
                    record["exit_code"] = process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    record["timed_out"] = True
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
                    else:
                        process.kill()
                    record["exit_code"] = process.wait()
        except OSError as error:
            record["launch_error"] = str(error)
        record["seconds"] = round(time.monotonic() - started, 3)
        record["log_sha256"] = _hash(log) if log.exists() else None
        return record

    def run(self, manifest_path, run_dir, run_smoke=False, timeout_seconds=600) -> dict:
        """Return and save evidence. Manifest errors raise; execution errors fail.

        run_dir must be empty/new. No generated scripts, arbitrary commands,
        simulator flags or Python imports from the candidate are executed.
        """
        if type(run_smoke) is not bool or type(timeout_seconds) is not int or timeout_seconds < 1:
            raise ManifestError("run_smoke must be boolean and timeout_seconds a positive integer")
        manifest = self.plan(manifest_path)
        run = Path(run_dir).resolve()
        _safe_string(str(run), "run_dir")
        if run.exists() and (not run.is_dir() or any(run.iterdir())):
            raise ManifestError("Use a new or empty run directory; prior evidence must not be overwritten")
        run.mkdir(parents=True, exist_ok=True)
        evidence = {"schema_version": 1, "backend": "vivado_xsim_generic", "status": "running",
                    "verified": False, "run_dir": str(run), "run_smoke": run_smoke,
                    "manifest": manifest, "inputs": self._inputs(manifest), "commands": [], "artifacts": [],
                    "coverage": {"status": "not_evaluated", "reason": "Generic smoke does not evaluate coverage bins"},
                    "meets_coverage_target": False}
        _write_json(run / "input_manifest.json", evidence)
        texts = {}
        try:
            steps = _arguments(manifest, run, run_smoke)
            if run_smoke:
                script = run / "smoke.tcl"
                script.write_text(_script_text(manifest), encoding="ascii")
                evidence["artifacts"].append({"path": str(script), "sha256": _hash(script)})
            for tool, tokens in steps:
                response = run / (tool + ".options")
                evidence["artifacts"].append(self._options(response, tokens))
                command = self._execute(tool, ["-f", str(response)], run, manifest, timeout_seconds)
                command["expanded_arguments"] = tokens
                evidence["commands"].append(command)
                _write_json(run / "evidence.json", evidence)
                log = Path(command["log"]).read_text(encoding="utf-8", errors="replace") if Path(command["log"]).exists() else ""
                texts[tool] = log
                evidence["qualification_limitations"] = qualification_limitations(texts)
                if command["exit_code"] != 0 or command["timed_out"] or command.get("launch_error"):
                    raise SimulationError(f"{tool} failed or timed out; inspect {command['log']}")
                if re.search(r"(?m)^\s*(?:ERROR:|FATAL_ERROR:|FATAL:|Expected a switch)", log):
                    raise SimulationError(f"{tool} reported a tool error despite zero exit status")
            evidence["status"] = "built"
            if run_smoke:
                text = (run / "xsim_console.log").read_text(encoding="utf-8", errors="replace")
                evidence.update(_qualify_smoke(parse_smoke_log(text, manifest["activity_contract"]), evidence["qualification_limitations"]))
            if self._inputs(manifest) != evidence["inputs"]:
                raise SimulationError("Build inputs changed during execution; evidence is not reproducible")
        except (OSError, ValueError, SimulationError) as error:
            evidence.update(status="failed", verified=False, error=str(error))
        _write_json(run / "evidence.json", evidence)
        return evidence


def validate_generic_evidence(evidence: dict, manifest_path, phase=None, expected_inputs=None) -> dict:
    """Independently reopen evidence artifacts before a state transition.

    ``phase='build'`` can accept a completed compile/elaboration even if a later
    smoke failed. A positive smoke verdict can still have verified=False when no
    checking contract was supplied. expected_inputs anchors the run to the state
    controller's own pre-launch inventory rather than a mutable evidence claim.
    This authenticates local consistency, not a cryptographic simulator signature.
    """
    try:
        if not isinstance(evidence, dict) or evidence.get("backend") != "vivado_xsim_generic":
            raise SimulationError("Not generic XSim evidence")
        if type(evidence.get("run_smoke")) is not bool:
            raise SimulationError("Missing run mode")
        phase = phase or ("smoke" if evidence["run_smoke"] else "build")
        if phase not in {"build", "smoke"}:
            raise SimulationError("Validation phase must be build or smoke")
        if phase == "smoke" and not evidence["run_smoke"]:
            raise SimulationError("Build-only evidence cannot establish smoke completion")
        manifest = GenericXSimBackend(evidence["manifest"]["vivado_home"]).plan(manifest_path)
        if evidence["manifest"] != manifest:
            raise SimulationError("Resolved build contract differs from evidence")
        actual_inputs = GenericXSimBackend._inputs(manifest)
        if evidence.get("inputs") != actual_inputs:
            raise SimulationError("Input inventory or hashes no longer match the run")
        if expected_inputs is not None and actual_inputs != expected_inputs:
            raise SimulationError("Inputs differ from the state-owned pre-launch baseline")
        run = Path(evidence["run_dir"]).resolve()
        initial = json.loads((run / "input_manifest.json").read_text(encoding="utf-8"))
        for key in ("inputs", "manifest", "run_smoke", "run_dir", "backend"):
            if initial.get(key) != evidence.get(key):
                raise SimulationError("Initial input manifest differs from final evidence")
        commands = evidence.get("commands")
        minimum = 3 if phase == "smoke" else 2
        if not isinstance(commands, list) or not minimum <= len(commands) <= (3 if evidence["run_smoke"] else 2):
            raise SimulationError("Required compiler/elaborator/simulator steps are missing")
        artifacts = evidence.get("artifacts", [])
        artifact_map = {item["path"]: item for item in artifacts}
        if len(artifact_map) != len(artifacts):
            raise SimulationError("Duplicate artifact identities")
        expected_steps = _arguments(manifest, run, evidence["run_smoke"])
        texts = {}
        for index, (command, (tool, tokens)) in enumerate(zip(commands, expected_steps)):
            response = run / (tool + ".options")
            log = run / (tool + "_console.log")
            expected_argv = [str(Path(manifest["vivado_home"]) / "bin" / (tool + manifest["executable_suffix"])), "-f", str(response)]
            if (command.get("tool") != tool or command.get("argv") != expected_argv or
                    command.get("expanded_arguments") != tokens or command.get("cwd") != str(run) or
                    command.get("log") != str(log)):
                raise SimulationError("Tool execution identity/order/arguments differ from manifest")
            if command.get("log_sha256") != _hash(log):
                raise SimulationError("Raw console log hash mismatch")
            artifact = artifact_map.get(str(response), {})
            if (artifact.get("sha256") != _hash(response) or artifact.get("tokens") != tokens or
                    response.read_text(encoding="utf-8") != _response_text(tokens)):
                raise SimulationError("Response file differs from the declared ordered tool inputs")
            texts[tool] = log.read_text(encoding="utf-8", errors="replace")
            if index < minimum:
                if type(command.get("exit_code")) is not int or command["exit_code"] != 0 or command.get("timed_out") is not False or command.get("launch_error"):
                    raise SimulationError(f"Required {tool} step failed or did not complete")
                if re.search(r"(?m)^\s*(?:ERROR:|FATAL_ERROR:|FATAL:|Expected a switch)", texts[tool]):
                    raise SimulationError(f"Raw {tool} log contains a tool error")
        verdict = {"accepted": True, "status": "accepted", "phase": phase, "build_passed": True,
                   "verified": False, "coverage": {"status": "not_evaluated"}, "meets_coverage_target": False}
        limitations = qualification_limitations(texts)
        verdict["qualification_limitations"] = limitations
        if phase == "smoke":
            script = run / "smoke.tcl"
            if artifact_map.get(str(script), {}).get("sha256") != _hash(script) or script.read_text(encoding="ascii") != _script_text(manifest):
                raise SimulationError("Smoke runtime script differs from manifest")
            parsed = _qualify_smoke(parse_smoke_log(texts["xsim"], manifest["activity_contract"]), limitations)
            verdict.update(smoke_passed=True, smoke_status=parsed["status"], verified=parsed["verified"],
                           activity=parsed["activity"], verification_scope=parsed["verification_scope"])
        return verdict
    except (OSError, ValueError, KeyError, TypeError, SimulationError) as error:
        return {"accepted": False, "status": "rejected", "phase": phase, "verified": False,
                "reason": str(error), "meets_coverage_target": False}
