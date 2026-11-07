"""Local integrity checks; no network, simulator, or coverage claims."""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

_spec = importlib.util.spec_from_file_location("chip_starter_fetch", Path(__file__).with_name("fetch.py"))
assert _spec and _spec.loader
fetch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch)


class FetchIntegrityTests(unittest.TestCase):
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
