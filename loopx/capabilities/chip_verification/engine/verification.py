"""Real simulator evidence and constrained, agent-driven coverage closure."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import uuid

import jsonschema

from .agents import runtime
from .contracts import obj, arr, TEXT
from .models import ConflictError, ValidationError
from .workflow import save_json


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze_inputs(benchmark_dir, campaign_path):
    bench = Path(benchmark_dir).resolve()
    paths = [bench / "spec.json", bench / "coverage_manifest.json", bench / "programgen.py",
             bench / "upstream" / "picorv32.v", Path(campaign_path).resolve()]
    if (bench / "benchmark.json").is_file():
        paths.append(bench / "benchmark.json")
    paths.extend(sorted((bench / "tb").glob("*.sv")))
    paths.extend(sorted((bench / "tb").glob("*.svh")))
    return {str(path): digest(path) for path in paths}


def verify_hashes(hashes):
    for filename, expected in hashes.items():
        path = Path(filename)
        if not path.is_file() or digest(path) != expected:
            raise ValidationError("Input or evidence changed: " + filename)


def validate_simulation_evidence(evidence, benchmark_dir, expected_inputs=None):
    """Reparse raw observation evidence; never accept a reported percentage alone."""
    from .simulators.xsim import parse_evidence, parse_xcrg_report
    benchmark = Path(benchmark_dir).resolve()
    run = Path(evidence["run_dir"]).resolve()
    if evidence.get("status") != "passed":
        raise ValidationError("Simulation did not pass: " + evidence.get("error", "unknown failure"))
    if evidence.get("mutation"):
        raise ValidationError("Negative mutation runs cannot contribute to closure")
    verify_hashes(expected_inputs or {})
    recorded_inputs = {item["path"]: item["sha256"] for item in evidence.get("inputs", [])}
    verify_hashes(recorded_inputs)
    recorded_inputs[str(benchmark / "spec.json")] = evidence.get("spec_sha256")
    if expected_inputs and recorded_inputs != expected_inputs:
        raise ValidationError("Run was produced with a different input set or campaign")
    if evidence.get("spec_sha256") != digest(benchmark / "spec.json"):
        raise ValidationError("Run specification baseline differs")
    if evidence.get("rtl_sha256") != digest(benchmark / "upstream" / "picorv32.v"):
        raise ValidationError("Run RTL baseline differs")
    manifest_path = benchmark / "coverage_manifest.json"
    if evidence.get("coverage_manifest_sha256") != digest(manifest_path):
        raise ValidationError("Coverage model version mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    commands = evidence.get("commands", [])
    if [entry.get("tool") for entry in commands] != ["xvlog", "xelab", "xsim", "xcrg"]:
        raise ValidationError("Build/elaboration/simulation/native coverage reporting evidence is incomplete")
    if any(entry.get("exit_code") != 0 for entry in commands):
        raise ValidationError("An EDA command failed")
    for entry in commands:
        if Path(entry["log"]).resolve() != run / (entry["tool"] + "_console.log"):
            raise ValidationError("EDA command log is not the run's corresponding tool log")
    verify_hashes({entry["log"]: entry["log_sha256"] for entry in commands})
    verify_hashes({entry["path"]: entry["sha256"] for entry in evidence.get("artifacts", [])})
    log = (run / "xsim_console.log").read_text(encoding="utf-8", errors="replace")
    observed = parse_evidence(log, run / "coverage_hits.csv", manifest)
    claimed = evidence.get("coverage", {})
    if claimed.get("metric") != "functional":
        raise ValidationError("Wrong coverage metric")
    for key in ("hit_bins", "total_bins", "hit_ids"):
        if claimed.get(key) != observed["coverage"].get(key):
            raise ValidationError("Claimed coverage differs from raw evidence: " + key)
    percent = float(claimed.get("percent", -1))
    if not math.isfinite(percent) or not 0 <= percent <= 100:
        raise ValidationError("Claimed coverage must be a finite percentage in 0..100")
    if abs(percent - observed["coverage"]["percent"]) > 1e-7:
        raise ValidationError("Claimed percentage differs from raw evidence")
    if evidence.get("checks") != observed["checks"]:
        raise ValidationError("Claimed checker activity differs from raw evidence")
    if evidence.get("uvm_errors") != 0 or evidence.get("uvm_fatals") != 0:
        raise ValidationError("UVM reported an error")
    if not any(p.is_file() for p in (run / "xsim.covdb").rglob("*")):
        raise ValidationError("Native XSim coverage database is absent")
    if not any(p.is_file() for p in (run / "coverage_report").rglob("*")):
        raise ValidationError("Native XCRG report is absent")
    native_report = run / "coverage_report" / "xcrg_report.txt"
    if not native_report.is_file():
        raise ValidationError("Native XCRG per-bin text report is absent")
    native_score = parse_xcrg_report(native_report.read_text(encoding="utf-8", errors="replace"),
                                     observed["coverage"]["total_bins"], observed["coverage"]["hit_ids"])
    observed["coverage"]["xcrg_percent"] = native_score
    return {"passed": True, "validator": "xsim_raw_evidence_v1",
            "checks": ["frozen_inputs_unchanged", "four_eda_commands_passed", "uvm_summary_clean",
                       "checker_active", "raw_bins_equal_native_coverage", "native_database_and_report_present"],
            "measured": observed}


def _benchmark_path(project, benchmark_dir):
    path = Path(benchmark_dir)
    return path.resolve() if path.is_absolute() else (project.root / path).resolve()


def run_simulation(project, benchmark_dir, *, campaign=None, seed=1, timeout=600,
                   vivado_home=None, depends_on=None):
    from .simulators.xsim import XSimBackend
    project.assert_inputs_current()
    benchmark = _benchmark_path(project, benchmark_dir)
    if digest(benchmark / "spec.json") != project.config["spec_sha256"]:
        raise ValidationError("Benchmark specification is not the currently bound project baseline")
    campaign_path = Path(campaign).resolve() if campaign else benchmark / "campaign_smoke.json"
    inputs = freeze_inputs(benchmark, campaign_path)
    run_id = "sim_" + uuid.uuid4().hex
    directory = project.root / ".loopx" / "chip-verification" / "runs" / run_id
    directory.mkdir(parents=True)
    save_json(directory / "frozen_inputs.json", inputs)
    task = project.store.add_task("regress", "UVM simulation " + run_id,
                                  {"benchmark": str(benchmark), "campaign": str(campaign_path), "inputs": inputs},
                                  depends_on=depends_on or [], task_id=run_id)
    attempt = project.store.claim(task["id"], "simulator-host", lease_seconds=timeout * 5 + 180)
    project.store.record_run(run_id, "vivado_xsim", task_id=task["id"], attempt_id=attempt["id"],
                             metadata={"run_dir": str(directory), "input_hashes": inputs})
    project.store.update_run(run_id, "building")
    try:
        evidence = XSimBackend(vivado_home).run_benchmark(benchmark, directory,
                    campaign_path=campaign_path, seed=seed, timeout_seconds=timeout)
        project.assert_inputs_current()
        verify_hashes(inputs)
        if evidence.get("status") != "passed":
            raise ValidationError(evidence.get("error", "Simulator did not pass"))
        verdict = validate_simulation_evidence(evidence, benchmark, inputs)
        project.store.update_run(run_id, "running")

        def validator(task, result, artifacts):
            project.assert_inputs_current()
            return validate_simulation_evidence(result["evidence"], benchmark, inputs)

        artifacts = [directory / "evidence.json", directory / "frozen_inputs.json",
                     directory / "coverage_hits.csv"]
        artifacts.extend(Path(command["log"]) for command in evidence["commands"])
        artifacts.extend(p for p in (directory / "coverage_report").rglob("*") if p.is_file())
        artifacts.extend(p for p in (directory / "xsim.covdb").rglob("*") if p.is_file())
        result = {"run_id": run_id, "evidence": evidence, "coverage": verdict["measured"]["coverage"]}
        receipt = project.store.finish(task["id"], attempt["id"], result, artifacts=artifacts, validator=validator)
        project.store.update_run(run_id, "passed", metadata={"evidence": evidence, "validation": "accepted"},
                                 artifacts=[str(directory / "evidence.json")])
        return {"status": "passed", "run_id": run_id, "task_id": task["id"],
                "coverage": result["coverage"], "checks": evidence["checks"],
                "run_dir": str(directory), "evidence_path": str(directory / "evidence.json"), "receipt": receipt}
    except Exception as exc:
        current = project.store.get_run(run_id)
        if current["status"] in {"queued", "building", "running"}:
            project.store.update_run(run_id, "failed" if current["status"] != "queued" else "error",
                                     metadata={"error": str(exc)})
        project.store.fail(task["id"], attempt["id"], str(exc), retryable=True)
        return {"status": "failed", "run_id": run_id, "task_id": task["id"],
                "error": str(exc), "run_dir": str(directory)}


def campaign_schema(manifest):
    operations = [feature["name"].split(".", 1)[1] for feature in manifest["features"]
                  if feature["category"] == "instruction"]
    campaign = obj({
        "instructions": arr({"type": "string", "enum": operations}),
        "operand_classes": arr({"type": "string", "enum": ["zero", "one", "min_signed", "max_signed", "all_ones", "alternating", "general"]}),
        "both_branch_outcomes": {"type": "boolean"}, "all_memory_offsets": {"type": "boolean"},
        "division_exceptions": {"type": "boolean"}, "x0_discard": {"type": "boolean"},
        "random_arithmetic_count": {"type": "integer", "minimum": 0, "maximum": 1000},
        "seed": {"type": "integer", "minimum": 1, "maximum": 2147483647},
    })
    return obj({"campaign": campaign, "rationale": TEXT})


def _best_resumable_run(project, benchmark):
    """Recover a current accepted regression; session files are not authority."""
    from .simulators.xsim import SimulationError
    candidates = []
    for task in project.store.list_tasks(status="done", kind="regress"):
        if project.store.get_task(task["id"])["status"] != "done":
            continue
        payload = task["payload"]
        recorded_benchmark = payload.get("benchmark")
        if not isinstance(recorded_benchmark, str) or not recorded_benchmark or Path(recorded_benchmark).resolve() != benchmark:
            continue
        try:
            result = task["result"]
            evidence = result["evidence"]
            run = project.store.get_run(result["run_id"])
            if (run["status"] != "passed" or run["validity"] != "current" or
                    run["task_id"] != task["id"] or run["baseline_id"] != task["baseline_id"] or
                    run["tool"] != "vivado_xsim" or evidence.get("backend") != "vivado_xsim"):
                continue
            if Path(evidence["benchmark"]).resolve() != benchmark:
                raise ValidationError("Accepted run belongs to a different benchmark")
            # A campaign may differ from the smoke campaign; its actual frozen
            # input set must still equal today's full DUT/TB/model inventory.
            expected = freeze_inputs(benchmark, payload["campaign"])
            if payload["inputs"] != expected or run["metadata"].get("input_hashes") != expected:
                raise ValidationError("Regression input inventory no longer matches the current benchmark")
            directory = Path(evidence["run_dir"]).resolve()
            if Path(run["metadata"]["run_dir"]).resolve() != directory:
                raise ValidationError("State and evidence name different run directories")
            if json.loads((directory / "frozen_inputs.json").read_text(encoding="utf-8")) != expected:
                raise ValidationError("Frozen run input file differs from the accepted baseline")
            if json.loads((directory / "evidence.json").read_text(encoding="utf-8")) != evidence:
                raise ValidationError("Saved evidence differs from the accepted task result")
            verdict = validate_simulation_evidence(evidence, benchmark, expected)
            measured = verdict["measured"]
            candidates.append((measured["coverage"]["percent"], task["created"], task["id"], {
                "status": "passed", "run_id": result["run_id"], "task_id": task["id"],
                "coverage": measured["coverage"], "checks": measured["checks"],
                "run_dir": str(directory), "evidence_path": str(directory / "evidence.json"),
                "reused": True, "revalidated": True,
            }))
        except (OSError, ValueError, KeyError, TypeError, ValidationError, SimulationError) as error:
            project.store.invalidate(task["id"], "Cannot resume regression evidence: " + str(error))
    # Reject a candidate invalidated while checking a failed ancestor later in
    # the inventory; no aggregate of unrelated runs is created here.
    candidates = [item for item in candidates if project.store.get_task(item[2])["status"] == "done"]
    return max(candidates, key=lambda item: item[:3])[3] if candidates else None


def _reuse_campaign(project, task, previous, benchmark, protected, schema):
    """Recover a proposal accepted before an interrupted/failed simulation.

    The task result alone is insufficient: its previous observation, current
    inputs, accepted artifact digests, campaign bytes and original agent receipt
    must all identify the same immutable proposal.
    """
    project.assert_inputs_current()
    project.store.verify_artifacts()
    verify_hashes(protected)
    task = project.store.get_task(task["id"])
    predecessor = project.store.get_task(previous["task_id"])
    if (task["status"] != "done" or task["kind"] != "analyze" or
            task["baseline_id"] != project.store.baseline()["id"] or
            task["depends_on"] != [predecessor["id"]] or predecessor["status"] != "done" or
            predecessor["kind"] != "regress" or
            task["payload"] != {"run_id": previous["run_id"], "missing_bins": previous["coverage"]["missing_bins"],
                                "coverage_manifest_sha256": digest(benchmark / "coverage_manifest.json")}):
        raise ValidationError("Accepted campaign no longer matches the current predecessor and coverage gaps")
    expected = freeze_inputs(benchmark, predecessor["payload"]["campaign"])
    if predecessor["payload"]["inputs"] != expected or predecessor["result"]["run_id"] != previous["run_id"]:
        raise ValidationError("Previous run no longer has its accepted input inventory")
    measured = validate_simulation_evidence(predecessor["result"]["evidence"], benchmark, expected)
    if measured["measured"]["coverage"] != previous["coverage"]:
        raise ValidationError("Previous measured coverage changed before campaign recovery")

    artifacts = project.store.list_artifacts(task_id=task["id"], validity="current")
    verify_hashes({str(project.root / artifact["path"]): artifact["sha256"] for artifact in artifacts})
    required = {}
    for name in ("campaign.json", "response.json", "receipt.json"):
        matches = [artifact for artifact in artifacts if Path(artifact["path"]).name == name]
        if len(matches) != 1:
            raise ValidationError("Accepted campaign needs exactly one immutable " + name)
        required[name] = matches[0]
    attempts = {artifact["attempt_id"] for artifact in required.values()}
    if len(attempts) != 1:
        raise ValidationError("Campaign artifacts belong to different attempts")
    attempt_id = attempts.pop()
    directory = project.root / ".loopx" / "chip-verification" / "attempts" / attempt_id
    paths = {name: (project.root / artifact["path"]).resolve() for name, artifact in required.items()}
    if any(path.parent != directory.resolve() for path in paths.values()):
        raise ValidationError("Campaign evidence is outside its original accepted attempt")
    proposal = task["result"]["proposal"]
    jsonschema.validate(proposal, schema)
    if not proposal["campaign"]["instructions"] or not proposal["campaign"]["operand_classes"]:
        raise ValidationError("Accepted stimulus proposal is empty")
    if (json.loads(paths["response.json"].read_text(encoding="utf-8")) != proposal or
            json.loads(paths["campaign.json"].read_text(encoding="utf-8")) != proposal["campaign"]):
        raise ValidationError("Saved stimulus differs from the accepted proposal")
    receipt = json.loads(paths["receipt.json"].read_text(encoding="utf-8"))
    if (receipt.get("runtime") not in {"codex", "pi"} or receipt.get("attempt_id") != attempt_id or
            type(receipt.get("returncode")) is not int or receipt["returncode"] != 0 or
            not isinstance(receipt.get("session_id"), str) or not receipt["session_id"].strip() or
            receipt["session_id"] != task["result"].get("session_id") or receipt.get("response") != proposal):
        raise ValidationError("Original agent receipt does not authenticate this accepted proposal")
    return proposal, paths["campaign.json"]


def _request_campaign(project, task, previous, manifest, protected, schema, agent, timeout):
    # Read selected plan fragments only. The full tp.json is never a prompt.
    related_points = []
    for plan in project.store.list_tasks(status="done", kind="plan"):
        for point in plan["result"]["testpoints"]:
            sample = {key: point[key] for key in ("name", "stimulus", "checkers", "coverage")}
            if len(json.dumps(related_points + [sample])) > 6000:
                break
            related_points.append(sample)
        if len(json.dumps(related_points)) > 5000:
            break
    context = {"scope": manifest["scope"], "fixed_bin_model_sha256": task["payload"]["coverage_manifest_sha256"],
               "previous_coverage": previous["coverage"], "related_testplan": related_points,
               "generator_contract": "Instructions enable directed instruction programs. operand_classes selects source-register operand corner values. both_branch_outcomes enables both paths; all_memory_offsets enables each legal offset; division_exceptions adds divide-zero and signed overflow cases; x0_discard tests ignored x0 writes. Bus responder supplies varying waitstates independently. Select enough stimulus to cover the listed missing bins. Do not change DUT/checker/coverage model."}
    prompt = ("Act as a UVM coverage-closure engineer. This is measured evidence from a real XSim run. "
              "Return a stimulus campaign and rationale using the output schema. Do not use tools, edit files, "
              "claim new coverage, or alter the fixed model. A separate real simulation will validate your proposal.\n"
              + json.dumps(context, ensure_ascii=False))
    attempt = project.store.claim(task["id"], agent + "-coverage-worker", lease_seconds=timeout + 180)
    directory = project.root / ".loopx" / "chip-verification" / "attempts" / attempt["id"]
    try:
        agent_result = runtime(agent, timeout=timeout, max_prompt_chars=project.config["context_chars"]).run(
            prompt, cwd=project.root, run_dir=directory, schema=schema, attempt_id=attempt["id"])
        proposal = agent_result.response
        jsonschema.validate(proposal, schema)
        if not proposal["campaign"]["instructions"] or not proposal["campaign"]["operand_classes"]:
            raise ValidationError("Empty stimulus proposal")
        project.assert_inputs_current()
        verify_hashes(protected)
        campaign_path = directory / "campaign.json"
        save_json(campaign_path, proposal["campaign"])

        def validator(current, result, artifacts):
            project.assert_inputs_current()
            verify_hashes(protected)
            jsonschema.validate(result["proposal"], schema)
            return {"passed": True, "validator": "stimulus_only_proposal_v1",
                    "checks": ["campaign_schema_valid", "frozen_model_and_checker_unchanged"],
                    "claim": "proposal_only_not_coverage"}

        project.store.finish(task["id"], attempt["id"],
            {"proposal": proposal, "session_id": agent_result.session_id, "usage": agent_result.usage},
            artifacts=[campaign_path, directory / "response.json", directory / "receipt.json"], validator=validator)
        return proposal, campaign_path
    except Exception as error:
        current = project.store.get_task(task["id"])
        if current["status"] == "running" and current["attempt_id"] == attempt["id"]:
            try:
                project.store.fail(task["id"], attempt["id"], str(error), retryable=True)
            except ConflictError:
                pass
        raise


def close_coverage(project, benchmark_dir, *, target=90.0, max_iterations=3,
                   timeout=600, vivado_home=None, notify=None, agent="codex", fresh=False):
    if not 0 < target <= 100 or max_iterations < 1:
        raise ValueError("Target must be 0..100 and iterations positive")
    if type(fresh) is not bool:
        raise ValueError("fresh must be boolean")
    project.assert_inputs_current()
    project.store.verify_artifacts()
    project.store.recover_expired()
    benchmark = _benchmark_path(project, benchmark_dir)
    if digest(benchmark / "spec.json") != project.config["spec_sha256"]:
        raise ValidationError("Benchmark specification is not the currently bound project baseline")
    if not project.store.list_tasks(status="done", kind="plan"):
        raise ValidationError("Generate at least one accepted UVM testplan fragment before automated coverage closure")
    manifest_path = benchmark / "coverage_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    frozen_manifest = digest(manifest_path)
    protected = freeze_inputs(benchmark, benchmark / "campaign_smoke.json")
    results = []
    initial = None if fresh else _best_resumable_run(project, benchmark)
    if initial is None:
        initial = {**run_simulation(project, benchmark, timeout=timeout, vivado_home=vivado_home), "reused": False}
    results.append(initial)
    if notify:
        notify({"iteration": 0, "status": initial["status"], "coverage": initial.get("coverage"), "error": initial.get("error"), "reused": initial["reused"]})
    if initial["status"] != "passed":
        return {"target_met": False, "reason": "Initial UVM run failed", "runs": results}
    previous = initial
    for iteration in range(1, max_iterations + 1):
        if previous["coverage"]["percent"] >= target:
            break
        verify_hashes(protected)
        gap_task = project.store.add_task("analyze", "Generate stimulus for observed UVM coverage gaps",
                        {"run_id": previous["run_id"], "missing_bins": previous["coverage"]["missing_bins"],
                         "coverage_manifest_sha256": frozen_manifest}, depends_on=[previous["task_id"]])
        schema = campaign_schema(manifest)
        proposal_reused = gap_task["status"] == "done"
        try:
            if proposal_reused:
                proposal, campaign_path = _reuse_campaign(project, gap_task, previous, benchmark, protected, schema)
            elif gap_task["status"] == "pending":
                proposal, campaign_path = _request_campaign(project, gap_task, previous, manifest, protected, schema, agent, timeout)
            else:
                raise ValidationError("Coverage proposal is " + gap_task["status"] + "; inspect its preserved state before retrying")
        except Exception as exc:
            if proposal_reused and project.store.get_task(gap_task["id"])["status"] == "done":
                project.store.invalidate(gap_task["id"], "Cannot resume accepted campaign: " + str(exc))
            return {"target_met": False, "reason": str(exc), "proposal_task_id": gap_task["id"], "runs": results}
        # An accepted proposal can outlive its caller and its external child.
        # Do not launch a duplicate while an earlier execution is still active
        # or its termination has not yet been reconciled with durable run state.
        dependents = {task["id"] for task in project.store.list_tasks(kind="regress")
                      if gap_task["id"] in task["depends_on"]}
        active = [run["id"] for run in project.store.list_runs()
                  if run["task_id"] in dependents and run["validity"] == "current" and
                  run["status"] in {"queued", "building", "running"}]
        if active:
            return {"target_met": False, "reason": "An earlier simulation for this accepted campaign is still active or needs external-run reconciliation; no duplicate was launched",
                    "proposal_task_id": gap_task["id"], "pending_run_ids": active, "runs": results}
        previous = run_simulation(project, benchmark, campaign=campaign_path,
                                  seed=proposal["campaign"]["seed"], timeout=timeout,
                                  vivado_home=vivado_home, depends_on=[gap_task["id"]])
        previous["reused"] = False
        previous["proposal_reused"] = proposal_reused
        previous["proposal_task_id"] = gap_task["id"]
        results.append(previous)
        if notify:
            notify({"iteration": iteration, "status": previous["status"], "coverage": previous.get("coverage"), "error": previous.get("error"), "reused": False,
                    "proposal_reused": proposal_reused})
        if previous["status"] != "passed":
            break
    verify_hashes(protected)
    met = previous["status"] == "passed" and previous["coverage"]["percent"] >= target
    report = {"target_met": met, "target_percent": target, "metric": "functional",
              "scope": manifest["scope"], "coverage_model_sha256": frozen_manifest,
              "runs": results, "resumed": initial["reused"], "claim": "Measured benchmark functional coverage; not all-spec signoff or code coverage"}
    save_json(project.root / ".loopx" / "chip-verification" / "coverage_closure.json", report)
    return report
