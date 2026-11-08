"""Fetch pinned public chip documents into a user's workspace using only Python stdlib.

The generated specifications exercise large, layered inputs. They are not a
complete simulator project and do not qualify any chip coverage percentage.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import urllib.error
import urllib.request

from cadence_case_kit import stage_execution_kit, validate_execution_kit

CATALOGS = Path(__file__).resolve().parent / "catalogs"
CASES = ("opentitan_eg1", "cheshire")
LAYERS = {"system", "architecture", "microarchitecture", "module", "verification", "software"}
MINIMUM_PROSE_BYTES = 200 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024


def git_blob(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


def inside(root: Path, relative: str | Path) -> Path:
    resolved_root = root.resolve()
    path = (resolved_root / relative).resolve()
    def comparable(value: Path) -> Path:
        # Windows can return an extended-length spelling for one resolved path
        # but not its parent, especially while other downloads create folders.
        # Compare equivalent filesystem names after resolution, not before it.
        spelling = str(value)
        if os.name == "nt" and spelling.startswith("\\\\?\\UNC\\"):
            return Path("\\\\" + spelling[8:])
        if os.name == "nt" and spelling.startswith("\\\\?\\"):
            return Path(spelling[4:])
        return value
    if not comparable(path).is_relative_to(comparable(resolved_root)):
        raise ValueError(f"Output/cache path escapes selected root: {relative}; resolved {path}; root {resolved_root}")
    return path


def prose_text(text: str, suffix: str) -> str:
    """Conservative size accounting; preserve the complete source in the bundle."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    result, fence, rst_body = [], None, False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if suffix in {".md", ".markdown"}:
            marker = re.match(r"^\s*(`{3,}|~{3,})", line)
            if marker:
                token = marker.group(1)
                if fence is None:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence):
                    fence = None
                continue
        elif suffix == ".adoc" and stripped in {"----", "....", "++++"}:
            fence = None if fence == stripped else stripped
            continue
        if fence is not None:
            continue
        if suffix == ".rst" and re.match(r"^\s*\.\.\s+[\w-]+::", line):
            rst_body = True
            continue
        if rst_body:
            if not stripped or line[0].isspace():
                continue
            rst_body = False
        if line.startswith("    ") or line.startswith("\t"):
            continue
        result.append(line)
    return "".join(result)


def catalog_for(name: str) -> tuple[dict, bytes]:
    raw = (CATALOGS / f"{name}.json").read_bytes()
    catalog = json.loads(raw)
    identities = set()
    for source in catalog["documents"] + catalog["licenses"]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source["repository"]):
            raise ValueError("Invalid GitHub repository")
        for field, length in (("ref", 40), ("git_blob_sha1", 40), ("sha256", 64)):
            if not re.fullmatch(f"[0-9a-f]{{{length}}}", source[field]):
                raise ValueError(f"Every source needs a pinned {field}")
        path = PurePosixPath(source["path"])
        if path.is_absolute() or ".." in path.parts or "\\" in source["path"] or ":" in source["path"]:
            raise ValueError("Invalid upstream source path")
        identity = source["repository"], source["ref"], source["path"]
        if identity in identities:
            raise ValueError("Repeated upstream source in catalog")
        identities.add(identity)
    documents = catalog["documents"]
    ids = {doc["id"] for doc in documents}
    if len(ids) != len(documents):
        raise ValueError("Repeated document identity")
    for doc in documents:
        if doc["layer"] not in LAYERS or doc["role"] not in {"specification", "reference"}:
            raise ValueError("Invalid source layer or role")
        if doc["role"] == "specification" and Path(doc["path"]).suffix.lower() not in {".md", ".markdown", ".rst", ".adoc"}:
            raise ValueError("RTL and test sources must remain references")
    if not {"system", "microarchitecture", "module"} <= {doc["layer"] for doc in documents if doc["role"] == "specification"}:
        raise ValueError("A case needs specification documents at all three design layers")
    repositories = {(doc["repository"], doc["ref"]) for doc in documents}
    if not repositories <= {(item["repository"], item["ref"]) for item in catalog["licenses"]}:
        raise ValueError("Every upstream repository revision must retain its license")
    relation_ids = set()
    for relation in catalog["relations"]:
        if set(relation) != {"id", "source", "target", "kind", "rationale"}:
            raise ValueError("Invalid relation contract")
        if relation["source"] not in ids or relation["target"] not in ids or relation["source"] == relation["target"]:
            raise ValueError("Invalid relation endpoint")
        if relation["id"] in relation_ids or relation["kind"] not in {"refines", "integrates", "verifies", "related"}:
            raise ValueError("Invalid relation identity or kind")
        if not relation["rationale"].startswith("Candidate; unproven."):
            raise ValueError("Relationships must remain explicit unproven candidates")
        relation_ids.add(relation["id"])
    return catalog, raw


