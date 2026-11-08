"""Bind two candidate tracks to one specification revision before simulation."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .models import ConflictError, ValidationError
from .state import file_hash
from .workflow import save_json


def _tb_owners(project, paths, contract_task_id, contract_hash):
    """Only accepted UVM bytes for this contract may enter a design-backed join."""
    if any(not path.resolve().is_relative_to(project.root) for path in paths):
        raise ValidationError("Joined TB inputs must remain in the selected workspace")
    hashes = {str(path): file_hash(path) for path in paths}
    owned, owners = set(), []
    for task in project.store.list_tasks(status="done", kind="implement"):
        payload = task["payload"]
        if payload.get("contract_task_id") != contract_task_id or payload.get("contract_hash") != contract_hash:
            continue
        from .engineering import _assert_task_current
        _assert_task_current(project, task["id"])
        matching = []
        for record in task["result"].get("files", []):
            artifact = (project.root / record["artifact_path"]).resolve()
            if not artifact.is_relative_to(project.root) or not artifact.is_file() or file_hash(artifact) != record["sha256"]:
                raise ConflictError("Accepted UVM artifact changed before the design join")
            matching.extend(path for path, value in hashes.items() if value == record["sha256"])
        if matching:
            owned.update(matching)
            owners.append(task["id"])
    if owned != set(hashes):
        raise ValidationError("Every TB source in a design join must belong to the same accepted shared contract")
    return sorted(owners)


def _configuration(candidate, data):
    if candidate["contract"].get("parameters"):
        raise ValidationError("Parameterized design joins require an elaborated instance-configuration adapter; not yet qualified")
    if set(data.get("defines", [])) - {"UVM_NO_DPI"}:
        raise ValidationError("Joined simulation cannot change the preprocessor configuration checked for RTL")
    from .generation import _strip_comments
    if any(re.search(r"`(?:ifdef|ifndef|elsif)\s+UVM_NO_DPI\b", _strip_comments(Path(path).read_text(encoding="utf-8-sig")))
           for path in candidate["rtl_files"]):
        raise ValidationError("DUT cannot depend on a simulator-only UVM_NO_DPI define absent from static checks")


def validate_join(project, manifest_path, *, required=False):
    from .design import get_candidate
    from .design_checks import current_check
    from .simulators.generic import load_build_manifest

    raw = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
    if not raw.get("engineering_binding") and not required and not project.config.get("engineering_enabled"):
        return []  # Legacy manifest validation remains with its existing backend.
    data = load_build_manifest(manifest_path)
    binding = data.get("engineering_binding")
    if not binding:
        if required or project.config.get("engineering_enabled"):
            raise ValidationError("Design/DV simulation requires join-design to bind both tracks and static checks")
        return []
    candidate = get_candidate(project, binding["rtl_task_id"])
    if candidate.get("composition_required"):
        raise ValidationError("A partial module needs integration before a module-level simulation join")
    _configuration(candidate, data)
    if any(binding[key] != candidate[key] for key in ("contract_task_id", "contract_hash")):
        raise ConflictError("Design and DV name different shared specification revisions")
    if set(data["rtl_files"]) != set(candidate["rtl_files"]):
        raise ConflictError("Joined RTL inventory differs from the accepted design")
    if data["compile_order"][:len(candidate["rtl_files"])] != candidate["rtl_files"]:
        raise ConflictError("Joined RTL order differs from the checked design configuration")
    check = current_check(project, candidate["rtl_task_id"])
    if not check.get("qualified") or check.get("task_id") != binding["rtl_check_task_id"]:
        raise ValidationError("Current elaboration, lint and CDC evidence is required before joint simulation")
    paths = [Path(path) for path in data["tb_files"]]
    # Includes can affect executed behavior as much as translation units.
    for directory in data["include_dirs"]:
        if not Path(directory).resolve().is_relative_to(project.root):
            raise ValidationError("Join include directories must remain inside the workspace")
        paths.extend(path for path in Path(directory).rglob("*") if path.suffix.lower() in {".sv", ".v", ".svh", ".vh"})
    owners = _tb_owners(project, paths, candidate["contract_task_id"], candidate["contract_hash"])
    if owners != sorted(binding["tb_task_ids"]):
        raise ConflictError("Joined UVM task inventory changed")
    return [candidate["contract_task_id"], candidate["rtl_task_id"], check["task_id"], *owners]


def join(project, rtl_task_id, manifest_path):
    from .design import get_candidate
    from .design_checks import current_check
    from .simulators.generic import load_build_manifest

    project.assert_inputs_current()
    project.store.verify_artifacts()
    candidate = get_candidate(project, rtl_task_id)
    if candidate.get("composition_required"):
        return {"status": "blocked", "rtl_task_id": rtl_task_id,
                "reason": "Partial module candidates require a complete module integration; local completion is insufficient",
                "composition_required": True, "next_action": "engineering-status"}
    check = current_check(project, rtl_task_id)
    if not check.get("qualified"):
        return {"status": "blocked", "rtl_task_id": rtl_task_id, "checks": check,
                "next_action": "check-design", "reason": "Required static checks are not qualified"}
    source = Path(manifest_path).resolve()
    if not source.is_relative_to(project.root):
        raise ValidationError("Join manifest must remain in the selected workspace")
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    _configuration(candidate, data)
    directory = (source.parent / data.get("candidate_dir", ".")).resolve()
    if not directory.is_relative_to(project.root):
        raise ValidationError("UVM candidate directory must remain in the selected workspace")
    tb = [(directory / value).resolve() for value in data.get("tb_files", [])]
    if not tb or any(not path.is_relative_to(directory) or not path.is_file() for path in tb):
        raise ValidationError("UVM translation units must exist within their candidate directory")
    data["candidate_dir"] = str(directory)
    data["rtl_files"] = candidate["rtl_files"]
    # Retain the user's TB order; the actual design replaces only the RTL binding.
    previous_order = [(directory / value).resolve() for value in data.get("compile_order", [])]
    tb_order = [path for path in previous_order if path in tb] or tb
    if len(tb_order) != len(tb) or set(tb_order) != set(tb):
        raise ValidationError("Build template must order each TB unit exactly once")
    data["compile_order"] = [*candidate["rtl_files"], *map(str, tb_order)]
    paths = list(tb)
    for value in data.get("include_dirs", []):
        include = (directory / value).resolve()
        if not include.is_relative_to(project.root):
            raise ValidationError("Join include directories must remain inside the workspace")
        paths.extend(path for path in include.rglob("*") if path.suffix.lower() in {".sv", ".v", ".svh", ".vh"})
    owners = _tb_owners(project, paths, candidate["contract_task_id"], candidate["contract_hash"])
    data["engineering_binding"] = {
        "contract_task_id": candidate["contract_task_id"], "contract_hash": candidate["contract_hash"],
        "rtl_task_id": rtl_task_id, "rtl_check_task_id": check["task_id"], "tb_task_ids": owners}
    from .generation import _hash
    output = project.store.state_dir / "joins" / (_hash(data) + ".json")
    if output.exists() and json.loads(output.read_text(encoding="utf-8")) != data:
        raise ConflictError("Immutable joined build manifest changed")
    if not output.exists():
        save_json(output, data)
    load_build_manifest(output)
    validate_join(project, output, required=True)
    return {"status": "joined", "manifest": str(output), "binding": data["engineering_binding"],
            "next_action": "build-uvm", "compiled": False, "simulated": False, "coverage_verified": False}
