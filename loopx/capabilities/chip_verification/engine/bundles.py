"""Explicit multi-document input format; never a replacement for project state.

Only this opt-in format distinguishes wrapper metadata from document bodies.
Arbitrary JSON continues through the lossless, fully interpreted inventory.
"""
from __future__ import annotations

import hashlib
import re
from collections import deque


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
    by_id = {doc["id"]: doc for doc in docs}
    edges = {doc["id"]: set() for doc in docs}
    for doc in docs:
        origin = doc["origin"]
        if origin.get("kind") != "source_adjudication":
            continue
        require(origin.get("authored") is True and doc["role"] == "specification",
                "source adjudication must be an explicit authored specification")
        require(origin.get("sha256") == hashlib.sha256(doc["content"].encode("utf-8")).hexdigest(),
                "source adjudication needs its content SHA")
        targets = origin.get("targets")
        require(isinstance(targets, list) and 0 < len(targets) <= 64,
                "source adjudication needs 1..64 anchored targets")
        identities = set()
        for target in targets:
            require(isinstance(target, dict) and set(target) == {"document_id", "sha256", "line_start", "line_end"},
                    "invalid source adjudication target fields")
            target_id = target["document_id"]
            require(isinstance(target_id, str) and target_id in by_id and target_id != doc["id"],
                    "source adjudication target is missing or self-referential")
            original = by_id[target_id]
            require(original["role"] == "specification", "source adjudication cannot promote reference material")
            require(target["sha256"] == hashlib.sha256(original["content"].encode("utf-8")).hexdigest(),
                    "source adjudication target SHA mismatch: " + target_id)
            start, end = target["line_start"], target["line_end"]
            require(type(start) is int and type(end) is int and 1 <= start <= end <= len(original["content"].splitlines()),
                    "source adjudication target line range is invalid")
            identity = (target_id, start, end)
            require(identity not in identities, "duplicate source adjudication target")
            identities.add(identity)
            edges[doc["id"]].add(target_id)
    # Dependencies may refine another authored adjudication, but never form a
    # cycle. A normal document relation does not confer adjudication authority.
    incoming = {identity: 0 for identity in edges}
    for targets in edges.values():
        for target_id in targets:
            incoming[target_id] += 1
    ready = deque(identity for identity, count in incoming.items() if count == 0)
    visited = 0
    while ready:
        identity = ready.popleft()
        visited += 1
        for target_id in edges[identity]:
            incoming[target_id] -= 1
            if incoming[target_id] == 0:
                ready.append(target_id)
    require(visited == len(edges), "source adjudication dependency cycle")
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
            if doc["origin"].get("kind") == "source_adjudication":
                result["source_adjudication"] = True
    return result


def adjudication_dependencies(bundle, records):
    """Route explicit anchored policy bodies to overlapping original fragments.

    Both original text and authored policy remain separately traceable sources.
    These edges demand context and citations, not a semantic verdict or waiver.
    """
    if bundle is None:
        return {}
    bodies = {}
    for record in records:
        if record["metadata"].get("source_role") != "bundle_metadata":
            bodies.setdefault(record["metadata"].get("document_id"), []).append(record)
    documents = {doc["id"]: doc for doc in bundle["documents"]}
    direct = {}
    for doc in bundle["documents"]:
        if doc["origin"].get("kind") != "source_adjudication":
            continue
        dependency_ids = {record["source_id"] for record in bodies.get(doc["id"], [])}
        for target in doc["origin"]["targets"]:
            lines = documents[target["document_id"]]["content"].splitlines(keepends=True)
            start = sum(len(line) for line in lines[:target["line_start"] - 1])
            end = start + sum(len(line) for line in lines[target["line_start"] - 1:target["line_end"]])
            for record in bodies.get(target["document_id"], []):
                if record["start_char"] < end and start < record["end_char"]:
                    direct.setdefault(record["source_id"], set()).update(dependency_ids)
    result = {}
    for source_id, dependencies in direct.items():
        reached, pending = set(), list(dependencies)
        while pending:
            dependency = pending.pop()
            if dependency not in reached:
                reached.add(dependency)
                pending.extend(direct.get(dependency, ()))
        result[source_id] = sorted(reached)
    return result
