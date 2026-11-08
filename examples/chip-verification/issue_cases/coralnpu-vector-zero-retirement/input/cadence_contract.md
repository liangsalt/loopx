# Cadence contract: vector command execution and retirement

This is an authored, independently implementable SystemVerilog exercise inspired
by CoralNPU issue #89. It is not a port or a bit-exact reproduction of CoralNPU.
The complete implementation boundary is `coral_vector_retirement_subsystem`.
Original ISA documents supply the applicable behavioral rationale; this document
defines the exercise's interfaces, selected subset and environment. The original
Coral source tree, Scala, Chisel/firtool, Bazel, MPACT, Cocotb, Verilator, ELF files
and a RISC-V software compiler are not build or acceptance dependencies.

## V01. Shared design and verification decomposition

The required functional partition is command/configuration snapshot, legality
decode, vector arithmetic, element-address generation, tagged memory tracking,
completion buffering and ordered retirement. `module_graph.json` describes these
same relationships for RTL and TP/UVM. Implementations may merge physical blocks
while retaining their observable obligations. The memory system and command
producer are independent SV/UVM testbench models, outside the DUT. Implement an
in-order retirement subsystem, not an instruction fetch unit or a full NPU.

## V02. Static interface and operating profile

Compile `contracts/coral_vector_contract_pkg.sv` first. The top declaration uses
the port list in `contracts/coral_vector_ports.svh`. These two input assets define
types and ports only; they contain no DUT implementation, driver or golden model.
The fixed profile is XLEN=32, VLEN=128, ELEN=32, LMUL=1, four accepted command
slots, four outstanding read transactions, 8-bit command tags and 8-bit memory
transaction IDs. A source/destination vector is one 128-bit register value.
The external producer supplies operand values and the CSR snapshot in each
command; it is the instruction frontend/CSR bank boundary, not part of the DUT.
It must preserve producer-side data dependencies. Test sequences set `vl=0`
directly in the snapshot, avoiding the special `vsetvli x0,x0` encoding ambiguity.

All signals are sampled at the rising edge of `clk_i`. Use a 10 ns simulation
clock (an exercise timing choice, not a silicon performance claim). `rst_ni`
asserts asynchronously and deasserts synchronously after at least two clock
edges. During reset no command, request or retirement transfer is valid. Reset
discards the complete command/memory tracking state; the memory model discards
responses from the preceding reset epoch. The interface is single-clock.
Jasper CDC must identify that fact from the clock/reset constraints; no invented
asynchronous crossing or vacuous claimed multi-clock coverage is required.

The top ports are `clk_i`, `rst_ni`, `flush_i`, command input `cmd_valid_i`,
`cmd_ready_o`, `cmd_i`; memory request output `mem_req_valid_o`,
`mem_req_ready_i`, `mem_req_o`; memory response input `mem_rsp_valid_i`,
`mem_rsp_ready_o`, `mem_rsp_i`; retirement output `retire_valid_o`,
`retire_ready_i`, `retire_o`. Packed type fields and widths are normative in the
package. Commands, requests and ordinary retirements transfer when valid and
ready are high outside reset/flush. Payload and valid remain stable while stalled, except when
reset or flush explicitly cancels that interface. Memory responses are accepted
during flush draining. `mem_rsp_ready_o` is high outside reset: reserve response
storage for every issued transaction, rather than deadlocking on retirement.

Each command snapshots `op`, `tag`, `pc`, `vl`, `vstart`, `sew`, `vill`, `mask`,
`masked`, `base`, signed byte `stride`, `scalar`, `vs1`, `vs2` and `old_vd` at
acceptance. Later commands cannot modify a stalled or executing snapshot. Tags
are unique until retirement and any associated drain complete; memory IDs are
unique until their response is accepted. Reuse after those events is legal.
The memory model never fabricates a response for an unissued ID or duplicates
a response. Unknown/duplicate response IDs are protocol-environment violations,
not a mechanism for silently completing an unrelated command.

## V03. Selected operations and legality

