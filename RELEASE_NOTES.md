# VCO ISF Explorer 1.2.0

Open **Testbench setup** to prepare an ISF test from a generated Design Lab
project, an existing Maestro setup, or a new cell. The existing-setup entry
shows what the added test keeps or changes before you confirm it.

**Two-device analysis** lets you select both MOS occurrences and choose
**Complementary core** or **Asymmetric pair**. Inspect the estimates and closure
for each selected device. Symmetric-pair extrapolation requires measured
half-period terminal agreement. Flicker transfer projects PM from both sidebands
at the intrinsic drain-to-source port when that port is available.

**Results** now includes nine views (00–08), with a dedicated optimization metrics
view, matched-result comparisons, and table or plot export. Durable jobs retain
evidence across sessions. Storage estimates, archive previews, preflight and
troubleshooting are available from the desktop.

Use `vco-isf metrics` to inspect or compare saved results. Use
`vco-isf optim evaluate`, `gate` and `calibrate` for headless evaluation and
qualification. Structured CLI errors can be consumed by scripts.

Start without EDA using **Load synthetic example** in the dark or light theme.
Native preparation requires the separate VCO TB Generator and your own Cadence,
Spectre and PDK. [Requirements and quick start](README.md)

ISF attribution is scoped to the selected gm devices and supported passive LC
thermal loss. Compare matched sources and verify closure. ISF explains simulated
changes, and a baseline alone cannot predict tuning direction. [Theory](docs/ISF_THEORY.md)
