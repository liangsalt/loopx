"""Lossless source inventory for JSON specifications, independent of agent sessions.

JSON is parsed locally. Only task-owned semantic source units are sent to a model later;
inventorying a document is deliberately not a claim that its semantics are known.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

from .source_structure import PARTITIONING, object_units, sections


# Only content digests and small successful reports survive an audit call. A
# long-lived runtime must neither retain whole specifications nor trust mtimes.
_AUDIT_CACHE_LIMIT = 64
_AUDIT_CACHE: OrderedDict[tuple[str, ...], dict[str, Any]] = OrderedDict()
_AUDIT_CACHE_LOCK = Lock()


class SpecError(ValueError):
    """The source cannot be inventoried without silently changing information."""


class SpecIntegrityError(SpecError):
    """A preserved source or inventory has changed since ingestion."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SpecError(f"Duplicate JSON object key would lose source material: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise SpecError(f"Non-standard JSON number: {value}")


def _pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _hint(value: Any) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value)
        return text if len(text) <= 160 else text[:157] + "..."
    return None


def _metadata(value: Any, inherited: dict[str, Any]) -> dict[str, Any]:
    """Copy only explicit, small labels; never infer verification truth."""
    result = dict(inherited)
    if isinstance(value, dict):
        for keys, target in (
            (("module", "module_name", "block"), "module"),
            (("feature", "function", "function_name"), "feature"),
            (("interface", "protocol"), "interface"),
            (("requirement_id", "req_id"), "requirement_id"),
            (("title", "heading", "name"), "title"),
        ):
            for key in keys:
                label = _hint(value.get(key))
                if label is not None:
                    result[target] = label
                    break
        # Common structured requirements use an id paired with behavior text.
        if "id" in value and any(k in value for k in ("requirement", "shall", "description", "text")):
            label = _hint(value["id"])
            if label is not None:
                result["explicit_id"] = label
    return result


def _leaves(value: Any, pointer: str = "", inherited: dict[str, Any] | None = None) -> Iterator[tuple[str, str, str, dict[str, Any]]]:
    metadata = _metadata(value, inherited or {})
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from _leaves(child, pointer + "/" + _pointer_part(key), metadata)
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            yield from _leaves(child, pointer + "/" + str(index), metadata)
    else:
        if isinstance(value, str):
            kind, text = "string", value
        elif value is None:
            kind, text = "null", "null"
        elif isinstance(value, bool):
            kind, text = "boolean", _json(value)
        elif isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise SpecError(f"Non-finite numeric value at {pointer or '/'}")
            kind, text = "number", _json(value)
        elif isinstance(value, dict):
            kind, text = "empty_object", "{}"
        elif isinstance(value, list):
            kind, text = "empty_array", "[]"
        else:
            raise SpecError(f"Unsupported JSON value at {pointer}")
        yield pointer, kind, text, metadata


def _inventory(parsed, bundle, digest):
    """Derive semantic boundaries and ownership entirely from the input snapshot."""
    from .bundles import source_metadata
    records, leaves, total_chars = [], [], 0
    units = object_units(parsed)
    for pointer, kind, text, metadata in _leaves(parsed):
        if bundle is not None:
            metadata = {**metadata, **source_metadata(bundle, pointer)}
        owner = units[pointer]
        leaf_id = "leaf_" + _sha(pointer.encode("utf-8"))[:24]
        fragment_ids = []
        first_record = len(records)
        spans = sections(text) if kind == "string" and not owner["atomic"] and metadata.get("source_role") != "bundle_metadata" else [
            {"start": 0, "end": len(text), "index": None, "titles": [], "parents": []}]
        for span in spans:
            start, end = span["start"], span["end"]
            fragment = text[start:end]
            source_id = "src_" + _sha(_json([PARTITIONING, pointer, start, end]).encode("utf-8"))[:24]
            base = "object:" + owner["pointer"]
            unit = base if span["index"] is None else f"section:{pointer}:{span['index']}"
            parents = ["object:" + parent for parent in owner["parents"]]
            if span["index"] is not None:
                parents += [base] + [f"section:{pointer}:{index}" for index in span["parents"]]
            structure = {"unit": unit, "object_pointer": owner["pointer"],
                         "section_path": span["titles"], "parents": list(dict.fromkeys(parents))}
            fragment_ids.append(source_id)
            records.append({
                "source_id": source_id, "leaf_id": leaf_id, "pointer": pointer,
                "start_char": start, "end_char": end, "value_kind": kind, "text": fragment,
                "revision": _sha(fragment.encode("utf-8")), "spec_sha256": digest,
                "metadata": metadata, "structure": structure, "semantic_status": "unreviewed",
                "boundary": "complete_value" if start == 0 and end == len(text) else "complete_section",
            })
        for position, record in enumerate(records[first_record:]):
            record["previous_source_id"] = fragment_ids[position - 1] if position else None
            record["next_source_id"] = fragment_ids[position + 1] if position + 1 < len(fragment_ids) else None
        total_chars += len(text)
        leaves.append({"leaf_id": leaf_id, "pointer": pointer, "value_kind": kind,
                       "text_chars": len(text), "value_sha256": _sha(text.encode("utf-8")),
                       "source_ids": fragment_ids, "disposition": "awaiting_semantic_review"})
    return records, leaves, total_chars


