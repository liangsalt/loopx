/** Capability-local request admission; never a second Goal or scheduling owner. */
import {EffectRuntimeRequestError} from "../effect_runtime_errors.ts";

type JsonObject = Record<string, unknown>;
export const CHIP_VERIFICATION_ACTIONS = [
  "doctor", "import", "status", "design-map", "next", "claim", "context", "submit",
  "heartbeat", "abandon", "retry", "export-plan", "generation-scopes", "generate-uvm",
  "build-uvm", "job-status", "report", "install-trae",
  "prepare-design", "engineering-status", "check-design", "join-design", "revise",
] as const;
export type ChipVerificationAction = (typeof CHIP_VERIFICATION_ACTIONS)[number];
const MUTATIONS: ReadonlySet<ChipVerificationAction> = new Set([
  "import", "claim", "submit", "heartbeat", "abandon", "retry", "export-plan",
  "generate-uvm", "build-uvm", "install-trae",
  "prepare-design", "check-design", "join-design", "revise",
]);

const object = (value: unknown, label: string): JsonObject => {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new EffectRuntimeRequestError(`${label} must be an object`);
  }
  return value as JsonObject;
};
const text = (value: unknown, label: string): string => {
  if (typeof value !== "string" || !value.trim() || value.includes("\0") || value.length > 32768) {
    throw new EffectRuntimeRequestError(`${label} must be nonempty text without NUL, at most 32768 characters`);
  }
  return value;
};

export function admitChipVerification(input: unknown) {
  const request = object(input, "request");
  const actionText = text(request.action, "action");
  if (!(CHIP_VERIFICATION_ACTIONS as readonly string[]).includes(actionText)) {
    throw new EffectRuntimeRequestError(`unknown chip verification action: ${actionText}`);
  }
  const action = actionText as ChipVerificationAction;
  const workspace = text(request.workspace, "workspace");
  if (request.execute !== undefined && typeof request.execute !== "boolean") {
    throw new EffectRuntimeRequestError("execute must be a boolean");
  }
  const args = object(request.arguments ?? {}, "arguments");
  const required: Partial<Record<ChipVerificationAction, string[]>> = {
    import: ["spec"], claim: ["session_id"], context: ["attempt_id"], submit: ["attempt_id"],
    heartbeat: ["attempt_id"], abandon: ["attempt_id", "reason"], retry: ["task_id"],
    "generate-uvm": ["top"], "build-uvm": ["manifest"], "job-status": ["job_id"],
    "check-design": ["rtl_task_id"], "join-design": ["rtl_task_id", "manifest"],
    revise: ["task_id", "reason"],
  };
  for (const key of required[action] ?? []) text(args[key], key);
  for (const key of ["task_id", "attempt_id", "session_id", "kind", "profile_path", "scope_id", "simulator", "contract_task_id", "rtl_task_id"]) {
    if (args[key] !== undefined && args[key] !== null) text(args[key], key);
  }
  for (const key of ["chunk_chars", "context_chars", "limit", "timeout"]) {
    const value = args[key];
    if (value !== undefined && (value !== null || key === "timeout") && (!Number.isSafeInteger(value) || (value as number) < 1)) {
      throw new EffectRuntimeRequestError(`${key} must be a positive safe integer`);
    }
    const maximum = key === "timeout" ? 86400 : key === "chunk_chars" || key === "context_chars" ? 1000000 : undefined;
    if (maximum !== undefined && typeof value === "number" && value > maximum) {
      throw new EffectRuntimeRequestError(`${key} must not exceed ${maximum}`);
    }
  }
  if (args.run_smoke !== undefined && typeof args.run_smoke !== "boolean") {
    throw new EffectRuntimeRequestError("run_smoke must be a boolean");
  }
  if (args.flow !== undefined && !["design-dv", "dv"].includes(args.flow as string)) {
    throw new EffectRuntimeRequestError("flow must be design-dv or dv");
  }
  if (action === "submit") {
    const hasFile = args.response_file !== undefined && args.response_file !== null;
    const hasResponse = args.response !== undefined && args.response !== null;
    if (hasFile === hasResponse) throw new EffectRuntimeRequestError("submit requires exactly one of response_file or response");
    if (hasFile) text(args.response_file, "response_file");
    if (hasResponse) object(args.response, "response");
  }
  if (action === "generate-uvm") {
    const files = args.rtl_files ?? [];
    if (!Array.isArray(files) || (!files.length && !args.contract_task_id) || files.length > 4096) {
      throw new EffectRuntimeRequestError("rtl_files must contain 1 to 4096 paths, or bind a shared contract_task_id");
    }
    files.forEach((value, index) => text(value, `rtl_files[${index}]`));
  }
  const mutation = MUTATIONS.has(action);
  const allowed = !mutation || request.execute === true;
  return {
    schema_version: "chip_verification_admission_v0" as const,
    capability_id: "chip-verification" as const,
    action, workspace, allowed, mutation, requires_execute: mutation,
    status: allowed ? "admitted" as const : "preview" as const,
    reason: allowed ? "request admitted; provider must validate current domain state and evidence"
      : "explicit --execute is required for this local change",
    execution_performed: false,
    goal_authority_changed: false,
  };
}
