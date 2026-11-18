# Using VCO TB Generator from ISF Explorer

Testbench entries A, B and C build or extend a Virtuoso/Maestro test through the separate [VCO TB Generator](https://github.com/chloeyntu-byte/vco-tb-generator). ISF Explorer itself never writes OA data or Maestro setups; every write goes through the generator, following its [calling rules](https://github.com/chloeyntu-byte/vco-tb-generator/blob/main/docs/CALLING_RULES.md). The synthetic example, result viewing, metrics and the Maestro-history import in **Setup** do not need the generator.

## 1. Install

Either install it into the same Python environment:

```sh
git clone https://github.com/chloeyntu-byte/vco-tb-generator.git
python -m pip install ./vco-tb-generator
```

or keep a checkout and point ISF Explorer at it (no install needed):

```sh
export VCO_TBGEN_ROOT=/path/to/vco-tb-generator
```

ISF Explorer resolves the generator in this order: the **TB Generator path** field (desktop) or `--tbgen-root` (CLI) / `tbgen_root=` (Python API), then `VCO_TBGEN_ROOT`, then an installed `vco_tbgen` package. When a path is given, the imported module must come from that path, otherwise preparation stops with a module identity error. This release requires TB Generator 1.x with the `vco_tbgen.isf` module.

Check the connection:

```sh
vco-isf capabilities --tbgen-root /path/to/vco-tb-generator --json
```

## 2. Configure Cadence for the generator

The generator runs Virtuoso in batch mode with your own installation and PDK:

| Variable | Purpose |
| --- | --- |
| `TBGEN_VIRTUOSO` | Virtuoso executable, if `virtuoso` is not on `PATH` |
| `TBGEN_ENVIRONMENT_SCRIPT` | Shell script that sets up Cadence, Spectre and license variables |
| `TBGEN_COMMAND_TEMPLATE` | Optional JSON argv wrapper for a site lock or scheduler, e.g. `["site-lock", "--library", "{library}", "--", "{command}"]` |

Use one environment script for both tools so the generator and the ISF solver run under the same setup. Describe your PDK's model includes in a [binding file](https://github.com/chloeyntu-byte/vco-tb-generator/blob/main/docs/BINDINGS.md).

## 3. What each entry asks the generator to do

ISF Explorer always requests completeness level **L-d**: an HB + HBNOISE + HBXF bundle sharing one fundamental, harmonic count, output pair and offset list, a zero-DC probe across the selected gm device's drain and source, and saves for the output pair and device.

| Entry | You provide | Generator calls |
| --- | --- | --- |
| **A · Design Lab** | Generated project directory and exported DUT | `from_design_lab` (fixture), then the L-d steps |
| **B · Existing Maestro** | Library/cell/view of the setup, source test, new test name | Add-test mode on a closed setup, or the live-session transaction from the [Maestro menu](MAESTRO_PLUGIN.md) after you confirm in that window. Original tests, variables, corners and sweeps are backed up, preserved and read back. |
| **C · New cell** | Private `cds.lib`, DUT cell, supplies, controls, multi-bit registers, models, output nets | `browse_libraries` → `describe_cell` → `classify_ports` (you confirm each role), then the L-d steps |

L-d steps: `isf.analysis_bundle` → `isf.injection_for_device` → `isf.install_isf` → `plan` → `materialize` → `run` → `results`.

The selected device needs proven D/S terminals. With **Two-device analysis**, choose **Complementary core** or **Asymmetric pair** and select both devices; each device is analyzed separately and the results are combined as independent noise powers.

## 4. Command line and Python

```sh
vco-isf prepare cell --input request.json --workspace W --tbgen-root TBGEN --json
vco-isf complete --spec W/preparations/ID/HASH.json --decisions decisions.json --json
vco-isf run --spec SPEC --spec-hash HASH --workspace W --request-id ID --wait --json
vco-isf status --job JOB --workspace W --json
```

`prepare` accepts `design-lab`, `maestro` or `cell`. It is offline and returns `ready` or `needs_input` with the fields to confirm. `run` binds to the spec hash, so a changed spec must be prepared again. The Python equivalents are `isf_tool.api.prepare_from_design_lab`, `prepare_from_maestro`, `prepare_from_cell`, `complete` and `run`; see the [integration API](API.md).

## 5. Rules

- Work in a library and workspace you own. Entry B writes only after an explicit confirmation and refuses when the setup is locked or open read-only.
- Do not delete Cadence lock files to get past a refusal; close the session or use the menu path instead.
- Serialize writes and simulations through `TBGEN_COMMAND_TEMPLATE` when tools or licenses are shared.
- Keep at least 2 GiB free disk for native steps.
