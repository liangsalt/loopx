"""Real provider/RPC protocol tests; synthetic specs are NOT chip coverage evidence."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from loopx.capabilities.chip_verification.provider import STATE, invoke

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def runtime_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # Developer-local Node is optional; installed environments use their PATH.
    bundled = ROOT / ".local/dv-tools/node-v22.22.3-win-x64"
    if (bundled / "node.exe").is_file():
        monkeypatch.setenv("PATH", str(bundled) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("LOOPX_USAGE_PING", "0")


def files(root: Path) -> dict[str, tuple[int, str]]:
    # SQLite readers coordinate through -shm even in mode=ro. The database,
    # durable WAL and domain artifacts must remain unchanged by inspections.
    return {path.relative_to(root).as_posix(): (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in root.rglob("*") if path.is_file() and not path.name.endswith("-shm")}


def synthetic_spec(root: Path, *, large: bool = False) -> Path:
    """Intentionally synthetic protocol fixture, not a published chip design."""
    paragraphs = 240 if large else 2
    spec = {
        "fixture_notice": "SYNTHETIC protocol test only; no chip coverage qualification.",
        "modules": [{"name": f"synthetic_unit_{number:03d}", "description": (
            f"Synthetic unit {number} acknowledges a request after reset is released. "
            "This deliberately repetitive payload tests module-scoped import and durable source identities. "
        ) * 8} for number in range(paragraphs)],
    }
    path = root / "synthetic-spec.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    if large:
        assert path.stat().st_size >= 200 * 1024
    return path


def import_fixture(root: Path, *, large: bool = False) -> dict:
    source = synthetic_spec(root, large=large)
    return invoke("import", workspace=root, arguments={"spec": source.name,
                  "name": "Synthetic protocol test", "context_chars": 24000, "chunk_chars": 7000}, execute=True)


def response_for(context: dict) -> dict:
    pack = json.loads(context["prompt"].rsplit("\n", 1)[1])
    source_ids = [source["source_id"] for source in pack["sources"]]
    assert source_ids
    return {
        "requirements": [{"local_id": "synthetic-r1", "statement": "Synthetic fixture acknowledges a request after reset release.",
            "source_ids": source_ids, "modules": ["synthetic_unit"], "features": ["handshake"],
            "interfaces": ["request_acknowledge"], "conditions": ["reset released"],
            "expected": "Acknowledgement follows the request.", "exceptions": [], "applicability": "in_scope"}],
        "dispositions": [{"source_id": source_id, "classification": "requirements",
                          "reason": "Synthetic protocol fixture used to validate source accounting."} for source_id in source_ids],
        "unresolved": [], "cross_references": [],
    }


def fresh_process(action: str, root: Path, arguments: dict | None = None, *, execute: bool = False) -> dict:
    script = """
import json, sys
from loopx.capabilities.chip_verification.provider import invoke
request = json.loads(sys.stdin.read())
print(json.dumps(invoke(**request), ensure_ascii=False))
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
        input=json.dumps({"action": action, "workspace": str(root), "arguments": arguments, "execute": execute}),
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "PYTHONUTF8": "1"})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_uninitialized_reads_and_mutation_previews_never_create_domain_state(tmp_path: Path) -> None:
    workspace = tmp_path / "does-not-exist"
    for action in ("doctor", "status", "next", "design-map", "generation-scopes", "report"):
        result = invoke(action, workspace=workspace)
        assert result["initialized"] is False
        assert not workspace.exists()
    for action, arguments in (("import", {"spec": "missing-spec.json"}),
                              ("claim", {"session_id": "synthetic-host"}),
                              ("build-uvm", {"manifest": "missing-manifest.json"})):
        result = invoke(action, workspace=workspace, arguments=arguments)
        assert result["status"] == "preview"
        assert result["allowed"] is False
        assert result["execution_performed"] is False
        assert not workspace.exists()


