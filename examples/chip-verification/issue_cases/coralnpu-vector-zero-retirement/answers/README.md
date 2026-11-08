# Reference answer for the independent Cadence exercise

The executable acceptance boundary is `coral_vector_retirement_subsystem` as specified in the input's
`cadence_contract.md`. The answer is a behavior and interface oracle, not a claim
that upstream Coral code implements the new authored port contract.
`review_rubric.json` maps acceptance to each normative section. A correct solution
uses `behavior_oracle.md` for source-traceable expected-state reasoning and
concrete input/result witnesses; these are not executed EDA results. A solution
may choose different microarchitecture if it meets all observable obligations,
including four accepted command slots and concurrent independent reads.

The standard reasoning is to separate legality, execution completion and ordered
retirement. Keep per-command state and unique outstanding-memory identities;
complete legal empty work without waiting for nonexistent effects, enforce all
required ordering edges, and commit only defined retirement effects. A flush
drains physical work already issued and cannot undo a store accepted by memory.
The UVM scoreboard must compute expected values and ordering independently from
the input stream and authored contract. Include the stated mutations and retain
unreachable-bin review separately from observed coverage.

Coral issue #89 and correction `5c599a884fb07b25f12be6cd027c12e93a99de45` remain historical
evidence of zero-element legality gating from vector backend decode. `reference/` and
`upstream.patch` are preserved at their immutable Git blobs. They are not a
standalone build, an execution dependency or the golden implementation of the
new exercise. Upstream Bazel/Chisel/Cocotb targets are not acceptance commands.
Reproducing the original Coral whole core would be a separate benchmark.

Cadence acceptance uses the candidate's complete SV/UVM closure with Xcelium,
native IMC coverage, Jasper lint/CDC/FPV and Genus synthesizability evidence.
The required interface is single-clock; report actual CDC assumptions/results.
More than 90% applies to the frozen applicable functional-bin denominator and
does not replace mandatory directed checks, mutation detections or zero
unexpected UVM/SVA errors. Source/patch validation is not simulation or proof.
No Cadence compilation, simulation, formal/CDC result or measured percentage is
claimed for this package. Tool versions and licenses belong to the EDA host.

The original reference files and LICENSE retain Apache-2.0 attribution. RISC-V
documents and their CC-BY-4.0 license remain unchanged in the input. New authored
contract/port assets are Apache-2.0 under this repository's license.
