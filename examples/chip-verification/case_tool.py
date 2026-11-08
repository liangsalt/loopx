"""Offline integrity checks and input-only staging for the committed chip cases.

This example helper does not execute a model, RTL, UVM or a coverage scorer.
Durable design/DV work remains owned by the installed chip-verification capability.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile

from fetch import git_blob, prose_text
from cadence_case_kit import stage_execution_kit, validate_execution_kit


CASES = Path(__file__).resolve().parent / "cases"
MINIMUM_BODY_BYTES = 512 * 1024


def read_json(path: Path) -> dict:
    return json.loads(path.read_bytes())


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def safe_path(root: Path, relative: str) -> Path:
    """Reject traversal, symlinks and Windows junctions before reading/copying."""
    require(isinstance(relative, str) and bool(relative), "Empty inventory path")
    part = PurePosixPath(relative)
    require(not part.is_absolute() and part.as_posix() == relative
            and all(x not in {".", ".."} for x in part.parts)
            and not any(x in relative for x in ("\\", ":", "\x00")),
            f"Unsafe inventory path: {relative}")
    current = root
    for component in (None, *part.parts):
        if component is not None:
            current /= component
        require(not current.is_symlink() and not getattr(current, "is_junction", lambda: False)(),
                f"Linked inventory path: {relative}")
    require(current.resolve().is_relative_to(root.resolve()), f"Escaping inventory path: {relative}")
    return current


def check_files(root: Path, entries: list[dict], *, exact: bool = True,
                extra: tuple[str, ...] = ()) -> dict[str, bytes]:
    result = {}
    for entry in entries:
        path = entry["path"]
        require(path.casefold() not in {p.casefold() for p in result}, f"Duplicate inventory path: {path}")
        data = safe_path(root, path).read_bytes()
        require(len(data) == entry["bytes"] and hashlib.sha256(data).hexdigest() == entry["sha256"],
                f"File digest/size mismatch: {path}")
        if "git_blob_sha1" in entry:
            require(git_blob(data) == entry["git_blob_sha1"], f"Git blob mismatch: {path}")
        result[path] = data
    if exact:
        actual = set()
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            checked = safe_path(root, relative)
            if checked.is_file():
                actual.add(relative)
        require(actual == set(result) | set(extra),
                f"Unexpected/missing files in {root.name}: {sorted(actual ^ (set(result) | set(extra)))[:8]}")
    return result


def check_input(root: Path, *, minimum: int = MINIMUM_BODY_BYTES) -> dict:
    """Recompute the qualification metric from original input bodies, not claims."""
    bundle = read_json(root / "spec.json")
    manifest = read_json(root / "source_manifest.json")
    require(bundle.get("schema") == "dvflow.spec_bundle" and bundle.get("schema_version") == 1,
            "Unsupported specification bundle")
    require(hashlib.sha256((root / "spec.json").read_bytes()).hexdigest() == manifest["spec_sha256"],
            "Specification digest mismatch")
    require(len((root / "spec.json").read_bytes()) > minimum, "Specification JSON too small")
    upstream = manifest["upstream"]
    require(re.fullmatch(r"[0-9a-f]{40}", upstream["commit"]) is not None,
            "Upstream revision must be immutable")
    docs = {doc["id"]: doc for doc in bundle["documents"]}
    sources = {doc["id"]: doc for doc in manifest["documents"]}
    require(len(docs) == len(bundle["documents"]) and len(sources) == len(manifest["documents"])
            and docs.keys() == sources.keys(), "Document inventory mismatch")
    licenses = manifest["licenses"]
    require(bool(licenses), "Input export must retain source licenses")
    check_files(root, licenses, exact=False)
    allowed = {"spec.json", "source_manifest.json", "problem.md", "integration_profile.json", "module_graph.json"}
    allowed.update(source["path"] for source in sources.values())
    for entry in licenses:
        require(PurePosixPath(entry["path"]).name in {"LICENSE", "NOTICE"}, "Unexpected license filename")
        allowed.add(entry["path"])
    if (root / "eda/kit_manifest.json").exists():
        allowed.update(validate_execution_kit(root))
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    require(actual == allowed, f"Unexpected/missing input files: {sorted(actual ^ allowed)[:8]}")
    hashes = set()
    body_bytes = prose_bytes = 0
    for identity, doc in docs.items():
        source = sources[identity]
        require(doc["role"] == source["role"] == "specification", "Answer/reference document in input bundle")
        path = source["path"]
        require(PurePosixPath(path).suffix in {".md", ".rst", ".adoc", ".json", ".hjson"},
                f"Implementation file in input: {path}")
        require(not {"answer", "reference", "rtl", "dv", "testplans"} & set(PurePosixPath(path).parts),
                f"Answer path in input: {path}")
        require(not any(label in PurePosixPath(path).name for label in ("_testplan", "_sim_cfg")),
                f"Verification answer in input: {path}")
        data = doc["content"].encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        require(data == safe_path(root, path).read_bytes() and sha == source["sha256"]
                and len(data) == source["bytes"] and sha == doc["origin"]["sha256"],
                f"Original document bytes changed: {identity}")
        origin = doc["origin"]
        if "repository" in origin or "ref" in origin or source.get("count_toward_size", True):
            require(origin.get("ref") == upstream["commit"]
                    and origin.get("repository") == upstream["repository"], "Unpinned specification body")
            require(git_blob(data) == origin.get("git_blob_sha1"), "Source Git blob mismatch")
        if source.get("count_toward_size", True):
            require(PurePosixPath(path).suffix in {".md", ".rst", ".adoc"}, "Only normative documents count")
            require(sha not in hashes, "Repeated specification body")
            hashes.add(sha)
            body_bytes += len(data)
            prose_bytes += len(prose_text(doc["content"], PurePosixPath(path).suffix).encode("utf-8"))
    require(body_bytes > minimum, f"Unique normative bodies must exceed {minimum} bytes; got {body_bytes}")
    require(body_bytes == manifest["unique_specification_body_bytes"], "Declared body size differs from original sources")
    require({"system", "architecture", "microarchitecture", "module"} <= {d["layer"] for d in docs.values()},
            "Missing design abstraction layer")
    relation_ids = set()
    for edge in bundle["relations"]:
        require(edge["source"] in docs and edge["target"] in docs and edge["source"] != edge["target"],
                "Invalid document relationship")
        require(edge["id"] not in relation_ids, "Duplicate relationship")
        relation_ids.add(edge["id"])
    # The case and the importer must agree on source-bound task amendments.
    # This imports only the stdlib domain parser, not inference/EDA dependencies.
    repository = Path(__file__).resolve().parents[2]
    if str(repository) not in sys.path:
        sys.path.insert(0, str(repository))
    from loopx.capabilities.chip_verification.engine.bundles import validate_bundle
    validate_bundle(bundle)
    adjudications = [doc for doc in docs.values() if doc["origin"].get("kind") == "source_adjudication"]
    for doc in adjudications:
        require(sources[doc["id"]].get("count_toward_size") is False,
                "Authored source adjudications cannot count toward the original specification size")
    profile = read_json(root / "integration_profile.json")
    require(profile.get("toolchain", {}).get("family") == "cadence"
            and profile["toolchain"].get("simulation") == "xcelium"
            and profile["toolchain"].get("native_build_required") is False,
            "Cadence execution toolchain contract is required")
    contract = docs.get("adjudication-CADENCE")
    require(contract is not None and sources[contract["id"]]["path"] == "cadence_contract.md"
            and contract["origin"].get("kind") == "source_adjudication",
            "Missing routed Cadence execution contract")
    require({target["document_id"] for target in contract["origin"]["targets"]}
            == {entry["id"] for entry in sources.values() if entry.get("upstream_path")},
            "Cadence execution contract must cover every original source")
    require(profile.get("execution_contract", {}).get("document_id") == contract["id"]
            and profile["execution_contract"].get("path") == "cadence_contract.md",
            "Cadence profile and embedded execution contract diverge")
    return {"spec_bytes": (root / "spec.json").stat().st_size,
            "unique_specification_body_bytes": body_bytes,
            "unique_specification_prose_bytes": prose_bytes, "documents": len(docs),
            "source_adjudications": len(adjudications)}


def check_answer_anchors(root: Path, reference_sources: dict[str, bytes]) -> dict:
    """Check traceability, without certifying the rubric's semantic completeness."""
    source_manifest = read_json(root / "input" / "source_manifest.json")
    source_ids = {item["id"]: item for item in source_manifest["documents"]}
    sources = dict(reference_sources)
    for item in source_ids.values():
        if item.get("upstream_path"):
            sources[item["upstream_path"]] = safe_path(root / "input", item["path"]).read_bytes()
    profile = read_json(root / "input" / "integration_profile.json")
    graph = read_json(root / "input" / "module_graph.json")
    nodes = {node["id"] for node in graph["nodes"]}
    require(len(nodes) == len(graph["nodes"]) and graph["root_module"] in nodes, "Invalid shared graph nodes")
    require(profile["case_id"] == graph["case_id"] == source_manifest["case_id"]
            and profile["root_module"] == graph["root_module"]
            and profile["relationship_evidence"] == graph["edges"], "Design/DV input graphs diverge")
    evidence = list(profile["top_configuration_evidence"])
    for edge in graph["edges"]:
        require(edge["from"] in nodes and edge["to"] in nodes, "Unknown module relationship endpoint")
        evidence.append(edge["evidence"])
    for item in evidence:
        data = sources[item["upstream_path"]]
        require(item["revision"] == source_manifest["upstream"]["commit"]
                and hashlib.sha256(data).hexdigest() == item["sha256"], "Unpinned integration evidence")
        lines = data.decode("utf-8").splitlines()
        start, end = item["line_start"], item["line_end"]
        require(1 <= start <= end <= len(lines)
                and "\n".join(lines[start - 1:end]) == item["excerpt"], "Integration excerpt changed")
    rubric = read_json(root / "obligations.json")
    require(rubric["case_id"] == graph["case_id"]
            and rubric["revision"] == source_manifest["upstream"]["commit"], "Unpinned answer rubric")
    ids = set()
    for obligation in rubric["obligations"]:
        require(obligation["id"] not in ids, "Repeated answer obligation")
        ids.add(obligation["id"])
        require(bool(obligation["source_refs"]), "Answer obligation needs normative provenance")
        for source in obligation["source_refs"]:
            require(source_ids[source["source_id"]]["upstream_path"] == source["upstream_path"],
                    "Answer source identity differs from input")
            require(bool(source["anchor_exact"])
                    and source["anchor_exact"] in sources[source["upstream_path"]].decode("utf-8"),
                    "Answer source anchor absent")
        for testpoint in obligation["reference_testpoints"]:
            require(bool(testpoint["anchor_exact"])
                    and testpoint["anchor_exact"] in reference_sources[testpoint["upstream_path"]].decode("utf-8"),
                    "Reference testpoint absent")
    bundle = read_json(root / "input" / "spec.json")
    adjudications = {doc["id"]: doc for doc in bundle["documents"]
                    if doc["origin"].get("kind") == "source_adjudication"}
    policies = set()
    issue_ids = set()
    for obligation in rubric.get("adjudication_obligations", []):
        source_id = obligation["source_document_id"]
        require(source_id in adjudications and source_id not in policies,
                "Adjudication obligation must identify a unique input policy")
        require(obligation["issue_id"] not in issue_ids, "Repeated adjudication issue")
        require(obligation["module"] in nodes | {node.get("module_type") for node in graph["nodes"]},
                "Unknown adjudication module")
        require(isinstance(obligation["requirement"], str) and bool(obligation["requirement"].strip())
                and isinstance(obligation["checks"], list) and bool(obligation["checks"])
                and all(isinstance(check, str) and check.strip() for check in obligation["checks"]),
                "Adjudication obligation requires a rule and concrete checks")
        require(obligation["status"] == "not_run", "Stored answer rubric is not simulation evidence")
        require(obligation["requirement"] in adjudications[source_id]["content"],
                "Answer rule differs from the revised input policy")
        policies.add(source_id)
        issue_ids.add(obligation["issue_id"])
    # The scope policy bounds the whole question and is not a reported erratum.
    require(policies == {key for key in adjudications if key != "adjudication-SCOPE"},
            "Every revised input issue must have one matching answer obligation")
    return {"review_obligations": len(ids), "adjudication_obligations": len(policies),
            "integration_evidence_anchors": len(evidence)}


