"""Durable, bounded domain work exchanged with an IDE's native model worker.

The IDE owns its conversations. These tickets fence candidate artifact acceptance;
they neither create LoopX Goals nor prescribe a generic agent scheduler.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from .engine.models import ConflictError, ValidationError
from .engine.workflow import save_json

LEASE_SECONDS = 1800
HOST_INSTRUCTION = "Use only the supplied task material for design interpretation. Restrictions on tools inside the task prompt do not prohibit the required LoopX MCP heartbeat/submit/abandon protocol. Submit the JSON response through dv_submit; do not dump it into the parent conversation."


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def initialize(project):
    with project.store._db(write=True) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS host_tickets(
            attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), task_id TEXT NOT NULL,
            contract TEXT NOT NULL, request TEXT NOT NULL, response_hash TEXT, receipt TEXT)""")
        db.execute("""CREATE TABLE IF NOT EXISTS host_generations(
            id TEXT PRIMARY KEY, baseline_id TEXT NOT NULL, request TEXT NOT NULL)""")


def ticket(project, attempt_id, *, active=False):
    with project.store._db() as db:
        row = db.execute("SELECT * FROM host_tickets WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown host attempt; claim work in this workspace first")
        result = dict(row)
        result["contract"] = json.loads(result["contract"])
        result["request"] = json.loads(result["request"])
        result["receipt"] = json.loads(result["receipt"]) if result["receipt"] else None
        if active:
            _, attempt = project.store._active_attempt(db, result["task_id"], attempt_id)
            result["attempt"] = attempt
    return result


def summary(project, item):
    task = project.store.get_task(item["task_id"])
    contract = item["contract"]
    return {"status": "awaiting_host", "task_id": task["id"], "kind": task["kind"],
            "attempt_id": item["attempt_id"], "context_chars": contract["context_chars"],
            "context_budget_chars": contract["context_budget_chars"],
            "lease_seconds": LEASE_SECONDS, "next_action": "context",
            "instruction": "Delegate this attempt to the native worker; only that worker reads its context."}


def prepare(project, task, prompt, schema, session_id, request=None, budget=None):
    budget = budget or project.config.get("host_context_chars", project.config["context_chars"])
    contract = {"task_id": task["id"], "kind": task["kind"], "prompt": prompt,
                "host_instruction": HOST_INSTRUCTION, "output_schema": schema, "context_budget_chars": budget}
    # Count the actual serialized payload delivered to the worker, including schema.
    required = len(encoded(contract)) + 300
    if required > budget:
        raise ValidationError(f"Complete host context needs {required} characters; budget is {budget}. Split the scope or explicitly increase its budget; no dependency was truncated.")
    contract["context_chars"] = required
    contract["contract_sha256"] = digest(contract)
    attempt = project.store.claim(task["id"], session_id, lease_seconds=LEASE_SECONDS)
    try:
        directory = project.store.state_dir / "attempts" / attempt["id"]
        save_json(directory / "host-context.json", contract)
        with project.store._db(write=True) as db:
            project.store._active_attempt(db, task["id"], attempt["id"])
            db.execute("INSERT INTO host_tickets(attempt_id,task_id,contract,request) VALUES(?,?,?,?)",
                       (attempt["id"], task["id"], encoded(contract), encoded(request or {})))
        return summary(project, ticket(project, attempt["id"]))
    except Exception as error:
        project.store.fail(task["id"], attempt["id"], str(error), retryable=True)
        raise


def context(project, attempt_id):
    item = ticket(project, attempt_id, active=True)
    project.assert_inputs_current()
    path = project.store.state_dir / "attempts" / attempt_id / "host-context.json"
    if json.loads(path.read_text(encoding="utf-8")) != item["contract"]:
        raise ConflictError("Host context artifact changed after claim")
    return {"attempt_id": attempt_id, **item["contract"]}


def _receipt(project, item, response_hash, receipt):
    with project.store._db(write=True) as db:
        previous = db.execute("SELECT response_hash FROM host_tickets WHERE attempt_id=?", (item["attempt_id"],)).fetchone()[0]
        if previous and previous != response_hash:
            raise ConflictError("A different response already owns this host attempt")
        db.execute("UPDATE host_tickets SET response_hash=?,receipt=? WHERE attempt_id=?",
                   (response_hash, encoded(receipt), item["attempt_id"]))


