# OpenTitan serial I/O subsystem design and UVM verification problem

Build a specification-driven design and DV result for the selected serial I/O subsystem. The in-scope IP identities are `spi_host`, `spi_device`, `usbdev`. This is a bounded subsystem exercise using one pinned OpenTitan revision, not a request to implement all of Earlgrey or a claim that this subset is a standalone upstream top-level.

## Input authority and hierarchy

`spec.json` contains unchanged public system/architecture/module/behavior/register documents, normative register configuration, and an explicitly authored integration profile and module graph. The source manifest records which original prose bodies qualify the >512 KiB size floor. System background and whole-top context do not expand the implementation scope. Register HJSON and selected integration context do not count toward that floor. Upstream RTL, existing testplans, UVM code and answer obligations are not part of the exported input.

Use `integration_profile.json` to distinguish normative requirements, selected configuration, external environment assumptions, and proposed design choices. Use `module_graph.json` as an initial shared relationship map; source-check and refine it without silently converting a proposal to a proven contract. TL-UL/comportability documents constrain applicable interfaces; they do not by themselves require redesigning an entire SoC fabric. Retain concrete contradictions, missing dependencies and ambiguity with source anchors. Where prose and HJSON conflict, record the disagreement and block affected decisions instead of guessing precedence.

## Required design and verification outputs

1. Import the specification by path through LoopX. Persist every requirement and its provenance; work in bounded contexts and recover from durable state in fresh host sessions.
2. Create one shared module/interface/configuration graph for both tracks. Preserve boundary assumptions, clock/reset domains, address/register contracts, cross-module handshake and error behavior, and any unresolved ownership or composition obligations.
3. Design track: adopt or refine the available microarchitecture, propose explicit choices where the source permits them, and produce reviewable synthesizable RTL or a declared fixed-version IP binding. Produce real elaboration/lint/CDC evidence where tools and complete constraints permit it.
4. DV track: derive TP directly from the same specification and shared contract. Generate UVM transactions, sequencers/sequences, drivers, passive monitors, independent scoreboards, functional coverage, environment, test classes and top-level binding. Cover normal operations, boundaries, backpressure, reset/error paths, and the cross-module scenarios justified by the sources.
5. Join the two tracks only at the same accepted contract/configuration revision. Preserve all required composition work; a fragment is not a completed module. Run simulation only when a complete build and checker contract exists.
6. Report passed, failed, blocked and untested stages separately. Requirement extraction, test counts, generated files and smoke success are not coverage. Declare any percentage only from real simulator evidence using a frozen bin denominator; functional coverage and RTL code coverage are separate metrics.

## Execution and evaluation boundary

Start an installed LoopX capability in this input-only workspace with the selected Codex or TRAE host. Use public `dv_import`, `dv_next`, `dv_claim`, `dv_context`, `dv_submit` and relevant design/DV tools; do not manufacture state acceptance or direct database edits. The fixture is inert and does not automatically invoke models or EDA tools. Raw sessions, candidates, private state and logs belong in the execution workspace, never back in this public corpus.

An evaluator may compare outputs against a separately held pinned upstream reference implementation and verification material. Multiple correct RTL architectures and UVM organizations are acceptable; textual similarity is not the correctness criterion. File-only staging is not a claim of network/filesystem isolation from public upstream answers. Simulator/dependency qualification and any 90% target remain separate execution gates.
