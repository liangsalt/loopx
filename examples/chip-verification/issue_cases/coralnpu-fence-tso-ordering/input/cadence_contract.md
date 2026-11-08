# Cadence contract: memory ordering, fence decode and retirement

This authored independent SystemVerilog exercise is inspired by CoralNPU issue
#130; it is not a port or a bit-exact reproduction of the original core. Build
`coral_memory_order_subsystem` from this contract and the applicable ISA clauses.
The original Scala/Chisel decoder and upstream patch are historical mechanism
references only. Scala, Chisel/firtool, Bazel, MPACT, Cocotb, Verilator, the full
Coral repository, ELF generation and a RISC-V compiler are not build or
acceptance dependencies. A pure-SV UVM producer supplies instruction words and
operand values directly; no instruction fetch unit or full CPU is required.

## M01. Shared design and verification decomposition

Use instruction/configuration snapshot, legal decode, load/store address and
alignment checking, memory-order tracking, tagged response tracking, completion
buffering and ordered retirement. `module_graph.json` is shared by RTL and
TP/UVM. Physical modules may be merged, but every listed obligation remains.
The producer and byte-addressed memory BFM are external environment boundaries.
Decode exact supported encodings; do not fix the known symptom by globally
disabling illegal-instruction traps or accepting every MISC-MEM instruction.

## M02. Static interface and handshake contract

Compile `contracts/coral_memory_order_contract_pkg.sv` before the DUT; the top
uses `contracts/coral_memory_order_ports.svh`. These assets contain only types
and a port list, not an implementation or a scoreboard. The fixed profile has
XLEN=32, four accepted command slots, four outstanding read transactions,
8-bit command tags and 8-bit memory IDs. One rising-edge clock `clk_i` runs at
10 ns in simulation; this timing choice is not a silicon performance claim.
Active-low `rst_ni` asserts asynchronously and releases synchronously after two
edges. Reset clears all valid outputs and internal tracking; the environment
discards outstanding responses from the old reset epoch.

Ports are `clk_i`, `rst_ni`, `flush_i`, command input `cmd_valid_i`, `cmd_ready_o`,
`cmd_i`; memory output `mem_req_valid_o`, `mem_req_ready_i`, `mem_req_o`; memory
response input `mem_rsp_valid_i`, `mem_rsp_ready_o`, `mem_rsp_i`; and retirement
output `retire_valid_o`, `retire_ready_i`, `retire_o`. Types and widths are
normative in the package. Except reset/flush cancellation, valid and payload
remain stable while stalled. All transfers occur at valid/ready rising edges.
The DUT holds `mem_rsp_ready_o=1` outside reset by reserving a response slot
for each issued ID. The BFM may delay or reorder responses, never duplicate an
ID, and returns data/error only for an issued request. Command tags are unique
until retirement and associated drains complete; memory IDs are unique until
their response. Reuse after completion is legal and must be tested.

Each accepted command snapshots `tag`, `pc`, raw 32-bit `instruction`, `rs1`
and `rs2` values. The producer supplies correctly forwarded operand values for
its program stream; fetch, physical register-file hazards and instruction-cache
coherence are outside this subsystem. The DUT performs decode, effective-address
generation, memory operation, fence ordering and the defined retirement effects.
No result is inferred from a successful simulator exit.

## M03. Instruction and exception subset

Support RV32I ADDI, ADD, LB/LH/LW/LBU/LHU, SB/SH/SW, ordinary FENCE and the exact
FENCE.TSO encoding `32'h8330000f`. ADD/ADDI arithmetic wraps at 32 bits. Decode
opcode/funct3/funct7 correctly. Load effective address is rs1+sign-extended I
immediate; store address is rs1+sign-extended S immediate. The destination is
instruction rd; rd=x0 suppresses scalar writeback but not legal memory reads,
faults or retirement. Load bytes/halfwords sign-extend except LBU/LHU. Stores
take their low 8/16/32 data bits from rs2. PC is an opaque retirement observation.

Ordinary FENCE has opcode 0001111, funct3=000, fm=0; pred/succ use the standard
I/O/R/W bits. This exercise exposes only normal memory R/W: I/O subsets create
no extra device obligations. rd/rs1 are reserved operands ignored as specified
for FENCE; preserve the conservative legal semantics for fm=0. The exact
FENCE.TSO word is legal and is dispatched as a fence, not an ALU no-op or trap.
Other fm values are outside this selected exercise and retire illegal with
cause 2. FENCE.I (funct3=001) is not enabled because Zifencei and an instruction
cache are outside the profile; it must remain distinct and trap with cause 2.
All other unsupported opcode/funct combinations trap with cause 2 and no memory
or scalar write. Illegal words remain illegal even when their operands are zero.

