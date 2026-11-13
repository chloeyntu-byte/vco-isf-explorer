# Input noise bands and Qk presentation

**Input noise band → phase noise** is the main offset-sweep view; **Overview** includes its thumbnail. Band n=|m| identifies the absolute input band of stationary process i_n0 in the cyclostationary source i_n=i_n0·α. It is not a Fourier pairing order of α or Γ.

- n=0 includes the entire coherent flicker sum (two-device power 2·|ΣQk|²) and thermal m=0 power.
- n≥1 combines thermal m=±n power. All independent input bands add in power.
- The bold accent curve sums included bands; the dashed curve is the whole-VCO HBnoise reference. Missing components remain missing and are identified. A partial sum never replaces the original prediction or grants SIGNOFF.
- Legend slopes use least-squares fits of dB against log10(offset) over valid raw points in the lowest two decades of each curve. Reference slopes can be hidden. A single offset shows a point and states that slope fitting is unavailable.
- Table rows are input bands; columns show phase noise and fractions at each offset. Right-click to show all columns. Clicking a curve or legend selects its row; selecting a row emphasizes the curve. Ordinary legends in other views retain visibility controls.

**Harmonic decomposition** shows Qk single-order equivalent bars, projection fractions and the ΣQk complex plane at the selected offset. The projection is c_k=Re(Qk·conj(ΣQ))/|ΣQ|². Signed fractions sum to 1; negative values indicate cancellation. If |Qk|>|ΣQ|, the endpoint is hollow and the hover readout explains cancellation. Fractions are undefined when ΣQ=0.

**Thermal bands** shows independent band powers by source and |m| at the selected offset. Select a source from the list or a table row. One source is plotted at a time; the table drawer retains all sources. Legacy budgets that fail the current evidence contract appear only as labeled unverified diagnostics, outside the main band plot and current calculated total.

In **ISF process**, **Overlay · Fig.14** normalizes Γ, published quasi-static α (m_DC), locally corrected dynamic α (m_dyn), and both Γ·α products by their own peaks. Period means use trapezoidal integration of raw values on the original phase grid. The display compares 20log10(|<Γ·m_DC>|/|<Γ·m_dyn>|) with the summary dc−dynamic difference. This changes neither inputs nor calculation mathematics.

Reference: Hajimiri & Lee, *A General Theory of Phase Noise in Electrical Oscillators*, IEEE JSSC 33(2), 1998, Fig.12(a), Fig.14, Eqs.(19)(23)(27). Flicker k decomposes c0_eff by paired harmonic order; it does not place input noise at k·f0.

Fixtures and bundled examples are formula-generated synthetic multi-offset data, not circuit measurements.

View 04, **Noise contributions**, retains **symmetric gm pair · flicker comparison** as the first comparison row, with HBnoise and ISF bars and the simulated fraction. This aggregate overlaps subsequent source details and must not be added to source powers again. M1/M0 fn identities come from recorded target_noise_trace/peer_noise_trace, not inferred device names. An invalid legacy thermal budget affects thermal eligibility only and must not discard these flicker identities. The sources are marked **included in symmetric gm pair comparison**; single-device predictions are not fabricated.

Fig.14 mean ratios use full-period trapezoidal integration on raw waveforms and their phase grid. Summary differences read dc/dynamic fields independently. The two are displayed without forcing equality. See [ISF_THEORY.md](ISF_THEORY.md) for theory and synthetic examples.
