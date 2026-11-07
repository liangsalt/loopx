"""Vivado XSim execution and evidence validation.

The optional RV32IM benchmark adapter consumes an explicitly supplied external
benchmark directory. Its coverage model is not RTL code coverage or a claim of
complete ISA compliance; no benchmark or machine-local installation is bundled.
"""

from __future__ import annotations
import csv
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time


class SimulationError(RuntimeError):
    pass


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_evidence(log_text, hits_path, manifest):
    """Cross-check actual UVM activity, native covergroup and per-bin observation."""
    errors = re.findall(r"UVM_ERROR\s*:\s*(\d+)", log_text)
    fatals = re.findall(r"UVM_FATAL\s*:\s*(\d+)", log_text)
    checks = re.findall(
        r"DVFLOW_CHECKS=(\d+) RETIRED=(\d+) BUS=(\d+) TRAPS=(\d+) RESETS=(\d+)",
        log_text,
    )
    coverage = re.findall(
        r"DVFLOW_NATIVE_COVERAGE=([\d.]+) HIT_BINS=(\d+) TOTAL_BINS=(\d+)", log_text
    )
    if not errors or not fatals:
        raise SimulationError(
            "UVM report summary missing; cannot classify simulation as passing"
        )
    if any(map(int, errors + fatals)):
        raise SimulationError(
            f"UVM reported {max(map(int, errors))} errors and {max(map(int, fatals))} fatal errors"
        )
    if not checks or not all(int(v) > 0 for v in checks[-1]):
        raise SimulationError("No non-vacuous architectural checking evidence")
    if int(checks[-1][3]) != int(checks[-1][4]):
        raise SimulationError(
            "Each reset/program must reach exactly one observed terminal retirement"
        )
    if not coverage:
        raise SimulationError("Native functional coverage result missing")
    features = manifest["features"]
    with Path(hits_path).open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if len(rows) != len(features):
        raise SimulationError("Per-bin evidence does not cover frozen target inventory")
    hits = 0
    missing = []
    hit_ids = []
    for row, feature in zip(rows, features):
        if (
            int(row["id"]) != feature["id"]
            or row["name"] != feature["name"]
            or row["hit"] not in ["0", "1"]
        ):
            raise SimulationError("Coverage identity mismatch")
        hits += int(row["hit"])
        if row["hit"] == "1":
            hit_ids.append(int(row["id"]))
        if row["hit"] == "0":
            missing.append(feature["name"])
    native, reported_hits, reported_total = coverage[-1]
    percent = 100.0 * hits / len(features)
    if (
        int(reported_hits) != hits
        or int(reported_total) != len(features)
        or abs(float(native) - percent) > 0.001
    ):
        raise SimulationError(
            "Native coverage, observed hits and frozen denominator disagree"
        )
    c = dict(
        zip(
            ["checks", "retired", "bus_transactions", "traps", "resets"],
            map(int, checks[-1]),
        )
    )
    return {
        "status": "passed",
        "checks": c,
        "uvm_errors": 0,
        "uvm_fatals": 0,
        "coverage": {
            "metric": "functional",
            "hit_bins": hits,
            "hit_ids": hit_ids,
            "total_bins": len(features),
            "percent": percent,
            "native_percent": float(native),
            "missing_bins": missing,
            "scope": manifest["scope"],
        },
    }


def parse_xcrg_report(report_text, expected_total, expected_hit_ids):
    """Validate every native database bin against the independent UVM hit ledger."""
    rows = re.findall(
        r"^\s*requirement_(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,", report_text, re.MULTILINE
    )
    inventory = {
        int(identifier): (int(hits), int(at_least))
        for identifier, hits, at_least in rows
    }
    if (
        len(rows) != expected_total
        or len(inventory) != expected_total
        or set(inventory) != set(range(expected_total))
    ):
        raise SimulationError(
            "XCRG native bin inventory differs from frozen feature inventory"
        )
    if any(at_least != 1 for _, at_least in inventory.values()):
        raise SimulationError(
            "XCRG bin hit threshold differs from fixed at_least=1 model"
        )
    actual = sorted(i for i, (hits, at_least) in inventory.items() if hits >= at_least)
    if actual != sorted(expected_hit_ids):
        raise SimulationError(
            "XCRG native bin hits differ from the UVM observation ledger"
        )
    match = re.search(r"Coverage Score\s*:,\s*([\d.]+)", report_text)
    if not match:
        raise SimulationError("XCRG coverage score missing")
    native = float(match.group(1))
    if abs(native - 100.0 * len(actual) / expected_total) > 0.001:
        raise SimulationError("XCRG aggregate differs from native bin inventory")
    return native


