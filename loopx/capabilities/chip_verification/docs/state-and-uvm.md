# State, context and UVM contracts

This is an opt-in built-in LoopX capability. Importing a specification creates
domain work inside the selected workspace; discovery, help and an uninitialized
status query do not create domain state or start a model. TypeScript admits
capability requests. Python performs specialized spec parsing, candidate
validation and simulator evidence processing. Existing LoopX Goal, Todo, quota,
host and scheduler authority is unchanged.

## Durable state and disposable sessions

`.loopx/chip-verification/state.sqlite3` holds source baselines, traceability,
domain artifact tasks, attempts, evidence and events. Files carry large immutable
inputs and candidate outputs; files and JSON are interchange artifacts, not a
manual checklist that the user must update. Ordinary use is through the Trae
conversation and MCP tools. A worker's conversation is never needed to determine
what has been accepted or what must run next.

The same specification bytes identify a baseline. Changing the specification
creates a revision and invalidates old derived work. Source fragments preserve
JSON pointers, offsets, hashes and neighbors. Explicit document/layer/module/
feature labels produce intersecting views of the same sources, not independent
copies of requirements. Arbitrary JSON can be ingested without a custom wrapper;
the optional `dvflow.spec_bundle` interchange schema supplies richer hierarchy.
Declared cross-layer edges remain candidate relations until reviewed and verified.

## Domain stages

| Stage | Input and accepted output | What acceptance does not establish |
|---|---|---|
| Import | Snapshot, lossless fragment inventory, source membership | Semantic understanding |
| Model | Bounded source batch → sourced candidate requirements, dispositions and questions | Specification correctness or signoff |
| Plan | Candidate requirements → stimulus, observations, independent checkers, functional coverage goals, configuration and UVM ownership | Simulation or coverage closure |
| Architecture | Accepted TP scope, actual RTL declaration and optional sourced profile → acyclic UVM component graph, shared contracts, exclusive files | Compilable code |
| Implement | One component, complete required ancestor contracts/files and mapped TPs → validated candidate files | Build or behavioral correctness |
| Build | Bound manifest and input hashes → actual compile/elaboration evidence | Runtime stimulus or checker activity |
| Smoke | Successful build → DUT transactions and positive checker evidence without UVM errors/fatals | Coverage closure or full specification verification |
| Regress / Analyze | Qualified backend campaign and fixed coverage denominator → measured holes and proposed additional stimulus | Generic coverage support from a tool that lacks it |

The public Trae path currently supports Import through Build/Smoke. Generic XSim
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
silently truncated. Oversized contracts become explicit blockers. This is
structural validation: compiling and testing the candidate remains necessary.

## Host and EDA lifetimes

The Trae main Agent reads compact status/next summaries. A native child worker
claims one task, reads its bounded context, submits and returns references. The
24,000-character default includes the serialized host contract and schema; it is
not a guarantee of a tokenizer-specific count. The host decides how to create
child sessions; the capability does not claim an unverified Trae session API.

An EDA invocation returns a background job ID immediately. Its process, input
snapshot, logs and result outlive the MCP connection. A new connection polls it.
A dead process is reported as interrupted; retry is explicit. Unsupported or
ignored assertions make smoke evidence unqualified even when the UVM error
counter is zero. The checker is not weakened to obtain a passing result.
