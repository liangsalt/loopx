"""Opt-in external control loop; inference stays with an explicit host bridge.

The service owns only timer/transport bookkeeping. Chip decisions and recovery
remain in engine.watchdog, while host_work validates every model response.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from time import sleep as wait_for_tick
import uuid

from ...file_lock import process_is_alive, try_exclusive_file_lock, write_json_atomic
from . import host_work
from .engine.models import ConflictError
from .engine.workflow import DVProject

MAX_BRIDGE_ATTEMPTS = 3
MAX_OUTPUT_BYTES = 1024 * 1024


def _initialize_dispatches(project):
    with project.store._db(write=True) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS watchdog_dispatches(
            task_id TEXT PRIMARY KEY, attempt_id TEXT, session_id TEXT NOT NULL,
            status TEXT NOT NULL, tries INTEGER NOT NULL, pid INTEGER,
            next_attempt REAL NOT NULL, command_digest TEXT NOT NULL,
            error TEXT, updated REAL NOT NULL)""")


def read_service_status(project_or_root):
    """Read timer liveness without starting a process or writing domain state."""
    root = getattr(project_or_root, "root", Path(project_or_root) if isinstance(project_or_root, (str, Path)) else None)
    if root is None:
        raise ValueError("A project or workspace path is required")
    path = Path(root) / ".loopx" / "chip-verification" / "watchdog-service.json"
    if not path.is_file():
        return {"status": "not_started", "running": False}
    if path.stat().st_size > 65536:
        return {"status": "invalid_service_record", "running": False}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("service record is not an object")
        age = max(0, time.time() - float(value["updated"]))
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "invalid_service_record", "running": False}
    value["running"] = value.get("status") == "running" and process_is_alive(value.get("pid"))
    value["age_seconds"] = age
    if value.get("status") == "running" and not value["running"]:
        value["status"] = "interrupted"
    return value


def dispatch_status(project):
    with project.store._db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchdog_dispatches'").fetchone():
            return []
        return [dict(row) for row in db.execute(
            "SELECT task_id,attempt_id,status,tries,pid,next_attempt,error,updated "
            "FROM watchdog_dispatches ORDER BY updated DESC,task_id LIMIT 20")]


def _record_dispatch(project, task_id, **updates):
    allowed = {"attempt_id", "status", "pid", "next_attempt", "error"}
    if not set(updates) <= allowed:
        raise ValueError("Unsupported dispatch update")
    values = {**updates, "updated": time.time()}
    with project.store._db(write=True) as db:
        db.execute("UPDATE watchdog_dispatches SET " + ",".join(key + "=?" for key in values) + " WHERE task_id=?",
                   (*values.values(), task_id))


def _release_review(project, task_id, attempt_id, reason):
    if attempt_id:
        try:
            project.store.fail(task_id, attempt_id, reason, retryable=True)
        except ConflictError:
            # A newer owner/accepted result must not be changed by this runner.
            pass


def _reserve(project, task, command, now):
    """Reserve before spawning; an uncertain launch is never blindly repeated."""
    with project.store._db(write=True) as db:
        row = db.execute("SELECT * FROM watchdog_dispatches WHERE task_id=?", (task["id"],)).fetchone()
        if row:
            row = dict(row)
            if row["status"] in {"dispatching", "running", "uncertain"}:
                if row["pid"] and process_is_alive(row["pid"]):
                    return {"status": "bridge_alive", "task_id": task["id"]}
                # A lost acknowledgement is not proof that the host invocation
                # never happened. Preserve the lease/context for manual readback.
                db.execute("UPDATE watchdog_dispatches SET status='uncertain',error=?,updated=? WHERE task_id=?",
                           ("Interrupted bridge dispatch; inspect its saved context/output before manual review", now, task["id"]))
                return {"status": "dispatch_uncertain", "task_id": task["id"]}
            if row["status"] == "accepted":
                return {"status": "already_reviewed", "task_id": task["id"]}
            if row["tries"] >= MAX_BRIDGE_ATTEMPTS:
                return {"status": "bridge_budget_exhausted", "task_id": task["id"]}
            if row["next_attempt"] > now:
                return {"status": "bridge_backoff", "task_id": task["id"], "next_attempt": row["next_attempt"]}
        tries = (row["tries"] if row else 0) + 1
        session = "watchdog-" + uuid.uuid4().hex
        digest = hashlib.sha256(json.dumps(command).encode()).hexdigest()
        db.execute("INSERT INTO watchdog_dispatches VALUES(?,NULL,?,'dispatching',?,NULL,0,?,NULL,?) "
                   "ON CONFLICT(task_id) DO UPDATE SET attempt_id=NULL,session_id=excluded.session_id,"
                   "status='dispatching',tries=excluded.tries,pid=NULL,next_attempt=0,"
                   "command_digest=excluded.command_digest,error=NULL,updated=excluded.updated",
                   (task["id"], session, tries, digest, now))
        return {"status": "reserved", "session_id": session, "tries": tries}


