"""Build one semantic task view from durable references, never whole sessions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .spec import iter_sources, load_manifest


class ContextBudgetExceeded(ValueError):
    """Legacy import compatibility only; semantic context builders never raise it."""

    def __init__(self, required_chars: int, max_chars: int, required_tokens: int,
                 max_tokens: int | None, source_ids: list[str]):
        self.required_chars = required_chars
        self.max_chars = max_chars
        self.required_tokens = required_tokens
        self.max_tokens = max_tokens
        self.source_ids = source_ids
        super().__init__(f"Required context does not fit: {required_chars}/{max_chars} characters, "
                         f"{required_tokens}/{max_tokens if max_tokens is not None else 'unlimited'} "
                         "conservative byte-token budget. Split the task or resolve a smaller accepted contract; no mandatory source was dropped.")


def render_context(context: dict[str, Any]) -> str:
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def build_context(manifest: dict[str, Any] | str | Path, task: dict[str, Any],
                  max_chars: int | None = None, max_tokens: int | None = None) -> dict[str, Any]:
    """Materialize only task sources and explicit mandatory source dependencies.

    Legacy length arguments are accepted but do not truncate, split or reject
    a semantic unit. Source size is an observation, not a task boundary. External
    artifact references are retained as references, not silently read.
    """
    data = load_manifest(manifest)
    payload = task.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("Task payload must be an object")
    baseline = task.get("spec_sha256", payload.get("spec_sha256"))
    if baseline is not None and baseline != data["spec_sha256"]:
        raise ValueError("Task refers to a different specification baseline")
    raw_ids = task.get("source_ids", payload.get("source_ids", []))
    dependency_ids = task.get("dependency_source_ids", payload.get("dependency_source_ids", []))
    if not isinstance(raw_ids, list) or not isinstance(dependency_ids, list) or not all(isinstance(item, str) for item in raw_ids + dependency_ids):
        raise ValueError("Source references must be lists of source IDs")
    requested = list(dict.fromkeys(raw_ids + dependency_ids))
    wanted = set(requested)
    index = {source["source_id"]: source for source in iter_sources(data) if source["source_id"] in wanted}
    missing = wanted - index.keys()
    if missing:
        raise KeyError("Unknown mandatory source IDs: " + ", ".join(sorted(missing)))
    contract = {}
    for key in ("instruction", "acceptance", "semantic_scope", "partitioning", "output_contract", "dependency_refs", "artifact_refs", "known_issues", "shared_contracts"):
        if key in task:
            contract[key] = task[key]
        elif key in payload:
            contract[key] = payload[key]
    sources = []
    for source_id in requested:
        source = index[source_id]
        sources.append({key: source[key] for key in (
            "source_id", "pointer", "start_char", "end_char", "value_kind", "text", "revision", "metadata", "boundary",
            "previous_source_id", "next_source_id"
        )})
        if "structure" in source:
            sources[-1]["structure"] = source["structure"]
    pack = {
        "schema_version": 1,
        "task_id": task.get("task_id", task.get("id")),
        "kind": task.get("kind", "model"),
        "title": task.get("title", ""),
        "spec_sha256": data["spec_sha256"],
        "contract": contract,
        "sources": sources,
        "retrieval": {"manifest_path": data["manifest_path"], "further_reading_requires_source_id": True},
        "budget": {"max_chars": None, "max_tokens": None},
        "measurements": {"source_chars": sum(len(source["text"]) for source in sources),
                         "source_utf8_bytes": sum(len(source["text"].encode("utf-8")) for source in sources)},
    }
    pack["context_id"] = "ctx_" + hashlib.sha256(render_context(pack).encode("utf-8")).hexdigest()
    return pack
