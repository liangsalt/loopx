"""Local integrity checks; no network, simulator, or coverage claims."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location("chip_starter_fetch", Path(__file__).with_name("fetch.py"))
assert _spec and _spec.loader
fetch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch)


class FetchIntegrityTests(unittest.TestCase):
    def test_cadence_policy_routes_original_specs_and_cannot_pad_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = b"Source requirement\r\nSecond exact line\r\n"
            original = {"id": "public-spec", "role": "specification", "layer": "module",
                        "module": "device", "features": [], "title": "Source",
                        "origin": {"sha256": hashlib.sha256(raw).hexdigest()}, "content": raw.decode()}
            docs = [original]
            profile = fetch.add_cadence_context("cheshire", root, docs)
            self.assertIs(docs[0], original)
            self.assertEqual(docs[0]["content"].encode(), raw)
            policy = docs[-1]
            self.assertFalse(profile["scope_frozen"])
            self.assertEqual(profile["toolchain"]["family"], "cadence")
            self.assertFalse(profile["toolchain"]["native_build_required"])
            self.assertEqual(policy["origin"]["targets"], [{"document_id": "public-spec",
                "sha256": hashlib.sha256(raw).hexdigest(), "line_start": 1, "line_end": 2}])
            self.assertIn("not a\ncomplete runnable SoC", policy["content"])
            self.assertIn("dv_generate_uvm(profile_path='eda/verification_profile.json')", policy["content"])

    def test_offline_build_stages_kit_and_keeps_authored_bytes_out_of_totals(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            raw = b"# Public spec\nRequired behavior.\n"
            descriptor = {"id": "public-spec", "title": "Source", "layer": "system", "module": "chip",
                "features": [], "role": "specification", "repository": "example/chip", "ref": "a" * 40,
                "path": "doc/spec.md", "git_blob_sha1": fetch.git_blob(raw),
                "sha256": hashlib.sha256(raw).hexdigest(), "license": "Apache-2.0"}
            catalog = {"title": "Fixture", "documents": [descriptor], "licenses": [], "relations": []}
            cached = workspace / "cache/cheshire/sources/example__chip/doc/spec.md"
            cached.parent.mkdir(parents=True)
            cached.write_bytes(raw)
            with patch.object(fetch, "catalog_for", return_value=(catalog, b"{}")), \
                 patch.object(fetch, "MINIMUM_PROSE_BYTES", 1):
                report = fetch.build("cheshire", workspace / "out", offline=True, cache_root=workspace / "cache")
                fetch.build("cheshire", workspace / "out", offline=True, cache_root=workspace / "cache")
            output = workspace / "out/cheshire"
            manifest = json.loads((output / "source_manifest.json").read_bytes())
            self.assertEqual(manifest["specification_source_bytes"], len(raw))
            self.assertEqual(manifest["specification_prose_bytes"], len(raw))
            self.assertEqual(report["document_count"], 2)
            self.assertFalse(manifest["sources"][-1]["count_toward_specification_prose_bytes"])
            self.assertTrue((output / "eda/verification_profile.json").is_file())
            self.assertEqual((output / "sources/example__chip/doc/spec.md").read_bytes(), raw)
            (output / "eda/cadence_runner.py").write_bytes(b"tampered")
            with patch.object(fetch, "catalog_for", return_value=(catalog, b"{}")), \
                 patch.object(fetch, "MINIMUM_PROSE_BYTES", 1), self.assertRaisesRegex(ValueError, "kit content changed"):
                fetch.build("cheshire", workspace / "out", offline=True, cache_root=workspace / "cache")

    def test_shipped_catalogs_pin_all_sources_and_retain_reference_roles(self):
        expected = {"opentitan_eg1": (28, 8, 1), "cheshire": (32, 14, 5)}
        for name, (documents, references, licenses) in expected.items():
            catalog, _ = fetch.catalog_for(name)
            self.assertEqual(len(catalog["documents"]), documents)
            self.assertEqual(sum(doc["role"] == "reference" for doc in catalog["documents"]), references)
            self.assertEqual(len(catalog["licenses"]), licenses)
            for doc in catalog["documents"]:
                if Path(doc["path"]).suffix in {".sv", ".hjson", ".c", ".S", ".lock"}:
                    self.assertEqual(doc["role"], "reference")

    def test_two_hashes_are_required_even_when_one_matches(self):
        data = b"Original upstream prose.\n"
        descriptor = {"repository": "example/chip", "path": "doc/spec.md",
                      "git_blob_sha1": fetch.git_blob(data), "sha256": "0" * 64}
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            fetch.verified(data, descriptor)
        descriptor["sha256"] = hashlib.sha256(data).hexdigest()
        self.assertEqual(fetch.verified(data, descriptor), data)
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            fetch.verified(data + b"changed", descriptor)

    def test_offline_cache_is_verified_and_original_bytes_survive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = b"# Source\r\nExact CRLF and final newline.\r\n"
            descriptor = {"repository": "example/chip", "ref": "a" * 40,
                          "path": "doc/spec.md", "git_blob_sha1": fetch.git_blob(data),
                          "sha256": hashlib.sha256(data).hexdigest(), "license": "Apache-2.0"}
            cached = root / "cache/sources/example__chip/doc/spec.md"
            cached.parent.mkdir(parents=True)
            cached.write_bytes(data)
            origin, content, receipt = fetch.fetch_source(root / "output", descriptor,
                offline=True, cache=root / "cache", timeout=1)
            self.assertEqual(content.encode("utf-8"), data)
            self.assertEqual((root / "output" / origin["local_path"]).read_bytes(), data)
            self.assertEqual(receipt["method"], "verified_cache_offline")
            self.assertNotIn("network_failure", receipt)

    def test_corrupt_existing_source_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = b"pinned original"
            descriptor = {"repository": "example/chip", "ref": "a" * 40, "path": "spec.md",
                          "git_blob_sha1": fetch.git_blob(expected),
                          "sha256": hashlib.sha256(expected).hexdigest(), "license": "Apache-2.0"}
            target = root / "sources/example__chip/spec.md"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"modified by user")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                fetch.fetch_source(root, descriptor, offline=True, cache=None, timeout=1)
            self.assertEqual(target.read_bytes(), b"modified by user")

    def test_prose_gate_excludes_fenced_code_and_comments(self):
        text = "Specification behavior.\n```systemverilog\nmodule padding; endmodule\n```\n<!-- comment -->\n    indented_code();\n"
        result = fetch.prose_text(text, ".md")
        self.assertIn("Specification behavior.", result)
        self.assertNotIn("padding", result)
        self.assertNotIn("comment", result)
        self.assertNotIn("indented_code", result)

    def test_output_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "escapes"):
                fetch.inside(Path(temporary), "../outside")

    @unittest.skipUnless(os.name == "nt", "Windows extended-length path spelling")
    def test_windows_extended_path_keeps_same_containment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            candidate = Path("\\\\?\\" + str(root / "source.md"))
            self.assertEqual(fetch.inside(root, candidate).name, "source.md")


if __name__ == "__main__":
    unittest.main()
