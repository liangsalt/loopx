"""Exercise shipped command routing and dependency isolation at the user boundary."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from loopx.capabilities.chip_verification.cli import (
    handle_chip_verification_command, register_chip_verification_commands,
)
from loopx.cli_runtime import add_subcommand_format, output_format

ROOT = Path(__file__).resolve().parents[2]


def parse(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=["json", "markdown"])
    commands = parser.add_subparsers(dest="command", required=True)
    register_chip_verification_commands(commands, add_subcommand_format)
    return parser.parse_args(arguments)


@pytest.mark.parametrize("args,code", [
    (["--help"], 0), (["import", "--help"], 0), (["generate-uvm", "--help"], 0),
    ([], 2), (["unknown"], 2), (["claim"], 2), (["import"], 2),
    (["submit", "--attempt-id", "a1"], 2),
    (["--workspace", "missing", "context"], 2),
])
def test_console_and_full_parser_match(args: list[str], code: int) -> None:
    results = []
    for module in ("loopx.cli", "loopx.entrypoint"):
        script = f"import sys; sys.argv[0]='loopx'; from {module} import main; raise SystemExit(main())"
        result = subprocess.run(
            [sys.executable, "-c", script, "chip-verification", *args],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=45,
            env={**os.environ, "LOOPX_USAGE_PING": "0", "PYTHONUTF8": "1"},
        )
        results.append(result)
    assert [result.returncode for result in results] == [code, code], results[0].stderr + results[1].stderr
    assert (results[0].stdout, results[0].stderr) == (results[1].stdout, results[1].stderr)


def test_help_loads_no_provider_and_starts_no_process(tmp_path: Path) -> None:
    script = """
import importlib.abc
import sys
class BlockProvider(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('loopx.capabilities.chip_verification.provider', 'loopx.capabilities.chip_verification.engine', 'loopx.control_plane.effect_runtime', 'jsonschema')):
            raise AssertionError('help imported ' + fullname)
sys.meta_path.insert(0, BlockProvider())
def audit(event, args):
    if event in ('subprocess.Popen', 'os.system', 'os.posix_spawn', 'socket.connect', 'socket.getaddrinfo'):
        raise AssertionError('help attempted ' + event)
sys.addaudithook(audit)
from loopx.entrypoint import main
raise SystemExit(main(['chip-verification', '--workspace', sys.argv[1], '--help']))
"""
    workspace = tmp_path / "never-initialized"
    result = subprocess.run(
        [sys.executable, "-c", script, str(workspace)], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=20,
        env={**os.environ, "PATH": str(tmp_path / "no-executables"), "LOOPX_USAGE_PING": "0"},
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert not workspace.exists()


def test_transport_preserves_scope_identity_and_explicit_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    provider = ModuleType("loopx.capabilities.chip_verification.provider")

    def invoke(action, *, workspace, arguments, execute):
        calls.append((action, workspace, arguments, execute))
        return {"ok": True, "action": action}

    provider.invoke = invoke
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    args = parse(["chip-verification", "--workspace", "space project", "--execute", "generate-uvm",
                  "--rtl-files", "a.sv", "b.sv", "--top", "dut", "--scope-id", "slice1",
                  "--session-id", "host-session", "--format", "json"])
    printed = []
    assert handle_chip_verification_command(args, output_format=output_format,
                                            print_payload=lambda *items: printed.append(items)) == 0
    assert calls == [("generate-uvm", "space project", {"session_id": "host-session", "rtl_files": ["a.sv", "b.sv"],
                                                       "top": "dut", "scope_id": "slice1"}, True)]
    assert printed[0][1] == "json"


def test_inline_response_and_provider_failure_have_structured_output(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ModuleType("loopx.capabilities.chip_verification.provider")
    seen = []

    def invoke(action, **kwargs):
        seen.append(kwargs)
        raise ValueError("response fails source reference validation")

    provider.invoke = invoke
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    args = parse(["chip-verification", "submit", "--attempt-id", "a1", "--response", '{"source":"s1"}', "--execute"])
    printed = []
    assert handle_chip_verification_command(args, output_format=output_format,
                                            print_payload=lambda *items: printed.append(items)) == 1
    assert seen[0]["arguments"]["response"] == {"source": "s1"}
    assert printed[0][0]["ok"] is False
    assert "source reference" in printed[0][0]["error"]


def test_format_and_workspace_work_on_each_side_of_action() -> None:
    first = parse(["chip-verification", "--workspace", "one", "--format", "json", "status"])
    second = parse(["chip-verification", "status", "--workspace", "two", "--format", "json"])
    assert first.workspace == "one"
    assert second.workspace == "two"
    assert output_format(first, "chip_verification_format") == output_format(second, "chip_verification_format") == "json"
