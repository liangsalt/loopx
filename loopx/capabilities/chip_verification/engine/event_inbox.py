"""Durable adapter observations in the existing chip store, independent of sessions.

TypeScript owns protocol decisions. SQLite commits the inbox receipt and observed
run together. Remote evidence references are never read or accepted by this layer.
"""
from __future__ import annotations

import hashlib
import json
import time

from ....control_plane.effect_runtime import effect_runtime_result
from .models import ConflictError, StateError
from .state import _json

PROTOCOL = "chip_events_v1"


def initialize(project):
    with project.store._db(write=True) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS chip_event_inbox(
            cursor INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL, event_id TEXT NOT NULL, run_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL, envelope TEXT NOT NULL, disposition TEXT NOT NULL,
            receipt TEXT NOT NULL, received REAL NOT NULL, UNIQUE(source,event_id))""")
        db.execute("CREATE INDEX IF NOT EXISTS chip_events_run ON chip_event_inbox(run_id,cursor)")


def _exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chip_event_inbox'").fetchone() is not None


def register(project, request):
    """Register a stable adapter operation before dispatching it on an EDA host."""
    admitted = effect_runtime_result("chip_events.register", request)
    project.assert_inputs_current()
    initialize(project)
    store = project.store
    with store._db(write=True) as db:
        previous = store._decode(db.execute("SELECT * FROM runs WHERE id=?", (admitted["run_id"],)).fetchone(),
                                 ("metadata", "artifacts"))
        if previous:
            identity = previous["metadata"].get("registration", {})
            if (previous["metadata"].get("event_protocol") != PROTOCOL
                    or {key: identity.get(key) for key in admitted} != admitted):
                raise ConflictError("Run identity already belongs to different registration inputs")
            return {"status": "registered", "duplicate": True, "run": previous,
                    "execution_performed": False, "coverage_verified": False}
        task = store._task(db, admitted["task_id"])
        baseline = store._current(db)
        if task["baseline_id"] != baseline or task["status"] in {"stale", "cancelled", "blocked"}:
            raise ConflictError("External runs require a current, unblocked task")
        store._assert_not_held(db, task["id"])
        if admitted["attempt_id"]:
            store._active_attempt(db, task["id"], admitted["attempt_id"])
        now = time.time()
        registration = {**admitted, "baseline_id": baseline, "task_revision": task["revision"]}
        metadata = {"event_protocol": PROTOCOL, "registration": registration,
                    "last_sequence": 0, "lifecycle_sequence": 0, "last_heartbeat": now, "evidence_event_count": 0,
                    "input_digest": admitted["input_digest"], "coverage_verified": False}
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (admitted["run_id"], baseline, task["id"], admitted["attempt_id"], admitted["tool"],
                    "queued", _json(metadata), "[]", "current", now, now))
        store._event(db, "adapter_run_registered", {"run_id": admitted["run_id"], "source": admitted["source"]},
                     task["id"], admitted["attempt_id"])
        run = store._decode(db.execute("SELECT * FROM runs WHERE id=?", (admitted["run_id"],)).fetchone(),
                            ("metadata", "artifacts"))
        return {"status": "registered", "duplicate": False, "run": run,
                "execution_performed": False, "coverage_verified": False}


def ingest(project, envelope):
    """At-least-once input; an identical source/id returns the original receipt."""
    if not isinstance(envelope, dict):
        raise ValueError("Event envelope must be an object")
    encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("Event envelope exceeds 65536 UTF-8 bytes")
    source, event_id, run_id = (envelope.get(key) for key in ("source", "id", "run_id"))
    if any(not isinstance(value, str) or not value.strip() for value in (source, event_id, run_id)):
        raise ValueError("Event source, id and run_id are required")
    fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    store = project.store
    try:
        project.assert_inputs_current()
        inputs_current = True
    except (ValueError, OSError, RuntimeError):
        inputs_current = False
    with store._db(write=True) as db:
        if not _exists(db):
            raise StateError("No adapter run registered in this project")
        previous = db.execute("SELECT fingerprint,receipt FROM chip_event_inbox WHERE source=? AND event_id=?",
                              (source, event_id)).fetchone()
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise ConflictError("Event source/id was already delivered with a different payload")
            return {**json.loads(previous["receipt"]), "duplicate": True}
        run = store._decode(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone(),
                            ("metadata", "artifacts"))
        if not run or run["metadata"].get("event_protocol") != PROTOCOL:
            raise StateError("Unknown adapter run; register it before delivering events")
        metadata = run["metadata"]
        registration = metadata["registration"]
        task = store._task(db, run["task_id"])
        decision = effect_runtime_result("chip_events.reduce", {
            "event": envelope,
            "run": {"id": run_id, "source": registration["source"], "baseline_id": run["baseline_id"],
                    "status": run["status"], "validity": run["validity"],
                    "task_revision": registration["task_revision"], "attempt_id": run["attempt_id"],
                    "last_sequence": metadata["last_sequence"],
                    "lifecycle_sequence": metadata.get("lifecycle_sequence", 0)},
            "current": {"baseline_id": store._current(db), "task_revision": task["revision"],
                        "attempt_id": task["attempt_id"], "inputs_current": inputs_current},
        })
        event = decision["event"]
        now = time.time()
        # External execution may finish after its task scope was superseded.
        # Retain that physical fact without reviving the old business state.
        if decision["physical_terminal_observed"] and "observed_terminal" not in metadata:
            metadata["observed_terminal"] = {"type": event["type"], "sequence": event["sequence"],
                                             "event_id": event_id, "baseline_id": event["baseline_id"],
                                             "received": now}
            db.execute("UPDATE runs SET metadata=?,updated=? WHERE id=?", (_json(metadata), now, run_id))
        if decision["disposition"] == "applied":
            metadata["last_sequence"] = decision["last_sequence"]
            metadata["lifecycle_sequence"] = decision["lifecycle_sequence"]
            metadata["last_event_id"] = event_id
            metadata["last_event_type"] = event["type"]
            if decision["heartbeat_observed"]:
                metadata["last_heartbeat"] = now
            if event["type"] == "started":
                metadata.setdefault("started_at", now)
            if event["type"] in {"completed", "failed", "cancelled"}:
                metadata["completed_at"] = now
            if event["type"] == "failed":
                metadata["failure_signature"] = event["data"]["failure_signature"]
            if "message" in event["data"]:
                metadata["last_message"] = event["data"]["message"]
            if "progress" in event["data"]:
                metadata["claimed_progress"] = event["data"]["progress"]
            if "evidence" in event["data"]:
                metadata["observed_evidence"] = event["data"]["evidence"]
                metadata["evidence_event_count"] += 1
            db.execute("UPDATE runs SET status=?,metadata=?,updated=? WHERE id=?",
                       (decision["next_status"], _json(metadata), now, run_id))
        elif decision["disposition"] == "stale":
            # A malformed baseline label alone cannot invalidate a valid run.
            if (not inputs_current or run["baseline_id"] != store._current(db) or task["revision"] != registration["task_revision"]
                    or (run["attempt_id"] and run["attempt_id"] != task["attempt_id"])):
                db.execute("UPDATE runs SET validity='stale' WHERE id=?", (run_id,))
        inserted = db.execute("INSERT INTO chip_event_inbox(source,event_id,run_id,fingerprint,envelope,disposition,receipt,received) "
                              "VALUES(?,?,?,?,?,?,'{}',?)",
                              (source, event_id, run_id, fingerprint, encoded, decision["disposition"], now))
        receipt = {"cursor": inserted.lastrowid, "event_id": event_id, "source": source, "run_id": run_id,
                   "disposition": decision["disposition"], "run_status": decision["next_status"], "duplicate": False,
                   "physical_termination_observed": decision["physical_terminal_observed"],
                   "task_acceptance": False, "coverage_verified": False}
        db.execute("UPDATE chip_event_inbox SET receipt=? WHERE cursor=?", (_json(receipt), inserted.lastrowid))
        store._event(db, "adapter_event_received", {"cursor": inserted.lastrowid, "run_id": run_id,
                     "event_type": event["type"], "disposition": decision["disposition"]}, run["task_id"], run["attempt_id"])
        return receipt


def status(project, run_id=None, after=0, limit=50):
    """Read compact durable observations; raw data stays in the indexed inbox.

    A cursor is a stable evidence reference. A consumer persists its own cursor;
    this projection deliberately does not deliver entire log/evidence payloads.
    """
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("after must be nonnegative and limit must be in 1..200")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise ValueError("run_id must be nonempty text")
    store = project.store
    with store._db() as db:
        run = None
        if run_id is not None:
            run = store._decode(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone(), ("metadata", "artifacts"))
            if run is None or run["metadata"].get("event_protocol") != PROTOCOL:
                raise StateError("Unknown adapter run")
            metadata = run["metadata"]
            run["metadata"] = {key: metadata[key] for key in (
                "event_protocol", "registration", "last_sequence", "lifecycle_sequence", "last_heartbeat", "started_at",
                "completed_at", "failure_signature", "last_message", "evidence_event_count", "input_digest",
                "coverage_verified", "observed_terminal",
            ) if key in metadata}
            run["metadata"]["observed_evidence_count"] = len(metadata.get("observed_evidence", []))
            run["metadata"]["progress_is_unverified"] = "claimed_progress" in metadata
        if not _exists(db):
            return {"events": [], "next_cursor": after, "has_more": False, "run": run,
                    "coverage_verified": False}
        query = "SELECT * FROM chip_event_inbox WHERE cursor>?"
        params = [after]
        if run_id is not None:
            query += " AND run_id=?"
            params.append(run_id)
        rows = db.execute(query + " ORDER BY cursor LIMIT ?", (*params, limit + 1)).fetchall()
        events = []
        used = len(_json(run))
        for row in rows[:limit]:
            envelope = json.loads(row["envelope"])
            item = {key: row[key] for key in ("cursor", "received", "disposition", "fingerprint")}
            item.update({key: envelope[key] for key in ("id", "source", "run_id", "baseline_id", "sequence", "type")})
            item["payload_reference"] = "chip-event:" + str(row["cursor"])
            size = len(_json(item))
            if events and used + size > 16000:
                break
            events.append(item)
            used += size
        return {"events": events, "next_cursor": events[-1]["cursor"] if events else after,
                "has_more": len(rows) > len(events), "run": run, "coverage_verified": False}


def read_event(project, cursor, offset=0, limit=8000):
    """Resolve an immutable inbox reference in explicit, bounded text slices."""
    if type(cursor) is not int or cursor < 1 or type(offset) is not int or offset < 0:
        raise ValueError("cursor must be positive and offset nonnegative")
    if type(limit) is not int or not 1 <= limit <= 16000:
        raise ValueError("Event read limit must be in 1..16000 characters")
    with project.store._db() as db:
        if not _exists(db):
            raise StateError("Unknown event cursor")
        row = db.execute("SELECT envelope,fingerprint FROM chip_event_inbox WHERE cursor=?", (cursor,)).fetchone()
        if row is None:
            raise StateError("Unknown event cursor")
        body = row["envelope"]
        if offset > len(body):
            raise ValueError("Event read offset exceeds the stored payload length")
        end = min(len(body), offset + limit)
        return {"payload_reference": "chip-event:" + str(cursor), "cursor": cursor,
                "sha256": row["fingerprint"], "total_chars": len(body), "offset": offset,
                "next_offset": end, "has_more": end < len(body), "text": body[offset:end],
                "untrusted_observation": True, "coverage_verified": False}
