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
HOST_INSTRUCTION = "Use only the supplied bounded task material. Restrictions on tools inside the task prompt do not prohibit LoopX MCP heartbeat/submit/abandon. Submit through dv_submit and retain the receipt. One runtime may process tasks in successive sessions; an isolated child session is optional. Do not accumulate completed task contexts in a long conversation."


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def _domain(task):
    """Select the chip-domain validator, without changing session ownership."""
    if task["kind"] == "watchdog-review":
        from .engine import watchdog
        return watchdog
    if task["kind"] in {"spec-contract", "contract-plan"}:
        from .engine import engineering
        return engineering
    if task["kind"] in {"microarchitecture", "rtl"}:
        from .engine import design
        return design
    return None


def _feedback(project, task, prompt):
    items = [item["description"] for item in project.store.list_issues()
             if item["task_id"] == task["id"] and item["category"] == "candidate_revision" and item["status"] == "open"]
    prompt += "\nRevision feedback (preserve the source contract):\n" + items[-1] if items else ""
    if task["kind"] != "watchdog-review":
        with project.store._db() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchdog_incidents'").fetchone():
                review = db.execute(
                    "SELECT t.id,t.result FROM watchdog_incidents i JOIN tasks t ON t.id=i.review_task_id "
                    "WHERE i.task_id=? AND i.baseline_id=? AND i.status='resumed' AND t.status='done' "
                    "ORDER BY i.updated DESC LIMIT 1", (task["id"], task["baseline_id"])).fetchone()
                if review:
                    result = json.loads(review["result"])
                    prompt += ("\nAccepted independent recovery review " + review["id"] +
                               " (preserve the source contract and evidence gates):\n" +
                               encoded({key: result[key] for key in ("diagnosis", "strategy")}))
    return prompt


def revise(project, task_id, reason):
    """Repair accepted candidate bytes without editing history or changing the spec."""
    task = project.store.get_task(task_id)
    if task["kind"] not in {"rtl", "implement"} or task["status"] not in {"done", "stale"}:
        raise ValidationError("Revise requires an accepted RTL or UVM component candidate; correct a live attempt with submit")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise ValidationError("Revision feedback must contain 1..4000 characters")
    project.assert_inputs_current()
    if task["baseline_id"] != project.store.baseline()["id"]:
        raise ConflictError("Revision cannot revive a candidate from an old specification baseline")
    revision = "Candidate revision " + uuid.uuid4().hex
    # Persist the correction before invalidating: recovery must never requeue
    # an invalidated candidate without the diagnostic that caused the revision.
    project.store.add_issue("candidate_revision", revision + ": " + reason, task_id=task_id)
    affected = project.store.invalidate(task_id, revision)
    project.store.requeue(task_id)
    return {"status": "pending", "task_id": task_id, "invalidated_tasks": affected,
            "next_action": "claim", "specification_changed": False}


def _resume_revisions(project):
    with project.store._db() as db:
        held = ({row[0] for row in db.execute("SELECT task_id FROM watchdog_holds WHERE active=1 AND baseline_id=?",
                                            (project.store._current(db),))}
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchdog_holds'").fetchone()
                else set())
    for task in project.store.list_tasks(status="stale"):
        if (task["id"] not in held and task["kind"] in {"rtl", "implement"} and (task["reason"] or "").startswith("Candidate revision ")
                and all(project.store.get_task(key)["status"] == "done" for key in task["depends_on"])):
            project.store.requeue(task["id"])


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
            "instruction": "Read this attempt's bounded context in the active runtime session. Use a fresh session or optional child session when context pressure grows; durable state survives either choice."}


def prepare(project, task, prompt, schema, session_id, request=None, budget=None):
    if task["kind"] == "watchdog-review":
        from .engine.watchdog import assert_review_session
        assert_review_session(project, task, session_id)
    budget = budget or project.config.get("host_context_chars", project.config["context_chars"])
    contract = {"task_id": task["id"], "kind": task["kind"], "prompt": prompt,
                "host_instruction": HOST_INSTRUCTION, "output_schema": schema, "context_budget_chars": budget}
    # Count the actual serialized payload delivered to the worker, including schema.
    required = len(encoded(contract)) + 300
    if required > budget:
        if task["status"] in {"pending", "failed"}:
            project.store.block(task["id"], f"Complete host context needs {required} characters; budget is {budget}")
            project.store.add_issue("context_blocker", "Complete required task context exceeds the host budget; split the scope or increase its budget.", task_id=task["id"])
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
        prompt = _feedback(project, task, prompt)
        if self.item is None:
            request = {**self.request, "scope_id": task["payload"]["generation_scope_id"]}
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
        _resume_revisions(project)
        self.completed = True
        return project.store.get_task(task["id"]), self.receipt


def _remember_generation(project, request):
    with project.store._db(write=True) as db:
        baseline = project.store._current(db)
        # Refresh request order even when an explicitly selected budget was used
        # before. Claims replay the most recently requested matching contract.
        db.execute("INSERT OR REPLACE INTO host_generations VALUES(?,?,?)",
                   (digest([baseline, request]), baseline, encoded(request)))


