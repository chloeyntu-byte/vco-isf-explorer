# VCO ISF Explorer 1.0.0

First public release of the desktop workflow for LC-VCO ISF and flicker up-conversion analysis.

- Native Tk interface in the shared RF-FIELDWORK dark style, with a status bar, a single primary action per page, and dark-matched charts.
- Works offline out of the box: click **载入示例** to explore a synthetic ideal-LC bundle (three operating points, seven offsets).
- Cadence integration is optional and uses *your* Cadence environment, PDK and license; the repository ships no PDK data, model files or simulator results. PDK paths, device names and corners are user configuration.
- Requires Python 3.11+ with Tk 8.6. See the README for install steps and limitations.
- Licensed under MIT.
