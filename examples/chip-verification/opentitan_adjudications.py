"""Rebuild authored OpenTitan exercise corrections without changing upstream sources.

The decisions below are benchmark contracts, not claims of upstream bug fixes or
simulation results. Their targets are the pinned input documents, never the
held-out implementation. Run this module after changing a decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REVISION = "opentitan-adjudications-cadence-v2"
COMMIT = "c69eaea24a57c581ed274779887cb4a0c1405e3b"
CASES = ("opentitan-serial-io", "opentitan-aon-control")
CADENCE_TOOLCHAIN = {
    "family": "cadence", "simulation": "xcelium", "coverage": "imc",
    "lint": "jasper_superlint", "cdc": "jasper_cdc", "formal": "jasper_fpv",
    "synthesis": "genus", "execution": "external", "native_build_required": False,
}
CADENCE_RULE = (
    "Implement the selected subsystem as self-contained synthesizable SystemVerilog with an independent "
    "SystemVerilog UVM/SVA environment. Cadence Xcelium/IMC and Jasper/Genus are the execution targets. "
    "Upstream RTL, build descriptions and tests are historical comparison material, not required build "
    "inputs or an independent golden for this authored contract."
)


def cadence_contract(case_id, profile):
    serial = case_id == CASES[0]
    ports = (
        "clk_core_i, clk_usb_i, clk_aon_i, rst_ni; spi_dev_sck_i, spi_dev_csb_i, "
        "spi_dev_sd_i[3:0], spi_dev_sd_o[3:0], spi_dev_sd_oe_o[3:0]; "
        "spi_host_sck_o, spi_host_csb_o[1:0], spi_host_sd_i[3:0], spi_host_sd_o[3:0], "
        "spi_host_sd_oe_o[3:0]; usb_dp_i, usb_dn_i, usb_vbus_i, usb_dp_o, usb_dn_o, "
        "usb_d_o, usb_se0_o, usb_tx_oe_o, usb_dppullup_o, usb_dnpullup_o; "
        "usb_wake_req_o, usb_wake_ack_i, usb_functional_clock_enabled_i; "
        "irq_spi_host_o[31:0], irq_spi_device_o[31:0], irq_usbdev_o[31:0], alert_o[2:0]"
        if serial else
        "clk_core_i, clk_aon_i, clk_main_i, clk_io_i, clk_usb_i, rst_ni; "
        "sleep_req_i, wakeup_i[5:0], reset_req_i[3:0], "
        "ndm_reset_i, lifecycle_prod_i, ast_power_good_i; boot_req_o[3:0], boot_ack_i[3:0], "
        "boot_error_i[3:0], edn_req_o, edn_ack_i, edn_data_i[31:0], edn_error_i; "
        "mio_in_i[46:0], mio_out_o[46:0], mio_oe_o[46:0], "
        "dio_in_i[15:0], dio_out_o[15:0], dio_oe_o[15:0], dio_peripheral_data_i[15:0], "
        "dio_peripheral_oe_i[15:0], dio_peripheral_sample_o[15:0]; peripheral_in_i[74:0], "
        "peripheral_out_o[56:0], peripheral_oe_i[74:0]; alert_p_i[64:0], alert_n_i[64:0], "
        "alert_ack_p_o[64:0], alert_ack_n_o[64:0], esc_p_o[3:0], esc_n_o[3:0], "
        "esc_resp_p_i[3:0], esc_resp_n_i[3:0]; pwr_enable_o, isolation_o, "
        "clock_enable_o[7:0], clock_ack_i[7:0], clock_leaf_o[7:0], clock_idle_i[3:0], "
        "extclk_req_o, extclk_hi_speed_o, extclk_ack_i, jitter_req_o, "
        "rst_lc_no, rst_sys_no, rst_module_no[5:0], "
        "irq_sysrst_o, irq_alert_o[3:0], wakeup_record_o[5:0]"
    )
    return f"""# Cadence standalone execution contract: {case_id}

Authority: benchmark author, revision {REVISION}. This is a new digital subsystem
implementation exercise derived from the supplied sources. It is not a binary-compatible
OpenTitan IP port, a production pad-ring implementation, or an upstream regression replay.

Effective rule: {CADENCE_RULE}

## Implementation and source boundary

The DUT top is `{profile['root_module']}`; all modules listed in integration_profile.json
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

