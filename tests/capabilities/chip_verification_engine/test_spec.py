from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from loopx.capabilities.chip_verification.engine.context import ContextBudgetExceeded, build_context, render_context
from loopx.capabilities.chip_verification.engine.spec import SpecError, SpecIntegrityError, audit_manifest, ingest_spec, iter_sources, load_source


class SpecInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "spec.json"

    def tearDown(self):
        self.temporary.cleanup()

    def ingest(self, value, chunk=1000):
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return ingest_spec(self.path, self.root / "store", max_chunk_chars=chunk)

    def test_large_document_fully_inventoried_with_bounded_contexts(self):
        sections = [{"title": f"Register group {index}", "description":
                     f"Group {index} defines reset and write behavior. " +
                     "A read returns the value accepted on the preceding completed bus transaction.\n" * 40}
                    for index in range(90)]
        manifest = self.ingest({"modules": sections}, chunk=3000)
        self.assertGreater(manifest["spec_bytes"], 200 * 1024)
        audit = audit_manifest(manifest)
        self.assertEqual(audit["leaf_count"], 180)
        self.assertFalse(audit["semantic_review_complete"])
        sources = list(iter_sources(manifest))
        self.assertTrue(all(len(source["text"]) <= 3000 for source in sources))
        assigned = [source_id for task in manifest["extraction_tasks"] for source_id in task["source_ids"]]
        self.assertEqual(len(assigned), len(sources))
        self.assertEqual(set(assigned), {source["source_id"] for source in sources})
        for task in manifest["extraction_tasks"]:
            pack = build_context(manifest, task, max_chars=7000)
            self.assertLessEqual(len(render_context(pack)), 7000)

    def test_giant_string_reconstructs_exactly_and_is_not_summarized(self):
        original = "  标题\n\n条件：必须保持字段。\n" + "a" * 10000 + "\n\n Exception: preserve spaces.  "
        manifest = self.ingest({"behavior": original}, chunk=512)
        sources = list(iter_sources(manifest))
        self.assertEqual("".join(source["text"] for source in sources), original)
        self.assertGreater(len(sources), 10)
        self.assertTrue(all(source["semantic_status"] == "unreviewed" for source in sources))
        self.assertEqual(sources[0]["start_char"], 0)
        self.assertEqual(sources[-1]["end_char"], len(original))
        self.assertIsNone(sources[0]["previous_source_id"])
        self.assertEqual(sources[0]["next_source_id"], sources[1]["source_id"])
        self.assertIsNone(sources[-1]["next_source_id"])

    def test_json_pointer_escaping_and_explicit_requirement_metadata(self):
        manifest = self.ingest({"a/b": {"~key": [{"id": "REQ-1", "description": "Must acknowledge", "module": "bus"}]}})
        sources = list(iter_sources(manifest))
        source = next(source for source in sources if source["text"] == "Must acknowledge")
        self.assertEqual(source["pointer"], "/a~1b/~0key/0/description")
        self.assertEqual(source["metadata"]["explicit_id"], "REQ-1")
        self.assertEqual(source["metadata"]["module"], "bus")

    def test_empty_and_false_values_are_not_skipped(self):
        manifest = self.ingest({"zero": 0, "false": False, "null": None, "text": "", "list": [], "object": {}})
        self.assertEqual(manifest["leaf_count"], 6)
        self.assertEqual(manifest["source_count"], 6)
        self.assertEqual({source["text"] for source in iter_sources(manifest)}, {"0", "false", "null", "", "[]", "{}"})
        self.assertTrue(audit_manifest(manifest)["source_inventory_complete"])

    def test_duplicate_keys_and_non_finite_numbers_are_rejected(self):
        for text in ('{"a":1,"a":2}', '{"n":NaN}', '{"n":1e999}'):
            self.path.write_text(text, encoding="utf-8")
            with self.assertRaises(SpecError):
                ingest_spec(self.path, self.root / "store")

    def test_required_dependencies_never_silently_dropped(self):
        manifest = self.ingest({"a": "a" * 500, "b": "b" * 500}, chunk=600)
        sources = list(iter_sources(manifest))
        task = {"id": "work", "source_ids": [sources[0]["source_id"]], "dependency_source_ids": [sources[1]["source_id"]]}
        pack = build_context(manifest, task, max_chars=5000)
        self.assertEqual(len(pack["sources"]), 2)
        with self.assertRaises(ContextBudgetExceeded) as raised:
            build_context(manifest, task, max_chars=1000)
        self.assertEqual(len(raised.exception.source_ids), 2)
        with self.assertRaises(KeyError):
            build_context(manifest, {"source_ids": ["missing"]})

    def test_context_budget_includes_instruction_and_utf8_bytes(self):
        manifest = self.ingest({"text": "中文" * 100}, chunk=1000)
        task = manifest["extraction_tasks"][0]
        pack = build_context(manifest, task, max_chars=10000)
        rendered = render_context(pack)
        self.assertGreater(len(rendered.encode("utf-8")), len(rendered))
        with self.assertRaises(ContextBudgetExceeded):
            build_context(manifest, task, max_chars=10000, max_tokens=100)
        with self.assertRaises(ContextBudgetExceeded):
            build_context(manifest, {"source_ids": [], "instruction": "x" * 5000}, max_chars=1000)

    def test_snapshot_is_independent_of_original_and_detects_tampering(self):
        manifest = self.ingest({"description": "Original required behavior"})
        old_snapshot = Path(manifest["snapshot_path"]).read_bytes()
        self.path.write_text('{"description":"changed"}', encoding="utf-8")
        self.assertEqual(Path(manifest["snapshot_path"]).read_bytes(), old_snapshot)
        self.assertTrue(audit_manifest(manifest)["source_inventory_complete"])
        source = next(iter_sources(manifest))
        self.assertEqual(load_source(manifest, source["source_id"])["text"], "Original required behavior")
        with Path(manifest["source_records_path"]).open("a", encoding="utf-8") as stream:
            stream.write("{}\n")
        with self.assertRaises(SpecIntegrityError):
            build_context(manifest, manifest["extraction_tasks"][0])

    def test_reingestion_idempotency_and_stale_task_rejection(self):
        first = self.ingest({"description": "Behavior one"})
        repeated = ingest_spec(self.path, self.root / "store", max_chunk_chars=1000)
        self.assertEqual(first["manifest_path"], repeated["manifest_path"])
        second = self.ingest({"description": "Behavior two"})
        with self.assertRaises(ValueError):
            build_context(second, first["extraction_tasks"][0])


if __name__ == "__main__":
    unittest.main()