Before issuing a load/store, check natural alignment: halfword address bit 0
and word address bits 1:0 must be zero. Misaligned load/store traps with cause
4/6 and tval equal to effective address, issuing no memory transaction. Response
errors produce cause 5/7 and that same tval. Illegal-instruction tval is the raw
instruction. Successful commands have trap=0, cause=0 and tval=0. Successful
stores and fences never write a scalar destination. No implicit reset or halt
is used to turn a failed instruction into a completed instruction.

## M04. Tagged memory and observable ordering

Memory requests contain unique ID, command tag, original byte address, size
code 0=byte/1=halfword/2=word, write flag and low-byte-aligned store data.
Responses contain the same ID, error, and low-byte-aligned little-endian load
data. The memory BFM applies a successful store and returns a load's memory
value at its response-transfer edge; ready is always high, so a presented
response transfers without a hidden response holding interval. Writes do not
become globally visible merely when a request is queued. Address ranges are
ordinary coherent memory; device/MMIO behavior, caches and speculative external
masters are outside this exercise. Directed tests also use disjoint addresses
to distinguish ordering from same-address forwarding.

For each fence, divide accepted commands into earlier and later instructions.
A selected order edge is satisfied only when every relevant earlier memory
operation has completed its response before a relevant later request is issued
(strictly earlier edge, not the same edge). For fm=0 FENCE, pred R/W crossed
with succ R/W determines the required pairs. Empty predecessor or successor
sets impose no memory-order edge but the fence still retires once.
FENCE.TSO requires earlier loads before later loads and stores (R->R, R->W),
and earlier stores before later stores (W->W). W->R relaxation is permitted,
not required. Implementing TSO as stronger full FENCE RW,RW is correct. Neither
tests nor coverage may require an allowed reordering to occur in a correct
stronger implementation; the relaxation observation is informational only.

Ordinary memory ordering in this reduced exercise preserves overlapping byte
ranges with at least one write in acceptance order: the older access response
is on a strictly earlier clock edge than the younger request, never the same
edge. Byte ranges are compared modulo 2^32, including
address wrap. Different-address reads may issue concurrently
and complete out of order. A conservative implementation may serialize more
operations, subject to the explicit read-concurrency requirement in M06.
Only the oldest unretired command may issue stores; this defines a precise
store side-effect boundary around older exceptions and retirement backpressure.
Loads from younger commands may be outstanding, but their scalar writebacks
remain buffered until their ordered retirement. A trap does not silently retire
or delete younger commands; an explicit flush may cancel them.

## M05. Ordered retirement, flush and reset

Each accepted command produces exactly one retirement in acceptance order,
including fences, rd=x0 instructions, illegal encodings and memory faults.
The retirement packet carries tag, pc, instruction, cancelled, trap, cause,
tval, rd, rd_we and rd_data. Its valid/ready handshake is the sole architectural
scalar-write event. rd_we is false on trap, cancellation, stores, fences or rd=x0.
No younger result can pass a stalled older retirement. Trap/retirement and memory
activity are independently monitored; a completion counter alone is insufficient.

Synchronous flush suppresses command acceptance, new requests and normal
retirement on its edge. Accepted but not retired commands become cancelled
and later retire exactly once in order with no scalar write and no trap.
Cancelled packets have cause=0 and tval=0; unenabled destination fields are ignored.
Already completed stores remain visible; flush does not roll back memory. An
already-accepted store request may still complete and become visible after
flush; no new request is issued for a cancelled command. Outstanding old
responses are drained without scalar writes; do not associate them with reused tags.
Block new command acceptance until cancelled records have retired and old
requests have drained. Repeated flush during drain is idempotent. Reset discards
all records instead, and the BFM discards responses from that reset epoch.
If flush stays low after its last asserted edge and no reset intervenes, the
same ready/response fairness bounds require all cancelled retirements and
draining to complete within 1024 cycles of that edge, with cmd_ready_o restored
by the deadline. A held-valid new command then obeys the four-cycle acceptance
bound. Continually reasserted flush carries no finite recovery claim.

## M06. Capacity, fairness and directed obligations

Outside flush recovery, accept four independent commands into available slots without requiring their
retirement, including while responses or retire_ready are delayed. When a slot
is available and cmd_valid is held high outside flush recovery, accept that command within four cycles.
For the read-concurrency litmus, present two independent aligned loads
back-to-back with no required intervening order edge. Hold mem_req_ready_i=1
and temporarily withhold responses from acceptance of the first load. Within
eight cycles after acceptance of the second command, both read requests must
be accepted. Then release responses, meeting every request's 32-cycle deadline.
Failure to issue the second request within eight cycles fails the litmus;
withholding responses indefinitely is not a valid test. In ordinary tests an
immediate response is legal, without any claim that the second request must
precede it. This finite capacity test forbids using a single blocking
transaction slot as the answer. Do not require multiple
outstanding stores or force the optional TSO W->R relaxation.

