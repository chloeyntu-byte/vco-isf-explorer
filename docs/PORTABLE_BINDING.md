# Testbench binding and waveform transfer

## Scope and result references

The main simulator reference is the total PM-SSB HBnoise from the private run of the bound VCO operating point and output. The original history supplies the test and operating conditions. The noise fixture supplies device noise samples. Use **Noise contributions** for a matched comparison at one operating point and offset. Aggregate comparison rows overlap independent source rows.

A qualified symmetric gm pair sums independent device powers. Explicit complementary or asymmetric choices use per-device evidence. The thermal scope includes supported resistor and inductor contributions once. Additional MOS, unsupported custom sources, and shot noise may appear in HBnoise without an ISF prediction. See [thermal evidence](THERMAL_NOISE.md).

The total `out` uses V/√Hz and the differential HB carrier for normalization. The device contribution PSDs start in V²/Hz. Convert a PSD to ASD once by taking its square root. Source share is simulated source PSD divided by whole-VCO PM-SSB PSD. Harmonic Qk values have a separate coherent-sum interpretation. Older results without whole-VCO reference appear as N/A. Retain numerical warnings and HBAC phase-check evidence when interpreting closure.

## Bind a different testbench pair

1. Select the VCO and noise Maestro setups, their tests and completed histories. Use `REF_MOS_DC` to bind the ordinary MOS noise reference.
2. In **Advanced settings**, review **Calculation role** and **Actual name**. Select from all candidates of that type or type the actual name. Netlist aliases such as `/OUTN` and `OUTN` can resolve to the same net.
3. Apply and validate changes. Restore automatic selection to clear a field's override. A source change invalidates source-specific mappings and pending callbacks.
4. Check **VCO → Noise TB waveform transfer**. Run preflight and start only for the intended selection. Save, load, discovery and binding do not start the solver.

| Role | Name type | Use |
|---|---|---|
| Positive and negative differential output | VCO net | Determines subtraction polarity and the requested HBNOISE/HBXF output |
| Target and peer | VCO device | Establishes the MOS identities, four terminals and injection paths |
| Transfer amplitude and phase | VCO output series or individual output | Locates the saved source of complex current-to-voltage transfer for remeasurement |
| VGS, VDS and VBS waveform | Named VCO output | Verifies terminal differences and can fall back to original private HB terminals where permitted |
| VGS, VDS and VBS bias | Noise TB variable | Sets quasi-static bias samples for Mdc |
| VGS, VDS and VBS voltage source | Noise TB source | Proves which source controls each terminal and isolates DC samples from old PWL files |
| VGS, VDS and VBS waveform-file variable | Noise TB global variable | Supplies generated PWL paths to the private periodic replica |
| VS source common-level file variable | Noise TB global variable or private insertion | Retains the actual source voltage in addition to terminal differences |
| Noise target, current sensor and current-to-voltage readout | Noise TB device or source | Selects the physical drain branch, flicker mechanism and measurement scale |

## Transfer and terminal signals

Series such as `mag_pxfm1`, `mag_pxf0` … `mag_pxf6` group as `mag_pxf`. Corresponding phase series include `ph_pxf` and `harm_ph`. Use diagnostics to inspect exact members, data sources and sidebands. Individual legacy names remain selectable.

The selected current-injection path must match the intended circuit role. Output expressions locate raw signals and analyses. Both magnitude and phase are read from the same fresh complex result. Supported forms include `phaseDegUnwrapped(harmonic(getData(...) 0))` and `harmonic(phaseDegUnwrapped(getData(...)) 0)`. Sideband coordinates come from the data. Proven reversed injections retain their sign correction. Missing values, unavailable results or an invalid source are reported directly. Downstream steps show **Blocked by upstream binding** when they depend on a failed mapping.

The fundamental comes from the same complex HB differential output used for phase noise. VGS, VDS and VBS mean `V(G)-V(S)`, `V(D)-V(S)` and `V(B)-V(S)`. Single-ended G or D waveforms can serve as terminal differences only when the source is at the same potential as ground. The tool can compute absent VBS from saved B and S. The private replica retains source voltage VS for floating or moving source nodes.

## PWL files

`Selected VCO HB → four terminals on one time grid → VGS/VDS/VBS and VS → private noise replica`

Each operating point and offset has its own run directory. The relative waveform paths are `waveforms/vgs.pwl`, `vds.pwl`, `vbs.pwl`, and `vs.pwl` inside the replica netlist directory. The corresponding default globals are `ISF_VGS_PWL_FILE`, `ISF_VDS_PWL_FILE`, `ISF_VBS_PWL_FILE`, and `ISF_VS_PWL_FILE`. You can bind different declared names. The waveform manifest records sources, period, samples and file hashes. Missing data, path escapes, invalid grids and changed content block execution or reuse. Move a private netlist with its waveform directory and rediscover Cadence and model paths on the destination host.

## Maestro globals and test overrides

Prefer explicit global definitions for portable design variables and review effective per-test overrides. Preserve intentional differences such as `REF_GATE_PAC_V=1` for the periodic test and local zero for the DC test. Saved local values remain compatible. A global joint sweep applies only when groups, enablement and local overrides support that conclusion. A cached point matching the sweep minimum does not establish precedence.

Check the actual generated netlist after moving a sweep from ADE Explorer to Assembler or moving variables to global scope. A prior migration preserved 25 paired points after reopen. Duplicated native variable declarations can prevent execution. For a `vpwlf` file property use an expression such as `pPar("ISF_VGS_PWL_FILE")` where supported. A TB launched independently in Maestro uses the stored reference files; Explorer runs use their own generated waveforms.

## Limits

The waveform path uses HB/HBNOISE. Unsupported internal bias sources, missing analyses and unreadable expressions block preflight. Qua­si-static Mdc and periodic dynamic sampling are distinct measurements. A renamed sweep sharing one interactive Spectre log can fail strict history ownership checks. If separate evidence is required, generate a new per-point batch history preserving the sweep values. See [reference testbenches](REFERENCE_TESTBENCHES.md).
