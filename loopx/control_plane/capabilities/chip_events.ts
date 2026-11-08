/** Chip-local observed job protocol. No event grants task acceptance or coverage. */
import {EffectRuntimeRequestError} from "../effect_runtime_errors.ts";

type ObjectValue = Record<string, unknown>;
const fail = (message: string): never => { throw new EffectRuntimeRequestError(message); };
const object = (value: unknown, label: string): ObjectValue => {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(`${label} must be an object`);
  return value as ObjectValue;
};
const keys = (value: ObjectValue, allowed: string[], label: string) => {
  if (Object.keys(value).some(key => !allowed.includes(key))) fail(`${label} contains an unsupported field`);
};
const text = (value: unknown, label: string, maximum = 256): string => {
  if (typeof value !== "string" || !value.trim() || value.includes("\0") || value.length > maximum) {
    fail(`${label} must be nonempty text without NUL, at most ${maximum} characters`);
  }
  return value as string;
};
const integer = (value: unknown, label: string, minimum: number, maximum: number): number => {
  if (!Number.isSafeInteger(value) || (value as number) < minimum || (value as number) > maximum) {
    fail(`${label} must be a safe integer in ${minimum}..${maximum}`);
  }
  return value as number;
};
const digest = (value: unknown, label: string): string => {
  if (typeof value !== "string" || !/^[a-f0-9]{64}$/.test(value)) fail(`${label} must be lowercase SHA-256`);
  return value as string;
};

export function admitChipRun(input: unknown) {
  const request = object(input, "registration");
  keys(request, ["run_id", "task_id", "tool", "source", "input_digest", "attempt_id",
    "heartbeat_seconds", "timeout_seconds", "queue_timeout_seconds"], "registration");
  return {
    run_id: text(request.run_id, "run_id"), task_id: text(request.task_id, "task_id"),
    tool: text(request.tool, "tool"), source: text(request.source, "source", 1024),
    input_digest: digest(request.input_digest, "input_digest"),
    attempt_id: request.attempt_id === undefined || request.attempt_id === null
      ? null : text(request.attempt_id, "attempt_id"),
    heartbeat_seconds: integer(request.heartbeat_seconds === undefined ? 300 : request.heartbeat_seconds, "heartbeat_seconds", 1, 604800),
    timeout_seconds: integer(request.timeout_seconds === undefined ? 86400 : request.timeout_seconds, "timeout_seconds", 1, 2592000),
    queue_timeout_seconds: integer(request.queue_timeout_seconds === undefined ? 3600 : request.queue_timeout_seconds, "queue_timeout_seconds", 1, 2592000),
  };
}

export const CHIP_EVENT_TYPES = ["started", "heartbeat", "completed", "failed", "cancelled", "progress", "evidence"] as const;
type EventType = (typeof CHIP_EVENT_TYPES)[number];
const terminalStatuses = new Set(["passed", "failed", "error", "timeout", "cancelled"]);

