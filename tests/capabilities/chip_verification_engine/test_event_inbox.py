"""Event delivery invariants against SQLite and the real typed decision owner."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from unittest.mock import patch

import pytest

from loopx.capabilities.chip_verification.engine import event_inbox
from loopx.capabilities.chip_verification.engine.models import ConflictError, StateError
from loopx.capabilities.chip_verification.engine.workflow import DVProject


@pytest.fixture
def project(tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text('{"module":"DMA", "requirement":"Complete an accepted transfer once."}', encoding="utf-8")
    project = DVProject(tmp_path)
    project.import_spec(spec)
    return project


def registration(project, run_id="simulation-1", **changes):
    task = project.store.list_tasks(kind="model")[0]
    return {"run_id": run_id, "task_id": task["id"], "tool": "xcelium", "source": "eda-machine",
            "input_digest": "a" * 64, **changes}


def event(project, event_id="event-1", run_id="simulation-1", sequence=1, event_type="started", **data):
    return {"id": event_id, "source": "eda-machine", "run_id": run_id,
            "baseline_id": project.store.baseline()["id"], "sequence": sequence, "type": event_type, "data": data}


def test_read_before_initialization_does_not_write(project):
    before = project.store.db_path.read_bytes()
    result = event_inbox.status(DVProject(project.root, initialize=False))
    assert result["events"] == []
    assert project.store.db_path.read_bytes() == before
    with project.store._db() as db:
        assert not event_inbox._exists(db)


def test_duplicate_registration_and_event_have_one_durable_receipt(project):
    request = registration(project)
    first = event_inbox.register(project, request)
    assert not first["execution_performed"]
    assert event_inbox.register(project, request)["duplicate"]
    with pytest.raises(ConflictError):
        event_inbox.register(project, {**request, "input_digest": "b" * 64})
    envelope = event(project)
    receipt = event_inbox.ingest(project, envelope)
    recovered = DVProject(project.root)
    duplicate = event_inbox.ingest(recovered, envelope)
    assert duplicate == {**receipt, "duplicate": True}
    assert len(event_inbox.status(recovered)["events"]) == 1
    with pytest.raises(ConflictError):
        event_inbox.ingest(project, {**envelope, "data": {"message": "different result"}})


def test_terminal_before_started_and_late_evidence_do_not_accept_task(project):
    request = registration(project)
    event_inbox.register(project, request)
    done = event(project, sequence=3, event_type="completed")
    assert event_inbox.ingest(project, done)["run_status"] == "passed"
    assert event_inbox.ingest(project, event(project, "late-start", sequence=2))["disposition"] == "terminal_ignored"
    assert event_inbox.ingest(project, event(project, "late-heartbeat", sequence=4, event_type="heartbeat"))["disposition"] == "terminal_ignored"
    refs = [{"uri": "artifact:remote/report", "sha256": "b" * 64}]
    evidence = event_inbox.ingest(project, event(project, "report", sequence=5, event_type="evidence", evidence=refs))
    assert evidence["disposition"] == "applied"
    assert not evidence["task_acceptance"]
    assert project.store.get_task(request["task_id"])["status"] == "pending"
    run = project.store.get_run(request["run_id"])
    assert run["status"] == "passed"
    assert run["artifacts"] == []
    assert run["metadata"]["observed_evidence"] == refs
    assert not run["metadata"]["coverage_verified"]
    # Evidence delivery is independent of status ordering and never rewinds it.
    delayed_refs = [{"uri": "artifact:remote/earlier-report", "sha256": "c" * 64}]
    delayed = event_inbox.ingest(project, event(project, "delayed-report", sequence=1,
                                               event_type="evidence", evidence=delayed_refs))
    assert delayed["disposition"] == "applied"
    run = project.store.get_run(request["run_id"])
    assert run["status"] == "passed"
    assert run["metadata"]["last_sequence"] == 5
    assert run["metadata"]["evidence_event_count"] == 2


def test_same_baseline_revision_change_fences_old_completion(project):
    request = registration(project)
    event_inbox.register(project, request)
    project.store.claim(request["task_id"], "new-session")
    receipt = event_inbox.ingest(project, event(project, event_type="completed"))
    assert receipt["disposition"] == "stale"
    assert project.store.get_run(request["run_id"])["status"] == "queued"
    assert project.store.get_run(request["run_id"])["validity"] == "stale"
    assert project.store.get_run(request["run_id"])["metadata"]["observed_terminal"]["type"] == "completed"
    assert receipt["physical_termination_observed"]
    assert len(event_inbox.status(project)["events"]) == 1
    observed = project.store.get_run(request["run_id"])["metadata"]["observed_terminal"]
    assert event_inbox.ingest(project, event(project, "old-late-start", sequence=2))["disposition"] == "stale"
    assert project.store.get_run(request["run_id"])["metadata"]["observed_terminal"] == observed
    assert project.store.get_task(request["task_id"])["status"] == "running"


def test_old_baseline_event_is_retained_without_advancing_new_work(project):
    request = registration(project)
    event_inbox.register(project, request)
    old_event = event(project, event_type="completed")
    spec = project.root / "spec.json"
    spec.write_text('{"module":"DMA", "requirement":"Flush queued transfers on reset."}', encoding="utf-8")
    project.import_spec(spec)
    assert event_inbox.ingest(project, old_event)["disposition"] == "stale"
    assert project.store.get_run(request["run_id"])["status"] == "queued"
    assert project.store.get_run(request["run_id"])["metadata"]["observed_terminal"]["baseline_id"] == old_event["baseline_id"]
    assert all(task["status"] == "pending" for task in project.store.list_tasks())


def test_unimported_source_drift_is_retained_as_stale(project):
    event_inbox.register(project, registration(project))
    (project.root / "spec.json").write_text('{"changed":true}', encoding="utf-8")
    assert event_inbox.ingest(project, event(project, event_type="completed"))["disposition"] == "stale"


def test_concurrent_duplicate_delivery_is_one_atomic_observation(project):
    event_inbox.register(project, registration(project))
    envelope = event(project)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: event_inbox.ingest(DVProject(project.root), envelope), range(2)))
    assert sorted(result["duplicate"] for result in results) == [False, True]
    assert len(event_inbox.status(project)["events"]) == 1
    assert project.store.get_run("simulation-1")["metadata"]["last_sequence"] == 1


def test_failure_after_run_update_rolls_back_inbox_and_observation(project):
    event_inbox.register(project, registration(project))
    with patch.object(project.store, "_event", side_effect=RuntimeError("simulated process failure before commit")):
        with pytest.raises(RuntimeError, match="simulated process failure"):
            event_inbox.ingest(project, event(project))
    assert event_inbox.status(project)["events"] == []
    assert project.store.get_run("simulation-1")["status"] == "queued"
    assert event_inbox.ingest(project, event(project))["disposition"] == "applied"


def test_unknown_source_and_malformed_event_leave_no_receipt(project):
    event_inbox.register(project, registration(project))
    for envelope in [
        {**event(project), "source": "other-host"},
        {**event(project), "sequence": True},
        event(project, event_type="failed"),
        event(project, message="x" * 70000),
    ]:
        with pytest.raises((RuntimeError, ValueError)):
            event_inbox.ingest(project, envelope)
    assert event_inbox.status(project)["events"] == []


def test_hold_blocks_new_dispatch_without_breaking_receipt_replay(project):
    request = registration(project)
    event_inbox.register(project, request)
    with project.store._db(write=True) as db:
        db.execute("CREATE TABLE watchdog_holds(task_id TEXT PRIMARY KEY,baseline_id TEXT,incident_id TEXT,active INTEGER)")
        db.execute("INSERT INTO watchdog_holds VALUES(?,?,?,1)",
                   (request["task_id"], project.store._current(db), "incident-1"))
    assert event_inbox.register(project, request)["duplicate"]
    with pytest.raises(StateError, match="Watchdog hold"):
        event_inbox.register(project, {**request, "run_id": "simulation-2"})
    assert event_inbox.ingest(project, event(project, event_type="failed", failure_signature="compile:E001"))["run_status"] == "failed"


def test_cursor_pages_and_terminal_progress_claims_are_bounded(project):
    event_inbox.register(project, registration(project))
    for index in range(1, 4):
        event_inbox.ingest(project, event(project, f"progress-{index}", sequence=index, event_type="progress", progress={"coverage": index * 30}))
    first = event_inbox.status(project, limit=2)
    assert first["has_more"]
    second = event_inbox.status(project, after=first["next_cursor"], limit=2)
    assert len(second["events"]) == 1
    assert not second["has_more"]
    assert not second["coverage_verified"]
    assert project.store.get_run("simulation-1")["status"] == "queued"
    with pytest.raises(ValueError):
        event_inbox.status(project, limit=201)


def test_status_spills_large_payloads_and_still_pages_without_loss(project):
    event_inbox.register(project, registration(project, source="adapter:" + "x" * 1000))
    for index in range(1, 18):
        envelope = event(project, f"large-{index}", sequence=index, event_type="evidence",
                         evidence=[{"uri": "artifact:" + "y" * 2000, "sha256": "b" * 64}])
        envelope["source"] = "adapter:" + "x" * 1000
        event_inbox.ingest(project, envelope)
    cursor, seen = 0, []
    while True:
        page = event_inbox.status(project, run_id="simulation-1", after=cursor, limit=200)
        assert len(json.dumps(page)) < 24000
        assert "artifact:" not in json.dumps(page)
        assert all(item["payload_reference"].startswith("chip-event:") for item in page["events"])
        seen.extend(item["cursor"] for item in page["events"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert len(seen) == len(set(seen)) == 17


def test_attempt_lease_does_not_define_external_job_lifetime(project):
    request = registration(project)
    attempt = project.store.claim(request["task_id"], "origin-session")
    request["attempt_id"] = attempt["id"]
    event_inbox.register(project, request)
    with project.store._db(write=True) as db:
        db.execute("UPDATE attempts SET lease_until=0 WHERE id=?", (attempt["id"],))
    assert event_inbox.ingest(project, event(project))["disposition"] == "applied"
    project.store.recover_expired()
    assert event_inbox.ingest(project, event(project, "late-complete", sequence=2, event_type="completed"))["disposition"] == "stale"


def test_explicit_recall_reconstructs_immutable_body_with_a_digest(project):
    event_inbox.register(project, registration(project))
    envelope = event(project, message="异步仿真诊断" * 200)
    receipt = event_inbox.ingest(project, envelope)
    offset, chunks = 0, []
    while True:
        page = event_inbox.read_event(DVProject(project.root, initialize=False), receipt["cursor"], offset, limit=512)
        chunks.append(page["text"])
        assert len(page["text"]) <= 512
        assert page["untrusted_observation"]
        offset = page["next_offset"]
        if not page["has_more"]:
            break
    body = "".join(chunks)
    assert json.loads(body) == envelope
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == page["sha256"]
    with pytest.raises(ValueError):
        event_inbox.read_event(project, receipt["cursor"], offset + 1)
    with pytest.raises(StateError):
        event_inbox.read_event(project, receipt["cursor"] + 100)


def test_legacy_run_methods_cannot_rewrite_adapter_observations(project):
    request = registration(project)
    original = event_inbox.register(project, request)["run"]
    replay = project.store.record_run(request["run_id"], request["tool"], "passed", task_id=request["task_id"],
                                      metadata={"last_sequence": 999, "coverage_verified": True})
    assert replay == original
    with pytest.raises(StateError, match="event-ingest"):
        project.store.update_run(request["run_id"], "running", metadata={"last_sequence": 999})
    assert project.store.get_run(request["run_id"])["status"] == "queued"
    assert project.store.get_run(request["run_id"])["metadata"]["last_sequence"] == 0


@pytest.mark.parametrize("early_type,data", [
    ("heartbeat", {}), ("progress", {"progress": {"transactions": 5}}),
    ("evidence", {"evidence": [{"uri": "artifact:report", "sha256": "b" * 64}]}),
])
def test_reordered_observation_does_not_suppress_terminal_lifecycle(project, early_type, data):
    event_inbox.register(project, registration(project))
    event_inbox.ingest(project, event(project, "later-observation", sequence=3, event_type=early_type, **data))
    completed = event_inbox.ingest(project, event(project, "completed", sequence=2, event_type="completed"))
    assert completed["run_status"] == "passed"
    assert completed["disposition"] == "applied"
    run = project.store.get_run("simulation-1")
    assert run["metadata"]["last_sequence"] == 3
    assert run["metadata"]["lifecycle_sequence"] == 2
    assert run["metadata"]["observed_terminal"]["type"] == "completed"
    assert event_inbox.ingest(project, event(project, "delayed-start", sequence=1))["disposition"] == "terminal_ignored"
    assert event_inbox.ingest(project, event(project, "impossible-restart", sequence=4))["disposition"] == "terminal_ignored"
    assert project.store.get_run("simulation-1")["status"] == "passed"


def test_wrong_baseline_label_cannot_report_physical_termination(project):
    event_inbox.register(project, registration(project))
    envelope = event(project, event_type="completed")
    envelope["baseline_id"] = "not-the-registered-baseline"
    receipt = event_inbox.ingest(project, envelope)
    assert receipt["disposition"] == "stale"
    assert not receipt["physical_termination_observed"]
    run = project.store.get_run("simulation-1")
    assert run["status"] == "queued"
    assert run["validity"] == "current"
    assert "observed_terminal" not in run["metadata"]
