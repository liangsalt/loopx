# Vector command execution and retirement in Cadence

This authored exercise is inspired by public CoralNPU issue #89. Implement
the independent standard-SystemVerilog subsystem `coral_vector_retirement_subsystem` defined completely by
`cadence_contract.md` and its interface-only assets under `contracts/`. The focus
is zero-element completion, legality before shortcut, active-lane effects, special vector operation rules, fault prefixes and ordered retirement. It is not an upstream whole-core repair or a bit-exact CoralNPU port.

Read the shared module graph, the complete Cadence contract and the source scope
policy before deriving the RTL microarchitecture or TP/UVM. The two tracks must
use the same interface and configuration revision. Original upstream documents
remain unchanged and attributed; they are broader context, not permission to
expand the selected instruction/operation subset. The full original repository,
Scala/Chisel/firtool, Bazel, FuseSoC, Cocotb, Verilator, MPACT, ELF programs and a
RISC-V compiler are not required for any stage of this exercise.

`baseline/` preserves historical source bytes behind the reported issue. Those
files are not a compilable candidate seed and must not be added to the candidate
file list. Build a new SV implementation of the declared boundary. Do not use
the separately held upstream patch or reference implementation as the behavioral
oracle for the new subsystem. Its oracle is the authored contract and applicable
ISA clauses, implemented independently in the UVM scoreboard and assertions.

Required outputs are shared requirements/contracts, a microarchitecture and
synthesizable SV RTL, a source-derived TP, pure-SV UVM agents/sequences/monitors,
independent scoreboards, SVA, covergroups, constraints and complete ordered
filelists. The supplied packages and port includes define interfaces only;
they are not a DUT, a stimulus generator or a reference model.

On the Cadence host use Xcelium for SV/UVM simulation, IMC for native coverage,
Jasper Superlint/CDC/FPV for the corresponding static/formal obligations and
Genus for synthesizability. The staged `eda/cadence_runner.py` offers
inspect/plan/run, `eda/project.example.json` shows the candidate inventory and
`eda/verification_profile.json` is the profile_path for `dv_generate_uvm`.
Materialize the actual candidate configuration as `eda/project.json`. Tool
versions/licenses and command compatibility are checked on the EDA host.

Freeze the applicable functional-bin denominator before regression and require
more than 90%, all mandatory directed obligations and negative/mutation checks,
with no unexpected UVM/SVA failures. Code, toggle and assertion metrics remain
separate. A tool exit or LoopX event completion is not functional acceptance.
Record raw reports/databases, seeds, tool versions and source/constraint/model
hashes. No Cadence compilation, simulation, CDC/formal proof or measured coverage
is claimed by this packaged question.

Independent original source bodies exceed 512 KiB without duplicate documents
or authored padding. That is a context-size measure, NOT implemented requirement
bytes. The case intentionally retains broader ISA/history for scope reasoning.