class XSimBackend:
    def __init__(self, vivado_home=None):
        candidates = []
        if vivado_home:
            candidates.append(Path(vivado_home))
        if os.environ.get("XILINX_VIVADO"):
            candidates.append(Path(os.environ["XILINX_VIVADO"]))
        suffix = ".bat" if os.name == "nt" else ""
        executable = shutil.which("xvlog" + suffix)
        if executable:
            candidates.append(Path(executable).resolve().parent.parent)
        self.home = next(
            (
                p.resolve()
                for p in candidates
                if (p / "bin" / ("xvlog" + suffix)).is_file()
            ),
            None,
        )
        if self.home is None:
            raise SimulationError(
                "Vivado not found; supply vivado_home, XILINX_VIVADO or xvlog on PATH"
            )
        self.suffix = suffix

    def _command(self, name, args, cwd, timeout):
        exe = str(self.home / "bin" / (name + self.suffix))
        argv = [exe] + [str(a) for a in args]
        if os.name == "nt" and any(re.search(r"[\r\n&|<>^%!\"]", v) for v in argv):
            raise SimulationError(
                "Unsupported shell metacharacter in Vivado command argument"
            )
        log = Path(cwd) / (name + "_console.log")
        started = time.time()
        env = os.environ.copy()
        env["XILINX_VIVADO"] = str(self.home)
        with log.open("wb") as f:
            p = subprocess.Popen(
                argv, cwd=str(cwd), stdout=f, stderr=subprocess.STDOUT, env=env
            )
            try:
                code = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(p.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    p.kill()
                p.wait()
                raise SimulationError(f"{name} timed out; log: {log}")
        text = log.read_text(encoding="utf-8", errors="replace")
        record = {
            "tool": name,
            "argv": argv,
            "exit_code": code,
            "seconds": round(time.time() - started, 3),
            "log": str(log),
            "log_sha256": sha256(log),
        }
        if code or re.search(
            r"(?:^|\n)(?:ERROR:|FATAL_ERROR:|Expected a switch)", text
        ):
            raise SimulationError(f"{name} failed (exit {code}); log: {log}")
        return record

    def run_benchmark(
        self,
        benchmark_dir,
        run_dir,
        campaign_path=None,
        seed=1,
        timeout_seconds=300,
        mutation=False,
    ):
        if mutation not in (False, True, "next_pc"):
            raise SimulationError("Unsupported benchmark mutation")
        benchmark = Path(benchmark_dir).resolve()
        run = Path(run_dir).resolve()
        run.mkdir(parents=True, exist_ok=True)
        if (run / "evidence.json").exists():
            raise SimulationError(
                "Run directory already has evidence; use a fresh attempt directory"
            )
        campaign = (
            Path(campaign_path).resolve()
            if campaign_path
            else benchmark / "campaign_smoke.json"
        )
        manifest_path = benchmark / "coverage_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        module_spec = importlib.util.spec_from_file_location(
            "dvflow_benchmark_programgen", benchmark / "programgen.py"
        )
        pg = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(pg)
        if manifest != pg.coverage_manifest():
            raise SimulationError(
                "Coverage manifest differs from frozen benchmark definition"
            )
        programs = pg.generate_program(campaign, run / "programs")
        (run / "program_list.txt").write_text(
            "\n".join("programs/" + Path(x["path"]).name for x in programs["programs"])
            + "\n",
            encoding="ascii",
        )
        rtl = benchmark / "upstream" / "picorv32.v"
        tb = benchmark / "tb"
        tb_files = [tb / "cpu_if.sv", tb / "cpu_uvm_pkg.sv", tb / "tb_top.sv"]
        uvm = self.home / "data" / "system_verilog" / "uvm_1.2"
        if not (uvm / "uvm_pkg.sv").is_file():
            raise SimulationError("Vendor UVM 1.2 source not found")
        evidence = {
            "schema_version": 1,
            "backend": "vivado_xsim",
            "status": "running",
            "tool": {
                "home": str(self.home),
                "version": self.home.name,
                "uvm": "1.2",
                "dpi": False,
            },
            "benchmark": str(benchmark),
            "run_dir": str(run),
            "seed": seed,
            "mutation": mutation,
            "spec_bytes": (benchmark / "spec.json").stat().st_size
            if (benchmark / "spec.json").exists()
            else None,
            "spec_sha256": sha256(benchmark / "spec.json")
            if (benchmark / "spec.json").exists()
            else None,
            "rtl_sha256": sha256(rtl),
            "coverage_manifest_sha256": sha256(manifest_path),
            "campaign_sha256": sha256(campaign),
            "inputs": [
                {"path": str(p), "sha256": sha256(p)}
                for p in [
                    rtl,
                    *tb_files,
                    tb / "coverage_defs.svh",
                    tb / "coverage_init.svh",
                    manifest_path,
                    campaign,
                    benchmark / "programgen.py",
                ]
            ],
            "commands": [],
            "programs": programs,
        }
        descriptor = benchmark / "benchmark.json"
        if descriptor.exists():
            description = json.loads(descriptor.read_text(encoding="utf-8"))
            evidence["design_configuration"] = description["design_configuration"]
            evidence["assumptions"] = description["assumptions"]
            evidence["inputs"].append(
                {"path": str(descriptor), "sha256": sha256(descriptor)}
            )
        (run / "input_manifest.json").write_text(
            json.dumps(evidence, indent=2), encoding="utf-8"
        )
        try:
            args = [
                "-sv",
                "-d",
                "UVM_NO_DPI",
                "-d",
                "RISCV_FORMAL",
                "-i",
                uvm,
                "-i",
                tb,
            ]
            if mutation:
                args += [
                    "-d",
                    "PICORV32_TESTBUG_005"
                    if mutation == "next_pc"
                    else "PICORV32_TESTBUG_004",
                ]
            args += [uvm / "uvm_pkg.sv", rtl, *tb_files]
            evidence["commands"].append(
                self._command("xvlog", args, run, timeout_seconds)
            )
            evidence["commands"].append(
                self._command(
                    "xelab",
                    [
                        "work.tb_top",
                        "-s",
                        "picorv32_uvm",
                        "-debug",
                        "typical",
                        "-timescale",
                        "1ns/1ps",
                    ],
                    run,
                    timeout_seconds,
                )
            )
            evidence["commands"].append(
                self._command(
                    "xsim",
                    ["picorv32_uvm", "-runall", "-sv_seed", seed],
                    run,
                    timeout_seconds,
                )
            )
            log = (run / "xsim_console.log").read_text(
                encoding="utf-8", errors="replace"
            )
            evidence.update(parse_evidence(log, run / "coverage_hits.csv", manifest))
            evidence["coverage"]["manifest_sha256"] = sha256(manifest_path)
            evidence["commands"].append(
                self._command(
                    "xcrg",
                    [
                        "-dir",
                        run / "xsim.covdb",
                        "-db_name",
                        "picorv32_uvm",
                        "-report_dir",
                        run / "coverage_report",
                        "-report_format",
                        "all",
                    ],
                    run,
                    timeout_seconds,
                )
            )
            report = run / "coverage_report" / "xcrg_report.txt"
            evidence["coverage"]["xcrg_percent"] = parse_xcrg_report(
                report.read_text(encoding="utf-8", errors="replace"),
                len(manifest["features"]),
                evidence["coverage"]["hit_ids"],
            )
            dbs = list((run / "xsim.covdb").rglob("*"))
            if not any(p.is_file() for p in dbs):
                raise SimulationError("XSim native coverage database missing")
            evidence["artifacts"] = [
                {"path": str(p), "sha256": sha256(p)}
                for p in [
                    run / "coverage_hits.csv",
                    *sorted((run / "coverage_report").rglob("*")),
                    *sorted((run / "xsim.covdb" / "picorv32_uvm").rglob("*")),
                ]
                if p.is_file()
            ]
            evidence["meets_coverage_target"] = evidence["coverage"][
                "percent"
            ] >= float(manifest["target_percent"])
        except (SimulationError, OSError, ValueError) as exc:
            evidence["status"] = "failed"
            evidence["error"] = str(exc)
            evidence["meets_coverage_target"] = False
        (run / "evidence.json").write_text(
            json.dumps(evidence, indent=2), encoding="utf-8"
        )
        return evidence
