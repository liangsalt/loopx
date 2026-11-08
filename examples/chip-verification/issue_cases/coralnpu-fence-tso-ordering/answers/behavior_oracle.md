# Source-traceable behavioral answer: memory ordering subsystem

This authored answer applies to `coral_memory_order_subsystem` and the input's
`case-cadence-contract`, M01-M07. The immutable supporting ISA source is
`riscv-riscv-isa-manual--src--rv32-adoc`, especially its load/store and memory
ordering descriptions. Its FENCE.TSO discussion permits a stronger FENCE RW,RW
implementation. The authored request/retirement ports, four-slot profile,
single-clock timing and response-edge memory model are explicit exercise
choices. The historical Scala decoder and patch are not this subsystem's
implementation or independent expected-result model.

## Independent expected-state algorithm (M02-M05)

Decode each accepted instruction independently from raw fields. Snapshot its
tag, PC and supplied operand values; compute signed immediates, effective
address, width and scalar result. A memory operation with rd=x0 is still an
operation, including its possible faults. Queue one retirement expectation per
accepted command; reject requests from illegal or misaligned instructions.

Maintain a memory-operation observation table keyed by accepted command tag
and sequence index, with the candidate's request ID assigned only after
validating the request. The byte-memory BFM updates successful stores and
samples loads at response transfer. The architectural scoreboard sign/zero
extends according to the decoded instruction, withholds scalar writeback until
ordered retirement and checks no effect on errors. Separately construct the
required earlier-to-later order edges from every fence's pred/succ masks and
the authored overlapping-byte rule. These edges are independent of the DUT's
implementation of fences or queues.

A selected predecessor response must occur on a strictly earlier edge than
the successor's request. Checking response order alone is insufficient: a
successor request might already have escaped. A no-op fence mutant is exposed
using two loads to disjoint addresses around the fence while the older load
response is delayed. For R-to-W and W-to-W, other serialization may already
enforce the edge; verify observable correctness rather than requiring a
particular internal fence register. Stronger legal ordering must pass.

Flush cancels the accepted unretired suffix, requiring one ordered cancellation
per record, no scalar write and draining of old IDs. A store accepted before
flush may still complete after it; flush cannot roll back external memory.
Block tag reuse/new commands until cancelled records and old transactions have
cleared. Reset drops both DUT records and the BFM's prior response epoch.

## Concrete instruction and data witnesses

Hex values below are 32-bit words unless stated. All successful rows have
trap=0, cause=0, tval=0. Each row requires one retirement with the exact accepted
tag/PC/instruction; data fields with rd_we=0 are ignored.

| Source | Instruction / stimulus | Required result |
|---|---|---|
| M03 | FFF08293 = ADDI x5,x1,-1, rs1=0 | rd=5, rd_we=1, rd_data=FFFFFFFF; no memory request |
| M03 | 002082B3 = ADD x5,x1,x2, rs1=FFFFFFFF, rs2=2 | rd_data=00000001; no memory request |
| M03/M04 | 00008283 = LB x5,0(x1), byte response=80 | rd_data=FFFFFF80 |
| M03/M04 | 0000C283 = LBU x5,0(x1), byte response=80 | rd_data=00000080 |
| M03/M04 | 00009283 = LH x5,0(x1), aligned response=8001 | rd_data=FFFF8001 |
| M03 | 0000A003 = LW x0,0(x1), aligned successful response | A real word request, rd_we=0, successful retirement |
| M03 | 0000A003, rs1=102 | No request; trap cause=4, tval=00000102, rd_we=0 |
| M03/M04 | 00209023 = SH x2,0(x1), rs1=100, rs2=1234BEEF | Request address=100, size=1, low data=BEEF; successful response writes bytes [EF,BE], rd_we=0 |
| M03 | 0020A023 = SW x2,0(x1), rs1=102 | No request; trap cause=6, tval=00000102 |
| M03/M04 | Aligned LW request errors at address=100 | Trap cause=5, tval=100, rd_we=0 |
| M03/M04 | Aligned SW request errors at address=100 | Trap cause=7, tval=100; BFM memory unchanged |
| M03/M04 | 8330000F = FENCE.TSO | Legal fence; no memory request of its own and no scalar write; enforces R->R, R->W, W->W |
| M03/M04 | 0330000F = FENCE RW,RW | Legal fence; also enforces W->R |
| M03/M04 | 0000000F = FENCE with empty masks | Legal, no mandatory memory edge; still one ordered retirement |
| M03 | 0000100F = FENCE.I | Unsupported selected extension: cause=2, tval=0000100F, no request/write |
| M03 | 1330000F with unsupported fm=1 | Cause=2, tval=1330000F, no request/write |

## Observable ordering witnesses (M04-M06)

Use disjoint addresses for fence-edge tests, so byte-overlap protection cannot
accidentally make a missing fence appear correct. Accept the predecessor,
fence, and successor in that order. Delay the predecessor's response while
allowing request handshakes. For the selected pair, the successor must not
request until an edge strictly after the predecessor response. Cover R->R,
R->W and W->W for TSO; for ordinary FENCE cover all selected pred/succ pairs.
Back-to-back fences impose the union of their obligations, not a lost last-
writer flag. If a predecessor traps before issuing any request, it contributes
no outstanding memory request, but its ordered trapping retirement still exists.

For TSO with W->R, both outcomes pass: the younger load may issue after the
older store response, or earlier when addresses are disjoint. Absence of an
early issue never loses mandatory coverage. For a normal RW,RW fence that same
early issue fails. Separately use a word store at 0x100 followed by a byte load
at 0x103: the overlap rule requires store response before load request even
without a fence, despite unequal starting addresses.

Accept two independent loads with no fence and hold responses as specified in
the finite M06 litmus. Require both requests within its eight-cycle window,
then return the second response first. The younger scalar result must remain
buffered until the older retirement. Hold retire_ready low to test a stable
retirement packet, then release within the assumed fairness interval. Inject
flush when an older store response is pending: its physical effect may occur
later, but all unretired commands retire cancelled without scalar writes.

## Negative controls and evidence (M07)

Expose illegal TSO decode with the exact word above. Expose fence-as-no-op with
the delayed-load R->R witness. Expose an observable R->W violation by a mutant
that also bypasses any redundant serialization; deleting a redundant internal
fence term alone is not a behavioral fault. Also inject wrong sign extension,
response-ID substitution, duplicate retirement and dropped cancellation. Each
valid negative control must fail a named independent checker; an equivalent
edit is not evidence of a coverage hole.

Acceptance requires actual Xcelium/UVM execution and IMC native functional-bin
coverage above 90% on a frozen applicable denominator, all directed and valid
mutation obligations, no unexpected UVM/SVA errors, Jasper lint/CDC/FPV evidence
and Genus synthesis checks. Keep raw commands, tool/constraint/source hashes,
seeds and native outputs. No such EDA results are claimed by this answer file.
