# Unified HB, HBNOISE and HBXF analysis bundle

Entries A (Design Lab), B (existing Maestro), and C (new cell) call TB Generator's `analysis_bundle()` and `install_bundle()` through `core/testbench.py`. Both `extensions.analysis_bundle` and `extensions.tbgen_spec.analysis_bundle` carry the generated version. TB Generator owns the consistency rules and expression templates.

- Shared settings include oscillator mode, fguess, differential nodes, HB order, ISF N, sidebands, exact offsets, relharmvec, freqaxis, solver and IC, temperature, and variables.
- Entry B reads enabled and disabled analyses from the saved setup and aligns them in a new copy of the source test. The transaction protects source analyses, outputs, variables, corners, models, saves, and run settings. One-tier solver settings can be inherited or supplied via `analysis.solver_options`.
- Review shows the before value, after value and reason for each aligned field. Multiple tones or unresolved order or sidebands block materialization pending review.
- HBNOISE and HBXF share offsets and sidebands with a minimum of N+1. The auxiliary sample at twice a single requested offset preserves IC618 behavior. Only the requested offsets are reported by ISF.
- Confirmed DC supplies produce current, power and FoM outputs. Entry B identifies explicit DC voltage sources and supply nets. Supply bindings with nonstandard names require an explicit description.
- The preparation page summarizes shared parameters and outputs. The review shows shared and analysis-specific fields, inherited settings, and all output definitions.

TB Generator's `vco_tbgen/output_templates.py` owns the generated expressions. Scalar outputs are required. HBXF returns a required complex raw family with native harmonic, offset, magnitude and phase. The plan adds a zero-DC `ISF_TB_INJECT` current probe at the confirmed gm D/S terminals in a stimulus specific to the new test.

```text
ISF_HBXF_RAW_INJECTION = getData("/ISF_TB_INJECT" ?result "hbxf")
```

The transfer has units V/A. The native check requires every `harmonic = k−1` for `−N ≤ k ≤ N`, including −1, 0 and +1. A V/V supply transfer fails this requirement. Missing topology or D/S evidence and probe-name conflicts require review. The worker forwards the shared sidebands as `engine.main_max_sideband` for every value of N.

The IC618 `hb_fd` convention is single-sided peak. Differential fundamental amplitude is `abs(Vp(f0)-Vn(f0))`. Read total PM from `getData("out" ?result "hbnoise_pm")` and apply the existing ISF PM-SSB convention. Signed FoM is `PN−20log10(f0/offset)+10log10(Pdc/mW)`.

`core/maestro_outputs.py` attaches verified RDB values by matching corner and every operating parameter. Equivalent scalar representations such as `200p` and `2e-10` match. Ambiguous or absent values remain unknown. The results API may reattach outputs while reading a saved record. The results view displays native f0, native HBnoise PN, and its difference from the ISF whole-VCO reference.

Verify the native oscillator frequency and PM-SSB phase noise against the
owned ISF replay at the same sample. For the current probe, compare D/S nodes,
orientation and complex transfer at each required harmonic.

A/C write shared temperature to `simulator_options` and synchronize each register code to one variable value. B inherits temperature, variables and IC from the source test. Overrides that cannot be represented by the transaction stay pending. The TB spec is complete before `install_bundle()` validates the native temperature, variables, IC, corners and sweeps. Copy, hashes and preservation checks protect inherited settings. Regressions cover N=3 with 4, 10, 15 and 19 sidebands and reject V/V or incomplete required raw output.