def _extraction_tasks(bundle, records, digest, manifest_path):
    from .bundles import adjudication_dependencies
    groups, metadata_records = {}, []
    for record in records:
        if record["metadata"].get("source_role") == "bundle_metadata":
            metadata_records.append(record)
        else:
            groups.setdefault(record["structure"]["unit"], []).append(record)
    adjudications = adjudication_dependencies(bundle, records)
    direct = {}
    for record in records:
        parents = record["structure"]["parents"]
        direct[record["source_id"]] = set(adjudications.get(record["source_id"], [])) | {
            parent["source_id"] for unit in parents for parent in groups.get(unit, [])}
    tasks = []
    for unit, group in groups.items():
        ids = [record["source_id"] for record in group]
        seen, pending = set(ids), [item for sid in ids for item in direct[sid]]
        while pending:
            source_id = pending.pop()
            if source_id not in seen:
                seen.add(source_id)
                pending.extend(direct.get(source_id, []))
        dependencies = sorted(seen - set(ids))
        structure = group[0]["structure"]
        scope = {"unit": unit, "object_pointer": structure["object_pointer"],
                 "section_path": structure["section_path"]}
        title = " / ".join(scope["section_path"]) or group[0]["metadata"].get("title") or scope["object_pointer"] or "root object"
        payload = {"source_ids": ids, "spec_sha256": digest, "manifest_path": str(manifest_path),
                   "semantic_scope": scope, "partitioning": PARTITIONING,
                   "instruction": "Interpret one declared object or document section. Preserve its module/function scope, conditions and exceptions. Parent context and source-bound rulings are dependencies, not separate owned dispositions. Record unresolved references without guessing."}
        if dependencies:
            payload["dependency_source_ids"] = dependencies
            payload["acceptance"] = {"owned_source_ids": ids,
                "source_adjudication_sources": {sid: adjudications[sid] for sid in sorted(seen) if sid in adjudications}}
        tasks.append({"task_id": f"extract_s2_{digest[:12]}_{_sha(unit.encode())[:16]}",
                      "kind": "model", "title": "Interpret specification: " + title,
                      "source_ids": ids, "spec_sha256": digest,
                      "estimated_source_chars": sum(len(record["text"]) for record in group), "payload": payload})
    if metadata_records:
        ids = [record["source_id"] for record in metadata_records]
        tasks.append({"task_id": f"metadata_s2_{digest[:12]}", "kind": "model",
            "title": "Inventory explicit bundle labels, provenance and candidate relation metadata",
            "source_ids": ids, "spec_sha256": digest, "estimated_source_chars": 0,
            "payload": {"source_ids": ids, "spec_sha256": digest, "manifest_path": str(manifest_path),
                        "execution": "local_bundle_metadata", "partitioning": PARTITIONING}})
    return tasks


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(_json(record) + "\n")


