"""CLI transport for the opt-in DV provider; no domain authority or eager imports."""
from __future__ import annotations

import argparse
import json
from typing import Any, Callable

AddFormat = Callable[[argparse.ArgumentParser], None]
OutputFormat = Callable[..., str]
PrintPayload = Callable[[dict[str, Any], str, Callable[[dict[str, Any]], str]], None]
MAX_INLINE_RESPONSE_CHARS = 2 * 1024 * 1024


def register_chip_verification_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], add_format: AddFormat,
) -> None:
    parser = subparsers.add_parser(
        "chip-verification", help="Inspect and explicitly advance durable chip verification work.",
    )
    parser.add_argument("--workspace", default=".", help="Workspace containing the opt-in DV state.")
    parser.add_argument("--execute", action="store_true", help="Apply the requested local change; otherwise preview.")
    add_format(parser)
    actions = parser.add_subparsers(dest="chip_verification_action", required=True)

    def action(name: str, help_text: str) -> argparse.ArgumentParser:
        result = actions.add_parser(name, help=help_text)
        result.add_argument("--workspace", default=argparse.SUPPRESS, help="Workspace override.")
        result.add_argument("--execute", action="store_true", default=argparse.SUPPRESS,
                            help="Apply the requested local change; otherwise preview.")
        result.add_argument("--format", dest="chip_verification_format", choices=["markdown", "json"])
        return result

    action("doctor", "Read provider and EDA availability without initializing state.")
    imported = action("import", "Split a large specification into durable bounded work.")
    imported.add_argument("--spec", required=True, help="Local specification JSON path.")
    imported.add_argument("--name", default="DV project")
    imported.add_argument("--scope", default="Verify the features described by this specification; identify missing implementation information explicitly.")
    imported.add_argument("--chunk-chars", type=int, default=7000)
    imported.add_argument("--context-chars", type=int, default=24000)
    imported.add_argument("--flow", choices=["design-dv", "dv"], default="design-dv",
                          help="Shared specification to design and UVM, or compatibility DV-only flow.")
    action("prepare-design", "Enable shared specification contracts and both engineering tracks.")
    action("engineering-status", "Read entry views, shared contracts and design/DV readiness.")
    checks = action("check-design", "Run tool checks on an accepted RTL candidate in the background.")
    checks.add_argument("--rtl-task-id", required=True)
    checks.add_argument("--timeout", type=int, default=600)
    join = action("join-design", "Bind an accepted design and UVM manifest to the same shared contract.")
    join.add_argument("--rtl-task-id", required=True)
    join.add_argument("--manifest", required=True)
    action("status", "Read durable progress without changing a workspace.")
    design_map = action("design-map", "Read system, microarchitecture, module and feature slices.")
    design_map.add_argument("--dimension", choices=["document", "layer", "module", "feature"])
    design_map.add_argument("--limit", type=int, default=100)
    next_work = action("next", "Read eligible work without claiming it.")
    next_work.add_argument("--kind")
    claim = action("claim", "Claim a bounded domain attempt for the current host session.")
    claim.add_argument("--task-id")
    claim.add_argument("--session-id", required=True)
    claim.add_argument("--kind")
    context = action("context", "Read the bounded context of an existing attempt.")
    context.add_argument("--attempt-id", required=True)
    submit = action("submit", "Validate and submit a host-produced response for an attempt.")
    submit.add_argument("--attempt-id", required=True)
    response = submit.add_mutually_exclusive_group(required=True)
    response.add_argument("--response-file", help="Local response JSON path.")
    response.add_argument("--response", help="Inline response JSON object; prefer --response-file for generated code.")
    heartbeat = action("heartbeat", "Renew a live domain attempt without replacing its identity.")
    heartbeat.add_argument("--attempt-id", required=True)
    heartbeat.add_argument("--session-id")
    abandon = action("abandon", "Release an attempt with an explicit recovery reason.")
    abandon.add_argument("--attempt-id", required=True)
    abandon.add_argument("--reason", required=True)
    retry = action("retry", "Make a failed domain task eligible for a new attempt.")
    retry.add_argument("--task-id", required=True)
    revise = action("revise", "Revise accepted RTL or UVM component bytes using concrete tool feedback.")
    revise.add_argument("--task-id", required=True)
    revise.add_argument("--reason", required=True)
    action("export-plan", "Write the accepted test plan from durable validated artifacts.")
    action("generation-scopes", "Read bounded UVM generation scopes.")
    generate = action("generate-uvm", "Prepare native-host UVM generation work; does not launch another agent.")
    generate.add_argument("--rtl-files", nargs="+", default=[])
    generate.add_argument("--contract-task-id", help="Shared contract enables TB generation before RTL exists.")
    generate.add_argument("--top", required=True)
    generate.add_argument("--profile-path")
    generate.add_argument("--scope-id")
    generate.add_argument("--session-id", help="Optional current host session identity for the prepared attempt.")
    generate.add_argument("--context-chars", type=int)
    build = action("build-uvm", "Start an explicit asynchronous EDA build and simulation job.")
    build.add_argument("--manifest", required=True)
    build.add_argument("--simulator", default="vivado")
    build.add_argument("--timeout", type=int, default=600, help="Maximum simulator job duration in seconds.")
    build.add_argument("--run-smoke", action=argparse.BooleanOptionalAction, default=True,
                       help="Run smoke simulation after a successful build (default: enabled).")
    job = action("job-status", "Read an existing EDA job and its evidence.")
    job.add_argument("--job-id", required=True)
    action("report", "Read coverage evidence and limitations without exporting files.")
    action("install-trae", "Preview or install workspace-scoped Trae integration.")


def _render(payload: dict[str, Any]) -> str:
    return "# Chip verification\n\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```\n"


def handle_chip_verification_command(
    args: argparse.Namespace, *, output_format: OutputFormat, print_payload: PrintPayload,
) -> int | None:
    if args.command != "chip-verification":
        return None
    try:
        action = args.chip_verification_action
        if action == "install-trae":
            from .integration import install

            payload = install(args.workspace, execute=args.execute)
        else:
            # Provider import happens only after argparse has completed. Help and
            # capability discovery cannot initialize domain state or dependencies.
            from .provider import invoke

            fields = ("spec", "name", "scope", "chunk_chars", "context_chars", "dimension", "limit",
                      "kind", "task_id", "session_id", "attempt_id", "response_file", "response",
                      "reason", "rtl_files", "top", "profile_path", "scope_id", "manifest", "simulator", "job_id",
                      "timeout", "run_smoke", "flow", "contract_task_id", "rtl_task_id")
            arguments = {key: getattr(args, key) for key in fields if getattr(args, key, None) is not None}
            if "response" in arguments:
                raw = arguments["response"]
                if len(raw) > MAX_INLINE_RESPONSE_CHARS:
                    raise ValueError("inline response exceeds 2 MiB; use --response-file")
                arguments["response"] = json.loads(raw)
                if not isinstance(arguments["response"], dict):
                    raise ValueError("response must be a JSON object")
            payload = invoke(action, workspace=args.workspace, arguments=arguments, execute=args.execute)
    except (ValueError, OSError, RuntimeError, ImportError) as error:
        payload = {"ok": False, "capability_id": "chip-verification", "error": str(error)}
    print_payload(payload, output_format(args, "chip_verification_format"), _render)
    return 1 if payload.get("ok") is False else 0