For liveness tests the environment supplies ready at least once per eight
cycles and a response within 32 cycles for every issued request. Every accepted
non-flushed command then retires within 1024 cycles. No bounded-liveness claim
is made without those assumptions. Directed obligations include TSO legality,
all three required order pairs, ordinary fence masks including empty masks,
back-to-back fences, older delayed stores plus younger loads, out-of-order load
responses, rd=x0, all access sizes/sign modes, misalignment, bus error, illegal
nearby encodings, tag reuse, retire stalls and flush/reset races. Resetting the
DUT to escape a fence wait fails the progress obligation.

## M07. Cadence implementation and acceptance

Deliver synthesizable standard SV, explicit package/file ordering and a pure-SV
UVM environment. Required roles are command sequencer/driver, memory responder,
passive request/response/retirement monitors, a byte-memory and instruction
scoreboard, an order-edge scoreboard, SVA and TP-linked covergroups. The expected
model is independently derived from M03-M06 and the applicable ISA clauses;
do not read the candidate RTL to define expected behavior. Check behavior-changing
mutations for illegal TSO decode, fence treated as a no-op, violation of an R->W
edge (bypassing other serialization when needed), swapped response
ID, duplicate retirement, wrong sign extension and dropped flush cancellation.
Each mutation must create an observable contract violation; document
semantically equivalent/redundant edits as inapplicable negative controls.
Legal stronger ordering must not be labelled a surviving bug mutation.

Freeze applicable functional bins before the first run: decoded instruction
class, fence pred/succ masks, all mandatory order pairs, load/store size and
alignment, error class, rd=x0, tag reuse, queue occupancy, response permutation,
ready-pressure phase, reset/flush phase. Mandatory crosses include fence class
x required pair x delayed predecessor, fence x retirement stall, and flush x
pending response. An optional W->R-reordering bin cannot be placed in the
mandatory denominator. Require >90% applicable functional bins, all directed
obligations, all declared mutation detections and no unexpected UVM/SVA errors.
Report RTL code/toggle and assertion coverage separately.

Xcelium compiles and runs the direct SV/UVM closure; IMC consumes its native
coverage database. Use Jasper Superlint/CDC/FPV with explicit clock/reset and
fairness assumptions, and Genus for RTL elaboration/synthesizability. This is a
single-clock interface; CDC evidence must say so rather than fabricate a
crossing. None of these stages requires proprietary VIP or upstream build tools.
The exported kit contains `eda/cadence_runner.py` inspect/plan/run and
`eda/verification_profile.json`; supply that file to `dv_generate_uvm`.
Put the actual candidate and TB inventory in `eda/project.json`, with fixed
seeds, constraints, raw tool outputs, coverage model and input hashes. The EDA
host supplies Cadence binaries/licenses. This package has not been compiled,
simulated or formally proven on that host; source closure checks are not signoff.

## Exact interface assets

These definitions are the complete authored ABI; they are included in source contexts.

### contracts/coral_memory_order_contract_pkg.sv

```systemverilog
// SPDX-License-Identifier: Apache-2.0
// Authored interface contract only; no implementation or verification oracle.
package coral_memory_order_contract_pkg;
  localparam int XLEN = 32;
  localparam int COMMAND_SLOTS = 4;
  localparam int READ_SLOTS = 4;
  localparam logic [31:0] FENCE_TSO = 32'h8330000f;
  typedef struct packed {
    logic [7:0] tag;
    logic [31:0] pc;
    logic [31:0] instruction;
    logic [31:0] rs1;
    logic [31:0] rs2;
  } memory_command_t;
  typedef struct packed {
    logic [7:0] id;
    logic [7:0] tag;
    logic write;
    logic [31:0] address;
    logic [1:0] size;
    logic [31:0] data;
  } memory_request_t;
  typedef struct packed {
    logic [7:0] id;
    logic error;
    logic [31:0] data;
  } memory_response_t;
  typedef struct packed {
    logic [7:0] tag;
    logic [31:0] pc;
    logic [31:0] instruction;
    logic cancelled;
    logic trap;
    logic [4:0] cause;
    logic [31:0] tval;
    logic [4:0] rd;
    logic rd_we;
    logic [31:0] rd_data;
  } memory_retirement_t;
endpackage
```

### contracts/coral_memory_order_ports.svh

```systemverilog
// SPDX-License-Identifier: Apache-2.0
// Include inside module coral_memory_order_subsystem(...).
input  logic clk_i,
input  logic rst_ni,
input  logic flush_i,
input  logic cmd_valid_i,
output logic cmd_ready_o,
input  coral_memory_order_contract_pkg::memory_command_t cmd_i,
output logic mem_req_valid_o,
input  logic mem_req_ready_i,
output coral_memory_order_contract_pkg::memory_request_t mem_req_o,
input  logic mem_rsp_valid_i,
output logic mem_rsp_ready_o,
input  coral_memory_order_contract_pkg::memory_response_t mem_rsp_i,
output logic retire_valid_o,
input  logic retire_ready_i,
output coral_memory_order_contract_pkg::memory_retirement_t retire_o
```
