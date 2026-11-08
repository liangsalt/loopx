# Cadence execution kit

This exercise is solved with ordinary SystemVerilog RTL, SV UVM 1.2, SVA and
Cadence tools. Python only stages files and records processes; it is not a DUT
simulator or an architectural reference model. No Bazel, Chisel/firtool,
FuseSoC/DVSim, Verilator, MPACT, Cocotb or upstream software build is required.
Original repository material is provenance, not an executable dependency or an
independent golden model for the revised exercise.

The input package deliberately contains contracts, not a completed DUT or TB.
An empty example file list does not compile and cannot earn verification credit.
Produce the specified RTL and independent UVM/checkers first. Existing IP is
permitted only with its complete, versioned `.sv/.v/.svh` closure, configuration,
license and interface contract; unresolved black boxes and behavioral stubs are
not implementations.

## LoopX and native execution

Import `spec.json` normally. Both design and DV use the same authored interface
contract. For UVM generation supply `profile_path="eda/verification_profile.json"`;
`integration_profile.json` is the exercise scope, not the generation profile.
The generated build manifest must select `simulator="cadence_xcelium"` and
`execution="external"`. Its `top` is the TB wrapper; `dut_top` is the design.
Use Cadence's UVM library, not `UVM_NO_DPI` or copied UVM implementation files.

The installed LoopX local design-check/join backend remains a different execution
path. Do not invoke a Vivado/XSim job as proof of this exercise. `build-uvm` for a
Cadence manifest returns the external execution handoff. Run the kit on the EDA
host and retain its artifacts; events and Watchdog can observe that run through
the existing register-run/ingest-event interface. Neither an event `completed`
nor this kit's receipt changes task acceptance or proves coverage.

From the exported workspace, after generating a complete manifest:

```console
python eda/cadence_runner.py inspect --project candidate/build.json
python eda/cadence_runner.py plan --project candidate/build.json --output runs/plan-001
python eda/cadence_runner.py run --project candidate/build.json --output runs/seed-001 --execute
```

`run` without `--execute` only prepares a plan. Every output directory must be
new; logs and coverage from previous runs are never overwritten. `candidate_dir`
is relative to the manifest. List every translation unit exactly once and in
dependency order in `compile_order`. Declare headers using `include_dirs`, and
declare memory images/other runtime files in `extra_inputs`. Literal includes are
checked recursively. Macro-generated includes and candidate DPI are outside this
portable profile. Native elaboration still owns package/module/type correctness.
Literal `$readmemh`/`$readmemb` image paths must be portable relative paths. The
kit copies those declared images into the new run directory at the same paths
and checks their hashes; paths colliding with run outputs are rejected.

The Xcelium recipe uses `xrun -64bit -sv -uvm -uvmhome CDNS-1.2 -f files.f`,
`-top`, `-timescale`, `-svseed`, `-coverage A` and `+UVM_TESTNAME`.
Each run uses its own working directory, preserving the native coverage database
there. No coverage-reset Tcl or `-covoverwrite` is injected. Confirm the switches
with the target installation's `xrun -helpall` before qualifying that version.
The kit records the version query, exact command, input hashes and tool output.

Print `LOOPX_ACTIVITY checks=N transactions=N` from **actual** scoreboard and
monitor counters at end of test. Zero exit status alone is insufficient: a clean
UVM ERROR/FATAL summary and nonzero comparison/transaction activity are required
for the kit's simulation observation. `activity_contract.minimums` can require
higher counts; its pattern must use the documented marker. These counters are
not coverage and do not prove that the checker is correct.

## Lint, CDC, formal, synthesis and coverage

Supply target-version native Tcl under `scripts` for `lint`, `cdc`, `formal`,
`synthesis` and `coverage`, and declare SDC/reset assumptions in `constraints`.
Select Jasper Superlint, Jasper CDC, Jasper FPV, Genus and IMC respectively.
Genus elaboration/synthesis is not a substitute for Jasper CDC or functional
verification. Mapping/PPA requires a licensed process library on the tool host;
it is not needed to express or simulate the digital exercise.

Use `--site eda/site.json` for installation-specific executables and launch flags:

```json
{
  "tools": {"xrun": "xrun", "imc": "imc", "jg": "jg", "genus": "genus"},
  "version_args": {"xrun": ["-version"]},
  "launch_flags": {}
}
```

Populate `launch_flags` for each non-simulation stage from the installed tool's
documented batch invocation, with exactly one `{script}` placeholder. For
example, qualify your IMC batch/input flags before adding them for `coverage`.
The kit intentionally reports `site_recipe_required` when the native recipe is
absent; it does not invent a CDC command or accept an empty Tcl file as clean.
Run a selected stage with `--stage cdc` (or another listed stage).
Design stages (`lint`, `cdc`, `formal`, `synthesis`) inspect only the RTL closure,
constraints and supplied recipes. They can run before a TB or functional
coverage model exists. Their plans explicitly mark unvalidated verification
fields; passing a design-stage file check cannot qualify a later simulation.

Recipes receive `LOOPX_RTL_FILELIST`, `LOOPX_DUT_TOP`, `LOOPX_CANDIDATE_ROOT`,
`LOOPX_RUN_DIR` and `LOOPX_COVERAGE_MODEL` in their environment. They must produce
their native reports and databases under the run directory. Declare any Tcl
subscripts/libraries as `extra_inputs`; preserve their exact bytes and the tool
version. Capture clocks, resets, crossings, formal assumptions, black boxes,
waivers, unproven properties and counterexamples; a zero process exit code is
only a process observation.

## Frozen acceptance evidence

`coverage_model` names a reviewed JSON contract with `schema_version: 1`,
`metric: "functional"`, `target_percent: 90`, and a nonempty `bins` array. Each
bin has a stable `id` and nonempty `requirement_ids`; specify sampling, expected
hits and any reviewed exclusions alongside them. The question's required
obligations must be represented before freezing the model. Freezing an incomplete
model does not make its denominator acceptable.

For every native run retain RTL/TB/constraints/model hashes, seed, command,
version, logs, native coverage DB and reports. IMC merge inputs must have the
same design and frozen coverage model; report merge errors and unmerged runs.
Keep functional, code/toggle/assertion coverage and formal results separate.
The kit does **not** parse an arbitrary percentage as accepted coverage. A real
IMC per-bin report and the frozen denominator must support a 90% claim. Assertions,
wrong architectural results and deadlocks fail independently of that percentage.

The kit can be validated without licenses for file integrity, command generation,
process receipts and rejection of incomplete inputs. Such checks are not native
compilation, simulation, CDC, FPV or coverage qualification. The target machine
must perform those runs. No packaged question carries a fabricated passing EDA
receipt.

## Public command and product references

- [Xcelium](https://www.cadence.com/en_US/home/tools/system-design-and-verification/simulation-and-testbench-verification/xcelium-simulator.html)
- [Native UVM library selection](https://community.cadence.com/cadence_technology_forums/f/functional-verification/46950/xmvlog-e-nopbind-src-soc_tb_top-sv-46-20-package-soc_test_pkg-could-not-be-bound/1369925)
- [UVM test selection](https://community.cadence.com/cadence_technology_forums/f/functional-verification/51860/error-happen-when-using-debugging-uvm-with-simvision)
- [Instrumentation and coverage dumping](https://community.cadence.com/cadence_technology_forums/f/functional-verification/62425/xcelium-dump-coverage-information-in-the-middle-of-a-simulation)
- [IMC merge example](https://community.cadence.com/cadence_technology_forums/f/functional-verification/20269/how-to-merge-the-coverage-report-using-imc)
- [Jasper CDC](https://www.cadence.com/en_US/home/tools/system-design-and-verification/formal-and-static-verification/jasper-verification-platform/jaspergold-clock-domain-crossing-app.html)
- [Jasper formal apps](https://www.cadence.com/en_US/home/tools/system-design-and-verification/formal-and-static-verification/jasper-core-formal-apps.html)
- [Genus](https://www.cadence.com/en_US/home/resources/datasheets/genus-synthesis-solution-ds.html)

Public forum examples establish available mechanisms, not qualification of an
unobserved installation/version. Use the native installed help for final recipes.
