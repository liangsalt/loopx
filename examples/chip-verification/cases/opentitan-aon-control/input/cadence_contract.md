# Cadence standalone execution contract: opentitan-aon-control

Authority: benchmark author, revision opentitan-adjudications-cadence-v2. This is a new digital subsystem
implementation exercise derived from the supplied sources. It is not a binary-compatible
OpenTitan IP port, a production pad-ring implementation, or an upstream regression replay.

Effective rule: Implement the selected subsystem as self-contained synthesizable SystemVerilog with an independent SystemVerilog UVM/SVA environment. Cadence Xcelium/IMC and Jasper/Genus are the execution targets. Upstream RTL, build descriptions and tests are historical comparison material, not required build inputs or an independent golden for this authored contract.

## Implementation and source boundary

The DUT top is `aon_control_subsystem`; all modules listed in integration_profile.json
remain in scope. Implement their documented behavior and register fields under the named
adjudications. Supply ordinary .sv/.svh files, behavioral synthesizable RAMs and explicit
packages. No FuseSoC, DVSim, reggen/topgen, Bazel, Chisel/firtool, Verilator, C/C++ simulator,
software compiler, vendor VIP or upstream source checkout is required to solve this question.
Generated CSR RTL must be checked against the supplied HJSON fields; parsing HJSON is an
authoring aid, not an execution dependency. No encrypted IP or unavailable technology cell
may be introduced. A reused IP is allowed only with its complete pinned portable SV closure.
The saved upstream RTL/UVM/FPV subset has historical provenance, not a qualified Cadence
dependency closure. Its output never silently overrides an authored rule.

Both design and DV consume the same module_graph.json and this contract. Every proposal
identifies owned modules, interfaces, clocks, reset domains and integration obligations.
Do not replace the subsystem with a one-register smoke or count isolated module tests as
integrated closure. Unspecified internal pipelines are design choices, not hidden golden
latencies. Freeze chosen internal interface and assertion contracts before implementation.

## Top-level digital boundary

Use the following signal names; an unsized name denotes one logic bit, `_i` inputs and `_o`
outputs. Every `_no` is an active-low reset output. All vectors descend from N-1 to zero.
The common bus ports are `tl_a_valid_i`, `tl_a_ready_o`, `tl_a_i` (bench_tl_req_t),
`tl_d_valid_o`, `tl_d_ready_i`, `tl_d_o` (bench_tl_rsp_t), declared in bench_tl_pkg.

Additional ports: clk_core_i, clk_aon_i, clk_main_i, clk_io_i, clk_usb_i, rst_ni; sleep_req_i, wakeup_i[5:0], reset_req_i[3:0], ndm_reset_i, lifecycle_prod_i, ast_power_good_i; boot_req_o[3:0], boot_ack_i[3:0], boot_error_i[3:0], edn_req_o, edn_ack_i, edn_data_i[31:0], edn_error_i; mio_in_i[46:0], mio_out_o[46:0], mio_oe_o[46:0], dio_in_i[15:0], dio_out_o[15:0], dio_oe_o[15:0], dio_peripheral_data_i[15:0], dio_peripheral_oe_i[15:0], dio_peripheral_sample_o[15:0]; peripheral_in_i[74:0], peripheral_out_o[56:0], peripheral_oe_i[74:0]; alert_p_i[64:0], alert_n_i[64:0], alert_ack_p_o[64:0], alert_ack_n_o[64:0], esc_p_o[3:0], esc_n_o[3:0], esc_resp_p_i[3:0], esc_resp_n_i[3:0]; pwr_enable_o, isolation_o, clock_enable_o[7:0], clock_ack_i[7:0], clock_leaf_o[7:0], clock_idle_i[3:0], extclk_req_o, extclk_hi_speed_o, extclk_ack_i, jitter_req_o, rst_lc_no, rst_sys_no, rst_module_no[5:0], irq_sysrst_o, irq_alert_o[3:0], wakeup_record_o[5:0].

