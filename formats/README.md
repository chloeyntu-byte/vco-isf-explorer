# Canonical dual-TB bundle format

Files are UTF-8 CSV with one header row.  There are two deliberately different
ingestion paths:

- A user-selected/manual import is always treated as `replay`, even when the
  files contain old `signoff` claims.  It is suitable for viewing and
  recomputation, not for creating signoff evidence.
- An owned automatic run may request `signoff`.  In addition to the canonical
  CSV contract below, it must pass the current-job manifest and simulator
  provenance checks described under **Owned signoff bundle**.

The small files shipped in this directory exercise the manual replay path.
They are not a formal signoff fixture.

## Run classification

Every run request/result and portable bundle metadata uses exactly one
`run_kind`:

- `plan`: planning or dry-run output; non-signoff.
- `replay`: import/recompute of existing artifacts; non-signoff.
- `signoff`: fresh auditable output that passes every convention and provenance
  gate below.

`run_kind` is authoritative.  A compatibility `signoff` boolean, if present,
must agree with it.  A legacy bundle that omits `run_kind` or any required
signoff field may be handled only as non-signoff (normally `replay`); it must
never be inferred or promoted to `signoff`.

## Scalar sample identity

A canonical scalar `point` represents one physical operating point at one
noise offset. It must not contain harmonics from several offsets, even when
their k values are different and therefore do not collide. Before calculation,
the importer checks every supplied `offset_Hz` / `offset_hz`, `f0_Hz`, `corner`
and `operating_point_id` across that point's VCO, replica and workpoint rows.
Offset values and their aliases must agree exactly after numeric parsing;
f0 permits only the existing 1e-12 relative serialization tolerance.

Legacy optional identity columns may be absent. In that case, the point name
is its operating-point identity, and the existing scalar offset option or
metadata fallback remains available. A supplied conflicting value is never
treated as absent. Do not encode the offset only inside a human-readable
point name: plotting uses explicit numerical fields, never name parsing or
the current UI form.

## `vco_results.csv`

One row per design point and signed source harmonic.  Required columns:

```text
point,k,corner,f0_Hz,carrier_real_V,carrier_imag_V,H_real_ohm,H_imag_ohm,spectre_pm_asd_V_rtHz
```

`k` is the actual signed input harmonic in
`k*f0 + offset -> f0 + offset`; it is not the Cadence sideband index.  For the
configured output sideband, the Cadence HBXF index is normally `k-1`.
`spectre_pm_asd_V_rtHz` is PM-SSB ASD, not PSD or total/DSB noise.

For `run_kind=signoff`, every VCO row must additionally contain both of these
finite positive per-point pair values:

```text
second_device_predicted_pm_asd_V_rtHz,spectre_pair_pm_asd_V_rtHz
```

Each value must be identical on every signed-harmonic row for its design point.
The importer combines the two independent predicted device ASDs in the power
domain and compares that result with `spectre_pair_pm_asd_V_rtHz`.

## `replica_results.csv`

One row per design point and non-negative harmonic.  The complete signoff
header is:

```text
point,k,Id_real_A,Id_imag_A,Mdc_real_A_rtHz,Mdc_imag_A_rtHz,AM_A_rtHz,PM_A_rtHz,USB_A_rtHz,LSB_A_rtHz,Dk_real,Dk_imag,sqrt_Sxi_A_rtHz,Mdyn_real_A_rtHz,Mdyn_imag_A_rtHz,HBAC_norm_mag,HBAC_norm_phase_deg,phase_candidate,phase_score
```

For `run_kind=signoff`, every harmonic row, including `k=0`, must carry:

- the same finite positive `sqrt_Sxi_A_rtHz` within that design point;
- finite `Dk_real` and `Dk_imag`; and
- finite `Mdyn_real_A_rtHz` and `Mdyn_imag_A_rtHz` satisfying
  `Mdyn[k] = (Dk_real + j*Dk_imag) * sqrt_Sxi_A_rtHz`;
- finite `USB_A_rtHz`, `HBAC_norm_mag`, and `HBAC_norm_phase_deg`; and
- one common non-empty `phase_candidate` plus a finite `phase_score` repeated
  on every row.  The score must match the importer recomputation.

For `k>=1`, `AM_A_rtHz`, `PM_A_rtHz`, and `LSB_A_rtHz` are also required and
must be non-negative.  At `k=0`, `USB_A_rtHz` must equal the shared
`sqrt_Sxi_A_rtHz`.

The shared positive `sqrt_Sxi` may differ between design points.  Partial
declarations, per-harmonic renormalization, or an inconsistent `Mdyn` fail
signoff.

