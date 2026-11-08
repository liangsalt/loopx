"""Semantic boundaries and version migration, independent of prose length."""
from __future__ import annotations

from collections import OrderedDict
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from loopx.capabilities.chip_verification.engine.context import build_context
from loopx.capabilities.chip_verification.engine.models import ConflictError
from loopx.capabilities.chip_verification.engine.source_structure import PARTITIONING
from loopx.capabilities.chip_verification.engine import spec as spec_module
from loopx.capabilities.chip_verification.engine.spec import audit_manifest, ingest_spec, iter_sources, SpecIntegrityError
from loopx.capabilities.chip_verification.engine.workflow import DVProject, save_json


def imported(root, value, length=1):
    path = root / "spec.json"
    save_json(path, value)
    return ingest_spec(path, root / "sources", max_chunk_chars=length)


def boundaries(manifest):
    sources = {s["source_id"]: s for s in iter_sources(manifest)}
    return [(t["payload"]["semantic_scope"], [sources[s]["pointer"] for s in t["source_ids"]])
            for t in manifest["extraction_tasks"] if t["payload"].get("execution") != "local_bundle_metadata"]


def test_module_id_description_does_not_hide_functions_or_atomic_requirements(tmp_path):
    value = {"modules": [{"id": "fifo", "description": "Shared FIFO assumptions.", "functions": [
        {"function": "enqueue", "requirements": [{"id": "R1", "description": "Accept once.",
            "conditions": [{"input": "valid", "expected": "one write"}], "exceptions": ["reset"]}]},
        {"function": "dequeue", "requirements": [{"req_id": "R2", "description": "Preserve order."}]}]}]}
    manifest = imported(tmp_path, value)
    source = {s["source_id"]: s for s in iter_sources(manifest)}
    tasks = manifest["extraction_tasks"]
    units = {t["payload"]["semantic_scope"]["unit"]: t for t in tasks}
    first = units["object:/modules/0/functions/0/requirements/0"]
    owned = {source[s]["pointer"] for s in first["source_ids"]}
    assert "/modules/0/functions/0/requirements/0/conditions/0/expected" in owned
    assert "/modules/0/functions/0/requirements/0/exceptions/0" in owned
    assert "object:/modules/0/functions/1/requirements/0" in units
    assert all("/functions/1/" not in pointer for pointer in owned)
    pack = build_context(manifest, first, max_chars=1)
    assert any(s["text"] == "Shared FIFO assumptions." for s in pack["sources"])
    assert any(s["text"] == "enqueue" for s in pack["sources"])
    assert all(s["text"] != "dequeue" for s in pack["sources"])


def test_wording_length_and_legacy_limits_do_not_change_structural_boundaries(tmp_path):
    value = {"modules": [{"module": "a", "feature": "reset", "description": "Reset clears."},
                         {"module": "b", "feature": "traffic", "description": "Transfers arrive."}]}
    first = imported(tmp_path, value, 32)
    repeated = ingest_spec(tmp_path / "spec.json", tmp_path / "sources", max_chunk_chars=500000)
    assert first == repeated
    value["modules"][0]["description"] += "Complete behavioral detail. " * 10000
    second = imported(tmp_path, value, 1)
    assert boundaries(first) == boundaries(second)
    assert any(len(s["text"]) > 24000 for s in iter_sources(second))
    for task in second["extraction_tasks"]:
        assert build_context(second, task, max_chars=1, max_tokens=1) == build_context(second, task)


def test_markdown_sections_keep_fences_tables_and_parent_context(tmp_path):
    text = ("Shared preface.\n# Interface\nParent constraints.\n## Reset\n"
            "```systemverilog\n# Not a section\n```\n"
            "| field | value |\n| --- | --- |\n| mode | # not a heading |\n"
            "Reset behavior.\n## Transfer\n" + "Atomic transfer clause. " * 2000)
    manifest = imported(tmp_path, {"module": "fifo", "text": text})
    records = [s for s in iter_sources(manifest) if s["pointer"] == "/text"]
    assert "".join(s["text"] for s in records) == text
    assert len(records) == 4
    assert [r["structure"]["section_path"] for r in records] == [[], ["Interface"], ["Interface", "Reset"], ["Interface", "Transfer"]]
    reset = next(t for t in manifest["extraction_tasks"] if t["payload"]["semantic_scope"]["section_path"] == ["Interface", "Reset"])
    pack = build_context(manifest, reset)
    combined = "".join(s["text"] for s in pack["sources"])
    assert "Shared preface." in combined and "Parent constraints." in combined
    assert "Atomic transfer clause." not in combined
    assert "```systemverilog\n# Not a section\n```" in combined
    assert "| field | value |\n| --- | --- |\n| mode | # not a heading |" in combined


