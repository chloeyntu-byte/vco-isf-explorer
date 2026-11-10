# Architecture

## Boundaries

```text
Tk UI ───────────────┐
                    ├─> ToolService -> preflight / pair / jobs / results
loopback Web adapter ┘                      |
                                             v
                              DualTBOrchestrator
                          vco -> exchange -> replica -> extract
                                             |
                                  private run directory
```

The Tk and Web front ends share JSON-compatible contracts.  Neither contains
calculation, PDK, or simulator logic.  Tk uses one worker queue and returns all
widget changes to the Tk thread.

The compatibility Web adapter accepts only literal IPv4 loopback/localhost
requests and requires a per-process random API token.  Static preflight remains
read-only until an explicit execution action; only that request permits sourcing
the selected Cadence bridge, probing configured executables, and starting the
external execution chain.

## Input contract

Every live run binds `testbenches.vco` to a Maestro directory, test and
completed History. Manual reference runs also bind `testbenches.replica`.
The generated flicker path uses `testbenches.replica.source = "generated"`
and the VCO target MOS mapping. A binding is invalidated when its path,
test or History changes.

For each generated point, the runner restores the effective operating
point in a private VCO copy. The fixture generator resolves the selected
MOS and active include/section, carries the effective model parameters and
simulator options, and creates the four-terminal DC/noise template. The
dynamic replica uses the same selected device and periodic D/G/S/B voltages.
Unsupported hierarchical expressions, non-unique models or unproven terminal
order fail during inspection or preparation. The generated scope is selected
MOS flicker with a symmetric independent power estimate. Thermal and formal
pair signoff use the manual reference path.

## Portable signal and execution setup

Read-only pair inspection combines readable Maestro test/output/variable
records with generated netlists and histories.  Schematic connectivity is
available only through a readable generated netlist for the selected history;
the reader does not decode OA schematic binaries or infer unsaved OA edits.
Source paths, expressions, and names remain visible in the report.

Each role exposes a `signal_catalog` of nets, outputs, variables, devices,
analyses, parameters, and saved signals.  `mapping_fields` describes each
semantic role with a human label, detected value, candidate names, source,
status, help, required flag, and effective value. Consumption metadata
(`active`, `group`, `used_by`, `consumption_mode`) separates actual signal
inputs from execution details and reference-only names. Front ends prioritize
consumed roles and always expose invalid/stale/ambiguous choices; complete
catalogs remain dropdown sources and folded diagnostics. The effective `mapping`
combines current evidence with explicit `testbenches.<role>.mapping_overrides`.
An empty override means automatic detection.  Ambiguous candidates are not
silently resolved, and stale or invalid manual names produce diagnostics.

The pair's `cross_tb_mapping` joins VCO waveform outputs to flicker DC variables
by VGS/VDS/VBS meaning.  It preserves both names, the source expression, and
whether the mapping comes from a supported named waveform or confirmed device
terminals.  Supported simple voltage differences can identify terminals;
direct single-ended time voltages are ground-referenced. A separate
`topology.voltage_equivalence` records proven constant-zero ideal-source
connections and their source statements for matching, without renaming nets
or changing the execution profile's terminals. Nonzero, time-varying,
unknown and cross-history-varying voltage sources cannot establish this
equivalence; explicit frequency-domain expressions remain reference-only.
missing named waveforms can fall back to raw node differences in the private
run.  Arbitrary Calculator expressions stay in the catalog as reference data
and are never evaluated.  A reference-only expression cannot silently become
an executable waveform mapping; the user must choose a supported expression
or use confirmed terminals.