def ingest_spec(path: str | Path, output_dir: str | Path, max_chunk_chars: int | None = None) -> dict[str, Any]:
    """Create an immutable snapshot, lossless inventory and structural semantic tasks.

    ``output_dir`` is a storage parent, normally ``project/.loopx/chip-verification/specs``.
    Paths in the returned manifest are absolute. Extraction task payloads contain
    references only; no source is declared semantically reviewed by this parser.
    """
    # Deprecated length input is accepted for callers with saved configuration;
    # it never changes the source boundaries or rejects a natural unit.
    source_path = Path(path).resolve()
    raw = source_path.read_bytes()
    try:
        parsed = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise SpecError(f"Cannot parse UTF-8 JSON specification: {error}") from error
    from .bundles import validate_bundle
    bundle = validate_bundle(parsed)
    digest = _sha(raw)
    destination = Path(output_dir).resolve() / f"{digest[:20]}-v2-{PARTITIONING}"
    destination.mkdir(parents=True, exist_ok=True)
    manifest_path = destination / "manifest.json"
    if manifest_path.exists():
        existing = load_manifest(manifest_path)
        if existing.get("schema_version") != 2 or existing.get("partitioning") != PARTITIONING:
            raise SpecIntegrityError("Structural source cache cannot reuse another partitioning strategy")
        audit_manifest(existing)
        return existing

    try:
        records, leaves, total_chars = _inventory(parsed, bundle, digest)
    except (RecursionError, UnicodeEncodeError) as error:
        raise SpecError(f"Specification cannot be inventoried losslessly: {error}") from error

    snapshot = destination / "spec.original.json"
    sources_path = destination / "sources.jsonl"
    leaves_path = destination / "leaves.jsonl"
    snapshot.write_bytes(raw)
    _write_jsonl(sources_path, records)
    _write_jsonl(leaves_path, leaves)

    tasks = _extraction_tasks(bundle, records, digest, manifest_path)
    manifest = {
        "schema_version": 2, "partitioning": PARTITIONING,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "spec_path": str(source_path), "spec_sha256": digest, "spec_bytes": len(raw),
        "snapshot_path": str(snapshot), "manifest_path": str(manifest_path),
        "source_records_path": str(sources_path), "leaf_records_path": str(leaves_path),
        "sources_sha256": _sha(sources_path.read_bytes()), "leaves_sha256": _sha(leaves_path.read_bytes()),
        "source_count": len(records), "leaf_count": len(leaves), "source_text_chars": total_chars,
        "max_chunk_chars": None,
        "source_inventory_status": "complete", "semantic_status": "unreviewed",
        "extraction_tasks": tasks,
    }
    if bundle is not None:
        unique_bodies = {doc["content"] for doc in bundle["documents"] if doc["role"] == "specification"}
        manifest["bundle"] = {
            "documents": [{k: v for k, v in doc.items() if k not in {"content", "origin"}} for doc in bundle["documents"]],
            "relations": bundle.get("relations", []), "relation_status": "candidate_not_proven",
            "specification_body_bytes": sum(len(doc["content"].encode("utf-8")) for doc in bundle["documents"] if doc["role"] == "specification"),
            "unique_specification_body_bytes": sum(len(body.encode("utf-8")) for body in unique_bodies),
            "reference_body_bytes": sum(len(doc["content"].encode("utf-8")) for doc in bundle["documents"] if doc["role"] == "reference"),
        }
    temporary = manifest_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(manifest_path)
    audit_manifest(manifest)
    return manifest


