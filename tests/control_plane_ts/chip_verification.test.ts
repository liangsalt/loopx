import assert from "node:assert/strict";
import test from "node:test";
import {admitChipVerification as admit} from "../../loopx/control_plane/capabilities/chip_verification.ts";

test("mutation preview never admits a write or changes Goal authority", () => {
  const preview = admit({action: "import", workspace: "workspace with spaces", arguments: {spec: "large spec.json"}});
  assert.equal(preview.allowed, false);
  assert.equal(preview.status, "preview");
  assert.equal(preview.requires_execute, true);
  assert.equal(preview.execution_performed, false);
  assert.equal(preview.goal_authority_changed, false);
  const accepted = admit({action: "import", workspace: ".", arguments: {spec: "spec.json"}, execute: true});
  assert.equal(accepted.allowed, true);
  assert.equal(accepted.execution_performed, false);
});

test("uninitialized workspace reads need no mutation grant", () => {
  for (const action of ["doctor", "status", "design-map", "next", "generation-scopes", "report"]) {
    const result = admit({action, workspace: "absent-workspace"});
    assert.equal(result.allowed, true);
    assert.equal(result.mutation, false);
  }
});

test("session identities and attempt identities remain distinct and required", () => {
  assert.throws(() => admit({action: "claim", workspace: ".", execute: true}), /session_id/);
  assert.throws(() => admit({action: "context", workspace: ".", arguments: {session_id: "host-chat"}}), /attempt_id/);
  const result = admit({action: "heartbeat", workspace: ".", arguments: {attempt_id: "attempt-1", session_id: "host-chat"}});
  assert.equal(result.allowed, false);
  assert.equal(result.mutation, true);
});

test("submit accepts a single response source and never reflects generated material", () => {
  const request = {action: "submit", workspace: ".", execute: true, arguments: {attempt_id: "a1", response: {generated: "private RTL"}}};
  const result = admit(request);
  assert.equal(result.allowed, true);
  assert.ok(!JSON.stringify(result).includes("private RTL"));
  assert.throws(() => admit({...request, arguments: {...request.arguments, response_file: "response.json"}}), /exactly one/);
  assert.throws(() => admit({...request, arguments: {attempt_id: "a1", response: []}}), /response must be an object/);
});

test("malformed execute, unknown action, paths and context budgets fail closed", () => {
  assert.throws(() => admit({action: "import", workspace: ".", execute: "false", arguments: {spec: "spec.json"}}), /boolean/);
  assert.throws(() => admit({action: "finish-goal", workspace: ".", execute: true}), /unknown/);
  assert.throws(() => admit({action: "status", workspace: "\0"}), /NUL/);
  assert.throws(() => admit({action: "import", workspace: ".", arguments: {spec: "spec.json", context_chars: -1}}), /positive safe integer/);
  assert.throws(() => admit({action: "generate-uvm", workspace: ".", arguments: {top: "dut", rtl_files: []}}), /rtl_files/);
  assert.throws(() => admit({action: "generate-uvm", workspace: ".", arguments: {top: "dut", rtl_files: ["dut.sv", ""]}}), /rtl_files\[1\]/);
});

test("EDA launch still needs provider readiness after request admission", () => {
  const result = admit({action: "build-uvm", workspace: ".", arguments: {manifest: "manifest.json", simulator: "vivado"}, execute: true});
  assert.equal(result.allowed, true);
  assert.equal(result.execution_performed, false);
  assert.match(result.reason, /provider must validate/);
});

test("execution budgets reject coercion, nonfinite values and unbounded contexts", () => {
  const request = {action: "build-uvm", workspace: ".", execute: true};
  for (const timeout of [true, null, NaN, Infinity, 0, -1, 1.5, 86401]) {
    assert.throws(() => admit({...request, arguments: {manifest: "manifest.json", timeout}}), /timeout/);
  }
  for (const run_smoke of ["false", 0, null]) {
    assert.throws(() => admit({...request, arguments: {manifest: "manifest.json", run_smoke}}), /run_smoke/);
  }
  assert.equal(admit({...request, arguments: {manifest: "manifest.json", timeout: 86400, run_smoke: false}}).allowed, true);
  for (const key of ["context_chars", "chunk_chars"]) {
    for (const value of [true, NaN, 1000001]) {
      assert.throws(() => admit({action: "import", workspace: ".", arguments: {spec: "spec.json", [key]: value}}), new RegExp(key));
    }
  }
});