For `k>=1`, AM/PM data defines the default engineering phase candidates
`exp(j*phase(Id[k])) * (AM + j*s*PM)`, with `s` in `{-1,+1}`.
`phase_candidate` and `phase_score` record the engineering-selected branch.
The final VCO phase-noise reference must never select a branch.  HBAC and PAC
do not replace AM/PM selection or override it.  A policy may choose whether an
HBAC comparison is a pass/fail gate, but signoff rows still require the two
finite `HBAC_norm_*` measurements as auditable secondary evidence.

Legacy AM/PM-only rows may reconstruct `Mdyn`, but only for a non-signoff
`plan`/`replay`.  Missing `sqrt_Sxi`, `Dk`, `Mdyn`, or strict convention
metadata is never signoff-compatible.

## `workpoint.csv`

Optional for `plan`/`replay`, and required for every point in `signoff`:

```text
point,sample,time_s,wt_rad,VG_V,VD_V,VS_V,VB_V,ID_main_A,ID_replica_A,m_DC_A_rtHz,m_dyn_A_rtHz,isf,product,isf_i,isf_q,product_i,product_q
```

All periodic columns and the replica drive must use one period and one physical
time origin `t0`.  Raw traces must already share that origin; a fixed or fitted
manual waveform shift is forbidden and `manual_waveform_shift` must be
`false`.

For `canonical-two-tb-v2`, `isf_i/isf_q` are the two carrier-referenced
signed-H branches and `product_i/product_q = isf_i/isf_q * m_dyn`.  They are
derived from the carrier phasor and the complete signed transfer `H[-K:K]`, not
from `Mdyn` or the final output-noise phasor.  `isf/product` remain I-branch
compatibility aliases.  A formal importer recomputes both branches from
`vco_results.csv`; editing ISF and product together cannot bypass the gate.

For signoff, each point must contain at least three rows on a strictly
increasing, uniform, endpoint-inclusive `0..2*pi` phase grid.  `time_s` must
span exactly one oscillator period and imply the same physical time origin on
every row.  All periodic columns must agree at the two endpoints.  The
importer also recomputes the Fourier coefficients of `m_DC_A_rtHz`,
`m_dyn_A_rtHz`, and `ID_replica_A`, and checks both ISF branches and products.

## Metadata

`metadata.json` freezes the run classification, units, conventions, source
normalization, phase-selection role, and time origin.  A signoff example is:

```json
{
  "format": "canonical-two-tb-v2",
  "isf_source": "carrier_referenced_signed_H_iq_v1",
  "isf_independent_of_noise_modulation": true,
  "run_kind": "signoff",
  "signoff": true,
  "point_count": 6,
  "formal_point_count_qualified": true,
  "numerical_quality_all_pass": true,
  "hbac_secondary_all_pass": true,
  "offset_hz": 10000.0,
  "source_noise_quantity": "PSD_A2_per_Hz",
  "canonical_noise_quantity": "ASD_A_per_sqrt_Hz",
  "psd_square_roots": 1,
  "current_direction": "drain_to_source",
  "carrier_amplitude": "differential_peak",
  "output_noise": "PM_SSB",
  "phase_selection": "HBNOISE_AM_PM_engineering_candidate",
  "hbac_role": "secondary_validation_only",
  "pac_role": "secondary_validation_only",
  "common_source_normalization": "Mdyn[k]=Dk*sqrt_Sxi",
  "time_origin": "shared_unshifted_VCO_period",
  "raw_t0_spread_s": 0.0,
  "replica_drive_time_origin_s": 0.0,
  "manual_waveform_shift": false,
  "device_count": 2,
  "second_device_model": "symmetric_independent",
  "independent_device_combination": "power"
}
```

The one-point files shipped directly under `formats/` are illustrative manual
input and therefore declare `replay/signoff=false`; copying that example must
never create signoff.

Automatic exports also retain `operating_points[point]`, including the
effective `params`, original parameter records and available restoration
evidence from the selected Maestro run. This is a per-operating-point snapshot,
not an offset list. Recomputed summary rows expose that snapshot as
`operating_parameters` and the scalar identity as `operating_point_id`.

## Derived result fields and noise scope

The JSON presentation contains `summary` and per-point `summary`, `harmonics`,
`workpoint` and `phase_candidates` tables. The following fields are derived
from the same scalar calculation, without another PSD square root or a new
phase-selection method:

| Fields | Unit | Meaning / use |
| --- | --- | --- |
| `dynamic_dBc_Hz`, `spectre_fn_dBc_Hz` | dBc/Hz | Calculated and simulated target-device flicker comparison |
| `predicted_pair_dBc_Hz`, `spectre_pair_dBc_Hz` | dBc/Hz | Independent two-device flicker comparison; review `pair_model` |
| `Q_real_V_rtHz`, `Q_imag_V_rtHz` | V/√Hz | Complex order contribution; `Qk_real_V_rtHz` / `Qk_imag_V_rtHz` are equal aliases |
| `harmonic_equivalent_dBc_Hz` | dBc/Hz | Equivalent noise retaining one harmonic group only |
| `cum_real_V_rtHz`, `cum_imag_V_rtHz` | V/√Hz | Coherent complex sum from order 0 through the current k |
| `cumulative_dBc_Hz`, `cumulative_error_dB` | dBc/Hz, dB | Truncated coherent sum and its error relative to the single-device reference |

With the existing real-waveform coefficient convention:

```text
Q0 = H[0] * Mdyn[0]
Qk = H[k] * Mdyn[k] + H[-k] * conj(Mdyn[k])       (k > 0)
ASDtotal = abs(sum(Qk))
L(ASD) = 10*log10(ASD**2 / (carrier_peak_V**2 / 2))
error_dB = 20*log10(calculated_ASD / reference_ASD)
```

Positive and negative source harmonics are grouped once in each nonzero Qk.
Orders share the underlying noise source and can cancel. They are not
independent powers to stack or add, and their dBc/Hz values are not additive.
Only distinct independent devices combine as `sqrt(ASD1**2 + ASD2**2)`.
The symmetric-pair assumption is explicit as `pair_model=symmetric_independent`;
missing pair values remain unavailable instead of being guessed.

The legacy fields and formulas above concern the selected device(s)' **flicker** output
contribution. “Total” means the coherent sum of the retained harmonics, not the
whole VCO's total phase noise or all noise mechanisms. A Maestro point-noise
output is not interchangeable with this device-specific reference. The main UI
additionally uses explicitly sourced whole-VCO total PM-SSB evidence and the
independent thermal extension below; it never reinterprets the legacy fields.

## Optional thermal evidence

`vco_results.csv` may include `thermal_noise_json`, identical on every harmonic
row of one scalar point. JSON `null` or an absent field means not collected;
`status=unavailable` preserves the reason. Neither means zero thermal noise.
Available schema version 1 contains PM-SSB validation identity, source scope,
carrier/offset, the symmetric pair model, and each `(device, mechanism)` source
with nonnegative PSD and integer independent input-band powers. Import checks
unique source/band identities and band-to-source-to-total power sums, and
recomputes dBc/Hz using the existing carrier convention. Carrier and offset
must match the flicker sample. Oscillator frequency must also match: the exact
frequency or its known six-significant-digit legacy PSF serialization is
accepted, not an arbitrary broad frequency tolerance. A supplied malformed
budget is not discarded.

Source coefficient/transfer audit arrays stay in private run artifacts; the
portable budget retains compact powers rather than duplicating every coherent
path on every CSV row. CSV fields have a bounded 4 MiB parsing limit. Legacy
bundles remain readable. A combined prediction requires available thermal and
pair-flicker evidence; it is never silently replaced by flicker alone.

Per-sample `pm_magnitude_projection_valid`, `numerical_quality_pass` and
`hbac_secondary_validation_pass`, together with their codes/thresholds where
available, are retained in summaries, harmonics and plot samples. Missing
checks are unknown, not PASS. A finite equivalent dBc/Hz value does not by
itself prove that the PM approximation is valid or that signoff passed.
Missing/non-finite quantities, or zero-ASD logarithms, remain JSON `null`;
viewers must not substitute zero or interpolate across missing samples.

The shared [metric glossary](../isf_tool/ui/metric_catalog.json) supplies each
displayed definition's `label`, `unit`, `meaning`, `purpose`, `scope`, `formula`
and `aliases`. It explains Mdc/Mdyn, Ak, Ck, Qk and the diagnostic metrics in
the same way in both interfaces.

## Multi-offset requests

The UI sends a recipe in `analysis.offsets`; `core.offsets` expands it before
execution. The old `analysis.offset` remains the scalar compatibility field.
Examples of supported recipes are:

```json
{"mode": "list", "values": "1k,10k,1M,100M"}
```

```json
{"mode": "linear", "start": "1k", "stop": "10k", "spacing": "step", "step": "4k"}
```

```json
{"mode": "linear", "start": "1k", "stop": "10k", "spacing": "intervals", "intervals": 3}
```

