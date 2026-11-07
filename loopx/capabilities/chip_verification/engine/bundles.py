"""Explicit multi-document input format; never a replacement for project state.

Only this opt-in format distinguishes wrapper metadata from document bodies.
Arbitrary JSON continues through the lossless, fully interpreted inventory.
"""
from __future__ import annotations

import hashlib
import re


LAYERS = {"system", "architecture", "microarchitecture", "module", "verification", "software"}


def validate_bundle(value):
    if not isinstance(value, dict) or value.get("schema") != "dvflow.spec_bundle":
        return None
    from .spec import SpecError
    def require(condition, message):
        if not condition:
            raise SpecError("Invalid dvflow.spec_bundle: " + message)
    require(value.get("schema_version") == 1 and type(value.get("schema_version")) is int, "unsupported version")
    require(set(value) <= {"schema", "schema_version", "title", "documents", "relations"}, "unknown root field; put behavior in document content")
    require(isinstance(value.get("title"), str) and 0 < len(value["title"]) <= 200, "title required, maximum 200 characters")
    docs = value.get("documents")
    require(isinstance(docs, list) and bool(docs), "nonempty documents required")
    ids = set()
    for doc in docs:
        require(isinstance(doc, dict), "document must be an object")
        require(set(doc) <= {"id", "title", "layer", "module", "modules", "features", "role", "origin", "content"}, "unknown document field")
        require(isinstance(doc.get("id"), str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", doc["id"]), "invalid document id")
        require(doc["id"] not in ids, "duplicate document id")
        ids.add(doc["id"])
        require(isinstance(doc.get("title"), str) and 0 < len(doc["title"]) <= 200, "document title required")
        require(doc.get("layer") in LAYERS, "unknown design layer")
        require(doc.get("role") in {"specification", "reference"}, "document role required")
        require(isinstance(doc.get("module"), str) and 0 < len(doc["module"]) <= 160, "module label required")
        for key in ("features", "modules"):
            items = doc.get(key, [])
            require(isinstance(items, list) and len(items) <= 32 and all(isinstance(x, str) and 0 < len(x) <= 160 for x in items), "invalid " + key)
        require(isinstance(doc.get("origin"), dict), "origin object required")
        require(isinstance(doc.get("content"), str) and bool(doc["content"].strip()), "nonempty original content required")
        sha = doc["origin"].get("sha256")
        if sha is not None:
            require(sha == hashlib.sha256(doc["content"].encode("utf-8")).hexdigest(), "document content SHA mismatch: " + doc["id"])
    relations = value.get("relations", [])
    require(isinstance(relations, list), "relations must be an array")
    relation_ids = set()
    for rel in relations:
        require(isinstance(rel, dict) and set(rel) == {"id", "source", "target", "kind", "rationale"}, "invalid relation fields")
        require(isinstance(rel["id"], str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", rel["id"]), "invalid relation id")
        require(rel["id"] not in relation_ids, "duplicate relation id")
        relation_ids.add(rel["id"])
        require(rel["source"] in ids and rel["target"] in ids and rel["source"] != rel["target"], "relation references unknown or identical documents")
        require(rel["kind"] in {"refines", "integrates", "verifies", "related"}, "unknown relation kind")
        require(isinstance(rel["rationale"], str) and bool(rel["rationale"].strip()), "relation rationale required")
    return value


def source_metadata(bundle, pointer):
    """Classify only the schema wrapper, never the original document body."""
    result = {"source_role": "bundle_metadata"}
    match = re.match(r"^/documents/(\d+)(?:/|$)", pointer)
    if match:
        doc = bundle["documents"][int(match[1])]
        result.update(document_id=doc["id"], layer=doc["layer"], module=doc["module"],
                      modules=doc.get("modules", [doc["module"]]), features=doc.get("features", []),
                      title=doc["title"], document_role=doc["role"])
        if pointer == f"/documents/{match[1]}/content":
            result["source_role"] = doc["role"]
    return result