def generate(project, arguments, *, item=None, response=None, expected_task_id=None, materialize_only=False, replay=False):
    from .engine.generation import run_generation

    request = dict(arguments)
    session = request.pop("session_id", None) or "host-request-" + uuid.uuid4().hex
    # Pin the complete generation request so a later host can reconstruct validators.
    request["context_chars"] = request.get("context_chars") or project.config.get("host_context_chars", 24000)
    allowed = {"rtl_files", "top", "profile_path", "scope_id", "context_chars", "contract_task_id"}
    request = {key: value for key, value in request.items() if key in allowed and value is not None}
    if item is None and not replay:
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
        elif current["status"] == "done" and _domain(current):
            project.reconcile()
            from .engine import design
            design.reconcile(project)
        return {"status": "accepted", "task_id": item["task_id"], "attempt_id": attempt_id,
                "receipt": {**item["receipt"], "replayed": True},
                "current_task_status": project.store.get_task(item["task_id"])["status"]}
    context(project, attempt_id)
    task = project.store.get_task(item["task_id"])
    if task["kind"] in {"architecture", "implement"}:
        return generate(project, item["request"], item=item, response=response)
    domain = _domain(task)
    if domain:
        prompt, schema, _ = domain.task_context(project, task)
        prompt = _feedback(project, task, prompt)
        if item["contract"]["prompt"] != prompt or item["contract"]["output_schema"] != schema:
            raise ConflictError("Shared engineering contract changed after claim")
        domain.validate(project, task, response, [])
        path = _response_path(project, item, response)
        result, artifacts = response, []
        if hasattr(domain, "accept"):
            active = ticket(project, attempt_id, active=True)
            result, artifacts = domain.accept(project, task, response, active["attempt"], path.parent)
        receipt = project.store.finish(task["id"], attempt_id, result,
            artifacts=[*artifacts, path, path.parent / "host-context.json"],
            validator=lambda current, value, records: domain.validate(project, current, value, records))
        _receipt(project, item, response_hash, receipt)
        project.reconcile()
        _resume_revisions(project)
        from .engine import design
        design.reconcile(project)
        return {"status": "accepted", "task_id": task["id"], "attempt_id": attempt_id,
                "receipt": receipt, "claim": "candidate accepted; tool checks and simulation remain separate"}
    validator = project._validate_model if task["kind"] == "model" else project._validate_plan
    # Validation failure leaves the lease live so the worker can correct its result.
    validator(task, response, [])
    path = _response_path(project, item, response)
    receipt = project.store.finish(task["id"], attempt_id, response,
        artifacts=[path, path.parent / "host-context.json"], validator=validator)
    _receipt(project, item, response_hash, receipt)
    project.reconcile()
    _resume_revisions(project)
    from .engine.hierarchy import synchronize
    synchronize(project)
    return {"status": "accepted", "task_id": task["id"], "attempt_id": attempt_id, "receipt": receipt}


def claim(project, arguments):
    project.assert_inputs_current()
    project.store.verify_artifacts()
    if arguments.get("kind") != "watchdog-review":
        project.store.recover_expired()
    project.reconcile()
    _resume_revisions(project)
    if project.config.get("engineering_enabled"):
        from .engine import design
        design.reconcile(project)
    session = arguments.get("session_id")
    if not isinstance(session, str) or not session.strip() or len(session) > 200:
        raise ValueError("A nonempty worker execution label (session_id, at most 200 characters) is required")
    selected = arguments.get("task_id")
    ready = ([project.store.get_task(selected)] if selected else project.store.ready_tasks(10000))
    ready = [t for t in ready if not arguments.get("kind") or t["kind"] == arguments["kind"]]
    # A review is explicitly claimed in a separate host session, never silently
    # assigned to the worker whose recent actions it must assess.
    if arguments.get("kind") != "watchdog-review":
        ready = [t for t in ready if t["kind"] != "watchdog-review"]
    if project.config.get("engineering_enabled"):
        ready = [task for task in ready if task["kind"] != "plan"]
    for task in ready:
        domain = _domain(task)
        if domain:
            prompt, schema, _ = domain.task_context(project, task)
            prompt = _feedback(project, task, prompt)
            return prepare(project, task, prompt, schema, session)
        if task["kind"] in {"model", "plan"}:
            prompt, schema, _ = project.task_context(task)
            prompt = _feedback(project, task, prompt)
            return prepare(project, task, prompt, schema, session)
        if task["kind"] in {"architecture", "implement"}:
            basis = task["payload"].get("basis")
            if basis is None:
                ancestors = [project.store.get_task(key) for key in task["depends_on"]]
                basis = next((entry["payload"]["basis"] for entry in ancestors if entry["kind"] == "architecture"), {})
            with project.store._db() as db:
                requests = [json.loads(row[0]) for row in db.execute(
                    "SELECT request FROM host_generations WHERE baseline_id=? ORDER BY rowid DESC", (project.store.baseline()["id"],))]
            for request in requests:
                # A pinned scope is retained in the request; completed scopes are harmless.
                if request.get("scope_id") and request["scope_id"] != task["payload"].get("generation_scope_id"):
                    continue
                shared_id = task["payload"].get("contract_task_id") or basis.get("shared_specification", {}).get("task_id")
                if request.get("contract_task_id") != shared_id:
                    continue
                if (request.get("top") != basis.get("top")
                        or request.get("profile_path") != (basis.get("verification_profile") or {}).get("path")
                        or set(request.get("rtl_files", [])) != {entry["path"] for entry in basis.get("rtl", [])}):
                    continue
                result = generate(project, {**request, "session_id": session,
                                            "scope_id": task["payload"].get("generation_scope_id")}, expected_task_id=task["id"], replay=True)
                if result.get("status") == "awaiting_host":
                    return result
                # The newest exact request owns this scope's execution budget.
                # A durable blocker must not fall through to an older budget.
                break
    return {"status": "no_ready_work", "next_action": "status"}
