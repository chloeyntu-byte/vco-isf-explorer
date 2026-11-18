# Independent ISF optimization API 1.0.0

`isf_tool.optim` is an optional, headless evaluator for simulated or imported results. Python 3.11 and the
existing analysis extra (NumPy) are required. No new runtime installation is
needed. `isf_tool/schemas/vco.isf-optim.v1.schema.json` publishes the JSON contract;
`isf_tool.optim.schema.validate(value, definition)` validates its used subset
without a third-party JSON Schema dependency. Definitions are `DesignRef`,
`Evaluation`, `Calibration`, `Gate`, and `Ranking`. `api_version` is `1.0.0`.
Breaking field/unit/semantic changes require a new major schema/version. L0 validates the calibration schema and API version before creating a workspace or acquiring a solver slot. Missing or incompatible versions and boolean calibration signs are rejected. `calibrate()` validates its exported contract as well.

```python
from isf_tool.optim import evaluate, calibrate, rank_knobs, gate

cal = calibrate({'kind': 'artifact', 'result_path': 'isf-result.json',
                 'point': 'selected-point', 'offset_hz': 100000,
                 'identity': 'family-pdk-geometry-key'})
result = evaluate({'kind': 'hb_psf', 'psf_dir': 'hb-ascii/psf',
                   'nodes': {'output_p': 'OUTP', 'output_n': 'OUTN',
                             'gate': 'OUTN', 'drain': 'OUTP',
                             'source': '0', 'bulk': '0'},
                   'identity': cal['identity']},
                  level='L0', calibration=cal, offsets_hz=(1e5,))
decision = gate(result)
```

## Entry references and execution ownership

`evaluate(design_ref, *, level='L0', calibration=None, offsets_hz=(1e5,))`
returns an evaluation envelope with `level`, exact `offsets_hz`, one entry in
`metrics` per offset, `cost`, `reference`, `gate`, and `signoff=false`.
Invalid/ambiguous inputs raise `ValueError`; acquisition failures raise
`RuntimeError` with an evidence directory. CLI errors are JSON with exit code 2.

A reference is a dict or JSON `vco.isf-design-ref.v1` file. A directory can
contain `optim-ref.json`; sidecar paths should be absolute (they are not resolved
relative to the sidecar). Unambiguous generated directories, result directories,
project JSON files, native ISF spec JSON files, and HB PSF directories are also
recognized. Multiple points/offsets are never silently selected as “latest”.
Supply `point` for a multi-point artifact. An existing Maestro path requires a
sidecar or explicit dict identifying `test` and completed `history`.

| Kind | Required acquisition input | Supported use |
|---|---|---|
| `artifact` | `result_path` or inline `result`, optional `point` | L1 actual whole-VCO PM-SSB and L2 qualified flicker+thermal from canonical results |
| `hb_psf` | `psf_dir`, `nodes`, calibration and DC LUT for L0 | Read-only ASCII HB L0; ASCII HB + HBnoise PM L1 |
| `design_lab` | `project_path` or `generated_dir`, `workspace`, `design_lab_root` | New private HB at L0; HB+HBnoise at L1; complete ISF at L2 |
| `native_spec` | Completed `spec_path`, owned `workspace` | L2 through the existing native API; rejects source Maestro augmentation |
| `maestro` | `maestro_dir`, `test`, `history`, `workspace`, optional config/mapping overrides | L2 private replay of completed history; does not edit the source setup |

For Maestro L0/L1, supply its exported `psf_dir` in the reference; this reads
existing data. Direct binary PSF interpretation is deliberately not guessed:
export a waveform or ASCII PSF using the site-qualified reader, or use the
existing L2 engine. An `hb_waveform` dict can replace ASCII HB at L0:
`{theta_rad, vdiff_v, f0_hz}`, optionally `vd_v`, `vgs_v`, `vds_v`, `vbs_v`.
All arrays are finite equal-length closed 2π grids. `alpha_dc` is a matching
A/√Hz modulation array. Preserve the engine's signed, harmonic-reconstructed modulation; do not clamp it to a raw ASD. An HB voltage waveform alone cannot supply noise.

