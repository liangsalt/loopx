/** Chip-local progress supervision. Observations never confer DV acceptance. */
import {EffectRuntimeRequestError} from "../effect_runtime_errors.ts";

type ObjectValue = Record<string, any>;
const object = (value: unknown, name: string): ObjectValue => {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new EffectRuntimeRequestError(`${name} must be an object`);
  }
  return value as ObjectValue;
};
const finite = (value: unknown, name: string): number => {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new EffectRuntimeRequestError(`${name} must be finite and nonnegative`);
  }
  return value;
};

export const CHIP_WATCHDOG_POLICY = {
  failure_limit: 3,
  stall_seconds: 1800,
  review_interval_seconds: 900,
  semantic_review_seconds: 3600,
  max_resumes: 2,
  max_reviews_per_tick: 8,
  max_tasks_per_tick: 100,
} as const;

const policyBounds: Record<string, [number, number]> = {
  failure_limit: [2, 20], stall_seconds: [60, 604800],
  review_interval_seconds: [30, 86400], max_resumes: [0, 10],
  semantic_review_seconds: [60, 604800],
  max_reviews_per_tick: [1, 50], max_tasks_per_tick: [1, 500],
};

export function validateChipWatchdogPolicy(input: unknown) {
  const request = object(input, "request");
  const supplied = object(request.policy ?? {}, "policy");
  for (const key of Object.keys(supplied)) {
    if (!(key in policyBounds)) throw new EffectRuntimeRequestError(`unknown watchdog policy field: ${key}`);
  }
  const policy: Record<string, number> = {...CHIP_WATCHDOG_POLICY, ...supplied};
  for (const [key, [minimum, maximum]] of Object.entries(policyBounds)) {
    const value = policy[key];
    if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
      throw new EffectRuntimeRequestError(`${key} must be an integer from ${minimum} to ${maximum}`);
    }
  }
  return {policy, schema_version: "chip_watchdog_policy_v1"};
}

export function evaluateChipWatchdog(input: unknown) {
  const request = object(input, "request");
  const now = finite(request.now, "now");
  const {policy} = validateChipWatchdogPolicy({policy: request.policy});
  if (!Array.isArray(request.observations) || request.observations.length > policy.max_tasks_per_tick) {
    throw new EffectRuntimeRequestError("observations must be a bounded array");
  }
  const decisions = [];
  for (const raw of request.observations) {
    const observation = object(raw, "observation");
    const target = object(observation.target, "target");
    if (observation.has_open_incident || ["cancelled", "stale"].includes(target.status)) continue;
    let reason: string | null = null;
    let hold = false;
    if (observation.consecutive_failures >= policy.failure_limit) {
      reason = "repeated_failure";
      hold = true;
    } else if (target.status === "running" && observation.lease_until !== null && observation.lease_until <= now) {
      reason = "worker_lease_expired";
      hold = true;
    } else {
      for (const run of observation.active_runs ?? []) {
        if (run.status === "queued" && now - run.created >= run.queue_timeout_seconds) {
          reason = "run_queue_timeout";
        } else if (run.status !== "queued" && now - run.started_at >= run.timeout_seconds) {
          reason = "run_timeout";
        } else if (run.status !== "queued" && now - run.last_heartbeat >= run.heartbeat_seconds) {
          reason = "run_heartbeat_missed";
        }
        if (reason) { hold = true; break; }
      }
      if (!reason && target.status !== "done" && observation.started_at !== null &&
          now - observation.last_engineering_progress >= policy.stall_seconds &&
          now - observation.last_review_at >= policy.review_interval_seconds) {
        reason = "progress_review_due";
      }
      if (!reason && target.status === "running" && observation.started_at !== null &&
          now - Math.max(observation.started_at, observation.last_review_at) >= policy.semantic_review_seconds) {
        reason = "periodic_review_due";
      }
    }
    if (reason) decisions.push({task_id: target.id, reason, hold,
      observation_only: true, execution_performed: false, signoff: false});
    if (decisions.length >= policy.max_reviews_per_tick) break;
  }
  return {schema_version: "chip_watchdog_evaluation_v1", decisions};
}

export function decideChipWatchdogApply(input: unknown) {
  const request = object(input, "request");
  const snapshot = object(request.snapshot, "snapshot");
  const current = object(request.current, "current");
  const review = object(request.review, "review");
  const {policy} = validateChipWatchdogPolicy({policy: request.policy});
  if (!["resume", "wait"].includes(review.action)) throw new EffectRuntimeRequestError("unknown watchdog review action");
  if (request.independent !== true) throw new EffectRuntimeRequestError("watchdog review must use an independent session label");
  const expected = object(snapshot.target, "snapshot.target");
  const actual = object(current.target, "current.target");
  const same = expected.id === actual.id && expected.baseline_id === actual.baseline_id &&
    expected.revision === actual.revision && expected.attempt_id === actual.attempt_id &&
    snapshot.progress_fingerprint === current.progress_fingerprint &&
    snapshot.observation_fingerprint === current.observation_fingerprint;
  if (!same || current.baseline_id !== expected.baseline_id || (actual.status === "done" && expected.status !== "done")) {
    const trustedAdvance = current.baseline_id !== expected.baseline_id ||
      ["cancelled", "stale"].includes(actual.status) ||
      (actual.status === "done" && expected.status !== "done") ||
      snapshot.progress_fingerprint !== current.progress_fingerprint;
    return {disposition: "superseded", reason: "target or evidence changed after review snapshot",
      release_hold: trustedAdvance, refresh_review: !trustedAdvance};
  }
  if (review.action === "wait") return {disposition: "waiting", reason: "review requests a bounded recheck", release_hold: false};
  if ((current.active_run_count ?? (current.active_runs ?? []).length) > 0 || (actual.status === "running" && current.lease_until > request.now)) {
    return {disposition: "waiting", reason: "an execution still owns the work; inspect its outcome before retrying", release_hold: false};
  }
  if (!["pending", "failed", "blocked", "running", "done"].includes(actual.status)) {
    return {disposition: "superseded", reason: "current task state is not eligible for recovery", release_hold: true};
  }
  if (request.resume_count >= policy.max_resumes) {
    return {disposition: "budget_exhausted", reason: "repair budget exhausted without new accepted engineering evidence", release_hold: false};
  }
  if (request.strategy_repeated === true) {
    return {disposition: "waiting", reason: "resume requires a strategy different from previous failed repairs", release_hold: false};
  }
  return {disposition: "resumed", reason: "accepted independent review authorizes one bounded recovery",
    release_hold: true, recover_attempt: actual.status === "running",
    requeue: ["failed", "blocked", "running"].includes(actual.status)};
}
