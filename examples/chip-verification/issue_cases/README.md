# CoralNPU issue-derived design and UVM exercises

Two real public issue observations anchor these cases: [zero-element vector retirement #89](https://github.com/google-coral/coralnpu/issues/89) and [FENCE.TSO legality #130](https://github.com/google-coral/coralnpu/issues/130). Each has an immutable affected repair candidate and a separately packaged upstream correction. These are authored system integration exercises; neither is an official upstream 500KiB specification.

From `examples/chip-verification`:

```text
python coral_issue_cases.py list
python coral_issue_cases.py validate
python coral_issue_cases.py stage coralnpu-vector-zero-retirement /new/workspace/input
python coral_issue_cases.py stage coralnpu-fence-tso-ordering /another/workspace/input
```

The export must use a new destination and contains only source input, scope policy, profile, shared module graph, licenses and original baseline repair seeds. Fixed implementation, patch and regression answers stay under `answers/`. A repair seed is not a standalone compilable design; the input profile identifies the full pinned repository and dependency gaps. Spec-first entry targets the affected boundary integrated with existing IP, not reconstruction of an entire NPU.

Every spec.json exceeds 512KiB, as do unique original document bodies after excluding authored material, licenses, code seeds and duplicate bodies. This measures retained source context, including explicitly unsupported/background ISA sections. It does not measure applicable normative requirement bytes, complete semantic specification or coverage. The compact scope policy targets every source range and must accompany fragmented context. Keep independent original-source and policy citations.

Source bodies are byte-preserved from pinned Google CoralNPU (Apache-2.0) and RISC-V ISA (CC-BY-4.0) repositories; licenses, exact URLs, revisions, Git blob IDs and SHA-256 hashes are retained. The committed manifest is offline-checkable. Issue observations are paraphrased and dated; tracker state is not proof of reproduction or repair.

No simulator, UVM run, reference model or EDA coverage was executed to qualify these fixtures. More than 90% functional coverage is an acceptance target requiring a frozen applicable-bin plan and actual evidence on the EDA machine. The validator reports source integrity, never fabricated hardware coverage.
