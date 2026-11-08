"""Bounded, resumable UVM candidate generation for an explicitly supplied DUT.

This module validates contracts, ownership and provenance. It intentionally does
not promote generated source to compiled, simulated, or coverage-verified code.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import uuid

import jsonschema

from .agents import runtime
from .contracts import obj, arr, TEXT, TEXTS
from .models import ConflictError, ValidationError
from .workflow import save_json, effective_planning_tasks


ROLES = ["interface", "transaction", "config", "driver", "monitor", "sequencer", "agent",
         "reference_model", "scoreboard", "assertions", "coverage", "ral", "env", "sequence",
         "virtual_sequence", "test", "tb_top", "package", "build_manifest"]
ROLE = {"type": "string", "enum": ROLES}
_OUTPUT_SUFFIXES = (".sv", ".svh", ".v", ".f", ".tcl", ".md", ".json")
ARCHITECTURE_SCHEMA = obj({
    "top": {"type": "string", "description": "The supplied DUT module identifier (top_declaration.module), not the generated testbench wrapper. The build manifest top names that wrapper separately."},
    "uvm_version": TEXT,
    "interfaces": arr(obj({"name": TEXT, "description": TEXT, "signals": TEXTS})),
    "shared_contracts": arr(obj({"name": TEXT, "description": TEXT})),
    "components": arr(obj({
        "id": TEXT, "kind": ROLE, "responsibilities": arr(ROLE), "purpose": TEXT,
        "depends_on": TEXTS, "dependency_files": TEXTS, "testpoint_ids": TEXTS,
        "output_paths": TEXTS, "contract": TEXT,
    })),
    "omitted_optional": arr(obj({"kind": ROLE, "reason": TEXT})),
    "unresolved": TEXTS,
})
COMPONENT_SCHEMA = obj({"files": arr(obj({"path": TEXT, "content": TEXT})), "notes": TEXTS, "unresolved": TEXTS})

_PROFILE_FACT = obj({"id": TEXT, "statement": TEXT, "source_refs": TEXTS, "locator": TEXT, "value": {}})
_PROFILE_FACT["required"] = ["id", "statement", "source_refs", "locator"]
_PROFILE_CHOICE = obj({"id": TEXT, "statement": TEXT, "rationale": TEXT, "source_refs": TEXTS, "value": {}})
_PROFILE_CHOICE["required"] = ["id", "statement", "rationale", "source_refs"]
PROFILE_SCHEMA = obj({
    "schema_version": {"const": 1}, "top": TEXT, "uvm_version": TEXT,
    "sources": arr(obj({"id": TEXT, "path": TEXT, "sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"}, "purpose": TEXT})),
    "documented_design_facts": arr(_PROFILE_FACT), "rtl_bindings": arr(_PROFILE_FACT),
    "testbench_choices": arr(_PROFILE_CHOICE),
})

ARCHITECTURE_INSTRUCTION = """Design ONE coherent UVM testbench blueprint from the supplied DUT declaration, complete selected testpoints, related requirements and verification profile.
Do not use tools, read a complete spec/testplan, edit files, or replace the RTL. Return only the requested JSON.
Set blueprint.top to top_declaration.module: this is the DUT. Name the generated TB wrapper separately.
Follow build_manifest_contract for the execution target and supported JSON manifest. Only .sv, .svh, .v, .f,
.tcl, .md and .json output suffixes are supported; do not plan Questa .do or shell scripts.
Use interface-oriented agents and reusable transaction/configuration contracts. Separate stimulus, observations,
independent correctness checking and functional coverage. Distinguish testpoints, sequences and uvm_test classes.
Plan all needed files: shared interface/types/config, UVCs, scoreboard or assertions, coverage, env, sequences,
tests, TB top and build manifest. Related roles may be grouped into one bounded component. Mark optional RAL,
virtual sequences or reference models omitted with reasons when inapplicable; do not create empty classes.
Every testpoint must map to stimulus/test, checking, and coverage responsibilities. Component dependencies must
be acyclic. A component owns every output_path exclusively. Use simple relative POSIX paths and small components.
dependency_files lists exact logical output paths whose contents are needed from prior components. Shared
interface/transaction/config/package dependencies will also be supplied automatically. Distinguish profile
documented_design_facts (documented behavior), rtl_bindings (implementation/configuration binding, not an
independent behavioral oracle), and testbench_choices (authorized simulation choices, not DUT guarantees).
Use provided clock period, reset stimulus, memory ABI and simulator choices; do not block merely because such
testbench choices are not normative spec requirements. Make additional harmless testbench-local choices explicit
in shared_contracts. Never invent missing DUT behavior. Put essential design ambiguities in unresolved.
Candidate validation is not compilation.
When shared_specification is supplied, generate against its declared interface before RTL exists.
Pending RTL file binding, elaboration, lint or CDC are later join obligations, not missing
specification and not blueprint blockers. Record deferred tool obligations in shared_contracts;
do not claim that UVM pin observations establish internal structural properties. Keep unresolved
for essential undefined behavior/interface contradictions. The join supplies actual RTL paths.
"""
COMPONENT_INSTRUCTION = """Implement ONE UVM component from the accepted blueprint and supplied testpoint excerpts.
Do not use tools or read whole specs, testplans, RTL, or other sessions. Return files as JSON, without Markdown fences.
Only write the assigned logical paths. Preserve the shared interface/type/config contracts and component names.
Implement behavior and checks rather than TODO or empty placeholder scaffolding. Monitors observe actual DUT
transactions; checking must be independent of the stimulus; coverage samples defined observed events.
Do not change the DUT, weaken expected behavior, disable assertions, or remove coverage goals. Respect the selected
UVM version, package ordering, config_db paths, agent modes, TLM connections and test objection ownership.
Use the complete mapped requirements and selected verification profile. Keep documented design behavior, RTL
bindings and testbench choices distinct; configuration choices do not become independent correctness oracles.
Use exactly the assigned file paths. If an essential design detail is missing, return an explicit unresolved item rather
than guessing. These are candidate sources; never claim compile/simulation success or a coverage percentage.
For shared-specification generation, RTL is deliberately late-bound by join-design. A build manifest
may name its declared DUT path for later binding; do not create a fake DUT. Pending static tools are
separate obligations; implement observable functional checks without claiming structural closure.
Follow build_manifest_contract. A JSON build manifest names the TB wrapper as top, while the blueprint
top remains the DUT. A .f file alone is not the executable JSON build manifest. Preserve assigned paths;
an existing blueprint may leave JSON assembly to the host rather than permit an unowned extra file.
Known downstream manifest assembly, RTL binding and tool execution are not unresolved items for a
component that implements its assigned contract. Put those deferred obligations in notes. Reserve
unresolved for a concrete missing specification or dependency needed to implement the owned files;
identify that missing fact precisely rather than requiring downstream steps to finish first.
"""



class GenerationBlocked(ValidationError):
    """A candidate reports missing design/interface contracts, not a transient error."""
    def __init__(self, message, issues):
        super().__init__(message)
        self.issues = issues


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_encoded(value).encode("utf-8")).hexdigest()


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def simulation_target(value):
    """Normalize an explicit capability target without choosing a fallback."""
    aliases = {
        "vivado": "vivado_xsim", "xsim": "vivado_xsim", "vivado_xsim": "vivado_xsim",
        "vivado xsim": "vivado_xsim", "cadence": "cadence_xcelium", "xcelium": "cadence_xcelium",
        "cadence_xcelium": "cadence_xcelium", "cadence xcelium": "cadence_xcelium",
    }
    if not isinstance(value, str) or value.strip().lower() not in aliases:
        raise ValidationError("Unsupported simulator target; select Vivado XSim or external Cadence Xcelium explicitly")
    return aliases[value.strip().lower()]


def _build_manifest_contract(basis, profile):
    """Project a generation target; external Cadence work is not tool acceptance."""
    from .simulators.generic import _FIELDS

    targets = set()
    for section in ("documented_design_facts", "rtl_bindings", "testbench_choices"):
        for item in (profile or {}).get(section, []):
            value = item.get("value")
            if not isinstance(value, dict) or "simulator" not in value:
                continue
            try:
                targets.add(simulation_target(value["simulator"]))
            except ValidationError as error:
                question = "Verification profile item " + item["id"] + " selects an unsupported simulator. " + str(error)
                raise GenerationBlocked(question, [question])
    if len(targets) > 1:
        question = "Verification profile has conflicting simulator targets; reconcile the supplied choices before generation."
        raise GenerationBlocked(question, [question])
    target = next(iter(targets), "vivado_xsim")
    late_bound = bool(basis.get("shared_specification"))
    supplied_rtl = [item["path"] for item in basis["rtl"]]
    # This is an example, not the authoritative file inventory. Large existing
    # RTL inventories stay in the accepted task basis and must be bound locally.
    inline_rtl = not late_bound and len(_encoded(supplied_rtl)) <= 1000
    rtl = supplied_rtl if inline_rtl else []
    contract = {
        "simulator": "vivado_xsim", "schema_version": 1,
        "allowed_fields": sorted(_FIELDS), "provider_owned_fields": ["engineering_binding"],
        "dut_top": basis["top"], "output_suffixes": list(_OUTPUT_SUFFIXES),
        "rules": [
            "Emit a .json manifest for this backend; a .f list or Questa .do script is insufficient.",
            "candidate_dir is relative to the manifest's parent. All listed relative paths are relative to candidate_dir. Use '.' when the manifest is at the output root.",
            "top names the generated TB wrapper module; testname names its registered uvm_test class. Replace the example names and file list with the actual planned ones.",
            "tb_files contains each .sv/.v translation unit once, in dependency order. Do not also compile .svh files included by a package; list their directories in include_dirs.",
            "compile_order lists each RTL and TB translation unit once. In a late-binding template, order TB units only; join-design inserts actual RTL first.",
            "Use UVM_NO_DPI with this backend. Do not introduce DUT defines, omit checks, or disable assertions to make a run pass.",
            "Print the activity marker from actual monitor/scoreboard counters at report/end of test: checks counts comparisons executed and transactions counts observed DUT transfers. Never print fabricated constants or infer coverage percentage.",
            "engineering_binding is written by join-design. Omit optional tool paths/extra_inputs/coverage_manifest unless real provided artifacts require them; do not invent paths or coverage evidence.",
        ],
        "rtl_binding": ("rtl_files=[] is a preparation template before join-design, not a runnable build. Missing RTL is not a TB-generation blocker; never create a fake DUT."
                        if late_bound else "Use the complete actual RTL inventory in the generation basis; do not generate or replace DUT files. An empty example inventory means the host must bind that complete inventory before build."),
        "rtl_inventory": {"file_count": len(supplied_rtl), "sha256": _hash(basis["rtl"]), "included_in_example": inline_rtl},
        "template": {"schema_version": 1, "candidate_dir": ".", "top": "tb_top", "rtl_files": rtl,
                     "tb_files": ["tb_top.sv"], "compile_order": [*rtl, "tb_top.sv"], "include_dirs": [],
                     "defines": ["UVM_NO_DPI"], "uvm_no_dpi": True, "testname": "smoke_test", "runtime": "all",
                     "seed": 1, "timescale": "1ns/1ps",
                     "activity_contract": {"pattern": r"LOOPX_ACTIVITY checks=(?P<checks>[0-9]+) transactions=(?P<transactions>[0-9]+)",
                                           "minimums": {"checks": 1, "transactions": 1}}},
    }
    if target == "cadence_xcelium":
        # Keep the generated JSON consumable by the public external kit. Do not
        # send it through XSim's schema or require its DPI compatibility define.
        cadence_fields = (_FIELDS - {"uvm_no_dpi", "uvm_dir", "vivado_home", "coverage_manifest", "engineering_binding"}) | {
            "simulator", "execution", "dut_top", "coverage_model", "constraints", "scripts",
        }
        contract.update(
            simulator=target, execution="external", allowed_fields=sorted(cadence_fields),
            provider_owned_fields=[],
            rules=[
                "Emit a JSON Cadence manifest with simulator='cadence_xcelium' and execution='external'. Use ordinary SystemVerilog RTL, SV UVM and SVA with a complete, explicitly ordered source closure.",
                "candidate_dir is relative to the manifest's parent; listed paths are relative to candidate_dir. top is the TB wrapper; dut_top is the design module; testname is a registered uvm_test class.",
                "compile_order lists each RTL/TB translation unit once in dependency order. Headers included by a package belong in include_dirs and extra_inputs, not separate translation units. Preserve all interfaces, defines and required memory data.",
                "Use the selected Cadence installation's native UVM library. Do not force UVM_NO_DPI, invent a UVM source path, disable assertions, or import a Vivado build setting.",
                "Generate the reviewed frozen functional-bin contract named by coverage_model: schema_version=1, metric='functional', target_percent=90, and nonempty bins with unique id and nonempty requirement_ids arrays. Define sampling and exclusions from the accepted testplan; an absent model or incomplete denominator cannot earn coverage credit.",
                "constraints lists supplied clock/reset/SDC/formal inputs; scripts names supplied site-qualified IMC, Jasper and Genus scripts. Do not invent their contents or claim unknown tool commands were verified.",
                "Record the actual seed, timescale, testname and complete inputs. Report monitor/scoreboard activity from real counters, never fixed success constants. Functional coverage, code coverage, assertions and formal proof are separate evidence.",
                "Use the staged eda/cadence_runner.py inspect|plan|run --project <manifest> on the Cadence host; run requires --execute to launch a tool. This manifest is a generated candidate; no tool has executed merely because it validates.",
                "The current native check-design/join-design path is Vivado-specific. For this external target, bind the accepted RTL/TB/shared-contract revision and full source closure in the Cadence manifest; preserve unresolved composition/check obligations. Event completion is observation, not signoff.",
            ],
            rtl_binding=("rtl_files=[] is a preparation template before external RTL binding, not a runnable build. Bind every accepted RTL file and the shared contract/configuration revision before Cadence execution; never create a fake DUT."
                         if late_bound else "Use the complete actual RTL inventory in the generation basis; do not generate or replace DUT files. Bind every file before external execution."),
        )
        template = contract["template"]
        template.pop("uvm_no_dpi")
        cadence_rtl = [] if late_bound else supplied_rtl
        contract["rtl_inventory"]["included_in_example"] = not late_bound
        template.update(simulator=target, execution="external", dut_top=basis["top"], defines=[],
                        rtl_files=cadence_rtl, compile_order=[*cadence_rtl, "tb_top.sv"],
                        coverage_model="coverage_model.json", constraints=[], scripts={}, extra_inputs=[])
    return contract


def load_generation_profile(project, profile_path, top):
    """Load an explicit agent-prepared input artifact; never read its full sources into a prompt.

    Source paths are relative to the profile directory or explicitly absolute.
    Digests establish provenance/version identity, not truth of a paraphrased fact.
    """
    if profile_path is None:
        return None
    path = Path(profile_path)
    path = (path if path.is_absolute() else project.root / path).resolve()
    if not path.is_file():
        raise ValidationError("Verification profile does not identify an existing file")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValidationError("Verification profile contains duplicate JSON keys")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValidationError("Verification profile contains a non-JSON numeric constant: " + value)

    raw = path.read_bytes()
    try:
        profile = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object, parse_constant=invalid_constant)
        jsonschema.validate(profile, PROFILE_SCHEMA)
    except (UnicodeDecodeError, json.JSONDecodeError, jsonschema.ValidationError) as error:
        raise ValidationError("Invalid verification profile: " + str(error)) from error
    if profile["top"] != top or not profile["uvm_version"].strip():
        raise ValidationError("Verification profile top must match the requested DUT and UVM version must be explicit")
    source_ids, source_paths = set(), set()
    normalized_sources = []
    for source in profile["sources"]:
        identifier, source_text = source["id"], source["path"]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", identifier) or identifier in source_ids:
            raise ValidationError("Profile source IDs must be unique simple identifiers")
        if not source_text or any(ord(char) < 32 for char in source_text) or "://" in source_text or not source["purpose"].strip():
            raise ValidationError("Profile sources require local paths and a nonempty purpose")
        candidate = Path(source_text)
        if not candidate.is_absolute() and (candidate.drive or source_text.startswith(("/", "\\"))):
            raise ValidationError("Ambiguous profile source path; use a relative or absolute file path")
        candidate = (candidate if candidate.is_absolute() else path.parent / candidate).resolve()
        if not candidate.is_file() or candidate == path:
            raise ValidationError("Profile source must be an existing file distinct from the profile: " + source_text)
        if str(candidate).casefold() in source_paths:
            raise ValidationError("Profile lists the same source file under multiple identities")
        actual = _file_hash(candidate)
        if actual != source["sha256"].lower():
            raise ValidationError("Profile source SHA-256 mismatch: " + source_text)
        source_ids.add(identifier)
        source_paths.add(str(candidate).casefold())
        normalized_sources.append({**source, "path": str(candidate), "sha256": actual})
    item_ids = set()
    for section in ("documented_design_facts", "rtl_bindings", "testbench_choices"):
        for item in profile[section]:
            if not item["id"].strip() or item["id"] in item_ids or not item["statement"].strip():
                raise ValidationError("Profile facts, bindings and choices need unique IDs and nonempty statements")
            item_ids.add(item["id"])
            refs = item["source_refs"]
            if len(refs) != len(set(refs)) or not set(refs) <= source_ids:
                raise ValidationError("Profile record references an unknown or duplicate source")
            if section == "testbench_choices":
                if not item["rationale"].strip():
                    raise ValidationError("A testbench choice requires an explicit rationale")
            elif not refs or not item["locator"].strip():
                raise ValidationError("Documented behavior and RTL bindings require source references and a locator")
    return {**profile, "sources": normalized_sources, "profile_path": str(path),
            "profile_sha256": hashlib.sha256(raw).hexdigest()}


def _requirement_views(project, points):
    """Preserve complete behavioral fields while excluding unrelated source documents."""
    available = {item["id"]: item for item in project.store.list_requirements()}
    views = {}
    for identifier in sorted({identifier for point in points.values() for identifier in point["requirement_ids"]}):
        if identifier not in available:
            raise ValidationError("Accepted testpoint refers to an absent requirement: " + identifier)
        requirement = available[identifier]
        metadata = requirement["metadata"]
        views[identifier] = {"id": identifier, "statement": requirement["text"],
                             "source_ids": requirement["source_ids"],
                             **{key: metadata[key] for key in ("modules", "features", "interfaces", "conditions", "expected",
                                                               "exceptions", "applicability", "semantic_status", "extraction_task") if key in metadata}}
    return views


def _safe_path(value: str) -> str:
    def reject(message):
        rendered = repr(value)
        if len(rendered) > 160:
            rendered = rendered[:157] + "..."
        raise ValidationError(message + ": " + rendered + "; allowed suffixes: " + ", ".join(_OUTPUT_SUFFIXES))

    if (not isinstance(value, str) or not value or "\\" in value or any(char in value for char in '<>:"|?*') or
            any(ord(character) < 32 for character in value)):
        reject("Generated path must be a nonempty relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or PureWindowsPath(value).is_absolute() or any(part in ("..", ".") for part in value.split("/")):
        reject("Generated path escapes its component output directory")
    if "" in value.split("/") or path.suffix.lower() not in _OUTPUT_SUFFIXES:
        reject("Unsupported or ambiguous generated file path")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
        prefix + suffix for prefix in ("COM", "LPT") for suffix in "123456789¹²³"}
    if any(part.endswith((" ", ".")) or part.split(".", 1)[0].rstrip(" ").upper() in reserved for part in path.parts):
        reject("Generated path contains a reserved or aliased Windows filename")
    return path.as_posix()


def _within(root: Path, relative: str, boundary: Path) -> Path:
    if not root.resolve().is_relative_to(boundary.resolve()):
        raise ValidationError("Generated directory follows a link outside its generation scope")
    target = (root / _safe_path(relative)).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValidationError("Generated destination follows a link outside the output directory")
    return target


def _strip_comments(text: str) -> str:
    pattern = re.compile(r'"(?:\\.|[^"\\])*"|/\*[\s\S]*?\*/|//[^\n]*')
    return pattern.sub(lambda match: match.group() if match.group().startswith('"') else
                       "".join("\n" if char == "\n" else " " for char in match.group()), text)


def _semicolon(text: str, start: int) -> int:
    depth, quote, escape = 0, False, False
    for index in range(start, len(text)):
        character = text[index]
        if quote:
            if escape:
                escape = False
            elif character == "\\":
                escape = True
            elif character == '"':
                quote = False
        elif character == '"':
            quote = True
        elif character in "([{" :
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif character == ";" and depth == 0:
            return index + 1
    raise ValidationError("RTL module declaration has no balanced terminating semicolon")


def _rtl_view(paths: list[Path], top: str) -> tuple[list[dict], dict]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", top):
        raise ValidationError("Top must be an unescaped Verilog module identifier")
    inventory, matches = [], []
    for path in paths:
        raw = path.read_bytes()
        try:
            original = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValidationError(f"RTL must be UTF-8: {path}") from error
        text = _strip_comments(original)
        declarations = list(re.finditer(r"\bmodule\s+(?:(?:automatic|static)\s+)?([A-Za-z_][A-Za-z0-9_$]*)\b", text))
        inventory.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                          "bytes": len(raw), "modules": [item.group(1) for item in declarations]})
        for declaration in declarations:
            if declaration.group(1) != top:
                continue
            end = _semicolon(text, declaration.end())
            module_end = text.find("endmodule", end)
            if module_end < 0:
                raise ValidationError(f"Top declaration has no endmodule: {path}")
            # Capture ANSI headers plus non-ANSI I/O and parameter declarations.
            ports = []
            for port in re.finditer(r"(?m)^\s*(input|output|inout|parameter)\b", text[end:module_end]):
                start = end + port.start()
                stop = _semicolon(text, start)
                ports.append(text[start:stop].strip())
            matches.append({"module": top, "path": str(path), "line": original.count("\n", 0, declaration.start()) + 1,
                            "header": text[declaration.start():end], "body_declarations": ports,
                            "parser_limitations": "Declaration inventory only; macros/includes are not elaborated. Internal logic is not included. Compilation must validate all generated bindings."})
    if len(matches) != 1:
        raise ValidationError(f"Expected exactly one definition of top {top!r}; found {len(matches)}")
    return inventory, matches[0]


def _plans(project, contract_task_id=None):
    kind = "contract-plan" if project.config.get("engineering_enabled") or contract_task_id else "plan"
    plans = project.store.list_tasks(status="done", kind=kind)
    if contract_task_id:
        plans = [task for task in plans if task["payload"].get("contract_task_id") == contract_task_id]
    points = {}
    for task in plans:
        for point in task["result"].get("testpoints", []):
            reference = task["id"] + ":" + point["id"]
            if reference in points:
                raise ValidationError("Accepted plan contains duplicate testpoint identity")
            points[reference] = {**point, "id": reference, "plan_task_id": task["id"],
                                 **({"contract_task_id": task["payload"]["contract_task_id"]}
                                    if task["payload"].get("contract_task_id") else {})}
    if not points:
        raise ValidationError("UVM generation requires at least one accepted testpoint; run planning first")
    return plans, points


def _scope_entries(project, points):
    """Partition by complete module/function/interface and contract identities.

    These are independently generated bench scopes, not proof of correct
    whole-chip composition. Cross-module testpoints stay intact in one group.
    """
    requirements = {item["id"]: item for item in project.store.list_requirements()}
    requirement_views = _requirement_views(project, points)
    grouped = {}
    for reference, point in sorted(points.items()):
        tags = {name: set() for name in ("modules", "features", "interfaces")}
        for identifier in point["requirement_ids"]:
            metadata = requirements.get(identifier, {}).get("metadata", {})
            for name in tags:
                tags[name].update(value for value in metadata.get(name, []) if isinstance(value, str) and value.strip())
        normalized = {name: sorted(values) for name, values in tags.items()}
        binding = point.get("contract_task_id")
        key = _encoded([normalized, binding])
        grouped.setdefault(key, {"tags": normalized, "contract_task_id": binding, "points": []})["points"].append((reference, point))
    scopes = []

    def semantics(batch):
        related = sorted({identifier for _, point in batch for identifier in point["requirement_ids"]})
        return {"testpoints": [point for _, point in batch],
                "requirements": [requirement_views[identifier] for identifier in related]}

    def add(group, batch):
        refs = [reference for reference, _ in batch]
        chars = len(_encoded([point for _, point in batch]))
        semantic_chars = len(_encoded(semantics(batch)))
        identifier = "scope_" + _hash({"tags": group["tags"], "testpoint_ids": refs})[:20]
        scopes.append({"id": identifier, "tags": group["tags"], "testpoint_ids": refs,
                       "contract_task_id": group.get("contract_task_id"),
                       "plan_task_ids": sorted({point["plan_task_id"] for _, point in batch}),
                       "testpoint_count": len(refs), "testpoint_characters": chars,
                       "semantic_context_characters": semantic_chars,
                       "testpoint_budget": None, "ready": True, "reason": None})

    for _, group in sorted(grouped.items()):
        add(group, group["points"])
    return scopes


def generation_scopes(project, *, refresh=True) -> dict:
    """Return deterministic module/function scopes without complete TP prose."""
    if refresh:
        project.assert_inputs_current()
        project.store.verify_artifacts()
    _, points = _plans(project)
    scopes = _scope_entries(project, points)
    tasks = project.store.list_tasks(kind="implement")
    for scope in scopes:
        sets = {}
        for task in tasks:
            if task["payload"].get("generation_scope_id") == scope["id"]:
                sets.setdefault(task["payload"]["generation_id"], []).append(task)
        scope["candidate_sets"] = [{"generation_id": identifier,
                                    "components_total": len(items),
                                    "components_done": sum(task["status"] == "done" for task in items)}
                                   for identifier, items in sorted(sets.items())]
    return {"schema_version": 1, "testpoint_count": len(points), "scope_count": len(scopes),
            "context_chars": None, "scopes": scopes,
            "composition_status": "Independent generation scopes; cross-scope integration is a separate verification obligation."}


def _semantic_basis(basis):
    """Measurements and a retired admission budget do not identify design work."""
    result = dict(basis)
    result["generation_scope"] = {key: value for key, value in basis["generation_scope"].items()
                                  if key not in {"testpoint_budget", "ready", "reason", "testpoint_characters",
                                                 "semantic_context_characters"}}
    return result


def _snapshot(project, rtl_files, top, scope_id=None, profile_path=None, contract_task_id=None):
    project.assert_inputs_current()
    project.store.verify_artifacts()
    plans, points = _plans(project, contract_task_id)
    scopes = _scope_entries(project, points)
    shared = None
    if contract_task_id:
        from .engineering import get_contract, declaration as contract_declaration
        shared = get_contract(project, contract_task_id)
        if shared["contract"]["top"] != top:
            raise ValidationError("DUT top differs from the shared specification contract")
        inventory = []
        declaration = {"module": top, "path": "shared-contract:" + contract_task_id,
                       "header": contract_declaration(shared), "body_declarations": [],
                       "parser_limitations": "Declared interface contract; actual RTL binding must be checked at the simulation join."}
        if rtl_files:
            inventory, _ = _rtl_view(rtl_files, top)
    else:
        if project.config.get("engineering_enabled"):
            raise ValidationError("Design/DV generation requires a shared contract_task_id")
        inventory, declaration = _rtl_view(rtl_files, top)
    requirements = _requirement_views(project, points)
    profile = load_generation_profile(project, profile_path, top)

    def select(scope):
        selected_points = {reference: points[reference] for reference in scope["testpoint_ids"]}
        selected_plans = [task for task in plans if task["id"] in scope["plan_task_ids"]]
        if shared:
            questions = [item["question"] for task in selected_plans for item in task["result"].get("unresolved", [])
                         if item.get("blocking")]
            if questions:
                raise GenerationBlocked("Selected shared-contract testplan has unresolved verification obligations", questions)
        selected_requirements = {identifier: requirements[identifier] for identifier in sorted({
            identifier for point in selected_points.values() for identifier in point["requirement_ids"]})}
        basis = {"generator_contract_version": 2,
                 "spec_sha256": project.config["spec_sha256"], "scope": project.config["scope"],
                 "top": top, "rtl": inventory, "generation_scope": scope,
                 "requirements": {identifier: _hash(value) for identifier, value in selected_requirements.items()},
                 "verification_profile": None if profile is None else {
                     "path": profile["profile_path"], "sha256": profile["profile_sha256"],
                     "sources": [{key: source[key] for key in ("id", "path", "sha256")} for source in profile["sources"]]},
                 "plans": [{"id": task["id"], "result_sha256": _hash(task["result"])} for task in selected_plans]}
        if shared:
            basis["shared_specification"] = {"task_id": shared["task_id"], "hash": shared["hash"],
                                              "contract": shared["contract"]}
        # Existing accepted/running generations retain their immutable identity
        # only when every semantic source, scope and binding is identical. Old
        # length-created scopes with different testpoint membership never match.
        comparable = _semantic_basis(basis)
        for task in project.store.list_tasks(kind="architecture"):
            previous = task["payload"].get("basis")
            if (previous and task["status"] not in {"stale", "cancelled"}
                    and _semantic_basis(previous) == comparable):
                basis = previous
                break
        return basis, selected_plans, selected_points, declaration, scopes, selected_requirements, profile

    if scope_id is not None:
        matches = [scope for scope in scopes if scope["id"] == scope_id]
        if not matches:
            raise ConflictError("Generation scope is absent or changed; inspect generation_scopes for the current plan")
        selected = matches[0]
    else:
        selected = scopes[0]
        tasks = project.store.list_tasks(kind="implement")
        for scope in scopes:
            fingerprint = _hash(select(scope)[0])
            relevant = [task for task in tasks if task["payload"].get("input_fingerprint") == fingerprint]
            if not relevant or any(task["status"] != "done" for task in relevant):
                selected = scope
                break
    return select(selected)


def _schema_characters(agent, schema):
    # Pi's adapter appends the output schema to its message. Include those exact
    # characters in the observation; this is not a dispatch limit.
    return len("\nReturn only JSON conforming to this schema:\n" + json.dumps(schema)) if agent in {"pi", "host"} else 0


def _component_prompt(instruction, context):
    """Build the complete selected component context without cutting dependencies."""
    return instruction + "\n" + _encoded(context)


def _context_issues(project, task):
    if task["status"] not in {"blocked", "pending"} or (task["status"] == "blocked" and
            not (task["reason"] or "").startswith("uvm_context_blocker:")):
        return []
    records = []
    for issue in project.store.list_issues():
        if issue["task_id"] != task["id"] or issue["category"] != "uvm_context_blocker":
            continue
        try:
            details = json.loads(issue["description"])
        except (ValueError, TypeError):
            continue
        if (details.get("input_fingerprint") == task["payload"]["input_fingerprint"] and
                isinstance(details.get("context_chars"), int)):
            records.append((issue, details))
    return records


def _prepare_dispatch_context(project, task, instruction, context, budget, agent, schema,
                              explicit_budget, validate, dispatcher=None):
    """Resume selected semantic work and retire old length-only holds atomically."""
    selected = getattr(dispatcher, "expected_task_id", None)
    if selected and task["id"] != selected:
        return task, None, None
    budget_issues = _context_issues(project, task)
    if task["status"] != "pending" and not budget_issues and not (dispatcher and dispatcher.owns(task)):
        return task, None, None
    prompt = _component_prompt(instruction, context)
    if budget_issues:
        validate()
        resolution = _encoded({"reason": "Length-based admission was retired; the complete selected semantic context is preserved.",
                               "required_chars": len(prompt) + _schema_characters(agent, schema),
                               "context_chars": None, "input_fingerprint": task["payload"]["input_fingerprint"]})
        task, recovered = project.store.recover_context(task, [issue for issue, _ in budget_issues], resolution)
        if not recovered:
            return task, None, None
    return task, prompt, None


def _ordered_components(blueprint, points, top):
    jsonschema.validate(blueprint, ARCHITECTURE_SCHEMA)
    if blueprint["top"] != top:
        raise ValidationError("Blueprint changes the requested RTL top")
    if blueprint["unresolved"]:
        raise GenerationBlocked("UVM architecture has unresolved blockers: " + "; ".join(blueprint["unresolved"]), blueprint["unresolved"])
    components = blueprint["components"]
    if not 1 <= len(components) <= 64:
        raise ValidationError("Architecture needs between 1 and 64 bounded components")
    by_id, owners, responsibilities = {}, {}, set()
    for component in components:
        identifier = component["id"]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", identifier) or identifier in by_id:
            raise ValidationError("Component IDs must be unique simple identifiers")
        by_id[identifier] = component
        if not component["purpose"].strip() or not component["contract"].strip() or not component["responsibilities"]:
            raise ValidationError("Each UVM component needs purpose, responsibility and contract")
        responsibilities.update(component["responsibilities"])
        if not set(component["testpoint_ids"]) <= points.keys():
            raise ValidationError("Component references an unknown testpoint")
        if not component["output_paths"] or len(component["output_paths"]) > 16:
            raise ValidationError("Each component needs a bounded explicit output-file set")
        for filename in component["output_paths"]:
            filename = _safe_path(filename)
            key = filename.casefold()
            if key in owners:
                raise ValidationError("Duplicate output ownership (including case aliases): " + filename)
            owners[key] = identifier
    needed = {"interface", "monitor", "agent", "coverage", "env", "test", "tb_top"}
    if not needed <= responsibilities or not {"scoreboard", "assertions", "reference_model"} & responsibilities:
        raise ValidationError("Blueprint lacks required UVM environment/observation/checking/coverage responsibilities")
    for reference in points:
        mapped_roles = {role for component in components if reference in component["testpoint_ids"] for role in component["responsibilities"]}
        if not {"sequence", "virtual_sequence", "test"} & mapped_roles or not {"scoreboard", "reference_model", "assertions"} & mapped_roles or "coverage" not in mapped_roles:
            raise ValidationError("Every accepted testpoint needs stimulus, checking and coverage mappings: " + reference)
    for component in components:
        dependencies = component["depends_on"]
        if len(set(dependencies)) != len(dependencies) or component["id"] in dependencies or not set(dependencies) <= by_id.keys():
            raise ValidationError("Invalid component dependency")
    ordered = []
    remaining = dict(by_id)
    while remaining:
        available = [component for component in remaining.values() if set(component["depends_on"]) <= {item["id"] for item in ordered}]
        if not available:
            raise ValidationError("UVM component dependency graph contains a cycle")
        for component in available:
            ordered.append(component)
            del remaining[component["id"]]
    for component in ordered:
        ancestors = _ancestors(component["id"], by_id)
        for filename in component["dependency_files"]:
            owner = owners.get(_safe_path(filename).casefold())
            if owner not in ancestors:
                raise ValidationError("Dependency file is not owned by a prerequisite: " + filename)
    return ordered


def _ancestors(identifier, components):
    found = set()
    queue = list(components[identifier]["depends_on"])
    while queue:
        identifier = queue.pop()
        if identifier not in found:
            found.add(identifier)
            queue.extend(components[identifier]["depends_on"])
    return found


def _ask(project, task, prompt, schema, agent, timeout, validate, accept, budget):
    attempt = project.store.claim(task["id"], "uvm-worker-" + uuid.uuid4().hex, lease_seconds=timeout + 180)
    attempt_dir = project.root / ".loopx" / "chip-verification" / "attempts" / attempt["id"]
    attempt_dir.mkdir(parents=True, exist_ok=True)
    save_json(attempt_dir / "generation-context.json", {"prompt": prompt, "characters": len(prompt),
              "context_chars": budget, "schema_chars": _schema_characters(agent, schema),
              "required_chars": len(prompt) + _schema_characters(agent, schema)})
    try:
        response = runtime(agent, timeout=timeout, max_prompt_chars=budget).run(
            prompt, cwd=project.root, run_dir=attempt_dir, schema=schema, attempt_id=attempt["id"])
        validate()
        project.store.heartbeat(task["id"], attempt["id"], lease_seconds=180)
        result, artifacts, checks = accept(response.response, attempt, attempt_dir)

        def validator(_task, _result, _artifacts):
            validate()
            return {"passed": True, "validator": "uvm_candidate_structure_v1", "checks": checks,
                    "claim": "candidate_structure_and_provenance_only; not compiled or simulated"}

        receipt = project.store.finish(task["id"], attempt["id"], result,
                                       artifacts + [attempt_dir / name for name in ("response.json", "receipt.json", "events.jsonl", "generation-context.json")],
                                       validator=validator)
        return project.store.get_task(task["id"]), receipt
    except GenerationBlocked as error:
        current = project.store.get_task(task["id"])
        if current["status"] != "running" or current["attempt_id"] != attempt["id"]:
            raise ConflictError("A superseded attempt cannot block a generation task") from error
        path = attempt_dir / "blocked-contracts.json"
        save_json(path, {"task_id": task["id"], "attempt_id": attempt["id"], "unresolved": error.issues,
                         "response_path": str(attempt_dir / "response.json"), "reason": str(error)})
        blocked = project.store.block(task["id"], str(error))
        project.store.add_issue("generation_contract_blocker", str(error) + " Evidence: " + str(path), task_id=task["id"])
        return blocked, {"status": "blocked", "attempt_id": attempt["id"], "unresolved": error.issues, "evidence_path": str(path)}
    except Exception as error:
        try:
            project.store.fail(task["id"], attempt["id"], str(error), retryable=True)
        except ConflictError:
            pass
        raise


def _publish(project, directory, tasks, blueprint, basis):
    """Rebuild a disposable compile view from immutable accepted candidates."""
    files = []
    view = directory / "files"
    for task in tasks:
        if task["status"] != "done":
            continue
        for record in task["result"]["files"]:
            source = project.root / record["artifact_path"]
            if not source.is_file() or _file_hash(source) != record["sha256"]:
                raise ConflictError("Accepted generated artifact changed: " + str(source))
            destination = _within(view, record["path"], directory)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.is_file() or _file_hash(destination) != record["sha256"]:
                temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
                temporary.write_bytes(source.read_bytes())
                os.replace(temporary, destination)
            files.append({**record, "view_path": str(destination), "component_task": task["id"]})
    complete = all(task["status"] == "done" for task in tasks)
    result = {"schema_version": 1, "status": "candidate_generated" if complete else "partial",
              "generation_id": directory.name, "baseline": basis["spec_sha256"], "top": basis["top"],
              "generation_scope": basis["generation_scope"], "cross_scope_integration_verified": False,
              "verification_profile": basis["verification_profile"],
              "uvm_version": blueprint["uvm_version"], "input_fingerprint": _hash(basis),
              "output_directory": str(view), "files": files,
              "components_done": sum(task["status"] == "done" for task in tasks), "components_total": len(tasks),
              "pending_tasks": [{"id": task["id"], "status": task["status"], "reason": task["reason"]} for task in tasks if task["status"] != "done"],
              "compiled": False, "simulated": False, "coverage_verified": False,
              "limitations": ["Generated source is a structurally validated candidate, not a verified testbench.",
                              "RTL declaration extraction does not elaborate macros, includes or parameter configurations.",
                              "Only the accepted testpoints captured in this generation input are included; other plan work may remain.",
                              "Compilation, UVM connection/activity smoke tests and real regression evidence are separate acceptance stages."]}
    path = directory / "generation-manifest.json"
    save_json(path, result)
    return {**result, "manifest_path": str(path)}


def run_generation(project, rtl_files: list, top: str, agent: str = "codex", timeout: int = 600,
                   max_components: int | None = None, notify=None, scope_id: str | None = None,
                   profile_path: str | Path | None = None, context_chars: int | None = None,
                   dispatcher=None, contract_task_id: str | None = None) -> dict:
    """Generate/resume one UVM candidate graph for the current accepted plan scope.

    ``project`` is a DVProject. Each component is a fresh model session. Completed
    tasks are reused. By default the next unfinished automatically partitioned
    module/function scope is selected; scope_id pins a particular scope.
    context_chars is a deprecated compatibility argument. It cannot change
    scope partitioning, dispatch eligibility or semantic input identity.
    """
    if (agent not in {"codex", "pi", "host"} or (agent == "host" and dispatcher is None)
            or timeout <= 0 or (max_components is not None and max_components < 0)):
        raise ValueError("Invalid generation runtime, timeout or component limit")
    if not rtl_files and not contract_task_id:
        raise ValidationError("Provide the actual RTL source files; specification prose is not RTL")
    if context_chars is not None and (not isinstance(context_chars, int) or isinstance(context_chars, bool) or context_chars <= 0):
        raise ValueError("Deprecated context_chars must be a positive integer or None")
    budget = None
    paths = sorted({(Path(path) if Path(path).is_absolute() else project.root / path).resolve() for path in rtl_files}, key=str)
    if any(not path.is_file() for path in paths):
        raise ValidationError("An explicitly supplied RTL file does not exist")
    project.store.recover_expired()
    basis, plans, points, declaration, scopes, requirements, profile = _snapshot(project, paths, top, scope_id, profile_path, contract_task_id)
    build_manifest_contract = _build_manifest_contract(basis, profile)
    selected_scope = basis["generation_scope"]
    fingerprint = _hash(basis)
    generation_id = "gen_" + fingerprint[:24]
    directory = project.root / "generated_uvm" / basis["spec_sha256"][:20] / generation_id
    if not directory.resolve().is_relative_to(project.root.resolve()):
        raise ValidationError("Generation directory escapes the project")
    directory.mkdir(parents=True, exist_ok=True)

    def validate_inputs():
        if not directory.resolve().is_relative_to(project.root.resolve()):
            raise ConflictError("Generation output directory moved outside the project")
        try:
            current, *_ = _snapshot(project, paths, top, selected_scope["id"], profile_path, contract_task_id)
        except (ValidationError, OSError) as error:
            raise ConflictError("Generation inputs changed or became invalid during generation: " + str(error)) from error
        if _hash(current) != fingerprint:
            raise ConflictError("Spec, scope, RTL or accepted plan changed during generation")

    catalog = [{"id": reference, "name": point["name"], "level": point["level"],
                "uvm_components": point["uvm_components"], "requirement_ids": point["requirement_ids"]}
               for reference, point in points.items()]
    architecture_context = {"scope": basis["scope"], "generation_scope": selected_scope,
                            "shared_specification": basis.get("shared_specification"),
                            "build_manifest_contract": build_manifest_contract,
                            "top_declaration": declaration,
                            "verification_profile": profile,
                            "testpoints": list(points.values()), "requirements": list(requirements.values()),
                            "rtl_sources": {"file_count": len(basis["rtl"]),
                                            "total_bytes": sum(item["bytes"] for item in basis["rtl"]),
                                            "inventory_sha256": _hash(basis["rtl"]),
                                            "top_source": {key: item[key] for item in basis["rtl"] if item["path"] == declaration["path"]
                                                           for key in ("path", "sha256", "bytes")}},
                            "accepted_testpoint_catalog": catalog,
                            "output_root": str(directory / "files"), "requested_uvm_version": profile["uvm_version"] if profile else "1.2"}
    architecture = project.store.add_task("architecture", f"Design UVM architecture for {top}",
                                           {"generation_id": generation_id, "generation_scope_id": selected_scope["id"],
                                            "input_fingerprint": fingerprint, "basis": basis},
                                           depends_on=[task["id"] for task in plans] + ([contract_task_id] if contract_task_id else []),
                                           task_id="uvm_arch_" + fingerprint[:24])

    def accept_architecture(response, attempt, attempt_dir):
        _ordered_components(response, points, top)
        if profile and response["uvm_version"] != profile["uvm_version"]:
            raise ValidationError("Blueprint UVM version differs from the verification profile")
        path = directory / "architectures" / attempt["id"] / _hash(response) / "architecture.json"
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) != response:
                raise ConflictError("Immutable UVM architecture changed before acceptance")
        else:
            save_json(path, response)
        return response, [path], ["blueprint_schema", "top_identity", "acyclic_dependencies", "exclusive_file_ownership", "testpoint_stimulus_checker_coverage_mappings", "input_fingerprints_current"]

    architecture, prompt, budget_receipt = _prepare_dispatch_context(
        project, architecture, ARCHITECTURE_INSTRUCTION, architecture_context, budget, agent,
        ARCHITECTURE_SCHEMA, context_chars, validate_inputs, dispatcher)
    if budget_receipt and notify:
        notify({"stage": "architecture", "task_id": architecture["id"], "status": "blocked",
                "claim": "context_not_dispatched", "receipt": budget_receipt})
    if prompt is not None:
        ask = dispatcher.dispatch if dispatcher else _ask
        architecture, receipt = ask(project, architecture, prompt, ARCHITECTURE_SCHEMA, agent, timeout, validate_inputs, accept_architecture, budget)
        if notify:
            notify({"stage": "architecture", "task_id": architecture["id"], "status": architecture["status"], "claim": "candidate_blueprint_only", "receipt": receipt})
    if architecture["status"] != "done":
        return {"status": "blocked", "task_id": architecture["id"], "task_status": architecture["status"],
                "reason": architecture["reason"], "generation_scope": selected_scope,
                "compiled": False, "simulated": False, "context_chars": budget,
                "components_dispatched_this_call": 0, "components_generated_this_call": 0}
    blueprint = architecture["result"]
    ordered = _ordered_components(blueprint, points, top)
    by_id = {component["id"]: component for component in ordered}
    task_ids = {component["id"]: "uvm_impl_" + fingerprint[:24] + "_" + component["id"] for component in ordered}
    for component in ordered:
        project.store.add_task("implement", f"Generate UVM {component['id']} for {top}",
                              {"generation_id": generation_id, "generation_scope_id": selected_scope["id"],
                               "input_fingerprint": fingerprint, "component": component,
                               **({"contract_task_id": contract_task_id, "contract_hash": basis["shared_specification"]["hash"]}
                                  if contract_task_id else {})},
                              depends_on=[architecture["id"]] + [task_ids[identifier] for identifier in component["depends_on"]],
                              task_id=task_ids[component["id"]])
    completed_now, dispatched_now = 0, 0
    for component in ordered:
        if dispatcher and dispatcher.completed:
            break
        if max_components is not None and dispatched_now >= max_components:
            break
        selected = getattr(dispatcher, "expected_task_id", None)
        if selected and task_ids[component["id"]] != selected:
            continue
        task = project.store.get_task(task_ids[component["id"]])
        budget_issues = _context_issues(project, task)
        if task["status"] != "pending" and not budget_issues and not (dispatcher and dispatcher.owns(task)):
            continue
        ancestors = _ancestors(component["id"], by_id)
        upstream_tasks = [project.store.get_task(task_ids[identifier]) for identifier in sorted(ancestors)]
        if any(upstream["status"] != "done" for upstream in upstream_tasks):
            continue
        requested_files = {name.casefold() for name in component["dependency_files"]}
        for identifier in ancestors:
            if {"interface", "transaction", "config", "package"} & set(by_id[identifier]["responsibilities"]):
                requested_files.update(name.casefold() for name in by_id[identifier]["output_paths"])
        dependency_files = []
        for upstream in upstream_tasks:
            for record in upstream["result"]["files"]:
                if record["path"].casefold() not in requested_files:
                    continue
                path = project.root / record["artifact_path"]
                if not path.is_file() or _file_hash(path) != record["sha256"]:
                    raise ConflictError("A required UVM dependency artifact changed")
                dependency_files.append({"path": record["path"], "sha256": record["sha256"], "content": path.read_text(encoding="utf-8")})
        if {record["path"].casefold() for record in dependency_files} != requested_files:
            raise ValidationError("A required UVM dependency file is absent")
        context = {"scope": basis["scope"], "top_declaration": declaration, "uvm_version": blueprint["uvm_version"],
                   "shared_specification": basis.get("shared_specification"),
                   "build_manifest_contract": build_manifest_contract,
                   "verification_profile": profile,
                   "shared_contracts": blueprint["shared_contracts"], "interfaces": blueprint["interfaces"],
                   "component": component, "prerequisite_contracts": [by_id[identifier] for identifier in sorted(ancestors)],
                   "required_dependency_files": dependency_files,
                   "testpoints": [points[reference] for reference in component["testpoint_ids"]],
                   "requirements": [requirements[identifier] for identifier in sorted({
                       identifier for reference in component["testpoint_ids"] for identifier in points[reference]["requirement_ids"]})]}
        task, prompt, budget_receipt = _prepare_dispatch_context(
            project, task, COMPONENT_INSTRUCTION, context, budget, agent,
            COMPONENT_SCHEMA, context_chars, validate_inputs, dispatcher)
        if prompt is None:
            if budget_receipt and notify:
                notify({"stage": "implement", "task_id": task["id"], "component_id": component["id"],
                        "status": "blocked", "claim": "context_not_dispatched", "receipt": budget_receipt})
            continue

        def validate_component_inputs():
            validate_inputs()
            for upstream in upstream_tasks:
                current = project.store.get_task(upstream["id"])
                if current["status"] != "done" or current["result"] != upstream["result"]:
                    raise ConflictError("Component prerequisite changed during generation")
                for record in current["result"]["files"]:
                    if _file_hash(project.root / record["artifact_path"]) != record["sha256"]:
                        raise ConflictError("Component prerequisite file changed during generation")

        def accept_component(response, attempt, attempt_dir):
            jsonschema.validate(response, COMPONENT_SCHEMA)
            if response["unresolved"]:
                raise GenerationBlocked("Component has unresolved blockers: " + "; ".join(response["unresolved"]), response["unresolved"])
            actual = [file["path"] for file in response["files"]]
            if len(set(name.casefold() for name in actual)) != len(actual) or set(actual) != set(component["output_paths"]):
                raise ValidationError("Component must return exactly its exclusively owned output paths")
            for file in response["files"]:
                _safe_path(file["path"])
                if not file["content"].strip() or re.search(r"\b(?:TODO|FIXME|IMPLEMENT_ME)\b", file["content"]):
                    raise ValidationError("Empty or placeholder UVM implementation cannot be accepted")
                if re.search(r"\bmodule\s+(?:(?:automatic|static)\s+)?(?:" + re.escape(top) + r"\b|\\" + re.escape(top) + r"\s)", _strip_comments(file["content"])):
                    raise ValidationError("Generated testbench must not redefine the DUT")
            records, artifacts = [], []
            candidate_root = directory / "candidates" / attempt["id"] / _hash(response)[:24]
            for file in response["files"]:
                target = _within(candidate_root, file["path"], directory)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if target.read_text(encoding="utf-8") != file["content"]:
                        raise ConflictError("Immutable UVM candidate changed before acceptance")
                else:
                    with target.open("x", encoding="utf-8", newline="\n") as stream:
                        stream.write(file["content"])
                records.append({"path": file["path"], "artifact_path": target.relative_to(project.root).as_posix(), "sha256": _file_hash(target)})
                artifacts.append(target)
            record = {"component_id": component["id"], "files": records, "notes": response["notes"],
                      "input_fingerprint": fingerprint, "candidate_only": True, "compiled": False, "simulated": False}
            return record, artifacts, ["component_schema", "exact_output_ownership", "confined_paths", "nonempty_no_placeholders", "prerequisite_digests_current", "input_fingerprints_current"]

        dispatched_now += 1
        ask = dispatcher.dispatch if dispatcher else _ask
        task, receipt = ask(project, task, prompt, COMPONENT_SCHEMA, agent, timeout, validate_component_inputs, accept_component, budget)
        completed_now += task["status"] == "done"
        if notify:
            notify({"stage": "implement", "task_id": task["id"], "component_id": component["id"], "status": task["status"], "claim": "uncompiled_candidate", "receipt": receipt})
    validate_inputs()
    result = _publish(project, directory, [project.store.get_task(task_ids[item["id"]]) for item in ordered], blueprint, basis)
    result["components_generated_this_call"] = completed_now
    result["components_dispatched_this_call"] = dispatched_now
    result["context_chars"] = budget
    planning = ([task for task in project.store.list_tasks() if task["kind"] in {"model", "spec-contract", "contract-plan"}]
                if project.config.get("engineering_enabled") else effective_planning_tasks(project.store.list_tasks()))
    if project.config.get("engineering_enabled"):
        from .engineering import superseded_task_ids
        superseded = superseded_task_ids(project)
        planning = [task for task in planning if task["id"] not in superseded]
    result["planning_scope_complete"] = bool(planning) and all(task["status"] == "done" for task in planning)
    result["generation_scope"] = selected_scope
    result["scope_inventory"] = generation_scopes(project)
    result["cross_scope_integration_verified"] = False
    return result
