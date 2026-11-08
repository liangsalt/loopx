# opentitan-serial-io: reference answer and acceptance guide

The problem contains original specification bodies from OpenTitan `c69eaea24a57c581ed274779887cb4a0c1405e3b`. This guide and the [review obligations](obligations.json) belong to the evaluator side. They are a source-grounded reference answer framework, not an assertion that generated RTL/TB has passed simulation. Keep this entire answer pack and [reference manifest](../reference/opentitan-c69eaea/manifest.json) out of the agent's problem workspace.

## What constitutes a complete answer

Selected IP types: spi_host, spi_device, usbdev. The [integration profile](input/integration_profile.json) defines an authored reduced subsystem and its external boundaries. The upstream repository provides real leaf reference implementations, not this exact reduced wrapper. Different internal microarchitectures may be acceptable if they satisfy the source-defined observable behavior and declared interface contracts.

A complete result contains:

1. A versioned requirement inventory and shared module/interface graph. Each item traces to exact input source sections and named instances, clocks/resets, parameters, cross-module contracts and unresolved ambiguities. The RTL and DV flows consume the same graph revision.
2. A microarchitecture specification and RTL/IP composition per selected module plus a real integration wrapper. Define state transitions, data ownership, queues/backpressure, CSR side effects, clock/reset handling, timing assumptions and error containment. Label implementation choices distinctly from source requirements.
3. Reproducible build/file lists, lint and CDC/RDC analysis outputs. A declared synchronizer list does not establish CDC signoff; executed tools and applicable constraints must be recorded.
4. A spec-derived TP and UVM verification environment: active interface agents, monitors, UVM RAL/frontdoor checks, independent predictors/scoreboards, directed and constrained-random sequences, assertions and functional covergroups. Candidate RTL internals and hidden reference RTL cannot supply expected values during generation.
5. Execution artifacts: tool/version, file hashes, commands, seeds, compile/elaboration/run logs, assertion/scoreboard outcomes, native coverage database/report and reviewed exclusions. Compiling UVM classes alone does not establish checked transactions.

## How to evaluate

The 25 obligations in `obligations.json` cover major risk areas and map normative source sections to concrete checks, negative cases, coverage dimensions and actual upstream testpoint names. They are a review rubric, not exhaustive extraction of every CSR field and not a functional coverage denominator. Upstream testpoint links are comparison evidence; one listed leaf test does not prove an entire integrated obligation.

For each obligation, trace the candidate TP item to the actual sequence/checker/assertion and its execution evidence. Include a negative control or targeted mutation that makes the corresponding check fail; keep those runs separate from passing regression evidence. Then inspect missing cases against the pinned upstream testplans and independent spec review.

Apply the revised problem's source adjudications before implementing dependent behavior. Their author decisions are the exercise oracle; cite both the correction and pinned evidence. Remaining local gaps require explicit addenda only for affected acceptance claims. Never silently select golden RTL behavior.

## Coverage acceptance

The requested 90% is functional coverage of a reviewed, versioned applicable-bin model. Freeze that inventory and reviewed exclusions before closure iterations; retain covered/applicable counts and unhit bins. Report code coverage separately. A test cannot pass acceptance with assertion failures or scoreboard mismatches merely because coverage is high.

File sizes, extracted requirement counts, rubric completion and Python reference-model tests are not UVM simulation coverage. This pack has no certified simulation, formal proof, CDC/lint signoff or measured coverage result. Verify that the installed simulator supports the UVM/SystemVerilog features exercised by the actual environment; record unsupported features as tool gaps.

## Reference source and build limits

The [reference manifest](../reference/opentitan-c69eaea/manifest.json) identifies pinned original RTL, upstream UVM/formal sources, testplans and build descriptions by hash. Follow the original paths under `../reference/opentitan-c69eaea/upstream/`. Selected files are useful inspectable answers but do not prove a standalone dependency closure. Use the full pinned upstream checkout and its documented dependency/tool setup when resolving build targets.

The TL-UL README leaves its Bus Integrity Scheme unfilled. The input alone cannot define every integrity code or accepted fault. Explicitly supply a versioned normative addendum before claiming full integrity coverage. Do not inflate applicability exclusions to manufacture the requested percentage.

## Essential integrated behavior

- Drive an external SPI host through SPI_DEVICE command filtering/address or payload transformation, the actual SPI_HOST0 passthrough interface and an external flash model. Observe both serial interfaces and independently check downstream side effects and returned data.
- Concurrently access the selected IP through TL-UL while SPI queues stall and USB available/receive buffers fill. Check peripheral isolation, exact byte ownership, status and interrupts.
- Exercise USB suspend handoff to a controllable pinmux/AON boundary, remove the functional clock, inject resume/reset/sense loss and complete the wake handshake. The complete PINMUX RTL is outside this case.
- Reset during segmented SPI transfers, passthrough ownership changes and USB link/buffer transitions. Verify documented local reset effects and explicitly specified integration recovery assumptions.

There is no direct USB-to-SPI data path in this case. USB and SPI share software access and infrastructure; SPI_DEVICE-to-SPI_HOST0 passthrough is the direct functional relationship.

SH-05 fixes the exercise LEN contract to nine bits; SH-03/SH-04 fix period/timeslice formulas. Preserve the selected single-clock SPI_HOST interface. Event-interrupt temporal qualification still needs its separate assertion/deassertion and enable-while-active contract; resolved event identities do not supply that timing rule. Do not adjust the oracle to match the reference implementation.

Upstream starting points: [SPI_HOST testplan](../reference/opentitan-c69eaea/upstream/hw/ip/spi_host/data/spi_host_testplan.hjson), [SPI_DEVICE testplan](../reference/opentitan-c69eaea/upstream/hw/ip/spi_device/data/spi_device_testplan.hjson), [USBDEV testplan](../reference/opentitan-c69eaea/upstream/hw/ip/usbdev/data/usbdev_testplan.hjson).

<!-- BEGIN SOURCE-ADJUDICATIONS -->
## Revised-question acceptance

Use `opentitan-adjudications-v1`: 27 applicable grouped issue records plus one routed scope policy. The reported historical 66 log rows are not 66 independent defects. See `input/problem.md` for the normative rules and `adjudication_obligations` in the rubric for the required checks. Original upstream prose is preserved as provenance; the authored corrections revise the question.

For each applicable record, inspect RTL/TP/UVM traceability and execute its independent checker or scope audit. A defined contract is not a passing test. Classify observed results as passed, failed, untested or locally blocked. Background scope audits and not-a-conflict decisions do not add DUT coverage bins. Do not count unknown behavior as covered, delete unresolved requirements from the frozen denominator or treat all Q items as proven bugs.

Precise remaining local dependencies: CT-01, Q-03, Q-04, D-02. Their documents identify the affected binding/behavior; they do not globally block extraction, design or DV of specified behavior. Additional temporal or integration gaps outside the attached list remain explicit where already listed in the rubric.

The held-out pinned implementation is comparison material, not absolute authority over author-chosen conventions. If it disagrees with an exercise convention, record reference divergence and evaluate the generated candidate against the accepted exercise contract. No updated document establishes simulation success or a coverage percentage.
<!-- END SOURCE-ADJUDICATIONS -->