def test_asciidoc_and_setext_use_declared_headings_only(tmp_path):
    text = "== CSR\nCommon rule.\n[source,asm]\n----\n=== Not a heading\n----\n=== STATUS\n|===\n| name | meaning\n== Not a heading\n|===\n"
    manifest = imported(tmp_path, {"text": text})
    records = list(iter_sources(manifest))
    assert [r["structure"]["section_path"] for r in records] == [["CSR"], ["CSR", "STATUS"]]
    assert "".join(r["text"] for r in records) == text
    other = imported(tmp_path, {"text": "Registers\n=========\nDescription.\nSTATUS\n------\nRead behavior.\n"})
    assert [r["structure"]["section_path"] for r in iter_sources(other)] == [["Registers"], ["Registers", "STATUS"]]


def test_markdown_code_example_cannot_change_heading_syntax(tmp_path):
    text = "# Module A\ntext\n```asciidoc\n= example\n```\n\n----\n\n# Module B\ntext\n"
    manifest = imported(tmp_path, {"text": text})
    records = list(iter_sources(manifest))
    assert [record["structure"]["section_path"] for record in records] == [["Module A"], ["Module B"]]
    assert "".join(record["text"] for record in records) == text
    assert "```asciidoc\n= example\n```" in records[0]["text"]


def test_rehashed_source_structure_and_task_ownership_cannot_drift(tmp_path):
    manifest = imported(tmp_path, {"modules": [{"module": "a", "description": "A"}, {"module": "b", "description": "B"}]})
    records = list(iter_sources(manifest))
    bad = copy.deepcopy(manifest)
    changed = copy.deepcopy(records)
    changed[0]["structure"]["unit"] = "object:invented"
    data = "".join(json.dumps(s) + "\n" for s in changed).encode()
    path = tmp_path / "tampered.jsonl"
    path.write_bytes(data)
    bad["source_records_path"], bad["sources_sha256"] = str(path), hashlib.sha256(data).hexdigest()
    with pytest.raises(SpecIntegrityError, match="structure disagrees"):
        audit_manifest(bad)
    for change in ("label", "owned"):
        bad = copy.deepcopy(manifest)
        task = bad["extraction_tasks"][0]
        if change == "label":
            task["payload"]["semantic_scope"]["section_path"] = ["invented"]
        else:
            task["source_ids"] = bad["extraction_tasks"][1]["source_ids"]
            task["payload"]["source_ids"] = task["source_ids"]
        with pytest.raises(SpecIntegrityError, match="grouping, scope"):
            audit_manifest(bad)


def test_same_spec_import_migrates_old_partition_and_fences_live_attempts(tmp_path):
    raw = {"behavior": "Accepted words preserve order."}
    manifest = imported(tmp_path, raw)
    legacy = copy.deepcopy(manifest)
    legacy["schema_version"], legacy["max_chunk_chars"] = 1, 800
    legacy.pop("partitioning")
    legacy["manifest_path"] = str(tmp_path / "legacy-manifest.json")
    save_json(Path(legacy["manifest_path"]), legacy)
    assert audit_manifest(legacy)["source_inventory_complete"]
    legacy_before = Path(legacy["manifest_path"]).read_bytes()
    project = DVProject(tmp_path)
    old = project.store.initialize("FIFO", tmp_path / "spec.json", legacy["spec_sha256"], legacy["spec_bytes"])
    save_json(project.config_path, {"name": "FIFO", "scope": "", "manifest_path": legacy["manifest_path"],
                                   "spec_sha256": legacy["spec_sha256"], "context_chars": 6000})
    source = next(iter_sources(legacy))
    project.store.add_source(source["pointer"], source["text"], source["metadata"], source_id=source["source_id"])
    payload = {"source_ids": [source["source_id"]], "scope": "", "spec_sha256": legacy["spec_sha256"]}
    project.store.add_task("model", "Legacy accepted extraction", payload, task_id="legacy-model")
    attempt = project.store.claim("legacy-model", "old-session")
    result = {"requirements": [], "dispositions": [{"source_id": source["source_id"], "classification": "context", "reason": "Fixture only"}],
              "cross_references": [], "unresolved": []}
    artifact = tmp_path / "old-response.json"
    save_json(artifact, result)
    project.store.finish("legacy-model", attempt["id"], result, artifacts=[artifact], validator=project._validate_model)
    project.store.add_task("model", "Legacy active extraction", payload, task_id="legacy-live")
    live = project.store.claim("legacy-live", "old-live-session")
    imported_new = project.import_spec(tmp_path / "spec.json", name="FIFO", chunk_chars=1, context_chars=1)
    new = imported_new["baseline"]
    assert new["id"] != old["id"] and new["spec_hash"] == old["spec_hash"]
    assert project.config["partitioning"] == PARTITIONING
    assert project.config["context_chars"] is None and project.config["host_context_chars"] is None
    assert project.store.get_task("legacy-model")["status"] == "stale"
    assert project.store.get_task("legacy-model")["result"] == result
    assert artifact.read_bytes() and Path(legacy["manifest_path"]).read_bytes() == legacy_before
    assert all(t["id"] not in {"legacy-model", "legacy-live"} for t in project.store.list_tasks())
    with pytest.raises(ConflictError):
        project.store.finish("legacy-live", live["id"], result, artifacts=[artifact], validator=project._validate_model)
    before = project.store.list_tasks()
    project.import_spec(tmp_path / "spec.json", name="FIFO", chunk_chars=300000, context_chars=10)
    assert project.store.baseline() == new and project.store.list_tasks() == before
    assert audit_manifest(legacy)["source_inventory_complete"]


