"""Project-bound stdio transport for the chip-verification capability.

The selected runtime owns inference and sessions. This module exposes the same
provider used by the CLI; it neither launches a model nor owns DV transitions.
"""
from __future__ import annotations

import argparse
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any


def create_server(workspace: str | Path, *, invoker: Callable[..., dict] | None = None):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

    root = Path(workspace).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("workspace must be an existing directory")
    if invoker is None:
        from .provider import invoke

        invoker = invoke

    parameters: dict[str, frozenset[str]] = {}

    class StrictServer(FastMCP):
        async def call_tool(self, name: str, arguments: dict[str, Any]):
            arguments = dict(arguments)
            # Old hosts may resend these optional arguments from a cached tool
            # schema. Accept their former types, without advertising or using a
            # character budget to shape the task.
            legacy = {"dv_import": ("context_chars",), "dv_generate_uvm": ("context_chars",)}
            for key in legacy.get(name, ()):
                value = arguments.pop(key, None)
                if value is not None and (type(value) is not int or value < 1):
                    raise ToolError(f"Deprecated {key} must be a positive integer when supplied")
            unknown = set(arguments) - parameters.get(name, set(arguments))
            if unknown:
                raise ToolError(f"Unsupported {name} arguments: {', '.join(sorted(unknown))}")
            return await super().call_tool(name, arguments)

    server = StrictServer(
        "loopx-dv",
        instructions=(
            "LoopX chip-verification state is independent of this MCP connection. "
            "One selected Agent runtime reads dv_status/dv_next, claims a module/function/stage task, "
            "reads dv_context and submits its result. Fresh sessions or optional child "
            "sessions relieve context pressure; neither workflow requires two agents. "
            "A session_id is a caller-supplied execution label, not verified TRAE identity. "
            "Poll local build receipts with dv_job_status; external adapters deliver dv_ingest_event. "
            "Read dv_watchdog_status for durable incidents and explicitly claim kind=watchdog-review "
            "in a separate session. A pending review is not an automatic host wake-up."
        ),
        log_level="WARNING",
    )

    def tool(function):
        parameters[function.__name__] = frozenset(inspect.signature(function).parameters)
        return server.tool()(function)

    def call(action: str, arguments: dict | None = None, *, execute: bool = False) -> dict:
        supplied = {key: value for key, value in (arguments or {}).items() if value is not None}
        return invoker(action, workspace=root, arguments=supplied, execute=execute)

    @tool
    def dv_status() -> dict:
        """Read compact durable DV state; safe for the main Agent."""
        return call("status")

    @tool
    def dv_next() -> dict:
        """Read the next task summary without loading its spec/context."""
        return call("next")

    @tool
    def dv_claim(session_id: str, task_id: str | None = None, kind: str | None = None) -> dict:
        """Worker: claim one task using a unique execution label; retain attempt_id."""
        if not session_id.strip():
            raise ValueError("session_id must be a nonempty execution label")
        return call("claim", {"session_id": session_id, "task_id": task_id, "kind": kind}, execute=True)

    @tool
    def dv_context(attempt_id: str) -> dict:
        """Read complete task-scoped inputs and the output schema for one active attempt."""
        return call("context", {"attempt_id": attempt_id})

    @tool
    def dv_submit(attempt_id: str, response: dict | None = None, response_file: str | None = None) -> dict:
        """Worker: validate and submit a response or a response file within the workspace."""
        if (response is None) == (response_file is None):
            raise ValueError("provide exactly one of response and response_file")
        return call("submit", {"attempt_id": attempt_id, "response": response,
                               "response_file": response_file}, execute=True)

    @tool
    def dv_abandon(attempt_id: str, reason: str) -> dict:
        """Release an unsuccessful attempt with a concrete reason, without claiming completion."""
        return call("abandon", {"attempt_id": attempt_id, "reason": reason}, execute=True)

    @tool
    def dv_heartbeat(attempt_id: str) -> dict:
        """Worker: renew an active attempt lease while processing its domain task."""
        return call("heartbeat", {"attempt_id": attempt_id}, execute=True)

    @tool
    def dv_import(spec: str, flow: str = "design-dv") -> dict:
        """Import a spec file by path; do not paste the full spec into chat."""
        return call("import", {"spec": spec, "flow": flow}, execute=True)

    @tool
    def dv_prepare_design() -> dict:
        """Enable shared contracts, design and UVM tracks for an already imported specification."""
        return call("prepare-design", execute=True)

    @tool
    def dv_engineering_status() -> dict:
        """Read shared contract revisions and readiness from any specification entry level."""
        return call("engineering-status")

    @tool
    def dv_check_design(rtl_task_id: str, timeout: int = 600) -> dict:
        """Start durable RTL tool checks; unavailable lint or CDC is not a passing result."""
        return call("check-design", {"rtl_task_id": rtl_task_id, "timeout": timeout}, execute=True)

    @tool
    def dv_join_design(rtl_task_id: str, manifest: str) -> dict:
        """Bind design and generated UVM files to a common contract before simulation."""
        return call("join-design", {"rtl_task_id": rtl_task_id, "manifest": manifest}, execute=True)

    @tool
    def dv_design_map() -> dict:
        """Read system, microarchitecture and module relationships from indexed design state."""
        return call("design-map")

    @tool
    def dv_generation_scopes() -> dict:
        """Read available UVM function/interface scopes before preparing generation work."""
        return call("generation-scopes")

    @tool
    def dv_generate_uvm(top: str, rtl_files: list[str] | None = None, profile_path: str | None = None,
                        scope_id: str | None = None,
                        session_id: str | None = None, contract_task_id: str | None = None) -> dict:
        """Prepare UVM from shared interfaces or existing RTL; expected behavior comes from spec."""
        return call("generate-uvm", {"rtl_files": rtl_files or [], "top": top,
                                    "profile_path": profile_path, "scope_id": scope_id,
                                    "session_id": session_id,
                                    "contract_task_id": contract_task_id}, execute=True)

    @tool
    def dv_build_uvm(manifest: str, simulator: str = "vivado", run_smoke: bool = True) -> dict:
        """Start an EDA build job and return its receipt promptly; poll dv_job_status."""
        return call("build-uvm", {"manifest": manifest, "simulator": simulator,
                                  "run_smoke": run_smoke}, execute=True)

    @tool
    def dv_job_status(job_id: str) -> dict:
        """Read an asynchronous EDA job receipt and bounded diagnostics."""
        return call("job-status", {"job_id": job_id})

    @tool
    def dv_register_run(run_id: str, task_id: str, tool: str, source: str, input_digest: str,
                        attempt_id: str | None = None, heartbeat_seconds: int = 300,
                        timeout_seconds: int = 86400, queue_timeout_seconds: int = 3600) -> dict:
        """Pin external work to current task inputs before the adapter dispatches it; no EDA launch."""
        return call("run-register", {"run_id": run_id, "task_id": task_id, "tool": tool,
                    "source": source, "input_digest": input_digest, "attempt_id": attempt_id,
                    "heartbeat_seconds": heartbeat_seconds, "timeout_seconds": timeout_seconds,
                    "queue_timeout_seconds": queue_timeout_seconds}, execute=True)

    @tool
    def dv_ingest_event(event: dict) -> dict:
        """Durably ingest a bounded adapter event; tool completion cannot certify DV acceptance."""
        return call("event-ingest", {"event": event}, execute=True)

    @tool
    def dv_event_status(run_id: str | None = None, after: int = 0, limit: int = 50) -> dict:
        """Read an event cursor and observed runs; retain the cursor outside the session."""
        return call("event-status", {"run_id": run_id, "after": after, "limit": limit})

    @tool
    def dv_watchdog_status() -> dict:
        """Explain current progress/health incidents, holds, review tasks and last control tick."""
        return call("watchdog-status")

    @tool
    def dv_event_read(cursor: int, offset: int = 0, limit: int = 8000) -> dict:
        """Recall an immutable event body in bounded slices; compare its fingerprint across reads."""
        return call("event-read", {"cursor": cursor, "offset": offset, "limit": limit})

    @tool
    def dv_watchdog_tick() -> dict:
        """Run one independent control pass; repeated calls are durable and restart safe."""
        return call("watchdog-tick", execute=True)

    @tool
    def dv_watchdog_policy(policy: dict, reason: str) -> dict:
        """Version bounded detection/backoff thresholds; cannot rewrite task goals or execute code."""
        return call("watchdog-policy", {"policy": policy, "reason": reason}, execute=True)

    @tool
    def dv_watchdog_apply(review_task_id: str) -> dict:
        """Apply only an accepted review; stale evidence or exhausted recovery budgets hold work."""
        return call("watchdog-apply", {"review_task_id": review_task_id}, execute=True)

    @tool
    def dv_report() -> dict:
        """Read coverage evidence and limitations; planning progress is not measured coverage."""
        return call("report")

    @tool
    def dv_doctor() -> dict:
        """Inspect provider and simulator availability without starting a simulation."""
        return call("doctor")

    @tool
    def dv_retry(task_id: str) -> dict:
        """Requeue a recoverable failed task after resolving its reported cause."""
        return call("retry", {"task_id": task_id}, execute=True)

    @tool
    def dv_revise(task_id: str, reason: str) -> dict:
        """Repair accepted RTL or UVM component after tool failure; invalidate dependent evidence."""
        return call("revise", {"task_id": task_id, "reason": reason}, execute=True)

    @tool
    def dv_export_plan() -> dict:
        """Export the durable test plan as a reviewable artifact."""
        return call("export-plan", execute=True)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    create_server(args.workspace).run(transport="stdio")


if __name__ == "__main__":
    main()