Additional ports: {ports}.

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
typedef struct packed {{
  logic [2:0] opcode, param;
  logic [1:0] size;
  logic [3:0] source;
  logic [31:0] address;
  logic [3:0] mask;
  logic [31:0] data;
  logic [3:0] instr_type;
  logic [6:0] cmd_intg, data_intg;
}} bench_tl_req_t;
typedef struct packed {{
  logic [2:0] opcode;
  logic [1:0] param, size;
  logic [3:0] source;
  logic sink;
  logic [31:0] data;
  logic error;
  logic [6:0] rsp_intg, data_intg;
}} bench_tl_rsp_t;
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
{{opcode,param,size,source,address,mask,instr_type}} (52 bits), data_intg covers data
(32 bits), rsp_intg covers {{opcode,param,size,source,sink,error}} (13 bits).
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
"""
PATHS = {
    "SH-P": "hw/ip/spi_host/doc/programmers_guide.md",
    "SH-H": "hw/ip/spi_host/data/spi_host.hjson",
    "SH-T": "hw/ip/spi_host/doc/theory_of_operation.md",
    "SD-H": "hw/ip/spi_device/data/spi_device.hjson",
    "SD-T": "hw/ip/spi_device/doc/theory_of_operation.md",
    "SD-R": "hw/ip/spi_device/doc/registers.md",
    "UD-P": "hw/ip/usbdev/doc/programmers_guide.md",
    "UD-H": "hw/ip/usbdev/data/usbdev.hjson",
    "UD-T": "hw/ip/usbdev/doc/theory_of_operation.md",
    "TL": "hw/ip/tlul/README.md",
    "TOP-D": "hw/top_earlgrey/doc/design/README.md",
    "TOP-H": "hw/top_earlgrey/data/top_earlgrey.hjson",
    "PW-T": "hw/top_earlgrey/ip_autogen/pwrmgr/doc/theory_of_operation.md",
    "PW-H": "hw/top_earlgrey/ip_autogen/pwrmgr/data/pwrmgr.hjson",
    "RM-T": "hw/top_earlgrey/ip_autogen/rstmgr/doc/theory_of_operation.md",
    "SC-T": "hw/ip/sysrst_ctrl/doc/theory_of_operation.md",
    "SC-H": "hw/ip/sysrst_ctrl/data/sysrst_ctrl.hjson",
    "SC-R": "hw/ip/sysrst_ctrl/doc/registers.md",
    "PM-P": "hw/ip/pinmux/doc/programmers_guide.md",
    "PM-T": "hw/ip/pinmux/doc/theory_of_operation.md",
    "PM-H": "hw/top_earlgrey/ip/pinmux/data/autogen/pinmux.hjson",
    "AH-P": "hw/top_earlgrey/ip_autogen/alert_handler/doc/programmers_guide.md",
    "AH-T": "hw/top_earlgrey/ip_autogen/alert_handler/doc/theory_of_operation.md",
    "AH-H": "hw/top_earlgrey/ip_autogen/alert_handler/data/alert_handler.hjson",
}


def decision(issue_id, module, classification, spans, rule, rejected, checks, gate=None):
    return {
        "issue_id": issue_id, "module": module, "classification": classification,
        "spans": spans, "rule": rule, "rejected": rejected, "checks": checks,
        "local_gate": gate,
        "resolution_state": "local_obligation_open" if gate else "contract_defined",
    }


# Inclusive, one-based spans in the unchanged source files. They locate both
# obsolete statements and the evidence supporting the exercise decision.
DECISIONS = [
    decision("SH-01", "spi_host", "corrected", [("SH-P",141,146),("SH-H",602,610)],
        "IDLE uses STATUS.ACTIVE becoming low; READY uses STATUS.READY becoming high. They are distinct event predicates and EVENT_ENABLE fields.",
        "Enabling IDLE is not an alias for enabling READY.",
        "Exercise a command-ready but active transfer, then idle; predict the two predicates separately. Temporal IRQ qualification is a separate contract."),
    decision("SH-02", "spi_host", "exercise_convention", [("SH-P",152,154),("SH-H",204,208),("SH-H",280,282),("SH-H",622,626)],
        "For this exercise RXWM is true exactly when rx_depth_words > RX_WATERMARK. Use this strict comparison for STATUS.RXWM and the RXWM event predicate. The author selects the explicit greater-than event definition to adjudicate the conflicting reaches wording inside HJSON; this is not blanket HJSON precedence.",
        "Equality does not satisfy RXWM; do not use >= or compare a byte count to a word threshold.",
        "Check threshold-1, threshold and threshold+1 in FIFO words, including threshold zero and masking. Freeze that oracle before running the DUT."),
    decision("SH-03", "spi_host", "corrected", [("SH-T",236,238),("SH-T",277,282),("SH-H",378,381)],
        "During unstalled clock generation T_SCK = 2*(CLKDIV+1)*T_core and T_half_SCK = (CLKDIV+1)*T_core. Interpret waveform time units using this half-period.",
        "Reject the inverse-period expression and the extra factor of one half in the waveform foot.",
        "Measure complete and half periods for CLKDIV=0,1 and a larger value; distinguish intentional stalls from divider timing."),
    decision("SH-04", "spi_host", "corrected", [("SH-T",854,884)],
        "A running divider timeslice spans CLKDIV+1 core cycles: T_timeslice=(CLKDIV+1)*T_core. FSM updates remain subject to the documented enable/stall conditions.",
        "Do not divide the core period by CLKDIV+1 or advance a frozen FSM merely because time elapsed.",
        "Check divider reload/countdown and frozen state under a stall; relate unstalled slices to SH-03 half periods."),
    decision("SH-05", "spi_host", "corrected", [("SH-T",127,132),("SH-H",439,450)],
        "COMMAND.LEN is bits [8:0], range 0..511. A data segment transfers LEN+1 bytes; a dummy segment uses LEN+1 dummy cycles as defined by the field.",
        "Reject an 8-bit packer, a 255 maximum, and treating dummy cycles as data bytes.",
        "Cover encoded LEN=0,255,256,511, pack all nine bits and check exactly 1..512 units for the applicable direction."),
    decision("SH-06", "spi_host", "corrected", [("SH-T",402,404),("SH-T",821,824)],
        "Quad transfer emits the upper nibble [7:4], followed by [3:0], with the source-defined SD lane ordering.",
        "The phrase bits 7 through 3 is an erroneous five-bit range, not a fifth lane.",
        "Use bytes whose nibbles and all bit positions differ; reconstruct exactly eight bits per byte."),
    decision("SH-07", "spi_host", "corrected", [("SH-T",821,824),("SH-T",1079,1098),("SH-T",1116,1123)],
        "Dual output consumes [7:6], [5:4], [3:2], [1:0] in order for each byte. Each shift consumes exactly two bits.",
        "Reject waveform labels [6:5] and [4:3], which overlap or omit bits.",
        "Reconstruct asymmetric walking-one byte patterns in dual mode and verify no bit duplication or omission."),
    decision("SH-08", "spi_host", "corrected", [("SH-T",569,573),("SH-T",833,835)],
        "TXSTALL/RXSTALL are diagnostic states and do not directly create an event IRQ. Independently evaluate the declared FIFO and other event-enable predicates.",
        "Do not invent a stall-event interrupt field from the informal frequency-monitoring paragraph.",
        "Induce TX and RX stalls with event enables cleared and enabled individually; separate associated FIFO events from the diagnostic flags."),
    decision("SD-01", "spi_device", "exercise_convention", [("SD-H",421,424),("SD-H",1103,1108),("SD-T",310,326)],
        "The author adopts the coherent four-byte PAYLOAD_SWAP_MASK/PAYLOAD_SWAP_DATA contract: payload_swap_en enables masked substitution in the first four eligible payload bytes. Unselected bits and later bytes pass through unchanged; shorter payloads only transform bytes actually present. Preserve the documented byte ordering.",
        "Reject first-byte-only substitution and the obsolete DATA_SWAP CSR name; they conflict with the detailed four-byte algorithm.",
        "Check payload lengths 0,1,3,4,5, distinct mask/data bytes, all-zero and all-one masks, and disabled swap."),
    decision("SD-02", "spi_device", "corrected", [("SD-H",621,640),("SD-H",1020,1048)],
        "CMD_INFO.addr_mode refers to ADDR_MODE.addr_4b_en for the configured address-width choice. Bind the actual declared enum values and ADDR_MODE CSR.",
        "CFG.addr_4b_en is an obsolete path, not a second control register.",
        "Cover no-address, configured address and explicitly selected address-width modes using the declared encoding."),
    decision("SD-03", "spi_device", "corrected", [("SD-H",1111,1152)],
        "The declared field is CMD_INFO.read_pipeline_mode[23:22]. Preserve its multi-value enumeration and per-mode timing contract.",
        "payload_pipeline_en in prose/comments does not declare a single-bit enable or an extra CSR.",
        "Generate the register model from the two-bit field; exercise every declared mode and handle reserved values explicitly."),
    decision("SD-04", "spi_device", "corrected", [("SD-H",1020,1048),("SD-H",1153,1162),("SD-R",1123,1127)],
        "Upload address presence and width follow the current CMD_INFO.addr_mode enum and ADDR_MODE.addr_4b_en where that enum selects the global width. Upload only the address bytes present in the decoded command.",
        "Do not create addr_en, addr_4B_affected or addr_4b_forced registers from obsolete upload prose.",
        "For each address mode, independently compare uploaded opcode, address and payload boundaries, including address-free commands."),
    decision("SD-05", "spi_device", "corrected", [("SD-R",1650,1662),("SD-H",147,252)],
        "Ingress is 112 32-bit words (448 bytes): payload offset/depth 0/64, command 64/16, address 80/16, TPM-write 96/16, all in words. Egress is 848 words: read buffer 0/512, mailbox 512/256, SFDP 768/64, TPM-read 832/16. Banks have independent address spaces.",
        "Reject the 640-byte ingress sum, 32-byte command/address regions and placing SFDP in ingress.",
        "Convert words to bytes explicitly; check every first/last address, no intra-bank overlap and correct bank routing."),
    decision("AC-01", "rstmgr", "exercise_convention", [("RM-T",230,237),("RM-T",276,279)],
        "For this exercise the NDM reset summary table governs: outside PROD/PROD_END, assert LC and module trees and retain SYS; in PROD/PROD_END assert LC, SYS and module trees. NDM does not assert POR. This author choice resolves two conflicting prose accounts, not an observed RTL bug.",
        "Reject the later unconditional only-rst_sys_n account as the exercise reset-tree oracle.",
        "Check each tree separately under both lifecycle groups, including debug retention and reset-cause recording."),
    decision("AC-02", "sysrst_ctrl", "corrected", [("SC-T",49,50),("SC-T",95,96),("SC-T",119,120),("SC-R",59,67),("SC-H",279,294),("SC-H",970,1008)],
        "INTR_STATE.event_detected is read-only status. Clear the relevant source latch with its W1C operation: COMBO_INTR_STATUS, KEY_INTR_STATUS or ULP_STATUS as applicable. Apply the source-aggregation scope in Q-08; clearing one source does not clear another asserted source.",
        "Writing INTR_STATE is not a source-clear operation and must not change its declared read-only access.",
        "Set two independent sources, clear them separately, try an INTR_STATE write and verify remaining status/IRQ and enable behavior."),
    decision("AC-03", "sysrst_ctrl", "corrected", [("SC-T",39,42),("SC-H",797,799),("SC-H",837,840)],
        "Program combo precondition duration through COM_PRE_DET_CTL for the selected combo. Keep selection and duration fields distinct.",
        "COM_DET_SEL_CTL_0 in the duration example is not the declared timer register.",
        "Program distinct selection/duration values and check the resulting precondition timing against the configured timer width/clock."),
    decision("AC-04", "pinmux", "corrected", [("PM-P",13,25),("PM-H",606,627),("PM-H",709,722)],
        "Use the generated CSR layout: input_disable bit 7, slew_rate [17:16], drive_strength [23:20]. Map these named fields explicitly into the pad attribute interface; N-01 describes a different packed layout.",
        "Reject the guide's CSR slew [8:7]/drive [12:9] layout and do not use packed attr_i positions as CSR positions.",
        "Write distinguishable fields and check named-field mapping, reserved bits and readback under the declared pad capability profile (D-01)."),
    decision("AH-01", "alert_handler", "corrected", [("AH-T",369,372),("AH-T",462,466),("AH-H",903,907),("AH-P",113,116)],
        "Escalation due to accumulation occurs on the next alert after the threshold is reached: threshold 0 triggers on the first eligible alert; threshold 1 on the second; threshold 15 on the sixteenth, subject to class enable and escalation configuration.",
        "Fast-track threshold 1 does not mean escalate on the first alert.",
        "For thresholds 0,1,15 check the preceding count, exact trigger and later events; separate accumulation from IRQ timeout causes."),
    decision("AH-02", "alert_handler", "corrected", [("AH-P",55,57),("AH-H",759,780)],
        "Class configuration is guarded by the separate CLASSx_REGWEN CSR bound by CLASSx_CTRL_SHADOWED.regwen, for x=A..D.",
        "CLASSA_CTRL_SHADOWED.REGWEN is not a declared subfield.",
        "Lock each class via its declared guard, attempt a shadowed configuration update and verify lock persistence and independent classes."),
    decision("AH-03", "alert_handler", "corrected", [("AH-H",842,875),("AH-H",1026,1028),("AH-H",1174,1178),("AH-H",1477,1481),("AH-H",1780,1784),("AH-P",85,88)],
        "For each class A..D the escalation abort/clear write goes through CLASSx_CLR_SHADOWED and is guarded by CLASSx_CLR_REGWEN. CLASSx_REGWEN guards configuration, not this clear path. CTRL_SHADOWED.LOCK affects the separately documented clear lock behavior.",
        "Reject ESC_CNT prose that substitutes CLASSx_REGWEN for CLASSx_CLR_REGWEN. Do not merge the two guards.",
        "Cross configuration-lock and clear-lock states; test accepted and rejected clears in every class and confirm ESC_CNT remains read-only."),
    decision("AH-04", "alert_handler", "corrected", [("AH-H",503,518)],
        "PING_TIMER_REGWEN is rw0c with reset value 1. A write of zero clears the enable; writing one preserves its value. Once cleared, guarded ping configuration writes cannot restore it until reset.",
        "The write-one-to-clear sentence conflicts with the explicit access declaration and is superseded here.",
        "Check reset=1, write1 retention, write0 lock, failed re-enable and rejected guarded writes while locked."),
    decision("AH-05", "alert_handler", "corrected", [("AH-T",315,319),("AH-T",383,390),("AH-T",750,751)],
        "Ping response timeout uses PING_TIMEOUT_CYC_SHADOWED. CLASSx_TIMEOUT_CYC_SHADOWED controls the separate class interrupt timeout escalation path.",
        "The low-power ping paragraph's CLASSA link does not alias the ping timer to the class timer.",
        "Set visibly different ping/class timeouts and independently stimulate missed ping and unserviced class IRQ."),
    decision("CT-01", "tlul", "scoped_gap", [("TL",88,111),("TL",148,150),("TL",168,172),("TL",190,196)],
        "Use the current logical sidebands a_user={instr_type,cmd_intg,data_intg} and d_user={rsp_intg,data_intg}. The old reserved/parity typedef and TL_DUW=4 example are not a fixed-version ABI. D-02 owns the remaining public type/encoding contract; logical request/response planning can continue.",
        "Do not synthesize a supposedly compatible packed ABI from the historical example or infer it from held-out RTL.",
        "Check field identity in the shared interface contract; require D-02 evidence before byte/bit-level integrity acceptance.",
        "Only TL-UL packed interface binding and integrity-oracle acceptance await the D-02 normative type/encoding addendum."),
    decision("CT-02", "context", "background_scope", [("TOP-D",207,210),("TOP-H",181,226)],
        "The selected top configuration instantiates uart0..uart3 (four UARTs). UART is whole-chip background and is outside both reduced subsystem DUTs.",
        "One-UART prose neither overrides the configuration nor creates a new UART implementation task.",
        "Check scope inventory; do not add UART RTL, UART coverage bins or a global execution blocker."),
    decision("CT-03", "context", "scope_interpretation", [("TOP-D",216,217),("TOP-H",1235,1287)],
        "The base pinout has 47 muxed pads: IOA=9, IOB=13, IOC=13, IOR=12; direct IOR8/IOR9 are excluded. A 32-bit GPIO vector is not a count of all MIO pads. This constrains AON PINMUX geometry; it is external context in serial-I/O.",
        "Do not reduce the MIO geometry to 32 or count the two direct pads as muxed.",
        "For in-scope PINMUX check enumeration, index bounds and direct/muxed classification; otherwise retain a boundary fact only."),
    decision("CT-04", "context", "background_scope", [("TOP-D",172,185)],
        "The local ROM-key table enumerates seven slots, 0..6. Treat the earlier three-slot count as obsolete background; ROM/key provisioning is outside these exercises.",
        "Do not expand either subsystem into a ROM implementation or block unrelated design/DV tasks over this background inconsistency.",
        "Retain the table/count correction in provenance; create no ROM DUT bins or global gate."),
    decision("CT-05", "context", "scope_interpretation", [("TOP-D",223,231),("SD-H",5,11),("SD-R",197,202)],
        "In serial-I/O use the selected SPI_DEVICE CONTROL modes disabled/flashmode/passthrough with TPM controlled separately. Do not add a legacy Firmware mode without a declared interface. In AON this SPI paragraph is out-of-scope background only.",
        "A whole-chip overview is not authority to add an undeclared Firmware mode or a new SPI DUT to AON.",
        "Check selected mode enumeration for serial-I/O; for AON check only that the scope remains the six control IPs."),
    decision("Q-01", "spi_host", "exercise_convention", [("SH-H",330,350)],
        "Author convention: CPHA determines the output launch edge (0 trailing, 1 leading); FULLCYC modifies receive sampling latency only, from half a SCK cycle to one full cycle after the corresponding launch. Preserve CPOL edge polarity. This chooses the field's sampling-only description; it is not a proven upstream implementation property.",
        "Do not silently let FULLCYC change the CPHA=1 launch edge solely because of the trailing-edge example.",
        "Cross CPOL/CPHA/FULLCYC and divider values using an independent edge schedule; verify transmit and receive timing separately."),
    decision("Q-02", "spi_host", "exercise_convention", [("SH-H",57,60),("SH-H",513,515)],
        "Chip selects are zero-indexed. For this exercise valid CSID satisfies 0 <= CSID < NumCS; CSID=NumCS is invalid and follows the declared CSIDINVAL error handling.",
        "The word exceeds must not make equality a valid out-of-range chip select.",
        "Test NumCS-1, NumCS and the largest encodable value; handle NumCS=1 and avoid creating an extra chip-select pin."),
    decision("Q-03", "spi_host", "scoped_gap", [("SH-T",844,847),("SH-T",1302,1323)],
        "Require conservation/order of accepted one-byte segments and correct READY/backpressure behavior. Six-versus-seven pipeline examples do not define a universal first-stall cycle: record initial queue/FSM/divider conditions and accepted commands when evaluating that observation.",
        "Do not assert that the sixth or seventh segment always stalls regardless of initial state or convert that timing ambiguity into a module-wide blocker.",
        "Exercise bursts around six/seven segments; check no data loss and captured stalls. Keep an exact-first-stall timing claim pending a reviewed pipeline timing contract.",
        "Only a cycle-exact first-stall latency bin requires a versioned initial-state/acceptance/pipeline-depth contract."),
    decision("Q-04", "spi_device", "exercise_convention", [("SD-R",350,354),("SD-T",74,105)],
        "For this exercise use the detailed staged/committed contract: SW requests cross via the asynchronous queue; serial Read Status emits atomic bytes from the latest committed state and can observe BUSY changes during one transaction at eight-SPI-clock commit boundaries. Passthrough gating changes only at CSb release using committed BUSY. Do not invent a fixed SW-to-SPI CDC latency.",
        "The older transaction-start snapshot prose does not freeze all status bytes until transaction end.",
        "Poll multiple status bytes while issuing a non-racing SW clear; check byte atomicity and delayed passthrough reopening. Keep simultaneous HW/SW same-bit priority separate.",
        "Only simultaneous HW/SW updates to the same status bit await an explicit arbitration rule; serialized updates and byte-boundary polling are specified."),
    decision("Q-05", "usbdev", "scope_interpretation", [("UD-P",36,38),("UD-P",82,86),("UD-H",719,735)],
        "Apply the control-endpoint post-SETUP special case separately from ordinary OUT flow control: after SETUP clears protocol stall, the guide's set_nak_out/rxenable_out conditions hold subsequent IN/OUT requests in NAK until software prepares the control transfer. Do not extend that special condition to every endpoint/transaction.",
        "The rxenable_out name alone does not disprove the explicitly stated control-transfer IN behavior.",
        "Separate ordinary OUT backpressure from post-SETUP control IN/OUT tests; check recovery after software configures the new request."),
    decision("Q-06", "usbdev", "scope_interpretation", [("UD-T",38,42),("UD-T",157,159)],
        "Model synchronous accesses at each SRAM port. For this exercise the functional bus/USB clocks share the documented 48 MHz input; preserve their port/domain identities and the separate wake clock. Clock gating and wake handoff still require explicit CDC/reset analysis. The phrase asynchronous dual-port does not require asynchronous read data or unrelated functional frequencies.",
        "Do not declare RAM impossible, merge the wake domain into the functional clock, or infer safety solely from equal nominal frequency.",
        "Specify port edges, enables and reset/gating constraints in the shared contract; test buffer ownership and wake transfer when the functional clock stops."),
    decision("Q-07", "sysrst_ctrl", "exercise_convention", [("SC-T",7,9),("SC-T",80,84)],
        "For auto-block debounce the explicit two-sample description governs: qualify the start and end samples; an intermediate pulse that is not sampled at either endpoint is not guaranteed to cancel detection. This rule is confined to auto-block, not every SYSRST timer.",
        "Stays low is not a requirement for continuous glitch monitoring in this two-sample auto-block contract.",
        "Check start/end levels, just-before-expiry changes and an intermediate returned pulse; distinguish sampling guarantees from analog pulse capture."),
    decision("Q-08", "sysrst_ctrl", "exercise_convention", [("SC-T",119,122),("SC-H",31,35),("SC-H",279,294),("SC-R",64,67)],
        "Author convention for the status IRQ: include a latched ULP_STATUS.ulp_wakeup alongside enabled combo/key source status in event_detected; INTR_ENABLE gates the output IRQ. Clear ULP at its declared W1C ULP_STATUS source, not at read-only INTR_STATE. The abbreviated combo/keyboard summary is not a prohibition of ULP.",
        "Do not call the omission a proven hardware conflict or delete ULP interrupt behavior without a replacement contract.",
        "Trigger ULP alone and concurrently with a key/combo source; mask/unmask, clear each source and check source-OR retention. This authored merge rule must be evaluated as such."),
    decision("Q-09", "pwrmgr", "exercise_convention", [("PW-T",228,237),("PW-H",796,803)],
        "For wake recording, interpret leaving Active in the context of a valid low-power-entry path. A reset-only transition out of Active does not open a new wake-recording window. Apply the documented recording stop/clear controls within the valid low-power path.",
        "Do not automatically record wake reasons on every Active exit, including unrelated reset paths.",
        "Contrast valid low-power entry, entry abort/fall-through and reset-only exit; inject wake before/inside/after the defined recording window."),
    decision("Q-10", "rstmgr", "scoped_gap", [("RM-T",62,76)],
        "Treat the LC/SYS chains as reset classes, not proof that every leaf has multiple drivers. The named peripheral module-reset class derives from LC. For each additional consumer, retain one explicit reset parent and domain in the shared integration map, supported by the selected configuration.",
        "Do not wire every leaf to both LC and SYS or assume every SYS consumer is an LC-reset peripheral.",
        "Review one parent per leaf and propagation through the relevant chain; run reset tests for mapped leaves. Record unknown consumers individually.",
        "Only an unmapped leaf's reset binding/propagation acceptance awaits its documented parent; already mapped reset paths can proceed."),
    decision("Q-11", "alert_handler", "not_a_conflict", [("AH-T",412,413),("AH-P",85,88),("AH-H",862,875)],
        "Clearing the escalation counter describes an effect of the CLASSx_CLR_SHADOWED operation, subject to CLASSx_CLR_REGWEN. ESC_CNT remains read-only. The link to the counter describes the observed state, not a writable entry point.",
        "Do not change ESC_CNT to RW or register this wording alone as an unresolved contradiction.",
        "Clear through the shadowed clear interface and observe the counter/escalation effect; verify a write to the RO counter cannot substitute for clear."),
    decision("Q-12", "alert_handler", "exercise_convention", [("AH-H",478,497)],
        "Author-chosen exercise reset contract: class A..D interrupt state resets to zero, sets on its qualified alert and clears through the declared interrupt W1C action. The interrupt-list description has no explicit reset field; this addendum supplies the exercise value rather than claiming an inferred generator/RTL result.",
        "Do not derive a reset value of one solely from Defaults true in the description, or claim the pinned RTL was inspected to resolve it.",
        "Check all four state bits immediately after reset, first qualifying alerts and independent clear operations; record this as an authored reset convention."),
    decision("Q-13", "pinmux", "scope_interpretation", [("PM-T",110,114),("TOP-H",1441,1446)],
        "Use the selected top special-signal mapping with exposed dft0=IOR5 and dft1=IOR7, plus tap0=IOC8 and tap1=IOC5. Three generic DFT straps do not imply a third external pad in this selected profile. Any extra internal generic strap remains an explicit wrapper boundary with a separately declared binding.",
        "Do not invent an unnamed pad or call differing generic/selected counts an established impossibility.",
        "Check selected named straps and capture conditions; prevent an unbound internal generic port from being silently tied off or counted as verified.",
        "If the chosen generic IP exposes an additional strap port, only that port's binding awaits an explicit configuration decision."),
    decision("D-01", "pinmux", "configuration_gap", [("PM-H",551,556),("PM-H",622,627),("TOP-H",1443,1444)],
        "Per-pad attribute WARL support is a technology/profile dependency, not a contradictory spec. TAP strap0 is identified as IOC8 in the selected mapping; preserve its documented pull-enable reset exception. Parameterize the capability/readback mapping and record the chosen pad profile before claiming exact physical behavior.",
        "Do not report TAP0 identity as missing, invent all-supported masks or reinterpret a technology gap as an unavailable simulator.",
        "Check named-field plumbing and configured masks separately. Exact per-pad WARL/reset acceptance must cite a public or authored, reviewed pad capability table.",
        "Only precise technology-dependent pad masks/readback/reset qualification remains pending; mux routing and declared logical attributes can proceed."),
    decision("D-02", "tlul", "definition_gap", [("TL",88,111),("TL",148,196)],
        "The current input does not supply a complete normative packed TL-UL type and integrity-code algorithm. Before compatible interface/encoding acceptance, supply a pinned public interface package or explicit reviewed specification addendum covering widths, ordering, encoding and error policy. Keep the logical protocol requirements; CT-01 rejects obsolete ABI examples.",
        "Do not fabricate bit widths/parity, mine the answer RTL as implicit specification, equate this gap with an EDA installation failure, or call it a whole-project error.",
        "Validate the supplied type/encoding contract on both RTL and UVM sides with known vectors and invalid-code tests before counting integrity bins.",
        "TL-UL ABI and integrity encoding/fault bins remain unverified until a normative interface/algorithm addendum is accepted."),
    decision("N-01", "pinmux", "not_a_conflict", [("PM-T",195,215),("PM-H",606,619)],
        "CSR slew [17:16]/drive [23:20] and packed attr_i slew [9:8]/drive [13:10] are different interfaces. Translate by field name; differing packed positions are valid. AC-04 resolves the actual stale CSR layout.",
        "Do not demand identical packed positions, overwrite the CSR layout with attr_i bits or create a global ambiguity record.",
        "Use different field values and verify explicit CSR-to-attribute mapping in both directions supported by the interface."),
    decision("N-02", "usbdev", "not_a_conflict", [("UD-H",899,917),("UD-T",157,180)],
        "Partition USB transfer expectations by subtype. Apply the declared OUT_ISO behavior to isochronous endpoints and ordinary ACK/NAK rules to the ordinary transaction types for which they are specified.",
        "A protocol subtype exception is not a contradiction; do not impose ordinary retry/handshake expectations on isochronous traffic.",
        "Cross endpoint mode with available-buffer/error conditions and select an independent subtype-aware response oracle."),
    decision("N-03", "usbdev", "naming_correction", [("UD-T",56,58),("UD-H",1144,1149)],
        "Program the declared PHY_CONFIG.tx_use_d_se0 field. The theory name use_tx_d_se0 is an alias in prose for the same transmit encoding selection, not a second field.",
        "Do not generate a nonexistent register bit or block USB work over this name mismatch.",
        "Check both DP/DN and D/SE0 output encodings with the actual field and the corresponding pin mapping."),
    decision("N-04", "alert_handler", "not_a_conflict", [("AH-T",1,65),("AH-H",43,177)],
        "The selected HJSON provides NAlerts=65, NLpg=24 and the LpgMap/AsyncOn defaults. Parse those provided parameter values for the profile; the early see-RTL table does not leave them missing. Do not copy a reference implementation to populate them.",
        "Do not retain an unresolved missing-LpgMap/AsyncOn issue after reading the supplied configuration, and do not confuse a mapping array with absent specification.",
        "Validate parameter dimensions/ranges from the complete HJSON arrays and ensure RTL and DV use the same selected values."),
]

# The standalone revision supplies previously missing execution-level decisions.
# Original evidence spans remain unchanged; these are author choices, not upstream fixes.
_CADENCE_DECISIONS = {
    "CT-01": ("Use the logical instr_type/cmd_intg/data_intg and rsp_intg/data_intg sidebands with the explicit bench_tl_v1 packed ABI in cadence_contract.md. Historical reserved/parity typedefs do not define this exercise's ABI.",
              "Check field ordering, width and CRC7 oracle against cadence_contract.md and D-02 on both RTL and UVM sides."),
    "D-02": ("cadence_contract.md defines bench_tl_v1 field widths/order, CRC7 polynomial, bit iteration, protected words, known vectors, masks and error policy. This logical bus exercise does not claim binary or SECDED compatibility with upstream TL-UL.",
             "Independently calculate known vectors, single-bit protected-word faults and invalid requests; check rejection has no CSR or FIFO side effects."),
    "D-01": ("Use the explicit digital pad capability/WARL/reset table in cadence_contract.md, including IOC8 pull-enable reset=1. This authored profile exercises mux, attributes, retention and wake without production IO technology assets.",
             "Check all 47 MIO and 16 DIO named-field mappings, unsupported native-OD zero readback, slew/drive clipping and IOC8 reset exception; do not claim analog signoff."),
    "Q-03": ("Require accepted command conservation, ordered completion and correct READY/backpressure. Freeze candidate capacity/initial state; a universal sixth/seventh-command first-stall cycle is outside this exercise's acceptance model.",
             "Exercise capacity-1/capacity/capacity+1 bursts and six/seven-command observations with captured initial conditions; prove no drop/duplicate and eventual ordered completion under declared fairness."),
    "Q-04": ("Use staged SW-to-SPI delivery and atomic committed status at eight-SPI-clock byte boundaries. Hardware set wins over SW clear of the same status bit at one commit; distinct bits update independently. Passthrough gating changes at CSb release. The running-domain delivery bound is in cadence_contract.md.",
             "Race same-bit HW set/SW clear and distinct-bit updates, poll multiple bytes and check atomic visibility, queue conservation and CSb-based passthrough reopening."),
    "Q-10": ("Use the explicit exercise reset map in cadence_contract.md: all six selected peripheral leaves derive from LC; the external CPU derives from SYS. POR asserts both, LC propagates to SYS and isolated SYS reset does not assert LC. Each leaf has one parent and synchronized release.",
             "Check one reset parent per selected leaf; cover POR, LC, SYS, stopped-clock assertion and domain-specific release without stale state."),
    "Q-13": ("Use dft0=IOR5, dft1=IOR7, tap0=IOC8 and tap1=IOC5. Any optional third generic DFT strap is tied zero by this authored wrapper contract and tested as zero; no third external pad is required.",
             "Check named straps, sampling and lifecycle gating plus the constant third strap if instantiated; no unbound configuration port is permitted."),
}
for _item in DECISIONS:
    if _item["issue_id"] in _CADENCE_DECISIONS:
        _rule, _checks = _CADENCE_DECISIONS[_item["issue_id"]]
        _item.update(rule=_rule, checks=_checks, local_gate=None,
                     classification="exercise_convention", resolution_state="contract_defined")


AUTHOR_POLICY = (
    "Benchmark-author policy: explicit declared field names, bit ranges, access types and selected "
    "integration configuration govern over obsolete narrative references. This is an authored exercise "
    "policy, not an attributed user instruction or a universal upstream priority rule. Conflicts inside "
    "HJSON and behavioral ambiguities use the individually named adjudications; no blanket HJSON-wins "
    "rule resolves them. Authored decisions supersede only their stated scope. Preserve original source "
    "bytes and provenance. Local open obligations gate only affected acceptance claims. Candidate/golden "
    "RTL and UVM are not specification authority. No correction is simulation or coverage evidence."
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value, *, crlf=False) -> bytes:
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    return (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")


def replace_section(text: str, name: str, body: str) -> str:
    start, end = f"<!-- BEGIN {name} -->", f"<!-- END {name} -->"
    section = f"{start}\n{body.rstrip()}\n{end}\n"
    if start in text:
        prefix, tail = text.split(start, 1)
        _, suffix = tail.split(end, 1)
        return prefix + section + suffix.lstrip("\n")
    return text.rstrip() + "\n\n" + section


def applicable(case_id: str, item: dict) -> bool:
    if item["module"] in {"context", "tlul"}:
        return True
    serial = item["module"] in {"spi_host", "spi_device", "usbdev"}
    return serial == (case_id == CASES[0])


def module_for(case_id: str, item: dict, root_module: str) -> str:
    if item["issue_id"] == "CT-03" and case_id == CASES[1]:
        return "pinmux"
    if item["issue_id"] == "CT-05" and case_id == CASES[0]:
        return "spi_device"
    if item["module"] in {"context", "tlul"}:
        return root_module
    return item["module"]


def source_targets(item: dict, source_entries: dict, source_docs: dict) -> list[dict]:
    targets = []
    for alias, first, last in item["spans"]:
        path = PATHS[alias]
        if path not in source_entries:
            # E.g. the AON case has whole-chip SPI background but no SPI_DEVICE IP.
            if item["issue_id"] != "CT-05":
                raise ValueError(f"{item['issue_id']} source missing: {path}")
            continue
        entry = source_entries[path]
        document = source_docs[entry["id"]]
        count = len(document["content"].splitlines())
        if not 1 <= first <= last <= count:
            raise ValueError(f"Invalid anchor {path}:{first}-{last} of {count}")
        targets.append({"document_id": entry["id"], "sha256": entry["sha256"],
                        "line_start": first, "line_end": last})
    if not targets:
        raise ValueError(f"No source target for {item['issue_id']}")
    return targets


def correction_text(item: dict, case_id: str, source_entries: dict) -> str:
    anchors = []
    for alias, first, last in item["spans"]:
        path = PATHS[alias]
        if path in source_entries:
            anchors.append(f"- `{path}` lines {first}-{last}.")
    gate = f"Local acceptance gate: {item['local_gate']}\n\n" if item["local_gate"] else ""
    return (
        f"# {item['issue_id']}: {item['classification']}\n\n"
        "Authority: benchmark author. Exercise policy; upstream bytes unchanged; no RTL/EDA result claimed.\n\n"
        f"Effective rule: {item['rule']}\n\n"
        f"Rejected reading: {item['rejected']}\n\n"
        f"Checker obligations: {item['checks']}\n\n"
        f"{gate}"
        f"Pinned source anchors (OpenTitan `{COMMIT}`):\n" + "\n".join(anchors) + "\n"
    )


def build_case(case_dir: Path) -> dict:
    case_id = case_dir.name
    input_dir = case_dir / "input"
    spec_path = input_dir / "spec.json"
    manifest_path = input_dir / "source_manifest.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    profile = json.loads((input_dir / "integration_profile.json").read_text(encoding="utf-8"))
    root_module = profile["root_module"]
    original_entries = [entry for entry in manifest["documents"] if entry.get("upstream_path")]
    source_entries = {entry["upstream_path"]: entry for entry in original_entries}
    source_docs = {doc["id"]: doc for doc in spec["documents"] if doc.get("origin", {}).get("kind") != "source_adjudication"}
    for entry in original_entries:
        raw = (input_dir / entry["path"]).read_bytes()
        if sha(raw) != entry["sha256"] or raw.decode("utf-8") != source_docs[entry["id"]]["content"]:
            raise ValueError(f"Original source drift: {entry['path']}")

    items = [item for item in DECISIONS if applicable(case_id, item)]
    profile["toolchain"] = dict(CADENCE_TOOLCHAIN)
    profile["forbidden_execution_roots"] = ["sources"]
    profile["execution_contract"] = {"path": "cadence_contract.md", "document_id": "adjudication-CADENCE",
        "authority": "benchmark_author", "interface_revision": "bench_tl_v1",
        "upstream_native_build_required": False, "reference_role": "historical_semantic_comparison"}
    profile["design_choices"] = [
        choice.replace("Preserve source-defined parameters, clocks/resets and CSR semantics in a versioned configuration.",
                       "Preserve source-defined CSR fields and configured geometry; use the authored digital clock/reset/ABI convention in cadence_contract.md.")
        for choice in profile["design_choices"]]
    profile["source_precedence"] = AUTHOR_POLICY
    profile["source_adjudications"] = {
        "revision": REVISION, "authority": "benchmark_author",
        "issue_ids": [item["issue_id"] for item in items],
        "scope_policy": "adjudications/SCOPE.md",
        "execution_policy": "cadence_contract.md",
        "open_local_obligations": [item["issue_id"] for item in items if item["local_gate"]],
        "no_simulation_claim": True,
    }
    # These three pre-existing case files use CRLF; keep their repository format.
    (input_dir / "integration_profile.json").write_bytes(json_bytes(profile, crlf=True))

    problem_path = input_dir / "problem.md"
    problem = problem_path.read_text(encoding="utf-8")
    problem = problem.replace(
        "Where prose and HJSON conflict, record the disagreement and block affected decisions instead of guessing precedence.",
        "Use the revised benchmark-author contract below for named issues. New gaps require precise local obligations, not blanket source-conflict blockers.",
    )
    rows = ["## Revised normative exercise contract", "", AUTHOR_POLICY, "",
            "**[CADENCE execution contract](cadence_contract.md):** " + CADENCE_RULE, "",
            "The fixed top, logical bus, digital interfaces, clock/reset domains and physical abstraction in that contract are mandatory input. "
            "Use dv_generate_uvm(profile_path='eda/verification_profile.json'); complete and review eda/project.json for the staged Cadence runner. "
            "No upstream native build is required. No EDA result is supplied.", "",
            "The following rules are part of the question. Both RTL and TP/UVM generation must consume the same revision. "
            "Each linked correction is also embedded in spec.json with exact source-span targets so bounded contexts receive it alongside affected prose.", ""]
    for item in items:
        gate = f" **Local gate:** {item['local_gate']}" if item["local_gate"] else ""
        rows.append(f"- **[{item['issue_id']}](adjudications/{item['issue_id']}.md)** ({item['classification']}): {item['rule']}{gate}")
    rows.extend(["", "An authored convention defines this exercise, not a new claim about the held-out upstream implementation. "
                 "If that implementation differs, report reference divergence; do not change the oracle to fit it. "
                 "An open local obligation is not a passed or covered requirement and must not be silently removed from a frozen coverage denominator. "
                 "Unrelated specified work proceeds; full acceptance waits only on the applicable, precisely identified dependencies."])
    problem_path.write_bytes(replace_section(problem, "SOURCE-ADJUDICATIONS", "\n".join(rows)).encode("utf-8"))

    spec["documents"] = [doc for doc in spec["documents"] if doc.get("origin", {}).get("kind") != "source_adjudication"]
    manifest["documents"] = [entry for entry in manifest["documents"] if not entry["id"].startswith("adjudication-")]
    # Refresh the existing authored problem/profile documents, including their exact hashes.
    entries_by_id = {entry["id"]: entry for entry in manifest["documents"]}
    for doc in spec["documents"]:
        if doc["id"] not in {"case-problem-md", "case-integration_profile-json"}:
            continue
        entry = entries_by_id[doc["id"]]
        raw = (input_dir / entry["path"]).read_bytes()
        doc["content"] = raw.decode("utf-8")
        doc["origin"]["sha256"] = sha(raw)
        entry.update(sha256=sha(raw), bytes=len(raw))

    additions = input_dir / "adjudications"
    additions.mkdir(exist_ok=True)

    def add_document(name, content, module, layer, targets, path=None):
        raw = content.encode("utf-8")
        path = path or f"adjudications/{name}.md"
        (input_dir / path).write_bytes(raw)
        doc_id = f"adjudication-{name}"
        spec["documents"].append({
            "id": doc_id, "title": f"Benchmark author contract: {name}",
            "layer": layer, "module": module, "role": "specification",
            "origin": {"authored": True, "kind": "source_adjudication", "sha256": sha(raw), "targets": targets},
            "content": content,
        })
        manifest["documents"].append({
            "id": doc_id, "path": path, "sha256": sha(raw), "bytes": len(raw),
            "layer": layer, "module": module, "role": "specification", "count_toward_size": False,
            "authored": True, "kind": "source_adjudication",
        })

    for item in items:
        module = module_for(case_id, item, root_module)
        add_document(item["issue_id"], correction_text(item, case_id, source_entries), module,
                     "architecture" if module == root_module else "module",
                     source_targets(item, source_entries, source_docs))

    dut_names = ", ".join(entry["type"] for entry in profile["modules"])
    scope_text = (
        f"# SCOPE: {case_id} exercise contract\n\n"
        f"Benchmark-author authority ({REVISION}). DUT module types are exactly {dut_names}; "
        f"the wrapper is {root_module}. Preserve the selected instance/configuration identities in "
        "integration_profile.json and the shared module_graph.json. Whole-chip chapters, UART, ROM, "
        "CPU, pads/technology and other external consumers are context or boundaries unless explicitly "
        "listed as DUTs. Do not create new RTL modules/coverage obligations or global blockers from "
        "unrelated background discrepancies.\n\n"
        "Source-adjudication documents supersede only their named statements/scopes; cite them with "
        "the original source. Literal field/access/bit declarations and selected configuration govern "
        "over obsolete names. Intra-source behavioral conflicts need the individual author decision, "
        "not a universal HJSON-wins rule. Do not infer a different source variant to excuse contradictions.\n\n"
        "Both RTL and UVM use this same accepted contract. A precise missing interface/technology/timing "
        "definition gates only its dependent binding/checker; independent work continues. Unknown, "
        "unrun and reference-divergent behavior cannot count as passed or covered. Candidate/golden "
        "RTL never supplies silent requirements; author conventions are exercise choices, not claims "
        "of upstream bugs. Corrections and file size are not simulation evidence.\n"
    )
    scope_targets = [{"document_id": entry["id"], "sha256": entry["sha256"], "line_start": 1,
                      "line_end": len(source_docs[entry["id"]]["content"].splitlines())}
                     for entry in original_entries]
    add_document("SCOPE", scope_text, root_module, "architecture", scope_targets)
    add_document("CADENCE", cadence_contract(case_id, profile), root_module, "architecture",
                 scope_targets, "cadence_contract.md")

    rubric_path = case_dir / "obligations.json"
    rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
    rubric["adjudication_revision"] = REVISION
    rubric["adjudication_obligations"] = [{
        "issue_id": item["issue_id"], "classification": item["classification"],
        "resolution_state": item["resolution_state"], "module": module_for(case_id, item, root_module),
        "requirement": item["rule"], "checks": [item["checks"]],
        "local_gate": item["local_gate"], "status": "not_run",
        "source_document_id": f"adjudication-{item['issue_id']}",
    } for item in items]
    rubric["adjudication_obligations"].append({
        "issue_id": "CADENCE", "classification": "execution_contract", "resolution_state": "contract_defined",
        "module": root_module, "requirement": CADENCE_RULE,
        "checks": ["No network fetch or code generation may occur during EDA execution."],
        "local_gate": None, "status": "not_run", "source_document_id": "adjudication-CADENCE"})
    rubric["evaluation_contract"] = {"toolchain": dict(CADENCE_TOOLCHAIN),
        "normative_input": "input/cadence_contract.md", "reference_testpoint_role": "historical_review_hints",
        "upstream_native_regression_required": False, "independent_oracle_required": True,
        "functional_coverage_percent": 90, "execution_status": "not_run"}
    conflict_map = {"COMMON-TLUL-INTEGRITY": ["CT-01", "D-02"], "SPI-LEN-WIDTH": ["SH-05"],
                    "SPI-CLKDIV-EQUATIONS": ["SH-03", "SH-04"]}
    for conflict in rubric["source_conflicts"]:
        if conflict["id"] in conflict_map:
            ids = conflict_map[conflict["id"]]
            conflict["adjudication_ids"] = ids
            conflict["resolution_state"] = "contract_defined"
            conflict["required_resolution"] = "Apply the revised question's " + ", ".join(ids) + "; retain their checker obligations and any precise local gate. Do not reopen settled prose contradictions or silently copy golden RTL."
        if "event" in conflict.get("id", "").lower() or "event-interrupt" in conflict.get("issue", ""):
            conflict["resolution_state"] = "contract_defined"
            conflict["required_resolution"] = "Use the registered enabled-level event IRQ timing defined in cadence_contract.md; test enable-while-active and assertion/deassertion."
    rubric["reference_gaps"] = [
        "Historical upstream RTL/UVM/testpoint names are comparison evidence, not required execution dependencies or the authored golden.",
        "The candidate must supply its own complete portable SV/UVM/SVA closure and independent contract-derived oracle.",
        "Cadence execution, checker activity and coverage remain not_run until actual external tool evidence is verified."]
    rubric.pop("limitations", None)
    rubric_path.write_bytes(json_bytes(rubric, crlf=True))

    dependency_path = case_dir / "dependency_declaration.json"
    dependency = json.loads(dependency_path.read_text(encoding="utf-8"))
    dependency.update(toolchain=dict(CADENCE_TOOLCHAIN), native_build_required=False,
        stored_reference_role="historical_semantic_comparison_not_build_closure",
        required_build_inputs=["candidate ordered portable SV RTL and packages", "candidate SV UVM/SVA and BFMs",
                               "candidate local include/memory files", "Cadence installation UVM", "reviewed eda/project.json and constraints"],
        limitations=[
            "Stored original RTL/UVM/FPV/core files retain historical provenance; they are not a qualified standalone Cadence build closure.",
            "Listed upstream dependency roots and build descriptors are optional investigation hints, not required candidate dependencies.",
            "No full upstream checkout, reggen/topgen, DVSim, FuseSoC or upstream regression is required by the authored exercise.",
            "If an optional upstream comparison is attempted, its own complete dependencies and checker activity must first be qualified separately.",
            "Digital bus/pad/clock/reset conventions in input/cadence_contract.md define the oracle; production physical signoff is outside scope.",
            "No Cadence elaboration, simulation, lint/CDC, proof, synthesis or coverage result has been performed by this pack."])
    dependency_path.write_bytes(json_bytes(dependency))

    readme_path = case_dir / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    readme = readme.replace(
        "These stored files have not been qualified as a runnable subsystem with Vivado.",
        "The authored [Cadence contract](input/cadence_contract.md) defines a fresh portable SV implementation and independent SV UVM/SVA environment. The upstream subset is historical comparison material; no upstream native build or complete upstream checkout is required. The case tool stages a Cadence execution kit separately from original sources.")
    readme = readme.replace(
        "Pinmux lacks an upstream UVM simulation environment at this revision; available formal material is not a replacement for one.",
        "Create the independent PINMUX UVM environment from the authored digital contract; upstream formal examples are optional review material.")
    readme_path.write_bytes(readme.encode("utf-8"))

    guide_path = case_dir / "answer_guide.md"
    guide = guide_path.read_text(encoding="utf-8")
    guide = guide.replace(
        "Use the full pinned upstream checkout and its documented dependency/tool setup when resolving build targets.",
        "The candidate supplies a complete portable SV/UVM/SVA closure for Cadence. A full upstream checkout and its native build are not required.")
    guide = guide.replace(
        "The TL-UL README leaves its Bus Integrity Scheme unfilled. The input alone cannot define every integrity code or accepted fault. Explicitly supply a versioned normative addendum before claiming full integrity coverage. Do not inflate applicability exclusions to manufacture the requested percentage.",
        "The original TL-UL README has an unfilled integrity section. The authored input/cadence_contract.md now supplies bench_tl_v1 packed fields, CRC7, masks, errors and reset epochs. This is a logical exercise ABI, not upstream binary/SECDED compatibility; use it to build an independent SV oracle.")
    guide = guide.replace(
        "Event-interrupt temporal qualification still needs its separate assertion/deassertion and enable-while-active contract; resolved event identities do not supply that timing rule.",
        "The registered enabled-level event IRQ timing, including enable-while-active, is defined by input/cadence_contract.md.")
    guide = guide.replace(
        "Resolve documented source conflicts before implementing dependent behavior. Record the alternatives, chosen normative interpretation and source anchors in a versioned decision. Do not silently choose the behavior of golden RTL. A missing normative detail remains unresolved until an explicit addendum is supplied.",
        "Apply the revised problem's source adjudications before implementing dependent behavior. Their author decisions are the exercise oracle; cite both the correction and pinned evidence. Remaining local gaps require explicit addenda only for affected acceptance claims. Never silently select golden RTL behavior.",
    )
    guide = guide.replace(
        "The SPI_HOST theory lists an 8-bit LEN while the register definition uses 9 bits; README core-clock wording also conflicts with the declared single-clock interface. These require explicit interpretation records. The CLKDIV equations also conflict with the divider register formula, and event-interrupt timing needs an explicit level-versus-entry interpretation. Do not quietly adjust stimulus or expected values to the reference implementation.",
        "SH-05 fixes the exercise LEN contract to nine bits; SH-03/SH-04 fix period/timeslice formulas. Preserve the selected single-clock SPI_HOST interface. Event-interrupt temporal qualification still needs its separate assertion/deassertion and enable-while-active contract; resolved event identities do not supply that timing rule. Do not adjust the oracle to match the reference implementation.",
    )
    local_ids = [item["issue_id"] for item in items if item["local_gate"]]
    acceptance = (
        "## Revised-question acceptance\n\n"
        f"Use `{REVISION}`: {len(items)} applicable grouped issue records plus routed scope and Cadence execution policies. "
        "The reported historical 66 log rows are not 66 independent defects. See `input/problem.md` for the normative rules "
        "and `adjudication_obligations` in the rubric for the required checks. Original upstream prose is preserved as provenance; "
        "the authored corrections revise the question.\n\n"
        "For each applicable record, inspect RTL/TP/UVM traceability and execute its independent checker or scope audit. "
        "A defined contract is not a passing test. Classify observed results as passed, failed, untested or locally blocked. "
        "Background scope audits and not-a-conflict decisions do not add DUT coverage bins. Do not count unknown behavior "
        "as covered, delete unresolved requirements from the frozen denominator or treat all Q items as proven bugs.\n\n"
        "The supplied cadence_contract.md closes the previous packed-ABI, integrity, digital pad, reset-leaf and timing gaps "
        "using explicit author choices. Derive the independent golden from this contract; the upstream implementation may differ. "
        "Run portable candidate SV/UVM/SVA through Xcelium/IMC, Jasper lint/CDC/FPV and Genus using the staged kit. "
        "No native OpenTitan build, full upstream checkout or production pad technology is required.\n\n"
        "The held-out pinned implementation is comparison material, not absolute authority over author-chosen conventions. "
        "If it disagrees with an exercise convention, record reference divergence and evaluate the generated candidate against "
        "the accepted exercise contract. No updated document establishes simulation success or a coverage percentage."
    )
    guide_path.write_bytes(replace_section(guide, "SOURCE-ADJUDICATIONS", acceptance).replace("\n", "\r\n").encode("utf-8"))
    spec_raw = json_bytes(spec)
    spec_path.write_bytes(spec_raw)
    manifest["spec_sha256"] = sha(spec_raw)
    manifest["integration_profile_sha256"] = sha((input_dir / "integration_profile.json").read_bytes())
    manifest["adjudication_revision"] = REVISION
    manifest["adjudication_issue_count"] = len(items)
    manifest["adjudication_scope_policy_count"] = 1
    manifest["execution_contract_policy_count"] = 1
    manifest_path.write_bytes(json_bytes(manifest))
    return {"case_id": case_id, "issues": len(items), "scope_policies": 1, "local_obligations": local_ids,
            "unique_specification_body_bytes": manifest["unique_specification_body_bytes"], "spec_bytes": len(spec_raw)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", type=Path, default=ROOT / "cases")
    args = parser.parse_args()
    for case_id in CASES:
        print(json.dumps(build_case(args.cases_root / case_id), ensure_ascii=False))
    refresh_corpus_manifest(args.cases_root)


def refresh_corpus_manifest(cases_root: Path) -> None:
    """Rehash authored files while retaining upstream blob pins and metadata."""
    path = cases_root / "corpus_manifest.json"
    corpus = json.loads(path.read_text(encoding="utf-8"))
    for record in corpus["cases"]:
        if record["id"] not in CASES:
            continue
        case_dir = cases_root / record["id"]
        for field, base in (("input_files", case_dir / "input"), ("answer_files", case_dir)):
            previous = {row["path"]: row for row in record[field]}
            names = set(previous)
            if field == "input_files":
                names.add("cadence_contract.md")
            record[field] = []
            for name in sorted(names):
                raw = (base / name).read_bytes()
                entry = dict(previous.get(name, {"path": name}))
                entry.update(bytes=len(raw), sha256=sha(raw))
                record[field].append(entry)
    path.write_bytes(json_bytes(corpus))


if __name__ == "__main__":
    main()
