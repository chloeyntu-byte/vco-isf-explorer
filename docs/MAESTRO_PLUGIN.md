# Launch from the current Maestro window

Choose **Plug-in → Open VCO ISF Explorer** in the intended Maestro window. The launcher opens an independent Tk window with the calling library, cell, view and selected history.

## Installation

Set `VCO_ISF_HOME` to the checkout directory before loading the menu. The `.cdsinit` can also set `isfMaestroLauncher` to the executable with an explicit path.

```skill
setShellEnvVar("VCO_ISF_HOME=/path/to/vco-isf-explorer")
load("/path/to/vco-isf-explorer/cadence/vco_isf_menu.il")
```

The launcher finds Python 3.11 or newer and Tcl/Tk through its virtual environment, current environment, PATH, and supported local environment locations. It prefers NumPy-enabled interpreters. Set `VCO_ISF_PYTHON` to use an explicit interpreter. The desktop user needs read and execute access to the installation.

The plugin registers just its own menu item with `maestroPluginItemFactories` when available. Otherwise it adds a **VCO ISF** menu. Reload the menu and open a new Maestro window to see changes in an existing Virtuoso session. Restart an existing desktop instance to load new Python code.

## Select and analyze a history

1. Select a completed VCO history. The display shows the native name and the analysis binds its exact RDB. A history selected in Maestro is carried over. With no selection, choose a history explicitly.
2. Choose a target core MOS and offset. The generated fixture uses model and parameters from the selected history. For a manual noise reference, select the independent test and its completed history.
3. Review outputs and device binding, then use **Start analysis**. Opening the menu, refreshing, and selecting histories only read the source.
4. **Refresh History** reloads the same setup and retains a still-valid selection. **Change VCO…** clears the source-specific binding. **Restore launch source** switches back to the original context.

A launch creates a private context and workspace under `~/.vco_isf_explorer/maestro_sessions/session-*`. A command-line launch can use `--workspace`. Use separate workspaces for active desktop instances. Successfully preflighted preferences are source-specific. Reuse of signal overrides requires the same selected history and matching binding and history fingerprints.

The desktop preserves native Ocean, working-directory, PATH and license context for its native refresh process while using a separate Python/Tk process environment. **Environment check** and `vco-isf --diagnose --json` report offline, desktop and native readiness. Read-only detection checks literal tool locations. Native launches and license checkout require verification on the intended host.

## Evidence and context contract

- Capture the current window session at the click and retain the exact setup and history paths. RDB paths and result directories are tracked independently, including external result locations and older selected histories.
- Validate the selected test, PSF data, Spectre terminal receipt, netlist, model, parameters, signals, units and hashes. A menu entry alone proves only the source location.
- Keep historical run parameters separate from the current setup. If the native refresh fails, retain the selected source and show the error.
- Read data on the worker thread and apply current selections on the Tk thread. Discard callbacks from a superseded source.
- The `ISF_MAESTRO_CONTEXT_1` C/H/R/E records use SKILL `%L` strings. Python parses bounded records and removes the consumed temporary file. The context switch is specific to the desktop.

The **existing Maestro** entry can add a new test to the selected setup after review and confirmation. When the target setup is open, choose **Add test in current Maestro and analyze** from the desktop instance launched from that exact window. The transaction preserves source tests and verifies the updated setup before execution in the private copy. [Native API](API.md)

To roll back the launcher, remove its `load` from the project `.cdsinit` or restore the project backup. Close idle desktop instances using the normal close command. Keep the job workspaces and original histories.
