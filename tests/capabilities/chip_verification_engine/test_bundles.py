from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from loopx.capabilities.chip_verification.engine.bundles import validate_bundle
from loopx.capabilities.chip_verification.engine.hierarchy import design_map, focused_tasks, _blocking_questions
from loopx.capabilities.chip_verification.engine.models import ValidationError, ConflictError, StateError
from loopx.capabilities.chip_verification.engine.spec import SpecError, audit_manifest, ingest_spec, iter_sources
from loopx.capabilities.chip_verification.engine.workflow import DVProject


def bundle():
    documents = []
    for identifier, layer, module, role, content in (
        ("chip", "system", "soc", "specification", "Wakeup shall preserve reset ordering.\n" * 80),
        ("fsm", "microarchitecture", "pwrmgr", "specification", "The slow FSM shall wait for power acknowledgement.\n" * 70),
        ("uvm", "verification", "pwrmgr", "reference", "An observed acknowledgement is checked by the scoreboard."),
    ):
        documents.append({"id": identifier, "title": identifier, "layer": layer,
                          "module": module, "features": ["wakeup"], "role": role,
                          "origin": {"url": "https://example.org/" + identifier,
                                     "sha256": hashlib.sha256(content.encode()).hexdigest()}, "content": content})
    return {"schema": "dvflow.spec_bundle", "schema_version": 1, "title": "Synthetic unit fixture",
            "documents": documents, "relations": [{"id": "chip-fsm", "source": "chip", "target": "fsm",
                "kind": "refines", "rationale": "Candidate wakeup contract mapping"}]}


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "spec.json"
        self.value = bundle()

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        self.path.write_text(json.dumps(self.value), encoding="utf-8")

    def project(self):
        self.save()
        project = DVProject(self.root)
        project.import_spec(self.path, chunk_chars=900, context_chars=6000)
        return project

    def test_lossless_body_and_provenance_separation(self):
        self.save()
        manifest = ingest_spec(self.path, self.root / "storage", 900)
        self.assertTrue(audit_manifest(manifest)["source_inventory_complete"])
        sources = list(iter_sources(manifest))
        mapping = {s["source_id"]: s for s in sources}
        assigned = [sid for task in manifest["extraction_tasks"] for sid in task["source_ids"]]
        self.assertEqual(set(assigned), set(mapping))
        self.assertEqual(len(assigned), len(mapping))
        for task in manifest["extraction_tasks"]:
            local = task["payload"].get("execution") == "local_bundle_metadata"
            roles = {mapping[sid]["metadata"]["source_role"] for sid in task["source_ids"]}
            self.assertEqual(roles == {"bundle_metadata"}, local)
            if not local:
                self.assertEqual(len({mapping[sid]["metadata"]["document_id"] for sid in task["source_ids"]}), 1)
        self.assertTrue(any(s["metadata"]["source_role"] == "reference" for s in sources))
        self.assertEqual(manifest["bundle"]["specification_body_bytes"], sum(len(d["content"].encode()) for d in self.value["documents"][:2]))

    def test_invalid_wrappers_do_not_silently_drop_behavior(self):
        mutations = [lambda x: x.update(shall="must check me"),
                     lambda x: x["documents"][0].update(behavior="not metadata"),
                     lambda x: x["documents"][0].update(role="skip"),
                     lambda x: x["documents"][0]["origin"].update(sha256="bad"),
                     lambda x: x["relations"][0].update(target="missing"),
                     lambda x: x["documents"][1].update(id="chip")]
        for mutate in mutations:
            value = copy.deepcopy(self.value)
            mutate(value)
            with self.subTest(value=value), self.assertRaises(SpecError):
                validate_bundle(value)

    def test_arbitrary_json_metadata_is_not_auto_disposed(self):
        self.value = {"origin": {"description": "Reset shall clear secrets"}, "content": "Wakeup"}
        project = self.project()
        self.assertTrue(all(t["status"] == "pending" for t in project.store.list_tasks()))

    def test_focus_selects_overlapping_dimensions_without_completing_other_work(self):
        project = self.project()
        tasks = project.store.ready_tasks(1000)
        document = focused_tasks(project, tasks, "document:fsm")
        module = focused_tasks(project, tasks, "module:pwrmgr")
        feature = focused_tasks(project, tasks, "feature:wakeup")
        self.assertLess(len(document), len(module))
        self.assertEqual(len(feature), len(tasks))
        self.assertEqual(project.store.ready_tasks(1000), tasks)
        with self.assertRaises(ValueError):
            focused_tasks(project, tasks, "module:missing")

    def test_metadata_is_local_durable_and_idempotent(self):
        project = self.project()
        done = project.store.list_tasks(status="done")
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["result"]["requirements"], [])
        artifacts = project.store.list_artifacts()
        self.assertEqual(len(artifacts), 1)
        self.assertTrue(artifacts[0]["path"].endswith("local_dispositions.json"))
        resumed = DVProject(self.root)
        resumed.reconcile()
        self.assertEqual(resumed.store.list_artifacts(), artifacts)
        with resumed.store._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM design_nodes").fetchone()[0], 9)

    def test_readback_uses_existing_design_projection_without_refresh(self):
        project = self.project()
        expected = design_map(project)
        before = project.store.db_path.read_bytes()
        inspected = DVProject(self.root, initialize=False)
        with patch.object(inspected.store, "verify_artifacts", side_effect=AssertionError("readback mutated artifacts")), \
             patch.object(inspected, "reconcile", side_effect=AssertionError("readback reconciled work")), \
             patch("loopx.capabilities.chip_verification.engine.hierarchy.synchronize", side_effect=AssertionError("readback rewrote projection")):
            actual = design_map(inspected, refresh=False)
        self.assertEqual(expected, actual)
        self.assertEqual(before, project.store.db_path.read_bytes())

    def test_missing_projection_readback_reports_recovery_without_creating_tables(self):
        project = self.project()
        with project.store._db(write=True) as db:
            db.execute("DROP TABLE design_nodes")
        with self.assertRaisesRegex(StateError, "refresh the project"):
            design_map(DVProject(self.root, initialize=False), refresh=False)
        with project.store._db() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='design_nodes'").fetchone())

    def test_body_cannot_be_accepted_by_local_metadata_validator(self):
        project = self.project()
        body = project.store.list_tasks(status="pending")[0]
        result = {"requirements": [], "unresolved": [], "cross_references": [],
                  "dispositions": [{"source_id": sid, "classification": "metadata", "reason": "bad"} for sid in body["payload"]["source_ids"]]}
        with self.assertRaises(ValidationError):
            project._validate_bundle_metadata(body, result, [])

    def test_source_completion_cannot_prove_cross_layer_relation(self):
        project = self.project()
        before = design_map(project, dimension="feature")
        self.assertEqual(before["items"][0]["label"], "wakeup")
        self.assertEqual(before["relations"][0]["state"], "awaiting_source_interpretation")
        for task in project.store.list_tasks(status="pending"):
            result = {"requirements": [], "unresolved": [], "cross_references": [],
                      "dispositions": [{"source_id": sid, "classification": "context", "reason": "Unit test fixture"} for sid in task["payload"]["source_ids"]]}
            attempt = project.store.claim(task["id"], "unit-test-only")
            artifact = self.root / (task["id"] + ".json")
            artifact.write_text(json.dumps(result))
            project.store.finish(task["id"], attempt["id"], result, artifacts=[artifact], validator=project._validate_model)
        after = design_map(DVProject(self.root), dimension="document", limit=1)
        self.assertEqual(after["relations"][0]["state"], "awaiting_cross_layer_review")
        self.assertEqual(after["relations"][0]["claim"], "candidate_not_proven")
        self.assertEqual(after["omitted_nodes"], 2)
        self.assertFalse(after["signoff"])
        self.assertFalse(after["items"][0]["verification_complete"])

    def test_new_baseline_preserves_old_map_and_projects_only_current(self):
        project = self.project()
        old_id = design_map(project)["baseline_id"]
        self.value["documents"][1]["module"] = "pwrmgr_v2"
        self.save()
        project.import_spec(self.path, chunk_chars=900, context_chars=6000)
        current = design_map(project, dimension="module")
        self.assertNotEqual(old_id, current["baseline_id"])
        self.assertIn("pwrmgr_v2", [item["label"] for item in current["items"]])
        with project.store._db() as db:
            self.assertGreater(db.execute("SELECT COUNT(*) FROM design_nodes WHERE baseline_id=?", (old_id,)).fetchone()[0], 0)

    def test_concurrent_import_cannot_write_old_hierarchy_to_new_baseline(self):
        import loopx.capabilities.chip_verification.engine.hierarchy as hierarchy
        project = self.project()
        other = self.root / "other.json"
        new = copy.deepcopy(self.value)
        for doc in new["documents"]:
            doc["module"] = "new_module"
        other.write_text(json.dumps(new), encoding="utf-8")
        original = hierarchy.load_manifest
        switched = False
        def interleaved(path):
            nonlocal switched
            captured = original(path)
            if not switched:
                switched = True
                with patch.object(hierarchy, "load_manifest", original):
                    project.import_spec(other, chunk_chars=900, context_chars=6000)
            return captured
        with patch.object(hierarchy, "load_manifest", interleaved), self.assertRaises(ConflictError):
            hierarchy.synchronize(project)
        current = design_map(project, dimension="module")
        self.assertEqual([n["label"] for n in current["items"]], ["new_module"])

    def test_blocking_semantics_prevent_relation_progress(self):
        project = self.project()
        for task in project.store.list_tasks(status="pending"):
            ids = task["payload"]["source_ids"]
            result = {"requirements": [], "cross_references": [],
                      "unresolved": [{"source_ids": ids, "question": "Unspecified acknowledgement polarity", "blocking": True}],
                      "dispositions": [{"source_id": sid, "classification": "uncertain", "reason": "Needs polarity"} for sid in ids]}
            attempt = project.store.claim(task["id"], "unit-test-only")
            path = self.root / (task["id"] + ".json")
            path.write_text(json.dumps(result))
            project.store.finish(task["id"], attempt["id"], result, artifacts=[path], validator=project._validate_model)
        mapped = design_map(project, dimension="document")
        self.assertTrue(all(n["source_tasks_complete"] for n in mapped["items"]))
        self.assertTrue(all(not n["interpretation_complete"] for n in mapped["items"]))
        self.assertEqual(mapped["relations"][0]["state"], "awaiting_source_clarification")

    def test_finished_candidate_questions_count_before_issue_materialization(self):
        task = {"id": "m1", "result": {"unresolved": [{"question": "polarity?", "blocking": True}],
                                           "cross_references": [{"reference": "clock table", "reason": "missing"}],
                                           "requirements": []}}
        self.assertEqual(_blocking_questions({"m1": task}, []), 2)
        issues = [{"task_id": "m1", "category": "spec_blocker", "description": "polarity?", "status": "resolved"}]
        self.assertEqual(_blocking_questions({"m1": task}, issues), 1)


if __name__ == "__main__":
    unittest.main()
