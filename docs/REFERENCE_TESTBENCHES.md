# Independent noise reference testbenches

These references test phase reading and actual MOS flicker separately. They can be used alongside the original VCO and noise fixture. Repeat validation after moving a reference to another host or model.

| Cell / Maestro test | Purpose | Use |
|---|---|---|
| TB_ISF_NOISE_REF_ANALYTIC / REF_ANALYTIC | Known signed modulation and phase | Run independently in Maestro and cross-check complex HBAC sidebands against flicker output |
| TB_ISF_NOISE_REF_MOS / REF_MOS_DC | Ordinary DC/noise current measurement | Bind as the noise replica with an explicit completed test/history and review preflight |
| TB_ISF_NOISE_REF_MOS / REF_MOS | Real MOS noise under periodic bias | Inspect HB/HBNOISE/HBAC independently in Maestro |

The standard ISF workflow binds two testbenches, the VCO and either its existing noise TB or compatible REF_MOS_DC.

Select the completed history you intend to analyze. Cadence can retain the old result path for a renamed history. The tool verifies exact names, selected tests and points, netlists and completion logs. Conflicting evidence blocks selection.

## Current readout and source identity

REF_MOS_DC's VGS, VDS and VBS are DC bias variables defined by terminal differences. A zero-volt current sensor in series with the drain feeds a 1 V/A readout. Detection follows the branch to the actual VDS source. Review output, device, noise analysis and cross-TB voltages in **Advanced settings**. Pick explicit names when binding is ambiguous.

The `noise` total may include many mechanisms. Match the selected device's `fn` mechanism, offset and readout. Convert PSD in V²/Hz to ASD in V/√Hz once. Divide ASD by transimpedance R or PSD by R² to obtain the corresponding current quantity.

## Analytic and MOS evidence

The analytic fixture modulates a fixed flicker source with a known signed periodic function. Its deterministic injection uses the same input. It checks complex sideband sign, phase, normalization and frequency coordinates. [Flicker-noise modeling reference](https://kenkundert.com/docs/tcad20-flicker-noise.pdf)

Ideal MOS bias sources can shunt external D/S injection. Gate injection can instead measure a transconductance path. Retain deterministic port tests and actual MOS HBNOISE.fn current as separate evidence until their equivalence is established.

## Phase checks

The flicker sum combines complex harmonic contributions coherently. Displayed equivalent single-order and cumulative dB values should be read as separate projections. The historical current-referenced AM±jPM selection is not a unique phase reconstruction.

Confirm whether HB stores positive-frequency peak amplitudes or two-sided Fourier coefficients. Confirm whether HBAC reports input frequency or output sideband frequency. Read reference passes alongside PM validity, model warnings and phase evidence for the actual VCO.

## Move the fixtures

Copy the reference cells and their waveform, Verilog-A and measurement support together. Rebind models and waveforms, check test, variables, nodes and outputs, then produce and verify a new history. Verify the netlist, polarity, ground, transimpedance, model, completion and required outputs. Retain PSF cross-checks and run sideband/order convergence checks. [Cadence sideband guidance](https://community.cadence.com/cadence_blogs_8/b/rf/posts/guidelines-for-setting-pnoise-hbnoise-sidebands-to-get-accurate-results)
