"""Stage and verify the shared Cadence kit without changing source inventories."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


KIT = Path(__file__).with_name("cadence")
VERSION = "cadence-exercise-kit-v1"


def _json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _files(destination, profile):
    top = profile.get("root_module")
    if not isinstance(top, str) or not re.fullmatch(r"[A-Za-z_]\w*", top):
        raise ValueError("Cadence exercise needs an explicit SystemVerilog root_module")
    contract = destination / "cadence_contract.md"
    if not contract.is_file() or contract.is_symlink():
        raise ValueError("Cadence generation needs the authored cadence_contract.md")
    generation = {"schema_version": 1, "top": top, "uvm_version": "1.2",
        "sources": [{"id": "cadence_contract", "path": "../cadence_contract.md",
                     "sha256": _hash(contract.read_bytes()), "purpose": "Authored Cadence execution and interface contract"}],
        "documented_design_facts": [], "rtl_bindings": [],
        "testbench_choices": [{"id": "execution_target", "statement": "Use Cadence Xcelium with native UVM 1.2 and the external execution kit",
            "rationale": "Selected exercise toolchain; no upstream build dependency", "source_refs": ["cadence_contract"],
            "value": {"simulator": "cadence_xcelium"}}]}
    example = {"schema_version": 1, "simulator": "cadence_xcelium", "execution": "external",
        "candidate_dir": "..", "dut_top": top, "top": top + "_tb", "testname": "subsystem_test",
        "rtl_files": [], "tb_files": [], "compile_order": [], "include_dirs": [], "defines": [],
        "seed": 1, "runtime": "all", "timescale": "1ns/1ps",
        "activity_contract": {"pattern": r"LOOPX_ACTIVITY checks=(?P<checks>[0-9]+) transactions=(?P<transactions>[0-9]+)",
                              "minimums": {"checks": 1, "transactions": 1}},
        "coverage_model": "verification/coverage_model.json", "constraints": [], "extra_inputs": [], "scripts": {}}
    return {"eda/cadence_runner.py": (KIT / "runner.py").read_bytes(),
            "eda/README.md": (KIT / "README.md").read_bytes(),
            "eda/project.example.json": _json(example), "eda/verification_profile.json": _json(generation)}


def stage_execution_kit(destination: Path, profile: dict) -> dict:
    destination = Path(destination)
    if (destination / "eda").exists():
        raise ValueError("Cadence kit destination already exists")
    files = _files(destination, profile)
    manifest = {"schema_version": 1, "version": VERSION,
                "profile": {"case_id": profile.get("case_id"), "root_module": profile["root_module"]},
                "files": [{"path": path, "sha256": _hash(raw), "bytes": len(raw)} for path, raw in files.items()],
                "native_tool_execution_performed": False, "coverage_percent": None}
    files["eda/kit_manifest.json"] = _json(manifest)
    for name, raw in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    validate_execution_kit(destination)
    return {"version": VERSION, "files": sorted(files), "native_tool_execution_performed": False}


def validate_execution_kit(destination: Path) -> list[str]:
    destination = Path(destination)
    directory = destination / "eda"
    if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
        raise ValueError("Linked Cadence kit directory")
    manifest_path = directory / "kit_manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("Linked Cadence kit manifest")
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get("version") != VERSION or manifest.get("schema_version") != 1:
        raise ValueError("Unsupported Cadence kit revision")
    expected = _files(destination, manifest["profile"])
    entries = manifest["files"]
    if len(entries) != len(expected) or {item["path"] for item in entries} != set(expected):
        raise ValueError("Cadence kit inventory differs")
    for entry in entries:
        path = destination / entry["path"]
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing/linked Cadence kit file")
        raw = path.read_bytes()
        if raw != expected[entry["path"]] or _hash(raw) != entry["sha256"] or len(raw) != entry["bytes"]:
            raise ValueError("Cadence kit content changed: " + entry["path"])
    allowed = [*expected, "eda/kit_manifest.json"]
    if {p.relative_to(destination).as_posix() for p in directory.rglob("*") if p.is_file()} != set(allowed):
        raise ValueError("Unexpected staged Cadence kit files")
    profile_path = destination / "integration_profile.json"
    if profile_path.is_file():
        profile = json.loads(profile_path.read_bytes())
        if any(profile.get(key) != manifest["profile"].get(key) for key in ("case_id", "root_module")):
            raise ValueError("Cadence kit belongs to a different exercise")
    return sorted(allowed)