function validateEvent(input: unknown) {
  const event = object(input, "event");
  keys(event, ["id", "source", "run_id", "baseline_id", "sequence", "type", "data"], "event");
  const type = text(event.type, "type") as EventType;
  if (!(CHIP_EVENT_TYPES as readonly string[]).includes(type)) fail("Unsupported chip event type");
  const data = object(event.data === undefined ? {} : event.data, "data");
  keys(data, ["message", "failure_signature", "exit_code", "evidence", "progress"], "data");
  if (data.message !== undefined) text(data.message, "message", 2048);
  if (type === "failed" || data.failure_signature !== undefined) text(data.failure_signature, "failure_signature");
  if (data.exit_code !== undefined) integer(data.exit_code, "exit_code", -2147483648, 2147483647);
  if (data.evidence !== undefined) {
    if (!Array.isArray(data.evidence) || data.evidence.length > 32) fail("evidence must contain at most 32 references");
    for (const value of data.evidence as unknown[]) {
      const ref = object(value, "evidence reference");
      keys(ref, ["uri", "sha256", "kind"], "evidence reference");
      text(ref.uri, "evidence uri", 2048);
      digest(ref.sha256, "evidence sha256");
      if (ref.kind !== undefined) text(ref.kind, "evidence kind", 64);
    }
  }
  if (data.progress !== undefined) {
    const progress = object(data.progress, "progress");
    if (Object.keys(progress).length > 16) fail("progress must contain at most 16 observations");
    for (const [key, value] of Object.entries(progress)) {
      text(key, "progress key", 64);
      if (typeof value === "number") {
        if (!Number.isFinite(value)) fail("progress numbers must be finite");
      } else text(value, "progress value", 128);
    }
  }
  if (JSON.stringify(event).length > 32768) fail("event exceeds 32768 characters");
  return {
    id: text(event.id, "id"), source: text(event.source, "source", 1024),
    run_id: text(event.run_id, "run_id"), baseline_id: text(event.baseline_id, "baseline_id"),
    sequence: integer(event.sequence, "sequence", 1, Number.MAX_SAFE_INTEGER), type, data,
  };
}

/** The provider supplies current durable facts under its write transaction. */
export function reduceChipEvent(input: unknown) {
  const request = object(input, "request");
  const event = validateEvent(request.event);
  const run = object(request.run, "run");
  const current = object(request.current, "current");
  if (event.run_id !== run.id || event.source !== run.source) fail("Event source or run identity is not registered");
  const lastSequence = integer(run.last_sequence, "last_sequence", 0, Number.MAX_SAFE_INTEGER);
  const lifecycleSequence = integer(run.lifecycle_sequence ?? 0, "lifecycle_sequence", 0, Number.MAX_SAFE_INTEGER);
  const lifecycleEvent = ["started", "completed", "failed", "cancelled"].includes(event.type);
  const terminalEvent = ["completed", "failed", "cancelled"].includes(event.type);
  const status = text(run.status, "run status");
  if (!["queued", "building", "running", ...terminalStatuses].includes(status)) fail("Unsupported run status");
  const stale = current.inputs_current === false || event.baseline_id !== run.baseline_id || run.baseline_id !== current.baseline_id
    || run.validity !== "current" || run.task_revision !== current.task_revision
    || (run.attempt_id !== null && run.attempt_id !== current.attempt_id);
  let disposition = "applied";
  let nextStatus = status;
  if (stale) disposition = "stale";
  else if (terminalStatuses.has(status) && event.type !== "evidence") disposition = "terminal_ignored";
  else if (lifecycleEvent ? event.sequence <= lifecycleSequence
    : event.sequence <= lastSequence && event.type !== "evidence") disposition = "out_of_order";
  else if (event.type === "started") nextStatus = "running";
  else if (event.type === "completed") nextStatus = "passed";
  else if (event.type === "failed") nextStatus = "failed";
  else if (event.type === "cancelled") nextStatus = "cancelled";
  return {
    event, disposition, next_status: nextStatus,
    last_sequence: disposition === "applied" ? Math.max(event.sequence, lastSequence) : lastSequence,
    lifecycle_sequence: disposition === "applied" && lifecycleEvent ? event.sequence : lifecycleSequence,
    // A delayed liveness or evidence message cannot suppress a lifecycle fact.
    heartbeat_observed: disposition === "applied" && !terminalStatuses.has(status)
      && (lifecycleEvent || event.sequence > lastSequence),
    // This acknowledges physical termination only. A stale task or baseline
    // still cannot receive a current business-state transition or acceptance.
    physical_terminal_observed: terminalEvent && event.baseline_id === run.baseline_id
      && (event.sequence > lifecycleSequence || terminalStatuses.has(status)),
    terminal: terminalStatuses.has(nextStatus), task_acceptance: false, coverage_verified: false,
  };
}
