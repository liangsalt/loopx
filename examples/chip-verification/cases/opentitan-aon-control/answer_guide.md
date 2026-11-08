# opentitan-aon-control: reference answer and acceptance guide

The problem contains original specification bodies from OpenTitan `c69eaea24a57c581ed274779887cb4a0c1405e3b`. This guide and the [review obligations](obligations.json) belong to the evaluator side. They are a source-grounded reference answer framework, not an assertion that generated RTL/TB has passed simulation. Keep this entire answer pack and [reference manifest](../reference/opentitan-c69eaea/manifest.json) out of the agent's problem workspace.

## What constitutes a complete answer

Selected IP types: pwrmgr, rstmgr, clkmgr, sysrst_ctrl, pinmux, alert_handler. The [integration profile](input/integration_profile.json) defines an authored reduced subsystem and its external boundaries. The upstream repository provides real leaf reference implementations, not this exact reduced wrapper. Different internal microarchitectures may be acceptable if they satisfy the source-defined observable behavior and declared interface contracts.

A complete result contains:

1. A versioned requirement inventory and shared module/interface graph. Each item traces to exact input source sections and named instances, clocks/resets, parameters, cross-module contracts and unresolved ambiguities. The RTL and DV flows consume the same graph revision.
2. A microarchitecture specification and RTL/IP composition per selected module plus a real integration wrapper. Define state transitions, data ownership, queues/backpressure, CSR side effects, clock/reset handling, timing assumptions and error containment. Label implementation choices distinctly from source requirements.
3. Reproducible build/file lists, lint and CDC/RDC analysis outputs. A declared synchronizer list does not establish CDC signoff; executed tools and applicable constraints must be recorded.
4. A spec-derived TP and UVM verification environment: active interface agents, monitors, UVM RAL/frontdoor checks, independent predictors/scoreboards, directed and constrained-random sequences, assertions and functional covergroups. Candidate RTL internals and hidden reference RTL cannot supply expected values during generation.
5. Execution artifacts: tool/version, file hashes, commands, seeds, compile/elaboration/run logs, assertion/scoreboard outcomes, native coverage database/report and reviewed exclusions. Compiling UVM classes alone does not establish checked transactions.

## How to evaluate

The 34 obligations in `obligations.json` cover major risk areas and map normative source sections to concrete checks, negative cases, coverage dimensions and actual upstream testpoint names. They are a review rubric, not exhaustive extraction of every CSR field and not a functional coverage denominator. Upstream testpoint links are comparison evidence; one listed leaf test does not prove an entire integrated obligation.

For each obligation, trace the candidate TP item to the actual sequence/checker/assertion and its execution evidence. Include a negative control or targeted mutation that makes the corresponding check fail; keep those runs separate from passing regression evidence. Then inspect missing cases against the pinned upstream testplans and independent spec review.

Resolve documented source conflicts before implementing dependent behavior. Record the alternatives, chosen normative interpretation and source anchors in a versioned decision. Do not silently choose the behavior of golden RTL. A missing normative detail remains unresolved until an explicit addendum is supplied.

## Coverage acceptance

The requested 90% is functional coverage of a reviewed, versioned applicable-bin model. Freeze that inventory and reviewed exclusions before closure iterations; retain covered/applicable counts and unhit bins. Report code coverage separately. A test cannot pass acceptance with assertion failures or scoreboard mismatches merely because coverage is high.

File sizes, extracted requirement counts, rubric completion and Python reference-model tests are not UVM simulation coverage. This pack has no certified simulation, formal proof, CDC/lint signoff or measured coverage result. Verify that the installed simulator supports the UVM/SystemVerilog features exercised by the actual environment; record unsupported features as tool gaps.

## Reference source and build limits

The [reference manifest](../reference/opentitan-c69eaea/manifest.json) identifies pinned original RTL, upstream UVM/formal sources, testplans and build descriptions by hash. Follow the original paths under `../reference/opentitan-c69eaea/upstream/`. Selected files are useful inspectable answers but do not prove a standalone dependency closure. Use the full pinned upstream checkout and its documented dependency/tool setup when resolving build targets.

The TL-UL README leaves its Bus Integrity Scheme unfilled. The input alone cannot define every integrity code or accepted fault. Explicitly supply a versioned normative addendum before claiming full integrity coverage. Do not inflate applicability exclusions to manufacture the requested percentage.

## Essential integrated behavior

- Delay AST, OTP, lifecycle and ROM startup acknowledgements independently. Check PWRMGR/CLKMGR/RSTMGR/PINMUX clock, reset, strap and execution ordering without always-ready environment tieoffs.
- Drive a SYSRST key/combo or retained-pad wake through the actual PWRMGR vectors into coordinated clock/reset restoration. Include abort/fall-through and wake/reset races, plus cause retention and clearing.
- Escalate a distinguishable alert class through the power-manager reset path and capture the alert dump in RSTMGR. Exercise low-power-group qualification and missed pings while clocks gate and restart.
- Check PINMUX data/output-enable sleep retention, USB wake handoff with the main clock stopped, strap capture and lifecycle-qualified TAP behavior across non-debug reset.

PINMUX has upstream FPV/security testplans and formal properties, but no upstream UVM environment at this revision. Its generated UVM environment and the integrated subsystem TB require new review and actual execution. Formal reference files do not certify either a UVM run or a proof.

Upstream starting points: [PWRMGR testplan](../reference/opentitan-c69eaea/upstream/hw/top_earlgrey/ip_autogen/pwrmgr/data/pwrmgr_testplan.hjson), [RSTMGR testplan](../reference/opentitan-c69eaea/upstream/hw/top_earlgrey/ip_autogen/rstmgr/data/rstmgr_testplan.hjson), [CLKMGR testplan](../reference/opentitan-c69eaea/upstream/hw/top_earlgrey/ip_autogen/clkmgr/data/clkmgr_testplan.hjson), [SYSRST testplan](../reference/opentitan-c69eaea/upstream/hw/ip/sysrst_ctrl/data/sysrst_ctrl_testplan.hjson), [PINMUX formal testplan](../reference/opentitan-c69eaea/upstream/hw/ip/pinmux/data/pinmux_fpv_testplan.hjson), [ALERT_HANDLER testplan](../reference/opentitan-c69eaea/upstream/hw/top_earlgrey/ip_autogen/alert_handler/data/alert_handler_testplan.hjson).
