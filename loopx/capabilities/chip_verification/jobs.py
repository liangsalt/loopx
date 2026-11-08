"""Explicit local EDA subprocesses with durable receipts independent of MCP lifetime."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .engine.state import file_hash
from .host_work import encoded


def launch(project, arguments):
    # Fail fast on malformed inputs/missing tools before claiming a background run.
    if arguments.get("operation") == "check-design":
        from .engine.design import get_candidate
        candidate = get_candidate(project, arguments["rtl_task_id"])
        inputs = [{"path": path, "sha256": value} for path, value in candidate["inputs"].items()]
    else:
        from .engine.simulators.generic import GenericXSimBackend
        backend = GenericXSimBackend()
        backend.plan(arguments["manifest"])
        inputs = backend.snapshot_inputs(arguments["manifest"])
    arguments = {**arguments, "inputs": inputs}
    identifier = "job-" + uuid.uuid4().hex
    with project.store._db(write=True) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS host_jobs(
            id TEXT PRIMARY KEY, baseline_id TEXT NOT NULL, arguments TEXT NOT NULL,
            status TEXT NOT NULL, pid INTEGER, created REAL NOT NULL, updated REAL NOT NULL, result TEXT)""")
        db.execute("INSERT INTO host_jobs VALUES(?,?,?,'queued',NULL,?,?,NULL)",
                   (identifier, project.store._current(db), encoded(arguments), time.time(), time.time()))
    directory = project.store.state_dir / "jobs"
    directory.mkdir(exist_ok=True)
    try:
        with (directory / (identifier + ".log")).open("wb") as log:
            child = subprocess.Popen([sys.executable, "-m", "loopx.capabilities.chip_verification.jobs",
                                      "--workspace", str(project.root), "--job-id", identifier],
                cwd=project.root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt")
        with project.store._db(write=True) as db:
            db.execute("UPDATE host_jobs SET pid=? WHERE id=?", (child.pid, identifier))
    except Exception as error:
        with project.store._db(write=True) as db:
            db.execute("UPDATE host_jobs SET status='failed',result=?,updated=? WHERE id=?",
                       (encoded({"error": str(error)}), time.time(), identifier))
        raise
    return {"status": "queued", "job_id": identifier, "next_action": "job-status"}


def status(project, identifier):
    from ...file_lock import process_is_alive
    with project.store._db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='host_jobs'").fetchone():
            raise ValueError("Unknown local EDA job")
        row = db.execute("SELECT * FROM host_jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("Unknown local EDA job")
        row = dict(row)
    result = {key: row[key] for key in ("id", "status", "created", "updated")}
    result["job_id"] = identifier
    result["baseline_current"] = row["baseline_id"] == project.store.baseline()["id"]
    if row["status"] in {"queued", "running"} and row["pid"] and not process_is_alive(row["pid"]):
        result.update(status="interrupted", instruction="Inspect the preserved job log; explicitly start a new build to retry.")
    if row["status"] == "queued" and not row["pid"] and time.time() - row["created"] > 30:
        result.update(status="interrupted", instruction="No process was registered after dispatch; explicitly start a new build to retry.")
    if row["result"]:
        result["result"] = json.loads(row["result"])
    result["log"] = str(project.store.state_dir / "jobs" / (identifier + ".log"))
    return result


def run(root, identifier):
    from .engine.workflow import DVProject
    from .engine.build import run_build
    project = DVProject(root)
    with project.store._db(write=True) as db:
        row = db.execute("SELECT * FROM host_jobs WHERE id=?", (identifier,)).fetchone()
        if not row or row["status"] != "queued":
            raise ValueError("Job was already started or is absent")
        db.execute("UPDATE host_jobs SET status='running',pid=?,updated=? WHERE id=?", (os.getpid(), time.time(), identifier))
        arguments = json.loads(row["arguments"])
    try:
        if row["baseline_id"] != project.store.baseline()["id"]:
            raise ValueError("Specification changed before the EDA job started")
        for item in arguments.pop("inputs"):
            if file_hash(item["path"]) != item["sha256"]:
                raise ValueError("Build input changed after dispatch: " + item["path"])
        if arguments.pop("operation", None) == "check-design":
            from .engine.design_checks import run_checks
            result = run_checks(project, **arguments)
        else:
            result = run_build(project, arguments.pop("manifest"), **arguments)
    except Exception as error:
        result = {"status": "failed", "error": str(error)}
    with project.store._db(write=True) as db:
        db.execute("UPDATE host_jobs SET status=?,result=?,updated=? WHERE id=?",
                   (result.get("status", "failed"), encoded(result), time.time(), identifier))
    return result


def main():
    parser = argparse.ArgumentParser(description="Internal LoopX DV job runner")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    print(json.dumps(run(Path(args.workspace), args.job_id)))


if __name__ == "__main__":
    main()