Live Design Lab L2 supports two backends. The default `backend='native'`
uses Design Lab's owned DUT export and ISF prepare/run/results through TB
Generator. `tbgen_root` can select that external checkout. Native assets and
solver outputs are relocated beneath the evaluation workspace; source Maestro
augmentation through `native_spec` is rejected. A new native `create_new`
analysis bundle is rebuilt through TB Generator's public `analysis_bundle` /
`install_bundle` after ISF changes offsets, retaining required qualified metrics.

`backend='private_netlist'` instead freezes a generated directory into the
owned workspace and calls the **same bundled ISF engine** and canonical exporter.
It needs explicit `gm`, `psf` executable and optional `peer`. It validates source
and model hashes, detected terminal/model bindings, and measured pair symmetry.
It writes only to the owned workspace and accepts one offset. Both backends
preserve the bundled engine's numerical-quality evidence. The default pair
calculation requires proven symmetry. Use explicit two-device analysis for
complementary or asymmetric cores. L2 requires thermal evidence and reports
the source-closure gate independently of execution success.

Every live evaluation needs an explicit owned `workspace`; it creates a new
`optim-<uuid>` child. L0/L1, Maestro private replay, and generated private L2
can use the optional `VCO_ISF_COMMAND_TEMPLATE` resource wrapper with
`sim:slots`. Native L2 delegates resource coordination to the native
and ISF stages so a parent does not hold a competing resource. Workers check root free space before acquiring results and refuse
below 1.5 GiB. Native library writes remain confined to the owned workspace.

## Fidelity, calibration and gate

The verified scope covers **Spectre 18.1**, the selected gm devices, and
supported passive LC thermal loss. Use L0 for ranking and diagnostics when
you already have HB and a suitable calibration.

- L0 returns `rho_hat`, `c0_hat`, waveform slope/edge diagnostics and single-ended
  ψ2/ψ3. It returns no PN. Γ̂ is the calibrated negative periodic derivative of
  differential voltage, with ω0²/Vpk² scaling; alpha is the DC modulation with
  transferred harmonic δ_k/A_k. It is a ranking hypothesis, not signoff.
- L1 returns measured `pn_dbc_hz` at **exact** requested offsets from Spectre
  HBnoise PM-SSB. Missing frequencies are errors; no interpolation or extrapolation.
  For Design Lab's standard generator only its configured 10k/100k/1M/10M samples
  are available. Partial source lists cannot establish a modeled closure gap.
- L2 returns `vco.isf-metrics.v1` diagnostics with qualified flicker and thermal,
  plus `closure.pair_error_db`. The latter is predicted minus measured **gm-pair
  flicker** PN in dB; thermal has separate engine validation. Inspect those
  numerical, AM/PM and HBAC diagnostics before adopting any physical conclusion.

`calibrate(design_ref)` selects exactly one measured workpoint/offset and calls
`isf_tool.metrics.calibrate`. `offset_hz` (or a singleton `offsets_hz`) selects
the same offset for live acquisition and calibration; conflicting declarations
or multiple calibration offsets fail before simulation. Without an explicit
offset, live calibration defaults to 100 kHz; artifact selection remains exact
and must be unambiguous. It returns δ_k (degrees), A_k (linear), Γ scale/sign,
provenance and a geometry identity. The harmonic calibration itself uses flicker;
it cannot assert thermal or pair closure. A complete L2 run is required to open
the optimization gate. Where full VG/VD/VS/VB and m_DC exist, it also exports
`device_lut` rows `[VGS,VDS,VBS,alpha_dc]`. This is an **empirical trajectory LUT**,
not a general PDK grid: interpolation is normalized inverse-distance over four
neighbors, with no axis extrapolation (1 mV numerical tolerance) and maximum
nearest-neighbor normalized distance 0.25. Out-of-domain candidates must acquire
a wider source LUT or recalibrate; never replace m_DC by |I_D| without saying so.
The calibration offset must exactly match L0. An explicit identity must match;
a caller that omits an identity must manage applicability itself. Family, PDK,
model, core geometry or large parameter changes require renewed L2 validation.

