# Shared specification, design and UVM state

This is an opt-in built-in LoopX capability. Importing a specification creates
domain work inside the selected workspace; discovery, help and an uninitialized
status query do not create domain state or start a model. TypeScript admits
capability requests. Python performs specialized spec parsing, candidate
validation, RTL checks and simulator evidence processing. Existing LoopX Goal, Todo, quota,
host and scheduler authority is unchanged.

## Durable state and disposable sessions

`.loopx/chip-verification/state.sqlite3` holds source baselines, traceability,
domain artifact tasks, attempts, evidence and events. Files carry large immutable
inputs and candidate outputs; files and JSON are interchange artifacts, not a
manual checklist that the user must update. Ordinary use is through the Trae
conversation and MCP tools, or another runtime using the CLI/provider. An executor's conversation is never needed to determine
what has been accepted or what must run next.

The same specification bytes identify a baseline. Changing the specification
creates a revision and invalidates the entire old baseline's derived work; this
increment does not claim source-granular reuse across specification revisions.
Within a baseline, changed accepted artifacts invalidate their transitive dependents.
`revise` accepts a concrete diagnostic for a completed RTL or UVM component,
invalidates its dependent evidence and creates a fresh attempt on the same source
contract. Candidate corrections do not rewrite accepted files. Tool-task retries
return `check-design` or `build-uvm` rather than creating an unexecutable model task.
Source fragments preserve
JSON pointers, offsets, hashes and neighbors. Explicit document/layer/module/
feature labels produce intersecting views of the same sources, not independent
copies of requirements. Arbitrary JSON can be ingested without a custom wrapper;
the optional `dvflow.spec_bundle` interchange schema supplies richer hierarchy.
Declared cross-layer edges remain candidate relations until reviewed and verified.

## Entering at any specification view

The public import defaults to `flow="design-dv"`. Existing RTL verification can
select `flow="dv"`; `prepare-design` enables both tracks in an existing project.
FS, AS, microarchitecture and interface documents are input views, not mandatory
serial workflow steps. A module can have several views, and a document can mix
them. The source-entry contract records supplied views independently of the
owning module scope. Missing FS/AS paperwork does not prevent adopting supplied
microarchitecture. Undefined behavior required for implementation or an
independent correctness check remains a concrete blocker.

Source interpretation initially finishes before module contracts are materialized,
so the first module identity map uses the complete source inventory. The runtime
still reads one structural source task at a time. Module names retain exact
identities; aliases and relationships are not guessed from co-occurrence.

A system/functional entry with no supplied partition starts from a scope root.
Its shared contract can propose child modules as explicit `design_choice`
records with source requirement references, qualified names and stable
`design_module:<qualified-name>` identities. Parent references must form an
acyclic hierarchy rooted in the owning scope. Original source module identities
remain unchanged. Both tracks see these same proposed architectural choices;
the pipeline does not force a new decomposition for supplied microarchitecture.
An input containing only context and no behavior/design obligations reports
`entry_readiness.status=insufficient_behavior`, records a source blocker and
cannot export an empty successful testplan.

`spec-contract` records canonical module identities, the selected ports and
configuration, clock/reset domains, cross-module obligations, requirement
provenance and unresolved questions. Each supplied fact has source requirement
references; proposed implementation decisions remain `design_choice` records.
Clock-domain fields reference clock identities, while clock/reset `port` fields
reference physical ports. Supplied periods remain supplied facts. When a source
does not specify a clock target, its period may remain unknown, or the contract
may record an explicit implementation/test target as `origin=design_choice`.
A concrete chosen target requires `timing_basis=implementation_target|test_clock`,
source requirement references, a rationale and `performance_guarantee=false`.
This enables a declared check configuration without inventing a supplied
performance guarantee. Undefined normative timing, latency or throughput
semantics remain blockers; the chosen clock does not satisfy those obligations.

Supplied environmental timing, reset assumptions and interface/configuration
facts remain source-backed `applicability=context` records when they are not DUT
behavior obligations. Relevant module context, and explicitly unassigned global
context, accompany each affected scope; context-only modules do not create design
targets or mandatory standalone testpoints. These shared dependencies can appear
in several scope packets without duplicating their canonical source identities.
When a scope's required source set expands, its old contract and dependent work
are invalidated together. Explicit replacement records distinguish superseded
history from current artifact failures in readiness and testplan exports.

Both `contract-plan` and `microarchitecture` depend on the same accepted contract
task and content hash. Testplan expectations come from the source requirements.
The contract supplies common bindings to both tracks; generated RTL behavior is
never promoted into a verification oracle. Sibling scopes must agree on shared
interfaces and relation identities. A structurally accepted contract may retain
blocking questions, but it cannot release downstream work in that condition.

