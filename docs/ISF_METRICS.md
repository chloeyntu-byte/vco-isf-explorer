# Optimization ISF metrics v1

`isf_tool.metrics.compute_metrics(result, *, point=None, offset_hz=None)` is a
read-only, NumPy-based diagnostic. It never changes solver calculations or
signoff. Scalars follow the research report's `vco.isf-metrics.v1` envelope;
`metric_info[path]` records unit, definition, provenance, status and reason.
`null` means unavailable, never zero. Status is `measured` (derived from saved
measurements), `approximate`, or `N/A`.

## Selection and export

```python
from isf_tool.api import metrics
m = metrics("result.json", point="band_high", offset_hz=1e6)
m = metrics("JOB_ID", workspace="workspace", point="band_high", offset_hz=1e6)
```

```sh
vco-isf metrics result.json --point band_high --offset-hz 1000000 --json
vco-isf metrics --job JOB_ID --workspace workspace --point band_high --json
```

API jobs resolve only in the supplied workspace; legacy `jobs/JOB_ID/result.json`
is also supported. API manifests use their saved result snapshot first, then
`canonical_path/result.json`. Ambiguous selection fails instead of borrowing a
sample or choosing the newest point. Result-page **Export → Optimization metrics JSON** exports
the currently selected operating point and offset, independently of the visible
noise-kind filter. The metrics cover the saved result, not a filter-dependent
partial sum. Schema: `isf_tool/schemas/vco.isf-metrics.v1.schema.json`.

## Waveform and integration convention

The input must contain a closed, strictly increasing, complete 2π `wt_rad` grid.
All moments, Fourier coefficients and signed areas use trapezoids over that
actual grid, including its actual closing sample, consistent with the existing
result-page cycle statistics. Incomplete grids and missing samples give N/A;
there is no filling across missing samples. Fourier coefficients are
`Vk = ∫ V exp(-ikθ) dθ/(2π)`. Derivatives use a periodic FFT on uniform grids,
and a second-order local gradient on nonuniform grids. No smoothing is applied;
staircase HB voltages can amplify derivative noise.

Vdiff prefers `workpoint.Vdiff_V`, then `VD_V - VDN_V`. If only VD is saved,
`VD(θ)-VD(θ+π)` is an explicitly approximate symmetric-pair reconstruction. It
cancels all even harmonics by construction. In particular, its ψ2 is N/A rather
than the phase of a numerical zero. `single_ended.*` reports VD harmonics
separately because the research report's ψ2/ψ3 used VD, not Vdiff. No claim of
measured differential asymmetry is made from one drain.

| Fields | Definition | Unit |
|---|---|---|
| `f0_hz`, `vpk_v` | Oscillation frequency; half peak-to-peak Vdiff | Hz, V |
| `waveform.{rise,fall}_max_slope_v_rad` | Maximum signed-edge slope **magnitude** | V/rad |
| `waveform.{rise,fall}_mean_slope_v_rad` | 0.8 Vpp divided by the 10–90% phase duration | V/rad |
| `waveform.{rise,fall}_{max,mean}_slope_v_s` | Above slope × 2πf0 | V/s |
| `waveform.{rise,fall}_max_phase_rad` | Phase of corresponding maximum slope | rad |
| `waveform.{max,mean}_slope_ratio` | Rising/falling slope magnitudes | 1 |
| `waveform.{rise,fall}_time_s` | 10–90% rise / 90–10% fall, along global min→max / max→min edge; first ordered crossings, linearly interpolated | s |
| `waveform.duty_cycle` | Trapezoid mean of indicator Vdiff > midpoint(Vmin,Vmax) | cycle |
| `{waveform,single_ended}.psi{2,3}_deg` | arg(Vk) − k arg(V1), wrapped to [−180,180); N/A when harmonic ratio ≤1e−8 | deg |
| `{waveform,single_ended}.v{2,3}_over_v1` | \|Vk\|/\|V1\| | 1 |