`gate(metrics)` returns `{isf_shaping_allowed, reason, ...}`. Numerical quality
and PM projection validity must both be explicitly `true`; missing/null flags
close the gate. Finite rho_dyn, c0_eff, coherent harmonic Qk projections, qualified
thermal evidence and measured/modeled HBnoise closure references are required.
Harmonic rows must contain each integer order `0..max_harmonic` exactly once,
with finite real and imaginary Qk. `design_ref.max_harmonic` declares that range
(default 3, supported 1–12); it is never inferred from the remaining rows.
Imported artifacts with a different range must declare it explicitly. The range
is retained as `metrics.source.max_harmonic` so a standalone gate also checks
that `flicker.c_k` covers it. Duplicate rows fail before their keys can be lost
in the projection map.
Incomplete flicker or thermal evidence is rejected as L2 (L1 can retain partial
diagnostics with a closed gate). Reasons include `ISF_NUMERICAL_QUALITY_UNQUALIFIED`,
`ISF_PM_PROJECTION_UNQUALIFIED`, `ISF_FLICKER_RHO_DYN_UNAVAILABLE`,
`ISF_FLICKER_C0_EFF_UNAVAILABLE`, `ISF_HARMONIC_Q_UNAVAILABLE`,
`ISF_HARMONIC_ORDERS_INCOMPLETE`, `ISF_HARMONIC_ORDER_DUPLICATE`,
`ISF_HARMONIC_ORDER_INVALID`, `ISF_HARMONIC_RANGE_INVALID`,
`ISF_PAIR_REFERENCE_UNAVAILABLE`, `ISF_THERMAL_UNAVAILABLE` and
`ISF_HBNOISE_REFERENCE_UNAVAILABLE`. Unknown G/pair error, |pair error| ≥0.5 dB,
G>3 dB or overprediction G<−0.5 dB also closes it.
The aggregate gate opens only if every requested offset opens. G is measured
whole-VCO total minus the actually modeled flicker+thermal power sum. A closed
gate permits source attribution, not waveform-shaping adoption. Pair closure
compares identical source scope; G deliberately measures a different total scope.
L1 imported from canonical ISF data retains the engine quality flags. A partial
flicker-only artifact explicitly closes the shaping gate even when its apparent
gap is small; a measured PN remains usable as L1 evidence.

