"""Public transport proof for control behavior, without a simulator/model claim."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from loopx.capabilities.chip_verification.provider import invoke
from loopx.capabilities.chip_verification.engine.workflow import DVProject


ROOT = Path(__file__).resolve().parents[2]


def call(root, action, **arguments):
    return invoke(action, workspace=root, arguments=arguments, execute=True)


def scenario(root):
    (root / "spec.json").write_text(json.dumps({"module": "reset_controller", "requirement":
        "Deassert reset synchronously in each clock domain; retain independent source requirements."}), encoding="utf-8")
    call(root, "import", spec="spec.json", flow="dv")
    claimed = call(root, "claim", session_id="design-worker")
    call(root, "abandon", attempt_id=claimed["attempt_id"], reason="Inspect reset constraints before another compile")
    task_id = claimed["task_id"]
    for index in range(3):
        registered = call(root, "run-register", run_id=f"run-{index}", task_id=task_id,
                          tool="fixture-compiler", source="fixture-adapter", input_digest="a" * 64)
        event = {"id": f"failure-{index}", "source": "fixture-adapter", "run_id": f"run-{index}",
                 "baseline_id": registered["run"]["baseline_id"], "sequence": 1, "type": "failed",
                 "data": {"failure_signature": "unconstrained-reset", "message": "Check reset crossing assumptions."}}
        receipt = call(root, "event-ingest", event=event)
        assert call(root, "event-ingest", event=event)["duplicate"] is True
        assert receipt["coverage_verified"] is False
    return task_id


def test_external_events_hold_review_and_recovery_strategy_is_adopted(tmp_path):
    target = scenario(tmp_path)
    tick = call(tmp_path, "watchdog-tick")
    assert len(tick["review_task_ids"]) == 1
    review = tick["review_task_ids"][0]
    assert call(tmp_path, "watchdog-tick")["review_task_ids"] == []
    assert call(tmp_path, "claim", session_id="design-worker")["status"] == "no_ready_work"
    with pytest.raises(RuntimeError, match="[Hh]old"):
        call(tmp_path, "run-register", run_id="repeat-blindly", task_id=target,
             tool="fixture-compiler", source="fixture-adapter", input_digest="a" * 64)
    with pytest.raises(RuntimeError, match="[Ii]ndependent|session"):
        call(tmp_path, "claim", task_id=review, kind="watchdog-review", session_id="design-worker")
    claim = call(tmp_path, "claim", task_id=review, kind="watchdog-review", session_id="independent-review")
    context = call(tmp_path, "context", attempt_id=claim["attempt_id"])
    assert context["context_chars"] <= context["context_budget_chars"]
    snapshot = DVProject(tmp_path).store.get_task(review)["payload"]["snapshot"]
    strategy = "Inspect the source reset-domain requirements and pin the missing constraints before a different compile."
    response = {"diagnosis": "Three equivalent compiles failed on the same reset constraint with unchanged inputs.",
                "evidence_refs": snapshot["evidence_refs"][:2], "strategy": strategy,
                "action": "resume", "next_check_seconds": 60}
    assert call(tmp_path, "submit", attempt_id=claim["attempt_id"], response=response)["status"] == "accepted"
    applied = call(tmp_path, "watchdog-apply", review_task_id=review)
    assert applied["status"] == "resumed"
    assert call(tmp_path, "watchdog-apply", review_task_id=review)["replayed"] is True
    next_work = call(tmp_path, "claim", task_id=target, session_id="worker-after-review")
    resumed_context = call(tmp_path, "context", attempt_id=next_work["attempt_id"])
    assert strategy in resumed_context["prompt"]
    report = call(tmp_path, "report")
    assert report["coverage"]["percent"] is None and report["signoff"] is False


def test_real_mcp_reconnect_recalls_event_and_watchdog_state(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    target = scenario(tmp_path)
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "loopx.capabilities.chip_verification.mcp_server", "--workspace", str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1", "LOOPX_USAGE_PING": "0"}, cwd=str(tmp_path))

    async def tool(session, name, args=None):
        result = await session.call_tool(name, args or {})
        assert not result.isError, result.content
        return result.structuredContent or json.loads(result.content[0].text)

    async def exercise():
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
            await session.initialize()
            registered = await tool(session, "dv_register_run", {"run_id": "mcp-run", "task_id": target,
                "tool": "fixture-compiler", "source": "mcp-fixture", "input_digest": "a" * 64})
            event = {"id": "mcp-failed", "run_id": "mcp-run", "source": "mcp-fixture",
                     "baseline_id": registered["run"]["baseline_id"], "sequence": 1, "type": "failed",
                     "data": {"failure_signature": "unconstrained-reset"}}
            assert (await tool(session, "dv_ingest_event", {"event": event}))["disposition"] == "applied"
            assert (await tool(session, "dv_ingest_event", {"event": event}))["duplicate"] is True
            result = await tool(session, "dv_event_status", {"limit": 1})
            assert result["has_more"] is True
            cursor = result["events"][0]["cursor"]
            detail = await tool(session, "dv_event_read", {"cursor": cursor, "limit": 80})
            assert detail["has_more"] is True and len(detail["text"]) == 80
            tick = await tool(session, "dv_watchdog_tick")
            assert len(tick["review_task_ids"]) == 1
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
            await session.initialize()
            assert (await tool(session, "dv_event_read", {"cursor": cursor, "limit": 80})) == detail
            assert len((await tool(session, "dv_watchdog_status"))["active_holds"]) == 1
            assert (await tool(session, "dv_watchdog_tick"))["review_task_ids"] == []
            bad = await session.call_tool("dv_watchdog_policy", {"policy": {"max_resumes": 999}, "reason": "Bypass bound"})
            assert bad.isError
    asyncio.run(exercise())


def test_cli_preview_and_registration_share_live_owner(tmp_path):
    (tmp_path / "spec.json").write_text('{"requirements":["Reset clears the count."]}', encoding="utf-8")
    call(tmp_path, "import", spec="spec.json", flow="dv")
    task = call(tmp_path, "status")["next_tasks"][0]["id"]
    command = [sys.executable, "-c", "from loopx.entrypoint import main; raise SystemExit(main())",
               "chip-verification", "--workspace", str(tmp_path),
               "run-register", "--run-id", "cli-run", "--task-id", task,
               "--tool", "portable-emitter", "--source", "fixture", "--input-digest", "b" * 64, "--format", "json"]
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1", "LOOPX_USAGE_PING": "0"}
    preview = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=40)
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["status"] == "preview"
    assert not DVProject(tmp_path).store.list_runs()
    executed = subprocess.run([*command, "--execute"], env=env, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=40)
    assert executed.returncode == 0, executed.stderr
    assert json.loads(executed.stdout)["run"]["id"] == "cli-run"
    assert call(tmp_path, "event-status", run_id="cli-run")["run"]["status"] == "queued"