## ISF and effective ISF

Γ always denotes the saved **carrier in-phase** `isf_i` (C⁻¹), not a
normalization to dimensionless textbook ISF, nor the quadrature component.
The effective ISF uses saved modulation in A/√Hz, so Γm has s⁻¹/√Hz units.

| Fields | Definition | Unit |
|---|---|---|
| `gamma.{positive,negative}_peak` | Positive and negative lobe peak magnitudes; negative peak is reported as a positive magnitude | C⁻¹ |
| `gamma.{positive,negative}_peak_phase_rad` | Phase of signed lobe maximum | rad |
| `gamma.{positive,negative}_fwhm_cycle` | Width of the connected half-height lobe containing that signed global peak, divided by 2π; linear crossing interpolation; circular seam handled | cycle |
| `gamma.{positive,negative}_area` | ∫ max(±Γ,0)dθ, positive magnitudes | rad/C |
| `gamma.area_ratio` | Positive / negative area magnitudes | 1 |
| `gamma.rms` | sqrt(⟨Γ²⟩) | C⁻¹ |
| `waveform.gamma_crest` | max\|Γ\| / Γrms | 1 |
| `flicker.area_{positive,negative}_{dyn,dc}` | ∫ max(±Γm,0)dθ, positive magnitudes | rad s⁻¹/√Hz |
| `flicker.area_ratio_{dyn,dc}` | Positive / negative effective areas | 1 |
| `flicker.{c0_eff,c0_dc}` | ⟨Γm_dyn⟩, ⟨Γm_DC⟩ = signed integral / 2π | s⁻¹/√Hz |
| `flicker.{area_asym,area_asym_dc}` | (positive−negative)/(positive+negative) areas | 1 |
| `flicker.{rho_dyn,rho_dc}` | ⟨Γm⟩/(Γrms mrms) | 1 |
| `flicker.c_k[k]` | Re(Qk ΣQ*)/\|ΣQ\|²; signed coherent projection, **not** power share | 1 |
| `flicker.delta_k_deg[k]`, `A_k[k]` | arg(Mdyn,k/Mdc,k), \|Mdyn,k/Mdc,k\| from stored harmonics | deg, 1 |

The connected positive and negative FWHMs differ from the report's sampled
`mean(abs(Γ)>max(abs(Γ))/2)` occupancy. Both definitions give a combined 2/3
cycle for an ideal sine; they need not agree for unequal lobes or multiple peaks.
Areas are full phase integrals, whereas the report prints cycle-normalized
positive/negative means; divide these exported areas by 2π to compare.

## Thermal, closure and conditional benefit

| Fields | Definition | Unit |
|---|---|---|
| `thermal.band_frac[n]` | Σsources Σ\|m\|=n Bm / Σall retained thermal Bm; includes ±m, excludes flicker | 1 |
| `sources[i].share` | Spectre source PM PSD / **total** Spectre PM PSD; never renormalized to a partial list | 1 |
| `thermal.overlap.target_mdyn2` | ⟨Γ² m_dyn²⟩/(⟨Γ²⟩⟨m_dyn²⟩), flicker modulation used as thermal PSD-shape proxy | 1 |
| `thermal.overlap.target_abs_id` | ⟨Γ² \|ID\|⟩/(⟨Γ²⟩⟨\|ID\|⟩), PSD assumed ∝ \|ID\| | 1 |
| Other `thermal.overlap.*` | Source-specific O_j is N/A: result.json lacks source-specific Γ_j and phase-resolved thermal PSD | 1 |
| `closure.spectre_total_dbc_hz` | Proven whole-VCO PM-SSB reference | dBc/Hz |
| `closure.modeled_total_dbc_hz` | Available flicker (pair, otherwise target single device) + qualified thermal, summed in linear power; missing components are excluded, single and pair are never added together | dBc/Hz |
| `closure.gap_db` | Spectre total − actual modeled total | dB |
| `closure.dominant_unmodeled_source` | Largest explicitly unmatched source; no circuit-role guessing from instance names | source ID |
| `potential_db[i].value` | −10 log10[1−s_j(1−O_target/O_j)], conditional target O=0.5 | dB |