def _response_path(project, item, response):
    response_hash = digest(response)
    path = project.store.state_dir / "attempts" / item["attempt_id"] / ("response-" + response_hash + ".json")
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != response:
            raise ConflictError("Immutable host response artifact changed")
    else:
        save_json(path, response)
    return path


class DispatchReady(Exception):
    def __init__(self, value):
        super().__init__("Native worker context prepared")
        self.value = value


class GenerationDispatcher:
    """Rebuild generator validation closures from durable inputs at submission."""

    def __init__(self, request, session_id, item=None, response=None, expected_task_id=None):
        self.request = request
        self.session_id = session_id
        self.item = item
        self.response = response
        self.completed = False
        self.receipt = None
        self.expected_task_id = expected_task_id

    def owns(self, task):
        return bool(self.item and self.item["task_id"] == task["id"]
                    and self.item["attempt_id"] == task["attempt_id"])

    def dispatch(self, project, task, prompt, schema, agent, timeout, validate, accept, budget):
        if self.expected_task_id and task["id"] != self.expected_task_id:
            return task, {"status": "not_selected"}
        validate()
        if self.item is None:
            request = {**self.request, "scope_id": task["payload"]["generation_scope_id"]}
            _remember_generation(project, request)
            raise DispatchReady(prepare(project, task, prompt, schema, self.session_id, request, budget))
        if not self.owns(task):
            raise ConflictError("Generation prerequisites changed; this submission cannot claim another task")
        item = ticket(project, self.item["attempt_id"], active=True)
        if item["contract"]["prompt"] != prompt or item["contract"]["output_schema"] != schema:
            raise ConflictError("Generation contract changed after claim")
        directory = project.store.state_dir / "attempts" / item["attempt_id"]
        response_path = _response_path(project, item, self.response)
        result, artifacts, checks = accept(self.response, item["attempt"], directory)

        def validator(current, value, records):
            validate()
            return {"passed": True, "validator": "host_uvm_candidate_v1", "checks": checks,
                    "semantic_claim": "candidate_structure_not_compiled"}

        self.receipt = project.store.finish(task["id"], item["attempt_id"], result,
            artifacts=artifacts + [response_path, directory / "host-context.json"], validator=validator)
        _receipt(project, item, digest(self.response), self.receipt)
        self.completed = True
        return project.store.get_task(task["id"]), self.receipt


def _remember_generation(project, request):
    with project.store._db(write=True) as db:
        baseline = project.store._current(db)
        db.execute("INSERT OR IGNORE INTO host_generations VALUES(?,?,?)",
                   (digest([baseline, request]), baseline, encoded(request)))


def generate(project, arguments, *, item=None, response=None, expected_task_id=None, materialize_only=False):
    from .engine.generation import run_generation

    request = dict(arguments)
    session = request.pop("session_id", None) or "host-request-" + uuid.uuid4().hex
    # Pin the complete generation request so a later host can reconstruct validators.
    request["context_chars"] = request.get("context_chars") or project.config.get("host_context_chars", 24000)
    allowed = {"rtl_files", "top", "profile_path", "scope_id", "context_chars"}
    request = {key: value for key, value in request.items() if key in allowed and value is not None}
    if item is None:
        _remember_generation(project, request)
    dispatcher = GenerationDispatcher(request, session, item, response,
                                      expected_task_id or (item["task_id"] if item else None))
    if materialize_only:
        dispatcher.completed = True
        dispatcher.expected_task_id = "__projection_only__"
    try:
        result = run_generation(project, **request, agent="host", dispatcher=dispatcher)
    except DispatchReady as ready:
        return ready.value
    if item and not dispatcher.completed:
        raise ConflictError("The claimed UVM task is no longer dispatchable for its recorded inputs")
    if dispatcher.receipt:
        result = {"status": "accepted", "task_id": item["task_id"], "attempt_id": item["attempt_id"],
                  "receipt": dispatcher.receipt, "generation": result}
    return result