`rank_knobs(metrics, recipe)` accepts one L2 metrics entry (or one-offset evaluation)
and a Design Lab topology/recipe. It maps the existing diagnostics to the actual
`two_f.step`/`knobs` and `power_knob`: aux code for `aux_per_code`, explicit ratio
knobs for `ratio`, and the actual `two_f.vars` component names (LH1, LH2, KH1H2,
CH in Guo's recipe) for `cmr_z11`. Core L is never suggested. When
gated, it returns source attribution only. Conditional thermal benefit P_j uses
the metrics module's measured source share and overlap-target estimate. Known
P_j values sort descending; unavailable benefits remain `null` and sort after
known values. Names break ties, not physical optimality. Flicker benefits and
search directions are not invented from harmonic projection coefficients.
Each suggestion includes evidence, mechanism/basis, and L1/loopgain requirements.

## Cost and downstream contracts

`cost.spectre_calls` counts **new Spectre process invocations**, not analyses,
harmonics, phase samples, Virtuoso processes, or candidate count. Acquisition
receipts and unique execution-directory paths are retained in
`spectre-call-directories.json`. Failures retain evidence as well. Importing an
existing result costs zero new calls and sets `reused=true`;
`acquisition_spectre_calls=null` means historical cost is unknown, not zero.
Live evaluation reports acquisition cost and worker plus end-to-end elapsed time
(including queue wait). Inputs are not automatically cached across live calls.

Report cold-start acquisition, calibration and confirmation costs separately
from the incremental cost when saved HB, LUT and calibration evidence are
available. Include all original solver calls in the cold-start ledger.
HBnoise is an analysis inside the same Spectre process, not a separate call.

`isf_tool.optim.adapters.design_lab_evaluator(reference_factory, level=...,
calibration=..., constraints=...)` returns `evaluate(project, dir)` with status,
feasible, total_violation, merit, objectives, violations and evidence. An independent
constraint callback must provide measured startup/frequency/power status and,
for ISF shaping, qualified L2 `isf_metrics`. Missing electrical evidence is
`not_evaluated`; failed acquisition is `failed`; neither is feasible. L0 merit
is |rho_hat|; L1 merit is measured PN; L2 merit is |rho_dyn|. All minimize.
At L0 the independent L2 evidence supplies the closure gate. At L2 the newly
evaluated candidate must pass its own gate; a qualified baseline cannot override
its failed numerical quality, missing thermal evidence or pair closure.
The electrical callback must compare each candidate's measured gm·Z21 with the
baseline when nondegradation is required, in addition to any absolute gain floor.
[The evaluator example](../examples/optim/design_lab_evaluator.py) shows how to
supply references and independent constraints. The generic `ask_tell` adapter
accepts an injected population generator with `ask` and `tell` methods.

```sh
vco-isf optim evaluate design-ref.json --level L0 --calibration calibration.json --offset-hz 100000
vco-isf optim evaluate isf-result.json --level L2 --offset-hz 100000
vco-isf optim calibrate selected-result-ref.json
vco-isf metrics after.json --point band_high --offset-hz 100000 --qualify --json > metrics.json
vco-isf optim gate metrics.json
vco-isf optim rank-knobs metrics.json topology.json
# Source checkout uses: python3.11 -B run.py optim ...
```

## Complementary L2

For a generated Design Lab or Maestro reference, set `gm` and
`complementary.target_instance` explicitly. Optional same-polarity peers use
`peer` and `complementary.peer_instance`. Native-spec references use the
device selections in the completed spec. Artifact references use the saved
device selections and can name the shared physical point with `point`. Use
`--device` on a saved L2 result to select one occurrence. Its metrics retain
the combined modeled closure calculation and its own matched source errors.

```json
{
  "schema": "vco.isf-design-ref.v1",
  "kind": "design_lab",
  "generated_dir": "/path/to/generated",
  "workspace": "/path/to/owned/workspace",
  "backend": "private_netlist",
  "design_lab_root": "/path/to/design-lab",
  "psf": "/path/to/psf",
  "gm": "DUT.MN1",
  "complementary": {"target_instance": "DUT.MP1"}
}
```

L2 returns two metrics entries per offset. The device path is in
`source.device_instance` and the two signed closure errors are under
`closure.device`. Both mechanisms on both devices must have absolute error
at most 2 dB for the closure part of the shaping gate. Numerical quality,
PM projection, harmonic coverage, and the whole-VCO modeled-power gap remain
independent gate inputs. The gap uses the combined modeled power.

A generated or Maestro reference can explicitly analyze an asymmetric pair.
Set `gm` and `complementary.target_instance` to the two
same-type occurrences and set `complementary.mode` to
`"asymmetric_pair"`. Omit both symmetric peer fields. Closure is evaluated
for each selected device and mechanism before the gate evaluates the
independently summed budget.


### Qualification from a selected sample

Use `metrics --qualify --json` to include the exact sample's L2 quality flags,
pair or device closure, and declared harmonic range. Pass that JSON directly to
`optim gate`. Without `--qualify`, `metrics` provides waveform and attribution
diagnostics, and the gate may deny a result with missing qualification evidence.
A full `optim evaluate --level L2` result qualifies all returned samples and
can also be passed directly to `optim gate`.

For a saved sweep, pass `--point` and `--offset-hz` to either `metrics` or
`optim evaluate`. Both accept `--workspace` and `--job`. A design-reference
JSON with an explicit supported `kind` can omit its schema tag.