def verified(data: bytes, source: dict) -> bytes:
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError("Source exceeds the 16 MiB file limit")
    if git_blob(data) != source["git_blob_sha1"] or hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise ValueError(f"Pinned source hash mismatch: {source['repository']}/{source['path']}")
    data.decode("utf-8")
    return data


def fetch_source(root: Path, source: dict, *, offline: bool, cache: Path | None, timeout: float) -> tuple[dict, str, dict]:
    repo, ref, source_path = source["repository"], source["ref"], source["path"]
    relative = Path("sources") / repo.replace("/", "__") / source_path
    target = inside(root, relative)
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{source_path}"
    acquisition = {"path": relative.as_posix(), "method": "existing_verified_source"}
    if target.is_file():
        data = verified(target.read_bytes(), source)
    else:
        data = None
        if not offline:
            request = urllib.request.Request(url, headers={"User-Agent": "LoopX-chip-verification-starter/1"})
            for attempt in range(2):
                try:
                    with urllib.request.urlopen(request, timeout=timeout) as response:
                        data = verified(response.read(MAX_SOURCE_BYTES + 1), source)
                    acquisition["method"] = "network"
                    break
                except (urllib.error.URLError, TimeoutError) as error:
                    acquisition["network_failure"] = type(error).__name__
                    if attempt == 1 and cache is None:
                        raise
        if data is None:
            if cache is None:
                raise FileNotFoundError(f"Offline source absent: {relative}")
            data = verified(inside(cache, relative).read_bytes(), source)
            acquisition["method"] = "verified_cache_offline" if offline else "verified_cache_after_network_failure"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    origin = {"repository": repo, "ref": ref, "path": source_path, "url": url,
              "browse_url": f"https://github.com/{repo}/blob/{ref}/{source_path}",
              "local_path": relative.as_posix(), "bytes": len(data), "sha256": source["sha256"],
              "git_blob_sha1": source["git_blob_sha1"], "license": source["license"]}
    return origin, data.decode("utf-8"), acquisition


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def add_cadence_context(name: str, root: Path, documents: list[dict]) -> dict:
    """Route execution intent with original prose, without crediting authored bytes."""
    top = name + "_subsystem"
    profile = {"schema_version": 1, "case_id": name, "root_module": top,
        "classification": "reading_corpus_requires_authored_subsystem_contract",
        "scope_frozen": False, "forbidden_execution_roots": ["sources"],
        "toolchain": {"family": "cadence", "simulation": "xcelium", "coverage": "imc",
            "lint": "jasper_superlint", "cdc": "jasper_cdc", "formal": "jasper_fpv",
            "synthesis": "genus", "execution": "external", "native_build_required": False},
        "execution_contract": {"path": "cadence_contract.md", "document_id": "cadence-execution-policy",
            "upstream_native_build_required": False},
        "coverage_status": "not_run"}
    content = f"""# {name}: Cadence reading-corpus execution policy

Authority: benchmark author. This starter is a pinned reading collection, not a
complete runnable SoC or a qualified dependency closure. Its proposed candidate
top is `{top}`, not an upstream production top. No native tool execution or coverage
percentage is claimed. The original files and licenses remain byte-exact.

Before generating RTL or TP, choose a substantial connected subsystem from these
sources and publish its module graph, scope, observable requirements and interfaces.
Freeze one shared design/DV contract: top ports and packed widths; parameters and CSR
address map; clocks, reset parents and release order; queue ownership, backpressure,
error responses and timing; external boundary models and an independent oracle.
Do not claim the full SoC is runnable based on the selected reference snippets.
This explicit scope-design step is still pending; an empty profile cannot pass EDA.

Implement the accepted scope as self-contained portable synthesizable SystemVerilog
RTL/IP plus pure SV UVM/SVA, behavioral memory and controllable environment BFMs.
Use Cadence Xcelium and IMC for simulation/coverage, Jasper Superlint/CDC/FPV for
applicable analysis and Genus for elaboration/synthesis. No upstream FuseSoC, DVSim,
regtool, Bender, Bazel, Verilator or other native build flow is a required dependency.
No C/ELF software, custom DPI/PLI or technology macro is required: model digital
interfaces directly and declare any physical abstraction before freezing scope.
Reuse requires a complete reviewed portable SV closure; files below sources remain
read-only reference material, not an executable project. Upstream RTL/tests cannot
silently fill specification gaps or become the independent golden.

Run dv_generate_uvm(profile_path='eda/verification_profile.json') with the accepted
scope added as source-traced facts. Supply a complete reviewed eda/project.json,
ordered RTL/TB filelists, include files and constraints for the common Cadence runner
inspect/plan/run commands. No network fetch or code generation occurs during EDA.
Retain tool versions, input hashes, seeds, checker activity, failures, native databases
and reports; external events alone do not certify success. Freeze applicable functional
bins and exclusions before iterations; any 90% target requires measured functional
coverage and zero assertion/scoreboard failures, with code coverage separate.
Reference divergence from an explicit author convention is recorded, not automatically
classified as a candidate bug. These instructions add no original-prose size credit.
"""
    raw = content.encode("utf-8")
    inside(root, "cadence_contract.md").write_bytes(raw)
    write_json(inside(root, "integration_profile.json"), profile)
    targets = [{"document_id": doc["id"], "sha256": doc["origin"]["sha256"],
                "line_start": 1, "line_end": len(doc["content"].splitlines())}
               for doc in documents if doc["role"] == "specification" and doc["content"].splitlines()]
    documents.append({"id": "cadence-execution-policy", "title": "Authored Cadence execution scope policy",
        "layer": "architecture", "module": top, "features": ["execution_contract"], "role": "specification",
        "origin": {"authored": True, "kind": "source_adjudication", "sha256": hashlib.sha256(raw).hexdigest(),
            "local_path": "cadence_contract.md", "bytes": len(raw), "targets": targets}, "content": content})
    return profile


