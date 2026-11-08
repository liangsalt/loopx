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
    from .engine.source_structure import PARTITIONING

    status = project.store.status()
    ready = project.store.ready_tasks(10000)
    reviews = [task for task in ready if task["kind"] == "watchdog-review"]
    ready = [task for task in ready if task["kind"] != "watchdog-review"]
    if project.config.get("engineering_enabled"):
        ready = [task for task in ready if task["kind"] != "plan"]
        status["ready_count"] = len(ready)
    status.update(initialized=True, capability_id="chip-verification",
                  ready_count=len(ready),
                  watchdog_reviews=[{"task_id": t["id"], "title": t["title"]} for t in reviews[:8]],
                  watchdog_review_count=len(reviews),
                  context_budget_chars=None,
                  decomposition="module_function_relationship_stage",
                  next_tasks=[{key: t[key] for key in ("id", "kind", "title", "status")}
                              for t in ready[:8]],
                  coverage={"status": "not_evaluated", "percent": None}, signoff=False)
    status["partitioning"] = project.config.get("partitioning")
    status["partitioning_migration_required"] = status["partitioning"] != PARTITIONING
    try:
        project.assert_inputs_current()
    except (OSError, ValueError, RuntimeError) as error:
        status.update(inputs_current=False, input_error=str(error), next_action="import")
    else:
        status.update(inputs_current=True, next_action="claim" if status["ready_count"] else "inspect")
    if status["partitioning_migration_required"]:
        status.update(next_action="import", migration_instruction=(
            "Import the same specification through the normal entrypoint to replace legacy character-based "
            "partitioning. Historical attempts and artifacts are retained; derived work is invalidated for the new decomposition."))
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
                "executables": {name: shutil.which(name) for name in ("node", "vivado", "xvlog", "xelab", "xsim", "verible-verilog-lint", "xrun", "imc", "jg", "genus")},
                "tool_bindings": {name: os.environ.get(name) for name in ("XILINX_VIVADO", "LOOPX_VERIBLE_LINT")},
                "host": "trae-cn-ide", "host_ui_validated": False,
                "instruction": "Enable the project MCP server in the selected runtime. One active session can claim a module, function or engineering-stage task; child sessions are optional. Tool discovery alone is not an end-to-end verification run."}
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
        from .engine.workflow import save_json
        if initialized and project.config.get("engineering_enabled") and args.get("flow") == "dv":
            raise ValueError("An active design/DV baseline cannot silently switch to legacy DV-only semantics")
        # Legacy size options remain accepted by admission but never determine
        # domain task boundaries or whether the runtime may read a task.
        result = project.import_spec(spec, name=args.get("name", "Chip verification"), scope=args.get("scope", ""))
        save_json(project.config_path, {**project.config, "context_chars": None, "host_context_chars": None})
        if args.get("flow", "design-dv") == "design-dv":
            from .engine.engineering import enable
            result["engineering"] = enable(project)
        return {"status": "imported", **result, "context_budget_chars": None,
                "decomposition": "module_function_relationship_stage", "next_action": "claim"}
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
    if action in {"run-register", "event-ingest", "event-status", "event-read"}:
        from .engine import event_inbox
        if action == "run-register":
            project.assert_inputs_current()
            return event_inbox.register(project, args)
        if action == "event-ingest":
            return event_inbox.ingest(project, _object_argument(root, args, "event"))
        if action == "event-read":
            return event_inbox.read_event(project, args["cursor"], offset=args.get("offset", 0),
                                          limit=args.get("limit", 8000))
        return event_inbox.status(project, run_id=args.get("run_id"),
                                  after=args.get("after", 0), limit=args.get("limit", 50))
    if action.startswith("watchdog-"):
        from .engine import watchdog
        if action == "watchdog-status":
            from .watchdog_service import read_service_status, dispatch_status
            return {**watchdog.status(project), "service": read_service_status(project),
                    "review_dispatches": dispatch_status(project)}
        if action == "watchdog-tick":
            return watchdog.tick(project)
        if action == "watchdog-policy":
            return watchdog.configure(project, _object_argument(root, args, "policy"), args["reason"])
        if action == "watchdog-apply":
            project.assert_inputs_current()
            return watchdog.apply(project, args["review_task_id"])
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
        from .engine.generation import simulation_target

        manifest = _path(root, args["manifest"])
        value = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("Build manifest must be a JSON object")
        requested = simulation_target(args["simulator"]) if args.get("simulator") is not None else None
        declared = simulation_target(value["simulator"]) if "simulator" in value else None
        if requested and declared and requested != declared:
            raise ValueError("Requested simulator conflicts with the manifest; preserve the selected execution target")
        target = requested or declared or "vivado_xsim"
        if target == "cadence_xcelium":
            project.assert_inputs_current()
            if value.get("execution") not in (None, "external"):
                raise ValueError("Cadence manifests must declare external execution; this provider has no native Cadence evidence adapter")
            relative = manifest.relative_to(root).as_posix()
            runner = "eda/cadence_runner.py"
            return {"status": "external_execution_required", "simulator": target,
                    "manifest": relative, "manifest_contract_declared": declared == target and value.get("execution") == "external",
                    "runner": runner, "runner_available": (root / runner).is_file(),
                    "commands": [["python", runner, phase, "--project", relative] + (["--execute"] if phase == "run" else [])
                                 for phase in ("inspect", "plan", "run")],
                    "execution_performed": False, "compiled": False, "simulated": False,
                    "coverage_verified": False, "signoff": False,
                    "instruction": "Use the staged Cadence kit on the EDA host with an explicit cadence_xcelium/external manifest. Inspect and freeze the complete RTL/TB, shared contract, constraints, coverage model and site-qualified scripts first. No native build job was launched. Publish run observations through run-register/event-ingest; native reports still require validation, and completed events are not acceptance."}
        if value.get("execution") == "external":
            raise ValueError("An external build manifest needs an explicit supported external simulator target")
        from .jobs import launch
        return launch(project, {"manifest": str(manifest),
                               "run_smoke": args.get("run_smoke", True), "timeout": args.get("timeout", 600)})
    if action == "job-status":
        from .jobs import status
        return status(project, args["job_id"])
    raise ValueError("Unsupported chip verification action: " + action)


def _object_argument(root, args, key):
    value = args.get(key)
    if key + "_file" in args:
        path = _path(root, args[key + "_file"])
        if path.stat().st_size > 65536:
            raise ValueError(key + " file exceeds 64 KiB; reference bulky evidence instead")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(key + " must be an object")
    return value
