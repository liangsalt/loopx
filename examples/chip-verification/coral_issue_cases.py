"""Offline qualification and Cadence input export for issue-inspired SV cases.

This Python example manages public benchmark files, not control-plane decisions.
It never invokes an agent, EDA, a reference model, or a coverage scorer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile


CASES = Path(__file__).resolve().parent / "issue_cases"
MINIMUM_BYTES = 512 * 1024
CADENCE_CASES = {
    "coralnpu-vector-zero-retirement": ("coral_vector_retirement_subsystem", "coral_vector"),
    "coralnpu-fence-tso-ordering": ("coral_memory_order_subsystem", "coral_memory_order"),
}
CADENCE_TOOLCHAIN = {
    "family": "cadence", "simulation": "xcelium", "coverage": "imc",
    "lint": "jasper_superlint", "cdc": "jasper_cdc", "formal": "jasper_fpv",
    "synthesis": "genus", "execution": "external", "native_build_required": False,
}
CADENCE_MUTATIONS = {
    "coralnpu-vector-zero-retirement": ["missing zero-element completion", "disabled legality check",
        "dropped masked condition", "wrong fault prefix", "duplicate retirement",
        "misassociated response ID", "dropped flush cancellation"],
    "coralnpu-fence-tso-ordering": ["illegal TSO decode", "fence treated as a no-op",
        "violation of an R->W edge", "swapped response ID", "duplicate retirement",
        "wrong sign extension", "dropped flush cancellation"],
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read_json(path: Path) -> dict:
    return json.loads(path.read_bytes())


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    require(isinstance(relative, str) and bool(relative), "Empty inventory path")
    part = PurePosixPath(relative)
    require(not part.is_absolute() and part.as_posix() == relative
            and not any(x in relative for x in ("\\", ":", "\x00"))
            and all(x not in {".", ".."} and x.rstrip(" .") == x for x in part.parts),
            f"Unsafe inventory path: {relative}")
    path = root / relative
    for ancestor in (path, *path.parents):
        require(not ancestor.is_symlink()
                and not getattr(ancestor, "is_junction", lambda: False)(),
                f"Linked inventory path: {relative}")
    require(path.resolve().is_relative_to(root.resolve()), f"Escaping path: {relative}")
    return path


def check_inventory(root: Path, entries: list[dict]) -> dict[str, bytes]:
    result = {}
    for entry in entries:
        relative = entry["path"]
        require(relative.casefold() not in {p.casefold() for p in result}, "Duplicate inventory path")
        data = safe_path(root, relative).read_bytes()
        require(len(data) == entry["bytes"] and digest(data) == entry["sha256"],
                f"Inventory digest/size mismatch: {relative}")
        result[relative] = data
    actual = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if safe_path(root, relative).is_file():
            actual.add(relative)
    require(actual == set(result), f"Unexpected/missing files: {sorted(actual ^ set(result))}")
    return result


def check_input(root: Path) -> dict:
    manifest = read_json(safe_path(root, "source_manifest.json"))
    bundle = read_json(safe_path(root, "spec.json"))
    require(bundle.get("schema") == "dvflow.spec_bundle" and bundle.get("schema_version") == 1,
            "Unsupported specification bundle")
    require(digest((root / "spec.json").read_bytes()) == manifest["spec_sha256"],
            "Specification digest mismatch")
    require((root / "spec.json").stat().st_size > MINIMUM_BYTES, "Specification JSON too small")
    docs = {d["id"]: d for d in bundle["documents"]}
    sources = {d["id"]: d for d in manifest["documents"]}
    require(len(docs) == len(bundle["documents"]) and len(sources) == len(manifest["documents"])
            and docs.keys() == sources.keys(), "Source inventory differs from bundle")
    revisions = manifest["upstreams"]
    require(set(revisions) == {"google-coral/coralnpu", "riscv/riscv-isa-manual"},
            "Unexpected source repository")
    require(all(re.fullmatch(r"[0-9a-f]{40}", value) for value in revisions.values()),
            "Mutable upstream revision")
    licenses = {entry["repository"]: entry for entry in manifest["licenses"]}
    require(licenses.keys() == revisions.keys(), "Missing source license")
    allowed = {"spec.json", "source_manifest.json", "problem.md", "module_graph.json", "integration_profile.json"}
    for license_entry in licenses.values():
        data = safe_path(root, license_entry["path"]).read_bytes()
        require(digest(data) == license_entry["sha256"] and git_blob(data) == license_entry["git_blob_sha1"],
                "Source license changed")
        require(license_entry["revision"] == revisions[license_entry["repository"]], "Unpinned license")
        allowed.add(license_entry["path"])
    counted, total = set(), 0
    for identity, source in sources.items():
        doc = docs[identity]
        require(doc["role"] == source["role"] == "specification", "Answer/reference document in input")
        require(doc["origin"] == source["origin"], "Origin metadata differs")
        relative = source["path"]
        require(PurePosixPath(relative).suffix in {".md", ".adoc"}, "Non-document input source")
        require(not {"answers", "reference", "rtl", "tests"} & set(PurePosixPath(relative).parts),
                "Answer path in input")
        raw = safe_path(root, relative).read_bytes()
        require(raw == doc["content"].encode("utf-8")
                and digest(raw) == source["sha256"] and len(raw) == source["bytes"],
                f"Original source body changed: {identity}")
        allowed.add(relative)
        if source["count_toward_size"]:
            origin = source["origin"]
            require(origin.get("ref") == revisions.get(origin.get("repository"))
                    and origin.get("git_blob_sha1") == git_blob(raw)
                    and origin.get("sha256") == digest(raw), "Unpinned original source")
            require(origin.get("license") == licenses[origin["repository"]]["spdx"], "License mismatch")
            require(bool(source.get("applicability")), "Unscoped source body")
            require(digest(raw) not in counted, "Duplicate normative body")
            counted.add(digest(raw))
            total += len(raw)
        else:
            require(source["origin"].get("kind") in {"authored_exercise", "source_adjudication"},
                    "Unexpected uncounted document")
    require(total > MINIMUM_BYTES, f"Unique original bodies must exceed {MINIMUM_BYTES}; got {total}")
    require(total == manifest["unique_original_body_bytes"], "Body size declaration differs")
    for seed in manifest["repair_seeds"]:
        require(seed["role"] == "historical_mechanism_reference"
                and seed.get("execution_required") is False and seed["path"].startswith("baseline/"),
                "Invalid baseline role")
        raw = safe_path(root, seed["path"]).read_bytes()
        origin = seed["origin"]
        require(origin["repository"] == "google-coral/coralnpu"
                and origin["ref"] == revisions[origin["repository"]]
                and digest(raw) == seed["sha256"] == origin["sha256"]
                and len(raw) == seed["bytes"] and git_blob(raw) == origin["git_blob_sha1"],
                "Unpinned baseline repair seed")
        allowed.add(seed["path"])
    top, asset_prefix = CADENCE_CASES[manifest["case_id"]]
    expected_assets = {f"contracts/{asset_prefix}_contract_pkg.sv", f"contracts/{asset_prefix}_ports.svh"}
    assets = manifest.get("execution_assets", [])
    require(len(assets) == len(expected_assets) and {entry["path"] for entry in assets} == expected_assets,
            "Incomplete or unexpected interface asset inventory")
    for asset in assets:
        require(asset.get("role") == "interface_contract" and asset.get("top") == top
                and asset.get("count_toward_size") is False, "Invalid interface asset role")
        raw = safe_path(root, asset["path"]).read_bytes()
        require(len(raw) == asset["bytes"] and digest(raw) == asset["sha256"],
                "Interface asset digest/size mismatch")
        require(raw.decode("utf-8").replace("\r\n", "\n")
                in docs["case-cadence-contract"]["content"].replace("\r\n", "\n"),
                "Interface asset differs from the embedded normative contract")
        allowed.add(asset["path"])
    if (root / "eda" / "kit_manifest.json").exists():
        from cadence_case_kit import validate_execution_kit
        allowed.update(validate_execution_kit(root))
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*")
              if safe_path(root, p.relative_to(root).as_posix()).is_file()}
    require(actual == allowed, f"Unexpected/missing input files: {sorted(actual ^ allowed)}")
    require({"system", "architecture", "microarchitecture", "module"}
            <= {d["layer"] for d in docs.values()}, "Missing design layer")
    graph = read_json(root / "module_graph.json")
    profile = read_json(root / "integration_profile.json")
    require(profile["case_id"] == graph["case_id"] == manifest["case_id"], "Case identities differ")
    require(profile.get("historical_repository") == "https://github.com/google-coral/coralnpu"
            and profile.get("historical_revision") == revisions["google-coral/coralnpu"],
            "Historical repository/revision differs from original baseline")
    require(profile.get("root_module") == graph["root_module"], "Profile/graph root module differs")
    require(profile.get("root_module") == profile.get("execution_top") == top,
            "Cadence execution top differs from authored interface")
    require(profile.get("toolchain") == CADENCE_TOOLCHAIN and profile.get("native_build_required") is False
            and profile.get("reference_is_execution_oracle") is False
            and profile.get("entry_modes") == ["spec_first_standard_sv"]
            and profile.get("missing_build_dependencies") == [],
            "Cadence exercise must not require the upstream native build or reference oracle")
    require(profile.get("forbidden_execution_roots") == ["baseline", "sources"]
            and set(profile.get("interface_assets", [])) == expected_assets,
            "Cadence interface/history execution boundary differs")
    require(profile["module_graph_sha256"] == digest((root / "module_graph.json").read_bytes()),
            "Design/DV graph differs")
    policy = docs[profile["scope_policy_id"]]
    require(policy["origin"].get("authored") is True
            and policy["origin"].get("kind") == "source_adjudication"
            and policy["origin"]["sha256"] == digest(policy["content"].encode("utf-8")),
            "Invalid source adjudication policy")
    require(policy["origin"].get("case_id") == manifest["case_id"]
            and policy["origin"].get("repair_boundary") == profile["repair_boundary"],
            "Scope policy belongs to a different case or repair boundary")
    targets = {item["document_id"]: item for item in policy["origin"]["targets"]}
    originals = {identity: source for identity, source in sources.items() if source["count_toward_size"]}
    require(targets.keys() == originals.keys(), "Scope policy does not accompany all original sources")
    for identity, source in originals.items():
        target = targets[identity]
        require(target["sha256"] == source["sha256"] and target["line_start"] == 1
                and target["line_end"] == len(docs[identity]["content"].splitlines()),
                "Scope policy target range differs from original")
    contract = docs.get("case-cadence-contract", {})
    origin = contract.get("origin", {})
    require(profile.get("contract_document_id") == "case-cadence-contract"
            and profile.get("contract_document") == "cadence_contract.md"
            and profile.get("contract_sha256") == digest(contract.get("content", "").encode("utf-8"))
            and origin.get("sha256") == profile["contract_sha256"]
            and origin.get("kind") == "source_adjudication" and origin.get("authored") is True
            and origin.get("case_id") == manifest["case_id"]
            and origin.get("repair_boundary") == profile["repair_boundary"],
            "Cadence contract is missing, stale or bound to another exercise")
    contract_targets = origin.get("targets", [])
    require(len(contract_targets) == len(originals) + 1
            and {entry["document_id"] for entry in contract_targets} == set(originals) | {"case-problem"},
            "Cadence contract must accompany all original sources and the problem")
    for target in contract_targets:
        source = sources[target["document_id"]]
        require(target["sha256"] == source["sha256"] and target["line_start"] == 1
                and target["line_end"] == len(docs[source["id"]]["content"].splitlines()),
                "Cadence contract target differs from its source")
    nodes = {node["id"] for node in graph["nodes"]}
    require(len(nodes) == len(graph["nodes"]) and graph["root_module"] in nodes, "Invalid module graph")
    for edge in graph["edges"]:
        require(edge["from"] in nodes and edge["to"] in nodes and edge["from"] != edge["to"],
                "Invalid module edge")
        require(edge["source_id"] in docs and edge["anchor_exact"] in docs[edge["source_id"]]["content"],
                "Module edge source anchor absent")
    return {"case": manifest["case_id"], "spec_bytes": (root / "spec.json").stat().st_size,
            "unique_original_body_bytes": total, "documents": len(docs),
            "normative_applicability": "bounded_by_authored_cadence_contract", "applicable_behavior_bytes": None,
            "execution_toolchain": "cadence", "native_upstream_build_required": False,
            "execution_top": top, "interface_assets": len(assets)}


def validate(cases: Path = CASES) -> dict:
    corpus = read_json(safe_path(cases, "corpus_manifest.json"))
    require(corpus["schema_version"] == 1 and corpus["minimum_unique_original_body_bytes"] == MINIMUM_BYTES,
            "Unsupported corpus or lowered qualification floor")
    results, seen = [], set()
    for identity in corpus["cases"]:
        require(re.fullmatch(r"[a-z0-9-]+", identity) and identity not in seen, "Invalid case id")
        seen.add(identity)
        root = safe_path(cases, identity)
        case = read_json(safe_path(root, "case_manifest.json"))
        check_inventory(root / "input", case["input_files"])
        answers = check_inventory(root / "answers", case["answer_files"])
        report = check_input(root / "input")
        require(report["case"] == identity, "Case manifest identity differs")
        reference = read_json(root / "answers" / "reference_manifest.json")
        base = read_json(root / "input" / "source_manifest.json")["upstreams"]["google-coral/coralnpu"]
        require(reference["base_revision"] == base and reference["fix_parent"] == base,
                "Reference parent differs from input baseline")
        require(re.fullmatch(r"[0-9a-f]{40}", reference["fixed_revision"]), "Mutable fixed reference")
        for entry in reference["files"]:
            data = answers[entry["path"]]
            require(git_blob(data) == entry["git_blob_sha1"] and digest(data) == entry["sha256"],
                    "Fixed reference blob differs")
        require(reference["simulation_executed"] is False and reference["coverage_percent"] is None,
                "Packaged source is not simulation evidence")
        require(reference.get("role") == "historical_mechanism_reference"
                and reference.get("required_for_cadence_execution") is False
                and reference.get("oracle_for_standalone_subsystem") is False,
                "Historical upstream reference cannot be a mandatory Cadence dependency/oracle")
        rubric = read_json(root / "answers" / "review_rubric.json")
        profile = read_json(root / "input" / "integration_profile.json")
        require(rubric.get("cadence_contract_sha256") == profile["contract_sha256"]
                and rubric.get("execution_top") == profile["execution_top"]
                and rubric.get("native_upstream_regression_required") is False,
                "Answer rubric differs from standalone Cadence contract")
        require(rubric.get("required_mutation_checks") == CADENCE_MUTATIONS[identity],
                "Answer mutation checks differ from the case contract")
        oracle = rubric.get("behavior_oracle", {})
        require(oracle.get("path") == "behavior_oracle.md"
                and oracle.get("kind") == "authored_behavioral_oracle"
                and oracle.get("sha256") == digest(answers.get("behavior_oracle.md", b""))
                and oracle.get("source_contract_sha256") == profile["contract_sha256"]
                and set(oracle.get("source_document_ids", [])) == {"case-cadence-contract", "case-scope-policy",
                    "riscv-riscv-isa-manual--src--v-st-ext-adoc" if identity == "coralnpu-vector-zero-retirement"
                    else "riscv-riscv-isa-manual--src--rv32-adoc"},
                "Authored behavioral oracle differs from the source-bound rubric")
        results.append(report)
    return {"cases": results, "simulation_executed": False, "coverage_percent": None,
            "qualification": "offline_source_integrity_only"}


def stage(identity: str, destination: Path, cases: Path = CASES) -> dict:
    corpus = read_json(safe_path(cases, "corpus_manifest.json"))
    require(identity in corpus["cases"], "Unknown case")
    root = safe_path(cases, identity)
    case = read_json(safe_path(root, "case_manifest.json"))
    data = check_inventory(root / "input", case["input_files"])
    report = check_input(root / "input")
    destination = destination.absolute()
    for ancestor in (destination, *destination.parents):
        require(not ancestor.is_symlink() and not getattr(ancestor, "is_junction", lambda: False)(),
                "Linked stage destination")
    require(not destination.exists(), "Stage destination must not exist")
    require(not destination.resolve().is_relative_to(cases.resolve()), "Cannot stage inside corpus")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".coral-stage-", dir=destination.parent))
    try:
        for relative, raw in data.items():
            path = safe_path(temporary, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        check_inventory(temporary, case["input_files"])
        from cadence_case_kit import stage_execution_kit
        stage_execution_kit(temporary, read_json(temporary / "integration_profile.json"))
        check_input(temporary)
        require(not destination.exists(), "Stage destination appeared during export")
        os.rename(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {**report, "destination": str(destination), "answers_exported": False,
            "simulation_executed": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    commands.add_parser("list")
    export = commands.add_parser("stage")
    export.add_argument("case")
    export.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command == "stage":
        result = stage(args.case, args.destination)
    elif args.command == "list":
        result = read_json(CASES / "corpus_manifest.json")
    else:
        result = validate()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
