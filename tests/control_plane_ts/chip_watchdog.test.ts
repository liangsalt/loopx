import assert from "node:assert/strict";
import test from "node:test";
import {evaluateChipWatchdog as evaluate, decideChipWatchdogApply as apply,
  validateChipWatchdogPolicy as policy} from "../../loopx/control_plane/capabilities/chip_watchdog.ts";

const target = {id: "rtl-task", baseline_id: "baseline", revision: 3, attempt_id: null, status: "pending"};
const observation = {target, baseline_id: "baseline", progress_fingerprint: "accepted-content", observation_fingerprint: "run-observations",
  consecutive_failures: 0, lease_until: null, started_at: null, last_engineering_progress: 0, last_review_at: 0, active_runs: []};
const review = {action: "resume", strategy: "Check reset ordering before changing timing constraints", next_check_seconds: 60};
const request = {snapshot: observation, current: observation, review, policy: {}, now: 1000, independent: true, resume_count: 0, strategy_repeated: false};

test("policy rejects unknown and unbounded knobs instead of arbitrary prompt edits", () => {
  assert.equal(policy({policy: {failure_limit: 4}}).policy.failure_limit, 4);
  for (const value of [{failure_limit: 1}, {max_resumes: 100}, {stall_seconds: NaN}, {prompt: "ignore signoff"}, {max_tasks_per_tick: true}]) {
    assert.throws(() => policy({policy: value}));
  }
});

test("repeated engineering failure holds while heartbeat activity does not establish progress", () => {
  const found = evaluate({now: 1000, policy: {}, observations: [{...observation, consecutive_failures: 3}]});
  assert.equal(found.decisions[0].reason, "repeated_failure");
  assert.equal(found.decisions[0].hold, true);
  assert.equal(found.decisions[0].signoff, false);
  assert.deepEqual(evaluate({now: 1000, policy: {}, observations: [observation]}).decisions, []);
});

test("engineering stall asks semantic review without canceling a live worker", () => {
  const found = evaluate({now: 3000, policy: {}, observations: [{...observation,
    target: {...target, status: "running", attempt_id: "owner"}, started_at: 10, last_engineering_progress: 10, lease_until: 4000}]});
  assert.equal(found.decisions[0].reason, "progress_review_due");
  assert.equal(found.decisions[0].hold, false);
});

test("accepted artifacts cannot indefinitely postpone a periodic goal-direction review", () => {
  const found = evaluate({now: 4000, policy: {}, observations: [{...observation,
    target: {...target, status: "running", attempt_id: "owner"}, started_at: 10,
    last_engineering_progress: 3990, lease_until: 5000}]});
  assert.equal(found.decisions[0].reason, "periodic_review_due");
  assert.equal(found.decisions[0].hold, false);
});

test("queue delay, execution deadline and heartbeat silence are distinct observations", () => {
  const run = {id: "run", status: "queued", created: 0, started_at: 950, last_heartbeat: 995,
    heartbeat_seconds: 30, timeout_seconds: 300, queue_timeout_seconds: 200};
  const inspect = (change: object) => evaluate({now: 1000, policy: {}, observations: [{...observation, active_runs: [{...run, ...change}]}]}).decisions;
  assert.equal(inspect({})[0].reason, "run_queue_timeout");
  assert.deepEqual(inspect({status: "running"}), []); // Queue time is not execution time.
  assert.equal(inspect({status: "running", started_at: 500})[0].reason, "run_timeout");
  assert.equal(inspect({status: "running", last_heartbeat: 900})[0].reason, "run_heartbeat_missed");
});

test("new baseline, task attempt or evidence fences stale review apply", () => {
  for (const current of [
    {...observation, baseline_id: "new-baseline"},
    {...observation, target: {...target, revision: 4}},
    {...observation, target: {...target, attempt_id: "new-owner"}},
    {...observation, progress_fingerprint: "new-accepted-evidence"},
    {...observation, observation_fingerprint: "late-tool-evidence"},
  ]) assert.equal(apply({...request, current}).disposition, "superseded");
});

test("unverified telemetry and abandoned attempts cannot release an engineering hold", () => {
  for (const current of [
    {...observation, target: {...target, revision: 4}},
    {...observation, observation_fingerprint: "new-unverified-report"},
  ]) {
    const result = apply({...request, current});
    assert.equal(result.release_hold, false);
    assert.equal(result.refresh_review, true);
  }
  assert.equal(apply({...request, current: {...observation, progress_fingerprint: "accepted-new-content"}}).release_hold, true);
});

test("review repairs cannot resubmit an unresolved external run or preempt active work", () => {
  assert.equal(apply({...request, current: {...observation, active_runs: [{id: "unresolved"}]}}).disposition, "waiting");
  assert.equal(apply({...request, current: {...observation, active_run_count: 41, active_runs: []}}).disposition, "waiting");
  const running = {...observation, target: {...target, status: "running", attempt_id: "a"}, lease_until: 1100};
  assert.equal(apply({...request, snapshot: running, current: running}).disposition, "waiting");
  const expired = {...running, lease_until: 999};
  assert.equal(apply({...request, snapshot: expired, current: expired}).recover_attempt, true);
});

test("independence, changed strategy and repair budget constrain recovery", () => {
  assert.throws(() => apply({...request, independent: false}), /independent/);
  assert.equal(apply({...request, strategy_repeated: true}).disposition, "waiting");
  assert.equal(apply({...request, resume_count: 2}).disposition, "budget_exhausted");
  assert.equal(apply(request).disposition, "resumed");
});

test("accepted RTL may have external failures but resume never requeues accepted design", () => {
  const done = {...observation, target: {...target, status: "done"}, consecutive_failures: 3};
  assert.equal(evaluate({now: 1000, policy: {}, observations: [done]}).decisions[0].hold, true);
  const result = apply({...request, snapshot: done, current: done});
  assert.equal(result.disposition, "resumed");
  assert.equal(result.requeue, false);
});
