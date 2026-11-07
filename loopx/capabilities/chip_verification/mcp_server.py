"""Project-bound stdio transport for the chip-verification capability.

TRAE owns inference and subagent contexts. This module only exposes the same
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
            unknown = set(arguments) - parameters.get(name, set(arguments))
            if unknown:
                raise ToolError(f"Unsupported {name} arguments: {', '.join(sorted(unknown))}")
            return await super().call_tool(name, arguments)

    server = StrictServer(
        "loopx-dv",
        instructions=(
            "LoopX chip-verification state is independent of this MCP connection. "
            "The main Agent reads dv_status/dv_next summaries and delegates one task "
            "to loopx-dv-worker. Only the worker reads dv_context. "
            "A session_id is a caller-supplied execution label, not verified TRAE identity. "
            "Poll asynchronous build receipts with dv_job_status."
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
        """Worker only: read bounded inputs and the output schema for an attempt."""
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
        """Worker: renew an active attempt lease while doing bounded work."""
        return call("heartbeat", {"attempt_id": attempt_id}, execute=True)

    @tool
    def dv_import(spec: str, context_chars: int = 24000) -> dict:
        """Import a spec file by path; do not paste the full spec into chat."""
        return call("import", {"spec": spec, "context_chars": context_chars}, execute=True)

    @tool
    def dv_design_map() -> dict:
        """Read system, microarchitecture and module relationships from indexed design state."""
        return call("design-map")

    @tool
    def dv_generation_scopes() -> dict:
        """Read available UVM generation scopes before preparing bounded generation work."""
        return call("generation-scopes")

    @tool
    def dv_generate_uvm(rtl_files: list[str], top: str, profile_path: str | None = None,
                        scope_id: str | None = None, context_chars: int | None = None,
                        session_id: str | None = None) -> dict:
        """Prepare UVM generation work from RTL and an explicit scope; the host supplies inference."""
        return call("generate-uvm", {"rtl_files": rtl_files, "top": top,
                                    "profile_path": profile_path, "scope_id": scope_id,
                                    "context_chars": context_chars, "session_id": session_id}, execute=True)

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
