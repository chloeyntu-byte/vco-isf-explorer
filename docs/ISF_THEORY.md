# ISF theory, local algorithm checks, and presentation conventions

This document explains how the tool relates to Hajimiri–Lee ISF theory, what the local noise-current correction changes, what each decomposition means physically, and how the UI presents it. Section 4 uses reproducible synthetic examples to demonstrate algebraic identities; it does not claim measured circuit validation. See [THERMAL_NOISE.md](THERMAL_NOISE.md) for thermal data contracts and constants, and [THERMAL_REFERENCE.md](THERMAL_REFERENCE.md) for independent reference checks.

Version 1.1.0.

## 1. Reference

A. Hajimiri, T. H. Lee, "A General Theory of Phase Noise in Electrical Oscillators", IEEE JSSC 33(2), 1998. [Author PDF](https://chic.caltech.edu/wp-content/uploads/2013/05/general.pdf) (not bundled).

The paper establishes the theoretical relationship. Numerical conventions for the Fourier constant (c0 versus c0/2), one-sided/two-sided spectra, and carrier amplitude are **not copied directly**. This tool's constants follow its independent reference checks.

## 2. Key derivations in the paper

| Equation / figure | Content |
|---|---|
| (12) | Fourier expansion of the ISF: Γ(ω0τ) = c0/2 + Σ c_n cos(nω0τ + θ_n). |
| (13)–(17), Fig. 8 | For current injected near n·ω0 + Δω, only the c_n integral falls at low frequency, producing sidebands at ±Δω in S_φ: φ ≈ I_n c_n sin(Δωt) / (2 q_max Δω). |
| (18) | Single-tone injection sideband power: P_SBC = 10·log(I_n c_n / (4 q_max Δω))². |
| Fig. 11/12(a) | Noise is grouped by its **input band**: noise near n·f0 is translated into S_φ by c_n. Near-DC 1/f noise produces 1/f³ through c0; white noise in each band produces 1/f². Total PN is the sum of band powers. |
| (19)–(21) | White noise: L = 10·log(Σc_n² · i_n²/Δf / (8 q_max² Δω²)). Parseval's identity, Σc_n² = 2Γ_rms², gives the Γ_rms² form. |
| (22)–(24) | In the 1/f region: L = 10·log(c0² · i_n²/Δf · ω_1/f/Δω / (8 q_max² Δω²)). The 1/f³ corner is ω_1/f · c0²/(2Γ_rms²), usually below the device 1/f corner. |
| (25)–(27), Fig. 13/14 | Cyclostationary noise is i_n(t) = i_n0(t)·α(ω0t), where i_n0 is stationary and α is deterministic periodic modulation. Thus **Γ_eff = Γ·α**. The DC term c0_eff of Γ_eff determines 1/f³ noise. The paper obtains α from device noise characteristics and operating points, using a quasi-static approach. |

## 3. Relationship to the local algorithm

### 3.1 Flicker: Qk expands c0_eff by paired harmonic order

In `calculation.py::harmonic_contributions`:

```text
Q_0 = H_0·M_0
Q_k = H_k·M_k + H_-k·M_k*      (k ≥ 1)
ΣQ  = Σ_k Q_k                  (coherent complex sum)
```

H[k] is the measured complex HBXF transfer (signed, including I/Q); M contains the Fourier coefficients of noise modulation. Parseval gives ΣQ = ⟨h·m⟩, the harmonic expansion of the paper's c0_eff. Therefore:

- **k pairs Γ_k with M_k; it is not an input noise band.** Every Q_k up-converts baseband flicker and therefore follows 1/f³ (−30 dB/dec per order for a pure synthetic 1/f source; see §4).
- k=0 is only the Γ_dc·M_dc term. In a symmetric differential circuit, Γ_dc ≈ 0, so this term is small. The dominant term is often k=1: the f0 component of noise modulation pairs with the ISF fundamental.
- Q_k terms add coherently and may cancel. A single-order equivalent PN can exceed the total. The projected fraction of each order is c_k = Re(Q_k·ΣQ*)/|ΣQ|², with Σc_k = 1. A negative fraction indicates cancellation.

### 3.2 Local noise-current correction: a measured correction to α

| Local quantity | Paper counterpart | Description |
|---|---|---|
| Mdc | Quasi-static α | DC noise sampled at each instantaneous bias point, as in the paper |
| delay | — | Approximation obtained by fitting a uniform delay to Mdc |
| Mdyn (A_k, δ_k) | Measured α | Noise modulation measured by the dynamic replica under periodic bias, with magnitude correction A_k and phase δ_k |
| H[k] (complex I/Q) | Γ (real) | Direct transfer measurement to the PM output, without impulse injection or an assumption that Γ is real |

The local algorithm remains within the paper's framework (Γ_eff = Γ·α, phase noise determined by c0_eff). **It replaces quasi-static α with measured dynamic modulation.** The two descriptions are consistent; **no algorithm change is required**.

### 3.3 Thermal: B_m is the input-band decomposition of Fig. 12(a)

In `thermal.py::calculate_white_thermal`, B_m = Σ_k H_k·a_(k−m), where a contains the Fourier coefficients of √PSD modulation. Here m denotes the **input band of stationary process i_n0**, near m·f0. Paths within one band combine coherently; different bands are uncorrelated and add in power. This corresponds to the paper's c_n decomposition.

Bands must be defined for the stationary part i_n0. After modulation by α, current-noise components at different absolute frequencies are correlated and cannot simply be added in power.

### 3.4 A unified input-band view

Group source noise by n = |m|, independently of the flicker/thermal classification:

- n=0 (near DC): all flicker power associated with ΣQ, plus thermal |m|=0. The low-offset slope is 1/f³, gradually approaching 1/f² at higher offsets.
- n≥1 (near n·f0): thermal |m|=n. Flicker is negligible near GHz frequencies. These bands follow 1/f².
- Different bands are uncorrelated. Their power sum bounds each included band and equals the calculated total, predicted_total, when all required components are available.

## 4. Synthetic example

Everything in this section is synthetic, not measured circuit data. Run `python scripts/theory_example.py` to reproduce the tables.
The script uses the example offset grid and noise corner. A real vector Q=(2, −0.5, 0.25) illustrates cancellation.
Each order's amplitude scales by (1 kHz/f)^1.5, giving a power slope of −30 dB/dec.
Projected fractions sum to 1. If dynamic modulation is twice the quasi-static value, dc−dynamic = −6.020600 dB.
Band n=0 uses A/f³+B/f²; other bands use B_n/f². Their power sum exactly equals the total.

| k | projection (%) | slope (dB/dec) |
|---|---|---|
| 0 | 114.285714 | -30.000000 |
| 1 | -28.571429 | -30.000000 |
| 2 | 14.285714 | -30.000000 |

dc − dynamic = -6.020600 dB (dynamic modulation = 2 × DC).

| offset (Hz) | n=0 power | n=1 power | n=2 power | sum power |
|---|---|---|---|---|
| 1000 | 2.010000e-06 | 5.000000e-09 | 3.333333e-09 | 2.018333e-06 |
| 3000 | 7.518519e-08 | 5.555556e-10 | 3.703704e-10 | 7.611111e-08 |
| 10000 | 2.100000e-09 | 5.000000e-11 | 3.333333e-11 | 2.183333e-09 |
| 30000 | 8.518519e-11 | 5.555556e-12 | 3.703704e-12 | 9.444444e-11 |
| 100000 | 3.000000e-12 | 5.000000e-13 | 3.333333e-13 | 3.833333e-13 |
| 300000 | 1.851852e-13 | 5.555556e-14 | 3.703704e-14 | 2.777778e-13 |
| 1e+06 | 1.200000e-14 | 5.000000e-15 | 3.333333e-15 | 2.033333e-14 |

n=0 asymptotic slopes: −30 dB/dec below the corner, −20 dB/dec above it.
These are analytic synthetic identities, not validation of any physical circuit.

## 5. UI presentation conventions

1. The main phase-noise-versus-offset plot separates **input bands n** (§3.4), without flicker/thermal labels in the band legend. It also shows the power sum of included bands (bold), total HBnoise simulation PN (dashed), −30/−20 dB/dec reference lines, and fitted slopes in the legend.
2. Flicker single-order k curves are not plotted versus offset in the main plot because their slopes are identical. **Harmonic decomposition** shows them at the selected offset: equivalent dBc/Hz and projected fractions c_k as bars, plus the ΣQk complex plane. Table rows represent k and link bidirectionally with the charts.
3. The legacy `result_presentation.py::harmonic_noise_series` both branch combined flicker k and thermal |m|=k into one curve. This has no valid shared physical interpretation and is replaced by input-band decomposition in item 1. The bold calculated total itself was correct.
4. **ISF process** shows operating point → m_DC/m_dyn → Γ-I/Q → Γ×m_dyn on a shared phase axis. **Overlay · Fig.14** plots Γ, published quasi-static α (m_DC), locally corrected dynamic α (m_dyn), and their Γ·α products, each normalized by its own peak. It reports both c0_eff values and their dB difference alongside the summary dc − dynamic value (§4's consistency check). For complex Γ, the carrier-in-phase I branch is the default and Q is shown separately.
5. Flicker/thermal-specific analyses remain in **Noise contributions** and **Thermal bands** (bars by |m|). These are presentation changes only; calculations remain unchanged.

## 6. Scope

Current thermal scope: gm pair + passive LC. Other active devices are not modeled.
Synthetic examples check presentation and algebraic identities; they cannot replace numerical validation of user circuits. Harmonic truncation, phase grid, and noise models still require independent checks.
