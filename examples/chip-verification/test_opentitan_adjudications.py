"""Executable acceptance checks for the authored problem corrections (not DV coverage)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

import pytest


HERE = Path(__file__).resolve().parent
MODULE_SPEC = importlib.util.spec_from_file_location("opentitan_adjudications", HERE / "opentitan_adjudications.py")
assert MODULE_SPEC and MODULE_SPEC.loader
builder = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(builder)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*") if p.is_file()}


def test_all_grouped_ids_and_case_scopes():
    expected = {f"{prefix}-{n:02d}" for prefix, count in
                {"SH": 8, "SD": 5, "AC": 4, "AH": 5, "CT": 5, "Q": 13, "D": 2, "N": 4}.items()
                for n in range(1, count + 1)}
    records = {item["issue_id"]: item for item in builder.DECISIONS}
    assert len(builder.DECISIONS) == len(records) == 46
    assert records.keys() == expected
    seen = set()
    for case_id, count in zip(builder.CASES, (27, 25)):
        case = HERE / "cases" / case_id
        bundle = read_json(case / "input/spec.json")
        corrections = {doc["id"]: doc for doc in bundle["documents"]
                       if doc.get("origin", {}).get("kind") == "source_adjudication"}
        assert len(corrections) == count + 2
        assert "adjudication-SCOPE" in corrections
        assert "adjudication-CADENCE" in corrections
        assert "DUT module types" in corrections["adjudication-SCOPE"]["content"]
        ids = {key.removeprefix("adjudication-") for key in corrections} - {"SCOPE", "CADENCE"}
        assert ids == {item["issue_id"] for item in builder.DECISIONS if builder.applicable(case_id, item)}
        seen.update(ids)
        if case_id.endswith("aon-control"):
            assert not any(doc["module"] in {"spi_host", "spi_device", "usbdev"} for doc in corrections.values())
        else:
            assert not any(doc["module"] in {"pinmux", "rstmgr", "pwrmgr", "sysrst_ctrl", "alert_handler"}
                           for doc in corrections.values())
    assert seen == expected


@pytest.mark.parametrize("case_id,body_bytes", list(zip(builder.CASES, (567411, 592589))))
def test_targets_original_content_and_uncounted_amendments(case_id, body_bytes):
    input_dir = HERE / "cases" / case_id / "input"
    manifest = read_json(input_dir / "source_manifest.json")
    bundle = read_json(input_dir / "spec.json")
    docs = {doc["id"]: doc for doc in bundle["documents"]}
    entries = {entry["id"]: entry for entry in manifest["documents"]}
    assert manifest["spec_sha256"] == builder.sha((input_dir / "spec.json").read_bytes())
    assert manifest["unique_specification_body_bytes"] == body_bytes
    assert sum(entry["bytes"] for entry in entries.values() if entry["count_toward_size"]) == body_bytes
    for doc_id, entry in entries.items():
        raw = (input_dir / entry["path"]).read_bytes()
        assert builder.sha(raw) == entry["sha256"]
        assert len(raw) == entry["bytes"]
        assert docs[doc_id]["content"] == raw.decode("utf-8")
        if entry.get("upstream_path"):
            # Verify original source bytes also against the recorded git object,
            # independently of the embedded doc/sha256 copy.
            git_blob = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
            assert git_blob == entry["git_blob_sha1"]
            continue
        if not doc_id.startswith("adjudication-"):
            continue
        assert entry["count_toward_size"] is False
        doc = docs[doc_id]
        assert doc["role"] == "specification"
        assert doc["origin"]["authored"] is True
        assert doc["origin"]["sha256"] == entry["sha256"]
        assert doc["origin"]["targets"]
        for target in doc["origin"]["targets"]:
            source = docs[target["document_id"]]
            original = entries[source["id"]]
            assert original.get("upstream_path")
            assert target["sha256"] == original["sha256"]
            assert 1 <= target["line_start"] <= target["line_end"] <= len(source["content"].splitlines())
            if doc_id not in {"adjudication-SCOPE", "adjudication-CADENCE"}:
                assert f"`{original['upstream_path']}` lines {target['line_start']}-{target['line_end']}" in doc["content"]


@pytest.mark.parametrize("case_id", builder.CASES)
def test_rebuild_idempotent_and_never_rewrites_sources(case_id, tmp_path):
    target = tmp_path / case_id
    shutil.copytree(HERE / "cases" / case_id, target)
    original_sources = snapshot(target / "input/sources")
    before = snapshot(target)
    builder.build_case(target)
    assert snapshot(target / "input/sources") == original_sources
    assert snapshot(target) == before
    builder.build_case(target)
    assert snapshot(target) == before


def test_correction_oracles_and_false_positive_classification():
    items = {item["issue_id"]: item for item in builder.DECISIONS}
    assert "rx_depth_words > RX_WATERMARK" in items["SH-02"]["rule"]
    assert "inside HJSON" in items["SH-02"]["rule"]
    assert "first four" in items["SD-01"]["rule"]
    assert "PAYLOAD_SWAP_MASK/PAYLOAD_SWAP_DATA" in items["SD-01"]["rule"]
    assert "848 words" in items["SD-05"]["rule"] and "448 bytes" in items["SD-05"]["rule"]
    assert "rw0c" in items["AH-04"]["rule"] and "zero clears" in items["AH-04"]["rule"]
    assert "threshold 0" in items["AH-01"]["rule"]
    assert "remains read-only" in items["Q-11"]["rule"]
    assert items["Q-11"]["classification"] == "not_a_conflict"
    for issue_id in ("N-01", "N-02", "N-04"):
        assert items[issue_id]["classification"] == "not_a_conflict"
        assert items[issue_id]["local_gate"] is None
    assert items["N-03"]["classification"] == "naming_correction"
    assert "IOC8" in items["D-01"]["rule"]
    assert items["D-01"]["local_gate"] is None and items["D-02"]["local_gate"] is None
    assert "CRC7" in items["D-02"]["rule"]
    assert "cadence_contract.md" in items["D-01"]["rule"]
    assert items["Q-12"]["classification"] == "exercise_convention"
    assert "Author-chosen" in items["Q-12"]["rule"]
    for issue_id in ("CT-02", "CT-04", "CT-05"):
        assert items[issue_id]["local_gate"] is None
    assert "not an attributed user instruction" in builder.AUTHOR_POLICY


@pytest.mark.parametrize("case_id", builder.CASES)
def test_problem_and_answer_share_contract_without_claiming_results(case_id):
    case = HERE / "cases" / case_id
    problem = (case / "input/problem.md").read_text(encoding="utf-8")
    guide = (case / "answer_guide.md").read_text(encoding="utf-8")
    profile = read_json(case / "input/integration_profile.json")
    rubric = read_json(case / "obligations.json")
    spec = read_json(case / "input/spec.json")
    docs = {doc["id"]: doc for doc in spec["documents"]}
    assert "Revised normative exercise contract" in problem
    assert "block affected decisions instead of guessing precedence" not in problem
    assert profile["source_precedence"] == builder.AUTHOR_POLICY
    assert rubric["adjudication_revision"] == builder.REVISION
    assert "not a passing test" in guide
    assert "reference divergence" in guide
    records = rubric["adjudication_obligations"]
    assert len({item["issue_id"] for item in records}) == len(records)
    for record in records:
        assert record["status"] == "not_run"
        doc = docs[record["source_document_id"]]
        assert doc["origin"]["kind"] == "source_adjudication"
        assert record["requirement"] in doc["content"]
        assert record["requirement"] in problem
        assert record["checks"] and record["checks"][0] in doc["content"]
        assert "Authority: benchmark author" in doc["content"]
        if record["issue_id"] != "CADENCE":
            assert "Rejected reading:" in doc["content"]
        assert ("Local acceptance gate:" in doc["content"]) == bool(record["local_gate"])
        assert (record["resolution_state"] == "local_obligation_open") == bool(record["local_gate"])


def test_actual_source_anchor_words():
    # Check decisive boundaries against the unchanged input, not against authored
    # summaries. This catches a moved/wrong span even when all hashes are coherent.
    checks = {
        "SH-02": [("SH-H", 622, 626, "greater than")],
        "SH-05": [("SH-H", 439, 450, '"8:0"')],
        "SD-01": [("SD-H", 421, 424, "first 4 payload bytes")],
        "AH-04": [("AH-H", 503, 518, 'swaccess: "rw0c"')],
        "N-01": [("PM-T", 195, 215, "attr_i[13:10]")],
        "N-03": [("UD-H", 1144, 1149, "tx_use_d_se0")],
        "D-01": [("TOP-H", 1443, 1444, "IOC8")],
        "Q-08": [("SC-H", 279, 294, "ULP_STATUS")],
    }
    for issue_id, anchors in checks.items():
        item = next(item for item in builder.DECISIONS if item["issue_id"] == issue_id)
        case_id = next(case_id for case_id in builder.CASES if builder.applicable(case_id, item))
        for alias, first, last, expected in anchors:
            path = HERE / "cases" / case_id / "input/sources" / builder.PATHS[alias]
            actual = "\n".join(path.read_text(encoding="utf-8").splitlines()[first - 1:last])
            assert expected in actual


@pytest.mark.parametrize("case_id", builder.CASES)
def test_cadence_contract_supplies_missing_assets_and_reference_is_optional(case_id):
    case = HERE / "cases" / case_id
    profile = read_json(case / "input/integration_profile.json")
    contract = (case / "input/cadence_contract.md").read_text(encoding="utf-8")
    dependency = read_json(case / "dependency_declaration.json")
    rubric = read_json(case / "obligations.json")
    assert profile["toolchain"] == builder.CADENCE_TOOLCHAIN
    assert profile["source_adjudications"]["open_local_obligations"] == []
    assert f"`{profile['root_module']}`" in contract
    for anchor in ("bench_tl_req_t", "bench_tl_rsp_t", "x^7+x^3+1", "(52 bits)", "(13 bits)",
                   "hardware set wins", "first-stall cycle is not an acceptance criterion",
                   "IOC8 pull_en resets", "16 DIO", "two destination-clock", "independently authored"):
        assert anchor in contract
    assert dependency["native_build_required"] is False
    assert "not_build_closure" in dependency["stored_reference_role"]
    assert rubric["evaluation_contract"]["execution_status"] == "not_run"
    assert rubric["evaluation_contract"]["upstream_native_regression_required"] is False
    assert not any("use a full pinned checkout" in item for item in rubric["reference_gaps"])
    if case_id.endswith("aon-control"):
        for signal in ("clk_main_i", "clk_io_i", "clk_usb_i", "clock_leaf_o[7:0]",
                       "clock_idle_i[3:0]", "clock_ack_i[7:0]", "extclk_ack_i"):
            assert signal in contract
        assert "IO_DIV2_PERI, IO_PERI, USB_PERI" in contract
        assert "MAIN_AES, MAIN_HMAC, MAIN_KMAC, MAIN_OTBN" in contract
