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

    def test_original_context_carries_complete_scope_and_cadence_contract(self):
        for identity in corpus.read_json(corpus.CASES / "corpus_manifest.json")["cases"]:
            bundle = corpus.read_json(corpus.CASES / identity / "input" / "spec.json")
            policy = next(d for d in bundle["documents"] if d["id"] == "case-scope-policy")
            self.assertEqual(len(policy["origin"]["targets"]), 13)
            self.assertIn("NOT implemented requirement bytes", policy["content"])
            contract = next(d for d in bundle["documents"] if d["id"] == "case-cadence-contract")
            self.assertEqual({t["document_id"] for t in contract["origin"]["targets"]},
                             {t["document_id"] for t in policy["origin"]["targets"]} | {"case-problem"})
            self.assertIn("Exact interface assets", contract["content"])
            self.assertIn("4096 cycles" if identity == self.FIRST else "1024 cycles", contract["content"])

    def test_real_semantic_import_keeps_entire_contract_with_original_tasks(self):
        from loopx.capabilities.chip_verification.engine.context import build_context
        from loopx.capabilities.chip_verification.engine.spec import ingest_spec, iter_sources
        for identity in corpus.CADENCE_CASES:
            with self.subTest(case=identity), tempfile.TemporaryDirectory() as directory:
                source = corpus.CASES / identity / "input/spec.json"
                manifest = ingest_spec(source, Path(directory) / "sources", max_chunk_chars=1)
                records = {record["source_id"]: record for record in iter_sources(manifest)}
                contract_ids = {sid for sid, record in records.items()
                                if record["metadata"].get("document_id") == "case-cadence-contract"
                                and record["metadata"].get("source_role") != "bundle_metadata"}
                source_docs = corpus.read_json(source)["documents"]
                original_ids = {d["id"] for d in source_docs if d["origin"].get("repository")}
                selected = None
                for task in manifest["extraction_tasks"]:
                    if any(records[sid]["metadata"].get("document_id") in original_ids
                           and records[sid]["metadata"].get("source_role") != "bundle_metadata"
                           for sid in task["source_ids"]):
                        self.assertTrue(contract_ids <= set(task["payload"]["dependency_source_ids"]))
                        selected = task
                self.assertIsNotNone(selected)
                pack = build_context(manifest, selected, max_chars=1, max_tokens=1)
                actual = sorted((s for s in pack["sources"] if s["source_id"] in contract_ids),
                                key=lambda s: s["start_char"])
                expected = next(d["content"] for d in source_docs if d["id"] == "case-cadence-contract")
                self.assertEqual("".join(s["text"] for s in actual), expected)

    def test_stage_cli_keeps_history_and_exports_cadence_kit_without_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "candidate"
            result = subprocess.run([sys.executable, str(Path(corpus.__file__)), "stage", self.FIRST,
                                     str(destination)], check=True, capture_output=True, text=True)
            self.assertFalse(json.loads(result.stdout)["answers_exported"])
            self.assertFalse((destination / "answers").exists())
            manifest = corpus.read_json(destination / "source_manifest.json")
            seed = next(s for s in manifest["repair_seeds"] if s["path"].endswith("rvv_backend_decode_unit_lsu.sv"))
            self.assertEqual(seed["role"], "historical_mechanism_reference")
            self.assertFalse(seed["execution_required"])
            candidate = (destination / seed["path"]).read_bytes()
            self.assertIn(b"&check_evl_not_0", candidate)
            fixed = (corpus.CASES / self.FIRST / "answers/reference" / seed["origin"]["path"]).read_bytes()
            self.assertNotEqual(candidate, fixed)
            self.assertNotIn(b"&check_evl_not_0", fixed)
            profile = corpus.read_json(destination / "integration_profile.json")
            self.assertEqual(profile["toolchain"], corpus.CADENCE_TOOLCHAIN)
            self.assertEqual(profile["forbidden_execution_roots"], ["baseline", "sources"])
            self.assertTrue((destination / "eda/cadence_runner.py").is_file())
            generation = corpus.read_json(destination / "eda/verification_profile.json")
            self.assertEqual(generation["top"], profile["root_module"])
            self.assertEqual(generation["sources"][0]["sha256"], profile["contract_sha256"])
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
        self.assertIn("outside the vector", zero["content"])
        self.assertNotIn("Run scalar-only checks", zero["content"])
        self.assertEqual(zero_profile["pressure_constraints"]["excluded_operations"], ["FENCE.TSO"])
        self.assertEqual(fence["origin"]["repair_boundary"], "fence_tso_decode_dispatch_ordering")
        self.assertIn("Zero-element compliance is background, NOT an additional repair target", fence["content"])
        self.assertIn("stronger full RW,RW serialization is legal", fence["content"])
        self.assertIn("Do not require optional W->R", fence["content"])
        self.assertEqual(fence_profile["pressure_constraints"]["excluded_operations"],
                         ["vector_instructions", "FENCE.I"])

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

    def test_historical_profile_is_bound_to_the_original_repository_revision(self):
        mutations = [("historical_revision", "main"),
                     ("historical_revision", "5c599a884fb07b25f12be6cd027c12e93a99de45"),
                     ("historical_repository", "https://github.com/another/repository")]
        for field, value in mutations:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                root = self.copy_input(directory)
                profile = corpus.read_json(root / "integration_profile.json")
                profile[field] = value
                (root / "integration_profile.json").write_text(json.dumps(profile), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Historical repository/revision differs"):
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
            profile["historical_revision"] = "5c599a884fb07b25f12be6cd027c12e93a99de45"
            raw = json.dumps(profile).encode("utf-8")
            (root / "integration_profile.json").write_bytes(raw)
            case_manifest = corpus.read_json(corpus.CASES / self.FIRST / "case_manifest.json")
            entry = next(e for e in case_manifest["input_files"] if e["path"] == "integration_profile.json")
            entry.update(bytes=len(raw), sha256=corpus.digest(raw))
            (root.parent / "case_manifest.json").write_text(json.dumps(case_manifest), encoding="utf-8")
            shutil.copyfile(corpus.CASES / "corpus_manifest.json", local_corpus / "corpus_manifest.json")
            destination = Path(directory) / "export"
            with self.assertRaisesRegex(ValueError, "Historical repository/revision differs"):
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

    def test_cadence_contract_cannot_lose_original_or_problem_targets(self):
        for target_id in ("case-problem", None):
            with self.subTest(target_id=target_id), tempfile.TemporaryDirectory() as directory:
                root = self.copy_input(directory)
                bundle = corpus.read_json(root / "spec.json")
                manifest = corpus.read_json(root / "source_manifest.json")
                contract = next(d for d in bundle["documents"] if d["id"] == "case-cadence-contract")
                targets = contract["origin"]["targets"]
                targets.remove(next(t for t in targets if t["document_id"] == target_id)
                               if target_id else targets[0])
                source = next(d for d in manifest["documents"] if d["id"] == contract["id"])
                source["origin"] = contract["origin"]
                self.save_bundle(root, bundle, manifest)
                with self.assertRaisesRegex(ValueError, "must accompany all original sources and the problem"):
                    corpus.check_input(root)

    def test_execution_assets_are_explicitly_scoped_and_embedded(self):
        for change in ("extra", "implementation", "different_ports"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = self.copy_input(directory)
                manifest = corpus.read_json(root / "source_manifest.json")
                asset = manifest["execution_assets"][0]
                if change == "extra":
                    manifest["execution_assets"].append(dict(asset, path="contracts/hidden.sv"))
                    error = "unexpected interface asset inventory"
                elif change == "implementation":
                    asset["role"] = "candidate_implementation"
                    error = "Invalid interface asset role"
                else:
                    raw = (root / asset["path"]).read_bytes().replace(b"XLEN = 32", b"XLEN = 64")
                    (root / asset["path"]).write_bytes(raw)
                    asset.update(bytes=len(raw), sha256=corpus.digest(raw))
                    error = "differs from the embedded normative contract"
                (root / "source_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, error):
                    corpus.check_input(root)

    def test_cadence_profile_cannot_regress_to_native_build_or_reference_oracle(self):
        changes = [{"native_build_required": True}, {"reference_is_execution_oracle": True},
                   {"missing_build_dependencies": ["bazel"]}, {"entry_modes": ["native_repair"]},
                   {"toolchain": {**corpus.CADENCE_TOOLCHAIN, "simulation": "verilator"}}]
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = self.copy_input(directory)
                profile = corpus.read_json(root / "integration_profile.json")
                profile.update(change)
                (root / "integration_profile.json").write_text(json.dumps(profile), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "must not require the upstream native build"):
                    corpus.check_input(root)

    def test_answer_rubric_cannot_drop_case_mutations_even_with_refreshed_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            local_corpus = Path(directory) / "cases"
            shutil.copytree(corpus.CASES, local_corpus)
            root = local_corpus / self.FIRST
            rubric = corpus.read_json(root / "answers/review_rubric.json")
            rubric["required_mutation_checks"].remove("wrong fault prefix")
            raw = json.dumps(rubric).encode("utf-8")
            (root / "answers/review_rubric.json").write_bytes(raw)
            manifest = corpus.read_json(root / "case_manifest.json")
            entry = next(e for e in manifest["answer_files"] if e["path"] == "review_rubric.json")
            entry.update(bytes=len(raw), sha256=corpus.digest(raw))
            (root / "case_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "mutation checks differ"):
                corpus.validate(local_corpus)

    def test_kit_extra_file_and_rehashed_runner_are_rejected(self):
        for change in ("extra", "runner"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "candidate"
                corpus.stage(self.FIRST, destination)
                if change == "extra":
                    (destination / "eda/answer.sv").write_text("module answer; endmodule", encoding="utf-8")
                    error = "Unexpected staged Cadence kit files"
                else:
                    runner = destination / "eda/cadence_runner.py"
                    raw = runner.read_bytes() + b"\n# changed\n"
                    runner.write_bytes(raw)
                    manifest = corpus.read_json(destination / "eda/kit_manifest.json")
                    entry = next(e for e in manifest["files"] if e["path"].endswith("cadence_runner.py"))
                    entry.update(bytes=len(raw), sha256=corpus.digest(raw))
                    (destination / "eda/kit_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                    error = "Cadence kit content changed"
                with self.assertRaisesRegex(ValueError, error):
                    corpus.check_input(destination)

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