def test_successful_audit_cache_reuses_only_complete_content_and_copies_report(tmp_path, monkeypatch):
    manifest = imported(tmp_path, {"module": "fifo", "text": "Required behavior."})
    monkeypatch.setattr(spec_module, "_AUDIT_CACHE", OrderedDict())
    original_inventory, calls = spec_module._inventory, []

    def track_inventory(*args):
        calls.append(True)
        return original_inventory(*args)

    monkeypatch.setattr(spec_module, "_inventory", track_inventory)
    first = audit_manifest(manifest)
    second = audit_manifest(manifest["manifest_path"])
    assert first == second and len(calls) == 1
    first["source_inventory_complete"] = False
    assert audit_manifest(manifest)["source_inventory_complete"]
    assert len(calls) == 1

    # Changing any manifest field invalidates reuse, even if it is merely a
    # timestamp and source reconstruction will ultimately succeed again.
    changed = copy.deepcopy(manifest)
    changed["created_at"] = "different metadata"
    audit_manifest(changed)
    assert len(calls) == 2


@pytest.mark.parametrize("component", ["source", "leaf", "snapshot", "manifest"])
def test_warm_audit_cache_rejects_same_path_tampering_with_refreshed_hashes(tmp_path, component):
    manifest = imported(tmp_path, {"module": "fifo", "text": "Required behavior."})
    assert audit_manifest(manifest)["source_inventory_complete"]
    bad = copy.deepcopy(manifest)
    if component == "manifest":
        bad["extraction_tasks"][0]["payload"]["semantic_scope"]["section_path"] = ["invented"]
    else:
        path_key, hash_key = {"source": ("source_records_path", "sources_sha256"),
                              "leaf": ("leaf_records_path", "leaves_sha256"),
                              "snapshot": ("snapshot_path", "spec_sha256")}[component]
        path = Path(bad[path_key])
        stat = path.stat()
        if component == "snapshot":
            value = json.loads(path.read_bytes())
            value["text"] = "Invented behavior."
            raw = json.dumps(value).encode()
            bad["spec_bytes"] = len(raw)
        else:
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            if component == "source":
                records[0]["structure"]["unit"] = "object:invented"
            else:
                records[0]["pointer"] = "/invented"
            raw = "".join(json.dumps(record) + "\n" for record in records).encode()
        path.write_bytes(raw)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        bad[hash_key] = hashlib.sha256(raw).hexdigest()
    for _ in range(2):
        with pytest.raises(SpecIntegrityError):
            audit_manifest(bad)


def test_audit_cache_is_bounded_and_retains_no_source_bodies(tmp_path, monkeypatch):
    manifest = imported(tmp_path, {"text": "Complete potentially very large source body."})
    monkeypatch.setattr(spec_module, "_AUDIT_CACHE", OrderedDict())
    monkeypatch.setattr(spec_module, "_AUDIT_CACHE_LIMIT", 2)
    for index in range(4):
        changed = copy.deepcopy(manifest)
        changed["created_at"] = str(index)
        audit_manifest(changed)
    assert len(spec_module._AUDIT_CACHE) == 2
    for key, report in spec_module._AUDIT_CACHE.items():
        assert len(key) == 4 and all(len(digest) == 64 for digest in key)
        assert all(isinstance(value, (int, bool)) for value in report.values())


def test_source_iteration_consumes_the_same_bytes_that_were_verified(tmp_path, monkeypatch):
    manifest = imported(tmp_path, {"text": "Required behavior."})
    path = Path(manifest["source_records_path"])
    original = path.read_bytes()
    replacement = original.replace(b"Required behavior.", b"Invented behavior.")
    read_bytes = Path.read_bytes
    reads = []

    def replace_after_read(current):
        raw = read_bytes(current)
        if current == path:
            reads.append(True)
            path.write_bytes(replacement)
        return raw

    monkeypatch.setattr(Path, "read_bytes", replace_after_read)
    records = list(iter_sources(manifest))
    assert [record["text"] for record in records] == ["Required behavior."]
    assert len(reads) == 1
    with pytest.raises(SpecIntegrityError, match="Source inventory digest"):
        audit_manifest(manifest)


def test_jsonl_preserves_unicode_line_separators_within_source_strings(tmp_path):
    text = "Clause A\u2028Clause B\u2029Clause C\u0085Clause D"
    manifest = imported(tmp_path, {"text": text})
    assert [record["text"] for record in iter_sources(manifest)] == [text]
    assert audit_manifest(manifest)["source_inventory_complete"]
