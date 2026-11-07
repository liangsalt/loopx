"""Durable build/smoke orchestration for explicitly bound UVM manifests."""
from __future__ import annotations

from pathlib import Path
import uuid

from .models import ValidationError
from .verification import verify_hashes
from .workflow import save_json


def run_build(project, manifest_path, *, run_smoke=False, timeout=600, vivado_home=None):
    from .simulators.generic import GenericXSimBackend, validate_generic_evidence

    if timeout <= 0:
        raise ValueError("timeout must be positive")
    project.assert_inputs_current()
    project.store.verify_artifacts()
    manifest = Path(manifest_path)
    if not manifest.is_absolute():
        manifest = project.root / manifest
    manifest = manifest.resolve()
    backend = GenericXSimBackend(vivado_home)
    planned = backend.plan(manifest)
    inputs = {entry["path"]: entry["sha256"] for entry in backend.snapshot_inputs(manifest)}
    # Generated candidates and manually provided reusable UVCs share this entry.
    # Add dependencies only when exact accepted candidate bytes are being built.
    source_hashes = {str(Path(path).resolve()): inputs[path] for path in planned["compile_order"]}
    prerequisites = []
    for task in project.store.list_tasks(status="done", kind="implement"):
        records = task["result"].get("files", [])
        matched = False
        for record in records:
            artifact = (project.root / record["artifact_path"]).resolve()
            if source_hashes.get(str(artifact)) == record["sha256"]:
                matched = True
            # A generated files/ view is disposable; identity is the hash of its
            # immutable candidate, not a dependency on an arbitrary same-name file.
            if record["sha256"] in source_hashes.values():
                matched = True
        if matched:
            prerequisites.append(task["id"])
    run_id = "build_" + uuid.uuid4().hex
    directory = project.root / ".loopx" / "chip-verification" / "runs" / run_id
    directory.mkdir(parents=True)
    build = project.store.add_task("build", "Compile and elaborate UVM " + planned["top"],
                                  {"manifest": str(manifest), "inputs": inputs, "run_id": run_id},
                                  depends_on=prerequisites, task_id=run_id)
    attempt = project.store.claim(build["id"], "xsim-build-host", lease_seconds=timeout * 3 + 180)
    project.store.record_run(run_id, "vivado_xsim_generic", task_id=build["id"], attempt_id=attempt["id"],
                             metadata={"run_dir": str(directory), "run_smoke": run_smoke,
                                       "manifest": str(manifest), "input_hashes": inputs})
    project.store.update_run(run_id, "building")
    smoke = smoke_attempt = None
    evidence = None
    try:
        evidence = backend.run(manifest, directory, run_smoke=run_smoke, timeout_seconds=timeout)
        save_json(directory / "frozen_inputs.json", inputs)
        project.assert_inputs_current()
        verify_hashes(inputs)

        def validator(phase):
            def check(task, result, artifacts):
                project.assert_inputs_current()
                verify_hashes(inputs)
                actual = {entry["path"]: entry["sha256"] for entry in result["evidence"]["inputs"]}
                if actual != inputs:
                    raise ValidationError("Build input set changed between preparation and execution")
                if phase == "smoke" and result["evidence"].get("status") != "smoke_verified":
                    raise ValidationError("Smoke acceptance requires a clean UVM report, positive checker/transaction activity and no ignored/unsupported tool constructs")
                verdict = validate_generic_evidence(result["evidence"], manifest, phase=phase)
                if not verdict["accepted"] or (phase == "smoke" and not verdict.get("verified")):
                    raise ValidationError(verdict.get("reason", "Required UVM smoke activity or simulator feature support was not proven"))
                return {"passed": True, "validator": "generic_xsim_" + phase + "_v1",
                        "checks": ["input_inventory_current", "commands_revalidated", "raw_logs_revalidated"],
                        "measured": verdict}
            return check

        # Build is accepted separately even if a subsequent smoke exposes a bug.
        # The backend retains failed simulation evidence, so no success is inferred.
        artifacts = [directory / "evidence.json", directory / "frozen_inputs.json"]
        artifacts.extend(Path(entry["path"]) for entry in evidence.get("artifacts", []))
        artifacts.extend(Path(entry["log"]) for entry in evidence.get("commands", []))
        artifacts = list(dict.fromkeys(path.resolve() for path in artifacts))
        build_result = {"run_id": run_id, "evidence": evidence, "phase": "build", "coverage_verified": False}
        build_receipt = project.store.finish(build["id"], attempt["id"], build_result,
                                              artifacts=artifacts, validator=validator("build"))
        result = {"status": "built", "run_id": run_id, "build_task_id": build["id"],
                  "build_receipt": build_receipt, "evidence_path": str(directory / "evidence.json"),
                  "coverage": {"status": "not_evaluated"}}
        if run_smoke:
            project.store.update_run(run_id, "running")
            smoke = project.store.add_task("smoke", "Check UVM transaction and checker activity",
                                          {"manifest": str(manifest), "inputs": inputs, "run_id": run_id},
                                          depends_on=[build["id"]], task_id=run_id + "_smoke")
            smoke_attempt = project.store.claim(smoke["id"], "xsim-smoke-host", lease_seconds=180)
            smoke_result = {"run_id": run_id, "evidence": evidence, "phase": "smoke", "coverage_verified": False}
            receipt = project.store.finish(smoke["id"], smoke_attempt["id"], smoke_result,
                                           artifacts=artifacts, validator=validator("smoke"))
            result.update(status="smoke_verified", smoke_task_id=smoke["id"], smoke_receipt=receipt)
        project.store.update_run(run_id, "passed", metadata={"accepted_phase": "smoke" if run_smoke else "build"},
                                 artifacts=[str(directory / "evidence.json")])
        return result
    except Exception as error:
        task, active = (smoke, smoke_attempt) if smoke is not None else (build, attempt)
        if project.store.get_task(task["id"])["status"] == "running":
            project.store.fail(task["id"], active["id"], str(error), retryable=False)
        capability_block = bool(smoke is not None and evidence and evidence.get("status") == "smoke_unqualified")
        if capability_block:
            reason = "Simulator ignored/does not support required constructs; preserve checks and qualify another execution path. Raw diagnostics: " + str(directory / "evidence.json")
            project.store.block(task["id"], reason)
            project.store.add_issue("tool_capability", reason, task_id=task["id"])
        current = project.store.get_run(run_id)
        if current["status"] in {"queued", "building", "running"}:
            project.store.update_run(run_id, "failed", metadata={"error": str(error)})
        return {"status": "blocked" if capability_block else "failed", "run_id": run_id, "build_task_id": build["id"],
                "smoke_task_id": smoke["id"] if smoke else None, "error": str(error),
                "evidence_path": str(directory / "evidence.json"),
                "coverage": {"status": "not_evaluated"}}