`sew` encodes 0=8 bits, 1=16 bits, 2=32 bits; 3 is unsupported. VLMAX is
128/SEW. Ordinary operations are ADD, SUB, XOR, unit-stride LOAD/STORE and
strided LOAD/STORE. Element arithmetic wraps at SEW bits. Element i occupies
bits `i*SEW +: SEW` of the vector. ADD/SUB/XOR use corresponding `vs1` and
`vs2` elements; stores read `vs2`. A lane is active iff `vstart <= i < vl`
and either masked is false or `mask[i]` is one. All inactive/tail bytes preserve
`old_vd`; this exercise selects undisturbed tails and masks. A zero write-enable
retirement never writes a destination, even if its data field is arbitrary.
Vector write enables are exactly the bytes of successfully executed active
destination elements (or a successful MOVE_S_X element); other enables are zero.

For these ordinary operations, `vill`, unsupported SEW, reserved opcodes or
`vl > VLMAX` are illegal and retire with cause 2 and no memory/register effects.
An illegal command preserves its snapshot vstart in `vstart_next`.
Legality is checked before the zero-element shortcut. A legal operation with
`vl=0` or `vstart>=vl` issues no memory request, writes no vector/scalar element,
and still produces exactly one non-trapping retirement with `vstart_next=0`.
Never infer that every zero-VL encoding is a legal NOP.

The three exception classes are explicit:

* WHOLE_LOAD/WHOLE_STORE transfer one complete 128-bit register as sixteen
  byte elements, ignoring `vl`, SEW, `vill` and mask. `vstart` is a byte index;
  bytes below it are skipped and `vstart>=16` completes without transfers.
  This is the selected one-register, byte-element whole-register form; other
  group sizes and encodings are outside the exercise.
* MOVE_X_S reads element zero of `vs2`, sign-extends at SEW and writes the
  scalar result even when `vl=0` or `vstart>=vl`. It requires supported SEW
  and `vill=0`, ignores mask and leaves vector write enables zero.
* MOVE_S_X writes element zero from scalar only if `vl>0` and `vstart=0`,
  retaining other bytes. It requires supported SEW, `vill=0`, `vl<=VLMAX` and
  ignores mask. It is not converted into a general whole-vector write.

CONFIG computes `vl_next=min(scalar,VLMAX)` for legal SEW, returns `sew_next=sew`, `vill_next=0`,
`vstart_next=0` and sets `config_we`. Unsupported SEW instead returns
`vl_next=0`, `sew_next=sew`, `vill_next=1` without trapping. It ignores the prior vl/vstart/vill,
performs no memory or vector/scalar write, and still retires once. The external
CSR bank applies this result and supplies the resulting snapshot on later
commands; the producer waits for configuration retirement before dependent
commands. Other operations set `config_we=0` and retire `vstart_next=0` on
success. Reserved opcodes always trap, including when their snapshot has vl=0.

## V04. Memory, faults and architectural effects

Address arithmetic wraps modulo 2^32. Ordinary unit-stride element address is
`base+i*(SEW/8)`; strided address is `base+i*stride`, with signed two's-complement
stride. Whole-register byte address is `base+i`. Natural alignment is required
for active ordinary elements: a misaligned active load/store traps with cause
4/6 before issuing that element. Inactive lanes cannot fault or access memory.
The byte-addressed memory request carries tag, element index, a unique ID,
write flag, original element address, size code 0/1/2, and store data in its low
8/16/32 bits. Responses carry the same ID, little-endian load data in low bits,
and an error bit. The memory BFM applies a successful write and samples a read's
value at the response-transfer edge; an error response has no write effect.
Load/store response errors trap with cause 5/7 and report that element index.

Read responses may return in any order. Buffer them and retire an architectural
prefix: on the first failing active element in element-index order, retain
successfully completed lower active load elements, leave the faulting/higher
elements unchanged and set `vstart_next` to the failing element. Drain any
already-issued higher read responses without committing their data. Stores
issue active elements in ascending index order, with at most one store response
outstanding per command; do not issue the next store before the preceding
success acknowledgment. Thus store errors cannot expose later element stores.
Only the oldest command may issue stores, ensuring younger commands cannot
commit memory before an older trap is known. A command with no active lanes
cannot wait for a memory response that was never requested.
Across commands, overlapping byte ranges with at least one write preserve
acceptance order: the older access response is on a strictly earlier clock
edge than the younger request, never the same edge.
The overlap rule compares byte ranges modulo 2^32, including address wrap.
Disjoint reads may overlap and return in any order. The memory model has no
independent external writers or device side effects.

