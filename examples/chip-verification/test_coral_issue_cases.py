"""Provenance, scope propagation and candidate/answer isolation; no EDA claims."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import coral_issue_cases as corpus


class CoralIssueCasesTests(unittest.TestCase):
    FIRST = "coralnpu-vector-zero-retirement"

    def copy_input(self, directory):
        root = Path(directory) / "input"
        shutil.copytree(corpus.CASES / self.FIRST / "input", root)
        return root

    def save_bundle(self, root, bundle, manifest):
        data = (json.dumps(bundle, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        (root / "spec.json").write_bytes(data)
        manifest["spec_sha256"] = corpus.digest(data)
        (root / "source_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_committed_real_cases_have_large_unique_context_not_simulation_claims(self):
        report = corpus.validate()
        self.assertEqual({item["case"] for item in report["cases"]},
                         {self.FIRST, "coralnpu-fence-tso-ordering"})
        for item in report["cases"]:
            self.assertGreater(item["spec_bytes"], 512 * 1024)
            self.assertGreater(item["unique_original_body_bytes"], 512 * 1024)
            self.assertIsNone(item["applicable_behavior_bytes"])
        self.assertFalse(report["simulation_executed"])
        self.assertIsNone(report["coverage_percent"])

    def test_original_context_is_all_targeted_by_short_scope_policy(self):
        for identity in corpus.read_json(corpus.CASES / "corpus_manifest.json")["cases"]:
            bundle = corpus.read_json(corpus.CASES / identity / "input" / "spec.json")
            policy = next(d for d in bundle["documents"] if d["id"] == "case-scope-policy")
            self.assertLess(len(policy["content"]), 2500)
            self.assertEqual(len(policy["origin"]["targets"]), 13)
            self.assertIn("NOT implemented requirement bytes", policy["content"])

    def test_stage_cli_keeps_original_repair_seed_and_excludes_fixed_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "candidate"
            result = subprocess.run([sys.executable, str(Path(corpus.__file__)), "stage", self.FIRST,
                                     str(destination)], check=True, capture_output=True, text=True)
            self.assertFalse(json.loads(result.stdout)["answers_exported"])
            self.assertFalse((destination / "answers").exists())
            manifest = corpus.read_json(destination / "source_manifest.json")
            seed = next(s for s in manifest["repair_seeds"] if s["path"].endswith("rvv_backend_decode_unit_lsu.sv"))
            candidate = (destination / seed["path"]).read_bytes()
            self.assertIn(b"&check_evl_not_0", candidate)
            fixed = (corpus.CASES / self.FIRST / "answers/reference" / seed["origin"]["path"]).read_bytes()
            self.assertNotEqual(candidate, fixed)
            self.assertNotIn(b"&check_evl_not_0", fixed)
            corpus.check_input(destination)

    def test_each_case_scope_restricts_acceptance_to_its_own_defect(self):
        def load_scope(identity):
            root = corpus.CASES / identity / "input"
            bundle = corpus.read_json(root / "spec.json")
            return (next(d for d in bundle["documents"] if d["id"] == "case-scope-policy"),
                    corpus.read_json(root / "integration_profile.json"))
        zero, zero_profile = load_scope(self.FIRST)
        fence, fence_profile = load_scope("coralnpu-fence-tso-ordering")
        self.assertNotEqual(zero["content"], fence["content"])
        self.assertEqual(zero["origin"]["repair_boundary"], "zero_element_vector_command_retirement")
        self.assertIn("FENCE.TSO decode/ordering is a separate known defect", zero["content"])
        self.assertIn("Exclude its opcode from pressure programs", zero["content"])
        self.assertNotIn("Run scalar-only checks", zero["content"])
        self.assertEqual(zero_profile["pressure_constraints"]["excluded_opcodes"], ["0x8330000f"])
        self.assertEqual(fence["origin"]["repair_boundary"], "fence_tso_decode_dispatch_ordering")
        self.assertIn("Zero-element compliance is background, NOT an additional repair target", fence["content"])
        self.assertIn("independently baseline-qualified", fence["content"])
        self.assertEqual(fence_profile["pressure_constraints"]["vector_vl"], "0<vl<=VLMAX")
        self.assertEqual(fence_profile["pressure_constraints"]["vector_vstart"], 0)

    def test_scope_policy_cannot_be_rebound_to_the_other_case(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.copy_input(directory)
            bundle = corpus.read_json(root / "spec.json")
            manifest = corpus.read_json(root / "source_manifest.json")
            policy = next(d for d in bundle["documents"] if d["id"] == "case-scope-policy")
            policy["origin"]["case_id"] = "coralnpu-fence-tso-ordering"
            source = next(d for d in manifest["documents"] if d["id"] == "case-scope-policy")
            source["origin"] = policy["origin"]
            self.save_bundle(root, bundle, manifest)
            with self.assertRaisesRegex(ValueError, "different case or repair boundary"):
                corpus.check_input(root)

    def test_candidate_profile_is_bound_to_the_original_repository_revision(self):
        mutations = [("candidate_revision", "main"),
                     ("candidate_revision", "5c599a884fb07b25f12be6cd027c12e93a99de45"),
                     ("candidate_repository", "https://github.com/another/repository")]
        for field, value in mutations:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                root = self.copy_input(directory)
                profile = corpus.read_json(root / "integration_profile.json")
                profile[field] = value
                (root / "integration_profile.json").write_text(json.dumps(profile), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Candidate repository/revision differs"):
                    corpus.check_input(root)

    def test_candidate_root_module_cannot_disagree_with_shared_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.copy_input(directory)
            profile = corpus.read_json(root / "integration_profile.json")
            profile["root_module"] = "DifferentCore"
            (root / "integration_profile.json").write_text(json.dumps(profile), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Profile/graph root module differs"):
                corpus.check_input(root)

    def test_stage_rejects_repinning_even_when_inventory_is_refreshed(self):
        with tempfile.TemporaryDirectory() as directory:
            local_corpus = Path(directory) / "cases"
            root = self.copy_input(local_corpus / self.FIRST)
            profile = corpus.read_json(root / "integration_profile.json")
            profile["candidate_revision"] = "5c599a884fb07b25f12be6cd027c12e93a99de45"
            raw = json.dumps(profile).encode("utf-8")
            (root / "integration_profile.json").write_bytes(raw)
            case_manifest = corpus.read_json(corpus.CASES / self.FIRST / "case_manifest.json")
            entry = next(e for e in case_manifest["input_files"] if e["path"] == "integration_profile.json")
            entry.update(bytes=len(raw), sha256=corpus.digest(raw))
            (root.parent / "case_manifest.json").write_text(json.dumps(case_manifest), encoding="utf-8")
            shutil.copyfile(corpus.CASES / "corpus_manifest.json", local_corpus / "corpus_manifest.json")
            destination = Path(directory) / "export"
            with self.assertRaisesRegex(ValueError, "Candidate repository/revision differs"):
                corpus.stage(self.FIRST, destination, local_corpus)
            self.assertFalse(destination.exists())

    def test_extra_answer_cannot_enter_staged_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.copy_input(directory)
            (root / "golden.sv").write_text("module answer; endmodule", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unexpected/missing input"):
                corpus.check_input(root)

    def test_mutated_source_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.copy_input(directory)
            manifest = corpus.read_json(root / "source_manifest.json")
            (root / manifest["documents"][0]["path"]).write_bytes(b"Invented replacement specification")
            with self.assertRaisesRegex(ValueError, "Original source body changed"):
                corpus.check_input(root)

    def test_authored_padding_cannot_satisfy_original_size_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.copy_input(directory)
            bundle = corpus.read_json(root / "spec.json")
            manifest = corpus.read_json(root / "source_manifest.json")
            for doc, source in zip(bundle["documents"], manifest["documents"], strict=True):
                source["count_toward_size"] = False
                source["origin"] = doc["origin"] = {"kind": "authored_exercise"}
            self.save_bundle(root, bundle, manifest)
            with self.assertRaisesRegex(ValueError, "Unique original bodies must exceed"):
                corpus.check_input(root)

    def test_reference_role_rejected_even_with_updated_spec_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.copy_input(directory)
            bundle = corpus.read_json(root / "spec.json")
            manifest = corpus.read_json(root / "source_manifest.json")
            bundle["documents"][0]["role"] = "reference"
            self.save_bundle(root, bundle, manifest)
            with self.assertRaisesRegex(ValueError, "Answer/reference"):
                corpus.check_input(root)

    def test_missing_scope_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.copy_input(directory)
            bundle = corpus.read_json(root / "spec.json")
            manifest = corpus.read_json(root / "source_manifest.json")
            policy = next(d for d in bundle["documents"] if d["id"] == "case-scope-policy")
            policy["origin"]["targets"].pop()
            source = next(d for d in manifest["documents"] if d["id"] == "case-scope-policy")
            source["origin"] = policy["origin"]
            self.save_bundle(root, bundle, manifest)
            with self.assertRaisesRegex(ValueError, "Scope policy does not accompany"):
                corpus.check_input(root)

    def test_seed_cannot_claim_fixed_revision_as_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.copy_input(directory)
            manifest = corpus.read_json(root / "source_manifest.json")
            manifest["repair_seeds"][0]["origin"]["ref"] = "5c599a884fb07b25f12be6cd027c12e93a99de45"
            (root / "source_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unpinned baseline"):
                corpus.check_input(root)

    def test_divergent_design_dv_graph_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.copy_input(directory)
            graph = corpus.read_json(root / "module_graph.json")
            graph["edges"].pop()
            (root / "module_graph.json").write_text(json.dumps(graph), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Design/DV graph differs"):
                corpus.check_input(root)

    def test_stage_refuses_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "must not exist"):
                corpus.stage(self.FIRST, Path(directory))

    def test_traversal_windows_alias_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("../answer", "/answer", "C:/answer", "a\\b", "a//b", "a/../b", "a. /b"):
                with self.subTest(path=relative), self.assertRaisesRegex(ValueError, "Unsafe"):
                    corpus.safe_path(root, relative)
            (root / "real").mkdir()
            try:
                (root / "alias").symlink_to(root / "real", target_is_directory=True)
            except OSError:
                self.skipTest("Symlink creation unavailable")
            with self.assertRaisesRegex(ValueError, "Linked"):
                corpus.safe_path(root, "alias/file")


if __name__ == "__main__":
    unittest.main()
