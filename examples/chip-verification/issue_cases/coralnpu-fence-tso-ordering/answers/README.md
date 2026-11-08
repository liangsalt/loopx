# Reference answer and evidence boundary

The upstream correction adds the exact FENCE.TSO decode entry, carries its decoded flag, and routes it through fence dispatch. Serializing it as the existing full fence is permitted by the ISA. This upstream test primarily covers legality; the authored ordering/backpressure rubric requires additional independent tests.

Issue: https://github.com/google-coral/coralnpu/issues/130
Observed 2026-11-08: issue remains open. An open tracker state does not imply no upstream correction exists.
Candidate/fix parent: `08eadd1caede64ac4ee99dbbfc9cc70b74c2c5ca`
Upstream correction: https://github.com/google-coral/coralnpu/commit/40be297c9472a24a7baba3c6bdee6578e5e33407

`reference/` contains original changed files at that immutable correction, not a complete simulator checkout. `upstream.patch` preserves its source hunks. `review_rubric.json` extends the original regression to system-level requirements and UVM methodology; it is our acceptance proposal, not a claim of an upstream approved test plan.

In a complete pinned checkout, use a separate copy of the parent, apply the patch, and run matched baseline/fixed regressions. Upstream names these targets:

```text
//hdl/chisel/src/coralnpu:coralnpu_decode_tests
//tests/cocotb:core_mini_axi_sim_cocotb_core_mini_axi_coralnpu_isa_test
//tests/cocotb:rvv_core_mini_axi_sim_cocotb_core_mini_axi_coralnpu_isa_test
```

Offline validation applied the patch to original parent files and all 4 changed files matched the fixed-revision Git blobs. This validates patch lineage only. These simulator commands have NOT been run locally for this package. Testbench generation, elaboration, actual baseline failure/fixed success, EDA functional/code coverage and >90% acceptance remain unqualified. The package validator checks source integrity and export boundaries only.

Source license: Apache-2.0; retained in LICENSE. Issue descriptions in this package are short authored paraphrases with attribution links; no assumption is made that issue comments carry the repository license. RISC-V source documents and their CC-BY-4.0 license remain in the input with original attribution and immutable URLs.