def test_default_off_does_not_import_optional_domain_dependencies(tmp_path: Path) -> None:
    script = """
import importlib.abc, json, sys
class DenyDomain(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'jsonschema' or fullname.startswith('loopx.capabilities.chip_verification.engine'):
            raise AssertionError('default-off imported ' + fullname)
sys.meta_path.insert(0, DenyDomain())
from loopx.capabilities.chip_verification.provider import invoke
print(json.dumps(invoke('status', workspace=sys.argv[1])))
"""
    workspace = tmp_path / "off"
    result = subprocess.run([sys.executable, "-c", script, str(workspace)], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["initialized"] is False
    assert not workspace.exists()


def test_input_path_escape_fails_before_domain_initialization(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (tmp_path / "outside.json").write_text('{}', encoding="utf-8")
    with pytest.raises(ValueError, match="inside the selected workspace"):
        invoke("import", workspace=workspace, arguments={"spec": "../outside.json"}, execute=True)
    assert not (workspace / STATE).exists()


def test_large_spec_module_context_survives_restart_and_replay(tmp_path: Path) -> None:
    imported = import_fixture(tmp_path, large=True)
    assert imported["spec_bytes"] >= 200 * 1024
    assert imported["model_tasks"] > 1
    claimed = invoke("claim", workspace=tmp_path, arguments={"session_id": "synthetic-host-before-restart", "kind": "model"}, execute=True)
    assert claimed["status"] == "awaiting_host"
    assert claimed["context_chars"] > 0 and claimed["context_budget_chars"] is None
    attempt = claimed["attempt_id"]
    original = invoke("context", workspace=tmp_path, arguments={"attempt_id": attempt})
    restored = fresh_process("context", tmp_path, {"attempt_id": attempt})
    assert restored == original
    assert restored["context_budget_chars"] is None
    assert len(restored["prompt"]) < imported["spec_bytes"] / 5
    response = response_for(restored)
    response_path = tmp_path / "synthetic-response.json"
    response_path.write_text(json.dumps(response), encoding="utf-8")
    accepted = fresh_process("submit", tmp_path, {"attempt_id": attempt, "response_file": response_path.name}, execute=True)
    assert accepted["status"] == "accepted"
    replay = invoke("submit", workspace=tmp_path, arguments={"attempt_id": attempt, "response": response}, execute=True)
    assert replay["status"] == "accepted"
    assert replay["receipt"]["replayed"] is True
    changed = json.loads(json.dumps(response))
    changed["requirements"][0]["expected"] = "A different candidate must not overwrite an accepted attempt."
    with pytest.raises((ValueError, RuntimeError), match="different response"):
        invoke("submit", workspace=tmp_path, arguments={"attempt_id": attempt, "response": changed}, execute=True)
    status = invoke("status", workspace=tmp_path)
    assert status["tasks"]["done"] >= 1
    assert status["requirement_count"] >= 1
    assert status["coverage"]["percent"] is None
    assert status["signoff"] is False
    before = files(tmp_path)
    for action in ("status", "next", "design-map", "report"):
        invoke(action, workspace=tmp_path)
    with pytest.raises((ValueError, RuntimeError), match="accepted testpoint"):
        invoke("generation-scopes", workspace=tmp_path)
    assert files(tmp_path) == before


def test_complete_function_context_can_exceed_former_limit_and_ignore_legacy_options(tmp_path: Path) -> None:
    behavior = "The counter increments on each accepted tick while enabled and preserves its value otherwise. " * 800
    source = tmp_path / "spec.json"
    source.write_text(json.dumps({"module": "timer", "function": "count", "behavior": behavior}), encoding="utf-8")
    imported = invoke("import", workspace=tmp_path, arguments={"spec": source.name,
                      "chunk_chars": 32, "context_chars": 1}, execute=True)
    assert imported["context_budget_chars"] is None
    assert imported["decomposition"] == "module_function_relationship_stage"
    claimed = invoke("claim", workspace=tmp_path, arguments={"session_id": "semantic-host"}, execute=True)
    assert claimed["context_chars"] > 24000
    context = invoke("context", workspace=tmp_path, arguments={"attempt_id": claimed["attempt_id"]})
    pack = json.loads(context["prompt"].rsplit("\n", 1)[1])
    assert behavior in "".join(item["text"] for item in pack["sources"])
    assert context["context_budget_chars"] is None
    assert fresh_process("context", tmp_path, {"attempt_id": claimed["attempt_id"]}) == context
    # An enormous legacy hint neither creates new partitions nor mutates an
    # already active attempt on the same semantic baseline.
    repeated = invoke("import", workspace=tmp_path, arguments={"spec": source.name,
                      "chunk_chars": 1500000, "context_chars": 1500000}, execute=True)
    assert repeated["model_tasks"] == imported["model_tasks"]
    assert invoke("context", workspace=tmp_path, arguments={"attempt_id": claimed["attempt_id"]}) == context


def test_invalid_candidate_can_be_corrected_without_losing_attempt(tmp_path: Path) -> None:
    import_fixture(tmp_path)
    claimed = invoke("claim", workspace=tmp_path, arguments={"session_id": "synthetic-worker", "kind": "model"}, execute=True)
    attempt = claimed["attempt_id"]
    context = invoke("context", workspace=tmp_path, arguments={"attempt_id": attempt})
    response = response_for(context)
    invalid = json.loads(json.dumps(response))
    invalid["requirements"][0]["source_ids"] = ["not-a-source-in-this-task"]
    with pytest.raises((ValueError, RuntimeError), match="outside this task"):
        invoke("submit", workspace=tmp_path, arguments={"attempt_id": attempt, "response": invalid}, execute=True)
    assert invoke("context", workspace=tmp_path, arguments={"attempt_id": attempt}) == context
    accepted = invoke("submit", workspace=tmp_path, arguments={"attempt_id": attempt, "response": response}, execute=True)
    assert accepted["status"] == "accepted"


def test_changed_spec_prevents_old_attempt_acceptance(tmp_path: Path) -> None:
    import_fixture(tmp_path)
    claimed = invoke("claim", workspace=tmp_path, arguments={"session_id": "synthetic-worker", "kind": "model"}, execute=True)
    attempt = claimed["attempt_id"]
    response = response_for(invoke("context", workspace=tmp_path, arguments={"attempt_id": attempt}))
    source = tmp_path / "synthetic-spec.json"
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError)):
        invoke("submit", workspace=tmp_path, arguments={"attempt_id": attempt, "response": response}, execute=True)
    status = invoke("status", workspace=tmp_path)
    assert status["inputs_current"] is False
    assert status["next_action"] == "import"
    assert status["signoff"] is False