def build(name: str, workspace: Path, *, offline: bool = False, cache_root: Path | None = None, timeout: float = 20) -> dict:
    catalog, catalog_bytes = catalog_for(name)
    root = inside(workspace, name)
    # The explicit fetch command owns creation of this case output directory.
    root.mkdir(parents=True, exist_ok=True)
    cache = inside(cache_root, name) if cache_root else None
    descriptors = catalog["documents"] + catalog["licenses"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        fetched = list(pool.map(lambda item: fetch_source(root, item, offline=offline, cache=cache, timeout=timeout), descriptors))
    documents, manifest_sources, acquisitions = [], [], []
    hashes, counts = set(), Counter()
    for index, (source, (origin, content, acquisition)) in enumerate(zip(descriptors, fetched)):
        acquisitions.append(acquisition)
        if index >= len(catalog["documents"]):
            manifest_sources.append({"role": "license", "origin": origin})
            continue
        doc = {key: source[key] for key in ("id", "title", "layer", "module", "features", "role")}
        doc.update(origin=origin, content=content)
        documents.append(doc)
        prose_bytes = 0
        if source["role"] == "specification":
            if origin["sha256"] in hashes:
                raise ValueError("Repeated specification content does not count toward the size threshold")
            hashes.add(origin["sha256"])
            prose_bytes = len(prose_text(content, Path(source["path"]).suffix.lower()).encode("utf-8"))
            counts["specification_source_bytes"] += origin["bytes"]
            counts["specification_prose_bytes"] += prose_bytes
        else:
            counts["reference_source_bytes"] += origin["bytes"]
        manifest_sources.append({**{key: doc[key] for key in ("id", "role", "layer", "module", "features")},
                                 "prose_bytes": prose_bytes, "origin": origin})
    if counts["specification_prose_bytes"] < MINIMUM_PROSE_BYTES:
        raise ValueError("Unique specification prose must reach 200 KiB; references and licenses do not count")
    profile = add_cadence_context(name, root, documents)
    authored = documents[-1]
    manifest_sources.append({key: authored[key] for key in ("id", "role", "layer", "module", "features", "origin")}
                            | {"prose_bytes": 0, "count_toward_specification_prose_bytes": False})
    bundle = {"schema": "dvflow.spec_bundle", "schema_version": 1, "title": catalog["title"],
              "documents": documents, "relations": catalog["relations"]}
    write_json(inside(root, "spec.json"), bundle)
    spec_bytes = (root / "spec.json").read_bytes()
    manifest = {"corpus": name, "title": catalog["title"], "schema": "dvflow.spec_bundle", "schema_version": 1,
                "spec_path": "spec.json", "spec_bytes": len(spec_bytes), "spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
                "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(), **counts,
                "minimum_prose_bytes": MINIMUM_PROSE_BYTES, "document_count": len(documents),
                "relation_count": len(catalog["relations"]),
                "layers": dict(sorted(Counter(doc["layer"] for doc in documents).items())),
                "roles": dict(sorted(Counter(doc["role"] for doc in documents).items())),
                "checks": {"immutable_refs": True, "git_blobs_verified": True, "sha256_pins_verified": True,
                           "unique_sources": True, "unique_specification_contents": True,
                           "original_bytes_preserved": True, "minimum_prose_bytes_met": True,
                           "relation_endpoints_valid": True, "upstream_licenses_retained": True},
                "size_policy": "Only specification-role Markdown/RST/AsciiDoc prose counts; fenced or indented code, comments and RST directive bodies are excluded. Original document bytes remain intact.",
                "relationship_policy": "Edges are unproven candidate relations; source applicability still requires review for the selected RTL configuration.",
                "toolchain": profile["toolchain"], "execution_scope_frozen": False,
                "coverage_policy": "This corpus qualifies source integrity and semantic context input only, not measured chip coverage or SoC signoff.",
                "sources": manifest_sources}
    write_json(inside(root, "source_manifest.json"), manifest)
    inside(root, "catalog.json").write_bytes(catalog_bytes)
    license_lines = ["# Upstream source licenses", "", "Source copyright headers and license files are preserved. The declarations below are provenance labels; consult each original file and license.", ""]
    for source in manifest_sources:
        if source["role"] == "license":
            origin = source["origin"]
            license_lines += [f"- {origin['repository']} @ `{origin['ref']}`: {origin['license']}; [retained license]({origin['local_path']})"]
    inside(root, "UPSTREAM_LICENSES.md").write_text("\n".join(license_lines) + "\n", encoding="utf-8")
    # Verify the generated JSON did not normalize source text or drop final newlines.
    recovered = json.loads(spec_bytes)
    for doc in recovered["documents"]:
        if doc["content"].encode("utf-8") != inside(root, doc["origin"]["local_path"]).read_bytes():
            raise ValueError(f"Source roundtrip failed: {doc['id']}")
    receipt = {"case": name, "acquisition_counts": dict(Counter(item["method"] for item in acquisitions)),
               "sources": acquisitions, "spec_sha256": manifest["spec_sha256"],
               "network_failure_fallback_is_explicit": True}
    write_json(inside(root, "fetch_receipt.json"), receipt)
    kit = ({"files": validate_execution_kit(root), "native_tool_execution_performed": False}
           if (root / "eda").exists() else stage_execution_kit(root, profile))
    return {key: manifest[key] for key in ("corpus", "spec_bytes", "spec_sha256", "document_count", "relation_count",
            "specification_prose_bytes", "layers", "roles")} | {"acquisition_counts": receipt["acquisition_counts"], "cadence_kit": kit}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True, help="Write one independent case folder below this output workspace.")
    parser.add_argument("--case", action="append", choices=CASES, help="Repeat to select cases; defaults to both.")
    parser.add_argument("--offline", action="store_true", help="Verify existing files/cache without any network request.")
    parser.add_argument("--cache-root", type=Path, help="Optional portable cache containing case/sources/repository__name paths; every file must match both pinned hashes.")
    parser.add_argument("--timeout", type=float, default=20, help="Per-request network timeout in seconds (two attempts).")
    args = parser.parse_args()
    if not 0 < args.timeout <= 120:
        parser.error("timeout must be greater than zero and at most 120 seconds")
    result = [build(name, args.workspace.resolve(), offline=args.offline,
                    cache_root=args.cache_root.resolve() if args.cache_root else None, timeout=args.timeout)
              for name in dict.fromkeys(args.case or CASES)]
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
