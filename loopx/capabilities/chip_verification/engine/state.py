"""Transactional project state, independent of any agent conversation.

An agent may propose a result. Only an application-supplied validator can accept
it, and only for the still-current input revision and claimed execution attempt.
Large material stays in files/source rows; status queries return compact views.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .models import ConflictError, RUN_TRANSITIONS, Stage, StateError, ValidationError


Validator = Callable[[dict, dict, list[dict]], dict]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def file_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class StateStore:
    """Project-local DV evidence ledger; callers may use separate instances.

    This ledger owns domain artifacts, not LoopX Goal, Todo or host authority.

    Keep the database on a local filesystem. Use a service boundary if several
    machines need to write it, rather than sharing this WAL database over NFS.
    """

    def __init__(self, project_dir: str | Path, *, initialize: bool = True):
        self.root = Path(project_dir).resolve()
        self.state_dir = self.root / ".loopx" / "chip-verification"
        self.db_path = self.state_dir / "state.sqlite3"
        self.validators: dict[str, Validator] = {}
        self._read_only = not initialize
        if not initialize:
            if not self.db_path.is_file():
                raise StateError("No chip verification state exists; import a specification first")
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS baselines(
                    id TEXT PRIMARY KEY,revision INTEGER UNIQUE NOT NULL,
                    name TEXT NOT NULL,spec_path TEXT NOT NULL,spec_hash TEXT NOT NULL,
                    spec_bytes INTEGER NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sources(
                    id TEXT NOT NULL,baseline_id TEXT NOT NULL REFERENCES baselines(id),
                    pointer TEXT NOT NULL,text TEXT NOT NULL,sha256 TEXT NOT NULL,
                    metadata TEXT NOT NULL,created REAL NOT NULL,
                    PRIMARY KEY(id,baseline_id));
                CREATE TABLE IF NOT EXISTS requirements(
                    id TEXT NOT NULL,baseline_id TEXT NOT NULL REFERENCES baselines(id),
                    text TEXT NOT NULL,source_ids TEXT NOT NULL,metadata TEXT NOT NULL,
                    created REAL NOT NULL,PRIMARY KEY(id,baseline_id));
                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY,baseline_id TEXT NOT NULL REFERENCES baselines(id),
                    kind TEXT NOT NULL,title TEXT NOT NULL,payload TEXT NOT NULL,
                    status TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,
                    attempt_id TEXT,result TEXT,reason TEXT,created REAL NOT NULL,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS dependencies(
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    depends_on TEXT NOT NULL REFERENCES tasks(id),
                    PRIMARY KEY(task_id,depends_on),CHECK(task_id<>depends_on));
                CREATE TABLE IF NOT EXISTS attempts(
                    id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),
                    baseline_id TEXT NOT NULL,session_id TEXT NOT NULL,
                    task_revision INTEGER NOT NULL,status TEXT NOT NULL,
                    lease_until REAL NOT NULL,started REAL NOT NULL,ended REAL,reason TEXT);
                CREATE TABLE IF NOT EXISTS artifacts(
                    id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),
                    attempt_id TEXT NOT NULL REFERENCES attempts(id),baseline_id TEXT NOT NULL,
                    path TEXT NOT NULL,sha256 TEXT NOT NULL,bytes INTEGER NOT NULL,
                    kind TEXT NOT NULL,validity TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS evidence(
                    id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),
                    attempt_id TEXT NOT NULL,baseline_id TEXT NOT NULL,
                    validator TEXT NOT NULL,checks TEXT NOT NULL,artifact_ids TEXT NOT NULL,
                    validity TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts(
                    operation_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,
                    task_id TEXT NOT NULL,receipt TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY,baseline_id TEXT NOT NULL,task_id TEXT,attempt_id TEXT,
                    tool TEXT NOT NULL,status TEXT NOT NULL,metadata TEXT NOT NULL,
                    artifacts TEXT NOT NULL,validity TEXT NOT NULL,
                    created REAL NOT NULL,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS issues(
                    id TEXT PRIMARY KEY,baseline_id TEXT NOT NULL,task_id TEXT,
                    category TEXT NOT NULL,description TEXT NOT NULL,status TEXT NOT NULL,
                    resolution TEXT,created REAL NOT NULL,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,
                    type TEXT NOT NULL,task_id TEXT,attempt_id TEXT,payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS tasks_status ON tasks(baseline_id,status);
                CREATE INDEX IF NOT EXISTS dependencies_parent ON dependencies(depends_on);
                CREATE INDEX IF NOT EXISTS sources_baseline ON sources(baseline_id);
            """)
            self._migrate_fragment_sources(db)

    @staticmethod
    def _migrate_fragment_sources(db: sqlite3.Connection) -> None:
        """Preserve early databases while allowing several fragments per pointer."""
        def has_pointer_constraint():
            for index in db.execute("PRAGMA index_list('sources')"):
                if index[2]:
                    columns = [item[2] for item in db.execute(f'PRAGMA index_info("{index[1]}")')]
                    if columns == ["baseline_id", "pointer"]:
                        return True
            return False

        if not has_pointer_constraint():
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            # Another process may have upgraded while this connection waited.
            if has_pointer_constraint():
                db.execute("CREATE TABLE sources_fragments_v2("
                           "id TEXT NOT NULL,baseline_id TEXT NOT NULL REFERENCES baselines(id),"
                           "pointer TEXT NOT NULL,text TEXT NOT NULL,sha256 TEXT NOT NULL,"
                           "metadata TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(id,baseline_id))")
                db.execute("INSERT INTO sources_fragments_v2 SELECT * FROM sources")
                db.execute("DROP TABLE sources")
                db.execute("ALTER TABLE sources_fragments_v2 RENAME TO sources")
                db.execute("CREATE INDEX sources_baseline ON sources(baseline_id)")
            db.commit()
        except BaseException:
            db.rollback()
            raise

    @contextmanager
    def _db(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        if self._read_only and write:
            raise StateError("This chip verification state was opened for inspection only")
        if self._read_only:
            db = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True,
                                 timeout=30, isolation_level=None)
            db.execute("PRAGMA query_only=ON")
        else:
            db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        if not self._read_only:
            db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, event_type, payload, task_id=None, attempt_id=None):
        db.execute("INSERT INTO events(at,type,task_id,attempt_id,payload) VALUES(?,?,?,?,?)",
                   (time.time(), event_type, task_id, attempt_id, _json(payload)))

    @staticmethod
    def _current(db) -> str:
        row = db.execute("SELECT value FROM settings WHERE key='baseline_id'").fetchone()
        if not row:
            raise StateError("No specification baseline has been initialized")
        return row[0]

    @staticmethod
    def _decode(row, fields=()) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        for field in fields:
            if result.get(field) is not None:
                result[field] = json.loads(result[field])
        return result

    def initialize(self, name: str, spec_path: str | Path, spec_hash: str,
                   spec_bytes: int, *, partitioning: str | None = None) -> dict:
        if not name or not spec_hash or spec_bytes < 0:
            raise ValueError("name, spec_hash and nonnegative spec_bytes are required")
        with self._db(write=True) as db:
            old_setting = db.execute("SELECT value FROM settings WHERE key='baseline_id'").fetchone()
            old_id = old_setting[0] if old_setting else None
            if old_id:
                old = db.execute("SELECT * FROM baselines WHERE id=?", (old_id,)).fetchone()
                strategy = db.execute("SELECT value FROM settings WHERE key='source_partitioning'").fetchone()
                same_strategy = partitioning is None or (strategy is not None and strategy[0] == partitioning)
                if old["spec_hash"] == spec_hash and old["spec_bytes"] == spec_bytes and same_strategy:
                    return dict(old)
            reason = "Specification source partitioning changed" if old_id and old["spec_hash"] == spec_hash else "Specification baseline changed"
            revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM baselines").fetchone()[0]
            baseline_id = f"baseline-{revision}-{spec_hash[:12]}"
            now = time.time()
            db.execute("INSERT INTO baselines VALUES(?,?,?,?,?,?,?)",
                       (baseline_id, revision, name, str(Path(spec_path).resolve()), spec_hash,
                        spec_bytes, now))
            db.execute("INSERT INTO settings(key,value) VALUES('baseline_id',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (baseline_id,))
            if partitioning is not None:
                db.execute("INSERT INTO settings(key,value) VALUES('source_partitioning',?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (partitioning,))
            else:
                db.execute("DELETE FROM settings WHERE key='source_partitioning'")
            if old_id:
                db.execute("UPDATE attempts SET status='superseded',ended=?,reason=? "
                           "WHERE status='running'", (now, reason))
                db.execute("UPDATE tasks SET status='stale',attempt_id=NULL,revision=revision+1,"
                           "reason=?,updated=? WHERE status<>'cancelled'",
                           (reason, now))
                db.execute("UPDATE artifacts SET validity='stale' WHERE validity='current'")
                db.execute("UPDATE evidence SET validity='stale' WHERE validity='current'")
                db.execute("UPDATE runs SET validity='stale' WHERE validity='current'")
            self._event(db, "baseline_initialized", {"baseline_id": baseline_id, "previous": old_id,
                                                     "spec_hash": spec_hash, "spec_bytes": spec_bytes,
                                                     "partitioning": partitioning, "reason": reason})
            return dict(db.execute("SELECT * FROM baselines WHERE id=?", (baseline_id,)).fetchone())

    def baseline(self) -> dict:
        with self._db() as db:
            return dict(db.execute("SELECT * FROM baselines WHERE id=?", (self._current(db),)).fetchone())

    def add_source(self, pointer: str, text: str, metadata: dict | None = None,
                   source_id: str | None = None) -> dict:
        metadata = metadata or {}
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._db(write=True) as db:
            baseline_id = self._current(db)
            source_id = source_id or "src-" + _digest([baseline_id, pointer, metadata.get("start_char"), metadata.get("end_char")])[:20]
            existing = db.execute("SELECT * FROM sources WHERE baseline_id=? AND id=?",
                                  (baseline_id, source_id)).fetchone()
            if existing:
                if existing["pointer"] != pointer or existing["sha256"] != sha:
                    raise ConflictError("Source identity already refers to different immutable content")
                return self._decode(existing, ("metadata",))
            db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?)",
                       (source_id, baseline_id, pointer, text, sha, _json(metadata), time.time()))
            return self._decode(db.execute("SELECT * FROM sources WHERE id=? AND baseline_id=?", (source_id, baseline_id)).fetchone(),
                                ("metadata",))

    def get_source(self, source_id: str, *, baseline_id: str | None = None) -> dict:
        with self._db() as db:
            row = self._decode(db.execute("SELECT * FROM sources WHERE id=? AND baseline_id=?", (source_id, baseline_id or self._current(db))).fetchone(),
                               ("metadata",))
            if row is None:
                raise KeyError(source_id)
            return row

    def list_sources(self, limit: int | None = None, *, include_text: bool = False,
                     baseline_id: str | None = None) -> list[dict]:
        with self._db() as db:
            query = "SELECT * FROM sources WHERE baseline_id=? ORDER BY pointer"
            params: list[Any] = [baseline_id or self._current(db)]
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
            rows = [self._decode(row, ("metadata",)) for row in db.execute(query, params)]
            if not include_text:
                for row in rows:
                    row["characters"] = len(row.pop("text"))
            return rows

    def add_requirement(self, text: str, source_ids: list[str] | None = None,
                        metadata: dict | None = None, requirement_id: str | None = None) -> dict:
        source_ids = sorted(set(source_ids or []))
        if not text.strip() or not source_ids:
            raise ValidationError("A requirement needs nonempty text and original-source references")
        metadata = metadata or {}
        with self._db(write=True) as db:
            baseline_id = self._current(db)
            for source_id in source_ids:
                source = db.execute("SELECT baseline_id FROM sources WHERE id=? AND baseline_id=?", (source_id, baseline_id)).fetchone()
                if not source or source[0] != baseline_id:
                    raise ValidationError(f"Unknown or stale requirement source: {source_id}")
            requirement_id = requirement_id or "req-" + _digest([baseline_id, text, source_ids])[:20]
            existing = db.execute("SELECT * FROM requirements WHERE id=? AND baseline_id=?", (requirement_id, baseline_id)).fetchone()
            if existing:
                if (existing["baseline_id"] != baseline_id or existing["text"] != text
                        or existing["source_ids"] != _json(source_ids)):
                    raise ConflictError("Requirement identity already exists with different content")
                return self._decode(existing, ("source_ids", "metadata"))
            db.execute("INSERT INTO requirements VALUES(?,?,?,?,?,?)",
                       (requirement_id, baseline_id, text, _json(source_ids), _json(metadata), time.time()))
            return self._decode(db.execute("SELECT * FROM requirements WHERE id=? AND baseline_id=?",
                                           (requirement_id, baseline_id)).fetchone(), ("source_ids", "metadata"))

    def get_requirement(self, requirement_id: str, *, baseline_id: str | None = None) -> dict:
        with self._db() as db:
            row = self._decode(db.execute("SELECT * FROM requirements WHERE id=? AND baseline_id=?",
                                          (requirement_id, baseline_id or self._current(db))).fetchone(), ("source_ids", "metadata"))
            if row is None:
                raise KeyError(requirement_id)
            return row

    def list_requirements(self, limit: int | None = None, *, baseline_id: str | None = None) -> list[dict]:
        with self._db() as db:
            query = "SELECT * FROM requirements WHERE baseline_id=? ORDER BY id"
            params: list[Any] = [baseline_id or self._current(db)]
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
            return [self._decode(row, ("source_ids", "metadata")) for row in db.execute(query, params)]

    def add_task(self, kind: str, title: str, payload: dict | None = None,
                 depends_on: list[str] | None = None, task_id: str | None = None) -> dict:
        kind = Stage(kind).value
        if not title.strip():
            raise ValueError("A task needs a title")
        payload = payload or {}
        depends_on = sorted(set(depends_on or []))
        with self._db(write=True) as db:
            baseline_id = self._current(db)
            task_id = task_id or "task-" + _digest([baseline_id, kind, title, payload, depends_on])[:20]
            existing = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if existing:
                task = self._task(db, task_id)
                if (task["baseline_id"] != baseline_id or task["kind"] != kind or task["title"] != title
                        or task["payload"] != payload or task["depends_on"] != depends_on):
                    raise ConflictError("Task identity already exists with different input")
                return task
            for dependency_id in depends_on:
                dependency = db.execute("SELECT baseline_id FROM tasks WHERE id=?", (dependency_id,)).fetchone()
                if not dependency or dependency[0] != baseline_id:
                    raise ValidationError(f"Dependency is missing or belongs to another baseline: {dependency_id}")
                if dependency_id == task_id:
                    raise ValidationError("A task cannot depend on itself")
            now = time.time()
            db.execute("INSERT INTO tasks(id,baseline_id,kind,title,payload,status,created,updated) "
                       "VALUES(?,?,?,?,?,'pending',?,?)",
                       (task_id, baseline_id, kind, title, _json(payload), now, now))
            for dependency_id in depends_on:
                db.execute("INSERT INTO dependencies VALUES(?,?)", (task_id, dependency_id))
            self._event(db, "task_created", {"kind": kind, "depends_on": depends_on}, task_id)
            return self._task(db, task_id)

    def _task(self, db, task_id):
        task = self._decode(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone(),
                            ("payload", "result"))
        if task is None:
            raise KeyError(task_id)
        task["depends_on"] = [row[0] for row in db.execute(
            "SELECT depends_on FROM dependencies WHERE task_id=? ORDER BY depends_on", (task_id,))]
        return task

    def get_task(self, task_id: str) -> dict:
        with self._db() as db:
            return self._task(db, task_id)

    def list_tasks(self, status: str | None = None, *, kind: str | None = None,
                   baseline_id: str | None = None, limit: int | None = None) -> list[dict]:
        with self._db() as db:
            query = "SELECT id FROM tasks WHERE baseline_id=?"
            params: list[Any] = [baseline_id or self._current(db)]
            if status:
                query += " AND status=?"
                params.append(status)
            if kind:
                query += " AND kind=?"
                params.append(kind)
            query += " ORDER BY created,id"
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
            return [self._task(db, row[0]) for row in db.execute(query, params)]

    @staticmethod
    def _unmet(db, task_id: str, baseline_id: str) -> list[str]:
        return [row[0] for row in db.execute(
            "SELECT p.id FROM dependencies d JOIN tasks p ON p.id=d.depends_on "
            "WHERE d.task_id=? AND (p.status<>'done' OR p.baseline_id<>?)",
            (task_id, baseline_id))]

    def ready_tasks(self, limit: int = 20) -> list[dict]:
        with self._db() as db:
            baseline_id = self._current(db)
            hold_filter = (
                "AND NOT EXISTS(SELECT 1 FROM watchdog_holds h WHERE h.task_id=t.id "
                "AND h.baseline_id=t.baseline_id AND h.active=1) "
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchdog_holds'").fetchone()
                else "")
            ids = db.execute(
                "SELECT t.id FROM tasks t WHERE t.baseline_id=? AND t.status='pending' "
                "AND NOT EXISTS(SELECT 1 FROM dependencies d JOIN tasks p ON p.id=d.depends_on "
                "WHERE d.task_id=t.id AND (p.status<>'done' OR p.baseline_id<>t.baseline_id)) "
                + hold_filter + "ORDER BY t.created,t.id LIMIT ?", (baseline_id, limit)).fetchall()
            return [self._task(db, row[0]) for row in ids]

    def _assert_not_held(self, db, task_id):
        """A durable review hold fences retries without changing task evidence."""
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchdog_holds'").fetchone():
            held = db.execute("SELECT incident_id FROM watchdog_holds WHERE task_id=? AND baseline_id=? AND active=1",
                              (task_id, self._current(db))).fetchone()
            if held:
                raise ConflictError("Watchdog holds this work; inspect and resolve incident " + held[0])

    def claim(self, task_id: str, session_id: str, lease_seconds: float = 300) -> dict:
        if not session_id or lease_seconds <= 0:
            raise ValueError("A session identity and positive lease duration are required")
        with self._db(write=True) as db:
            task = self._task(db, task_id)
            self._assert_not_held(db, task_id)
            baseline_id = self._current(db)
            if task["baseline_id"] != baseline_id or task["status"] != "pending":
                raise ConflictError(f"Task cannot be claimed from {task['status']}")
            unmet = self._unmet(db, task_id, baseline_id)
            if unmet:
                raise StateError("Unmet prerequisites: " + ", ".join(unmet))
            now = time.time()
            attempt_id = "attempt-" + uuid.uuid4().hex
            revision = task["revision"] + 1
            db.execute("UPDATE tasks SET status='running',attempt_id=?,revision=?,reason=NULL,updated=? "
                       "WHERE id=?", (attempt_id, revision, now, task_id))
            db.execute("INSERT INTO attempts(id,task_id,baseline_id,session_id,task_revision,status,"
                       "lease_until,started) VALUES(?,?,?,?,?,'running',?,?)",
                       (attempt_id, task_id, baseline_id, session_id, revision, now + lease_seconds, now))
            self._event(db, "task_claimed", {"session_id": session_id, "revision": revision,
                                            "lease_until": now + lease_seconds}, task_id, attempt_id)
            return dict(db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone())

    def _active_attempt(self, db, task_id, attempt_id):
        task = self._task(db, task_id)
        attempt = db.execute("SELECT * FROM attempts WHERE id=? AND task_id=?",
                             (attempt_id, task_id)).fetchone()
        if (not attempt or task["status"] != "running" or task["attempt_id"] != attempt_id
                or attempt["status"] != "running" or task["revision"] != attempt["task_revision"]
                or task["baseline_id"] != self._current(db)):
            raise ConflictError("Attempt no longer owns the current task revision")
        if attempt["lease_until"] <= time.time():
            raise ConflictError("Attempt lease expired; recover before claiming new work")
        return task, dict(attempt)

    def heartbeat(self, task_id: str, attempt_id: str, lease_seconds: float = 300) -> dict:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._db(write=True) as db:
            self._active_attempt(db, task_id, attempt_id)
            db.execute("UPDATE attempts SET lease_until=? WHERE id=?", (time.time() + lease_seconds, attempt_id))
            return dict(db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone())

    def register_validator(self, kind: str, validator: Validator) -> None:
        if not callable(validator):
            raise TypeError("validator must be callable")
        self.validators[Stage(kind).value] = validator

    def _artifact_records(self, artifacts: list[str | Path | dict]) -> list[dict]:
        records = []
        seen: set[str] = set()
        for value in artifacts:
            item = dict(value) if isinstance(value, dict) else {"path": str(value)}
            if not item.get("path"):
                raise ValidationError("An artifact needs a path")
            path = Path(item["path"])
            path = (path if path.is_absolute() else self.root / path).resolve()
            try:
                relative = path.relative_to(self.root).as_posix()
            except ValueError as error:
                raise ValidationError("Task artifacts must be inside the project directory") from error
            if not path.is_file():
                raise ValidationError(f"Artifact does not exist: {relative}")
            if relative in seen:
                continue
            seen.add(relative)
            sha = file_hash(path)
            if item.get("sha256") and item["sha256"] != sha:
                raise ValidationError(f"Artifact content does not match supplied digest: {relative}")
            records.append({"path": relative, "sha256": sha, "bytes": path.stat().st_size,
                            "kind": str(item.get("kind", "file"))})
        return records

    def finish(self, task_id: str, attempt_id: str, result: dict,
               artifacts: list[str | Path | dict] | None = None, *,
               validator: Validator | None = None, operation_id: str | None = None) -> dict:
        if not isinstance(result, dict):
            raise ValidationError("Result must be a mapping")
        artifacts = artifacts or []
        operation_id = operation_id or "finish-" + attempt_id
        normalized = [dict(value) if isinstance(value, dict) else {"path": str(value)} for value in artifacts]
        fingerprint = _digest([task_id, attempt_id, result, normalized])
        with self._db() as db:
            previous = db.execute("SELECT * FROM receipts WHERE operation_id=?", (operation_id,)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ConflictError("Idempotency key was reused for a different submission")
                receipt = json.loads(previous["receipt"])
                receipt["replayed"] = True
                receipt["current_task_status"] = self._task(db, task_id)["status"]
                return receipt
            task, attempt = self._active_attempt(db, task_id, attempt_id)
        selected_validator = validator or self.validators.get(task["kind"])
        if selected_validator is None:
            raise ValidationError("Completion requires an application-supplied trusted validator")
        records = self._artifact_records(artifacts)
        if not records:
            raise ValidationError("Completion requires at least one durable evidence artifact")
        verdict = selected_validator(task, result, records)
        if (not isinstance(verdict, dict) or verdict.get("passed") is not True
                or not isinstance(verdict.get("checks"), list) or not verdict["checks"]):
            raise ValidationError("Validator did not establish a passing result with explicit checks")
        # Validation can take time. Recheck ownership, baseline and bytes at commit.
        with self._db(write=True) as db:
            previous = db.execute("SELECT * FROM receipts WHERE operation_id=?", (operation_id,)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ConflictError("Idempotency key was reused for a different submission")
                receipt = json.loads(previous["receipt"])
                receipt["replayed"] = True
                receipt["current_task_status"] = self._task(db, task_id)["status"]
                return receipt
            task, attempt = self._active_attempt(db, task_id, attempt_id)
            if self._unmet(db, task_id, task["baseline_id"]):
                raise ConflictError("A prerequisite became invalid during execution")
            for record in records:
                path = self.root / record["path"]
                if not path.is_file() or file_hash(path) != record["sha256"]:
                    raise ConflictError("Evidence changed during validation")
            now = time.time()
            artifact_ids = []
            for record in records:
                artifact_id = "artifact-" + _digest([attempt_id, record["path"], record["sha256"]])[:20]
                db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (artifact_id, task_id, attempt_id, task["baseline_id"], record["path"],
                            record["sha256"], record["bytes"], record["kind"], "current", now))
                artifact_ids.append(artifact_id)
            evidence_id = "evidence-" + uuid.uuid4().hex
            validator_name = str(verdict.get("validator") or getattr(selected_validator, "__name__", "validator"))
            db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?)",
                       (evidence_id, task_id, attempt_id, task["baseline_id"], validator_name,
                        _json(verdict["checks"]), _json(artifact_ids), "current", now))
            db.execute("UPDATE attempts SET status='accepted',ended=? WHERE id=?", (now, attempt_id))
            db.execute("UPDATE tasks SET status='done',result=?,attempt_id=NULL,revision=revision+1,"
                       "reason=NULL,updated=? WHERE id=?", (_json(result), now, task_id))
            receipt = {"operation_id": operation_id, "task_id": task_id, "attempt_id": attempt_id,
                       "status": "done", "baseline_id": task["baseline_id"],
                       "revision": task["revision"] + 1, "evidence_id": evidence_id,
                       "artifact_ids": artifact_ids, "accepted_at": now}
            db.execute("INSERT INTO receipts VALUES(?,?,?,?,?)",
                       (operation_id, fingerprint, task_id, _json(receipt), now))
            self._event(db, "task_accepted", receipt, task_id, attempt_id)
            return receipt

    def fail(self, task_id: str, attempt_id: str, reason: str, retryable: bool = True) -> dict:
        if not reason:
            raise ValueError("Failure needs a concrete reason")
        with self._db(write=True) as db:
            self._active_attempt(db, task_id, attempt_id)
            now = time.time()
            status = "pending" if retryable else "failed"
            db.execute("UPDATE attempts SET status='failed',ended=?,reason=? WHERE id=?", (now, reason, attempt_id))
            db.execute("UPDATE tasks SET status=?,attempt_id=NULL,revision=revision+1,reason=?,updated=? WHERE id=?",
                       (status, reason, now, task_id))
            self._event(db, "task_failed", {"reason": reason, "retryable": retryable}, task_id, attempt_id)
            return self._task(db, task_id)

    def block(self, task_id: str, reason: str, *, expected_revision: int | None = None) -> dict:
        return self._stop_task(task_id, "blocked", reason, expected_revision=expected_revision)

    def cancel(self, task_id: str, reason: str = "Cancelled by request") -> dict:
        return self._stop_task(task_id, "cancelled", reason)

    def _stop_task(self, task_id, status, reason, *, expected_revision=None):
        if not reason:
            raise ValueError("A stopped task needs a reason")
        with self._db(write=True) as db:
            return self._stop_task_current(db, task_id, status, reason, expected_revision=expected_revision)

    def _stop_task_current(self, db, task_id, status, reason, *, expected_revision=None):
        task = self._task(db, task_id)
        if expected_revision is not None and (task["revision"] != expected_revision or
                                              task["baseline_id"] != self._current(db)):
            raise ConflictError("Task changed while preparing its blocker")
        if task["status"] == "done":
            raise StateError("Invalidate accepted evidence before stopping completed work")
        now = time.time()
        if task["attempt_id"]:
            db.execute("UPDATE attempts SET status=?,ended=?,reason=? WHERE id=?",
                       (status, now, reason, task["attempt_id"]))
        db.execute("UPDATE tasks SET status=?,attempt_id=NULL,revision=revision+1,reason=?,updated=? WHERE id=?",
                   (status, reason, now, task_id))
        self._event(db, "task_" + status, {"reason": reason}, task_id)
        return self._task(db, task_id)

    def block_context(self, task_id: str, expected_revision: int, reason: str, description: str) -> tuple[dict, dict]:
        """Record an unsent UVM context blocker and its evidence in one transaction."""
        if not reason or not description:
            raise ValueError("A context blocker needs a reason and evidence")
        with self._db(write=True) as db:
            task = self._task(db, task_id)
            if task["status"] not in {"pending", "blocked"}:
                raise ConflictError("Task changed while preparing its context budget blocker")
            blocked = self._stop_task_current(db, task_id, "blocked", reason, expected_revision=expected_revision)
            issue_id = "issue-" + _digest([task["baseline_id"], "uvm_context_blocker", description, task_id, expected_revision])[:20]
            issue = self._add_issue_current(db, "uvm_context_blocker", description, task_id=task_id, issue_id=issue_id)
            return blocked, issue

    def recover_context(self, expected: dict, issues: list[dict], resolution: str) -> tuple[dict, bool]:
        """Resolve admitted context blockers without replacing another revision or lease."""
        if not issues or not resolution:
            raise ValueError("Context recovery needs blocker evidence and a resolution")
        with self._db(write=True) as db:
            current = self._task(db, expected["id"])
            if (current["baseline_id"] != self._current(db) or current["baseline_id"] != expected["baseline_id"]
                    or current["payload"] != expected["payload"]):
                raise ConflictError("Task inputs changed while recovering its context budget blocker")
            if current["status"] not in {"blocked", "pending"}:
                return current, False
            requeue = current["status"] == "blocked"
            planning_prefix = "Planning context exceeds budget:"
            planning = (current["kind"] == "plan" and all(
                issue.get("category") == "context_blocker" and
                (issue.get("description") or "").startswith(planning_prefix) for issue in issues))
            allowed_revision = expected["revision"] + (1 if expected["status"] == "blocked" and not requeue else 0)
            if current["revision"] != allowed_revision:
                return current, False
            if requeue and (current["reason"] != expected["reason"] or
                            not (current["reason"] or "").startswith(planning_prefix if planning else "uvm_context_blocker:")):
                return current, False
            if requeue and planning and not any(issue.get("description") == current["reason"] for issue in issues):
                return current, False
            unresolved = []
            for issue in issues:
                row = db.execute("SELECT * FROM issues WHERE id=?", (issue["id"],)).fetchone()
                if (not row or row["category"] != ("context_blocker" if planning else "uvm_context_blocker") or row["task_id"] != current["id"]
                        or row["baseline_id"] != current["baseline_id"]
                        or any(row[key] != issue[key] for key in ("baseline_id", "task_id", "category", "description"))
                        or row["status"] not in ({"open"} if requeue else {"open", "resolved"})):
                    return current, False
                if not planning:
                    try:
                        details = json.loads(row["description"])
                    except (ValueError, TypeError):
                        return current, False
                    if not isinstance(details, dict) or details.get("input_fingerprint") != current["payload"].get("input_fingerprint"):
                        return current, False
                if row["status"] == "open":
                    unresolved.append(issue["id"])
            now = time.time()
            if requeue:
                db.execute("UPDATE tasks SET status='pending',result=NULL,attempt_id=NULL,"
                           "revision=revision+1,reason=NULL,updated=? WHERE id=?", (now, current["id"]))
                self._event(db, "task_requeued", {}, current["id"])
            for issue_id in unresolved:
                db.execute("UPDATE issues SET status='resolved',resolution=?,updated=? WHERE id=?", (resolution, now, issue_id))
                self._event(db, "issue_resolved", {"issue_id": issue_id, "resolution": resolution})
            return self._task(db, current["id"]), True

    def requeue(self, task_id: str, *, payload: dict | None = None) -> dict:
        with self._db(write=True) as db:
            task = self._task(db, task_id)
            self._assert_not_held(db, task_id)
            if task["status"] not in {"failed", "blocked", "stale", "cancelled"}:
                raise StateError(f"Cannot requeue a task in {task['status']}")
            baseline_id = self._current(db)
            if task["baseline_id"] != baseline_id:
                raise ConflictError("Create new-baseline work; old task inputs cannot be silently rebased")
            db.execute("UPDATE tasks SET status='pending',payload=?,result=NULL,attempt_id=NULL,"
                       "revision=revision+1,reason=NULL,updated=? WHERE id=?",
                       (_json(payload if payload is not None else task["payload"]), time.time(), task_id))
            self._event(db, "task_requeued", {}, task_id)
            return self._task(db, task_id)

    def recover_expired(self, *, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        with self._db(write=True) as db:
            expired = db.execute("SELECT * FROM attempts WHERE status='running' AND lease_until<=?", (now,)).fetchall()
            recovered = []
            for attempt in expired:
                task = self._task(db, attempt["task_id"])
                try:
                    self._assert_not_held(db, task["id"])
                except ConflictError:
                    # An independent review owns recovery of held work. Other
                    # tasks may still recover through the normal host path.
                    continue
                db.execute("UPDATE attempts SET status='abandoned',ended=?,reason=? WHERE id=?",
                           (now, "Execution lease expired", attempt["id"]))
                if task["status"] == "running" and task["attempt_id"] == attempt["id"]:
                    status = "pending" if task["baseline_id"] == self._current(db) else "stale"
                    db.execute("UPDATE tasks SET status=?,attempt_id=NULL,revision=revision+1,reason=?,updated=? WHERE id=?",
                               (status, "Previous execution lease expired", now, task["id"]))
                    recovered.append(task["id"])
                    self._event(db, "attempt_recovered", {"next_status": status}, task["id"], attempt["id"])
            return recovered

    def _invalidate(self, db, task_id, reason):
        self._task(db, task_id)
        rows = db.execute("WITH RECURSIVE affected(id) AS (SELECT ? UNION SELECT d.task_id "
                          "FROM dependencies d JOIN affected a ON d.depends_on=a.id) SELECT id FROM affected",
                          (task_id,)).fetchall()
        ids = [row[0] for row in rows]
        now = time.time()
        for affected_id in ids:
            db.execute("UPDATE attempts SET status='superseded',ended=?,reason=? WHERE task_id=? AND status='running'",
                       (now, reason, affected_id))
            db.execute("UPDATE tasks SET status='stale',attempt_id=NULL,revision=revision+1,reason=?,updated=? WHERE id=?",
                       (reason, now, affected_id))
            db.execute("UPDATE artifacts SET validity='stale' WHERE task_id=?", (affected_id,))
            db.execute("UPDATE evidence SET validity='stale' WHERE task_id=?", (affected_id,))
            db.execute("UPDATE runs SET validity='stale' WHERE task_id=?", (affected_id,))
        self._event(db, "work_invalidated", {"reason": reason, "affected_tasks": ids}, task_id)
        return ids

    def invalidate(self, task_id: str, reason: str) -> list[str]:
        if not reason:
            raise ValueError("Invalidation needs a reason")
        with self._db(write=True) as db:
            return self._invalidate(db, task_id, reason)

    def verify_artifacts(self) -> list[dict]:
        """Detect changed/missing accepted files; invalidate their dependent work."""
        with self._db() as db:
            artifacts = [dict(row) for row in db.execute("SELECT * FROM artifacts WHERE validity='current'")]
        changes = []
        for artifact in artifacts:
            path = self.root / artifact["path"]
            actual = file_hash(path) if path.is_file() else None
            if actual != artifact["sha256"]:
                changes.append({"artifact_id": artifact["id"], "task_id": artifact["task_id"],
                                "path": artifact["path"], "expected_sha256": artifact["sha256"],
                                "actual_sha256": actual})
        with self._db(write=True) as db:
            for task_id in sorted({item["task_id"] for item in changes}):
                self._invalidate(db, task_id, "Accepted artifact changed or disappeared")
        return changes

    def list_artifacts(self, *, task_id: str | None = None, validity: str | None = None) -> list[dict]:
        with self._db() as db:
            query = "SELECT * FROM artifacts WHERE 1=1"
            params = []
            if task_id:
                query += " AND task_id=?"
                params.append(task_id)
            if validity:
                query += " AND validity=?"
                params.append(validity)
            return [dict(row) for row in db.execute(query + " ORDER BY created,id", params)]

    def record_run(self, run_id: str, tool: str, status: str = "queued", *,
                   task_id: str | None = None, attempt_id: str | None = None,
                   metadata: dict | None = None, artifacts: list | None = None) -> dict:
        """Register an external job; its lifetime is independent of a session.

        A run status is an observation, never a task-completion certificate.
        Domain validators must inspect simulator evidence before accepting work.
        """
        if status not in RUN_TRANSITIONS or not run_id or not tool:
            raise ValueError("A run needs an identity, tool and supported status")
        metadata, artifacts = metadata or {}, artifacts or []
        if "event_protocol" in metadata:
            raise ValueError("event_protocol is reserved for the event registration owner")
        with self._db(write=True) as db:
            baseline_id = self._current(db)
            existing = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if existing:
                if (existing["tool"] != tool or existing["baseline_id"] != baseline_id
                        or existing["task_id"] != task_id or existing["attempt_id"] != attempt_id):
                    raise ConflictError("Run identity is already registered to different work")
                return self._decode(existing, ("metadata", "artifacts"))
            if task_id:
                self._assert_not_held(db, task_id)
                task = self._task(db, task_id)
                if task["baseline_id"] != baseline_id:
                    raise ConflictError("Cannot submit a run for stale work")
            if attempt_id:
                if not task_id:
                    raise ValueError("An attempt-bound run also needs task_id")
                self._active_attempt(db, task_id, attempt_id)
            now = time.time()
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (run_id, baseline_id, task_id, attempt_id, tool, status, _json(metadata),
                        _json(artifacts), "current", now, now))
            self._event(db, "run_registered", {"run_id": run_id, "tool": tool, "status": status}, task_id, attempt_id)
            return self._decode(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone(), ("metadata", "artifacts"))

    def update_run(self, run_id: str, status: str, *, metadata: dict | None = None,
                   artifacts: list | None = None) -> dict:
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            if status != row["status"] and status not in RUN_TRANSITIONS.get(row["status"], set()):
                raise StateError(f"Invalid external run transition: {row['status']} -> {status}")
            merged_metadata = json.loads(row["metadata"])
            if merged_metadata.get("event_protocol"):
                raise StateError("Event-managed runs must be updated through event-ingest")
            merged_metadata.update(metadata or {})
            validity = "current" if row["baseline_id"] == self._current(db) and row["validity"] == "current" else "stale"
            db.execute("UPDATE runs SET status=?,metadata=?,artifacts=?,validity=?,updated=? WHERE id=?",
                       (status, _json(merged_metadata), _json(artifacts if artifacts is not None else json.loads(row["artifacts"])),
                        validity, time.time(), run_id))
            self._event(db, "run_updated", {"run_id": run_id, "status": status, "validity": validity}, row["task_id"], row["attempt_id"])
            return self._decode(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone(), ("metadata", "artifacts"))

    def get_run(self, run_id: str) -> dict:
        with self._db() as db:
            row = self._decode(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone(), ("metadata", "artifacts"))
            if row is None:
                raise KeyError(run_id)
            return row

    def list_runs(self, *, baseline_id: str | None = None) -> list[dict]:
        with self._db() as db:
            return [self._decode(row, ("metadata", "artifacts")) for row in db.execute(
                "SELECT * FROM runs WHERE baseline_id=? ORDER BY created,id", (baseline_id or self._current(db),))]

    def add_issue(self, category: str, description: str, *, task_id: str | None = None,
                  issue_id: str | None = None) -> dict:
        if not category or not description:
            raise ValueError("An issue needs a category and description")
        with self._db(write=True) as db:
            return self._add_issue_current(db, category, description, task_id=task_id, issue_id=issue_id)

    def _add_issue_current(self, db, category, description, *, task_id=None, issue_id=None):
        baseline_id = self._current(db)
        issue_id = issue_id or "issue-" + _digest([baseline_id, category, description, task_id])[:20]
        now = time.time()
        if task_id:
            self._task(db, task_id)
        db.execute("INSERT OR IGNORE INTO issues VALUES(?,?,?,?,?,'open',NULL,?,?)",
                   (issue_id, baseline_id, task_id, category, description, now, now))
        return dict(db.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone())

    def resolve_issue(self, issue_id: str, resolution: str) -> dict:
        if not resolution:
            raise ValueError("Resolution must explain how the issue was handled")
        with self._db(write=True) as db:
            if not db.execute("SELECT id FROM issues WHERE id=?", (issue_id,)).fetchone():
                raise KeyError(issue_id)
            db.execute("UPDATE issues SET status='resolved',resolution=?,updated=? WHERE id=?",
                       (resolution, time.time(), issue_id))
            self._event(db, "issue_resolved", {"issue_id": issue_id, "resolution": resolution})
            return dict(db.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone())

    def list_issues(self, *, status: str | None = "open") -> list[dict]:
        with self._db() as db:
            query = "SELECT * FROM issues WHERE baseline_id=?"
            params = [self._current(db)]
            if status:
                query += " AND status=?"
                params.append(status)
            return [dict(row) for row in db.execute(query + " ORDER BY created,id", params)]

    def events(self, limit: int = 100, after: int = 0) -> list[dict]:
        with self._db() as db:
            return [self._decode(row, ("payload",)) for row in db.execute(
                "SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit))]

    def status(self) -> dict:
        with self._db() as db:
            try:
                baseline_id = self._current(db)
            except StateError:
                return {"project": str(self.root), "baseline": None, "tasks": {}, "task_count": 0,
                        "ready_count": 0, "source_count": 0, "requirement_count": 0,
                        "artifact_count": 0, "runs": {}, "open_issues": 0}
            baseline = dict(db.execute("SELECT * FROM baselines WHERE id=?", (baseline_id,)).fetchone())
            counts = {row[0]: row[1] for row in db.execute(
                "SELECT status,COUNT(*) FROM tasks WHERE baseline_id=? GROUP BY status", (baseline_id,))}
            runs = {row[0]: row[1] for row in db.execute(
                "SELECT status,COUNT(*) FROM runs WHERE baseline_id=? GROUP BY status", (baseline_id,))}
            result = {"project": str(self.root), "baseline": baseline, "tasks": counts,
                      "task_count": sum(counts.values()), "runs": runs}
            for table, field in (("sources", "source_count"), ("requirements", "requirement_count"), ("artifacts", "artifact_count")):
                result[field] = db.execute(f"SELECT COUNT(*) FROM {table} WHERE baseline_id=?", (baseline_id,)).fetchone()[0]
            result["open_issues"] = db.execute("SELECT COUNT(*) FROM issues WHERE baseline_id=? AND status='open'", (baseline_id,)).fetchone()[0]
            result["ready_count"] = db.execute(
                "SELECT COUNT(*) FROM tasks t WHERE t.baseline_id=? AND t.status='pending' "
                "AND NOT EXISTS(SELECT 1 FROM dependencies d JOIN tasks p ON p.id=d.depends_on "
                "WHERE d.task_id=t.id AND (p.status<>'done' OR p.baseline_id<>t.baseline_id))", (baseline_id,)).fetchone()[0]
            return result
