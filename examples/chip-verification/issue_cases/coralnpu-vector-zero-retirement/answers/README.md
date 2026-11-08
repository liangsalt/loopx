# Reference answer and evidence boundary

The upstream correction removes zero-element legality gating from backend ALU/LSU decode, allowing legal commands to advance and retire. Other instruction legality checks remain. This patch is not a general license to turn every vl=0 encoding into a NOP.

Issue: https://github.com/google-coral/coralnpu/issues/89
Observed 2026-11-08: issue remains open. An open tracker state does not imply no upstream correction exists.
Candidate/fix parent: `c0b98e39b995174fa052c3dd248bd540d120e96f`
Upstream correction: https://github.com/google-coral/coralnpu/commit/5c599a884fb07b25f12be6cd027c12e93a99de45

`reference/` contains original changed files at that immutable correction, not a complete simulator checkout. `upstream.patch` preserves its source hunks. `review_rubric.json` extends the original regression to system-level requirements and UVM methodology; it is our acceptance proposal, not a claim of an upstream approved test plan.

In a complete pinned checkout, use a separate copy of the parent, apply the patch, and run matched baseline/fixed regressions. Upstream names these targets:

```text
//tests/cocotb:rvv_assembly_cocotb_test_core_mini_rvv_vl0_v0_corruption_test
//tests/cocotb:rvv_load_store_test_whole_reg_repro
```

Offline validation applied the patch to original parent files and all 8 changed files matched the fixed-revision Git blobs. This validates patch lineage only. These simulator commands have NOT been run locally for this package. Testbench generation, elaboration, actual baseline failure/fixed success, EDA functional/code coverage and >90% acceptance remain unqualified. The package validator checks source integrity and export boundaries only.

Source license: Apache-2.0; retained in LICENSE. Issue descriptions in this package are short authored paraphrases with attribution links; no assumption is made that issue comments carry the repository license. RISC-V source documents and their CC-BY-4.0 license remain in the input with original attribution and immutable URLs.