Requirements are organized by declared modules, functions, interfaces and
relationships. Source objects and document sections remain complete, and each
engineering stage works on its own artifact or component. Character or token
counts neither select boundaries nor reject a task; an unstructured paragraph
remains whole. Canonical module identity is unchanged; scope identity,
index/count, `partial_module` and `composition_required` preserve the remaining
integration obligation. Sibling interface bindings and mandatory policy sources
are supplied without truncation.
Automatic integration of partial module candidates is not implemented yet:
engineering status reports this obligation and the simulation join refuses a
partial candidate. Local scope completion does not establish whole-module closure.

## Domain stages

| Stage | Input and accepted output | What acceptance does not establish |
|---|---|---|
| Import | Snapshot, lossless fragment inventory, source membership | Semantic understanding |
| Model | Bounded source batch → sourced candidate requirements, dispositions and questions | Specification correctness or signoff |
| Spec-contract | Source requirements for one module scope → common interface, configuration, module and relation contract | Semantic truth, resolved questions or complete module integration |
| Contract-plan | The shared contract and original requirements → stimulus, observations, independent checkers, functional coverage goals and UVM ownership | Simulation or coverage closure |
| Microarchitecture | The same contract → adopted or refined internal behavior, owned RTL units and pinned local IP bindings | Correct RTL or completed tool checks |
| RTL | Accepted microarchitecture → immutable RTL/IP candidate bytes | Compilation, lint, CDC or functional correctness |
| RTL-check | Exact candidate bytes and constraints → separate elaboration/synthesis, lint and CDC evidence | ASIC signoff, RDC or functional correctness |
| Architecture (UVM) | Accepted TP scope and shared port declaration, or actual RTL on the compatibility path → acyclic UVM component graph and exclusive files | Compilable code |
| Implement (UVM) | One component, complete required ancestor contracts/files and mapped TPs → validated candidate files | Build or behavioral correctness |
| Build | Bound manifest and input hashes → actual compile/elaboration evidence | Runtime stimulus or checker activity |
| Smoke | Successful build → DUT transactions and positive checker evidence without UVM errors/fatals | Coverage closure or full specification verification |
| Regress / Analyze | Qualified backend campaign and fixed coverage denominator → measured holes and proposed additional stimulus | Generic coverage support from a tool that lacks it |

The compatibility DV-only flow retains the original `plan` stage and actual RTL
input. The design/DV flow can generate UVM before RTL exists, using the shared
contract's declaration. `join-design` later requires matching contract revisions,
accepted design and UVM file inventories, and current static-check evidence.
It produces a bound manifest for the existing Build/Smoke path. Parameterized
instance joins are not qualified and are rejected rather than assuming defaults.

Generic XSim
coverage is explicitly `not_evaluated`; the specialized regression backend is an
internal extension seam and is not exposed as arbitrary-chip coverage closure.
Reaching a requested percentage requires a qualified simulator coverage adapter,
a reviewed coverage model and its stable denominator. Never replace these with
the proportion of completed tasks, generated tests or passing smoke checks.

## Attempt lifecycle

| State | Entry condition | Allowed continuation |
|---|---|---|
| Pending | Inputs recorded, dependencies may still be waiting | Claim only after all dependencies are accepted/current |
| Running | Unique attempt, task revision and lease recorded | Heartbeat; submit; abandon; lease recovery |
| Done | Trusted application validator accepts matching inputs and durable evidence | Reuse; invalidate if inputs/evidence change |
| Failed | Execution failed without a successful result | Explicit retry or corrected input |
| Blocked | Context cannot fit, unresolved contracts or missing tool capability | Resolve the specific blocker, then retry |
| Stale | Changed baseline, prerequisite or artifact invalidated a result | Rebuild from current inputs |
| Cancelled | Explicit cancellation or a validated lossless partition supersedes a task | It is never itself evidence of completion |

A lease fences submissions, not the host's actual session identity. `session_id`
is an execution label. `attempt_id` plus baseline/task revision determines whether
a response can still be accepted. An expired or superseded attempt is rejected.
An identical completed submission replays its receipt; a changed response is
rejected. If a process dies after the task commit but before returning a receipt,
the accepted response artifact reconstructs the receipt and derived graph.

Read projections do not recover leases or invalidate evidence. Explicit writes
perform recovery and reconciliation. A subsequent claim can recover an expired
worker without needing its transcript. Status/report rechecks baseline/evidence
when relevant and does not turn stale data into a new verification claim.

## UVM decomposition

Partition verification around interfaces, behavior, shared resources and
dependencies. A hardware module is a useful ownership boundary but does not
require its own UVM agent. Reuse agents by protocol; use virtual sequences for
multi-agent behavior. Keep the DUT's specified behavior separate from testbench
choices such as clock period or stimulus distribution.

