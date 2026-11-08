# CoralNPU issue-inspired Cadence design and UVM exercises

Two public issue observations anchor these cases: [zero-element vector retirement #89](https://github.com/google-coral/coralnpu/issues/89) and [FENCE.TSO legality #130](https://github.com/google-coral/coralnpu/issues/130). The executable questions are newly authored, independent SystemVerilog subsystems. Each has a complete interface/behavior contract, a shared RTL/DV module graph and historical source references. Neither is an official upstream 500KiB specification or a reproduction of the full CoralNPU core.

| Case | Implementation top | Required behavior |
|---|---|---|
| `coralnpu-vector-zero-retirement` | `coral_vector_retirement_subsystem` | Vector command/CSR snapshots, active lane effects, legal empty completion, whole-register/move/config exceptions, concurrent tagged reads, fault prefixes, ordered retirement and flush/drain |
| `coralnpu-fence-tso-ordering` | `coral_memory_order_subsystem` | Scalar instruction decode, address/alignment, FENCE/FENCE.TSO memory ordering, concurrent tagged reads, precise faults, ordered retirement and flush/drain |

From `examples/chip-verification`:

```text
python coral_issue_cases.py list
python coral_issue_cases.py validate
python coral_issue_cases.py stage coralnpu-vector-zero-retirement /new/workspace/input
python coral_issue_cases.py stage coralnpu-fence-tso-ordering /another/workspace/input
```

The export must use a new destination. It contains source input, `cadence_contract.md`, scope policy, profile, shared module graph, licenses, interface-only assets under `contracts/`, unchanged historical baseline files and the shared `eda/` Cadence kit. The contract is embedded in `spec.json` and mandatorily accompanies every original-source fragment and the problem. It is therefore available during semantic decomposition, not only as a sidecar README. Contracts define exact widths, static parameters, valid/ready rules, response IDs, reset/flush behavior, finite fairness assumptions and the external memory model.

Build a new synthesizable SV implementation plus pure-SV UVM agents, passive monitors, independent scoreboards, SVA and covergroups. Do not compile `baseline/` or `sources/` as the candidate. Original source, patch and regression files are preserved for historical mechanism/provenance; their native build and upstream fixed implementation are not the golden oracle for the authored port contract. The original Coral checkout, Bazel, Chisel/firtool, MPACT, Cocotb, Verilator and a RISC-V software compiler are not required. `answers/` stays separate and supplies source-traceable acceptance reasoning/rubrics and clearly labelled historical references.

Use `eda/verification_profile.json` as `profile_path` for `dv_generate_uvm`. Complete the candidate's explicit source closure in `eda/project.json`, then use `eda/cadence_runner.py inspect`, `plan` and `run` according to the exported `eda/README.md`. Actual stages are Xcelium/UVM simulation, IMC coverage, Jasper Superlint/CDC/FPV and Genus synthesis checks on the Cadence host. The example profile is not a finished candidate. Missing RTL, clocks, constraints or licenses remain visible gaps. The shared kit is independently inventoried and never contributes to original-source size qualification.

Every spec.json exceeds 512KiB, as do unique original document bodies after excluding authored material, licenses, code references and duplicate bodies. The two original body totals remain 606,813 and 625,849 bytes. This measures retained source context, including explicitly unsupported/background ISA sections. It does not measure applicable normative requirement bytes or coverage. Splitting follows source structure and semantic responsibilities, without a hard context-length threshold. Keep independent original-source, scope-policy and authored-contract citations.

Source bodies are byte-preserved from pinned Google CoralNPU (Apache-2.0) and RISC-V ISA (CC-BY-4.0) repositories; licenses, exact URLs, revisions, Git blob IDs and SHA-256 hashes are retained. The committed manifest is offline-checkable. Issue observations are paraphrased and dated; tracker state is not proof of reproduction or repair.

No simulator, UVM run, formal/CDC proof or EDA coverage was executed to qualify these fixtures. More than 90% functional coverage is an acceptance target requiring a frozen applicable-bin plan and actual evidence on the EDA machine, together with all mandatory directed/mutation checks and no unexpected UVM/SVA errors. A stronger full fence is legal for FENCE.TSO: optional W-to-R reordering is never a mandatory coverage bin. The validator reports source integrity, never hardware coverage.
