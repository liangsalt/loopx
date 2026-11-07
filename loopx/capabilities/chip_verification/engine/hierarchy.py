"""Durable structural/functional views over source and task state.

Membership is explicit input classification, never proof of refinement. Candidate
cross-document relations cannot close a requirement or a simulation obligation.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json

from .spec import iter_sources, load_manifest
from .models import ConflictError, StateError


def _id(dimension, label):
    return "node_" + hashlib.sha256(json.dumps([dimension, label], ensure_ascii=False).encode()).hexdigest()[:24]


def focused_tasks(project, tasks, focus=None):
    """Select work by explicit design labels without hiding its prerequisites."""
    if not focus:
        return tasks
    dimension, separator, label = focus.partition(":")
    if not separator or dimension not in {"document", "layer", "module", "feature"} or not label:
        raise ValueError("Focus must be document:<id>, layer:<name>, module:<name> or feature:<name>")
    matching = set()
    for source in iter_sources(project.config["manifest_path"]):
        meta = source["metadata"]
        if meta.get("source_role") == "bundle_metadata":
            continue
        keys = {"document": ("document_id",), "layer": ("layer",),
                "module": ("module", "modules"), "feature": ("feature", "features")}[dimension]
        if any(label == meta.get(key) or isinstance(meta.get(key), list) and label in meta[key] for key in keys):
            matching.add(source["source_id"])
    if not matching:
        raise ValueError("Unknown design focus: " + focus)
    return [task for task in tasks if matching.intersection(task["payload"].get("source_ids", []))]


def _blocking_questions(tasks, issues):
    """A finished candidate can precede reconciliation; its questions already count."""
    categories = {"spec_blocker", "source_reference", "context_blocker"}
    relevant = {(i["task_id"], i["category"], i["description"]): i["status"]
                for i in issues if i["category"] in categories}
    open_keys = {key for key, status in relevant.items() if status == "open"}
    for task in tasks.values():
        result = task.get("result") or {}
        candidates = [("spec_blocker", q["question"]) for q in result.get("unresolved", []) if q["blocking"]]
        candidates += [("source_reference", r["reference"] + ": " + r["reason"]) for r in result.get("cross_references", [])]
        for req in result.get("requirements", []):
            if req["applicability"] == "uncertain":
                rid = "req_" + hashlib.sha256((task["id"] + ":" + req["local_id"]).encode()).hexdigest()[:20]
                candidates.append(("spec_blocker", "Unresolved requirement applicability " + rid + ": " + req["statement"]))
        for category, description in candidates:
            key = (task["id"], category, description)
            if relevant.get(key) != "resolved":
                open_keys.add(key)
    return sum(key[0] in tasks for key in open_keys)


def synchronize(project):
    project.assert_inputs_current()
    manifest = load_manifest(project.config["manifest_path"])
    frozen_baseline = project.store.baseline()
    baseline = frozen_baseline["id"]
    if frozen_baseline["spec_hash"] != manifest["spec_sha256"]:
        raise ConflictError("Baseline changed while loading the design-map sources")
    nodes, members = {}, set()
    for source in iter_sources(manifest):
        meta = source["metadata"]
        if meta.get("source_role") == "bundle_metadata":
            continue
        dimensions = {"document": [meta.get("document_id")], "layer": [meta.get("layer")],
                      "module": [meta.get("module"), *meta.get("modules", [])],
                      "feature": [meta.get("feature"), *meta.get("features", [])]}
        for dimension, labels in dimensions.items():
            for label in labels:
                if not label:
                    continue
                node_id = _id(dimension, label)
                nodes[node_id] = (dimension, label)
                members.add((node_id, source["source_id"]))
    relations = manifest.get("bundle", {}).get("relations", [])
    with project.store._db(write=True) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS design_nodes(
                baseline_id TEXT NOT NULL,id TEXT NOT NULL,dimension TEXT NOT NULL,label TEXT NOT NULL,
                PRIMARY KEY(baseline_id,id));
            CREATE TABLE IF NOT EXISTS design_members(
                baseline_id TEXT NOT NULL,node_id TEXT NOT NULL,source_id TEXT NOT NULL,
                PRIMARY KEY(baseline_id,node_id,source_id));
            CREATE TABLE IF NOT EXISTS design_relations(
                baseline_id TEXT NOT NULL,id TEXT NOT NULL,source_node TEXT NOT NULL,target_node TEXT NOT NULL,
                kind TEXT NOT NULL,rationale TEXT NOT NULL,claim TEXT NOT NULL,
                PRIMARY KEY(baseline_id,id));
        """)
        # executescript commits SQLite's transaction; acquire the writer lock
        # again before replacing this derived view as one atomic generation.
        db.execute("BEGIN IMMEDIATE")
        if project.store._current(db) != baseline:
            raise ConflictError("Baseline changed during design-map projection")
        if db.execute("SELECT spec_hash FROM baselines WHERE id=?", (baseline,)).fetchone()[0] != manifest["spec_sha256"]:
            raise ConflictError("Design-map source hash differs from its baseline")
        for table in ("design_members", "design_relations", "design_nodes"):
            db.execute(f"DELETE FROM {table} WHERE baseline_id=?", (baseline,))
        db.executemany("INSERT INTO design_nodes VALUES(?,?,?,?)", [(baseline, key, *value) for key, value in nodes.items()])
        db.executemany("INSERT INTO design_members VALUES(?,?,?)", [(baseline, *item) for item in sorted(members)])
        db.executemany("INSERT INTO design_relations VALUES(?,?,?,?,?,?,?)", [
            (baseline, rel["id"], _id("document", rel["source"]), _id("document", rel["target"]),
             rel["kind"], rel["rationale"], "candidate_not_proven") for rel in relations])
    return {"nodes": len(nodes), "memberships": len(members), "candidate_relations": len(relations),
            "baseline_id": baseline, "manifest_path": manifest["manifest_path"]}


