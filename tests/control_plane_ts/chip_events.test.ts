import assert from "node:assert/strict";
import test from "node:test";
import {admitChipRun, reduceChipEvent} from "../../loopx/control_plane/capabilities/chip_events.ts";

const registration = {run_id: "sim-1", task_id: "rtl-1", tool: "xcelium", source: "eda-host", input_digest: "a".repeat(64)};
const request = () => ({
  event: {id: "event-1", source: "eda-host", run_id: "sim-1", baseline_id: "baseline-1", sequence: 1, type: "started", data: {}},
  run: {id: "sim-1", source: "eda-host", baseline_id: "baseline-1", status: "queued", validity: "current",
    task_revision: 3, attempt_id: null, last_sequence: 0, lifecycle_sequence: 0},
  current: {baseline_id: "baseline-1", task_revision: 3, attempt_id: null, inputs_current: true},
});

test("registration has bounded time budgets without coercion or unrecognized authority", () => {
  assert.equal(admitChipRun(registration).heartbeat_seconds, 300);
  for (const heartbeat_seconds of [0, -1, null, true, "5", NaN, 604801]) {
    assert.throws(() => admitChipRun({...registration, heartbeat_seconds}), /heartbeat_seconds/);
  }
  assert.throws(() => admitChipRun({...registration, input_digest: "bad"}), /SHA-256/);
  assert.throws(() => admitChipRun({...registration, shell: "execute-this"}), /unsupported field/);
});

test("terminal event settles a queued run but cannot accept work or coverage", () => {
  const value = request();
  value.event.type = "completed";
  const result = reduceChipEvent(value);
  assert.equal(result.next_status, "passed");
  assert.equal(result.disposition, "applied");
  assert.equal(result.task_acceptance, false);
  assert.equal(result.coverage_verified, false);
});

test("stale baseline, revision, source bytes and attempt cannot change observations", () => {
  const value = request();
  for (const current of [
    {...value.current, baseline_id: "baseline-2"}, {...value.current, task_revision: 4},
    {...value.current, inputs_current: false},
  ]) assert.equal(reduceChipEvent({...value, current}).disposition, "stale");
  assert.equal(reduceChipEvent({...value, run: {...value.run, attempt_id: "expired"},
    current: {...value.current, attempt_id: "replacement"}}).disposition, "stale");
  assert.equal(reduceChipEvent({...value, event: {...value.event, baseline_id: "incorrect"}}).disposition, "stale");
  assert.throws(() => reduceChipEvent({...value, event: {...value.event, source: "unregistered"}}), /not registered/);
});

test("reordering never regresses terminal state and evidence may arrive after completion", () => {
  const value = request();
  const run = {...value.run, status: "passed", last_sequence: 5, lifecycle_sequence: 5};
  assert.equal(reduceChipEvent({...value, run}).disposition, "terminal_ignored");
  const event = {...value.event, sequence: 6};
  assert.equal(reduceChipEvent({...value, run, event}).disposition, "terminal_ignored");
  const evidence = reduceChipEvent({...value, run, event: {...event, type: "evidence", data: {
    evidence: [{uri: "artifact:run-1/log", sha256: "b".repeat(64)}],
  }}});
  assert.equal(evidence.disposition, "applied");
  assert.equal(evidence.next_status, "passed");
  assert.equal(evidence.heartbeat_observed, false);
  const delayed = reduceChipEvent({...value, run, event: {...event, sequence: 2, type: "evidence"}});
  assert.equal(delayed.disposition, "applied");
  assert.equal(delayed.last_sequence, 5);
  assert.equal(delayed.heartbeat_observed, false);
});

test("observation sequence cannot suppress an earlier terminal lifecycle message", () => {
  const value = request();
  const run = {...value.run, last_sequence: 3, lifecycle_sequence: 0};
  const result = reduceChipEvent({...value, run, event: {...value.event, sequence: 2, type: "completed"}});
  assert.equal(result.disposition, "applied");
  assert.equal(result.next_status, "passed");
  assert.equal(result.last_sequence, 3);
  assert.equal(result.lifecycle_sequence, 2);
  assert.equal(result.physical_terminal_observed, true);
});

test("superseded task terminal is physically observed without current state adoption", () => {
  const value = request();
  const event = {...value.event, type: "completed", sequence: 2};
  const result = reduceChipEvent({...value, event, current: {...value.current, task_revision: 4}});
  assert.equal(result.disposition, "stale");
  assert.equal(result.next_status, "queued");
  assert.equal(result.physical_terminal_observed, true);
  const mismatch = reduceChipEvent({...value, event: {...event, baseline_id: "unregistered-baseline"}});
  assert.equal(mismatch.physical_terminal_observed, false);
});

test("malformed payload and oversized diagnostics cannot enter bounded event context", () => {
  const value = request();
  for (const event of [
    {...value.event, sequence: true}, {...value.event, type: "approve-coverage"},
    {...value.event, data: null},
    {...value.event, data: {coverage_verified: true}},
    {...value.event, type: "failed", data: {}},
    {...value.event, data: {message: "x".repeat(2049)}},
    {...value.event, data: {evidence: [{uri: "x", sha256: "invalid"}]}},
    {...value.event, data: {progress: {coverage: Infinity}}},
  ]) assert.throws(() => reduceChipEvent({...value, event}));
});
