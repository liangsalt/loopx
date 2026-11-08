"""Exercise complete semantic contexts and cross-process recovery on public cases.

Run with the LoopX installation under test. This is a control-plane experiment;
it neither invokes a model nor executes EDA or measures hardware coverage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from loopx.capabilities.chip_verification import host_work
from loopx.capabilities.chip_verification.engine.spec import audit_manifest, iter_sources, load_manifest
from loopx.capabilities.chip_verification.engine.workflow import DVProject
from loopx.capabilities.chip_verification.provider import invoke


CASES = {
    "opentitan-serial-io": "case_tool.py",
    "opentitan-aon-control": "case_tool.py",
    "coralnpu-vector-zero-retirement": "coral_issue_cases.py",
    "coralnpu-fence-tso-ordering": "coral_issue_cases.py",
}


def fingerprint(value):
    return hashlib.sha256(host_work.encoded(value).encode("utf-8")).hexdigest()


def run_case(name, destination):
    started = time.monotonic()
    root = destination / name
    helper = CASES[name]
    arguments = (["stage", "--case", name, "--destination", str(root)] if helper == "case_tool.py"
                 else ["stage", name, str(root)])
    subprocess.run([sys.executable, str(Path(__file__).with_name(helper)), *arguments],
                   check=True, capture_output=True)

    def call(action, **arguments):
        return invoke(action, workspace=root, arguments=arguments, execute=True)

    first = call("import", spec="spec.json", flow="design-dv")
    project = DVProject(root)
    task_ids = [task["id"] for task in project.store.list_tasks()]
    # Exercise compatibility with callers that still send old numeric knobs.
    second = call("import", spec="spec.json", flow="design-dv", chunk_chars=32, context_chars=1)
    assert first["baseline"]["id"] == second["baseline"]["id"]
    assert task_ids == [task["id"] for task in project.store.list_tasks()]
    manifest = load_manifest(project.config["manifest_path"])
    assert audit_manifest(manifest)["source_inventory_complete"]
    sources = {source["source_id"]: source for source in iter_sources(manifest)}
    tasks = [task for task in project.store.list_tasks(kind="model")
             if task["payload"].get("execution") != "local_bundle_metadata"]
    observations, source_checks = [], 0
    for task in tasks:
        prompt, schema, pack = project.task_context(task)
        expected = set(task["payload"]["source_ids"]) | set(task["payload"].get("dependency_source_ids", []))
        assert {source["source_id"] for source in pack["sources"]} == expected
        for source in pack["sources"]:
            assert source["text"] == sources[source["source_id"]]["text"]
            source_checks += 1
        assert pack["budget"] == {"max_chars": None, "max_tokens": None}
        contract = {"task_id": task["id"], "kind": task["kind"], "prompt": prompt, "output_schema": schema,
                    "host_instruction": host_work.HOST_INSTRUCTION, "context_budget_chars": None, "context_chars": 0}
        while True:
            measured = len(host_work.encoded({**contract, "contract_sha256": "0" * 64}))
            if measured == contract["context_chars"]:
                break
            contract["context_chars"] = measured
        observations.append((measured, task["id"]))
    maximum, task_id = max(observations)
    claimed = call("claim", session_id="semantic-partition-trial", task_id=task_id)
    context = call("context", attempt_id=claimed["attempt_id"])
    assert context["context_chars"] == maximum and context["context_budget_chars"] is None
    script = """import hashlib,sys
from loopx.capabilities.chip_verification import host_work
from loopx.capabilities.chip_verification.provider import invoke
value=invoke('context',workspace=sys.argv[1],arguments={'attempt_id':sys.argv[2]})
print(hashlib.sha256(host_work.encoded(value).encode('utf-8')).hexdigest())
"""
    recovered = subprocess.check_output([sys.executable, "-c", script, str(root), claimed["attempt_id"]],
                                        text=True).strip()
    assert recovered == fingerprint(context)
    call("abandon", attempt_id=claimed["attempt_id"], reason="Context integrity trial complete; no model response produced")
    return {
        "case": name, "spec_bytes": (root / "spec.json").stat().st_size,
        "spec_sha256": manifest["spec_sha256"], "partitioning": manifest["partitioning"],
        "semantic_tasks": len(tasks), "source_records": len(sources), "complete_source_checks": source_checks,
        "maximum_observed_host_context_chars": maximum,
        "tasks_exceeding_former_24000_chars": sum(size > 24000 for size, _ in observations),
        "maximum_task_claimed": True, "fresh_process_context_equal": True,
        "legacy_length_arguments_changed_boundaries": False, "context_budget_chars": None,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "model_executed": False, "simulation_executed": False, "coverage_percent": None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path, help="New experiment directory outside this repository")
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    args = parser.parse_args()
    destination = args.destination.resolve()
    repository = Path(__file__).resolve().parents[2]
    if destination == repository or repository in destination.parents:
        parser.error("Use a fresh directory outside the repository containing reference answers")
    destination.mkdir(parents=True, exist_ok=False)
    reports = []
    for name in dict.fromkeys(args.cases):
        report = run_case(name, destination)
        reports.append(report)
        (destination / "summary-public-safe.json").write_text(
            json.dumps({"cases": reports}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
