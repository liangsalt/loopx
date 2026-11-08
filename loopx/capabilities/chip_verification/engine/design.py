"""Bounded RTL/IP candidates bound to the same source contract as UVM work.

These validators establish provenance and file ownership, not RTL correctness.
Compilation, lint, CDC and simulation remain separate evidence-producing tasks.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import jsonschema

from .contracts import TEXT, TEXTS, arr, obj
from .generation import _safe_path, _strip_comments, _within, _rtl_view
from .models import ConflictError, ValidationError
from .state import file_hash
from .workflow import save_json


REQUIRED_CHECKS = ("elaboration", "lint", "cdc")
QUESTION = obj({"question": TEXT, "requirement_ids": TEXTS, "blocking": {"type": "boolean"}})
MICROARCHITECTURE_SCHEMA = obj({
    "contract_task_id": TEXT, "contract_hash": TEXT, "top": TEXT,
    "approach": {"type": "string", "enum": ["adopt_supplied_microarchitecture", "refine_specification"]},
    "rationale": TEXT,
    "blocks": arr(obj({"id": TEXT, "module_id": TEXT, "purpose": TEXT,
                       "state_behavior": TEXT, "datapath": TEXT, "error_behavior": TEXT,
                       "clock_reset": TEXT, "requirement_ids": TEXTS, "design_choices": TEXTS})),
    "rtl_units": arr(obj({"path": TEXT, "modules": TEXTS, "purpose": TEXT, "requirement_ids": TEXTS})),
    "ip_bindings": arr(obj({"path": TEXT, "sha256": TEXT, "modules": TEXTS,
                             "requirement_ids": TEXTS, "rationale": TEXT})),
    "compile_order": TEXTS,
    "required_checks": arr({"type": "string", "enum": list(REQUIRED_CHECKS)}),
    "unresolved": arr(QUESTION),
})
RTL_SCHEMA = obj({
    "contract_task_id": TEXT, "contract_hash": TEXT, "top": TEXT,
    "files": arr(obj({"path": TEXT, "content": TEXT})),
    "notes": TEXTS, "unresolved": arr(QUESTION),
})

MICROARCHITECTURE_INSTRUCTION = """Develop ONE bounded module's microarchitecture from its shared source contract.
The input may already be microarchitecture: adopt its specified design rather than inventing prerequisite FS/AS documents.
For earlier requirements, refine their implementation explicitly. Preserve all supplied interfaces and relations.
Keep normative requirements separate from design_choices. Cover state transitions, datapath, errors, clocks and resets.
Allocate exclusive relative SV/V paths and a complete compile_order, or bind existing workspace IP by exact path/hash.
Never fetch IP, modify files, or use tools. IP reuse needs known local evidence; otherwise leave a precise unresolved item.
Map all in-scope requirements. A missing document name alone is not a blocker; unknown behavior needed for correctness is.
Keep elaboration, lint and CDC obligations. Return only the schema. This is a candidate, not a tool or verification result.
"""
RTL_INSTRUCTION = """Implement ONE bounded RTL/IP candidate from the accepted microarchitecture and shared source contract.
Return only the assigned RTL source files as JSON. Do not use tools or edit files. Never change the supplied interfaces,
module identities, parameters, source requirements, or cross-module relations. Implement all assigned behavior; no empty
stubs or TODO scaffolding. Existing IP is copied from pinned local bytes by the provider; do not return or rewrite it.
Keep testbench/checker code out of DUT files. Essential missing design detail must be explicit in unresolved rather than
guessed. Candidate acceptance does not mean compilation, lint, CDC, simulation, coverage or signoff has passed.
"""


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_encoded(value).encode("utf-8")).hexdigest()


def _declared_modules(content):
    return re.findall(r"\bmodule\s+(?:(?:automatic|static)\s+)?([A-Za-z_][A-Za-z0-9_$]*)\b", _strip_comments(content))


def _scope(task):
    payload = task["payload"]
    return {"scope_id": payload["scope_id"], "module_scope_id": payload.get("module_scope_id", payload["module_id"]),
            "scope_index": payload.get("scope_index", 0), "scope_count": payload.get("scope_count", 1),
            "partial_module": payload.get("partial_module", False),
            "composition_required": payload.get("composition_required", False)}


def _refs(values, allowed, label, *, required=True):
    if (required and not values) or len(values) != len(set(values)) or not set(values) <= allowed:
        raise ValidationError(label + " requires unique references to supplied requirements")


def _source_path(project, value):
    relative = _safe_path(value)
    path = (project.root / relative).resolve()
    if not path.is_relative_to(project.root) or not path.is_file():
        raise ValidationError("IP source must be an existing file inside the workspace")
    if path.suffix.lower() not in {".sv", ".svh", ".v"}:
        raise ValidationError("IP binding must identify SV/V source")
    return path


def _read_task(project, identifier, kind):
    task = project.store.get_task(identifier)
    if task["kind"] != kind or task["status"] != "done" or task["baseline_id"] != project.store.baseline()["id"]:
        raise ConflictError("Design dependency is not an accepted current " + kind + " task")
    records = project.store.list_artifacts(task_id=identifier, validity="current")
    if not records:
        raise ConflictError("Accepted design dependency has no current evidence")
    for record in records:
        path = (project.root / record["path"]).resolve()
        if not path.is_relative_to(project.root) or not path.is_file() or file_hash(path) != record["sha256"]:
            raise ConflictError("Accepted design dependency artifact changed")
    return task


def _bound(project, task):
    from .engineering import get_contract

    project.assert_inputs_current()
    if task["baseline_id"] != project.store.baseline()["id"]:
        raise ConflictError("Design task belongs to a superseded specification baseline")
    payload = task["payload"]
    shared = get_contract(project, payload["contract_task_id"])
    if shared["hash"] != payload["contract_hash"] or shared["task_id"] not in task["depends_on"]:
        raise ConflictError("Design task does not depend on its exact shared contract revision")
    if payload.get("requirements") != shared["requirements"]:
        raise ConflictError("Design task requirements differ from its shared source contract")
    return shared


def _blocking(response):
    return [item for item in response["unresolved"] if item["blocking"]]


def _validate_microarchitecture(project, task, response, shared):
    jsonschema.validate(response, MICROARCHITECTURE_SCHEMA)
    contract = shared["contract"]
    allowed = {item["id"] for item in shared["requirements"]}
    needed = {item["id"] for item in shared["requirements"] if item.get("applicability") == "in_scope"}
    module_ids = {item["id"] for item in contract["modules"]}
    if not response["rationale"].strip():
        raise ValidationError("Microarchitecture needs an implementation rationale")
    if set(response["required_checks"]) != set(REQUIRED_CHECKS) or len(response["required_checks"]) != len(REQUIRED_CHECKS):
        raise ValidationError("Microarchitecture must retain elaboration, lint and CDC obligations")
    mapped, block_ids, owned, modules = set(), set(), {}, []
    for block in response["blocks"]:
        if not block["id"].strip() or block["id"] in block_ids or block["module_id"] not in module_ids:
            raise ValidationError("Microarchitecture blocks need unique IDs and a shared module identity")
        block_ids.add(block["id"])
        if any(not block[key].strip() for key in ("purpose", "state_behavior", "datapath", "error_behavior", "clock_reset")):
            raise ValidationError("Microarchitecture must describe behavior, datapath, errors, clocks and resets")
        _refs(block["requirement_ids"], allowed, "Microarchitecture block")
        if any(not value.strip() for value in block["design_choices"]):
            raise ValidationError("Design choices must be explicit nonempty explanations")
        mapped.update(block["requirement_ids"])
    for section in ("rtl_units", "ip_bindings"):
        for unit in response[section]:
            path = _safe_path(unit["path"])
            if Path(path).suffix.lower() not in {".sv", ".svh", ".v"} or path.casefold() in owned:
                raise ValidationError("RTL/IP paths require exclusive SV/V file ownership")
            owned[path.casefold()] = path
            _refs(unit["requirement_ids"], allowed, "RTL/IP unit")
            mapped.update(unit["requirement_ids"])
            if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name) for name in unit["modules"]):
                raise ValidationError("RTL module names must be unescaped SV identifiers")
            modules.extend(unit["modules"])
            if not unit["purpose" if section == "rtl_units" else "rationale"].strip():
                raise ValidationError("RTL/IP unit needs a purpose or reuse rationale")
            if section == "ip_bindings":
                source = _source_path(project, path)
                if not re.fullmatch(r"[0-9a-f]{64}", unit["sha256"]) or file_hash(source) != unit["sha256"]:
                    raise ConflictError("IP binding does not match the pinned source bytes")
                declared = _declared_modules(source.read_text(encoding="utf-8-sig"))
                if len(declared) != len(set(declared)) or set(declared) != set(unit["modules"]):
                    raise ValidationError("IP module definitions differ from assigned file ownership")
    order = response["compile_order"]
    if len(order) != len(set(order)) or set(order) != set(owned.values()):
        raise ValidationError("Compile order must include every RTL/IP unit exactly once")
    if len(modules) != len(set(modules)):
        raise ValidationError("A module cannot be owned by multiple RTL/IP units")
    for issue in response["unresolved"]:
        _refs(issue["requirement_ids"], allowed, "Design question")
        if not issue["question"].strip():
            raise ValidationError("Unresolved design questions must explain the missing detail")
        mapped.update(issue["requirement_ids"])
    if not needed <= mapped:
        raise ValidationError("Microarchitecture silently omits in-scope source requirements")
    if not _blocking(response) and (not response["blocks"] or modules.count(contract["top"]) != 1):
        raise ValidationError("A usable microarchitecture must implement its shared top exactly once")


def _microarchitecture(project, task):
    parent = _read_task(project, task["payload"]["microarchitecture_task_id"], "microarchitecture")
    if parent["id"] not in task["depends_on"] or _digest(parent["result"]) != task["payload"]["microarchitecture_hash"]:
        raise ConflictError("RTL task does not depend on its exact microarchitecture revision")
    validate(project, parent, parent["result"], [])
    if _blocking(parent["result"]):
        raise ValidationError("Microarchitecture still has blocking design questions")
    return parent["result"]


def validate(project, task, result, artifacts):
    """Validate either a proposed response or a persisted RTL artifact result."""
    shared = _bound(project, task)
    response = result.get("response", result) if task["kind"] == "rtl" else result
    if task["kind"] not in {"microarchitecture", "rtl"}:
        raise ValidationError("Unsupported design task kind")
    jsonschema.validate(response, MICROARCHITECTURE_SCHEMA if task["kind"] == "microarchitecture" else RTL_SCHEMA)
    if (response["contract_task_id"] != shared["task_id"] or response["contract_hash"] != shared["hash"] or
            response["top"] != shared["contract"]["top"]):
        raise ConflictError("Design response changed its shared contract revision or top")
    if task["kind"] == "microarchitecture":
        _validate_microarchitecture(project, task, response, shared)
    else:
        microarchitecture = _microarchitecture(project, task)
        expected = {unit["path"]: unit for unit in microarchitecture["rtl_units"]}
        files = response["files"]
        names = [item["path"] for item in files]
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise ValidationError("RTL response must contain exactly its assigned output paths")
        for item in files:
            _safe_path(item["path"])
            content = _strip_comments(item["content"])
            if not content.strip() or re.search(r"\buvm_(?:test|env|driver|monitor|scoreboard)\b", content):
                raise ValidationError("RTL output needs design source, not an empty file or UVM testbench")
            declared = _declared_modules(content)
            if len(declared) != len(set(declared)) or set(declared) != set(expected[item["path"]]["modules"]):
                raise ValidationError("RTL module definitions differ from assigned file ownership")
        allowed = {item["id"] for item in shared["requirements"]}
        for issue in response["unresolved"]:
            _refs(issue["requirement_ids"], allowed, "RTL question")
            if not issue["question"].strip():
                raise ValidationError("Unresolved RTL question needs an explanation")
        if "response" in result:
            _validate_publication(project, task, result, microarchitecture)
    return {"passed": True, "validator": "shared_contract_" + task["kind"] + "_candidate_v1",
            "checks": ["same_spec_baseline", "same_shared_contract", "requirement_traceability", "exclusive_rtl_ownership"],
            "semantic_claim": "candidate_not_compiled_or_verified"}


def _validate_publication(project, task, result, microarchitecture):
    records = result["files"]
    if [item["path"] for item in records] != microarchitecture["compile_order"]:
        raise ValidationError("Published RTL inventory differs from the accepted compile order")
    response_files = {item["path"]: item["content"] for item in result["response"]["files"]}
    bindings = {item["path"]: item for item in microarchitecture["ip_bindings"]}
    response_hash = result.get("response_hash")
    if response_hash is not None and response_hash != _digest(result["response"]):
        raise ConflictError("Published RTL response identity changed")
    attempt_dir = project.store.state_dir / "attempts" / result["attempt_id"]
    # Existing accepted candidates remain readable. New submissions isolate each
    # response so a rejected draft cannot occupy a corrected response's paths.
    publication_dir = attempt_dir / "rtl-candidates" / response_hash if response_hash else attempt_dir
    for record in records:
        path = (project.root / record["artifact_path"]).resolve()
        boundary = (publication_dir / "rtl").resolve()
        if not path.is_relative_to(boundary) or not path.is_file() or file_hash(path) != record["sha256"]:
            raise ConflictError("Published RTL candidate bytes changed or escaped their attempt")
        if record["path"] in response_files:
            if path.read_bytes() != response_files[record["path"]].encode("utf-8") or record["origin"] != "generated":
                raise ConflictError("Published RTL differs from its accepted response")
        else:
            binding = bindings[record["path"]]
            if record["sha256"] != binding["sha256"] or record["origin"] != "ip":
                raise ConflictError("Published IP differs from its source binding")
            if file_hash(_source_path(project, binding["path"])) != binding["sha256"]:
                raise ConflictError("Bound IP source changed after candidate acceptance")


def task_context(project, task):
    shared = _bound(project, task)
    pack = {"task_id": task["id"], "contract_task_id": shared["task_id"], "contract_hash": shared["hash"],
            "shared_contract": shared["contract"], "requirements": shared["requirements"], "scope": _scope(task)}
    if task["kind"] == "microarchitecture":
        instruction, schema = MICROARCHITECTURE_INSTRUCTION, MICROARCHITECTURE_SCHEMA
    elif task["kind"] == "rtl":
        pack["microarchitecture"] = _microarchitecture(project, task)
        instruction, schema = RTL_INSTRUCTION, RTL_SCHEMA
    else:
        raise ValidationError("Unsupported design task kind")
    if pack["scope"]["composition_required"]:
        instruction += "\nThis is a partial module scope. Preserve the common interface; completion cannot establish the complete module. An explicit composition obligation remains.\n"
    prompt = instruction + "\n" + _encoded(pack)
    return prompt, schema, pack


def accept(project, task, response, attempt, directory):
    """Materialize immutable design artifacts; the host owns the final state commit."""
    validate(project, task, response, [])
    if task["kind"] == "microarchitecture":
        return response, []
    directory = Path(directory).resolve()
    expected_directory = (project.store.state_dir / "attempts" / attempt["id"]).resolve()
    if (directory != expected_directory or not directory.is_relative_to(project.root) or
            task["id"] != attempt["task_id"]):
        raise ConflictError("RTL output directory must belong to its active attempt")
    microarchitecture = _microarchitecture(project, task)
    response_hash = _digest(response)
    publication_dir = directory / "rtl-candidates" / response_hash
    generated = {item["path"]: item["content"].encode("utf-8") for item in response["files"]}
    bindings = {item["path"]: item for item in microarchitecture["ip_bindings"]}
    records, artifacts = [], []
    for logical in microarchitecture["compile_order"]:
        target = _within(publication_dir / "rtl", logical, project.store.state_dir)
        data = generated[logical] if logical in generated else _source_path(project, logical).read_bytes()
        if logical in bindings and hashlib.sha256(data).hexdigest() != bindings[logical]["sha256"]:
            raise ConflictError("IP source changed during publication")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != data:
                raise ConflictError("Immutable RTL output already contains a different response")
        else:
            with target.open("xb") as stream:
                stream.write(data)
        records.append({"path": logical, "artifact_path": target.relative_to(project.root).as_posix(),
                        "sha256": file_hash(target), "origin": "generated" if logical in generated else "ip"})
        artifacts.append(target)
    if not _blocking(response):
        _rtl_view(artifacts, response["top"])
    result = {"response": response, "response_hash": response_hash, "attempt_id": attempt["id"], "files": records,
              "compiled": False, "simulated": False, "coverage_verified": False, **_scope(task)}
    publication = publication_dir / "candidate.json"
    if publication.exists() and json.loads(publication.read_text(encoding="utf-8")) != result:
        raise ConflictError("Immutable RTL publication already contains a different response")
    if not publication.exists():
        save_json(publication, result)
    artifacts.append(publication)
    return result, artifacts


def reconcile(project):
    """Recover derived RTL work after acceptance or a process restart."""
    created = 0
    for task in project.store.list_tasks(status="done", kind="microarchitecture"):
        response = task["result"]
        validate(project, task, response, [])
        if _blocking(response):
            for issue in _blocking(response):
                project.store.add_issue("design_blocker", issue["question"], task_id=task["id"])
            continue
        identifier = "rtl_" + _digest([task["id"], response])[:24]
        project.store.add_task("rtl", "Implement RTL/IP for " + response["top"],
            {**task["payload"], "microarchitecture_task_id": task["id"], "microarchitecture_hash": _digest(response)},
            depends_on=[task["id"], task["payload"]["contract_task_id"]], task_id=identifier)
        created += 1
    return created


def get_candidate(project, rtl_task_id):
    """Read and revalidate exact RTL/IP bytes; never qualify tool checks by inference."""
    project.assert_inputs_current()
    task = _read_task(project, rtl_task_id, "rtl")
    result = task["result"]
    validate(project, task, result, [])
    response = result["response"]
    if _blocking(response):
        raise ValidationError("RTL candidate still has blocking design questions")
    shared = _bound(project, task)
    microarchitecture = _microarchitecture(project, task)
    files = result["files"]
    paths = [str((project.root / record["artifact_path"]).resolve()) for record in files]
    _rtl_view([Path(path) for path in paths], response["top"])
    return {"rtl_task_id": task["id"], "contract_task_id": shared["task_id"], "contract_hash": shared["hash"],
            "top": response["top"], "rtl_files": paths,
            "inputs": dict(zip(paths, (record["sha256"] for record in files))), "files": files,
            "requirement_ids": [item["id"] for item in shared["requirements"]],
            "required_checks": microarchitecture["required_checks"], "contract": shared["contract"],
            "compiled": False, "simulated": False, "coverage_verified": False, **_scope(task)}