def load_manifest(manifest: dict[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(manifest, dict):
        value = manifest
    else:
        value = json.loads(Path(manifest).read_text(encoding="utf-8"))
    if value.get("schema_version") not in {1, 2}:
        raise SpecError("Unsupported specification inventory version")
    return value


def _parse_jsonl(raw: bytes) -> Iterator[dict[str, Any]]:
    # JSONL uses LF record delimiters. Unicode line separators inside a JSON
    # string are valid source content, not additional records.
    for line in raw.decode("utf-8").split("\n"):
        if line.strip():
            yield json.loads(line)


def iter_sources(manifest: dict[str, Any] | str | Path, verify: bool = True) -> Iterator[dict[str, Any]]:
    data = load_manifest(manifest)
    raw = Path(data["source_records_path"]).read_bytes()
    if verify and _sha(raw) != data["sources_sha256"]:
        raise SpecIntegrityError("Source inventory digest mismatch")
    yield from _parse_jsonl(raw)


def load_source(manifest: dict[str, Any] | str | Path, source_id: str) -> dict[str, Any]:
    for record in iter_sources(manifest):
        if record["source_id"] == source_id:
            return record
    raise KeyError(f"Unknown source ID: {source_id}")


def audit_manifest(manifest: dict[str, Any] | str | Path) -> dict[str, Any]:
    """Prove inventory reconstruction, not semantic or verification completeness.

    Every call reads and hashes the current content. Only an identical manifest,
    snapshot and both inventories may reuse a previous successful reconstruction.
    Verification and parsing consume the same bytes, including on a cache miss.
    """
    data = load_manifest(manifest)
    manifest_bytes = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    # Freeze caller-owned dictionaries before validating and memoizing them.
    data = json.loads(manifest_bytes)
    snapshot = Path(data["snapshot_path"]).read_bytes()
    snapshot_hash = _sha(snapshot)
    if snapshot_hash != data["spec_sha256"] or len(snapshot) != data["spec_bytes"]:
        raise SpecIntegrityError("Original specification snapshot digest/size mismatch")
    leaf_bytes = Path(data["leaf_records_path"]).read_bytes()
    leaf_hash = _sha(leaf_bytes)
    if leaf_hash != data["leaves_sha256"]:
        raise SpecIntegrityError("Leaf inventory digest mismatch")
    source_bytes = Path(data["source_records_path"]).read_bytes()
    source_hash = _sha(source_bytes)
    if source_hash != data["sources_sha256"]:
        raise SpecIntegrityError("Source inventory digest mismatch")
    cache_key = (_sha(manifest_bytes), snapshot_hash, leaf_hash, source_hash)
    with _AUDIT_CACHE_LOCK:
        cached = _AUDIT_CACHE.get(cache_key)
        if cached is not None:
            _AUDIT_CACHE.move_to_end(cache_key)
            return dict(cached)
    sources = list(_parse_jsonl(source_bytes))
    leaves = list(_parse_jsonl(leaf_bytes))
    index = {source["source_id"]: source for source in sources}
    if len(index) != len(sources) or len(sources) != data["source_count"] or len(leaves) != data["leaf_count"]:
        raise SpecIntegrityError("Source or leaf counts disagree")
    seen: set[str] = set()
    for leaf in leaves:
        text_parts = []
        offset = 0
        for source_id in leaf["source_ids"]:
            source = index.get(source_id)
            if source is None or source_id in seen:
                raise SpecIntegrityError("Missing or multiply assigned source fragment")
            seen.add(source_id)
            text = source["text"]
            if source["pointer"] != leaf["pointer"] or source["start_char"] != offset or source["end_char"] != offset + len(text):
                raise SpecIntegrityError("Fragment location gap or overlap")
            if _sha(text.encode("utf-8")) != source["revision"] or source["spec_sha256"] != data["spec_sha256"]:
                raise SpecIntegrityError("Fragment content or baseline digest mismatch")
            text_parts.append(text)
            offset += len(text)
        reconstructed = "".join(text_parts)
        if len(reconstructed) != leaf["text_chars"] or _sha(reconstructed.encode("utf-8")) != leaf["value_sha256"]:
            raise SpecIntegrityError("Leaf cannot be reconstructed from its fragments")
    if len(seen) != len(sources):
        raise SpecIntegrityError("Unassigned source fragments")
    from .bundles import adjudication_dependencies, source_metadata, validate_bundle
    parsed = json.loads(snapshot.decode("utf-8-sig"))
    bundle = validate_bundle(parsed)
    if bundle is not None:
        # Cache checksums establish internal consistency, not document authority.
        # Re-derive labels from the hash-verified snapshot before relying on them
        # for routing, prompt metadata or local metadata-only task classification.
        expected_metadata = {pointer: {**metadata, **source_metadata(bundle, pointer)}
                             for pointer, _, _, metadata in _leaves(parsed)}
        for source in sources:
            if (source["pointer"] not in expected_metadata or
                    source.get("metadata") != expected_metadata[source["pointer"]]):
                raise SpecIntegrityError("Source metadata disagrees with specification snapshot")
    if data.get("schema_version") == 2:
        if data.get("partitioning") != PARTITIONING:
            raise SpecIntegrityError("Unsupported source partitioning strategy")
        expected_sources, expected_leaves, _ = _inventory(parsed, bundle, data["spec_sha256"])
        if sources != expected_sources or leaves != expected_leaves:
            raise SpecIntegrityError("Source structure disagrees with specification snapshot")
        expected_tasks = _extraction_tasks(bundle, expected_sources, data["spec_sha256"], data["manifest_path"])
        if data["extraction_tasks"] != expected_tasks:
            raise SpecIntegrityError("Source grouping, scope or adjudication routing is stale or altered")
    expected = adjudication_dependencies(bundle, sources)
    if expected and data.get("schema_version") == 1:
        routed = set()
        for task in data["extraction_tasks"]:
            owned = task["source_ids"]
            applicable = {source_id: expected[source_id] for source_id in owned if source_id in expected}
            payload = task["payload"]
            acceptance = payload.get("acceptance", {})
            dependencies = sorted({dependency for values in applicable.values() for dependency in values})
            if (payload["source_ids"] != owned or
                    acceptance.get("source_adjudication_sources", {}) != applicable or
                    payload.get("dependency_source_ids", []) != dependencies or
                    (applicable and acceptance.get("owned_source_ids") != owned)):
                raise SpecIntegrityError("Source adjudication routing is stale or altered; re-import into fresh source storage")
            routed.update(applicable)
        if routed != set(expected):
            raise SpecIntegrityError("Source adjudication targets are missing extraction tasks")
    report = {"source_inventory_complete": True, "semantic_review_complete": False,
              "leaf_count": len(leaves), "source_count": len(sources), "spec_bytes": len(snapshot)}
    with _AUDIT_CACHE_LOCK:
        _AUDIT_CACHE[cache_key] = dict(report)
        _AUDIT_CACHE.move_to_end(cache_key)
        while len(_AUDIT_CACHE) > _AUDIT_CACHE_LIMIT:
            _AUDIT_CACHE.popitem(last=False)
    return report
