"""Public corpus qualification and input/export boundary regressions; no model/EDA."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import case_tool


class FileBoundaryTests(unittest.TestCase):
    def test_traversal_and_windows_aliases_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ("../secret", "/secret", "C:/secret", "a\\secret", "a/../secret", "a//b", "./a"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    case_tool.safe_path(root, path)

    def test_tamper_missing_and_unlisted_file_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "spec.md"
            path.write_bytes(b"Acknowledge exactly one transaction.\n")
            entries = [{"path": "spec.md", "bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]
            case_tool.check_files(root, entries)
            path.write_bytes(b"Acknowledge exactly two transactions.\n")
            with self.assertRaisesRegex(ValueError, "digest/size"):
                case_tool.check_files(root, entries)
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                case_tool.check_files(root, entries)
            path.write_bytes(b"Acknowledge exactly one transaction.\n")
            (root / "golden.sv").write_text("module answer; endmodule", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unexpected/missing"):
                case_tool.check_files(root, entries)

    def test_symlink_inventory_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "original").write_text("reference answer", encoding="utf-8")
            try:
                (root / "link").symlink_to(root / "original")
            except OSError:
                self.skipTest("OS does not permit symlink creation")
            with self.assertRaisesRegex(ValueError, "Linked"):
                case_tool.safe_path(root, "link")


class CorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = case_tool.read_json(case_tool.CASES / "corpus_manifest.json")
        cls.first = cls.corpus["cases"][0]["id"]

    def copy_input(self, temporary):
        root = Path(temporary) / "input"
        shutil.copytree(case_tool.CASES / self.first / "input", root)
        return root

    @staticmethod
    def save(root, bundle, manifest):
        raw = (json.dumps(bundle, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        (root / "spec.json").write_bytes(raw)
        manifest["spec_sha256"] = hashlib.sha256(raw).hexdigest()
        (root / "source_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_two_real_cases_qualify_and_reference_answers_exist(self):
        report = case_tool.validate()
        self.assertEqual({x["case"] for x in report["cases"]},
                         {"opentitan-serial-io", "opentitan-aon-control"})
        for case in report["cases"]:
            self.assertGreater(case["unique_specification_body_bytes"], 524288)
            self.assertGreater(case["spec_bytes"], 524288)
        self.assertFalse(report["simulation_executed"])
        self.assertIsNone(report["coverage_percent"])

    def test_reference_role_is_not_accepted_as_input_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_input(temporary)
            bundle = case_tool.read_json(root / "spec.json")
            manifest = case_tool.read_json(root / "source_manifest.json")
            bundle["documents"][0]["role"] = "reference"
            self.save(root, bundle, manifest)
            with self.assertRaisesRegex(ValueError, "Answer/reference"):
                case_tool.check_input(root)

    def test_counting_threshold_cannot_be_met_by_declared_totals(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_input(temporary)
            bundle = case_tool.read_json(root / "spec.json")
            manifest = case_tool.read_json(root / "source_manifest.json")
            for source in manifest["documents"]:
                source["count_toward_size"] = False
            self.save(root, bundle, manifest)
            with self.assertRaisesRegex(ValueError, "must exceed"):
                case_tool.check_input(root)

    def test_answer_file_cannot_be_smuggled_in_as_extra_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_input(temporary)
            (root / "golden.sv").write_text("module answer; endmodule", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unexpected/missing input"):
                case_tool.check_input(root)

    def test_duplicate_upstream_bodies_cannot_pad_the_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_input(temporary)
            bundle = case_tool.read_json(root / "spec.json")
            manifest = case_tool.read_json(root / "source_manifest.json")
            sources = [x for x in manifest["documents"] if x.get("count_toward_size", True)]
            documents = {x["id"]: x for x in bundle["documents"]}
            donor, target = sources[:2]
            data = documents[donor["id"]]["content"].encode("utf-8")
            documents[target["id"]]["content"] = data.decode("utf-8")
            for entry in (target, documents[target["id"]]["origin"]):
                entry["sha256"] = hashlib.sha256(data).hexdigest()
                entry["bytes"] = len(data)
                entry["git_blob_sha1"] = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
            (root / target["path"]).write_bytes(data)
            self.save(root, bundle, manifest)
            with self.assertRaisesRegex(ValueError, "Repeated specification body"):
                case_tool.check_input(root)

    def test_noncredited_upstream_context_is_still_revision_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_input(temporary)
            bundle = case_tool.read_json(root / "spec.json")
            manifest = case_tool.read_json(root / "source_manifest.json")
            target = next(x for x in manifest["documents"] if not x.get("count_toward_size", True)
                          and "ref" in next(d for d in bundle["documents"] if d["id"] == x["id"])["origin"])
            document = next(d for d in bundle["documents"] if d["id"] == target["id"])
            document["origin"]["ref"] = "0" * 40
            self.save(root, bundle, manifest)
            with self.assertRaisesRegex(ValueError, "Unpinned specification"):
                case_tool.check_input(root)

    def test_staged_workspace_has_only_declared_inputs_and_refuses_reuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "candidate"
            report = case_tool.stage(self.first, destination)
            self.assertFalse(report["answers_exported"])
            expected = next(x for x in self.corpus["cases"] if x["id"] == self.first)
            self.assertEqual({p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()},
                             {x["path"] for x in expected["input_files"]})
            self.assertFalse(any(destination.rglob("*.sv")))
            with self.assertRaisesRegex(ValueError, "Destination must be new"):
                case_tool.stage(self.first, destination)

    def test_answer_and_graph_anchors_cannot_drift_silently(self):
        reference = self.corpus["reference"]
        manifest = case_tool.read_json(case_tool.CASES / reference["manifest"])
        sources = {item["upstream_path"]: (case_tool.CASES / reference["root"] / item["path"]).read_bytes()
                   for item in manifest["files"]}
        for mutation in ("answer", "graph"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "case"
                shutil.copytree(case_tool.CASES / self.first, root)
                if mutation == "answer":
                    path = root / "obligations.json"
                    content = case_tool.read_json(path)
                    content["obligations"][0]["source_refs"][0]["anchor_exact"] = "NONEXISTENT NORMATIVE SOURCE"
                    expected = "Answer source anchor absent"
                else:
                    path = root / "input" / "module_graph.json"
                    content = case_tool.read_json(path)
                    content["edges"][0]["to"] = "unexpected_module"
                    expected = "graphs diverge"
                path.write_text(json.dumps(content), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, expected):
                    case_tool.check_answer_anchors(root, sources)


if __name__ == "__main__":
    unittest.main()
