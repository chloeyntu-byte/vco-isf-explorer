# Independent thermal and AM/PM reference

The reference isolates AM/PM projection, independent-band power summation, and inductor source units. It uses known inputs rather than fitted VCO values. The [thermal model](THERMAL_NOISE.md) documents the separate production context and sampling limits.

## Files and execution

- `tests/fixtures/thermal_reference/analytic_am_pm/input.scs` contains three analytic modulation paths and their HB, HBXF and HBnoise analyses.
- `white_source.va.txt` supplies known white voltage noise at `1e-18 V²/Hz`.
- `tests/fixtures/thermal_reference/inductor_units/input.scs` tests fixed series resistance and a skin-effect inductor.
- `scripts/verify_thermal_reference.py` reads existing PSF with the specified trusted reader. It uses standard Python and emits JSON on stdout.

Copy `white_source.va.txt` to `white_source.va` next to the netlist before running the analytic fixture.
The fixtures use standard Spectre devices, relative Verilog-A includes and no PDK or binary results. The verification script loads `--cadence-env` only in the reader subprocess. It performs no solver run or result write.

## Verify existing results

```bash
python3 scripts/verify_thermal_reference.py \
  --psf /path/to/analytic_am_pm/psf \
  --inductor-psf /path/to/inductor_units/psf \
  --psf-tool /path/to/cadence/psf \
  --cadence-env /path/to/cadence_env.sh
```

The default reader is `psf` on the subprocess PATH. Omitting `--inductor-psf` runs the AM/PM suite only. Keep the original `../netlist/input.scs`, Verilog-A files, `spectre.out` and analysis outputs. The checker compares SHA-256 against the reference source. Exit 0 means all requested suites passed, exit 1 indicates numeric or provenance failure, and exit 2 indicates missing or unreadable evidence. To generate new evidence, run the fixtures separately in a private Spectre directory.

## AM/PM projection

The carrier is `sin(2πf0t)` at `f0=1 MHz` and peak phasor approximately `−j V`. Use an offset of `1 kHz` and the same independent input band m for both responses.

```text
H⁺[m] = VSTIM from <case>_upper.(m−1).pxf_hbxf, output frequency f0+offset
H⁻[m] = conjugate(VSTIM from <case>_lower.(−m−1).pxf_hbxf), original output frequency f0−offset
r = conjugate(carrier) / abs(carrier)
H_AM[m] = (r H⁺[m] + conjugate(r) H⁻[m]) / 2
H_PM[m] = (r H⁺[m] − conjugate(r) H⁻[m]) / (2j)
S_AM/PM = Σ_m |H_AM/PM[m]|² × S_white
```

Reverse the lower sideband index and conjugate its response. The measured common range is `m=−7…7` and the analytic nonzero paths have `|m|≤3`. HBXF V/V times the square root of the specified voltage PSD gives V/√Hz. The factor 1/2 comes from the projection.

| Reference path | AM PSD (V²/Hz) | PM PSD (V²/Hz) |
|---|---:|---:|
| `BAM`: white noise × sin | 3.75e−19 | 1.25e−19 |
| `BPM`: white noise × cos | 1.25e−19 | 3.75e−19 |
| `BPWM`: white noise × cos × (1+0.5 sin) | 1.40625e−19 | 3.90625e−19 |

BAM has zero PM for low-frequency input m=0. Under broadband white excitation, m=±2 can still fold into PM. BPM likewise depends on the input band. Compare HBXF projection to the analytic value, HBnoise to the analytic value, and the two simulations to each other. The `W.known_white` device contribution matches the total. Square the `out` ASD (V/√Hz) once. The structure's contribution already uses V²/Hz. The analytic comparison uses a relative tolerance of 1e−9.

## Inductor series thermal units

The fixed element uses `L=200 pH, R=10 Ω` at `27 °C` with a noiseless 50 Ω load. The skin-effect element uses `mode=2, q=20, fq=5 GHz, rdc=0.01 Ω`. Each is verified at 10 kHz, 1 MHz, 5 GHz and 10 GHz.

For this verified element, Spectre labels `rn.src` A²/Hz while the fixture expectation is the **series voltage noise PSD** `4kTR`. The check uses a relative tolerance of 3e−5 and retains the raw unit label.

With AC stimulus `vin=1 V`, the independent V/V transfer is `out_fixed/vin` or `out_skin/vin`:

```text
S_out = |Vout/Vin|² × rn.src
```

Require the output to match the PSD `rn.out` and the squared AC transfer to match `rn.xf2`. Use each frequency's skin-effect source PSD. The printed AC complex values support a 1e−5 tolerance. The sourced structure comparison `src×xf2` uses 1e−9. All eight points passed. Use the series voltage port for this verified `rn` mechanism. The structure field resolves the ambiguity of the top-level static-noise `out`, which behaved as ASD despite its PSD label.

## Evidence boundaries

The JSON retains reader and simulator versions, reference and PSF hashes, run completion, per-band transfer and PSD, expectations, error and tolerance. Changed sources or mixed simulator versions fail validation. The reference covers the specified units and projection without a MOS, PDK or oscillator. Actual VCO qualification also needs valid device models, phase coverage, truncation and matched-source comparisons. The supported collection path is scoped to verified Spectre 18.1 device and native resistor/inductor mechanisms. Custom Verilog-A sources require their own port evidence. Unavailable thermal evidence appears as N/A.
