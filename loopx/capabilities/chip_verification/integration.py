"""Install the project-local TRAE China IDE facade, preserving user configuration.

This is packaging/transport glue, not a second lifecycle or state owner. The
sidecar records installation ownership only; DV state stays with the provider.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

SERVER_NAME = "loopx-dv"
SCHEMA = "loopx_chip_verification_trae_install_v1"
MARKER = ".trae/.loopx-dv-install.json"
RESOURCES = {
    "rule.md": ".trae/rules/loopx-dv.md",
    "SKILL.md": ".trae/skills/loopx-dv/SKILL.md",
    "worker.md": ".trae/agents/loopx-dv-worker.md",
    "command.md": ".trae/commands/loopx-dv.md",
}
HOST_STEPS = [
    "在 TRAE 中国版打开该工作区；设置 > MCP 中启用项目级 MCP，并确认 loopx-dv 可用。",
    "开启新对话，调用 loopx-dv Skill；Agent 按模块、功能、关系或工程步骤领取任务并提交，确认服务返回回执。",
    "完成当前任务后按需开启新会话并从状态恢复；也可选择启用 Subagents 使用可选 worker。",
]


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"installation path escapes workspace: {relative}")
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError(f"installation path uses a link: {relative}")
    return path


def _object(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected a JSON object: {path.name}")
    return raw


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o7777 if path.exists() else None
    handle, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _server_entry() -> dict:
    interpreter = Path(sys.executable).resolve()
    # TRAE documents command parsing failures for commands containing spaces.
    # Keep the executable name separate from argv and make its PATH explicit.
    command = str(interpreter) if not any(c.isspace() for c in str(interpreter)) else interpreter.name
    environment = {
        "PATH": str(interpreter.parent) + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONUTF8": "1",
        "START_MCP_TIMEOUT_MS": "60000",
        "RUN_MCP_TIMEOUT_MS": "60000",
    }
    for name in ("XILINX_VIVADO", "LOOPX_VERIBLE_LINT"):
        if os.environ.get(name):
            environment[name] = os.environ[name]
    package_parent = Path(__file__).resolve().parents[3]
    # Source checkouts can be run before installation. Wheels must import from
    # their own interpreter's site-packages without a PYTHONPATH dependency.
    if (package_parent / "pyproject.toml").is_file():
        environment["PYTHONPATH"] = str(package_parent)
    return {
        "command": command,
        "args": ["-m", "loopx.capabilities.chip_verification.mcp_server",
                 "--workspace", "${workspaceFolder}"],
        "env": environment,
    }


def install(workspace: str | Path, execute: bool = False) -> dict:
    """Preview or install the China IDE facade; conflicts cause no file writes."""
    root = Path(workspace).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("workspace must be an existing directory")
    result: dict[str, Any] = {
        "schema_version": SCHEMA, "host_surface": "trae-cn-ide", "ok": True,
        "status": "preview", "workspace": str(root), "executed": False,
        "files": [], "host_steps": HOST_STEPS,
        "host_verified": False,
        "limitations": [
            "文件安装不等于 TRAE 已启用 MCP 或实际执行了工程任务。",
            "本适配使用 TRAE 中国版 IDE 的模型；子智能体可选，不依赖 TraeX 或 CLI 登录。",
            "session_id 是调用方执行标签；并非经 TRAE API 验证的原生会话身份。",
        ],
    }
    try:
        marker_path = _safe_path(root, MARKER)
        marker = _object(marker_path)
        if marker and marker.get("schema_version") != SCHEMA:
            raise ValueError("unrecognized installation ownership marker")
        recorded_files = marker.get("files", {})
        if not isinstance(recorded_files, dict):
            raise ValueError("invalid installation ownership marker")
        config_path = _safe_path(root, ".trae/mcp.json")
        config = _object(config_path)
        servers = config.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcpServers must be a JSON object")
        existing = servers.get(SERVER_NAME)
        if SERVER_NAME in servers and (not isinstance(marker.get("server"), dict) or existing != marker["server"]):
            raise ValueError("loopx-dv MCP entry is user-owned or was modified; preserve it and resolve the name conflict")
        planned: dict[Path, bytes] = {}
        for source, relative in RESOURCES.items():
            target = _safe_path(root, relative)
            content = (Path(__file__).parent / "resources" / source).read_bytes()
            previous = target.read_bytes() if target.exists() else None
            if previous is not None and _digest(previous) != recorded_files.get(relative):
                raise ValueError(f"user-owned or modified file: {relative}")
            planned[target] = content
        entry = _server_entry()
        config["mcpServers"] = {**servers, SERVER_NAME: entry}
        planned[config_path] = _json_bytes(config)
        planned[marker_path] = _json_bytes({
            "schema_version": SCHEMA, "server": entry,
            "files": {relative: _digest(planned[root / relative]) for relative in RESOURCES.values()},
        })
        previous_contents = {path: path.read_bytes() if path.exists() else None for path in planned}
        for path, content in planned.items():
            previous = previous_contents[path]
            result["files"].append({
                "path": path.relative_to(root).as_posix(),
                "status": "unchanged" if previous == content else "update" if previous is not None else "create",
            })
        if execute:
            changed = []
            try:
                for path, content in planned.items():
                    if previous_contents[path] != content:
                        _atomic_write(path, content)
                        changed.append(path)
            except OSError:
                for path in reversed(changed):
                    previous = previous_contents[path]
                    if previous is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic_write(path, previous)
                raise
            result.update(status="installed", executed=True,
                          files_verified=all(path.read_bytes() == content for path, content in planned.items()))
    except (OSError, ValueError) as error:
        result.update(status="blocked", ok=False, error=str(error))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    result = install(args.workspace, execute=args.execute)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "blocked":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