```json
{"mode": "log", "start": "1k", "stop": "100M", "points_per_decade": 1}
```

Bare numbers are Hz. Supported engineering suffixes include `k`, `M`, `G`;
ambiguous lowercase `m` is rejected. Explicit lists may use commas or
whitespace. Values must be finite and positive; the resolved list is sorted
and deduplicated with at most 256 offsets. The list input itself is also
limited to 256 entries. A returned `values_hz` is derived preview data and
does not override the recipe.

Both endpoints are included. N linear intervals create N+1 sites before
deduplication, so at most 255 intervals are accepted. A linear step appends
the exact stop when it does not divide the span. Logarithmic interior spacing
has ratio `10**(1/points_per_decade)` and also appends a non-decade stop. The
last example yields exactly 1k, 10k, 100k, 1M, 10M and 100M. Equal start/stop
deduplicates to one point. No noise values are interpolated by this expansion.

## Portable multi-offset aggregate

Each offset uses its own ordinary scalar canonical bundle. The parent
directory adds a comparison manifest and cached display JSON; for example:

```text
offset_sweep.json
result.json
offsets/
  1000Hz/     # scalar vco_results.csv, replica_results.csv, metadata, ...
  10000Hz/    # another independent scalar bundle
```

`offset_sweep.json` has `schema_version=1`, `complete`, `offsets_hz` and one
`children` entry per requested offset. Each child records its `offset_hz`,
relative `bundle_path` and `bundle_tree_sha256`; job/workspace/result identity
fields provide source traceability. Export stages and content-checks each
child copy before publishing it. Resume may reuse an existing identical child
copy, but must not overwrite a different one.

Import requires a complete manifest, unique and complete offset coverage,
relative child paths that resolve inside the aggregate directory, and matching
content-tree hashes. It then **recomputes every child from its canonical CSVs**
and aggregates the results. Saved `result.json` curves are not numerical input.
Copy the whole parent directory, including `offsets/`, when moving the result
to another machine.

Aggregated point keys are `<scalar-point>@<offset>Hz`; all row types keep the
original `operating_point_id` and numeric `offset_Hz` / `offset_hz`. Every
child must contain the same physical point set. Two corners at six offsets
remain two operating points, not twelve formal-signoff points. Different
parameter settings sharing one corner label also remain separate identities.

The active job may expose a partial aggregate with `complete=false` and
`completed_offsets_hz` after cooperative stop or failure. It can be viewed and
resumed with verified frozen inputs; incomplete portable manifests cannot be
imported as if complete. A complete aggregate is still `replay/signoff=false`:
it is a comparison container, not new owned-signoff evidence. Scalar children
retain their original evidence and must satisfy their own signoff gates.

## Owned signoff bundle

Only the automatic orchestrator/exporter path can establish owned signoff.  A
complete bundle contains all of:

```text
vco_results.csv
replica_results.csv
workpoint.csv
metadata.json
manifest.json
```

The orchestrator verifies that the bundle belongs to the current job and was
created after that job started.  `manifest.json` binds the size and SHA-256 of
every canonical data file.  Signoff metadata must bind the current simulation
root, engine and resolved-config hashes, explicit physical-profile ID/hash,
and packaged runtime identity.  It must also contain current PSF evidence for
both `vco` and `replica` roles at every design point, including their
`run_environment.json` identities.  Formal signoff requires at least six real
design points.

Missing, stale, foreign, or hash-mismatched evidence fails closed with the
`ISF-E3001`/`ISF-E3002` family.  Manually adding a job ID, manifest, or
`signoff=true` cannot promote an imported replay bundle.

A formal exporter additionally rescans current Spectre logs.  Zero-error
completion does not override a known steady-state, model, or convergence
quality warning.

`--smoke` automatically performs an explicit non-signoff export.  The separate
`--non-signoff` switch does the same for other diagnostic runs.  Both may retain
a job ID for traceability and record `explicit_non_signoff`; a job ID alone is
not evidence that a single-point run passed the formal six-point gate.  Only
`--formal-signoff` explicitly requests that gate, and it also requires a job ID.

The source simulator quantity is PSD in A^2/Hz.  It is square-rooted exactly
once into canonical ASD in A/sqrt(Hz); all `*_A_rtHz` and `*_V_rtHz` columns
contain ASD.  The legacy ambiguous key `noise_quantity` alone is insufficient
for signoff.  Values must not be guessed or silently converted between PSD and
ASD, drain/source direction, RMS/peak, PM/total noise, or SSB/DSB.
Formal signoff also requires both per-point pair columns on every VCO row;
the two independent device ASDs are combined only in the power domain.
