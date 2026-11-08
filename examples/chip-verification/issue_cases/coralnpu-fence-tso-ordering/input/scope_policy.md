# Scope policy: independent Cadence subsystem

The acceptance target is `coral_memory_order_subsystem`, the authored standalone SV boundary in
case-cadence-contract (`cadence_contract.md`), not the original Coral whole core.
The Cadence contract defines exact ports, widths, clocks/resets, supported
operations, memory handshakes, fault/flush rules and liveness assumptions.
It accompanies original source fragments as mandatory source adjudication.
Derive obligations from that contract plus applicable ISA clauses; source size
is broader context, NOT implemented requirement bytes.

Coral issue #130, its original source and pinned correction explain the
historical symptom. They are not a required RTL dependency or a golden model.
Do not require the original repository, Chisel/firtool, Bazel, Cocotb, Verilator,
MPACT, a RISC-V compiler or ELF execution. Candidate RTL/TB is standard SV/UVM
with an explicit closure and executes on Cadence Xcelium/IMC, Jasper and Genus.
The baseline/ files are historical_mechanism_reference only; they are excluded
from the candidate compile inventory. Missing native build tools do not block
this new exercise; missing candidate RTL, valid constraints or Cadence licenses
are reported honestly and never counted as successful execution.

Zero-element compliance is background, NOT an additional repair target.
There is no vector opcode in this standalone decoder. The selected scalar and
fence encodings are exactly M03. FENCE.TSO requires R->R, R->W and W->W order;
stronger full RW,RW serialization is legal. Do not require optional W->R
reordering to occur or count its absence as a missing mandatory coverage bin.

Never turn descriptive examples, unsupported extensions, full-chip software or
upstream build recipes into new implementation obligations. Preserve concrete
ambiguities locally. Coverage requires frozen applicable bins and actual native
Cadence evidence; these authored scope decisions are not simulation evidence.