def _call_bridge(project, request, command, timeout, task_id):
    directory = project.store.state_dir / "watchdog-bridges" / request["attempt_id"]
    directory.mkdir(parents=True, exist_ok=True)
    input_path, output_path, error_path = [directory / name for name in ("request.json", "response.json", "stderr.log")]
    write_json_atomic(input_path, request)
    if input_path.stat().st_size > MAX_OUTPUT_BYTES:
        raise ValueError("Review bridge request exceeds 1 MiB")
    child = None
    try:
        with input_path.open("rb") as stdin, output_path.open("wb") as stdout, error_path.open("wb") as stderr:
            child = subprocess.Popen(command, cwd=project.root, stdin=stdin, stdout=stdout, stderr=stderr,
                                     shell=False, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            _record_dispatch(project, task_id, status="running", pid=child.pid)
            deadline, heartbeat = time.monotonic() + timeout, time.monotonic() + 60
            while child.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Review bridge timed out; no recovery action was accepted")
                if output_path.stat().st_size > MAX_OUTPUT_BYTES or error_path.stat().st_size > MAX_OUTPUT_BYTES:
                    raise ValueError("Review bridge output exceeded 1 MiB")
                if time.monotonic() >= heartbeat:
                    project.store.heartbeat(task_id, request["attempt_id"], host_work.LEASE_SECONDS)
                    heartbeat = time.monotonic() + 60
                time.sleep(0.1)
        if child.returncode:
            raise RuntimeError(f"Review bridge exited with code {child.returncode}; inspect the saved stderr file")
        if output_path.stat().st_size > MAX_OUTPUT_BYTES or error_path.stat().st_size > MAX_OUTPUT_BYTES:
            raise ValueError("Review bridge output exceeded 1 MiB")
        response = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(response, dict):
            raise ValueError("Review bridge stdout must be one JSON response object")
        return response
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def dispatch_one(project, command, *, timeout=300):
    """Execute at most one independent review through a user-configured bridge."""
    if not command or not all(isinstance(part, str) and part and "\0" not in part for part in command):
        raise ValueError("review-command needs an executable and separate arguments")
    _initialize_dispatches(project)
    host_work.initialize(project)
    tasks = [task for task in project.store.ready_tasks(10000) if task["kind"] == "watchdog-review"]
    skipped = []
    for task in tasks:
        reserved = _reserve(project, task, command, time.time())
        if reserved["status"] != "reserved":
            skipped.append(reserved)
            continue
        attempt_id = None
        try:
            claimed = host_work.claim(project, {"task_id": task["id"], "kind": "watchdog-review", "session_id": reserved["session_id"]})
            attempt_id = claimed["attempt_id"]
            _record_dispatch(project, task["id"], attempt_id=attempt_id)
            context = host_work.context(project, attempt_id)
            request = {"protocol": "loopx_chip_watchdog_review_v1", "workspace": str(project.root),
                       "task_id": task["id"], "attempt_id": attempt_id, "session_id": reserved["session_id"],
                       "instruction": "Use a fresh isolated host session. Return only the response JSON matching context.output_schema; do not submit or apply it yourself.",
                       "context": context}
            response = _call_bridge(project, request, command, timeout, task["id"])
            receipt = host_work.submit(project, attempt_id, response)
            _record_dispatch(project, task["id"], status="accepted", error=None, pid=None)
            return {"status": "accepted", "task_id": task["id"], "attempt_id": attempt_id,
                    "receipt": receipt["receipt"], "bridge_invocations": reserved["tries"]}
        except Exception as error:
            reason = str(error)[:2000]
            # A committed submission may have lost its transport acknowledgement.
            # Preserve it for the next watchdog tick, rather than releasing it.
            current = project.store.get_task(task["id"])
            if current["status"] == "done":
                _record_dispatch(project, task["id"], status="accepted", error=reason, pid=None)
                return {"status": "accepted", "task_id": task["id"], "receipt_recovery": True}
            _release_review(project, task["id"], attempt_id, reason)
            delay = min(300, 30 * 2 ** (reserved["tries"] - 1))
            _record_dispatch(project, task["id"], status="failed", error=reason, pid=None,
                             next_attempt=time.time() + delay)
            return {"status": "bridge_failed", "task_id": task["id"], "error": reason,
                    "bridge_invocations": reserved["tries"], "retry_budget": MAX_BRIDGE_ATTEMPTS}
    return {"status": "no_review_dispatched", "pending": skipped[:20]}


def run(workspace, *, execute=False, once=False, interval=30, max_ticks=None,
        review_command=None, review_timeout=300, emit=None):
    """Run the independent timer. No configured bridge means durable reviews wait."""
    if any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3600
           for value in (interval, review_timeout)):
        raise ValueError("interval and review-timeout must be in 1..3600 seconds")
    if max_ticks is not None and (isinstance(max_ticks, bool) or not isinstance(max_ticks, int) or max_ticks < 1):
        raise ValueError("max-ticks must be a positive integer")
    root = Path(workspace).expanduser().resolve()
    if not execute:
        return {"status": "preview", "execution_performed": False, "requires_execute": True,
                "review_bridge_configured": bool(review_command)}
    if not (root / ".loopx" / "chip-verification" / "project.json").is_file():
        raise ValueError("Import the specification before starting the Watchdog service")
    project = DVProject(root)
    service_path = project.store.state_dir / "watchdog-service.json"
    with try_exclusive_file_lock(service_path, agent_id="chip-watchdog", operation="external-watchdog-loop") as held:
        if held is None:
            return {"status": "already_running", "execution_performed": False}
        from .engine import watchdog
        ticks, failures = 0, 0
        started = time.time()
        summary = {"status": "running", "pid": os.getpid(), "started": started, "updated": started,
                   "ticks": 0, "review_bridge_configured": bool(review_command)}
        write_json_atomic(service_path, summary)
        final = None
        try:
            while True:
                try:
                    result = watchdog.tick(project)
                    if review_command:
                        result["bridge"] = dispatch_one(project, review_command, timeout=review_timeout)
                        if result["bridge"]["status"] == "accepted":
                            result["after_review"] = watchdog.tick(project)
                    failures = 0
                    summary["last_error"] = None
                except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
                    failures += 1
                    result = {"status": "control_error", "error": str(error)[:2000], "consecutive_failures": failures}
                    summary["last_error"] = result["error"]
                ticks += 1
                summary.update(updated=time.time(), ticks=ticks, last_result=result["status"])
                write_json_atomic(service_path, summary)
                if emit:
                    emit(result)
                final = result
                if once or (max_ticks is not None and ticks >= max_ticks):
                    break
                wait_for_tick(min(3600, interval * 2 ** min(failures, 5)))
        finally:
            summary.update(status="stopped", updated=time.time(), ticks=ticks)
            write_json_atomic(service_path, summary)
        return final


def main():
    parser = argparse.ArgumentParser(description="Independent LoopX chip Watchdog timer; no EDA or model is launched by default")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--max-ticks", type=int)
    parser.add_argument("--review-timeout", type=int, default=300)
    parser.add_argument("--review-command", nargs=argparse.REMAINDER,
                        help="Opt-in host bridge executable and arguments, last on command line; JSON stdin/stdout, no shell")
    args = parser.parse_args()
    try:
        result = run(**vars(args), emit=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
        if result.get("status") in {"preview", "already_running"}:
            print(json.dumps(result, ensure_ascii=False), flush=True)
        bridge = result.get("bridge", {})
        stalled = any(item.get("status") in {"dispatch_uncertain", "bridge_budget_exhausted"}
                      for item in bridge.get("pending", []))
        return 1 if result.get("status") == "control_error" or bridge.get("status") == "bridge_failed" or stalled else 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