IRQ vector bit indices follow the pinned interrupt_list order; unused bits are zero.
Serial alert_o bits are spi_host0, spi_device, usbdev in order and are observed digital
error outputs. They stay asserted until reset after a fatal integrity event and do not
require an unavailable alert receiver package. AON peripheral slots are numbered by
the selected pinmux configuration (75 outputs into the mux, 57 inputs out of it).
All 16 DIO entries retain the generated HJSON geometry; DIO0/1 model IOR8/IOR9,
and DIO2..15 are digital dedicated peripheral pins numbered in order, not extra MIO.
For DIO0/1 use the internal SYSRST ec_rst_l/flash_wp_l outputs; DIO2..15 use
dio_peripheral_data_i/oe_i. All DIO sample outputs apply the configured attributes.
Do not infer analog current, slew delay, metastability immunity or power signoff
from these digital ports. Where a listed generic signal is unused in this subsystem, bind
it by an explicit source-traced design choice, retain it in the shared contract and test
the binding. AON reset leaf vectors use profile.modules order. Each selected
peripheral leaf has LC as its sole reset parent, while rst_sys_no resets the external
CPU boundary; POR asynchronously asserts both. LC assertion propagates to SYS, but
an isolated SYS reset does not assert LC. Release is synchronized in each leaf domain.
Boot vector order is OTP, lifecycle, flash, ROM. Requests remain until acknowledgement;
errors enter the source-defined startup-failure path. Acknowledgements are controllable,
never permanent assumptions. ast_power_good_i and clock_ack_i model digital environment
responses. EDN ack returns one 32-bit word for an outstanding request; lack of ack is
backpressure and edn_error_i rejects that word. Lifecycle production mode disables
debug/DFT access. External wake/reset bits OR with selected source-listed internal
producers; reserved bits are ignored. All these conventions are digital exercise
interfaces, not a claim of the production SoC's exact packed interfaces.

For AON clock_leaf_o and clock_enable_o, bits 0..3 are IO_DIV4_PERI,
IO_DIV2_PERI, IO_PERI, USB_PERI, matching CLK_ENABLES field order. Their roots
are clk_io_i/4, clk_io_i/2, clk_io_i and clk_usb_i respectively. Bits 4..7 are
MAIN_AES, MAIN_HMAC, MAIN_KMAC, MAIN_OTBN from clk_main_i, in CLK_HINTS order.
clock_idle_i bits 0..3 are input idle indications for those four transactional
leaves; honor the source-defined idle qualification before disabling them.
clock_enable_o reports the effective enable and clock_leaf_o the actual gated
clock. clock_ack_i bits correspond to those same eight outputs and are input
acknowledgements from external consumers, allowing withheld/late responses in
the power sequence; never use an ack as the clock waveform or as CSR gate state.
The six selected managers retain an ungated clk_core_i control-plane clock and
their source-defined AON auxiliary clock; the eight gateable leaf consumers are
environment boundaries, not new AES/HMAC/KMAC/OTBN implementation tasks.
extclk_req_o and extclk_hi_speed_o expose the EXTCLK_CTRL request to the AST
boundary; extclk_ack_i is its independently delayed response. The AST BFM drives
the root input frequencies for normal/external-speed modes before acknowledging;
the DUT does not require an analog oscillator or technology clock mux.
jitter_req_o exposes JITTER_ENABLE digitally; characterize request/lock behavior,
not analog jitter spectra. Exercise source clock measurement/timeout on each
provided root and divided IO root with separately driven stop/frequency errors.

## Shared logical bus and integrity oracle (D-02 / CT-01)

The exercise bus preserves applicable TL-UL Get/Put handshakes, masks, ordering and
errors but uses an author-defined `bench_tl_v1` packed ABI. It is deliberately not the
upstream TL-UL binary ABI or SECDED code. The following declarations are normative,
ordered from most-significant field to least-significant field:

```systemverilog
typedef struct packed {
  logic [2:0] opcode, param;
  logic [1:0] size;
  logic [3:0] source;
  logic [31:0] address;
  logic [3:0] mask;
  logic [31:0] data;
  logic [3:0] instr_type;
  logic [6:0] cmd_intg, data_intg;
} bench_tl_req_t;
typedef struct packed {
  logic [2:0] opcode;
  logic [1:0] param, size;
  logic [3:0] source;
  logic sink;
  logic [31:0] data;
  logic error;
  logic [6:0] rsp_intg, data_intg;
} bench_tl_rsp_t;
```