Retirement is strictly in command-acceptance order, exactly once per accepted
command. Its packet atomically commits vector byte enables/data, scalar result,
configuration result, trap/cause and vstart result when retire valid/ready fires.
No other vector/scalar architectural write channel exists. Trapping commands
produce no scalar/configuration write; legal completed lower load elements may
be committed as the defined vector fault prefix. A trap does not silently
discard unrelated accepted commands: the producer may flush them explicitly.
Successful and cancelled retirements have cause=0. Fields gated by a false
write-enable are ignored; vector bytes with false enables never change state.
Cancellation ignores all CSR result fields, including vstart_next; it creates
no architectural CSR write. For a non-cancelled command vstart_next describes
the post-retirement vstart, including fault and unchanged-illegal cases.

## V05. Pressure, flush and progress

Outside flush recovery, with empty slots and no flush/reset, the DUT accepts four independent commands
before their retirement, even when memory responses are delayed. With an
available slot and a held-valid command outside flush recovery, acceptance occurs within four cycles.
For the read-concurrency litmus, accept an aligned load with at least two active
lanes and no older conflicting access; hold mem_req_ready_i=1 and temporarily
withhold responses. Within eight cycles after command acceptance, at least two
distinct read requests must be accepted. Then release the responses, still
meeting each request's 32-cycle deadline. The eight-cycle timeout fails the
litmus; never indefinitely withhold responses to manufacture a deadlock.
An immediately responding BFM is legal in ordinary tests and carries no
requirement to observe two requests before that early response. This directed
capacity test forbids solving the exercise with one blocking transaction slot.
Command, request and
retirement backpressure are independent; response order need not match issue
order. The memory model eventually responds to each request within 32 cycles,
and the environment raises each ready input at least once every eight cycles.
Under those explicit fairness assumptions each non-flushed accepted command
retires within 4096 cycles. Without fairness there is no finite liveness claim.

Synchronous `flush_i` suppresses command acceptance, new memory requests and
normal retirement at that edge. All accepted but not-yet-retired commands are
marked cancelled and subsequently retire once, in order, with `cancelled=1`,
no trap and no register/configuration write. Already acknowledged stores remain
visible; flush is not memory rollback. A store request already accepted by
memory may complete and become visible after flush. Drain its response; do not
pretend the external request was cancelled. Old read responses cannot introduce
later vector/scalar/configuration writes. Accept no
new commands and reuse no affected tag/ID until cancelled records have retired
and their transactions have drained. Repeated flush while draining is idempotent.
If flush stays low after its last asserted edge and no reset intervenes, the
same ready/response fairness bounds require all cancelled retirements and
draining to finish within 4096 cycles of that edge, with cmd_ready_o restored
by that deadline. A held-valid new command then obeys the four-cycle acceptance
bound. Continually reasserted flush carries no finite recovery claim.
Reset may discard cancellation records instead. Test races at flush/request,
flush/response, flush/stalled-retire and reset/drain boundaries explicitly.

## V06. Cadence implementation and acceptance

Implement synthesizable SV with explicit file order, includes and parameters.
No generated Scala/C++ model, upstream generator, DPI oracle or proprietary VIP
is required. External memory and the CSR/operand producer are pure-SV UVM agents.
Use a virtual sequence, passive request/retire monitors, an independent
element/reference-state scoreboard, protocol SVA and requirement-linked
covergroups. Expected values must come from commands and this contract, never
from reading the candidate implementation. Mutations must include a missing
zero-element completion, disabled legality check, dropped masked condition,
wrong fault prefix, duplicate retirement, misassociated response ID and dropped
flush cancellation. Each mutation must create an observable contract violation;
document semantically equivalent/redundant edits as inapplicable negative
controls, rather than treating them as proof of a checker failure.

Freeze functional bins before the first run: operation class; legal/illegal;
SEW; vl={0,1,VLMAX}; vstart={0,vl-1,vl,vl+1}; mask={all,none,sparse};
memory error position; response-order class; outstanding occupancy; flush phase;
tag reuse; and mandatory crosses zero-element x operation class x retirement,
legality x zero-element, and flush x pending memory. Unreachable combinations
need reviewed exclusions before scoring; do not delete hard bins after results.
Require >90% applicable functional bins, all mandatory directed obligations,
zero unexpected UVM_ERROR/UVM_FATAL/SVA failures, and all mutation detections.
Report code/toggle/assertion coverage separately from the functional denominator.