def submit(project, attempt_id, response):
    if not isinstance(response, dict) or len(encoded(response)) > 1024 * 1024:
        raise ValueError("Host response must be a JSON object of at most 1 MiB")
    item = ticket(project, attempt_id)
    response_hash = digest(response)
    # The task commit is authoritative. Reconstruct a missing transport receipt
    # after a crash, but only for the exact response artifact accepted by that commit.
    if not item["receipt"]:
        path = project.store.state_dir / "attempts" / attempt_id / ("response-" + response_hash + ".json")
        with project.store._db() as db:
            committed = db.execute("SELECT receipt FROM receipts WHERE operation_id=?", ("finish-" + attempt_id,)).fetchone()
            artifact = db.execute("SELECT sha256 FROM artifacts WHERE attempt_id=? AND path=?",
                                  (attempt_id, path.relative_to(project.root).as_posix())).fetchone()
        if committed:
            from .engine.state import file_hash
            if not artifact or not path.is_file() or file_hash(path) != artifact[0] or json.loads(path.read_text(encoding="utf-8")) != response:
                raise ConflictError("Committed attempt cannot accept a different response")
            item["receipt"] = json.loads(committed[0])
            item["response_hash"] = response_hash
            _receipt(project, item, response_hash, item["receipt"])
            project.reconcile()
    if item["receipt"]:
        if item["response_hash"] != response_hash:
            raise ConflictError("Completed attempt cannot accept a different response")
        current = project.store.get_task(item["task_id"])
        if current["status"] == "done" and current["kind"] in {"architecture", "implement"}:
            generate(project, item["request"], materialize_only=True)
        return {"status": "accepted", "task_id": item["task_id"], "attempt_id": attempt_id,
                "receipt": {**item["receipt"], "replayed": True},
                "current_task_status": project.store.get_task(item["task_id"])["status"]}
    context(project, attempt_id)
    task = project.store.get_task(item["task_id"])
    if task["kind"] in {"architecture", "implement"}:
        return generate(project, item["request"], item=item, response=response)
    validator = project._validate_model if task["kind"] == "model" else project._validate_plan
    # Validation failure leaves the lease live so the worker can correct its result.
    validator(task, response, [])
    path = _response_path(project, item, response)
    receipt = project.store.finish(task["id"], attempt_id, response,
        artifacts=[path, path.parent / "host-context.json"], validator=validator)
    _receipt(project, item, response_hash, receipt)
    project.reconcile()
    from .engine.hierarchy import synchronize
    synchronize(project)
    return {"status": "accepted", "task_id": task["id"], "attempt_id": attempt_id, "receipt": receipt}


def claim(project, arguments):
    project.assert_inputs_current()
    project.store.verify_artifacts()
    project.store.recover_expired()
    project.reconcile()
    session = arguments.get("session_id")
    if not isinstance(session, str) or not session.strip() or len(session) > 200:
        raise ValueError("A nonempty worker execution label (session_id, at most 200 characters) is required")
    selected = arguments.get("task_id")
    ready = ([project.store.get_task(selected)] if selected else project.store.ready_tasks(10000))
    ready = [t for t in ready if not arguments.get("kind") or t["kind"] == arguments["kind"]]
    for task in ready:
        if task["kind"] in {"model", "plan"}:
            prompt, schema, _ = project.task_context(task)
            return prepare(project, task, prompt, schema, session)
        if task["kind"] in {"architecture", "implement"}:
            with project.store._db() as db:
                requests = [json.loads(row[0]) for row in db.execute(
                    "SELECT request FROM host_generations WHERE baseline_id=? ORDER BY rowid DESC", (project.store.baseline()["id"],))]
            for request in requests:
                # A pinned scope is retained in the request; completed scopes are harmless.
                if request.get("scope_id") and request["scope_id"] != task["payload"].get("generation_scope_id"):
                    continue
                result = generate(project, {**request, "session_id": session,
                                            "scope_id": task["payload"].get("generation_scope_id")}, expected_task_id=task["id"])
                if result.get("status") == "awaiting_host":
                    return result
    return {"status": "no_ready_work", "next_action": "status"}