def validate(cases: Path = CASES) -> dict:
    manifest = read_json(cases / "corpus_manifest.json")
    require(manifest["schema_version"] == 1, "Unsupported corpus manifest")
    require(manifest["minimum_unique_specification_body_bytes"] == MINIMUM_BODY_BYTES,
            "Corpus cannot lower the 512 KiB qualification floor")
    result = []
    ids = set()
    inventory = {"corpus_manifest.json", ".gitattributes"}
    for case in manifest["cases"]:
        identity = case["id"]
        require(re.fullmatch(r"[a-z0-9-]+", identity) is not None and identity not in ids, "Invalid/repeated case ID")
        ids.add(identity)
        root = safe_path(cases, identity)
        check_files(root / "input", case["input_files"])
        check_files(root, case["answer_files"], exact=False)
        inventory.update(f"{identity}/input/{item['path']}" for item in case["input_files"])
        inventory.update(f"{identity}/{item['path']}" for item in case["answer_files"])
        result.append({"case": identity, **check_input(root / "input")})
    reference = manifest["reference"]
    reference_root = safe_path(cases, reference["root"])
    reference_manifest_path = safe_path(cases, reference["manifest"])
    check_files(cases, [{**reference, "path": reference["manifest"]}], exact=False)
    reference_manifest = read_json(reference_manifest_path)
    for case in manifest["cases"]:
        upstream = read_json(cases / case["id"] / "input" / "source_manifest.json")["upstream"]
        require(reference_manifest["revision"] == upstream["commit"]
                and reference_manifest["repository"].removeprefix("https://github.com/")
                == upstream["repository"].removeprefix("https://github.com/"),
                "Problem and reference answer revisions differ")
    reference_files = check_files(reference_root, reference_manifest["files"],
                                  extra=(reference_manifest_path.relative_to(reference_root).as_posix(),))
    require(any(PurePosixPath(path).name == "LICENSE" for path in reference_files), "Missing upstream license")
    require(any(PurePosixPath(path).suffix == ".sv" for path in reference_files), "Missing stored reference RTL/UVM")
    require(any("testplan" in path and path.endswith(".hjson") for path in reference_files), "Missing stored reference testplans")
    reference_sources = {item["upstream_path"]: reference_files[item["path"]]
                         for item in reference_manifest["files"]}
    inventory.add(reference["manifest"])
    inventory.update(f"{reference['root']}/{path}" for path in reference_files)
    actual = {path.relative_to(cases).as_posix() for path in cases.rglob("*") if path.is_file()}
    require(actual == inventory, f"Unexpected/missing corpus files: {sorted(actual ^ inventory)[:8]}")
    for case, report in zip(manifest["cases"], result):
        report.update(check_answer_anchors(cases / case["id"], reference_sources))
    return {"ok": True, "cases": result, "reference_files": len(reference_files),
            "simulation_executed": False, "coverage_percent": None}


