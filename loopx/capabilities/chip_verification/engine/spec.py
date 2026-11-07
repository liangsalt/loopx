"""Lossless source inventory for JSON specifications, independent of agent sessions.

JSON is parsed locally. Only bounded source fragments are sent to a model later;
inventorying a document is deliberately not a claim that its semantics are known.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterator


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


def _segments(text: str, maximum: int) -> Iterator[tuple[int, int, str]]:
    if not text:
        yield 0, 0, ""
        return
    start = 0
    while start < len(text):
        end = min(start + maximum, len(text))
        if end < len(text):
            # Prefer paragraph then line boundaries. Keep every separator and
            # fall back to a hard boundary for an indivisible giant field.
            floor = start + max(1, maximum // 3)
            for separator in ("\n\n", "\n", ". ", "; ", " "):
                boundary = text.rfind(separator, floor, end)
                if boundary >= floor:
                    end = boundary + len(separator)
                    break
        yield start, end, text[start:end]
        start = end


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(_json(record) + "\n")


def ingest_spec(path: str | Path, output_dir: str | Path, max_chunk_chars: int = 12000) -> dict[str, Any]:
    """Create an immutable raw snapshot, complete leaf inventory and bounded tasks.

    ``output_dir`` is a storage parent, normally ``project/.loopx/chip-verification/specs``.
    Paths in the returned manifest are absolute. Extraction task payloads contain
    references only; no source is declared semantically reviewed by this parser.
    """
    if isinstance(max_chunk_chars, bool) or max_chunk_chars < 32:
        raise ValueError("max_chunk_chars must be an integer of at least 32")
    source_path = Path(path).resolve()
    raw = source_path.read_bytes()
    try:
        parsed = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise SpecError(f"Cannot parse UTF-8 JSON specification: {error}") from error
    from .bundles import validate_bundle, source_metadata
    bundle = validate_bundle(parsed)
    digest = _sha(raw)
    destination = Path(output_dir).resolve() / f"{digest[:20]}-v1-c{max_chunk_chars}"
    destination.mkdir(parents=True, exist_ok=True)
    manifest_path = destination / "manifest.json"
    if manifest_path.exists():
        existing = load_manifest(manifest_path)
        audit_manifest(existing)
        return existing

    records: list[dict[str, Any]] = []
    leaves: list[dict[str, Any]] = []
    total_chars = 0
    try:
        leaf_iterator = _leaves(parsed)
        for pointer, kind, text, metadata in leaf_iterator:
            if bundle is not None:
                metadata = {**metadata, **source_metadata(bundle, pointer)}
            leaf_id = "leaf_" + _sha(pointer.encode("utf-8"))[:24]
            fragment_ids = []
            first_record = len(records)
            for start, end, fragment in _segments(text, max_chunk_chars):
                # Identity follows location. Content has a separate revision.
                source_id = "src_" + _sha(_json([pointer, start, end]).encode("utf-8"))[:24]
                fragment_ids.append(source_id)
                records.append({
                    "source_id": source_id,
                    "leaf_id": leaf_id,
                    "pointer": pointer,
                    "start_char": start,
                    "end_char": end,
                    "value_kind": kind,
                    "text": fragment,
                    "revision": _sha(fragment.encode("utf-8")),
                    "spec_sha256": digest,
                    "metadata": metadata,
                    "semantic_status": "unreviewed",
                    "boundary": "complete_value" if start == 0 and end == len(text) else "fragment_requires_neighbors",
                })
            for position, record in enumerate(records[first_record:]):
                record["previous_source_id"] = fragment_ids[position - 1] if position else None
                record["next_source_id"] = fragment_ids[position + 1] if position + 1 < len(fragment_ids) else None
            total_chars += len(text)
            leaves.append({
                "leaf_id": leaf_id, "pointer": pointer, "value_kind": kind,
                "text_chars": len(text), "value_sha256": _sha(text.encode("utf-8")),
                "source_ids": fragment_ids, "disposition": "awaiting_semantic_review",
            })
    except (RecursionError, UnicodeEncodeError) as error:
        raise SpecError(f"Specification cannot be inventoried losslessly: {error}") from error

    snapshot = destination / "spec.original.json"
    sources_path = destination / "sources.jsonl"
    leaves_path = destination / "leaves.jsonl"
    snapshot.write_bytes(raw)
    _write_jsonl(sources_path, records)
    _write_jsonl(leaves_path, leaves)

    groups: list[list[dict[str, Any]]] = []
    batch: list[dict[str, Any]] = []
    batch_chars = 0
    metadata_records = []
    for record in records:
        if record["metadata"].get("source_role") == "bundle_metadata":
            metadata_records.append(record)
            continue
        # Account for metadata overhead while grouping. A single maximum-sized
        # source may exceed this grouping target, but never the chunk bound.
        cost = len(_json(record))
        if batch and (batch_chars + cost > max_chunk_chars or
                      record["metadata"].get("document_id") != batch[0]["metadata"].get("document_id")):
            groups.append(batch)
            batch, batch_chars = [], 0
        batch.append(record)
        batch_chars += cost
    if batch:
        groups.append(batch)
    tasks = []
    for index, group in enumerate(groups):
        ids = [record["source_id"] for record in group]
        tasks.append({
            "task_id": f"extract_{digest[:12]}_{index:05d}",
            "kind": "model",
            "title": f"Interpret specification source batch {index + 1}/{len(groups)}",
            "source_ids": ids,
            "spec_sha256": digest,
            "estimated_source_chars": sum(len(record["text"]) for record in group),
            "payload": {
                "source_ids": ids, "spec_sha256": digest,
                "manifest_path": str(manifest_path),
                "instruction": "Extract candidate requirements, definitions, references and ambiguities. Classify every supplied source; preserve conditions and exceptions. Unresolved fragment neighbors must be recorded, not guessed.",
            },
        })
    if metadata_records:
        ids = [record["source_id"] for record in metadata_records]
        tasks.append({
            "task_id": f"metadata_{digest[:12]}", "kind": "model",
            "title": "Inventory explicit bundle labels, provenance and candidate relation metadata",
            "source_ids": ids, "spec_sha256": digest, "estimated_source_chars": 0,
            "payload": {"source_ids": ids, "spec_sha256": digest, "manifest_path": str(manifest_path),
                        "execution": "local_bundle_metadata"},
        })
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "spec_path": str(source_path), "spec_sha256": digest, "spec_bytes": len(raw),
        "snapshot_path": str(snapshot), "manifest_path": str(manifest_path),
        "source_records_path": str(sources_path), "leaf_records_path": str(leaves_path),
        "sources_sha256": _sha(sources_path.read_bytes()), "leaves_sha256": _sha(leaves_path.read_bytes()),
        "source_count": len(records), "leaf_count": len(leaves), "source_text_chars": total_chars,
        "max_chunk_chars": max_chunk_chars,
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
        return manifest
    value = json.loads(Path(manifest).read_text(encoding="utf-8"))
    if value.get("schema_version") != 1:
        raise SpecError("Unsupported specification inventory version")
    return value


def _read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def iter_sources(manifest: dict[str, Any] | str | Path, verify: bool = True) -> Iterator[dict[str, Any]]:
    data = load_manifest(manifest)
    path = Path(data["source_records_path"])
    if verify and _sha(path.read_bytes()) != data["sources_sha256"]:
        raise SpecIntegrityError("Source inventory digest mismatch")
    yield from _read_jsonl(path)


def load_source(manifest: dict[str, Any] | str | Path, source_id: str) -> dict[str, Any]:
    for record in iter_sources(manifest):
        if record["source_id"] == source_id:
            return record
    raise KeyError(f"Unknown source ID: {source_id}")


def audit_manifest(manifest: dict[str, Any] | str | Path) -> dict[str, Any]:
    """Prove inventory reconstruction, not semantic or verification completeness."""
    data = load_manifest(manifest)
    snapshot = Path(data["snapshot_path"]).read_bytes()
    if _sha(snapshot) != data["spec_sha256"] or len(snapshot) != data["spec_bytes"]:
        raise SpecIntegrityError("Original specification snapshot digest/size mismatch")
    leaves_path = Path(data["leaf_records_path"])
    if _sha(leaves_path.read_bytes()) != data["leaves_sha256"]:
        raise SpecIntegrityError("Leaf inventory digest mismatch")
    sources = list(iter_sources(data))
    leaves = list(_read_jsonl(leaves_path))
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
    return {"source_inventory_complete": True, "semantic_review_complete": False,
            "leaf_count": len(leaves), "source_count": len(sources), "spec_bytes": len(snapshot)}
