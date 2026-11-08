"""Original DV workflow: import -> bounded interpretation -> UVM planning."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import uuid

import jsonschema

from .agents import runtime
from .context import build_context, render_context
from .contracts import EXTRACTION_SCHEMA, PLAN_SCHEMA, EXTRACTION_INSTRUCTION, PLAN_INSTRUCTION
from .spec import ingest_spec, iter_sources, audit_manifest, load_manifest
from .state import StateStore
from .models import ConflictError, ValidationError


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def superseded_plan_ids(tasks):
    """Recognize cancellation only when a complete, lossless partition exists.

    A cancelled task by itself never means completed work. This projection is
    deliberately checked from current durable tasks, including all prerequisites
    and original requirement content; a reason string is not sufficient.
    """
    superseded = set()
    for original in tasks:
        if original["kind"] != "plan" or original["status"] != "cancelled":
            continue
        prefix = "Superseded by bounded plan partition "
        if not (original.get("reason") or "").startswith(prefix):
            continue
        partition = original["reason"][len(prefix):]
        parts = [task for task in tasks if task["kind"] == "plan" and
                 task["payload"].get("supersedes") == original["id"] and
                 task["payload"].get("partition_id") == partition]
        if not parts:
            continue
        count = parts[0]["payload"].get("partition_count")
        if type(count) is not int or count < 2 or len(parts) != count:
            continue
        indices = [task["payload"].get("partition_index") for task in parts]
        if any(type(index) is not int for index in indices) or set(indices) != set(range(count)):
            continue
        if any(task["payload"].get("partition_count") != count or
               task["baseline_id"] != original["baseline_id"] or
               set(task["depends_on"]) != set(original["depends_on"]) or
               any(task["payload"].get(key) != original["payload"].get(key)
                   for key in ("scope", "source_ids", "spec_sha256")) for task in parts):
            continue
        flattened = [requirement for task in sorted(parts, key=lambda task: task["payload"]["partition_index"])
                     for requirement in task["payload"].get("requirements", [])]
        if flattened == original["payload"].get("requirements"):
            superseded.add(original["id"])
    return superseded


def effective_planning_tasks(tasks):
    superseded = superseded_plan_ids(tasks)
    return [task for task in tasks if task["kind"] in {"model", "plan"} and task["id"] not in superseded]


class DVProject:
    def __init__(self, project_dir: str | Path, *, initialize: bool = True):
        self.root = Path(project_dir).resolve()
        self.store = StateStore(self.root, initialize=initialize)
        self.config_path = self.root / ".loopx" / "chip-verification" / "project.json"

    @property
    def config(self):
        if not self.config_path.is_file():
            raise ValueError("No specification bound. Import a spec first.")
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def import_spec(self, path: str | Path, *, name: str = "DV project", scope: str = "",
                    chunk_chars: int = 12000, context_chars: int = 24000):
        if context_chars < chunk_chars + 3000:
            raise ValueError("Context budget must reserve at least 3000 characters above the source chunk size")
        previous = self.config if self.config_path.exists() else {}

        def check_existing_identity(spec_hash):
            if previous.get("spec_sha256") != spec_hash:
                return
            existing_manifest = load_manifest(previous["manifest_path"])
            if existing_manifest["max_chunk_chars"] != chunk_chars:
                raise ValueError("Changing source partition size on the same specification requires a new project directory; the existing state was not changed")
            if previous.get("scope") != scope and self.store.list_tasks():
                raise ValueError("Changing scope on an existing baseline requires a new project directory; the existing state was not changed")

        # Reject incompatible identities before creating sources, rebinding the
        # configuration, or modifying SQLite. A larger execution budget remains
        # compatible with the same source inventory and task identities.
        check_existing_identity(hashlib.sha256(Path(path).read_bytes()).hexdigest())
        manifest = ingest_spec(path, self.root / ".loopx" / "chip-verification" / "specs", chunk_chars)
        check_existing_identity(manifest["spec_sha256"])
        baseline = self.store.initialize(name, path, manifest["spec_sha256"], manifest["spec_bytes"])
        config = {"name": name, "scope": scope, "manifest_path": manifest["manifest_path"],
                  "spec_sha256": manifest["spec_sha256"], "context_chars": context_chars}
        if previous.get("engineering_enabled"):
            config["engineering_enabled"] = True
        save_json(self.config_path, config)
        for source in iter_sources(manifest):
            metadata = {k: source[k] for k in ("start_char", "end_char", "revision", "value_kind", "metadata", "boundary")}
            self.store.add_source(source["pointer"], source["text"], metadata, source_id=source["source_id"])
        for task in manifest["extraction_tasks"]:
            payload = {**task["payload"], "scope": scope}
            self.store.add_task("model", task["title"], payload, task_id=task["task_id"])
        self._finish_bundle_metadata()
        from .hierarchy import synchronize
        hierarchy = synchronize(self)
        return {"baseline": baseline, "spec_bytes": manifest["spec_bytes"],
                "sources": manifest["source_count"], "model_tasks": len(manifest["extraction_tasks"]),
                "design_map": hierarchy,
                "inventory": audit_manifest(manifest), "semantic_status": "requires_bounded_interpretation"}

    def _finish_bundle_metadata(self):
        """A local schema disposition, recorded as evidence, never as an AI turn."""
        for task in self.store.list_tasks(status="pending", kind="model"):
            if task["payload"].get("execution") != "local_bundle_metadata":
                continue
            attempt = self.store.claim(task["id"], "local-bundle-inventory", lease_seconds=180)
            directory = self.root / ".loopx" / "chip-verification" / "attempts" / attempt["id"]
            directory.mkdir(parents=True, exist_ok=True)
            result = {"requirements": [], "unresolved": [], "cross_references": [],
                      "dispositions": [{"source_id": sid, "classification": "metadata",
                                        "reason": "Explicit dvflow.spec_bundle wrapper metadata; original document body is interpreted separately."}
                                       for sid in task["payload"]["source_ids"]]}
            path = directory / "local_dispositions.json"
            save_json(path, result)
            try:
                self.store.finish(task["id"], attempt["id"], result, artifacts=[path], validator=self._validate_bundle_metadata)
            except Exception as exc:
                self.store.fail(task["id"], attempt["id"], str(exc), retryable=True)
                raise

    def _validate_bundle_metadata(self, task, result, artifacts):
        from .bundles import validate_bundle, source_metadata
        self._validate_model(task, result, artifacts)
        manifest = load_manifest(self.config["manifest_path"])
        bundle = validate_bundle(json.loads(Path(manifest["snapshot_path"]).read_text(encoding="utf-8-sig")))
        if bundle is None or result["requirements"] or result["unresolved"] or result["cross_references"]:
            raise ValidationError("Local metadata classification cannot create semantics")
        allowed = set(task["payload"]["source_ids"])
        for source in iter_sources(manifest):
            if source["source_id"] in allowed and source_metadata(bundle, source["pointer"])["source_role"] != "bundle_metadata":
                raise ValidationError("Document content cannot be classified by the metadata adapter")
        if any(entry["classification"] != "metadata" for entry in result["dispositions"]):
            raise ValidationError("Metadata adapter may only classify metadata")
        return {"passed": True, "validator": "bundle_metadata_v1", "checks": ["explicit_schema", "all_wrappers_accounted", "no_body_skipped"],
                "semantic_claim": "none", "agent_session": False}

    def _validate_model(self, task, result, artifacts):
        self.assert_inputs_current()
        if task["payload"].get("scope") != self.config["scope"]:
            raise ConflictError("Verification scope changed while the agent was running")
        jsonschema.validate(result, EXTRACTION_SCHEMA)
        owned = set(task["payload"]["source_ids"])
        allowed = owned | set(task["payload"].get("dependency_source_ids", []))
        adjudications = task["payload"].get("acceptance", {}).get("source_adjudication_sources", {})
        dispositions = [entry["source_id"] for entry in result["dispositions"]]
        if set(dispositions) != owned or len(dispositions) != len(owned):
            raise ValidationError("Every input source needs exactly one disposition")

        def validate_references(references, label):
            cited = set(references)
            if not cited <= allowed or not cited.intersection(owned):
                raise ValidationError(label + " has sources outside this task or lacks owned source evidence")
            required = {dependency for source in cited.intersection(owned)
                        for dependency in adjudications.get(source, [])}
            if not required <= cited:
                raise ValidationError(label + " must cite the applicable source adjudication dependencies")

        local_ids = set()
        for requirement in result["requirements"]:
            if requirement["local_id"] in local_ids or not requirement["local_id"].strip():
                raise ValidationError("Requirement local IDs must be unique and nonempty")
            local_ids.add(requirement["local_id"])
            if not requirement["statement"].strip() or not requirement["source_ids"]:
                raise ValidationError("Requirement needs a statement and source references")
            validate_references(requirement["source_ids"], "Requirement")
            if requirement["applicability"] == "in_scope" and not requirement["expected"].strip():
                raise ValidationError("In-scope requirement needs an expected behavior")
            for key in ("modules", "features", "interfaces", "conditions", "exceptions"):
                if any(not value.strip() for value in requirement[key]):
                    raise ValidationError("Empty semantic field: " + key)
        for entry in result["dispositions"]:
            if not entry["reason"].strip():
                raise ValidationError("Source disposition requires a reason")
            if entry["classification"] == "requirements" and not any(
                    entry["source_id"] in req["source_ids"] for req in result["requirements"]):
                raise ValidationError("Source classified as requirements but no requirements were extracted")
        for issue in result["unresolved"]:
            validate_references(issue["source_ids"], "Unresolved issue")
        for reference in result["cross_references"]:
            if reference["from_source_id"] not in allowed:
                raise ValidationError("Cross reference source is outside this task")
        return {"passed": True, "validator": "source_traceability_v1",
                "checks": ["schema_valid", "all_sources_accounted", "source_references_valid"],
                "semantic_claim": "candidate_requirements_only"}

    def _validate_plan(self, task, result, artifacts):
        self.assert_inputs_current()
        if task["payload"].get("scope") != self.config["scope"]:
            raise ConflictError("Verification scope changed while the agent was running")
        jsonschema.validate(result, PLAN_SCHEMA)
        requirements = task["payload"]["requirements"]
        allowed = {r["id"] for r in requirements}
        needed = {r["id"] for r in requirements if r["applicability"] == "in_scope"}
        mapped = set()
        ids = set()
        for point in result["testpoints"]:
            if point["id"] in ids or not point["id"].strip():
                raise ValidationError("Testpoint IDs must be unique and nonempty")
            ids.add(point["id"])
            if not point["requirement_ids"] or not set(point["requirement_ids"]) <= allowed:
                raise ValidationError("Testpoint has invalid requirement references")
            if (not point["stimulus"].strip() or
                    not all(point[k] and all(value.strip() for value in point[k])
                            for k in ("observations", "checkers", "coverage", "uvm_components"))):
                raise ValidationError("Testpoint needs stimulus, observation, checking and coverage")
            mapped.update(point["requirement_ids"])
        for issue in result["unresolved"]:
            if not issue["requirement_ids"] or not set(issue["requirement_ids"]) <= allowed:
                raise ValidationError("Plan issue has invalid requirement references")
            mapped.update(issue["requirement_ids"])
        if not needed <= mapped:
            raise ValidationError("In-scope requirements are silently missing from the plan")
        return {"passed": True, "validator": "uvm_plan_mapping_v1",
                "checks": ["schema_valid", "in_scope_requirements_accounted", "stimulus_checker_coverage_separated"]}

    def _plan_context(self, task_id, requirements):
        config = self.config
        pack = {"task_id": task_id, "scope": config["scope"],
                "spec_sha256": config["spec_sha256"], "requirements": requirements}
        return PLAN_INSTRUCTION + "\n" + json.dumps(pack, ensure_ascii=False), pack

    def _materialize_plan(self, model, requirements):
        """Keep legacy identities when they fit; partition complete requirements."""
        config = self.config
        identifier = "plan_" + model["id"]
        payload = {"requirements": requirements, "scope": config["scope"],
                   "source_ids": model["payload"]["source_ids"], "spec_sha256": config["spec_sha256"]}
        original = self.store.add_task("plan", "Plan UVM tests for " + model["title"], payload,
                                       depends_on=[model["id"]], task_id=identifier)
        # Accepted documents are immutable evidence. A concurrent worker owns
        # its active claim; reconciliation does not cancel that computation.
        if original["status"] in {"done", "running"}:
            return 1
        budget = config["context_chars"]
        if original["status"] == "cancelled":
            tasks = self.store.list_tasks()
            if identifier in superseded_plan_ids(tasks):
                partition = original["reason"].removeprefix("Superseded by bounded plan partition ")
                parts = [task for task in tasks if task["payload"].get("supersedes") == identifier and
                         task["payload"].get("partition_id") == partition]
                # Changing a context limit must not duplicate accepted plans.
                # Preserve the original partition and release its blocked item
                # once an explicit larger budget fits the complete input.
                for child in parts:
                    chars = len(self._plan_context(child["id"], child["payload"]["requirements"])[0])
                    if chars <= budget and child["status"] == "blocked" and (child["reason"] or "").startswith("Planning context exceeds budget:"):
                        self.store.requeue(child["id"])
                        for issue in self.store.list_issues():
                            if issue["task_id"] == child["id"] and issue["category"] == "context_blocker":
                                self.store.resolve_issue(issue["id"], "The current context budget now fits the complete requirement batch.")
                    elif chars > budget and child["status"] in {"pending", "failed"}:
                        reason = f"Planning context exceeds budget: this retained requirement batch needs {chars} characters, budget {budget}. Restore its prior budget or refine the scope; no text was truncated."
                        self.store.block(child["id"], reason)
                        self.store.add_issue("context_blocker", reason, task_id=child["id"])
                return len(parts)
            if not (original["reason"] or "").startswith("Superseded by bounded plan partition "):
                return 1  # Preserve an explicit operator cancellation.
        if len(self._plan_context(identifier, requirements)[0]) <= budget:
            if original["status"] == "blocked" and (original["reason"] or "").startswith("Planning context exceeds budget:"):
                self.store.requeue(identifier)
                for issue in self.store.list_issues():
                    if issue["task_id"] == identifier and issue["category"] == "context_blocker":
                        self.store.resolve_issue(issue["id"], "The current context budget now fits this complete requirement.")
            return 1

        # The fingerprint includes the budget because it determines batch
        # boundaries. IDs use a fixed-width index so the size calculation and
        # eventual prompt are identical, including the task identity.
        partition = hashlib.sha256(json.dumps({"requirements": requirements, "budget": budget},
                                              ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        def part_id(index):
            return identifier + "__part_" + partition + "_" + f"{index:06d}"

        batches, batch = [], []
        for requirement in requirements:
            trial = batch + [requirement]
            if batch and len(self._plan_context(part_id(len(batches)), trial)[0]) > budget:
                batches.append(batch)
                batch = []
            batch.append(requirement)
        if batch:
            batches.append(batch)

        if len(batches) == 1:
            # One requirement cannot be split without changing its semantics.
            chars = len(self._plan_context(identifier, requirements)[0])
            reason = f"Planning context exceeds budget: one complete requirement needs {chars} characters, budget {budget}. Refine its semantics or explicitly increase the budget; no text was truncated."
            if original["status"] != "blocked" or original["reason"] != reason:
                self.store.block(identifier, reason)
            self.store.add_issue("context_blocker", reason, task_id=identifier)
            return 1

        for index, items in enumerate(batches):
            child_id = part_id(index)
            child = self.store.add_task("plan", f"Plan UVM tests for {model['title']} ({index + 1}/{len(batches)})",
                {**payload, "requirements": items, "supersedes": identifier, "partition_id": partition,
                 "partition_index": index, "partition_count": len(batches)},
                depends_on=[model["id"]], task_id=child_id)
            chars = len(self._plan_context(child_id, items)[0])
            if chars > budget:
                reason = f"Planning context exceeds budget: complete requirement {items[0]['id']} needs {chars} characters, budget {budget}. Refine its semantics or explicitly increase the budget; no text was truncated."
                if child["status"] not in {"done", "running"} and (child["status"] != "blocked" or child["reason"] != reason):
                    self.store.block(child_id, reason)
                self.store.add_issue("context_blocker", reason, task_id=child_id)
        # Cancel only after the full replacement set is durable. A crash before
        # this point is repaired by the same deterministic reconciliation.
        reason = "Superseded by bounded plan partition " + partition
        current = self.store.get_task(identifier)
        if current["status"] not in {"done", "running"} and (current["status"] != "cancelled" or current["reason"] != reason):
            self.store.cancel(identifier, reason)
        return len(batches)

    def reconcile(self):
        """Idempotently materialize derived records after a crash during writeback."""
        self.assert_inputs_current()
        self._finish_bundle_metadata()
        created = 0
        for task in self.store.list_tasks(status="done", kind="model"):
            result = task["result"]
            for issue in result["unresolved"]:
                self.store.add_issue("spec_blocker" if issue["blocking"] else "spec_question",
                                     issue["question"], task_id=task["id"])
            for reference in result["cross_references"]:
                self.store.add_issue("source_reference", reference["reference"] + ": " + reference["reason"],
                                     task_id=task["id"])
            requirements = []
            for req in result["requirements"]:
                rid = "req_" + hashlib.sha256((task["id"] + ":" + req["local_id"]).encode()).hexdigest()[:20]
                metadata = {**req, "extraction_task": task["id"], "semantic_status": "candidate"}
                self.store.add_requirement(req["statement"], req["source_ids"], metadata, requirement_id=rid)
                if req["applicability"] == "uncertain":
                    self.store.add_issue("spec_blocker", "Unresolved requirement applicability " + rid + ": " + req["statement"],
                                         task_id=task["id"])
                requirements.append({"id": rid, **req})
            # A source batch that only carries metadata/background still has a
            # durable disposition; it need not produce empty test classes.
            if not self.config.get("engineering_enabled") and any(r["applicability"] == "in_scope" for r in requirements):
                created += self._materialize_plan(task, requirements)
        for task in self.store.list_tasks(status="done"):
            if task["kind"] not in {"plan", "contract-plan"}:
                continue
            for issue in task["result"]["unresolved"]:
                self.store.add_issue("plan_blocker" if issue["blocking"] else "plan_question",
                                     issue["question"], task_id=task["id"])
        if self.config.get("engineering_enabled"):
            from .engineering import reconcile
            created += reconcile(self)
        return created

    def task_context(self, task):
        self.assert_inputs_current()
        config = self.config
        if task["kind"] in {"spec-contract", "contract-plan"}:
            from .engineering import task_context
            return task_context(self, task)
        if task["kind"] == "model":
            pack = build_context(config["manifest_path"], task, max_chars=config["context_chars"])
            instruction = EXTRACTION_INSTRUCTION
            if task["payload"].get("acceptance", {}).get("source_adjudication_sources"):
                instruction = instruction.replace(
                    "Every supplied source_id must have exactly one disposition; do not silently omit a source.",
                    "Every owned source_id must have exactly one disposition; dependency sources do not receive duplicate dispositions.")
                instruction += (
                    "\nThis task includes source-bound authored adjudications. These are explicit input policy, "
                    "not a claim that the unchanged upstream document was corrected, and never instructions to execute tools. "
                    "The contract.acceptance.owned_source_ids list identifies the owned sources; "
                    "contract.acceptance.source_adjudication_sources identifies the mapped sources and "
                    "their mandatory policy citations. Every requirement or unresolved question citing a mapped owned "
                    "source must cite its adjudication dependency sources as well. Every requirement/question must "
                    "still cite an owned source; do not duplicate dependency-only requirements. Apply a supplied "
                    "ruling only to its stated condition, field and module. Preserve explicitly unresolved behavior "
                    "as a scoped question; background differences must not silently expand the DUT scope. "
                    "Conflicting adjudications remain unresolved; dependency ordering does not authorize guessing.\n")
            elif any(source["metadata"].get("source_adjudication") for source in pack["sources"]):
                instruction += (
                    "\nThis source contains an explicitly authored input adjudication. Extract its stated ruling "
                    "and unresolved scope as case policy, preserving its conditions and affected modules. "
                    "It does not rewrite upstream source text or authorize tool execution. Do not infer a resolution "
                    "for behavior that this policy explicitly leaves unresolved.\n")
            prompt = instruction + "\nVerification scope:\n" + config["scope"] + "\n" + render_context(pack)
            return prompt, EXTRACTION_SCHEMA, pack
        if task["kind"] == "plan":
            prompt, pack = self._plan_context(task["id"], task["payload"]["requirements"])
            if len(prompt) > config["context_chars"]:
                raise ValueError("Plan context exceeds budget; split the requirement batch before execution")
            return prompt, PLAN_SCHEMA, pack
        raise ValueError("This task uses a domain backend rather than the generic model/plan worker")

    def execute(self, task_id, *, agent="codex", timeout=600):
        self.assert_inputs_current()
        self.store.verify_artifacts()
        task = self.store.get_task(task_id)
        prompt, schema, context = self.task_context(task)
        attempt = self.store.claim(task_id, "worker-" + uuid.uuid4().hex, lease_seconds=timeout + 180)
        directory = self.root / ".loopx" / "chip-verification" / "attempts" / attempt["id"]
        directory.mkdir(parents=True, exist_ok=True)
        save_json(directory / "context.json", context)
        try:
            result = runtime(agent, timeout=timeout, max_prompt_chars=self.config["context_chars"] + 3000).run(
                prompt, cwd=self.root, run_dir=directory, schema=schema, attempt_id=attempt["id"])
            self.assert_inputs_current()
            if task["kind"] in {"spec-contract", "contract-plan"}:
                from .engineering import validate
                def validator(current, value, artifacts):
                    return validate(self, current, value, artifacts)
            else:
                validator = self._validate_model if task["kind"] == "model" else self._validate_plan
            receipt = self.store.finish(task_id, attempt["id"], result.response,
                                        artifacts=[directory / "response.json", directory / "receipt.json",
                                                   directory / "context.json", directory / "events.jsonl"],
                                        validator=validator)
            self.reconcile()
            return {"task_id": task_id, "status": "done", "session_id": result.session_id,
                    "prompt_chars": result.prompt_chars, "usage": result.usage, "receipt": receipt}
        except Exception as exc:
            try:
                self.store.fail(task_id, attempt["id"], str(exc), retryable=True)
            except ConflictError:
                pass
            return {"task_id": task_id, "status": "failed", "error": str(exc)}

    def work(self, *, agent="codex", kinds=None, max_tasks=1, parallel=1, timeout=600, notify=None, focus=None):
        self.store.recover_expired()
        self.reconcile()
        finished = []
        while len(finished) < max_tasks:
            default_kinds = ["model", "spec-contract", "contract-plan"] if self.config.get("engineering_enabled") else ["model", "plan"]
            ready = [t for t in self.store.ready_tasks(100000)
                     if t["kind"] in (kinds or default_kinds)]
            from .hierarchy import focused_tasks
            ready = focused_tasks(self, ready, focus)
            batch = ready[:min(parallel, max_tasks - len(finished))]
            if not batch:
                break
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                futures = {pool.submit(self.execute, t["id"], agent=agent, timeout=timeout): t for t in batch}
                for future in as_completed(futures):
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {"task_id": futures[future]["id"], "status": "failed", "error": str(exc)}
                    finished.append(result)
                    if notify:
                        notify(result)
            if any(r["status"] == "failed" for r in finished[-len(batch):]):
                break
        return finished

    def export_plan(self, output: str | Path | None = None):
        self.assert_inputs_current()
        self.store.verify_artifacts()
        self.reconcile()
        engineering = self.config.get("engineering_enabled")
        plans = self.store.list_tasks(status="done", kind="contract-plan" if engineering else "plan")
        points, unresolved = [], []
        for task in plans:
            for point in task["result"]["testpoints"]:
                points.append({**point, "id": task["id"] + ":" + point["id"], "task_id": task["id"]})
            unresolved.extend(task["result"]["unresolved"])
        if engineering:
            from .engineering import superseded_task_ids
            superseded = superseded_task_ids(self)
            planning = [task for task in self.store.list_tasks()
                        if task["kind"] in {"model", "spec-contract", "contract-plan"} and task["id"] not in superseded]
        else:
            planning = effective_planning_tasks(self.store.list_tasks())
        value = {"schema_version": 1, "spec_sha256": self.config["spec_sha256"],
                 "scope": self.config["scope"], "testpoints": points, "unresolved": unresolved,
                 "planning_complete": bool(planning) and (not engineering or bool(plans) and bool(points))
                                      and all(t["status"] == "done" for t in planning),
                 "execution_claim": "No simulation or coverage claim is implied by this plan"}
        value["source_questions"] = [issue for issue in self.store.list_issues()
                                     if not engineering or issue["task_id"] not in superseded]
        value["semantic_review_complete"] = not any(issue["status"] == "open" and issue["category"] in {
            "spec_blocker", "source_reference", "plan_blocker", "context_blocker", "contract_blocker"}
                                                     for issue in value["source_questions"])
        value["semantic_review_complete"] = value["semantic_review_complete"] and not any(
            issue["blocking"] for issue in unresolved)
        value["planning_complete"] = value["planning_complete"] and value["semantic_review_complete"]
        target = Path(output) if output else self.root / "tp.json"
        save_json(target, value)
        return {"path": str(target.resolve()), "testpoints": len(points),
                "planning_complete": value["planning_complete"]}

    def status(self):
        if not self.config_path.is_file():
            return {**self.store.status(), "changed_artifacts": [], "next_tasks": [],
                    "next_action": "import", "instruction": "Provide a spec.json path to create bounded DV work."}
        changed = self.store.verify_artifacts()
        self.store.recover_expired()
        self.reconcile()
        status = self.store.status()
        status["changed_artifacts"] = changed
        status["scope"] = self.config["scope"]
        status["context_budget_chars"] = self.config["context_chars"]
        status["next_tasks"] = [{k: t[k] for k in ("id", "kind", "title")} for t in self.store.ready_tasks(5)]
        return status

    def assert_inputs_current(self):
        config = self.config
        manifest = load_manifest(config["manifest_path"])
        baseline = self.store.baseline()
        if not (baseline["spec_hash"] == config["spec_sha256"] == manifest["spec_sha256"]):
            raise ConflictError("State, source inventory and project configuration name different baselines")
        original = Path(manifest["spec_path"])
        if not original.is_file() or hashlib.sha256(original.read_bytes()).hexdigest() != config["spec_sha256"]:
            raise ConflictError("Original spec changed or disappeared; re-import to create a new baseline")
        audit_manifest(manifest)