def stage(case_id: str, destination: Path, cases: Path = CASES) -> dict:
    """Export only qualified input files; never mix them into an existing run."""
    validate(cases)
    manifest = read_json(cases / "corpus_manifest.json")
    selected = next((case for case in manifest["cases"] if case["id"] == case_id), None)
    require(selected is not None, f"Unknown case: {case_id}")
    repositories = [p for p in cases.resolve().parents if (p / ".git").exists()] or [cases.resolve()]
    destination = destination.absolute()
    require(not destination.exists() and not destination.is_symlink(), "Destination must be new")
    require(not any(destination.resolve().is_relative_to(root) for root in repositories),
            "Stage outside the repository containing the answers")
    # No destructive cleanup is used; TemporaryDirectory owns only its new staging folder.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".chip-input-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "input"
        staging.mkdir()
        source_root = cases / case_id / "input"
        for item in selected["input_files"]:
            target = safe_path(staging, item["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(safe_path(source_root, item["path"]).read_bytes())
        check_files(staging, selected["input_files"])
        kit = stage_execution_kit(staging, read_json(staging / "integration_profile.json"))
        check_input(staging)
        require(not destination.exists(), "Destination appeared during staging")
        os.rename(staging, destination)
    return {"ok": True, "case": case_id, "input_files": len(selected["input_files"]),
            "destination": str(destination), "answers_exported": False,
            "cadence_kit": kit,
            "isolation": "input-only export; host filesystem and network access are not sandboxed"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("validate", help="Verify stored public problems and reference answer bytes offline")
    exporter = actions.add_parser("stage", help="Create a new input-only workspace outside the answer repository")
    exporter.add_argument("--case", required=True)
    exporter.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = validate() if args.action == "validate" else stage(args.case, args.destination)
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f"Case validation/staging failed: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