A recognized HBXF output or output family such as `mag_pxf` binds the
raw transfer source, not its saved magnitude value. The user owns its physical
meaning; redundant source/model/same-branch checks do not override that choice.
A previously established reversed branch retains `direction_factor=-1`.
The private run measures complete
complex `H[k]` again at the current differential output, retaining established
polarity conversion and otherwise using the user-declared direction. Original/effective output nodes, source definition
and polarity remain inspectable. HBXF sideband 0 corresponds to algorithm
harmonic k=1, not k=0. Unsupported raw-result reading contracts fail rather than
silently selecting another source. A disclosed private drain/source injection
is the fallback when no supported existing-source mapping is selected.
Named point-noise expressions are reference-only; the pipeline reads raw
noise results and does not evaluate Calculator expressions per offset.

`portable_setup` builds an inspectable execution template from that evidence.
Its field list exposes model paths, devices, terminals, probes, trace names,
and related settings; intentional changes are held in
`portable_setup.overrides`.  These settings feed the shared preflight and
private-job preparation.  Runtime engine/profile JSON remains an internal
artifact in the normal workflow, not a file the user must author beforehand.

Both front ends group discovery details and manual overrides in Advanced
settings and send those overrides through inspect, preflight, and start.
Changing a TB clears obsolete role overrides and pair-specific execution
settings with a visible notice.  Editing settings invalidates preflight;
front ends reject late responses for an older selection.  Automatically
detected values are not silently persisted as user overrides.

An explicit expert `config_path` retains compatibility with existing engine
files, including inline `physical_profile` or a relative
`physical_profile_path`.  That file takes precedence over generated execution
settings.  The UI displays generated suggestions as inactive/read-only until
the expert path is cleared, so editing a control cannot imply an effect that
the expert file would override.  Numerical calculation, consent, provenance,
and formal signoff gates remain shared with the existing execution core.

## Effective operating points

ADE RDB histories may reuse one generated netlist across several points while
changing parameters through the interactive simulator. `maestro_pair` now
seals each selected run's evaluated parameter records and effective operating
point. Selected-test values override that point's common values; conflicts at
the winning priority fail closed.

`restore_operating_point_text` restores supported design variables and
temperature options only in the private root-level Spectre scope. It preserves
subcircuit-local declarations, balanced expressions, continuation lines,
model includes/sections and unrelated options. It does not evaluate arbitrary
expressions, interpret unknown parameter types, or apply instance-local
overrides. Ambiguous declarations or unsafe restoration locations block the
run instead of falling back to the unmodified template. Template helpers use
the same root-statement parser so later preparation cannot reintroduce a
subcircuit-local parameter/options/include as a top-level setting.

Flicker templates are deduplicated over the mapped VGS/VDS/VBS bias sweep
dimensions, retaining non-bias conditions. Pair selection checks corner,
effective shared non-bias parameters, temperature/scaling settings and common
model-family sections; it does not pair by point ordinal. `metadata.operating_points`
retains the per-point contract, and `summary.operating_parameters` exposes the
effective parameter snapshot to the result viewer.

## Frequency-offset scheduling

`core.offsets` is the authoritative parser for legacy scalar offsets, explicit
lists, linear ranges and logarithmic ranges. Bare values are Hz; `k`, `M` and
`G` are supported engineering suffixes, while ambiguous `m` is rejected.
Expansion sorts/deduplicates finite positive values and includes both ends,
subject to `MAX_OFFSET_POINTS = 256`. Linear N intervals produce N+1 sites;
linear steps or logarithmic spacing append the exact stop when necessary.
`points_per_decade = P` produces a ratio of `10**(1/P)` between interior sites.
Only sampling sites are expanded: neither this module nor a frontend
interpolates or extrapolates noise results.

`core.offset_sweep` dispatches one isolated scalar `ToolService` job per offset,
reusing `DualTBOrchestrator` and the existing numerical path. The scalar child
contains all selected operating points. The parent preflights every offset
before starting any simulator and records child jobs in `offset_sweep.json`.
A singleton recipe keeps the scalar path. Different offsets cannot share a
child run directory or a simulator cache identity.

