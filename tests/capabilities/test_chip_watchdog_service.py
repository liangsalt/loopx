"""Real timer/bridge transport checks; bridge fixtures do not prove model judgment."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from loopx.capabilities.chip_verification import watchdog_service as service
from loopx.capabilities.chip_verification.engine import watchdog
from loopx.capabilities.chip_verification.engine.workflow import DVProject
from loopx.file_lock import try_exclusive_file_lock


ROOT = Path(__file__).resolve().parents[2]


def environment():
    return {**os.environ, "PYTHONUTF8": "1", "LOOPX_USAGE_PING": "0", "PYTHONPATH": str(ROOT)}


def invoke(root, *args):
    return subprocess.run([sys.executable, "-m", "loopx.capabilities.chip_verification.watchdog_service",
                           "--workspace", str(root), *args], cwd=ROOT, env=environment(),
                          capture_output=True, encoding="utf-8", timeout=60)


@pytest.fixture
def project(tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"module": "fifo", "behavior": "Accepted writes are returned in order; reset empties the FIFO."}))
    project = DVProject(tmp_path)
    project.import_spec(spec, scope="Implement and verify FIFO ordering and reset")
    return project


def repeated_failure(project):
    task = project.store.list_tasks(kind="model")[0]
    for _ in range(3):
        attempt = project.store.claim(task["id"], "same-worker")
        project.store.fail(task["id"], attempt["id"], "Repeated type mismatch with identical inputs")
    tick = watchdog.tick(project)
    assert len(tick["review_task_ids"]) == 1
    return task["id"], tick["review_task_ids"][0]


def bridge_file(project, body):
    path = project.root / "bridge.py"
    path.write_text(body, encoding="utf-8")
    return [sys.executable, str(path)]


def test_preview_creates_no_project_or_process(tmp_path):
    workspace = tmp_path / "absent"
    result = invoke(workspace, "--once")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "preview"
    assert not workspace.exists()


def test_separate_timer_process_restarts_without_duplicate_review(project):
    target, review = repeated_failure(project)
    for _ in range(2):
        result = invoke(project.root, "--execute", "--once")
        assert result.returncode == 0, result.stderr + result.stdout
        assert json.loads(result.stdout)["status"] == "observed"
    recovered = DVProject(project.root)
    assert [task["id"] for task in recovered.store.list_tasks(kind="watchdog-review")] == [review]
    assert watchdog.status(recovered)["active_holds"][0]["task_id"] == target
    assert service.read_service_status(recovered)["status"] == "stopped"
    assert service.read_service_status(recovered)["running"] is False


def test_timer_single_flight_is_a_kernel_process_fence(project):
    path = project.store.state_dir / "watchdog-service.json"
    with try_exclusive_file_lock(path) as held:
        assert held
        result = invoke(project.root, "--execute", "--once")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "already_running"
    assert watchdog.status(project)["initialized"] is False


def test_configured_bridge_uses_fresh_context_and_validated_submission(project):
    target, review = repeated_failure(project)
    command = bridge_file(project, """
import json, sys
request = json.load(sys.stdin)
assert request['protocol'] == 'loopx_chip_watchdog_review_v1'
assert request['session_id'].startswith('watchdog-')
assert request['context']['kind'] == 'watchdog-review'
snapshot = json.loads(request['context']['prompt'].split('\\n')[-1])
assert len(request['context']['prompt']) < request['context']['context_budget_chars']
print(json.dumps({'diagnosis':'Repeated failures require an independent constraint inspection.',
  'evidence_refs':[snapshot['evidence_refs'][0]],
  'strategy':'Inspect the unresolved constraint before allowing another retry.',
  'action':'wait','next_check_seconds':30}))
