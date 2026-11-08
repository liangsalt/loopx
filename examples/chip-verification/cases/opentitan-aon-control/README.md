# opentitan-aon-control

Public specification-only design/DV problem from [OpenTitan](https://github.com/lowRISC/opentitan/tree/c69eaea24a57c581ed274779887cb4a0c1405e3b), fixed at `c69eaea24a57c581ed274779887cb4a0c1405e3b`.

- In-scope IPs: pwrmgr, rstmgr, clkmgr, sysrst_ctrl, pinmux, alert_handler.
- Unique credited original specification bodies: **592,589 bytes**, above 512 KiB. JSON wrapping, authored contracts, register HJSON and system context are excluded from this count.
- [Problem and required outputs](input/problem.md), [input specification](input/spec.json), [source manifest](input/source_manifest.json).
- Evaluator-only [answer guide](answer_guide.md) and [obligations](obligations.json); actual unchanged [reference source inventory](../reference/opentitan-c69eaea/manifest.json).

Stage only `input/` into a separate execution workspace using the case tool. Opening this entire repository gives the host access to the reference answers. Exporting input files does not enforce network or filesystem isolation.

Reference material is upstream RTL, UVM/FPV, testplans and build metadata, not a generated candidate or a unique textual answer. The stored original system top-level provides integration context; it is not the authored reduced-subsystem wrapper requested by this exercise. Shared/external dependencies are not a proven complete closure. These stored files have not been qualified as a runnable subsystem with Vivado. Corpus integrity, actual host execution, functional correctness and measured coverage are separate results. Pinmux lacks an upstream UVM simulation environment at this revision; available formal material is not a replacement for one.

Original OpenTitan files retain their Apache-2.0 license and notices. Exercise scope, profile, graph and evaluator obligations are explicitly authored fixture material. Preserve [LICENSE](input/licenses/LICENSE) and [NOTICE](input/licenses/NOTICE) when exporting the input.
