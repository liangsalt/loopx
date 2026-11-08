"""TRAE facade installation and protocol contracts, without claiming IDE UI evidence."""
from __future__ import annotations

import asyncio
import json
import re

import pytest

from loopx.capabilities.chip_verification import integration
from loopx.capabilities.chip_verification.mcp_server import create_server


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def test_install_preview_merge_idempotency_and_host_readback(tmp_path):
    directory = tmp_path / ".trae"
    directory.mkdir()
    config = directory / "mcp.json"
    original = {"mcpServers": {"customer": {"command": "customer-tool", "args": ["keep"]}},
                "customerSetting": {"enabled": True}}
    config.write_text(json.dumps(original), encoding="utf-8")
    before = snapshot(tmp_path)
    preview = integration.install(tmp_path)
    assert preview["status"] == "preview"
    assert snapshot(tmp_path) == before
    result = integration.install(tmp_path, execute=True)
    assert result["status"] == "installed" and result["files_verified"]
    assert result["host_verified"] is False
    installed = json.loads(config.read_text())
    assert installed["mcpServers"]["customer"] == original["mcpServers"]["customer"]
    assert installed["customerSetting"] == original["customerSetting"]
    entry = installed["mcpServers"]["loopx-dv"]
    assert entry["args"] == ["-m", "loopx.capabilities.chip_verification.mcp_server",
                             "--workspace", "${workspaceFolder}"]
    assert not any(c.isspace() for c in entry["command"])
    after = snapshot(tmp_path)
    repeat = integration.install(tmp_path, execute=True)
    assert repeat["status"] == "installed"
    assert all(item["status"] == "unchanged" for item in repeat["files"])
    assert snapshot(tmp_path) == after


@pytest.mark.parametrize("config", [[], {"mcpServers": []}, {"mcpServers": None},
                                     {"mcpServers": {"loopx-dv": None}},
                                     {"mcpServers": {"loopx-dv": {"command": "customer"}}}])
def test_invalid_or_user_owned_mcp_causes_no_writes(tmp_path, config):
    (tmp_path / ".trae").mkdir()
    (tmp_path / ".trae/mcp.json").write_text(json.dumps(config), encoding="utf-8")
    before = snapshot(tmp_path)
    result = integration.install(tmp_path, execute=True)
    assert result["status"] == "blocked" and result["ok"] is False
    assert snapshot(tmp_path) == before


def test_invalid_json_is_preserved(tmp_path):
    (tmp_path / ".trae").mkdir()
    (tmp_path / ".trae/mcp.json").write_text('{"mcpServers":', encoding="utf-8")
    before = snapshot(tmp_path)
    assert integration.install(tmp_path, execute=True)["status"] == "blocked"
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("target", [".trae/skills/loopx-dv/SKILL.md", ".trae/agents/loopx-dv-worker.md",
                                   ".trae/rules/loopx-dv.md", ".trae/commands/loopx-dv.md"])
def test_hand_edits_and_unmanaged_files_are_not_overwritten(tmp_path, target):
    path = tmp_path / target
    path.parent.mkdir(parents=True)
    path.write_text("user content", encoding="utf-8")
    before = snapshot(tmp_path)
    assert integration.install(tmp_path, execute=True)["status"] == "blocked"
    assert snapshot(tmp_path) == before
    path.unlink()
    assert integration.install(tmp_path, execute=True)["status"] == "installed"
    path.write_text(path.read_text(encoding="utf-8") + "\ncustomer edit", encoding="utf-8")
    before = snapshot(tmp_path)
    assert integration.install(tmp_path, execute=True)["status"] == "blocked"
    assert snapshot(tmp_path) == before


def test_managed_server_edit_and_marker_corruption_preserve_config(tmp_path):
    integration.install(tmp_path, execute=True)
    config = tmp_path / ".trae/mcp.json"
    value = json.loads(config.read_text())
    value["mcpServers"]["loopx-dv"]["args"].append("customer-option")
    config.write_text(json.dumps(value), encoding="utf-8")
    before = snapshot(tmp_path)
    assert integration.install(tmp_path, execute=True)["status"] == "blocked"
    assert snapshot(tmp_path) == before


def test_command_with_spaces_uses_basename_and_interpreter_path(tmp_path, monkeypatch):
    executable = tmp_path / "Python Runtime" / "python.exe"
    monkeypatch.setattr(integration.sys, "executable", str(executable))
    result = integration.install(tmp_path, execute=True)
    assert result["status"] == "installed"
    entry = json.loads((tmp_path / ".trae/mcp.json").read_text())["mcpServers"]["loopx-dv"]
    assert entry["command"] == "python.exe"
    assert entry["env"]["PATH"].startswith(str(executable.parent))