The cooperative stop is checked between scalar children, never by killing a
Cadence process. Completed children remain viewable as an explicitly partial
aggregate after stop/failure. Resume uses the frozen request and verifies
runtime/input identities and persisted result hashes before reusing completed
children or resuming a failed/interrupted child. Changed runtime inputs are
not silently mixed with prior results. Partial results survive task persistence
and process restart; an unsuccessful resume does not discard completed data.

The aggregate key is `<scalar-point>@<offset>Hz`; every result row carries
`operating_point_id` without the offset plus both `offset_Hz`/`offset_hz`.
The point-set must agree across children. Thus two design conditions at six
offsets mean twelve samples but only two physical operating points. Aggregates
are comparison/replay manifests, never an enlarged formal-signoff point set.

## Runtime resources

A source checkout uses top-level `scripts/` and `configs/`.  Wheel builds copy
the same files to `isf_tool/_runtime/`; `isf_tool.resources` selects the correct
layout.  Runtime file hashes use installed module/script locations rather than
assuming a checkout.

Reusable Spectre output is accepted only when its versioned cache identity
matches the prepared netlist, engine configuration, physical/model inputs, and
tool/environment identities. Frequency offset and selected transfer branch/
polarity are part of the execution inputs. Preflight and the exporter share
the runtime-module inventory, including parameter restoration, offset
scheduling and result-presentation modules. Adapter timeouts terminate the full POSIX child
process group before returning an error.

## Result contract

Canonical results expose `summary` plus per-point `workpoint`, `harmonics`, and
`phase_candidates`.  Missing and non-finite values remain unavailable; a UI
must not turn them into zero.  Imported bundles are replay/non-signoff.

Before numerical recomputation, each scalar point must have one consistent
offset, f0, corner and operating-point identity across all provided VCO,
replica and workpoint fields. Distinct offsets are rejected even when their
harmonic orders do not overlap. Legacy optional columns may be absent, but
conflicting supplied values and conflicting offset aliases cannot be ignored.

`core.result_presentation` supplies result-owned `operating_points`,
`phase_noise_series`, `harmonic_noise_series` and `cumulative_noise_series`.
They group by operating-point identity and numeric offset, not a parsed corner
label or a current form value. Missing samples remain gaps. Presentation adds
carrier-normalized dBc/Hz and consistent Q aliases without changing phase
selection or the numerical kernel. Single-order equivalents, cumulative sums
and the total use complex coherent Q sums; independent devices combine in
power. Legacy raw fields retain their single-device meaning. Generated results expose
that selected device and the separate symmetric +3.01 dB estimate. The main
plot includes whole-VCO HBnoise PM-SSB for context. The error plot only uses
reference values with the same source scope as the corresponding prediction.
Per-sample PM-projection, numerical-quality and HBAC
secondary-validation evidence remains attached to each series sample.

## Independent thermal branch

`thermal_execution` adds private upper/lower HBXF, intrinsic static source
noise and periodic MOS quasi-static samples without editing the original TB
or the flicker netlists/math. `noise_sources` decodes actual PSF types and
units; `thermal_ports` supplies mechanism-specific physical transfer ports.
`thermal.py` projects PM and accumulates independent input bands and sources
in power. Within one input band, periodic modulation paths remain coherent.
No whole-VCO HBNoise output is used as a prediction input or fitted gain.

The user-requested `max_harmonic=N` bounds both the PM transfer orders and the
independent thermal input bands `-N..N`; the wider HBNoise reference sideband
setting no longer expands this calculation. Periodic amplitude coefficients
may retain wider support internally, but convolution tails outside the input
limit are excluded and recorded as `input_band_truncation`. This is a finite
band budget, not a claim that a truncated sum equals the full Parseval RMS.
Analyze-only rejects a newly tagged thermal budget with a different requested
limit by marking thermal unavailable; it leaves flicker analysis intact.
Historical untagged results retain their recorded bands, never the current form.