Get=4, PutFullData=0, PutPartialData=1; request param=0. AccessAck=0,
AccessAckData=1; response param=0, sink=0, size/source echo the accepted request.
Sizes 0/1/2 mean 1/2/4 bytes, natural alignment; larger sizes and unknown opcodes
return error without register side effects. Get/PutFull masks select all addressed
byte lanes; PutPartial selects a nonempty subset. A read of a write-only field is
zero; a write to read-only/reserved fields has no effect except source-specified errors.
instr_type=4'b0101 is data and 4'b1010 is instruction; peripheral instruction access
and other encodings return error. On error read data is zero and response integrity
still encodes that response. No write or FIFO pop/push may occur for a rejected request.

CRC7 uses polynomial x^7+x^3+1, initial register 7'b0, no final XOR, MSB-first.
For each input bit b: feedback=crc[6] XOR b; shift crc left by one, discard bit 6,
append zero; if feedback is one XOR the seven-bit value 7'h09. cmd_intg covers
{opcode,param,size,source,address,mask,instr_type} (52 bits), data_intg covers data
(32 bits), rsp_intg covers {opcode,param,size,source,sink,error} (13 bits).
Zero input has CRC 0 and the 32-bit value 1 has CRC 7'h09. Test every single-bit
corruption in each protected word and independently calculated multi-bit examples;
do not claim SECDED properties or require detection of CRC collisions. This supplies
the previously missing interface/algorithm; no held-out RTL mining is needed.

At most four requests may be outstanding; source IDs must be distinct while outstanding.
Transfer occurs only on valid && ready. Producers hold payload stable while stalled.
Responses retire in acceptance order; response backpressure cannot drop/duplicate effects.
The wrapper must return a response within 4096 clk_core_i edges when the target domain is
running and response ready remains high. Clock-disabled targets return an error within
4096 running core edges, rather than waiting for a disabled clock. The bound allows
CSR synchronization with the slow AON domain; source-defined polling/status protocols
still apply and a bus acknowledgement must not pretend a slow operation has completed.
Reset cancels the current
bus epoch, clears accepted-but-unanswered requests and prohibits later stale responses.
The scoreboard records cancellation explicitly rather than counting it as a completed read.

Assign 64-KiB windows in profile.modules order starting at address zero. Low 16 bits
are the source-defined CSR/memory offsets; addresses outside these windows error. These
are exercise addresses, not the whole-EarlGrey memory map. Register reset/field behavior
comes from the pinned files with the named adjudications, not autogenerated answer RTL.

## Clock, reset, timing and physical abstraction

For serial I/O, clk_core_i and clk_usb_i are phase-aligned 48 MHz inputs, and clk_aon_i
is an unrelated 200 kHz input. SPI_DEVICE sck is asynchronous external stimulus up to
12 MHz. USB is full-speed digital signaling; software/USB RAM ports are synchronous
as Q-06 specifies. For AON, clk_core_i is ungated 24 MHz, nominally phase-aligned
with clk_io_i/4 before root-switch/fault tests; clk_io_i is 96 MHz, clk_main_i is unrelated 100 MHz, clk_usb_i is
unrelated 48 MHz and clk_aon_i is unrelated 200 kHz. The AST environment can
stop/change the three functional roots without stopping the core/AON control
clocks. Root frequency measurements use programmed thresholds for these declared
rates; the negative tests deliberately violate those thresholds or stop a root.
These are chosen simulation constraints, not achieved timing or frequency claims.
rst_ni asserts asynchronously; each domain releases through two destination-clock
stages. A stopped functional clock cannot suppress AON wake or reset assertion.
Use behavioral integrated clock-gating models with stable-low enable capture; make
every intentional clock crossing and reset crossing explicit to Jasper CDC/RDC.
Do not declare all crossings false-path to obtain a clean report.