| Concern | Typical UVM ownership |
|---|---|
| Wire protocol and transaction sampling | Interface, transaction, driver, monitor, sequencer, reusable agent |
| Scenario and scheduling | Sequence, virtual sequence, virtual sequencer where needed |
| Correctness | Monitor observations, predictor/reference model and scoreboard |
| Functional coverage | Observed DUT events and reviewed coverpoints/crosses |
| Configuration and registers | Config objects and optional RAL model/adapter/predictor |
| Composition | Environment, `uvm_test`, harness, build manifest |

The architecture validator requires stimulus/checker/coverage mappings for each
testpoint, an acyclic dependency graph and exclusive output ownership. Components
receive required shared types and ancestor files; those dependencies are never
silently truncated. Their size does not create a context blocker. This is
structural validation: compiling and testing the candidate remains necessary.

## Host and EDA lifetimes

One selected Agent runtime reads compact status/next summaries, claims one task,
reads the complete context of that module, function, relationship or stage and
submits. It may use a fresh session or an optional native child session to keep
earlier task context out of subsequent work. Context size is measured for
observation only; there is no fixed character or token ceiling. The host owns actual session
creation and context management. Two workflow tracks do not require two agents,
and the capability does not claim an unverified Trae session API.

Legacy `chunk_chars` and `context_chars` arguments remain compatibility inputs
and do not influence decomposition or admission. Existing workspaces retain
historical inventories and receipts. The normal import entrypoint migrates an
old character-based decomposition to the structural strategy, invalidating its
derived work without rewriting earlier evidence; read-only status identifies
when that migration is needed. Actual runtime/tool failures remain visible.

A native EDA invocation returns a background job ID immediately. Its process, input
snapshot, logs and result outlive the MCP connection. A new connection polls it.
A dead process is reported as interrupted; retry is explicit. Unsupported or
ignored assertions make smoke evidence unqualified even when the UVM error
counter is zero. The checker is not weakened to obtain a passing result.

The design-check provider records Vivado synthesis on its selected FPGA check
target, Verible syntax/style lint, and Vivado structural CDC on the constrained
design. These are separate observations, not comprehensive RTL lint or ASIC
signoff. Missing tools, incomplete clock-domain/period constraints, unrecognized
reports or changed inputs do not pass. A clockless design is not silently marked
CDC-not-applicable. Evidence retains the check target, tool identity, commands,
input hashes and raw report/log hashes for revalidation.

An explicit generation profile can instead select `cadence_xcelium`. The
architecture and component tasks then receive one external Cadence manifest
contract, including the complete RTL/TB source closure, separate DUT and TB
tops, native UVM, test/seed, constraints and a frozen coverage model. Unknown
or conflicting simulator choices block generation. Omitting a profile preserves
the existing Vivado target; Cadence never silently falls back to it.
`build-uvm` infers the target from the generated manifest and returns
`external_execution_required` with the staged `eda/cadence_runner.py` entrypoint.
It launches no native job and claims no compilation, simulation or coverage.
The Cadence host performs those steps with its installed tools and qualified
scripts. Native `check-design` and `join-design` remain Vivado-specific; external
composition must preserve the same accepted shared contract and full input
closure. Run events and Watchdog observations do not accept tool evidence or
mark those checks complete. See the [Cadence path](../README.md#cadence-题目执行路径).

## Public actions and owners

Ordinary use remains conversational. `dv_import`, `dv_claim`, `dv_context` and
`dv_submit` drive task-scoped work; `dv_engineering_status` exposes the shared state.
`dv_generate_uvm` accepts `contract_task_id` for contract-first generation.
Native `dv_check_design` and `dv_build_uvm` return durable background job receipts;
`dv_job_status` reads them across reconnects. `dv_join_design` binds the accepted
tracks before the design-backed simulation. `dv_report` distinguishes current
evidence from unknown coverage. CLI actions use the same provider and TypeScript
admission boundary; no alternate state machine lives in the host instructions.

| Owner | Responsibility |
|---|---|
| `engine/spec.py`, `engine/workflow.py` | Lossless structural import, source task extraction, requirement traceability and recovery |
| `engine/engineering.py` | Entry views, shared module/function contracts, relationship scopes and both-track dependencies |
| `engine/design.py` | Microarchitecture adoption/refinement, RTL ownership and pinned IP candidates |
| `engine/generation.py` | UVM architecture/components; rejects DUT redefinition in TB output |
| `engine/design_checks.py` | Tool-backed static evidence and current-input validation |
| `engine/joining.py`, `engine/build.py` | Matching design/DV revisions, bound manifests and simulation admission |
| `engine/state.py`, `host_work.py` | Domain tasks, attempts, leases, artifact evidence and session-independent submission |
| `provider.py`, `cli.py`, `mcp_server.py`, `jobs.py` | Existing capability transports and durable EDA jobs |
