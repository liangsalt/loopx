"""LoopX capability boundary for chip DV; heavy domain dependencies load on use."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil

STATE = Path(".loopx") / "chip-verification"


def _path(root, value, *, exists=True):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError("A workspace-relative file path is required")
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Capability input/output paths must stay inside the selected workspace")
    if exists and not path.is_file():
        raise ValueError("Input file does not exist: " + str(path))
    return path


def _project(root, *, write=False):
    try:
        from .engine.workflow import DVProject
    except ImportError as error:
        raise RuntimeError('Install the optional capability: python -m pip install "loopx[chip-verification]"') from error
    return DVProject(root, initialize=write)


def _status(project):
    status = project.store.status()
    ready = project.store.ready_tasks(10000)
    if project.config.get("engineering_enabled"):
        ready = [task for task in ready if task["kind"] != "plan"]
        status["ready_count"] = len(ready)
    status.update(initialized=True, capability_id="chip-verification",
                  context_budget_chars=project.config.get("host_context_chars", project.config["context_chars"]),
                  next_tasks=[{key: t[key] for key in ("id", "kind", "title", "status")}
                              for t in ready[:8]],
                  coverage={"status": "not_evaluated", "percent": None}, signoff=False)
    try:
        project.assert_inputs_current()
    except (OSError, ValueError, RuntimeError) as error:
        status.update(inputs_current=False, input_error=str(error), next_action="import")
    else:
        status.update(inputs_current=True, next_action="claim" if status["ready_count"] else "inspect")
    if project.config.get("engineering_enabled"):
        from .engine.engineering import status as engineering_status
        status["engineering"] = engineering_status(project)
    return status


def _report(project):
    """Read proof again without turning a projection into a state mutation."""
    status = _status(project)
    from .engine.simulators.generic import validate_generic_evidence
    from .engine.verification import verify_hashes
    results = []
    for task in project.store.list_tasks(status="done"):
        if task["kind"] not in {"build", "smoke"}:
            continue
        result = {"task_id": task["id"], "phase": task["kind"], "accepted": False}
        try:
            if not status["inputs_current"]:
                raise ValueError("Specification baseline is no longer current")
            verify_hashes(task["payload"]["inputs"])
            from .engine.joining import validate_join
            validate_join(project, task["payload"]["manifest"])
            evidence = task["result"]["evidence"]
            if {entry["path"]: entry["sha256"] for entry in evidence.get("inputs", [])} != task["payload"]["inputs"]:
                raise ValueError("Run inputs differ from the accepted domain task")
            verdict = validate_generic_evidence(evidence, task["payload"]["manifest"], phase=task["kind"])
            result.update(verdict)
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            result["error"] = str(error)
        results.append(result)
    checks = []
    if project.config.get("engineering_enabled"):
        from .engine.design_checks import current_check
        for task in project.store.list_tasks(status="done", kind="rtl"):
            try:
                checks.append({"rtl_task_id": task["id"], **current_check(project, task["id"])})
            except (OSError, ValueError, RuntimeError) as error:
                checks.append({"rtl_task_id": task["id"], "qualified": False, "error": str(error)})
    return {**status, "executions": results[-20:], "execution_count": len(results), "design_checks": checks[-20:],
            "claim": "Requirement, TP and UVM candidate completion is not measured coverage. Generic XSim build/smoke does not qualify a coverage percentage."}


def invoke(action: str, *, workspace: str | Path, arguments: dict | None = None, execute: bool = False) -> dict:
    from ...control_plane.effect_runtime import effect_runtime_result

    root = Path(workspace).expanduser().resolve()
    args = dict(arguments or {})
    admission = effect_runtime_result("chip_verification.admit", {
        "action": action, "workspace": str(root), "arguments": args, "execute": execute})
    if not admission["allowed"]:
        return admission
    if action == "install-trae":
        from .integration import install
        return install(root, execute=True)
    if action == "doctor":
        return {"capability_id": "chip-verification", "initialized": (root / STATE / "project.json").is_file(),
                "optional_dependencies": {name: importlib.util.find_spec(name) is not None for name in ("jsonschema", "mcp")},
                "executables": {name: shutil.which(name) for name in ("node", "vivado", "xvlog", "xelab", "xsim", "verible-verilog-lint")},
                "tool_bindings": {name: os.environ.get(name) for name in ("XILINX_VIVADO", "LOOPX_VERIBLE_LINT")},
                "host": "trae-cn-ide", "host_ui_validated": False,
                "instruction": "Enable the project MCP server in the selected runtime. One active session can claim bounded work; child sessions are optional. Tool discovery alone is not an end-to-end verification run."}
    initialized = (root / STATE / "project.json").is_file()
    if not initialized and action != "import":
        return {"status": "not_initialized", "initialized": False, "next_action": "import",
                "capability_id": "chip-verification", "execution_performed": False}
    write = admission["mutation"]
    # Resolve external inputs before creating any domain state.
    if action == "import":
        spec = _path(root, args["spec"])
    project = _project(root, write=write)
    if write:
        from . import host_work
        host_work.initialize(project)
    if action == "import":
        from .engine.contracts import EXTRACTION_SCHEMA, PLAN_SCHEMA, EXTRACTION_INSTRUCTION, PLAN_INSTRUCTION
        from .engine.workflow import save_json
        budget = args.get("context_chars", 24000)
        if initialized and project.config.get("engineering_enabled") and args.get("flow") == "dv":
            raise ValueError("An active design/DV baseline cannot silently switch to legacy DV-only semantics")
        reserve = max(len(json.dumps(s)) for s in (EXTRACTION_SCHEMA, PLAN_SCHEMA)) + max(len(EXTRACTION_INSTRUCTION), len(PLAN_INSTRUCTION)) + 1000
        engine_budget = budget - reserve
        chunk = args.get("chunk_chars", 7000)
        result = project.import_spec(spec, name=args.get("name", "Chip verification"), scope=args.get("scope", ""),
                                     chunk_chars=chunk, context_chars=engine_budget)
        save_json(project.config_path, {**project.config, "host_context_chars": budget})
        if args.get("flow", "design-dv") == "design-dv":
            from .engine.engineering import enable
            result["engineering"] = enable(project)
        return {"status": "imported", **result, "context_budget_chars": budget, "next_action": "claim"}
    if action == "prepare-design":
        from .engine.engineering import enable
        return enable(project)
    if action == "engineering-status":
        from .engine.engineering import status
        return status(project)
    if action == "check-design":
        from .jobs import launch
        return launch(project, {"operation": "check-design", "rtl_task_id": args["rtl_task_id"],
                               "timeout": args.get("timeout", 600)})
    if action == "join-design":
        from .engine.joining import join
        return join(project, args["rtl_task_id"], _path(root, args["manifest"]))
    if action in {"status", "next"}:
        return _status(project)
    if action == "report":
        return _report(project)
    if action == "design-map":
        from .engine.hierarchy import design_map
        return design_map(project, dimension=args.get("dimension"), limit=args.get("limit", 100), refresh=False)
    if action == "generation-scopes":
        from .engine.generation import generation_scopes
        return generation_scopes(project, refresh=False)
    if action == "context":
        from .host_work import context
        return context(project, args["attempt_id"])
    if action == "claim":
        return host_work.claim(project, args)
    if action == "submit":
        response = args.get("response")
        if "response_file" in args:
            if response is not None:
                raise ValueError("Supply either response or response_file")
            path = _path(root, args["response_file"])
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("Response file exceeds 1 MiB")
            response = json.loads(path.read_text(encoding="utf-8-sig"))
        return host_work.submit(project, args["attempt_id"], response)
    if action in {"heartbeat", "abandon"}:
        item = host_work.ticket(project, args["attempt_id"], active=True)
        if action == "heartbeat":
            attempt = project.store.heartbeat(item["task_id"], item["attempt_id"], host_work.LEASE_SECONDS)
            return {"status": "running", "attempt_id": attempt["id"], "lease_until": attempt["lease_until"]}
        project.store.fail(item["task_id"], item["attempt_id"], args.get("reason", "Host worker abandoned attempt"), retryable=True)
        return {"status": "released", "task_id": item["task_id"], "next_action": "claim"}
    if action == "retry":
        original = project.store.get_task(args["task_id"])
        if original["kind"] in {"rtl-check", "build", "smoke"}:
            return {"status": "tool_retry_required", "task_id": original["id"],
                    "next_action": "check-design" if original["kind"] == "rtl-check" else "build-uvm",
                    "arguments": ({"rtl_task_id": original["payload"]["rtl_task_id"]} if original["kind"] == "rtl-check"
                                  else {"manifest": original["payload"]["manifest"]})}
        task = project.store.requeue(args["task_id"])
        return {"status": task["status"], "task_id": task["id"], "next_action": "claim"}
    if action == "revise":
        return host_work.revise(project, args["task_id"], args["reason"])
    if action == "export-plan":
        output = _path(root, args.get("output", "tp.json"), exists=False)
        return project.export_plan(output)
    if action == "generate-uvm":
        params = {**args, "rtl_files": [str(_path(root, name)) for name in args.get("rtl_files", [])]}
        if args.get("profile_path"):
            params["profile_path"] = str(_path(root, args["profile_path"]))
        return host_work.generate(project, params)
    if action == "build-uvm":
        if args.get("simulator", "vivado") not in {"vivado", "xsim"}:
            raise ValueError("This provider currently qualifies Vivado XSim; another simulator needs its own evidence adapter")
        from .jobs import launch
        return launch(project, {"manifest": str(_path(root, args["manifest"])),
                               "run_smoke": args.get("run_smoke", True), "timeout": args.get("timeout", 600)})
    if action == "job-status":
        from .jobs import status
        return status(project, args["job_id"])
    raise ValueError("Unsupported chip verification action: " + action)