SPI_HOST event IRQ is the enabled OR of current event predicates sampled on a core
edge, registered on the following core edge; enabling an already true predicate
asserts it on that following edge. Error IRQ follows the source's sticky W1C state.
For Q-03, exact first-stall cycle is not an acceptance criterion across valid chosen
pipelines: freeze its own capacity and initial state, then prove conservation, ordered
completion and ready/valid behavior at capacity-1/capacity/capacity+1. This replaces
the unfounded universal sixth/seventh-command latency bin, not a failed functional bin.
For Q-04, on one SPI-domain commit boundary hardware set wins over software clear of
the same status bit; all distinct bits update independently. SW requests cross through
a lossless acknowledged queue; committed status is atomically visible at byte boundaries.
Do not impose a fixed phase-independent SW-to-SPI latency. With both clocks running and
no destination backpressure, queue delivery takes at most eight destination edges.

## AON digital pad and strap convention (D-01 / Q-13)

This section applies only to the AON DUT. MIO order is IOA0..8, IOB0..12, IOC0..12,
IOR0..7, IOR10..13 (47 pads). IOR8/IOR9 are DIO0/1 in that order.
IOC8 is TAP0, IOC5 TAP1, IOR5 DFT0 and IOR7 DFT1.
Any optional generic third DFT strap is tied low in this exercise and sampled/tested
as that constant, not treated as a missing externally driven pin.

The digital pad supports inversion, virtual open-drain, pull enable/select, keeper,
Schmitt selection and input-disable. Native open-drain bit 6 is unsupported and reads
zero; virtual open-drain bit 1 is used. Slew values 0 and 1 are supported (writes 2/3
read 1); drive-strength values 0..3 are supported (writes 4..15 read 3). These rules
apply uniformly to the 47 MIO and 16 DIO pads. CSR fields map by name to attr_i as
N-01 says. All supported attribute reset values are zero except IOC8 pull_en resets
one; unsupported/reserved bits read zero. Input disable returns zero to peripherals,
but raw pad sampling used for wake/straps remains observable. Slew/drive values are
digital settings only: no analog delay/current behavior is part of this problem.
This is an explicit author-chosen digital pad capability table, not an inference
about OpenTitan production IO cells or an excuse to skip real mux/wake/retention bins.

## Independent UVM and Cadence deliverables

Derive TP from the source and these author contracts. Supply a bus agent, peripheral
BFMs, passive monitors, independent register/protocol scoreboards, covergroups and SVA;
use virtual sequences for concurrency, errors, reset interruption, clock stop/restart
and cross-module paths. Drivers may not increment pass/coverage counters on their own.
Use a pure SV UVM implementation supplied with the selected Xcelium installation;
no custom DPI/PLI, Python test runner or upstream VIP is required. A C/ELF program is
not required: exercise CSR transactions directly in UVM.

Deliver complete ordered RTL and TB filelists, include directories/defines/top/parameters,
SDC plus Jasper clock/reset/CDC intent, bounded fairness assumptions, named properties,
checker activity counters and a frozen coverage-bin model. Every macro, package, RAM
initialization file and include must be inside the candidate closure.
No network fetch or code generation may occur during EDA execution. Retain Xcelium logs and native
coverage databases, IMC reports, Jasper lint/CDC/FPV reports and Genus elaboration/
synthesis reports. Identify every tool version and run's input hashes, seed and timeout.
Read tool-specific warnings and failed/aborted assertions; process exit code alone is
not acceptance. A waveform, event heartbeat or generated file is not correctness.

Use `dv_generate_uvm(profile_path='eda/verification_profile.json')` for Cadence-oriented
generation. `eda/cadence_runner.py inspect`, `plan` and `run` consume the candidate's
reviewed `eda/project.json`; `project.example.json` is only a non-runnable template.
The stage command supplies that common kit. Existing LoopX native Vivado build/check
and PicoRV32 closure paths are not the Cadence evaluation path. External EDA events
remain observations until their actual evidence is independently checked.

Freeze all applicable functional bins and exclusions before closure iterations. Require
at least 90% functional-bin coverage and zero checker/assertion failures, with code,
toggle and assertion coverage reported separately. Cross-check an independently authored
SV oracle against the declared examples and error mutations before relying on it.
Historical upstream comparisons are optional supporting evidence; differences from
author conventions are recorded, not silently treated as candidate bugs. No Cadence
elaboration, simulation, CDC, proof or coverage result is supplied by this question pack.