def test_explicit_eda_bindings_survive_installed_host_restart(tmp_path, monkeypatch):
    home = str(tmp_path / "eda-tool-home")
    lint = str(tmp_path / "verible-verilog-lint")
    monkeypatch.setenv("XILINX_VIVADO", home)
    monkeypatch.setenv("LOOPX_VERIBLE_LINT", lint)
    integration.install(tmp_path, execute=True)
    environment = json.loads((tmp_path / ".trae/mcp.json").read_text())["mcpServers"]["loopx-dv"]["env"]
    assert environment["XILINX_VIVADO"] == home
    assert environment["LOOPX_VERIBLE_LINT"] == lint


def test_write_failure_restores_existing_files(tmp_path, monkeypatch):
    real = integration._atomic_write
    attempts = 0

    def fail_once(path, content):
        nonlocal attempts
        attempts += 1
        if attempts == 3:
            raise OSError("simulated interrupted installation")
        real(path, content)

    monkeypatch.setattr(integration, "_atomic_write", fail_once)
    before = snapshot(tmp_path)
    result = integration.install(tmp_path, execute=True)
    assert result["status"] == "blocked"
    assert snapshot(tmp_path) == before


def test_installed_resources_use_native_subagent_and_no_hidden_cli(tmp_path):
    integration.install(tmp_path, execute=True)
    worker = (tmp_path / ".trae/agents/loopx-dv-worker.md").read_text(encoding="utf-8")
    assert worker.startswith("---\nname: loopx-dv-worker\n")
    assert "mcpServers:\n  - loopx-dv" in worker
    assert "mcp__loopx-dv__dv_context" in worker
    assert "mcp__loopx-dv__dv_heartbeat" in worker
    assert "model:" not in worker
    assert "traex exec" not in worker
    assert len((tmp_path / ".trae/rules/loopx-dv.md").read_text(encoding="utf-8")) < 1000


def test_mcp_schemas_fail_closed_and_writes_are_explicit(tmp_path):
    calls = []

    def invoke(action, **kwargs):
        calls.append((action, kwargs))
        return {"ok": True, "action": action}

    server = create_server(tmp_path, invoker=invoke)

    async def exercise():
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert {"dv_register_run", "dv_ingest_event", "dv_event_status", "dv_watchdog_tick",
                "dv_watchdog_status", "dv_watchdog_policy", "dv_watchdog_apply"} <= tools.keys()
        assert {"dv_prepare_design", "dv_engineering_status", "dv_check_design", "dv_join_design"} <= tools.keys()
        assert tools["dv_import"].inputSchema["properties"]["flow"]["default"] == "design-dv"
        assert "rtl_files" not in tools["dv_generate_uvm"].inputSchema.get("required", [])
        assert "context_chars" not in tools["dv_import"].inputSchema["properties"]
        assert "context_chars" not in tools["dv_generate_uvm"].inputSchema["properties"]
        assert tools["dv_build_uvm"].inputSchema["properties"]["run_smoke"]["default"] is True
        assert tools["dv_build_uvm"].inputSchema["properties"]["simulator"]["default"] is None
        await server.call_tool("dv_status", {})
        await server.call_tool("dv_claim", {"session_id": "trae-worker-one", "task_id": "task-one"})
        await server.call_tool("dv_context", {"attempt_id": "attempt-one"})
        await server.call_tool("dv_submit", {"attempt_id": "attempt-one", "response": {"result": "ok"}})
        with pytest.raises(Exception, match="Unsupported"):
            await server.call_tool("dv_status", {"workspace": "elsewhere"})
        with pytest.raises(Exception, match="Unsupported"):
            await server.call_tool("dv_build_uvm", {"manifest": "m.json", "command": "arbitrary-shell"})
        with pytest.raises(Exception, match="exactly one"):
            await server.call_tool("dv_submit", {"attempt_id": "attempt-one"})
        with pytest.raises(Exception, match="nonempty"):
            await server.call_tool("dv_claim", {"session_id": "  "})

    asyncio.run(exercise())
    assert [action for action, _ in calls] == ["status", "claim", "context", "submit"]
    assert [kwargs["execute"] for _, kwargs in calls] == [False, True, False, True]
    assert all(kwargs["workspace"] == tmp_path.resolve() for _, kwargs in calls)