def design_map(project, *, dimension=None, limit=100, refresh=True):
    """Read the stored design projection; refresh only for authorized mutations."""
    if dimension not in {None, "document", "module", "feature", "layer"}:
        raise ValueError("Unknown map dimension")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Map limit must be 1..1000")
    if refresh:
        project.store.verify_artifacts()
        project.reconcile()
        synchronize(project)
    config = project.config
    manifest_path = config["manifest_path"]
    with project.store._db() as db:
        db.execute("BEGIN")  # all projections below use one SQLite read snapshot
        baseline = project.store._current(db)
        if db.execute("SELECT spec_hash FROM baselines WHERE id=?", (baseline,)).fetchone()[0] != config["spec_sha256"]:
            raise ConflictError("Baseline changed during design-map read; retry the bounded query")
        required_tables = {"design_nodes", "design_members", "design_relations"}
        existing_tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required_tables <= existing_tables:
            raise StateError("No stored design map exists; import a specification or refresh the project first")
        counts = {
            "nodes": db.execute("SELECT COUNT(*) FROM design_nodes WHERE baseline_id=?", (baseline,)).fetchone()[0],
            "memberships": db.execute("SELECT COUNT(*) FROM design_members WHERE baseline_id=?", (baseline,)).fetchone()[0],
            "candidate_relations": db.execute("SELECT COUNT(*) FROM design_relations WHERE baseline_id=?", (baseline,)).fetchone()[0],
        }
        nodes = [dict(row) for row in db.execute("SELECT * FROM design_nodes WHERE baseline_id=? ORDER BY dimension,label", (baseline,))]
        memberships = list(db.execute("SELECT node_id,source_id FROM design_members WHERE baseline_id=?", (baseline,)))
        relations = [dict(row) for row in db.execute("SELECT * FROM design_relations WHERE baseline_id=? ORDER BY id", (baseline,))]
        issues = [dict(row) for row in db.execute("SELECT * FROM issues WHERE baseline_id=?", (baseline,))]
        tasks = [project.store._task(db, row[0]) for row in db.execute("SELECT id FROM tasks WHERE baseline_id=? ORDER BY created,id", (baseline,))]
        requirements = [project.store._decode(row, ("source_ids", "metadata")) for row in db.execute("SELECT * FROM requirements WHERE baseline_id=?", (baseline,))]
    sources = defaultdict(set)
    for node, source in memberships:
        sources[node].add(source)
    source_tasks = defaultdict(list)
    for task in tasks:
        if task["kind"] == "model":
            for sid in task["payload"].get("source_ids", []):
                source_tasks[sid].append(task)
    summaries, index = [], {}
    for node in nodes:
        ids = sources[node["id"]]
        local_tasks = {t["id"]: t for sid in ids for t in source_tasks[sid]}
        requirement_ids = {r["id"] for r in requirements if ids.intersection(r["source_ids"])}
        plan_tasks = [t for t in tasks if t["kind"] == "plan" and any(r["id"] in requirement_ids for r in t["payload"].get("requirements", []))]
        source_tasks_complete = bool(local_tasks) and all(t["status"] == "done" for t in local_tasks.values())
        blocking_questions = _blocking_questions(local_tasks, issues)
        summary = {"id": node["id"], "dimension": node["dimension"], "label": node["label"],
                   "source_fragments": len(ids), "interpretation_states": dict(Counter(t["status"] for t in local_tasks.values())),
                   "candidate_requirements": len(requirement_ids), "planning_states": dict(Counter(t["status"] for t in plan_tasks)),
                   "source_tasks_complete": source_tasks_complete, "blocking_source_questions": blocking_questions,
                   "interpretation_complete": source_tasks_complete and not blocking_questions,
                   "verification_complete": False}
        summaries.append(summary)
        index[node["id"]] = summary
    link_summaries = []
    for relation in relations:
        left, right = index[relation["source_node"]], index[relation["target_node"]]
        state = "awaiting_source_interpretation"
        if left["source_tasks_complete"] and right["source_tasks_complete"]:
            state = "awaiting_source_clarification" if left["blocking_source_questions"] or right["blocking_source_questions"] else "awaiting_cross_layer_review"
        link_summaries.append({"id": relation["id"], "source": left["label"], "target": right["label"],
                               "kind": relation["kind"], "claim": relation["claim"],
                               "state": state})
    selected = [s for s in summaries if dimension is None or s["dimension"] == dimension]
    manifest = load_manifest(manifest_path)
    if manifest["spec_sha256"] != config["spec_sha256"]:
        raise ConflictError("Stored design map and source manifest name different baselines")
    if project.store.baseline()["id"] != baseline:
        raise ConflictError("Baseline changed during design-map read; retry the bounded query")
    return {"baseline_id": baseline, **counts, "dimension": dimension,
            "specification_body_bytes": manifest.get("bundle", {}).get("specification_body_bytes"),
            "unique_specification_body_bytes": manifest.get("bundle", {}).get("unique_specification_body_bytes"),
            "reference_body_bytes": manifest.get("bundle", {}).get("reference_body_bytes"),
            "items": selected[:limit], "omitted_nodes": max(0, len(selected) - limit),
            "relations": link_summaries[:limit], "omitted_relations": max(0, len(link_summaries) - limit),
            "signoff": False, "claim": "Input hierarchy and candidate relations; local completion does not prove system behavior."}
