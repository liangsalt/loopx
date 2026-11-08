# Source-traceable behavioral answer: vector command subsystem

This is an authored answer for `coral_vector_retirement_subsystem`, not an
upstream implementation or an executed simulator result. The normative source
is `case-cadence-contract`, V01-V06, in the input spec. Supporting immutable
ISA rationale is `riscv-riscv-isa-manual--src--v-st-ext-adoc`: headings Vector
Start Index CSR, Vector Type Illegal, Prestart/Active/Inactive/Body/Tail Element
Definitions, Vector Load/Store Whole Register Instructions, and Integer Scalar Move
Instructions. The contract's command ABI, bounded queues, selected exceptions,
single-clock timing and flush protocol are exercise choices, not claims that
those mechanisms are specified by the ISA or match Coral RTL internals.

## Independent expected-state algorithm (V02-V05)

On command acceptance, append an immutable snapshot to an ordered expectation
queue. First classify the operation and legality under V03, including each
special-operation exception. Never use `vl==0` as a legality test. Determine the
active element set from this snapshot; compute arithmetic/address results from
its operands with the specified bit widths. Preserve untouched bytes from
`old_vd`. An empty legal set produces an immediately complete expectation with
no request or register-write obligations and vstart_next=0.

For each memory request, independently match tag, element, expected address,
size and write data against the accepted command. Track the DUT's arbitrary
unique transaction ID after validating that binding; an ID is not the expected
element index. The memory responder independently owns byte storage and applies
successful writes/samples reads at response transfer. The passive scoreboard
records those observations by ID and checks ordering constraints. At retirement
combine successful lower-index load elements up to the first failing active
element; arrival order cannot determine the architectural fault prefix.

Only the head expectation may retire. Compare gated architectural data, exact
byte enables, scalar/configuration enables, tag/PC, trap/cause and vstart.
Unenabled data fields are not compared. Never obtain expected values or
completion directly from internal DUT state. Invalid external response IDs are
environment errors; the verification model must not silently assign them to a
pending command. At flush, mark the accepted suffix cancelled, suppress its
architectural writes and drain outstanding physical transactions; accepted
stores remain capable of completing. Reset starts a new epoch for DUT and BFM.

## Concrete oracle witnesses

Unless stated otherwise: SEW=8, vill=0, mask disabled, vstart=0, no flush/reset,
all responses successful and old_vd contains sixteen bytes of AA. Hex values
below are numeric; listed element/byte arrays run from lowest index upward.
Every row still requires exactly one ordered retirement with matching tag/PC.

| Source | Accepted command / observed responses | Required result |
|---|---|---|
| V03 | ADD, vl=0 | No request; vector_we=0, scalar_we=0, config_we=0; trap=0; vstart_next=0 |
| V03 | STORE, SEW=16, vl=0, base=0x101 | No request and no misalignment trap, because no active lane exists |
| V03 | ADD, vl=0, vill=1, vstart=3 | Illegal cause=2, vstart_next=3; no memory/vector/scalar/config effect |
| V03 | Reserved op=0xF, vl=0 | Illegal cause=2; not a successful empty command |
| V03 | ADD, vl=4, vs1 bytes=[FF,7F,80,01], vs2=[01,01,80,FF] | vector_we=000F, output bytes [00,80,00,00]; remaining bytes unchanged |
| V03 | ADD, vl=4, vstart=2, masked=1, mask=0004 | Only element 2 executes; vector_we=0004 |
| V03 | LOAD, vl=4, vstart=4 | No request, vector_we=0, success, vstart_next=0 |
| V03/V04 | WHOLE_LOAD, vl=0, sew=3, vill=1, mask=0, vstart=14 | Two byte requests base+14/base+15; success vector_we=C000, no trap, vstart_next=0 |
| V03 | MOVE_X_S, vs2[7:0]=80, vl=0, vstart=31 | scalar_we=1, scalar_data=FFFFFF80; vector_we=0; success |
| V03 | MOVE_S_X, SEW=16, vl=0, scalar=DEADBEEF | No vector/scalar write; success |
| V03 | MOVE_S_X, SEW=16, vl=1, scalar=DEADBEEF | vector_we=0003; bytes [EF,BE]; scalar_we=0 |
| V03 | CONFIG, SEW=16, scalar=99 | config_we=1, vl_next=8, sew_next=1, vill_next=0, vstart_next=0 |
| V03 | CONFIG, sew=3 | config_we=1, vl_next=0, sew_next=3, vill_next=1, no trap |
| V04 | LOAD, SEW=16, vl=4, vstart=1; responses: element 3 succeeds=3333, 2 errors, 1 succeeds=1111 | Trap cause=5, vstart_next=2, vector_we=000C; bytes 2/3=[11,11]; higher values uncommitted despite earlier arrival |
| V04 | STRIDED_STORE, SEW=32, vl=3, mask=0005, masked=1, base=100, stride=-4 | Requests at 100 then F8, data elements 0 then 2; second request only after first successful acknowledgement; none for element 1 |
| V04 | STORE, SEW=32, vl=1, base=102 | No request; trap cause=6, vstart_next=0, no vector/scalar/config write |
| V04/V05 | Store request accepted, then flush before response | Later successful response may update BFM memory; one cancelled retirement, no register/CSR/trap effect; no new request; tag cannot be reused until retirement and drain |

For out-of-order response tests, choose both the ID assignment and response
permutation independently. To exercise fault-prefix logic, explicitly withhold
a lower response until a higher success/error has arrived. Compare observed
requests as well as retirement: a correct final value cannot hide masked,
illegal or zero-element memory traffic. Cross-command overlapping accesses with
at least one write must obey the strictly earlier response rule from V04.

## Capacity, mutation and acceptance oracle (V05-V06)

Use the finite two-read litmus in V05: hold the responder only for the specified
window, require two accepted requests by the eight-cycle deadline, then release
responses before their 32-cycle deadlines. An immediately responding normal BFM
does not have to create simultaneous outstanding requests. Fill four command
slots under controlled retirement stalls; exercise ID/tag reuse only after the
defined lifetime ends. Check liveness only under the declared readiness and
response bounds, with reset/flush transitions treated explicitly.

Seed behavioral faults that drop empty completion, bypass legality, ignore a
mask, choose a fault prefix by response arrival, misassociate an ID or retire a
command twice, or drop flush cancellation. Each must trigger an independently mapped checker; document the
stimulus, observation and checker, not only a nonzero simulation exit. An edit
that leaves behavior identical is not a valid negative control.

The answer is accepted only with real candidate Xcelium/UVM results, native IMC
data for a frozen applicable denominator above 90%, all mandatory checks and
mutations, no unexpected UVM/SVA failures, explicit Jasper lint/CDC/FPV evidence
and Genus synthesizability evidence. Those artifacts are not included here.
The immutable historical patch explains the original mechanism but cannot
serve as this subsystem's golden implementation or coverage evidence.