""")
    result = invoke(project.root, "--execute", "--once", "--review-command", *command)
    assert result.returncode == 0, result.stderr + result.stdout
    observed = json.loads(result.stdout)
    assert observed["bridge"]["status"] == "accepted"
    assert observed["after_review"]["applied"][0]["status"] == "waiting"
    assert project.store.get_task(review)["status"] == "done"
    assert project.store.get_task(target)["status"] == "pending"
    dispatch = service.dispatch_status(project)[0]
    assert dispatch["status"] == "accepted" and dispatch["tries"] == 1
    with project.store._db() as db:
        label = db.execute("SELECT session_id FROM attempts WHERE id=?", (dispatch["attempt_id"],)).fetchone()[0]
    assert label != "same-worker"


def test_malformed_bridge_response_cannot_apply_and_retry_budget_survives_restart(project):
    target, review = repeated_failure(project)
    command = bridge_file(project, "print('not a JSON review')\n")
    for count in range(1, 4):
        result = service.dispatch_one(DVProject(project.root), command)
        assert result["status"] == "bridge_failed"
        assert result["bridge_invocations"] == count
        with project.store._db(write=True) as db:
            db.execute("UPDATE watchdog_dispatches SET next_attempt=0 WHERE task_id=?", (review,))
    fourth = service.dispatch_one(DVProject(project.root), command)
    assert fourth["status"] == "no_review_dispatched"
    assert fourth["pending"][0]["status"] == "bridge_budget_exhausted"
    assert project.store.get_task(review)["status"] == "pending"
    assert watchdog.status(project)["active_holds"][0]["task_id"] == target
    assert not project.store.list_artifacts(task_id=review)


def test_bridge_failure_backoff_is_persisted(project):
    repeated_failure(project)
    command = bridge_file(project, "raise SystemExit(7)\n")
    first = service.dispatch_one(project, command)
    second = service.dispatch_one(DVProject(project.root), command)
    assert first["status"] == "bridge_failed"
    assert second["pending"][0]["status"] == "bridge_backoff"
    assert service.dispatch_status(project)[0]["tries"] == 1


def test_bridge_timeout_stops_owned_process_without_acceptance(project):
    _, review = repeated_failure(project)
    command = bridge_file(project, "import time\ntime.sleep(30)\n")
    started = time.monotonic()
    result = service.dispatch_one(project, command, timeout=1)
    assert time.monotonic() - started < 15
    assert result["status"] == "bridge_failed" and "timed out" in result["error"]
    assert project.store.get_task(review)["status"] == "pending"
    assert not project.store.list_artifacts(task_id=review)


@pytest.mark.parametrize("pid,expected", [(None, "dispatch_uncertain"), (os.getpid(), "bridge_alive")])
def test_interrupted_launch_is_not_blindly_dispatched_again(project, pid, expected):
    _, review = repeated_failure(project)
    command = bridge_file(project, "raise AssertionError('must not execute')\n")
    service._initialize_dispatches(project)
    with project.store._db(write=True) as db:
        db.execute("INSERT INTO watchdog_dispatches VALUES(?,NULL,'previous-session','running',1,?,0,'digest',NULL,?)",
                   (review, pid, time.time()))
    result = service.dispatch_one(project, command)
    assert result["pending"][0]["status"] == expected
    assert service.dispatch_status(project)[0]["tries"] == 1
    assert not (project.store.state_dir / "watchdog-bridges").exists()


def test_preview_rejects_invalid_timer_configuration(tmp_path):
    with pytest.raises(ValueError, match="1..3600"):
        service.run(tmp_path, interval=0)
    with pytest.raises(ValueError, match="positive integer"):
        service.run(tmp_path, max_ticks=True)


def test_transient_control_failure_is_rechecked_by_external_loop(project, monkeypatch):
    real_tick = watchdog.tick
    ticks, sleeps, results = [], [], []

    def transient(project):
        ticks.append(True)
        if len(ticks) == 1:
            raise sqlite3.OperationalError("temporary database lock")
        return real_tick(project)

    monkeypatch.setattr(watchdog, "tick", transient)
    monkeypatch.setattr(service, "wait_for_tick", sleeps.append)
    result = service.run(project.root, execute=True, max_ticks=2, interval=2, emit=results.append)
    assert [item["status"] for item in results] == ["control_error", "observed"]
    assert result["status"] == "observed" and sleeps == [4]
    assert service.read_service_status(project)["last_error"] is None
