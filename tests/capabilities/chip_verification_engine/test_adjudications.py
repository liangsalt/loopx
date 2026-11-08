"""Source-bound authored rulings are real, bounded input, never upstream edits."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from loopx.capabilities.chip_verification.engine import engineering
from loopx.capabilities.chip_verification.engine.bundles import validate_bundle
from loopx.capabilities.chip_verification.engine.context import ContextBudgetExceeded, build_context
from loopx.capabilities.chip_verification.engine.models import ValidationError
from loopx.capabilities.chip_verification.engine.spec import SpecError, SpecIntegrityError, audit_manifest, ingest_spec, iter_sources
from loopx.capabilities.chip_verification.engine.workflow import DVProject, save_json


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document(identifier, content, module="spi"):
    return {"id": identifier, "title": identifier, "layer": "module", "module": module,
            "role": "specification", "origin": {"sha256": sha(content)}, "content": content}


def adjudication(identifier, content, target, start=1, end=1):
    result = document(identifier, content)
    result["origin"].update(authored=True, kind="source_adjudication", targets=[{
        "document_id": target["id"], "sha256": sha(target["content"]), "line_start": start, "line_end": end}])
    return result


def fixture():
    original = document("upstream", "Ambiguous ready/idle event wording. " + "original " * 10 + "\n" +
                        "Unrelated transfer field. " * 5 + "\n" + "Unaffected FIFO field. " * 5 + "\n")
    other = document("usb", "USB-only behavior must remain in the USB task.\n", module="usb")
    ruling = adjudication("ruling", "Case rule: ready is READY rising; idle is ACTIVE falling. "
                          "Sampling latency remains unspecified.\n", original)
    return {"schema": "dvflow.spec_bundle", "schema_version": 1, "title": "Adjudication fixture",
            "documents": [original, other, ruling], "relations": []}


class AdjudicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "spec.json"
        self.value = fixture()

    def tearDown(self):
        self.temporary.cleanup()

    def save(self):
        save_json(self.path, self.value)

    def ingest(self, chunk=160):
        self.save()
        return ingest_spec(self.path, self.root / "sources", chunk)

    def project(self):
        self.save()
        project = DVProject(self.root / "project")
        project.import_spec(self.path, chunk_chars=160, context_chars=40000)
        return project

    def routed(self, project):
        return next(task for task in project.store.list_tasks(kind="model")
                    if task["payload"].get("dependency_source_ids"))

    def response(self, task, unresolved=False):
        owned = task["payload"]["source_ids"]
        sources = owned + task["payload"].get("dependency_source_ids", [])
        return {"requirements": [{"local_id": "event-rule", "statement": "READY rising denotes ready; ACTIVE falling denotes idle.",
                "source_ids": sources, "modules": ["spi"], "features": ["events"], "interfaces": ["status"],
                "conditions": ["event enabled"], "expected": "Distinguish the two transitions.",
                "exceptions": [], "applicability": "in_scope"}],
                "dispositions": [{"source_id": sid, "classification": "requirements", "reason": "Original behavior with authored ruling"}
                                 for sid in owned], "cross_references": [],
                "unresolved": [{"source_ids": sources, "question": "Sampling latency remains unspecified for event observation.",
                                "blocking": True}] if unresolved else []}

    def finish(self, project, task, result, validator):
        attempt = project.store.claim(task["id"], "unit-test-only")
        output = project.root / "responses" / (attempt["id"] + ".json")
        save_json(output, result)
        project.store.finish(task["id"], attempt["id"], result, artifacts=[output], validator=validator)

    def extract(self, project, unresolved=False):
        target = self.routed(project)
        for task in project.store.list_tasks(kind="model", status="pending"):
            result = self.response(task, unresolved) if task["id"] == target["id"] else {
                "requirements": [], "unresolved": [], "cross_references": [],
                "dispositions": [{"source_id": sid, "classification": "context", "reason": "Fixture context"}
                                 for sid in task["payload"]["source_ids"]]}
            self.finish(project, task, result, project._validate_model)
        project.reconcile()
        return target

    def test_exact_original_survives_and_only_overlapping_sources_receive_policy(self):
        manifest = self.ingest()
        self.assertTrue(audit_manifest(manifest)["source_inventory_complete"])
        records = list(iter_sources(manifest))
        bodies = [item for item in records if item["metadata"].get("source_role") != "bundle_metadata"]
        original = [item for item in bodies if item["metadata"]["document_id"] == "upstream"]
        self.assertEqual(self.value["documents"][0]["content"], "".join(item["text"] for item in original))
        policy_ids = {item["source_id"] for item in bodies if item["metadata"]["document_id"] == "ruling"}
        affected = {item["source_id"] for item in original if item["start_char"] < len(self.value["documents"][0]["content"].splitlines(True)[0])}
        all_owned = []
        for task in manifest["extraction_tasks"]:
            all_owned.extend(task["source_ids"])
            if affected.intersection(task["source_ids"]):
                self.assertEqual(policy_ids, set(task["payload"]["dependency_source_ids"]))
                self.assertEqual(task["source_ids"], task["payload"]["acceptance"]["owned_source_ids"])
            else:
                self.assertNotIn("dependency_source_ids", task["payload"])
        self.assertEqual(len(records), len(all_owned))
        self.assertEqual(len(all_owned), len(set(all_owned)))

    def test_real_task_context_receives_ruling_and_excludes_other_module(self):
        project = self.project()
        task = self.routed(project)
        prompt, _, pack = project.task_context(task)
        self.assertIn(self.value["documents"][2]["content"], "".join(source["text"] for source in pack["sources"]))
        self.assertNotIn("USB-only", prompt)
        self.assertIn("Every owned source_id", prompt)
        self.assertIn("scoped question", prompt)
        self.assertEqual({"upstream", "ruling"}, {source["metadata"]["document_id"] for source in pack["sources"]})

    def test_adjudication_itself_has_no_implicit_original_dependency(self):
        project = self.project()
        task = next(task for task in project.store.list_tasks(kind="model", status="pending")
                    if project.store.get_source(task["payload"]["source_ids"][0])["metadata"]["metadata"]["document_id"] == "ruling")
        prompt, _, pack = project.task_context(task)
        self.assertEqual({"ruling"}, {source["metadata"]["document_id"] for source in pack["sources"]})
        self.assertIn("explicitly authored input adjudication", prompt)

    def test_invalid_or_unanchored_adjudication_rejected(self):
        mutations = [
            lambda doc: doc["origin"].update(authored=False),
            lambda doc: doc["origin"].pop("sha256"),
            lambda doc: doc["origin"].pop("targets"),
            lambda doc: doc["origin"].update(targets=[]),
            lambda doc: doc.update(role="reference"),
            lambda doc: doc["origin"]["targets"][0].update(sha256="0" * 64),
            lambda doc: doc["origin"]["targets"][0].update(document_id="absent"),
            lambda doc: doc["origin"]["targets"][0].update(document_id="ruling"),
            lambda doc: doc["origin"]["targets"][0].update(line_start=True),
            lambda doc: doc["origin"]["targets"][0].update(line_start=0),
            lambda doc: doc["origin"]["targets"][0].update(line_end=99),
            lambda doc: doc["origin"]["targets"][0].update(line_start=3, line_end=2),
            lambda doc: doc["origin"]["targets"].append(copy.deepcopy(doc["origin"]["targets"][0])),
        ]
        for index, mutate in enumerate(mutations):
            value = copy.deepcopy(self.value)
            mutate(value["documents"][2])
            with self.subTest(index=index), self.assertRaises(SpecError):
                validate_bundle(value)

    def test_reference_cannot_be_promoted_by_adjudication(self):
        self.value["documents"][0]["role"] = "reference"
        with self.assertRaisesRegex(SpecError, "cannot promote reference"):
            validate_bundle(self.value)

    def test_original_edit_invalidates_target_even_if_original_digest_updated(self):
        original = self.value["documents"][0]
        original["content"] += "Another upstream line.\n"
        original["origin"]["sha256"] = sha(original["content"])
        with self.assertRaisesRegex(SpecError, "target SHA mismatch"):
            validate_bundle(self.value)

    def test_cycle_rejected_and_acyclic_policy_dependencies_follow_transitively(self):
        first = self.value["documents"][2]
        second = adjudication("second", "Additional scoped rule, without resolving latency.\n", first)
        self.value["documents"].append(second)
        manifest = self.ingest()
        mapping = {source["source_id"]: source for source in iter_sources(manifest)}
        target = next(task for task in manifest["extraction_tasks"]
                      if mapping[task["source_ids"][0]]["metadata"].get("document_id") == "upstream")
        self.assertEqual({"ruling", "second"}, {mapping[sid]["metadata"]["document_id"]
                         for sid in target["payload"]["dependency_source_ids"]})
        first["origin"]["targets"] = [{"document_id": "second", "sha256": sha(second["content"]), "line_start": 1, "line_end": 1}]
        with self.assertRaisesRegex(SpecError, "dependency cycle"):
            validate_bundle(self.value)

    def test_ordinary_old_bundle_and_relations_do_not_gain_policy_priority(self):
        self.value["documents"][2]["origin"].pop("kind")
        self.value["relations"] = [{"id": "related", "source": "ruling", "target": "upstream", "kind": "refines", "rationale": "Candidate mapping"}]
        manifest = self.ingest()
        self.assertTrue(audit_manifest(manifest)["source_inventory_complete"])
        self.assertTrue(all("dependency_source_ids" not in task["payload"] for task in manifest["extraction_tasks"]))

    def test_stale_or_altered_manifest_cannot_silently_lose_routing(self):
        manifest = self.ingest()
        routed = next(task for task in manifest["extraction_tasks"] if task["payload"].get("dependency_source_ids"))
        routed["payload"].pop("dependency_source_ids")
        with self.assertRaisesRegex(SpecIntegrityError, "routing is stale or altered"):
            audit_manifest(manifest)

    def test_rehashed_metadata_cannot_erase_or_redirect_adjudication_routing(self):
        manifest = self.ingest()
        originals = list(iter_sources(manifest))
        mutations = [("upstream", "source_role", "bundle_metadata"),
                     ("upstream", "document_id", "usb"),
                     ("upstream", "module", "usb"),
                     ("ruling", "source_adjudication", False),
                     ("ruling", "source_role", "bundle_metadata")]
        for document_id, key, value in mutations:
            tampered = copy.deepcopy(manifest)
            records = copy.deepcopy(originals)
            for source in records:
                if source["pointer"].endswith("/content") and source["metadata"].get("document_id") == document_id:
                    source["metadata"][key] = value
            # Model a stale/rebuilt cache whose checksums are internally consistent:
            # source text, source pointers and the trusted spec snapshot are unchanged.
            data = "".join(json.dumps(source) + "\n" for source in records).encode("utf-8")
            Path(tampered["source_records_path"]).write_bytes(data)
            tampered["sources_sha256"] = hashlib.sha256(data).hexdigest()
            for task in tampered["extraction_tasks"]:
                task["payload"].pop("dependency_source_ids", None)
                task["payload"].pop("acceptance", None)
            with self.subTest(document=document_id, field=key), self.assertRaisesRegex(SpecIntegrityError, "metadata disagrees with specification snapshot"):
                audit_manifest(tampered)

    def test_context_budget_counts_all_policy_fragments_without_truncation(self):
        self.value["documents"][2]["content"] += "Mandatory scoped obligation. " * 100
        self.value["documents"][2]["origin"]["sha256"] = sha(self.value["documents"][2]["content"])
        manifest = self.ingest()
        task = next(task for task in manifest["extraction_tasks"] if task["payload"].get("dependency_source_ids"))
        with self.assertRaises(ContextBudgetExceeded) as raised:
            build_context(manifest, task, max_chars=2000)
        self.assertEqual(set(task["source_ids"] + task["payload"]["dependency_source_ids"]), set(raised.exception.source_ids))
        pack = build_context(manifest, task, max_chars=50000)
        policies = [source["text"] for source in pack["sources"] if source["metadata"]["document_id"] == "ruling"]
        # Source IDs are sorted dependencies; reconstruct by declared source offsets.
        bodies = sorted((source for source in pack["sources"] if source["metadata"]["document_id"] == "ruling"), key=lambda source: source["start_char"])
        self.assertGreater(len(policies), 1)
        self.assertEqual(self.value["documents"][2]["content"], "".join(source["text"] for source in bodies))

    def test_requirements_and_unresolved_need_owned_and_applicable_policy_citations(self):
        project = self.project()
        task = self.routed(project)
        result = self.response(task, unresolved=True)
        self.assertTrue(project._validate_model(task, result, [])["passed"])
        for key in ("requirements", "unresolved"):
            for refs, message in ((task["payload"]["source_ids"], "applicable source adjudication"),
                                  (task["payload"]["dependency_source_ids"], "owned source")):
                invalid = copy.deepcopy(result)
                invalid[key][0]["source_ids"] = refs
                with self.subTest(key=key, refs=refs), self.assertRaisesRegex(ValidationError, message):
                    project._validate_model(task, invalid, [])
        result["dispositions"].append({"source_id": task["payload"]["dependency_source_ids"][0], "classification": "context", "reason": "Duplicate owner"})
        with self.assertRaisesRegex(ValidationError, "exactly one disposition"):
            project._validate_model(task, result, [])

    def test_dependency_cross_reference_can_commit_without_transferring_ownership(self):
        project = self.project()
        task = self.routed(project)
        result = self.response(task, unresolved=True)
        dependency = task["payload"]["dependency_source_ids"][0]
        result["cross_references"] = [{"from_source_id": dependency, "reference": "module_graph.json",
                                       "reason": "The supplied policy references the shared module graph."}]
        unknown = copy.deepcopy(result)
        unknown["cross_references"][0]["from_source_id"] = "src_not_supplied"
        with self.assertRaisesRegex(ValidationError, "Cross reference source is outside this task"):
            project._validate_model(task, unknown, [])
        no_owner = copy.deepcopy(result)
        no_owner["requirements"][0]["source_ids"] = [dependency]
        with self.assertRaisesRegex(ValidationError, "owned source"):
            project._validate_model(task, no_owner, [])
        duplicate_owner = copy.deepcopy(result)
        duplicate_owner["dispositions"].append({"source_id": dependency, "classification": "context", "reason": "Policy context"})
        with self.assertRaisesRegex(ValidationError, "exactly one disposition"):
            project._validate_model(task, duplicate_owner, [])
        self.finish(project, task, result, project._validate_model)
        stored = project.store.get_task(task["id"])
        self.assertEqual("done", stored["status"])
        self.assertEqual(result, stored["result"])

    def test_shared_design_and_dv_inputs_preserve_both_citation_sets(self):
        project = self.project()
        engineering.enable(project)
        source_task = self.extract(project)
        contract = project.store.list_tasks(kind="spec-contract")[0]
        expected = set(source_task["payload"]["source_ids"] + source_task["payload"]["dependency_source_ids"])
        self.assertEqual(expected, set(contract["payload"]["source_ids"]))
        refs = [requirement["id"] for requirement in contract["payload"]["requirements"]]
        candidate = {"entry_views": ["functional"], "top": "spi",
            "modules": [{**module, "role": "event controller"} for module in contract["payload"]["canonical_modules"]],
            "ports": [{"name": "ready", "direction": "output", "width": 1, "clock_domain": "", "reset": "",
                       "origin": "supplied", "requirement_ids": refs}],
            "parameters": [], "clocks": [], "resets": [], "relations": [],
            "requirements": [{"requirement_id": ref, "module_ids": [contract["payload"]["module_id"]], "relation_ids": []} for ref in refs],
            "decisions": [], "unresolved": []}
        self.finish(project, contract, candidate, lambda task, result, artifacts: engineering.validate(project, task, result, artifacts))
        project.reconcile()
        for kind in ("contract-plan", "microarchitecture"):
            child = project.store.list_tasks(kind=kind)[0]
            self.assertEqual(expected, set(child["payload"]["source_ids"]))
            self.assertEqual(expected, set(child["payload"]["requirements"][0]["source_ids"]))

    def test_scoped_unresolved_policy_blocks_shared_contract_and_survives_resume(self):
        project = self.project()
        engineering.enable(project)
        self.extract(project, unresolved=True)
        resumed = DVProject(project.root)
        resumed.reconcile()
        contract = resumed.store.list_tasks(kind="spec-contract")[0]
        self.assertEqual("blocked", contract["status"])
        self.assertEqual("Sampling latency remains unspecified for event observation.", contract["payload"]["known_questions"][0]["question"])
        self.assertEqual([], resumed.store.list_tasks(kind="contract-plan"))
        self.assertEqual([], resumed.store.list_tasks(kind="microarchitecture"))


if __name__ == "__main__":
    unittest.main()