Run Xcelium with Cadence's selected UVM library and use IMC native databases and
reports. Use Jasper Superlint, CDC and FPV for their distinct obligations;
Genus elaboration/synthesis establishes synthesizability, not functional proof.
Single-clock CDC result and all formal assumptions must be explicit. Do not
waive missing constraints or convert a tool launch/exit code into signoff.
The staged kit provides `eda/cadence_runner.py` inspect/plan/run and
`eda/verification_profile.json`; pass that profile to `dv_generate_uvm`.
The generated candidate and TB file inventory belongs in `eda/project.json`.
Record tool versions, actual commands, source/constraint/model hashes, seeds,
logs and raw coverage databases. Cadence tools/licenses are supplied by the EDA
host. This source package has not been compiled, simulated or proven there.

## Exact interface assets

These definitions are the complete authored ABI; they are included in source contexts.

### contracts/coral_vector_contract_pkg.sv

```systemverilog
// SPDX-License-Identifier: Apache-2.0
// Authored interface contract only; no implementation or verification oracle.
package coral_vector_contract_pkg;
  localparam int XLEN = 32;
  localparam int VLEN = 128;
  localparam int COMMAND_SLOTS = 4;
  localparam int READ_SLOTS = 4;
  typedef enum logic [3:0] {
    OP_ADD = 4'h0, OP_SUB = 4'h1, OP_XOR = 4'h2,
    OP_LOAD = 4'h3, OP_STORE = 4'h4,
    OP_STRIDED_LOAD = 4'h5, OP_STRIDED_STORE = 4'h6,
    OP_WHOLE_LOAD = 4'h7, OP_WHOLE_STORE = 4'h8,
    OP_MOVE_X_S = 4'h9, OP_MOVE_S_X = 4'ha, OP_CONFIG = 4'hb
  } operation_e;
  typedef struct packed {
    logic [3:0] op;
    logic [7:0] tag;
    logic [31:0] pc;
    logic [4:0] vl;
    logic [4:0] vstart;
    logic [1:0] sew;
    logic vill;
    logic masked;
    logic [15:0] mask;
    logic [31:0] base;
    logic signed [31:0] stride;
    logic [31:0] scalar;
    logic [127:0] vs1;
    logic [127:0] vs2;
    logic [127:0] old_vd;
  } vector_command_t;
  typedef struct packed {
    logic [7:0] id;
    logic [7:0] tag;
    logic [4:0] element;
    logic write;
    logic [31:0] address;
    logic [1:0] size;
    logic [31:0] data;
  } vector_memory_request_t;
  typedef struct packed {
    logic [7:0] id;
    logic error;
    logic [31:0] data;
  } vector_memory_response_t;
  typedef struct packed {
    logic [7:0] tag;
    logic [31:0] pc;
    logic cancelled;
    logic trap;
    logic [4:0] cause;
    logic [4:0] vstart_next;
    logic [15:0] vector_we;
    logic [127:0] vector_data;
    logic scalar_we;
    logic [31:0] scalar_data;
    logic config_we;
    logic [4:0] vl_next;
    logic [1:0] sew_next;
    logic vill_next;
  } vector_retirement_t;
endpackage
```

### contracts/coral_vector_ports.svh

```systemverilog
// SPDX-License-Identifier: Apache-2.0
// Include inside module coral_vector_retirement_subsystem(...).
input  logic clk_i,
input  logic rst_ni,
input  logic flush_i,
input  logic cmd_valid_i,
output logic cmd_ready_o,
input  coral_vector_contract_pkg::vector_command_t cmd_i,
output logic mem_req_valid_o,
input  logic mem_req_ready_i,
output coral_vector_contract_pkg::vector_memory_request_t mem_req_o,
input  logic mem_rsp_valid_i,
output logic mem_rsp_ready_o,
input  coral_vector_contract_pkg::vector_memory_response_t mem_rsp_i,
output logic retire_valid_o,
input  logic retire_ready_i,
output coral_vector_contract_pkg::vector_retirement_t retire_o
```