def test_cached_mcp_character_arguments_are_ignored_without_advertising_them(tmp_path):
    calls = []

    def invoke(action, **kwargs):
        calls.append((action, kwargs["arguments"]))
        return {"ok": True}

    server = create_server(tmp_path, invoker=invoke)

    async def exercise():
        await server.call_tool("dv_import", {"spec": "spec.json"})
        await server.call_tool("dv_import", {"spec": "spec.json", "context_chars": 1})
        await server.call_tool("dv_generate_uvm", {"top": "dut", "contract_task_id": "c1", "context_chars": 1500000})
        with pytest.raises(Exception, match="positive integer"):
            await server.call_tool("dv_import", {"spec": "spec.json", "context_chars": False})

    asyncio.run(exercise())
    assert calls[0] == calls[1] == ("import", {"spec": "spec.json", "flow": "design-dv"})
    assert calls[2] == ("generate-uvm", {"top": "dut", "rtl_files": [], "contract_task_id": "c1"})


def test_real_installed_stdio_reconnect_validation_and_idempotent_submit(tmp_path):
    """Exercise the installed command and real provider, not an in-process fake."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    workspace = tmp_path / "chip 项目 with spaces"
    workspace.mkdir()
    (workspace / "spec.json").write_text(json.dumps({
        "module": "timer",
        "requirements": ["Reset sets count to zero.", "Disabled timer retains its count."],
    }), encoding="utf-8")
    (workspace / "cadence-build.json").write_text(json.dumps({
        "schema_version": 1, "simulator": "cadence_xcelium", "execution": "external",
        "rtl_files": [], "tb_files": [], "top": "tb_top",
    }), encoding="utf-8")
    assert integration.install(workspace, execute=True)["files_verified"]
    entry = json.loads((workspace / ".trae/mcp.json").read_text())["mcpServers"]["loopx-dv"]
    params = StdioServerParameters(
        command=entry["command"],
        args=[arg.replace("${workspaceFolder}", str(workspace)) for arg in entry["args"]],
        env=entry["env"], cwd=str(workspace),
    )

    async def call(session, name, arguments=None):
        result = await session.call_tool(name, arguments or {})
        assert not result.isError, result.content
        payload = result.structuredContent or json.loads(result.content[0].text)
        assert payload.get("ok") is not False, payload
        assert payload.get("allowed") is not False, payload
        return payload

    async def exercise():
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert "dv_claim" in {tool.name for tool in tools.tools}
            assert (await call(session, "dv_status"))["initialized"] is False
            assert (await call(session, "dv_import", {"spec": "spec.json"}))["status"] == "imported"
            external = await call(session, "dv_build_uvm", {"manifest": "cadence-build.json"})
            assert external["status"] == "external_execution_required"
            assert external["execution_performed"] is False and external["signoff"] is False
            assert "job_id" not in external
            conflict = await session.call_tool("dv_build_uvm", {"manifest": "cadence-build.json", "simulator": "vivado"})
            assert conflict.isError
            claim = await call(session, "dv_claim", {"session_id": "trae-protocol-worker-one"})
            attempt = claim["attempt_id"]
            context = await call(session, "dv_context", {"attempt_id": attempt})
            assert context["context_chars"] > 0 and context["context_budget_chars"] is None
            assert "prompt" not in claim
            rejected = await session.call_tool("dv_submit", {"attempt_id": attempt, "response": {}})
            assert rejected.isError
            heartbeat = await call(session, "dv_heartbeat", {"attempt_id": attempt})
            assert heartbeat["status"] == "running"
        # The process and MCP connection are new. Durable attempt context survives.
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
            await session.initialize()
            restored = await call(session, "dv_context", {"attempt_id": attempt})
            assert restored == context
            source_ids = sorted(set(re.findall(r'"source_id":"(src_[^"]+)"', context["prompt"])))
            assert source_ids
            response = {
                "requirements": [],
                "dispositions": [{"source_id": source_id, "classification": "uncertain",
                                  "reason": "Transport fixture leaves semantic review to a real model."}
                                 for source_id in source_ids],
                "unresolved": [{"source_ids": source_ids, "question": "Model review pending.", "blocking": True}],
                "cross_references": [],
            }
            accepted = await call(session, "dv_submit", {"attempt_id": attempt, "response": response})
            assert accepted["status"] == "accepted"
            duplicate = await call(session, "dv_submit", {"attempt_id": attempt, "response": response})
            assert duplicate["receipt"]["replayed"] is True
            changed = {**response, "unresolved": []}
            rejected = await session.call_tool("dv_submit", {"attempt_id": attempt, "response": changed})
            assert rejected.isError
            report = await call(session, "dv_report")
            assert report["coverage"]["percent"] is None and report["signoff"] is False
            assert report["execution_count"] == 0
            escape = await session.call_tool("dv_submit", {"attempt_id": attempt,
                                                             "response_file": "../../outside.json"})
            assert escape.isError

    asyncio.run(exercise())