Existing result-presentation qualification and exact source matching determine
which sources and thermal budgets are modeled. `sources[i].modeled` means
membership in the components counted in closure, not availability of an
individual calculated PSD. A valid pair estimate includes both exact `fn`
members named by `flicker_pair_identity`, even without per-source predictions.
Single-device fallback includes only the explicitly identified target `fn`;
thermal membership requires a qualified budget and exact device/mechanism.
An unverified historical thermal budget cannot close the gap. If only a
single-device dynamic prediction exists, it remains the modeled total rather
than becoming N/A; the missing peer remains unmodeled. Per-source potential is
available for the explicitly identified modeled target channel using the
approximate flicker shape. A pair
estimate additionally requires proven symmetric-independent pair identity and
explicitly assumes the peer has the same O proxy. No estimate is a promised gain.
Shares outside [0,1] are not silently clamped; the potential is N/A.

`closure.gate` marks source attribution when G exceeds 3 dB or closure is missing.
The remaining views explain coherent flicker projection, dynamic and DC overlap,
and thermal input bands using the selected result's evidence. Section 7 of
[ISF theory](ISF_THEORY.md) reports the prospective attribution checks and the
Rg sweep. The optimizer's shaping eligibility gate additionally requires
numerical, projection, and source closure evidence.

## L0 calibration and proxy

```python
from isf_tool.metrics import calibrate, proxy_metrics
cal = calibrate(result, point="band_high", offset_hz=1e6)
p = proxy_metrics({"theta_rad": theta, "vdiff_v": voltage, "f0_hz": f0},
                  alpha_dc=quasistatic_modulation, calibration=cal)
```

`calibrate` exports JSON-safe δ_k/A_k and a **single measured sample's** Γ
scale/sign. Voltage selection is identical to `compute_metrics`: explicit
`Vdiff_V`, then actual `VD_V - VDN_V`, then the labeled half-cycle single-drain
approximation. Calibration's `waveform` records that selection and provenance.
Base Γ̂ is `−dVdiff/dθ × (2πf0)²/Vpk²`; scale aligns RMS and sign
with that sample. A missing calibration cannot supply an absolute ĉ0. Transferred
calibration gives corrected α by changing each saved Fourier order with
`A_k exp(jδ_k)` (k=0 changes its real mean only). Unspecified orders remain
unchanged; orders beyond Nyquist fail. `proxy_metrics` returns Γ̂, α̂, ρ̂ and ĉ0
in the L0 variant of the schema. All proxy outputs are approximate, for ranking
only. Recalibration is needed for a changed family/geometry; this data establishes
no generalization across processes. α_DC must be on the same closed phase grid.

## Result-page interaction and regression

The new **Optimization metrics** view has waveform/derivative, signed Γ area/FWHM, and
Γm_dyn/DC comparison panels. Table rows highlight edges, markers, widths or
curves. Selecting harmonic, thermal-band, overlap, closure or source-share rows
replaces the top plot with the corresponding context chart while retaining the
complete table and the two ISF plots. Values, units, definitions and provenance
can be copied at full precision; long columns remain available by horizontal
scrolling or “All columns”. Missing data remains a gap.

Tests cover analytic sine/asymmetric second-harmonic waveforms, seam crossings,
nonuniform grids, actual endpoint integration, missing data, gating, frozen real
workpoints, transferred correction and API/CLI selection. GUI tests are opt-in
with `ISF_GUI_TEST=1` and an explicitly assigned private `DISPLAY`. They check
1280×800 / 1600×1000 in both themes, real mapped table rows, region/marker links,
context plots, and export. Existing calculation/thermal/adapter code is untouched.