`thermal_budget` validates the optional canonical source/band PSD evidence and
recomputes displayed values with the existing SSB normalization. Flicker and
thermal combine only in linear power; unavailable thermal makes the combined
prediction unavailable, never a relabelled flicker-only result. Diagnostics
retain bandwidth, sampling, source scope and independent-reference evidence.
The automatic port model is limited to Spectre 18.1 BSIM3v3/BSIM4 `id/rd/rs` and
supported native resistor/inductor `rn` sources in verified, fully passive LC
root instances. The scope label `gm_pair_and_lc_network` plus
`passive_scope=passive_only_lc_roots_v1` does not promise arbitrary passive or
behavioral models, standalone resistors outside LC roots, or active arrays.
Custom Verilog-A `thermal_noise` has no automatic production port
mapping. Unsupported discovered sources remain listed as excluded evidence.
BSIM4 source ports were independently checked against native DC noise output
with full-precision XF for the exercised `tnoimod=0/1` models; this is not a
validation of every BSIM4 mode or periodic-noise approximation. The PSF command
line exporter still rounds complex values to six significant digits, including
when a higher real-number precision is requested. Subtracting nearly equal
node transfers can therefore reduce the accuracy of very small source terms.

`load_thermal_evidence` returns unavailable immediately when the expert setting
`thermal_enabled=false`, including analyze-only use; it cannot resurrect a
previous `thermal/budget.json`. Frequency identity requires a positive offset
below the actual HB carrier. The new reader uses the measured frequency
coordinate, and import accepts either that carrier or its known legacy PSF
six-significant-digit serialization, not an arbitrary broad mismatch.
Model warnings such as CMI-2151 (missing bulk-junction diode forward bias)
remain numerical-quality evidence even when Spectre exits with zero errors.
See [thermal method and limits](THERMAL_NOISE.md) and
[independent reference verification](THERMAL_REFERENCE.md).

## UI state and history provenance

`core.ui_state` saves versioned, non-executable snapshots under the fixed
workspace `states/` directory. Names are labels, not filesystem paths; generated
IDs, size limits, regular-file checks and symlink rejection guard file access.
Saving/loading recursively revokes execution consent and binding fingerprints
without removing explicit signal or execution overrides. The service only
returns data: the UI must rebind before a new run. It does not automatically
write the active config, inspect, preflight, run, or resume. Result restoration
requires the same existing job, point, operating identity and offset.

`maestro_pair._history_binding` exposes the root generated-netlist design header
and file mtime alongside the sealed source hash. Display-only mtime is not part
of content identity. Nested schematic comments never replace a root `config`
header; absent/ambiguous headers remain unknown. Current setup vs selected
history differences are disclosed without forbidding deliberate history replay.

The experimental paired-subcircuit object path has been removed. Prediction
is restricted to the existing gm flicker/thermal producer and supported LC
thermal sources. Historical expanded-object budgets are not accepted as this
restricted calculation. Whole-VCO HBNoise remains the independent reference;
unmodeled devices stay N/A rather than being copied from that reference.

`ui/metric_catalog.json` is the shared glossary resource. `core.metrics` exposes
defensive `metric_catalog()` and `metric_definition(fieldname)` accessors to
Tk; Web reads the same JSON. Definitions contain label, unit, meaning, purpose,
scope, formula and aliases, keeping explanatory text out of numerical logic.

Portable multi-offset export copies the complete scalar canonical bundles
under relative child paths and records content-tree hashes. Import verifies
child completeness, uniqueness, relative-path containment and hashes, then
recomputes each child CSV bundle; it does not trust cached aggregate plot JSON.
Child evidence remains independent, and importing the aggregate never grants
signoff. See [the bundle format](../formats/README.md).

Owned signoff requires, at minimum, a signoff run kind, dual-TB signoff, a
matching current job ID, a verified manifest hash, and positive PSF run count.
The UI displays this derived state and does not trust metadata alone.
