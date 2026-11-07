"""Source-entry and shared-contract tests using the real durable domain ledger."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

from loopx.capabilities.chip_verification.engine import engineering
from loopx.capabilities.chip_verification.engine.models import ConflictError, ValidationError
from loopx.capabilities.chip_verification.engine.workflow import DVProject, save_json


class EngineeringTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.spec = self.root / "spec.json"
        self.spec.write_text(json.dumps({"behavior": "A counter resets to zero and increments on each enabled rising clock edge."}), encoding="utf-8")
        self.project = DVProject(self.root)
        self.project.import_spec(self.spec, chunk_chars=7000, context_chars=24000)
        engineering.enable(self.project)

    def tearDown(self):
        self.temporary.cleanup()

    def extract(self, modules=None, unresolved=None, classify=None, cross_references=None):
        for task in self.project.store.list_tasks(kind="model"):
            if task["status"] == "done":
                continue
            response = {"requirements": [
                {"local_id": f"R{index}", "statement": self.project.store.get_source(sid)["text"],
                 "source_ids": [sid], "modules": modules or ["counter"], "features": ["counting"],
                 "interfaces": ["control"], "conditions": ["outside reset"],
                 "expected": self.project.store.get_source(sid)["text"], "exceptions": [], "applicability": "in_scope"}
                for index, sid in enumerate(task["payload"]["source_ids"])],
                "dispositions": [{"source_id": sid, "classification": "requirements", "reason": "Explicit source behavior"}
                                 for sid in task["payload"]["source_ids"]],
                "unresolved": unresolved or [], "cross_references": cross_references or []}
            if classify:
                for requirement in response["requirements"]:
                    classify(requirement)
            attempt = self.project.store.claim(task["id"], "source-session")
            path = self.root / "responses" / (attempt["id"] + ".json")
            save_json(path, response)
            self.project.store.finish(task["id"], attempt["id"], response, artifacts=[path], validator=self.project._validate_model)
        self.project.reconcile()
        return self.project.store.list_tasks(kind="spec-contract")

    def candidate(self, task, views=None):
        payload = task["payload"]
        refs = [item["id"] for item in payload["requirements"]]
        provenance = {"origin": "supplied", "requirement_ids": refs}
        return {
            "entry_views": views or ["functional"], "top": "counter",
            "modules": [{**module, "role": "counter interface"} for module in payload["canonical_modules"]],
            "ports": [
                {"name": "clk", "direction": "input", "width": 1, "clock_domain": "clock", "reset": "", **provenance},
                {"name": "rst_n", "direction": "input", "width": 1, "clock_domain": "clock", "reset": "reset", **provenance},
                {"name": "value", "direction": "output", "width": 4, "clock_domain": "clock", "reset": "reset", **provenance}],
            "parameters": [], "clocks": [{"name": "clock", "port": "clk", "edge": "rising", "period_ns": None, **provenance}],
            "resets": [{"name": "reset", "port": "rst_n", "active": "low", "clock_domain": "clock", "asynchronous": True, **provenance}],
            "relations": [],
            "requirements": [{"requirement_id": ref, "module_ids": [payload["module_id"]], "relation_ids": []} for ref in refs],
            "decisions": [], "unresolved": [],
        }

    def commit(self, task, result):
        attempt = self.project.store.claim(task["id"], "contract-session")
        path = self.root / "responses" / (attempt["id"] + ".json")
        save_json(path, result)
        receipt = self.project.store.finish(task["id"], attempt["id"], result, artifacts=[path],
                                            validator=lambda current, value, records: engineering.validate(self.project, current, value, records))
        return receipt, path

    def plan(self, task):
        return {"testpoints": [{"id": "TP" + str(index), "name": "Observed counting behavior",
                                "requirement_ids": [requirement["id"]], "level": "ip",
                                "stimulus": "Apply reset and enabled clock edges", "observations": ["sample output count"],
                                "checkers": ["compare observed count with the specified transition"],
                                "coverage": ["observe reset and counting transitions"], "uvm_components": ["counter_scoreboard"],
                                "configurations": ["source specified clock"], "priority": "critical"}
                               for index, requirement in enumerate(task["payload"]["requirements"])
                               if requirement["applicability"] == "in_scope"], "unresolved": []}

    def test_functional_entry_forks_same_contract_to_design_and_dv(self):
        task = self.extract()[0]
        self.commit(task, self.candidate(task))
        self.project.reconcile()
        plans = self.project.store.list_tasks(kind="contract-plan")
        design = self.project.store.list_tasks(kind="microarchitecture")
        self.assertEqual(1, len(plans))
        self.assertEqual(1, len(design))
        self.assertEqual(plans[0]["payload"]["contract_hash"], design[0]["payload"]["contract_hash"])
        self.assertEqual([task["id"]], plans[0]["depends_on"])
        self.assertEqual([], self.project.store.list_tasks(kind="plan"))
        prompt, _, pack = engineering.task_context(self.project, plans[0])
        self.assertEqual("counter", pack["shared_contract"]["top"])
        self.assertIn("not an oracle", prompt)

    def test_resolved_source_questions_stay_closed_while_other_questions_block(self):
        source_id = self.project.store.list_tasks(kind="model")[0]["payload"]["source_ids"][0]
        reference = {"from_source_id": source_id, "reference": "Reset interface",
                     "reason": "Confirm the reset binding"}
        questions = [{"source_ids": [source_id], "question": question, "blocking": True}
                     for question in ("Which reset polarity applies?", "Which enable level applies?")]
        original = self.extract(unresolved=questions, cross_references=[reference])[0]
        self.assertEqual("blocked", original["status"])
        self.assertEqual(3, len(original["payload"]["known_questions"]))
        issues = {issue["description"]: issue for issue in self.project.store.list_issues()}
        reference_issue = issues["Reset interface: Confirm the reset binding"]
        polarity_issue = issues["Which reset polarity applies?"]
        enable_issue = issues["Which enable level applies?"]
        self.assertEqual("source_reference", reference_issue["category"])
        self.assertEqual("spec_blocker", polarity_issue["category"])
        self.project.store.resolve_issue(reference_issue["id"], "Source review identified the reset interface.")
        self.project.store.resolve_issue(polarity_issue["id"], "Source review clarified the reset polarity.")
        self.project.reconcile()

        current = [task for task in self.project.store.list_tasks(kind="spec-contract")
                   if task["status"] not in {"stale", "cancelled"}]
        self.assertEqual(1, len(current))
        self.assertEqual("blocked", current[0]["status"])
        self.assertEqual([{"question": "Which enable level applies?", "category": "spec_blocker"}],
                         current[0]["payload"]["known_questions"])
        self.assertEqual([enable_issue["id"]], [issue["id"] for issue in self.project.store.list_issues()])
        self.assertEqual([], self.project.store.ready_tasks())

        self.project.store.resolve_issue(enable_issue["id"], "Source review clarified the enable level.")
        self.project.reconcile()
        ready = self.project.store.ready_tasks()
        self.assertEqual(1, len(ready))
        self.assertEqual("spec-contract", ready[0]["kind"])
        self.assertEqual([], ready[0]["payload"]["known_questions"])
        restarted = DVProject(self.root)
        for _ in range(2):
            restarted.reconcile()
            self.assertEqual([ready[0]["id"]], [task["id"] for task in restarted.store.ready_tasks()])
            self.assertEqual([], restarted.store.list_issues())
        self.assertEqual(3, len(restarted.store.list_issues(status="resolved")))
        self.commit(ready[0], self.candidate(ready[0]))
        self.project.reconcile()
        self.assertEqual(1, len(self.project.store.list_tasks(kind="contract-plan")))
        self.assertEqual(1, len(self.project.store.list_tasks(kind="microarchitecture")))

    def test_system_functional_entry_can_propose_shared_architectural_decomposition(self):
        task = self.extract(classify=lambda requirement: requirement.update(modules=[]))[0]
        self.assertEqual("$project", task["payload"]["module_name"])
        result = self.candidate(task, ["system", "functional"])
        refs = [item["id"] for item in task["payload"]["requirements"]]
        control = {"id": engineering.proposed_module_id("project.control"), "name": "project.control",
                   "role": "Proposed enable and reset control", "parent_id": task["payload"]["module_id"],
                   "origin": "design_choice", "requirement_ids": refs}
        datapath = {"id": engineering.proposed_module_id("project.datapath"), "name": "project.datapath",
                    "role": "Proposed counter state and incrementer", "parent_id": task["payload"]["module_id"],
                    "origin": "design_choice", "requirement_ids": refs}
        result["modules"].extend([control, datapath])
        result["relations"] = [{"id": "control_to_datapath", "source": control["id"], "target": datapath["id"],
                                "interface": "count_enable", "behavior": "Control selects the source-specified increment and reset operations",
                                "origin": "design_choice", "requirement_ids": refs}]
        result["requirements"][0]["module_ids"].extend([control["id"], datapath["id"]])
        result["requirements"][0]["relation_ids"] = ["control_to_datapath"]
        prompt, _, _ = engineering.task_context(self.project, task)
        self.assertIn("canonical modules plus explicit source-bound", prompt)
        self.assertNotIn("both endpoints from canonical_modules", prompt)
        self.commit(task, result)
        self.project.reconcile()
        plan = self.project.store.list_tasks(kind="contract-plan")[0]
        micro = self.project.store.list_tasks(kind="microarchitecture")[0]
        from loopx.capabilities.chip_verification.engine import design
        _, _, dv_pack = engineering.task_context(self.project, plan)
        _, _, design_pack = design.task_context(self.project, micro)
        self.assertEqual(dv_pack["shared_contract"], design_pack["shared_contract"])
        self.assertEqual(plan["payload"]["contract_hash"], micro["payload"]["contract_hash"])
        self.assertEqual(["design_choice", "design_choice"], [item["origin"] for item in dv_pack["shared_contract"]["modules"][1:]])
        self.assertEqual(task["payload"]["requirements"], dv_pack["requirements"])

    def test_proposed_decomposition_rejects_unproven_identity_and_cycles(self):
        task = self.extract(classify=lambda requirement: requirement.update(modules=[]))[0]
        result = self.candidate(task, ["functional"])
        proposed = {"id": engineering.proposed_module_id("project.control"), "name": "project.control", "role": "Control logic",
                    "parent_id": task["payload"]["module_id"], "origin": "design_choice",
                    "requirement_ids": [task["payload"]["requirements"][0]["id"]]}
        result["modules"].append(proposed)
        for field, value in (("origin", "supplied"), ("requirement_ids", []), ("id", "random-module"), ("parent_id", "absent")):
            candidate = copy.deepcopy(result)
            candidate["modules"][-1][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                engineering.validate(self.project, task, candidate, [])
        result["modules"].append({**proposed, "id": engineering.proposed_module_id("project.datapath"), "name": "project.datapath",
                                  "parent_id": proposed["id"]})
        result["modules"][1]["parent_id"] = result["modules"][2]["id"]
        with self.assertRaisesRegex(ValidationError, "acyclic"):
            engineering.validate(self.project, task, result, [])

    def test_proposal_cannot_borrow_another_scopes_source_module_name(self):
        self.spec.write_text(json.dumps({"system": "The chip shall count accepted commands.",
                                         "control": "The control IP shall reset its state and gate command acceptance."}), encoding="utf-8")
        self.project.import_spec(self.spec, chunk_chars=7000, context_chars=24000)

        def classify(requirement):
            requirement["modules"] = ["project.control"] if "control IP" in requirement["statement"] else []

        tasks = self.extract(classify=classify)
        root = next(task for task in tasks if task["payload"]["module_name"] == "$project")
        child = next(task for task in tasks if task["payload"]["module_name"] == "project.control")
        result = self.candidate(root, ["system", "functional"])
        result["modules"].append({"id": engineering.proposed_module_id("project.control"), "name": "project.control",
                                  "role": "Proposed control child", "parent_id": root["payload"]["module_id"],
                                  "origin": "design_choice", "requirement_ids": [root["payload"]["requirements"][0]["id"]]})
        # The source identity is protected even before the other scope has an
        # accepted contract; parallel interpretation cannot create a second ID.
        with self.assertRaisesRegex(ValidationError, engineering.module_id("project.control")):
            engineering.validate(self.project, root, result, [])
        self.commit(child, self.candidate(child))
        with self.assertRaisesRegex(ValidationError, "source canonical identity"):
            engineering.validate(self.project, root, result, [])

    def test_context_only_entry_is_insufficient_and_cannot_export_empty_success(self):
        self.spec.write_text(json.dumps({"clock": "The environment supplies a rising clock at 10 ns period."}), encoding="utf-8")
        self.project.import_spec(self.spec, chunk_chars=7000, context_chars=24000)
        tasks = self.extract(classify=lambda requirement: requirement.update(applicability="context"))
        self.assertEqual([], tasks)
        self.assertEqual([], self.project.store.ready_tasks())
        state = engineering.status(self.project)
        self.assertEqual("insufficient_behavior", state["entry_readiness"]["status"])
        self.assertEqual(1, state["entry_readiness"]["context_requirements"])
        self.assertTrue(any(item["status"] == "open" and item["description"] == engineering.ENTRY_BLOCKER
                            for item in self.project.store.list_issues()))
        exported = self.project.export_plan()
        self.assertFalse(exported["planning_complete"])
        self.assertEqual(0, exported["testpoints"])

    def test_source_unspecified_clock_can_have_an_explicit_non_guaranteed_target(self):
        task = self.extract()[0]
        result = self.candidate(task, ["functional"])
        target = result["clocks"][0]
        target.update(period_ns=10, origin="design_choice", timing_basis="implementation_target",
                      rationale="Use a 10 ns check target for the implementation; the source does not guarantee this performance.",
                      performance_guarantee=False)
        engineering.validate(self.project, task, result, [])
        target["timing_basis"] = "test_clock"
        engineering.validate(self.project, task, result, [])
        prompt, _, _ = engineering.task_context(self.project, task)
        self.assertIn("Preserve supplied clock periods", prompt)
        self.assertIn("Undefined normative timing", prompt)
        self.commit(task, result)
        self.project.reconcile()
        plan = self.project.store.list_tasks(kind="contract-plan")[0]
        _, _, pack = engineering.task_context(self.project, plan)
        chosen = pack["shared_contract"]["clocks"][0]
        self.assertEqual("design_choice", chosen["origin"])
        self.assertFalse(chosen["performance_guarantee"])
        self.assertEqual(10, chosen["period_ns"])

    def test_chosen_clock_requires_rationale_and_cannot_clear_normative_timing_gaps(self):
        task = self.extract()[0]
        result = self.candidate(task, ["functional"])
        result["clocks"][0].update(period_ns=10, origin="design_choice")
        with self.assertRaisesRegex(ValidationError, "chosen clock target"):
            engineering.validate(self.project, task, result, [])
        result["clocks"][0].update(timing_basis="implementation_target", rationale="Explicit check target only", performance_guarantee=False)
        for field, value in (("rationale", " "), ("requirement_ids", []), ("performance_guarantee", True)):
            candidate = copy.deepcopy(result)
            candidate["clocks"][0][field] = value
            with self.subTest(field=field), self.assertRaises((ValidationError, jsonschema.ValidationError)):
                engineering.validate(self.project, task, candidate, [])
        result["unresolved"] = [{"question": "Required maximum response latency is still undefined",
                                 "requirement_ids": [task["payload"]["requirements"][0]["id"]], "blocking": True}]
        self.commit(task, result)
        self.project.reconcile()
        self.assertEqual([], self.project.store.list_tasks(kind="microarchitecture"))
        self.assertEqual([], self.project.store.list_tasks(kind="contract-plan"))

    def test_microarchitecture_entry_does_not_require_upstream_documents(self):
        task = self.extract()[0]
        result = self.candidate(task, ["microarchitecture", "interface"])
        self.commit(task, result)
        restarted = DVProject(self.root)
        restarted.reconcile()
        restarted.reconcile()
        self.assertEqual(1, len(restarted.store.list_tasks(kind="microarchitecture")))
        self.assertEqual({"microarchitecture": 1, "interface": 1}, engineering.status(restarted)["entry_views"])
        self.assertEqual(result, engineering.get_contract(restarted, task["id"])["contract"])
        self.assertIn("module counter", engineering.declaration(result))

    def test_blocking_candidate_is_durable_but_cannot_start_either_track(self):
        task = self.extract()[0]
        result = self.candidate(task)
        result["unresolved"] = [{"question": "Does reset clear an in-flight transfer?", "requirement_ids": [], "blocking": True}]
        self.commit(task, result)
        self.project.reconcile()
        self.assertEqual([], self.project.store.list_tasks(kind="microarchitecture"))
        self.assertEqual([], self.project.store.list_tasks(kind="contract-plan"))
        with self.assertRaisesRegex(ValidationError, "blocking"):
            engineering.get_contract(self.project, task["id"])
        self.assertTrue(any(item["category"] == "contract_blocker" for item in self.project.store.list_issues()))

    def test_modified_contract_invalidates_both_tracks_after_readonly_rejection(self):
        task = self.extract()[0]
        _, path = self.commit(task, self.candidate(task))
        self.project.reconcile()
        path.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ConflictError, "bytes changed"):
            engineering.get_contract(self.project, task["id"])
        self.assertEqual("done", self.project.store.get_task(task["id"])["status"])
        self.project.store.verify_artifacts()
        for kind in ("spec-contract", "contract-plan", "microarchitecture"):
            self.assertEqual("stale", self.project.store.list_tasks(kind=kind)[0]["status"])

    def test_unknown_modules_and_dangling_clock_are_rejected(self):
        task = self.extract()[0]
        response = self.candidate(task)
        response["modules"][0]["id"] = "unrecognized"
        with self.assertRaisesRegex(ValidationError, "unknown module"):
            engineering.validate(self.project, task, response, [])
        response = self.candidate(task)
        response["ports"][2]["clock_domain"] = "missing_clock"
        with self.assertRaisesRegex(ValidationError, "unknown clock"):
            engineering.validate(self.project, task, response, [])

    def test_cross_module_relation_must_agree_in_both_contracts(self):
        left, right = self.extract(["counter", "consumer"])
        a, b = self.candidate(left), self.candidate(right)
        relation = {"id": "counter_to_consumer", "source": engineering.module_id("counter"),
                    "target": engineering.module_id("consumer"), "interface": "value", "behavior": "Consumer observes the updated count",
                    "origin": "supplied", "requirement_ids": [item["id"] for item in left["payload"]["requirements"]]}
        a["relations"] = [relation]
        self.commit(left, a)
        b["relations"] = [copy.deepcopy(relation)]
        engineering.validate(self.project, right, b, [])
        b["relations"][0]["behavior"] = "Consumer observes the previous count"
        with self.assertRaisesRegex(ValidationError, "Conflicting shared relation"):
            engineering.validate(self.project, right, b, [])

    def test_unknown_requirement_and_silent_omission_are_rejected(self):
        task = self.extract()[0]
        result = self.candidate(task)
        result["ports"][0]["requirement_ids"] = ["invented"]
        with self.assertRaisesRegex(ValidationError, "unknown or duplicate requirements"):
            engineering.validate(self.project, task, result, [])
        result = self.candidate(task)
        result["requirements"] = []
        with self.assertRaisesRegex(ValidationError, "silently omits"):
            engineering.validate(self.project, task, result, [])

    def test_large_single_module_is_losslessly_partitioned_into_bounded_scopes(self):
        self.spec.write_text(json.dumps({"clauses": ["Explicit behavior " + str(i) + ": " + "x" * 650 for i in range(320)]}), encoding="utf-8")
        self.assertGreater(self.spec.stat().st_size, 200 * 1024)
        self.project.import_spec(self.spec, chunk_chars=7000, context_chars=24000)
        tasks = self.extract()
        self.assertGreater(len(tasks), 1)
        mapped = [item["id"] for task in tasks for item in task["payload"]["requirements"]]
        self.assertEqual(320, len(mapped))
        self.assertEqual(320, len(set(mapped)))
        self.assertEqual(1, len({task["payload"]["module_id"] for task in tasks}))
        self.assertEqual(len(tasks), len({task["payload"]["scope_id"] for task in tasks}))
        for task in tasks:
            self.assertEqual("pending", task["status"])
            self.assertTrue(task["payload"]["partial_module"])
            prompt, schema, _ = engineering.task_context(self.project, task)
            self.assertLess(len(json.dumps({"prompt": prompt, "output_schema": schema})), 24000)
        obligation = engineering.status(self.project)["composition_obligations"][0]
        self.assertFalse(obligation["whole_module_complete"])
        self.assertEqual(len(tasks), obligation["scope_count"])

    def test_one_atomic_requirement_that_cannot_fit_remains_explicitly_blocked(self):
        self.spec.write_text(json.dumps({"behavior": "x" * 18000}), encoding="utf-8")
        self.project.import_spec(self.spec, chunk_chars=20000, context_chars=24000)
        task = self.extract()[0]
        self.assertEqual("blocked", task["status"])
        self.assertIn("no required content was truncated", task["reason"])
        self.assertEqual(1, len(task["payload"]["requirements"]))
        self.assertEqual([], self.project.store.list_tasks(kind="microarchitecture"))

    def test_source_blocker_is_not_silently_cleared_by_contract_phase(self):
        source = self.project.store.list_tasks(kind="model")[0]["payload"]["source_ids"][0]
        task = self.extract(unresolved=[{"source_ids": [source], "question": "Counter overflow behavior is unspecified", "blocking": True}])[0]
        self.assertEqual("blocked", task["status"])
        self.assertIn("Source clarification required", task["reason"])

    def test_clock_context_reaches_both_tracks_without_becoming_a_dut_testpoint(self):
        self.spec.write_text(json.dumps({"behavior": "Counter increments on enabled rising edges.",
                                         "clock": "The environment supplies a 10 ns rising core clock."}), encoding="utf-8")
        self.project.import_spec(self.spec, chunk_chars=7000, context_chars=24000)

        def classify(requirement):
            if "10 ns" in requirement["statement"]:
                requirement["applicability"] = "context"

        task = self.extract(classify=classify)[0]
        contexts = [item for item in task["payload"]["requirements"] if item["applicability"] == "context"]
        self.assertEqual(1, len(contexts))
        result = self.candidate(task)
        result["clocks"][0].update(period_ns=10, requirement_ids=[contexts[0]["id"]])
        self.commit(task, result)
        self.project.reconcile()
        plan = self.project.store.list_tasks(kind="contract-plan")[0]
        design = self.project.store.list_tasks(kind="microarchitecture")[0]
        self.assertEqual(plan["payload"]["requirements"], design["payload"]["requirements"])
        self.assertIn(contexts[0], plan["payload"]["requirements"])
        response = self.plan(plan)
        engineering.validate(self.project, plan, response, [])
        self.assertNotIn(contexts[0]["id"], {rid for point in response["testpoints"] for rid in point["requirement_ids"]})
        self.assertEqual(10, engineering.get_contract(self.project, task["id"])["contract"]["clocks"][0]["period_ns"])

    def test_global_context_is_shared_without_creating_context_only_modules(self):
        self.spec.write_text(json.dumps({"counter": "Counter shall increment.", "receiver": "Receiver shall capture accepted values.",
                                         "clock": "The global environment supplies a 10 ns clock.",
                                         "reference": "Unrelated reference-only component detail."}), encoding="utf-8")
        self.project.import_spec(self.spec, chunk_chars=7000, context_chars=24000)

        def classify(requirement):
            text = requirement["statement"]
            if "Receiver" in text:
                requirement["modules"] = ["receiver"]
            if "10 ns" in text:
                requirement.update(applicability="context", modules=[])
            if "reference-only" in text:
                requirement.update(applicability="context", modules=["reference_only"])

        tasks = self.extract(classify=classify)
        self.assertEqual({"counter", "receiver"}, {task["payload"]["module_name"] for task in tasks})
        for task in tasks:
            self.assertEqual(1, sum(item["applicability"] == "context" for item in task["payload"]["requirements"]))
            self.assertNotIn("$project", {item["name"] for item in task["payload"]["canonical_modules"]})

    def test_new_context_membership_supersedes_old_contract_and_both_tracks(self):
        self.spec.write_text(json.dumps({"behavior": "Counter increments on enabled rising edges.",
                                         "clock": "The environment supplies a 10 ns rising core clock."}), encoding="utf-8")
        self.project.import_spec(self.spec, chunk_chars=7000, context_chars=24000)

        def classify(requirement):
            if "10 ns" in requirement["statement"]:
                requirement["applicability"] = "context"

        # Characterize the previous materializer's omission without rewriting any
        # accepted source or receipt: retain its source-backed subset as old input.
        with patch.object(engineering, "reconcile", return_value=0):
            self.extract(classify=classify)
        requirements = [engineering._requirement(item) for item in self.project.store.list_requirements()
                        if item["metadata"]["applicability"] == "in_scope"]
        payload = engineering._scope_payload(self.project, "counter", requirements, 0, 1)
        old = self.project.store.add_task("spec-contract", "Previous contract without environmental context", payload,
                                          depends_on=[requirements[0]["extraction_task"]], task_id="old_contract")
        response = self.candidate(old)
        self.commit(old, response)
        # Reproduce the prior children's immutable inputs, before context
        # completeness was an admission rule. Their TP still has real source checks.
        child_payload = {**payload, "contract_task_id": old["id"], "contract_hash": engineering.digest(response)}
        old_plan = self.project.store.add_task("contract-plan", "Previous TP", child_payload, depends_on=[old["id"]])
        old_micro = self.project.store.add_task("microarchitecture", "Previous microarchitecture", child_payload, depends_on=[old["id"]])
        old_attempt = self.project.store.claim(old_plan["id"], "previous-plan-session")
        path = self.root / "responses" / "previous-plan.json"
        plan_response = self.plan(old_plan)
        save_json(path, plan_response)
        self.project.store.finish(old_plan["id"], old_attempt["id"], plan_response, artifacts=[path], validator=self.project._validate_plan)
        with self.assertRaisesRegex(ConflictError, "environmental context"):
            engineering.get_contract(self.project, old["id"])
        downstream = self.project.store.add_task("architecture", "UVM task for the previous contract", {}, depends_on=[old_plan["id"]])
        attempt = self.project.store.claim(downstream["id"], "old-live-session")
        self.project.reconcile()
        for task in (old, old_plan, old_micro, downstream):
            self.assertEqual("stale", self.project.store.get_task(task["id"])["status"])
        ignored = engineering.superseded_task_ids(self.project)
        self.assertTrue({old["id"], old_plan["id"], old_micro["id"], downstream["id"]} <= ignored)
        with self.assertRaises(ConflictError):
            self.project.store.heartbeat(downstream["id"], attempt["id"], 120)
        current = self.project.store.list_tasks(status="pending", kind="spec-contract")
        self.assertEqual(1, len(current))
        self.assertEqual(2, len(current[0]["payload"]["requirements"]))
        self.commit(current[0], self.candidate(current[0]))
        self.project.reconcile()
        plan = self.project.store.list_tasks(status="pending", kind="contract-plan")[0]
        self.commit(plan, self.plan(plan))
        self.assertTrue(self.project.export_plan()["planning_complete"])
        self.assertEqual(1, len(engineering.status(self.project)["contracts"]))
        self.assertEqual("counter", engineering.status(self.project)["contracts"][0]["top"])
        self.project.store.invalidate(plan["id"], "Accepted artifact changed or disappeared")
        self.assertNotIn(plan["id"], engineering.superseded_task_ids(self.project))
        self.assertFalse(self.project.export_plan()["planning_complete"])


if __name__ == "__main__":
    unittest.main()
