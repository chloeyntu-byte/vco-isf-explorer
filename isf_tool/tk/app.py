"""Native Tk front end for the portable VCO ISF analysis procedure.

The desktop UI talks to :class:`~isf_tool.core.service.ToolService` through
``IsfController`` directly.  No HTTP server is created.  Service calls that
can inspect files, preflight a setup, import evidence, or manage jobs are
serialized by one worker and delivered back to Tk with ``after``.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import io
import json
import math
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

from ..core.limits import MAX_PHASE_CANDIDATE_HARMONICS
from ..core.metrics import metric_catalog, metric_definition
from ..core.result_presentation import (
    is_generated_flicker, operating_points, phase_noise_comparison_series, phase_noise_series,
)
from ..core.ui_state import strip_retired_noise_objects
from .charts import ContributionBars, LineChart, PhasorChart, contribution_bars
from . import fieldwork_tk as fw
from .controller import ACTIVE_JOB_STATES, BackgroundTaskQueue, IsfController
from .preferences import FIELDS as PREFERENCE_FIELDS, mapping_key, preference_path, read_preferences, write_preferences
from .viewmodel import (
    N_A,
    CATALOG_KIND_LABELS,
    active_mapping_rows,
    apply_maestro_context,
    build_result_view,
    catalog_review_rows,
    catalog_summary,
    clean_mapping_overrides,
    cross_tb_review_rows,
    deep_merge,
    flatten_config,
    invalidate_role,
    maestro_pair_payload,
    mapping_candidate_names,
    mapping_review_rows,
    mapping_selection_details,
    mapping_source_text,
    merge_config,
    offset_preview,
    portable_issue_rows,
    result_sample_choices,
    select_history_candidate,
    waveform_exchange_view,
)


DEFAULT_WORKSPACE = Path.home() / ".vco_isf_explorer"
TERMINAL_STATES = frozenset(("succeeded", "failed", "interrupted"))
AUTOMATIC_TARGET = "Automatic detection"

# Retain legacy representative-device values in exported evidence, but do not
# surface them as alternative PN comparisons in the fixed symmetric-pair UI.
LEGACY_DEVICE_COMPARISON_FIELDS = frozenset(
    f"{method}_{suffix}" for method in ("dc", "delay", "dynamic")
    for suffix in ("dBc_Hz", "pm_asd_V_rtHz", "error_dB")
) | frozenset((
    "spectre_fn_dBc_Hz", "spectre_fn_pm_asd_V_rtHz", "spectre_pm_asd_V_rtHz",
    "second_device_predicted_pm_asd_V_rtHz", "harmonic_equivalent_dBc_Hz",
    "cumulative_dBc_Hz", "cumulative_error_dB", "cumulative_error_db",
    "cumulativeErrorDb", "dynamic_error_db", "errorDb", "noise_scope",
))

THEME_PALETTES = {
    # Light is an optional high-brightness mode; dark (the RF-FIELDWORK look) is the default.
    "light": {
        "bg": "#EEF2EC",
        "surface": "#FFFFFF",
        "surface_alt": "#F5F8F2",
        "text": "#1A241C",
        "muted": "#55645A",
        "accent": "#3E7A1F",
        "accent_hover": "#2F6016",
        "accent_text": "#FFFFFF",
        "border": "#D2DACD",
        "selection": "#DDEFCF",
        "success": "#2F8A4B",
        "warning": "#B7791F",
        "danger": "#C0443A",
        "grid": "#E4EAE0",
        "plot_bg": "#FFFFFF",
        "series": None,
    },
    "dark": {
        "bg": fw.BG,
        "surface": fw.PANEL,
        "surface_alt": fw.RAISED,
        "text": fw.TEXT,
        "muted": fw.DIM,
        "accent": fw.accent("isf"),
        "accent_hover": fw._mix(fw.accent("isf"), "#FFFFFF", .15),
        "accent_text": fw.INK,
        "border": fw.LINE,
        "selection": fw._mix(fw.BG, fw.accent("isf"), .35),
        "success": fw.OK,
        "warning": fw.WARN,
        "danger": fw.ERR,
        "grid": fw.tk_plot_colors()["grid"],
        "plot_bg": fw.PLOT_BG,
        "series": tuple(fw.SERIES),
    },
}


def _ui_font_family(families: Sequence[str], fallback: str) -> str:
    """Prefer a Latin sans-serif family with consistent display metrics."""
    available = {name.casefold(): name for name in families}
    for family in ("Inter", "Noto Sans", "Liberation Sans", "DejaVu Sans", "Helvetica"):
        if family.casefold() in available:
            return available[family.casefold()]
    return fallback


class _HelpText(tk.Text):
    """Selectable, read-only guidance with real line spacing and width-driven height."""

    def __init__(self, parent: tk.Misc, *, font: tkfont.Font, text: str = "", textvariable: tk.Variable | None = None) -> None:
        super().__init__(parent, font=font, width=1, height=1, wrap="word", borderwidth=0,
                         highlightthickness=0, padx=0, pady=6, spacing1=3, spacing2=5,
                         spacing3=5, takefocus=False, cursor="arrow", undo=False)
        self._body_font = font
        self._variable = textvariable
        self._trace_id = None
        self._resize_id = None
        if textvariable is not None:
            self._trace_id = textvariable.trace_add("write", self._replace_text)
        self._replace_text(text=text)
        self.bind("<Configure>", self._schedule_resize, add="+")
        self.bind("<Destroy>", self._cleanup, add="+")

    def _replace_text(self, *_args: Any, text: str = "") -> None:
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.insert("1.0", str(self._variable.get()) if self._variable is not None else text)
        self.configure(state="disabled")
        self._schedule_resize()

    def _schedule_resize(self, _event: Any = None) -> None:
        if self._resize_id is None:
            self._resize_id = self.after_idle(self._fit_height)

    def _fit_height(self) -> None:
        self._resize_id = None
        if self.winfo_width() <= 1:
            return
        pixels = self.count("1.0", "end", "ypixels")
        height = max(1, math.ceil((pixels[0] if pixels else 0) / self._body_font.metrics("linespace")))
        if int(self.cget("height")) != height:
            self.configure(height=height)

    def _cleanup(self, _event: Any) -> None:
        if self._trace_id is not None and self._variable is not None:
            self._variable.trace_remove("write", self._trace_id)
            self._trace_id = None
        if self._resize_id is not None:
            self.after_cancel(self._resize_id)
            self._resize_id = None


def _display(value: Any) -> str:
    if value is None or value == N_A:
        return N_A
    if isinstance(value, float):
        return f"{value:.7g}"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _error_message(exc: Exception) -> str:
    code = getattr(exc, "code", "")
    details = getattr(exc, "details", None)
    text = f"{code}: {exc}" if code else str(exc)
    if details:
        text += "\n\n" + json.dumps(details, ensure_ascii=False, indent=2, default=str)
    return text


def _write_json(path: str, value: Mapping[str, Any]) -> None:
    target = Path(path).expanduser()
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with io.open(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write_csv(path: str, rows: Sequence[Mapping[str, Any]]) -> None:
    normalized = [dict(row) for row in rows if isinstance(row, Mapping)]
    columns: list[str] = []
    for row in normalized:
        for key in row:
            key = str(key)
            if key not in columns:
                columns.append(key)
    target = Path(path).expanduser()
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with io.open(
            descriptor, "w", encoding="utf-8", newline="", closefd=True
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=columns or ["value"])
            writer.writeheader()
            for row in normalized:
                output = {}
                for key in columns:
                    value = row.get(key)
                    output[key] = (
                        json.dumps(value, ensure_ascii=False, sort_keys=True)
                        if isinstance(value, (dict, list, tuple))
                        else value
                    )
                writer.writerow(output)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def active_jobs(jobs_payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return active jobs for the close guard without mutating the payload."""

    payload = _mapping(jobs_payload)
    return [
        dict(item)
        for item in _sequence(payload.get("jobs"))
        if isinstance(item, Mapping) and str(item.get("status")) in ACTIVE_JOB_STATES
    ]


def history_provenance_rows(report: Mapping[str, Any]) -> list[dict[str, str]]:
    """Display exact per-point history ownership without inferring current OA."""
    rows = []
    for key, label in (("vco", "VCO"), ("flicker", "Noise TB")):
        role = _mapping(_mapping(report.get("roles")).get(key))
        if not role:
            continue
        design = _mapping(_mapping(role.get("selected_test")).get("design"))
        current = tuple(str(design.get(name) or (design.get("library") if name == "lib" else "") or "Unknown") for name in ("lib", "cell", "view"))
        history = _mapping(role.get("selected_history"))
        manifest = _mapping(role.get("history_manifest")) or _mapping(history.get("history_manifest")) or _mapping(history.get("manifest"))
        entries = manifest.get("source_netlists", history.get("source_netlists", []))
        sources = [item for item in _sequence(entries) if isinstance(item, Mapping)] or [{}]
        for source in sources:
            design = _mapping(source.get("source_design"))
            old = tuple(str(design.get(name) or (design.get("library") if name == "lib" else "") or "Unknown") for name in ("lib", "cell", "view"))
            modified = source.get("source_mtime")
            if isinstance(modified, (int, float)) and not isinstance(modified, bool):
                try:
                    modified = datetime.fromtimestamp(modified).astimezone().isoformat(timespec="seconds")
                except (ValueError, ArithmeticError, OSError):
                    modified = "Unknown"
            known = "Unknown" not in old and "Unknown" not in current
            status = "View names match; content follows History is authoritative" if known and current == old else "differs from the current design; still uses History Run" if known else "source was not fully recorded, not inferred"
            rows.append({"role": label, "current": "/".join(current), "history": "/".join(old),
                         "point": f"{source.get('point_id', source.get('point', 'Unknown'))} / {source.get('corner', 'Unknown')}",
                         "mtime": str(modified or "Unknown"), "sha256": str(source.get("sha256") or "Unknown"), "status": status})
    return rows


class IsfApplication:
    """Four-tab native desktop interface backed by one ``ToolService``."""

    def __init__(self, root: tk.Tk, controller: IsfController, *, maestro_context: Mapping[str, Any] | None = None,
                 preferences_directory: Path | None = None):
        self.root = root
        self.controller = controller
        self.queue = BackgroundTaskQueue(root)
        self.raw_config: dict[str, Any] = {}
        self.current_job_id = ""
        self.current_job: dict[str, Any] = {}
        self.current_result: dict[str, Any] | None = None
        self.current_result_job_id = ""
        self.current_result_view: dict[str, Any] | None = None
        self._result_load_pending_job_id = ""
        self.last_preflight: dict[str, Any] | None = None
        self._poll_pending = False
        self._poll_after_id: str | None = None
        self._close_check_pending = False
        self._job_command_pending = False
        self._foreground_operations = 0
        self._state_load_pending = False
        self._page_canvases: dict[str, tk.Canvas] = {}
        self._updating_form = False
        self._error_entries: dict[str, dict[str, Any]] = {}
        self._mapping_overrides: dict[str, dict[str, Any]] = {"vco": {}, "replica": {}}
        self._portable_overrides: dict[str, Any] = {}
        self._signal_report: dict[str, Any] = {}
        self._mapping_rows: dict[str, dict[str, Any]] = {}
        self._mapping_inputs: dict[str, ttk.Combobox] = {}
        self._mapping_input_vars: dict[str, tk.StringVar] = {}
        self._mapping_status_labels: dict[str, ttk.Label] = {}
        self._rendering_mapping_inputs = False
        self._portable_rows: dict[str, dict[str, Any]] = {}
        self._catalog_rows: dict[str, dict[str, Any]] = {}
        self._cross_tb_rows: dict[str, dict[str, Any]] = {}
        self._selection_revision = 0
        self._auto_engine_path = ""
        self._help_widgets: list[_HelpText] = []
        self._preflight_issues: list[dict[str, Any]] = []
        self._preflight_messages: list[str] = []
        self.maestro_context = deep_merge({}, maestro_context or {})
        self._context_mode = bool(self.maestro_context)
        self._preferences_path = preference_path(preferences_directory, self.maestro_context) if preferences_directory else None
        self._preferences: dict[str, Any] = {}
        self._restored_mapping_keys: set[str] = set()
        self._preferences_saving = False
        self.preference_status_var = tk.StringVar(value="Project preferences save only offsets, Sample, theme and validated VCO Mapping. ")
        self._history_choices: dict[str, dict[str, str]] = {"vco": {}, "replica": {}}
        self._history_display_vars = {role: tk.StringVar() for role in ("vco", "replica")}
        self._inspection_after_id: str | None = None
        self._last_auto_selection: tuple[str, ...] | None = None
        self._observed_role_paths = {"vco": "", "replica": ""}
        self.maestro_source_var = tk.StringVar()

        self.vars: dict[str, tk.Variable] = {
            "project_name": tk.StringVar(),
            "project_root": tk.StringVar(),
            "vco_path": tk.StringVar(),
            "vco_test": tk.StringVar(),
            "vco_history": tk.StringVar(),
            "vco_binding_fingerprint": tk.StringVar(),
            "vco_history_fingerprint": tk.StringVar(),
            "replica_source": tk.StringVar(value="generated"),
            "replica_path": tk.StringVar(),
            "replica_test": tk.StringVar(),
            "replica_history": tk.StringVar(),
            "replica_binding_fingerprint": tk.StringVar(),
            "replica_history_fingerprint": tk.StringVar(),
            "offset_value": tk.StringVar(value="10"),
            "offset_unit": tk.StringVar(value="kHz"),
            "offset_mode": tk.StringVar(value="single"),
            "offset_values": tk.StringVar(value="1k,10k,1M"),
            "offset_start": tk.StringVar(value="1k"),
            "offset_stop": tk.StringVar(value="100M"),
            "offset_spacing": tk.StringVar(value="step"),
            "offset_step": tk.StringVar(value="1k"),
            "offset_intervals": tk.StringVar(value="5"),
            "offset_points_per_decade": tk.StringVar(value="1"),
            "max_harmonic": tk.StringVar(value="3"),
            "phase_samples": tk.StringVar(value="512"),
            "period_count": tk.StringVar(value="8"),
            "engine_config": tk.StringVar(),
            "run_root": tk.StringVar(),
            "bundle_path": tk.StringVar(),
            "pdk_name": tk.StringVar(),
            "pdk_root": tk.StringVar(),
            "analysis_python": tk.StringVar(),
        }
        self.mode_var = tk.StringVar(value="live")
        self.formal_signoff_var = tk.BooleanVar(value=False)
        self.allow_execute_var = tk.BooleanVar(value=False)
        self.theme_var = tk.StringVar(value="dark")
        self.advanced_visible_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Reading local configuration…")
        self.execution_summary_var = tk.StringVar(value="Live execution · not signoff")
        self.preflight_note_var = tk.StringVar(
            value="Preflight checks bindings and briefly probes the selected Cadence environment and analysis Python without starting simulation. "
            "Start runs preflight and then invokes Spectre. Binding and saving configuration do not run simulations or save execution consent. "
        )
        self.binding_status_var = tk.StringVar(value="Select a completed VCO History. ")
        self.binding_provenance_var = tk.StringVar(value="Simulation copies the fixed netlist from the selected History. It does not switch to the current schematic / config. Source views, timestamps and hashes are available after binding. ")
        self.mapping_status_var = tk.StringVar(value="Signals and execution settings are detected after binding; Details and manual edits are in Advanced settings. ")
        self.mapping_detail_var = tk.StringVar(value="Select a candidate or enter an actual name in each row. Input is retained; click “Apply all & validate” to validate it. ")
        self.waveform_exchange_var = tk.StringVar()
        self.portable_edit_var = tk.StringVar()
        self.portable_detail_var = tk.StringVar(value="Execution settings are generated after binding; Select a row to override. ")
        self.catalog_filter_var = tk.StringVar()
        self.catalog_kind_var = tk.StringVar(value="All types")
        self.catalog_status_var = tk.StringVar(value="After binding, inspect both TB net names, Maestro Outputs and Variables; includes more than VGS / VDS / VBS. ")
        self.catalog_detail_var = tk.StringVar(value="The inventory preserves discovered names; Select a row for the full expression and source. ")
        self.preflight_status_var = tk.StringVar(value="Preflight not run")
        self.preflight_detail_var = tk.StringVar()
        self.job_status_var = tk.StringVar(value="No job selected")
        self.point_var = tk.StringVar()
        self.operating_point_var = tk.StringVar()
        self.result_offset_var = tk.StringVar()
        # Results always use the pair model.  Keep the variable only as an
        # internal compatibility adapter, not as a user-selectable scope.
        self.noise_scope_var = tk.StringVar(value="pair")
        self.noise_kind_var = tk.StringVar(value="both")
        self.log_offset_var = tk.BooleanVar(value=True)
        self.harmonic_curve_var = tk.StringVar(value="")
        self.metric_choice_var = tk.StringVar()
        self.offset_preview_var = tk.StringVar(value="Expanding offset list…")
        self.show_all_mappings_var = tk.BooleanVar(value=False)
        self._operating_points: list[dict[str, Any]] = []
        self._offset_point_choices: dict[str, str] = {}
        self.metrics_var = tk.StringVar(value="No results yet")
        self.result_quality_var = tk.StringVar(value="Metric details: click a table cell or heading, or select a metric below and press Enter. ")
        self.noise_contribution_note_var = tk.StringVar(value="Select a result operating point and offset to show simulation contributions. ")
        self.noise_pie_note_var = tk.StringVar(value="Pie charts follow the operating point, offset and noise kind. ")
        self._result_terminal_loaded = False
        self._curve_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._request_close)
        for role in ("vco", "replica"):
            for suffix in ("path", "test", "history"):
                self.vars[f"{role}_{suffix}"].trace_add(
                    "write", lambda *_args, current=role: self._mark_binding_dirty(current)
                )
        self.vars["engine_config"].trace_add("write", self._engine_changed)
        for name, variable in self.vars.items():
            if name.startswith("offset_"):
                variable.trace_add("write", self._offset_inputs_changed)
        self._submit(
            "Read configuration",
            self._read_initial_inputs,
            self._initial_inputs_loaded,
        )
        self._submit("Read error catalog", self.controller.error_catalog, self._render_error_catalog)
        self._refresh_jobs(silent=True)

    # ---- general UI -------------------------------------------------

    def _configure_fonts(self) -> None:
        """Keep the kit's sizes and use a Latin sans-serif family."""
        default_font = tkfont.nametofont("TkDefaultFont", root=self.root)
        family = getattr(self, "ui_family", "") or _ui_font_family(
            tkfont.families(self.root), str(default_font.actual("family")))
        self.ui_family = family
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkTooltipFont"):
            tkfont.nametofont(name, root=self.root).configure(family=family)
        fonts = getattr(self.root, "fw_fonts", None)
        if isinstance(fonts, dict):
            for key, value in list(fonts.items()):
                if key != "mono" and isinstance(value, tuple):
                    fonts[key] = (family,) + tuple(value[1:])
        if not hasattr(self, "body_font"):
            self.body_font = tkfont.Font(self.root, family=family, size=10)
            self.help_font = tkfont.Font(self.root, family=family, size=10)
            self.heading_font = tkfont.Font(self.root, family=family, size=10, weight="bold")
            self.title_font = tkfont.Font(self.root, family=family, size=14, weight="bold")
            self.fixed_font = tkfont.Font(self.root, **tkfont.nametofont("TkFixedFont", root=self.root).actual())
            self.fixed_font.configure(size=9)

    def _build_ui(self) -> None:
        self.root.geometry("1440x900")
        self.root.minsize(1280, 800)
        fw.apply(self.root, product="isf")
        self._configure_fonts()
        self.style = ttk.Style(self.root)
        self._set_window_title()

        def header_actions(box: tk.Frame) -> None:
            self.theme_button = ttk.Button(box, text="Light theme", style="Ghost.TButton", command=self._toggle_theme)
            self.theme_button.pack(side="left")
            self.save_state_button = ttk.Button(box, text="Save state…", command=self._save_state_dialog)
            self.save_state_button.pack(side="left", padx=(8, 0))
            self.load_state_button = ttk.Button(box, text="Load state…", command=self._load_state_dialog)
            self.load_state_button.pack(side="left", padx=(8, 0))

        self.header_bar = fw.header(
            self.root, "VCO ISF Explorer", "Oscillator phase-noise analysis · ISF and harmonic contributions",
            product="isf", right=header_actions)
        self.header_bar.pack(fill="x", side="top")

        # Bottom status bar: state chip + one concrete message (see _sync_status_chip).
        self.status_bar = tk.Frame(self.root, bg=fw.PANEL)
        self.status_bar.pack(fill="x", side="bottom")
        tk.Frame(self.status_bar, bg=fw.LINE, height=1).pack(fill="x", side="top")
        self.status_chip = fw.StatusChip(self.status_bar)
        self.status_chip.pack(side="left", padx=16, pady=8)
        self.status_progress = ttk.Progressbar(self.status_bar, mode="indeterminate", length=160)
        self.status_var.trace_add("write", self._sync_status_chip)
        self._sync_status_chip()

        outer = ttk.Frame(self.root, style="App.TFrame", padding=(16, 12))
        outer.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(outer, style="App.TNotebook")
        self.notebook.pack(fill="both", expand=True)
        self.setup_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=12)
        self.run_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=12)
        self.result_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=8)
        self.error_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=12)
        self.notebook.add(self.setup_tab, text="Setup")
        self.notebook.add(self.run_tab, text="Jobs & preflight")
        self.notebook.add(self.result_tab, text="Results")
        self.notebook.add(self.error_tab, text="Troubleshooting")
        self.notebook.enable_traversal()
        self._build_setup_tab()
        self._build_run_tab()
        self._build_result_tab()
        self._build_error_tab()
        self._apply_theme()
        self._mode_changed()

    def _set_window_title(self, name: str = "") -> None:
        self.root.title("VCO ISF Explorer — " + name if name else "VCO ISF Explorer")

    def _sync_status_chip(self, *_args: Any) -> None:
        """Map the free-text status onto the idle/running/done/warn/error chip."""
        text = str(self.status_var.get())
        if text.endswith("…"):
            state = "running"
        elif text.endswith("failed") or "failed: " in text:
            state = "error"
        elif text.endswith("done"):
            state = "done"
        else:
            state = "idle"
        self.status_chip.set(state, text)
        if state == "running":
            if not self.status_progress.winfo_manager():
                self.status_progress.pack(side="right", padx=16)
            self.status_progress.start(60)
        elif self.status_progress.winfo_manager():
            self.status_progress.stop()
            self.status_progress.pack_forget()

    def _toggle_theme(self) -> None:
        self.theme_var.set("dark" if self.theme_var.get() == "light" else "light")
        self._apply_theme()

    def _apply_theme(self) -> None:
        palette = THEME_PALETTES[self.theme_var.get()]
        # Reset to the shared RF-FIELDWORK styles (Primary/Ghost/Danger/Section ...) first;
        # the palette-driven overrides below only adapt the app-specific styles and the
        # optional light mode.
        fw.apply(self.root, product="isf")
        self._configure_fonts()
        style = self.style
        self.root.configure(background=palette["bg"])
        style.configure(".", font=self.body_font)
        style.configure("App.TFrame", background=palette["bg"])
        style.configure("TFrame", background=palette["surface"])
        style.configure("TLabel", background=palette["surface"], foreground=palette["text"])
        style.configure(
            "Title.TLabel",
            background=palette["bg"],
            foreground=palette["text"],
            font=self.title_font,
        )
        style.configure(
            "Subtitle.TLabel", background=palette["bg"], foreground=palette["muted"]
        )
        style.configure(
            "Status.TLabel", background=palette["bg"], foreground=palette["muted"]
        )
        style.configure("Help.TLabel", foreground=palette["muted"], font=self.help_font, padding=(0, 3))
        style.configure("Setup.TLabel", foreground=palette["text"], font=self.heading_font)
        style.configure(
            "Card.TLabelframe",
            background=palette["surface"],
            bordercolor=palette["border"],
            lightcolor=palette["border"],
            darkcolor=palette["border"],
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=palette["surface"],
            foreground=palette["text"],
            font=self.heading_font,
        )
        style.configure(
            "TButton",
            background=palette["surface_alt"],
            foreground=palette["text"],
            bordercolor=palette["border"],
            padding=(12, 7),
            relief="flat",
        )
        style.map(
            "TButton",
            background=[("active", palette["selection"]), ("disabled", palette["surface_alt"])],
            foreground=[("disabled", palette["muted"])],
        )
        if self.theme_var.get() == "light":
            style.configure(
                "Primary.TButton",
                background=palette["accent"],
                foreground=palette["accent_text"],
                bordercolor=palette["accent"],
                lightcolor=palette["accent"],
                darkcolor=palette["accent"],
                padding=(15, 8),
                font=self.heading_font,
            )
            style.map(
                "Primary.TButton",
                background=[("active", palette["accent_hover"]), ("disabled", palette["border"])],
                foreground=[("disabled", palette["muted"])],
                bordercolor=[("disabled", palette["border"])],
            )
        style.configure(
            "Secondary.TButton",
            background=palette["surface_alt"],
            foreground=palette["text"],
            bordercolor=palette["border"],
        )
        style.configure(
            "TEntry",
            fieldbackground=palette["surface_alt"],
            foreground=palette["text"],
            insertcolor=palette["text"],
            bordercolor=palette["border"],
            lightcolor=palette["border"],
            darkcolor=palette["border"],
            padding=6,
        )
        style.configure(
            "TCombobox",
            fieldbackground=palette["surface_alt"],
            background=palette["surface_alt"],
            foreground=palette["text"],
            arrowcolor=palette["muted"],
            bordercolor=palette["border"],
            lightcolor=palette["border"],
            darkcolor=palette["border"],
            padding=5,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", palette["surface_alt"])],
            foreground=[("readonly", palette["text"])],
            selectbackground=[("readonly", palette["surface_alt"])],
            selectforeground=[("readonly", palette["text"])],
        )
        for name in ("TCheckbutton", "TRadiobutton"):
            style.configure(name, background=palette["surface"], foreground=palette["text"])
            style.map(name, background=[("active", palette["surface_alt"])])
        style.configure("TSpinbox", fieldbackground=palette["surface_alt"], foreground=palette["text"],
                        background=palette["surface_alt"], arrowcolor=palette["muted"],
                        bordercolor=palette["border"], lightcolor=palette["border"],
                        darkcolor=palette["border"], padding=5)
        style.configure("TScrollbar", background=palette["surface_alt"], troughcolor=palette["bg"],
                        arrowcolor=palette["muted"], bordercolor=palette["border"],
                        lightcolor=palette["border"], darkcolor=palette["border"], gripcount=0)
        style.configure("TNotebook", background=palette["surface_alt"], bordercolor=palette["border"])
        style.configure("TNotebook.Tab", background=palette["surface_alt"], foreground=palette["muted"], padding=(10, 6))
        style.map("TNotebook.Tab", background=[("selected", palette["surface"]), ("active", palette["selection"])],
                  foreground=[("selected", palette["accent"]), ("active", palette["text"])])
        style.configure("TMenubutton", background=palette["surface_alt"], foreground=palette["text"],
                        bordercolor=palette["border"], padding=(10, 5))
        style.map("TMenubutton", background=[("active", palette["selection"])])
        style.configure(
            "App.TNotebook",
            background=palette["bg"],
            bordercolor=palette["border"],
            lightcolor=palette["border"],
            darkcolor=palette["border"],
            borderwidth=0,
            tabmargins=(0, 0, 0, 8),
        )
        style.configure(
            "App.TNotebook.Tab",
            background=palette["bg"],
            foreground=palette["muted"],
            padding=(18, 10),
            bordercolor=palette["border"],
            lightcolor=palette["border"],
            darkcolor=palette["border"],
            borderwidth=0,
            font=self.heading_font,
        )
        style.map(
            "App.TNotebook.Tab",
            background=[("selected", palette["surface"]), ("active", palette["surface_alt"])],
            foreground=[("selected", palette["accent"]), ("active", palette["text"])],
        )
        style.configure(
            "Treeview",
            background=palette["surface"],
            fieldbackground=palette["surface"],
            foreground=palette["text"],
            bordercolor=palette["border"],
            rowheight=max(28, self.body_font.metrics("linespace") + 12),
        )
        style.map(
            "Treeview",
            background=[("selected", palette["selection"])],
            foreground=[("selected", palette["text"])],
        )
        style.configure(
            "Treeview.Heading",
            background=palette["surface_alt"],
            foreground=palette["muted"],
            bordercolor=palette["border"],
            padding=(8, 7),
            font=self.heading_font,
        )
        style.configure(
            "Horizontal.TProgressbar",
            thickness=6,
            background=palette["accent"],
            troughcolor=palette["surface_alt"],
            bordercolor=palette["border"],
            lightcolor=palette["accent"],
            darkcolor=palette["accent"],
        )
        for widget in (getattr(self, "raw_text", None), getattr(self, "job_log", None)):
            if widget is not None:
                widget.configure(
                    background=palette["surface_alt"],
                    foreground=palette["text"],
                    insertbackground=palette["text"],
                    selectbackground=palette["selection"],
                    selectforeground=palette["text"],
                    highlightbackground=palette["border"],
                    highlightcolor=palette["accent"],
                    relief="flat",
                )
        for canvas in self._page_canvases.values():
            canvas.configure(background=palette["bg"])
        if hasattr(self, "mapping_canvas"):
            self.mapping_canvas.configure(background=palette["surface"])
        for widget in self._help_widgets:
            widget.configure(background=palette["surface"], foreground=palette["muted"],
                             selectbackground=palette["selection"], selectforeground=palette["text"])
        for tree in (
            getattr(self, "preflight_tree", None),
            getattr(self, "jobs_tree", None),
            getattr(self, "summary_tree", None),
            getattr(self, "harmonic_tree", None),
            getattr(self, "noise_contribution_tree", None),
            getattr(self, "error_tree", None),
        ):
            if tree is not None:
                tree.tag_configure("pass", foreground=palette["success"])
                tree.tag_configure("warn", foreground=palette["warning"])
                tree.tag_configure("fail", foreground=palette["danger"])
        chart_palette = {
            "background": palette["plot_bg"],
            "text": palette["text"],
            "muted": palette["muted"],
            "grid": palette["grid"],
            "border": palette["border"],
            "accent": palette["accent"],
            "accent_alt": palette["warning"],
            "selection": palette["text"],
        }
        if palette["series"]:
            chart_palette["series"] = palette["series"]
        if hasattr(self, "results_view"):
            self.results_view.set_palette(chart_palette)
        for chart in (getattr(self, "isf_chart", None), getattr(self, "qk_chart", None), getattr(self, "phase_noise_chart", None), getattr(self, "phase_error_chart", None), getattr(self, "harmonic_noise_chart", None), getattr(self, "simulated_noise_pie", None), getattr(self, "calculated_noise_pie", None)):
            if chart is not None:
                chart.set_palette(chart_palette)
        self.theme_button.configure(
            text="Light theme" if self.theme_var.get() == "dark" else "Dark theme"
        )

    def _submit(
        self,
        label: str,
        function: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
        *,
        quiet: bool = False,
        on_failure: Callable[[Exception], None] | None = None,
    ) -> None:
        if not quiet:
            self._foreground_operations = getattr(self, "_foreground_operations", 0) + 1
        if not quiet:
            self.status_var.set(f"{label}…")

        def success(value: Any) -> None:
            if not quiet:
                self._foreground_operations -= 1
            try:
                if on_success is not None:
                    on_success(value)
                if not quiet:
                    self.status_var.set(f"{label} done")
            except Exception as exc:
                self._show_error(exc, f"{label}: result processing failed")

        def failure(exc: Exception) -> None:
            if not quiet:
                self._foreground_operations -= 1
            if on_failure is not None:
                try:
                    on_failure(exc)
                except Exception as callback_error:
                    self._show_error(callback_error, f"{label}: failure handler error")
            if not quiet:
                self.status_var.set(f"{label} failed")
            self._show_error(exc, f"{label} failed")

        self.queue.submit(function, success, failure)

    def _show_error(self, exc: Exception, title: str = "Operation failed") -> None:
        messagebox.showerror(title, _error_message(exc), parent=self.root)

    @staticmethod
    def _clear_tree(tree: ttk.Treeview) -> None:
        children = tree.get_children()
        if children:
            tree.delete(*children)

    @staticmethod
    def _tree(parent: tk.Misc, columns: Sequence[tuple[str, str, int]]) -> ttk.Treeview:
        names = [item[0] for item in columns]
        tree = ttk.Treeview(parent, columns=names, show="headings", selectmode="browse")
        for name, heading, width in columns:
            tree.heading(name, text=heading)
            tree.column(name, width=width, minwidth=55, stretch=True)
        return tree

    def _help(self, parent: tk.Misc, *, text: str = "", textvariable: tk.Variable | None = None) -> _HelpText:
        widget = _HelpText(parent, font=self.help_font, text=text, textvariable=textvariable)
        self._help_widgets.append(widget)
        return widget

    @staticmethod
    def _with_scroll(parent: tk.Misc, widget: tk.Widget) -> ttk.Frame:
        frame = ttk.Frame(parent)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=widget.xview)
        horizontal.pack(side="bottom", fill="x")
        widget.configure(xscrollcommand=horizontal.set)
        widget.pack(in_=frame, side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
        scrollbar.pack(side="right", fill="y")
        widget.configure(yscrollcommand=scrollbar.set)
        # pack(in_=...) changes geometry ownership, not the Tk parent.  The
        # newly-created sibling frame would otherwise obscure the older tree.
        widget.tkraise(frame)
        return frame

    # ---- setup ------------------------------------------------------

    def _scrollable_page(self, tab: ttk.Frame, *, minimum_width: int = 940, fit_width: bool = False) -> ttk.Frame:
        """Keep natural content height, with page-level two-axis scrolling."""
        canvas = tk.Canvas(tab, highlightthickness=0, borderwidth=0, yscrollincrement=24)
        vertical = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        horizontal = ttk.Scrollbar(tab, orient="horizontal", command=canvas.xview)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        content = ttk.Frame(canvas, style="App.TFrame")
        window = canvas.create_window((0, 0), window=content, anchor="nw")

        geometry = None

        def resize(_event: Any = None) -> None:
            nonlocal geometry
            viewport = canvas.winfo_width()
            natural = 1 if fit_width else content.winfo_reqwidth()
            desired = max(minimum_width, viewport, natural)
            if fit_width:
                if desired <= viewport:
                    horizontal.grid_remove()
                else:
                    horizontal.grid()
            measured = (desired, content.winfo_height())
            if measured == geometry:
                return
            geometry = measured
            canvas.itemconfigure(window, width=desired)
            canvas.configure(scrollregion=canvas.bbox("all"))

        content.bind("<Configure>", resize)
        canvas.bind("<Configure>", resize)
        self._page_canvases[str(tab)] = canvas
        return content

    def _build_setup_tab(self) -> None:
        content = self._scrollable_page(self.setup_tab, minimum_width=0)
        self.setup_canvas = self._page_canvases[str(self.setup_tab)]
        self.root.bind_all("<MouseWheel>", self._scroll_setup, add="+")
        self.root.bind_all("<Button-4>", self._scroll_setup, add="+")
        self.root.bind_all("<Button-5>", self._scroll_setup, add="+")

        example = ttk.Frame(content, padding=(12, 0))
        example.pack(fill="x", pady=(0, 4))
        self.load_example_button = ttk.Button(example, text="Load example", command=self._load_example)
        self.load_example_button.pack(side="right")
        example_text = ttk.Label(
            example, text="No Cadence Environment？Load synthetic ideal-LC Example, inspect all result views offline. ",
            style="Help.TLabel", justify="left", wraplength=360,
        )
        example_text.pack(side="left", fill="x", expand=True)
        example_text.bind("<Configure>", lambda event: example_text.configure(wraplength=max(200, event.width - 8)))

        location = ttk.LabelFrame(
            content, text="optional: Discover project Testbench", style="Card.TLabelframe", padding=(14, 12)
        )
        location.columnconfigure(1, weight=1)
        ttk.Label(location, text="Project root").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 6)
        )
        ttk.Entry(location, textvariable=self.vars["project_root"]).grid(
            row=0, column=1, sticky="ew", pady=(0, 6)
        )
        ttk.Button(
            location,
            text="Choose directory",
            style="Secondary.TButton",
            command=lambda: self._choose_directory("project_root"),
        ).grid(row=0, column=2, padx=(8, 0), pady=(0, 6))
        ttk.Button(
            location,
            text="Discover Testbench",
            command=self._discover,
        ).grid(row=0, column=3, padx=(8, 0), pady=(0, 6))
        ttk.Label(
            location,
            text="scan this directory only to identify Maestro, the original project is unchanged. ",
            style="Help.TLabel",
        ).grid(row=1, column=1, columnspan=3, sticky="w")
        ttk.Label(
            location,
            text=f"Application data: {self.controller.workspace}",
            style="Help.TLabel",
        ).grid(row=2, column=1, columnspan=3, sticky="w", pady=(3, 0))

        pair = ttk.LabelFrame(content, text="01  VCO Source", style="Card.TLabelframe", padding=(14, 9))
        pair.pack(fill="x", pady=(0, 8))
        pair.columnconfigure(1, weight=1)
        if self.maestro_context:
            ttk.Label(pair, textvariable=self.maestro_source_var, style="Setup.TLabel", wraplength=350).grid(
                row=0, column=0, columnspan=2, sticky="w", pady=(0, 6)
            )
            source_actions = ttk.Frame(pair)
            source_actions.grid(row=0, column=2, columnspan=2, sticky="e", pady=(0, 6))
            ttk.Button(source_actions, text="Refresh History", command=self._refresh_context_histories).pack(side="left")
            ttk.Button(source_actions, text="Change VCO…", command=self._toggle_vco_source).pack(side="left", padx=(6, 0))
            self._update_maestro_banner()
        ttk.Label(pair, text="Complete History").grid(row=1, column=0, sticky="w", padx=(0, 10))
        self._maestro_box(pair, "vco", "history").grid(row=1, column=1, sticky="ew")
        ttk.Label(pair, text="Test").grid(row=1, column=2, sticky="w", padx=10)
        self._maestro_box(pair, "vco", "test", width=18).grid(row=1, column=3, sticky="ew")
        self.vco_source_controls = ttk.Frame(pair)
        self.vco_source_controls.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        self._build_maestro_path(self.vco_source_controls, "vco")
        if self.maestro_context:
            ttk.Button(self.vco_source_controls, text="Restore launch source", command=self._restore_maestro_context).pack(
                side="right", padx=(6, 0)
            )
            self.vco_source_controls.grid_remove()
        reference = ttk.LabelFrame(content, text="02  Device noise reference", style="Card.TLabelframe", padding=(14, 9))
        reference.pack(fill="x", pady=(0, 8))
        reference_mode = ttk.Frame(reference)
        reference_mode.pack(fill="x", pady=(0, 6))
        for label, mode in (("Generated", "generated"), ("Manual binding Noise TB", "maestro")):
            ttk.Radiobutton(reference_mode, text=label, value=mode, variable=self.vars["replica_source"],
                            command=self._replica_source_changed).pack(side="left", padx=(0, 14))

        self.generated_reference_frame = ttk.Frame(reference)
        self.generated_reference_frame.columnconfigure(1, weight=1)
        ttk.Label(self.generated_reference_frame, text="Target MOS").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.target_mos_var = tk.StringVar(value=AUTOMATIC_TARGET)
        self.target_mos_box = ttk.Combobox(self.generated_reference_frame, textvariable=self.target_mos_var,
                                          width=23, state="disabled")
        self.target_mos_box.grid(row=0, column=1, sticky="w")
        self.target_mos_box.bind("<<ComboboxSelected>>", self._target_mos_chosen)
        ttk.Label(self.generated_reference_frame, text="Single-device model · default symmetric pair +3 dB",
                  style="Help.TLabel").grid(row=1, column=1, sticky="w", pady=(3, 0))

        self.manual_reference_frame = ttk.Frame(reference)
        replica_path = ttk.Frame(self.manual_reference_frame)
        replica_path.pack(fill="x", pady=(0, 6))
        self._build_maestro_path(replica_path, "replica")
        replica_inputs = ttk.Frame(self.manual_reference_frame)
        replica_inputs.pack(fill="x")
        replica_inputs.columnconfigure(1, weight=1)
        ttk.Label(replica_inputs, text="Complete History").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self._maestro_box(replica_inputs, "replica", "history").grid(row=0, column=1, sticky="ew")
        ttk.Label(replica_inputs, text="Test").grid(row=0, column=2, sticky="w", padx=10)
        self._maestro_box(replica_inputs, "replica", "test", width=15).grid(row=0, column=3, sticky="ew")
        self._render_reference_controls()

        analysis = ttk.LabelFrame(
            content, text="03  Analysis target", style="Card.TLabelframe", padding=(14, 9)
        )
        analysis.pack(fill="x", pady=(0, 10))
        analysis.columnconfigure(1, weight=1)
        analysis.columnconfigure(3, weight=1)
        modes = ttk.Frame(analysis)
        modes.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(modes, text="Offset mode").pack(side="left", padx=(0, 12))
        for text, mode in (("Single offset", "single"), ("Offset list", "list"), ("Linear", "linear"), ("Logarithmic", "log")):
            ttk.Radiobutton(modes, text=text, variable=self.vars["offset_mode"], value=mode).pack(side="left", padx=(0, 14))
        self.offset_input_frames = {}
        for mode in ("single", "list", "linear", "log"):
            frame = ttk.Frame(analysis)
            frame.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(0, 8))
            self.offset_input_frames[mode] = frame
        offset = self.offset_input_frames["single"]
        ttk.Label(offset, text="Target offset").pack(side="left", padx=(0, 12))
        self.offset_value_entry = ttk.Entry(offset, textvariable=self.vars["offset_value"], width=14)
        self.offset_value_entry.pack(side="left")
        ttk.Combobox(
            offset,
            textvariable=self.vars["offset_unit"],
            values=("Hz", "kHz", "MHz"),
            state="readonly",
            width=7,
        ).pack(side="left", padx=(6, 0))
        multiple = self.offset_input_frames["list"]
        ttk.Label(multiple, text="Point list").pack(side="left", padx=(0, 12))
        ttk.Entry(multiple, textvariable=self.vars["offset_values"]).pack(side="left", fill="x", expand=True)
        for mode in ("linear", "log"):
            frame = self.offset_input_frames[mode]
            for name, label in (("start", "Start"), ("stop", "Stop")):
                ttk.Label(frame, text=label).pack(side="left", padx=(0, 6))
                ttk.Entry(frame, textvariable=self.vars["offset_" + name], width=12).pack(side="left", padx=(0, 16))
        linear = self.offset_input_frames["linear"]
        for label, value in (("Step", "step"), ("Intervals", "intervals")):
            ttk.Radiobutton(linear, text=label, variable=self.vars["offset_spacing"], value=value).pack(side="left")
        self.offset_step_entry = ttk.Entry(linear, textvariable=self.vars["offset_step"], width=12)
        self.offset_step_entry.pack(side="left", padx=6)
        self.offset_intervals_entry = ttk.Entry(linear, textvariable=self.vars["offset_intervals"], width=8)
        self.offset_intervals_entry.pack(side="left", padx=6)
        logarithmic = self.offset_input_frames["log"]
        ttk.Label(logarithmic, text="per decade Point count").pack(side="left", padx=(0, 6))
        ttk.Entry(logarithmic, textvariable=self.vars["offset_points_per_decade"], width=8).pack(side="left")
        ttk.Label(
            analysis,
            text=f"Maximum harmonic (≤{MAX_PHASE_CANDIDATE_HARMONICS})",
        ).grid(
            row=2, column=0, sticky="w", padx=(0, 10)
        )
        self.max_harmonic_entry = ttk.Spinbox(
            analysis,
            textvariable=self.vars["max_harmonic"],
            from_=1,
            to=MAX_PHASE_CANDIDATE_HARMONICS,
            width=8,
        )
        self.max_harmonic_entry.grid(row=2, column=1, sticky="w")
        ttk.Button(analysis, text="View actual offsets", command=self._show_offset_points).grid(row=2, column=2, columnspan=2, sticky="w", padx=16)
        self._help(analysis, textvariable=self.offset_preview_var).grid(row=3, column=0, columnspan=4, sticky="ew")
        self._refresh_offset_preview()

        self.basic_setup_frames = (pair, analysis)
        status = self._help(self.setup_tab, textvariable=self.binding_status_var)
        status.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        actions = ttk.Frame(self.setup_tab, style="App.TFrame")
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.setup_start_button = ttk.Button(actions, text="Start analysis", style="Primary.TButton", command=self._start_from_setup)
        self.setup_start_button.pack(side="left")
        self.setup_inspect_button = ttk.Button(actions, text="Check binding", command=self._inspect_pair)
        self.setup_inspect_button.pack(side="left", padx=8)
        self.environment_button = ttk.Button(actions, text="Environment check…", command=self._show_environment)
        self.environment_button.pack(side="left")
        self.advanced_button = ttk.Button(
            actions,
            text="Advanced settings",
            style="Secondary.TButton",
            command=self._toggle_advanced,
        )
        self.advanced_button.pack(side="right")
        self.advanced_frame = ttk.LabelFrame(
            content, text="Advanced settings", style="Card.TLabelframe", padding=(14, 12)
        )
        self.advanced_tabs = ttk.Notebook(self.advanced_frame)
        self.advanced_tabs.pack(fill="both", expand=True)
        mapping_tab = self.mapping_tab = ttk.Frame(self.advanced_tabs, padding=10)
        physical_tab = self.physical_tab = ttk.Frame(self.advanced_tabs, padding=10)
        inventory_tab = self.inventory_tab = ttk.Frame(self.advanced_tabs, padding=10)
        options_tab = ttk.Frame(self.advanced_tabs, padding=10)
        expert_tab = ttk.Frame(self.advanced_tabs, padding=10)
        for frame, label in ((mapping_tab, "Effective mappings"), (physical_tab, "Execution details"), (options_tab, "Discovery & execution options"), (expert_tab, "Expert compatibility")):
            self.advanced_tabs.add(frame, text=label)
        self._build_mapping_tabs(mapping_tab, physical_tab, inventory_tab)
        diagnostics = ttk.Frame(options_tab)
        diagnostics.pack(fill="x", pady=(0, 8))
        ttk.Button(diagnostics, text="View signal inventory", command=self._open_catalog).pack(side="left")
        ttk.Button(diagnostics, text="View History Netlist provenance",
                   command=lambda: self._open_advanced_tab(self.physical_tab)).pack(side="left", padx=8)
        self._help(options_tab, textvariable=self.mapping_status_var).pack(fill="x")
        self._help(options_tab, textvariable=self.binding_provenance_var).pack(fill="x")
        self._help(options_tab, text="Offset uses Hz is the default unit, k=thousand, M=million. maximum per request 256 points. ").pack(fill="x")
        if self._preferences_path:
            self._help(options_tab, textvariable=self.preference_status_var).pack(fill="x")
        location.pack(in_=options_tab, fill="x", pady=(0, 10))
        settings = ttk.Frame(options_tab)
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)

        advanced_fields = (
            (0, 0, "Project name", "project_name"),
            (1, 0, "Phase samples", "phase_samples"),
            (1, 2, "Analysis periods", "period_count"),
            (2, 0, "PDK Name override", "pdk_name"),
            (2, 2, "PDK Root override", "pdk_root"),
            (3, 0, "Run directory (optional)", "run_root"),
            (3, 2, "Result Bundle Directory (optional)", "bundle_path"),
            (4, 0, "Analysis Python (leave blank for automatic detection)", "analysis_python"),
        )
        for row, column, label, key in advanced_fields:
            ttk.Label(settings, text=label).grid(
                row=row, column=column, sticky="w", padx=(0, 8), pady=4
            )
            ttk.Entry(settings, textvariable=self.vars[key]).grid(
                row=row, column=column + 1, sticky="ew", padx=(0, 18), pady=4
            )
        self._help(
            expert_tab,
            text=(
                "File paths are normally unnecessary: Execution settings are generated from selected TB Generated. use this entry only to reuse an existing custom configuration; "
                "manual files take precedence over generated settings, Switch TB then confirm applicability again. "
            ),
        ).pack(fill="x", pady=(4, 8))
        engine_row = ttk.Frame(expert_tab)
        engine_row.pack(fill="x", pady=(0, 10))
        ttk.Label(engine_row, text="Execution configuration file (engine JSON, may be blank)").pack(side="left")
        ttk.Entry(engine_row, textvariable=self.vars["engine_config"]).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(engine_row, text="Choose file", command=self._choose_engine).pack(side="left")
        ttk.Button(engine_row, text="Restore automatic", command=lambda: self.vars["engine_config"].set("")).pack(side="left", padx=(6, 0))

        mode_row = ttk.Frame(options_tab)
        mode_row.pack(fill="x", pady=(12, 8))
        self.formal_signoff_check = ttk.Checkbutton(
            mode_row,
            text="Formal Signoff (at least 6 real design points and the full quality gate)",
            variable=self.formal_signoff_var,
            command=self._mode_changed,
        )
        self.formal_signoff_check.pack(side="left")

        fingerprint = ttk.LabelFrame(
            expert_tab,
            text="Binding integrity fingerprint (Generated)",
            style="Card.TLabelframe",
            padding=10,
        )
        fingerprint.pack(fill="x", pady=(4, 10))
        fingerprint.columnconfigure(1, weight=1)
        fingerprint.columnconfigure(2, weight=1)
        ttk.Label(fingerprint, text="").grid(row=0, column=0)
        ttk.Label(fingerprint, text="VCO").grid(row=0, column=1, sticky="w")
        ttk.Label(fingerprint, text="Flicker / Noise").grid(row=0, column=2, sticky="w")
        for row, (label, suffix) in enumerate(
            (("Binding fingerprint", "binding_fingerprint"), ("History Fingerprint", "history_fingerprint")),
            start=1,
        ):
            ttk.Label(fingerprint, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            for column, role in ((1, "vco"), (2, "replica")):
                ttk.Entry(
                    fingerprint,
                    textvariable=self.vars[f"{role}_{suffix}"],
                    state="readonly",
                ).grid(row=row, column=column, sticky="ew", padx=(0, 8), pady=3)
        ttk.Label(
            fingerprint,
            text="Confirms the same identity at runtime for Maestro, Test and History, no manual input needed. ",
            style="Help.TLabel",
        ).grid(row=3, column=1, columnspan=2, sticky="w", pady=(4, 0))

        config_actions = ttk.Frame(expert_tab)
        config_actions.pack(fill="x", pady=(0, 6))
        ttk.Button(config_actions, text="Load configuration JSON", command=self._load_config_dialog).pack(side="left")
        ttk.Button(config_actions, text="Save configuration JSON", command=self._save_config_dialog).pack(side="left", padx=6)
        ttk.Button(config_actions, text="Refresh from form JSON", command=self._render_merged_config).pack(side="left", padx=6)
        ttk.Button(config_actions, text="Apply the following JSON", command=self._apply_raw_editor).pack(side="left")
        ttk.Label(
            config_actions,
            text="Compatibility fields not shown in the form are retained. ",
            style="Help.TLabel",
        ).pack(side="right")
        raw_body = ttk.Frame(expert_tab)
        raw_body.pack(fill="both", expand=True)
        self.raw_text = tk.Text(
            raw_body, height=10, wrap="none", undo=True, font=self.fixed_font, borderwidth=0, spacing1=2, spacing3=2
        )
        self.raw_text.pack(side="left", fill="both", expand=True)
        raw_y = ttk.Scrollbar(raw_body, orient="vertical", command=self.raw_text.yview)
        raw_y.pack(side="right", fill="y")
        self.raw_text.configure(yscrollcommand=raw_y.set)

    def _maestro_box(self, parent: tk.Misc, role: str, field: str, *, width: int = 16) -> ttk.Combobox:
        is_history = field == "history"
        variable = self._history_display_vars[role] if is_history else self.vars[f"{role}_{field}"]
        box = ttk.Combobox(parent, textvariable=variable, width=width, state="readonly" if is_history else "normal")
        if is_history:
            box.bind("<<ComboboxSelected>>", lambda _event: self._history_chosen(role))
        else:
            box.bind("<<ComboboxSelected>>", lambda _event: self._schedule_role_inspection(role))
            box.bind("<FocusOut>", lambda _event: self._schedule_role_inspection(role))
            box.bind("<Return>", lambda _event: self._schedule_role_inspection(role))
        setattr(self, f"{role}_{field}_box", box)
        return box

    def _build_maestro_path(self, parent: ttk.Frame, role: str) -> None:
        ttk.Label(parent, text="Maestro Directory").pack(side="left", padx=(0, 10))
        ttk.Button(parent, text="Choose directory…", command=lambda: self._choose_directory(f"{role}_path")).pack(
            side="right", padx=(6, 0)
        )
        self._maestro_box(parent, role, "path").pack(side="left", fill="x", expand=True)

    def _toggle_vco_source(self) -> None:
        if self.vco_source_controls.winfo_manager():
            self.vco_source_controls.grid_remove()
        else:
            self.vco_source_controls.grid()
            self.vco_path_box.focus_set()

    def _generated_reference(self) -> bool:
        source = self.vars.get("replica_source")
        return source is not None and source.get() == "generated"

    def _render_reference_controls(self) -> None:
        shown, hidden = (
            (self.generated_reference_frame, self.manual_reference_frame) if self._generated_reference()
            else (self.manual_reference_frame, self.generated_reference_frame)
        )
        hidden.pack_forget()
        shown.pack(fill="x")

    def _replica_source_changed(self) -> None:
        self._render_reference_controls()
        self._mapping_changed()
        self._render_mapping_report({})
        generated = self._generated_reference()
        self.binding_status_var.set(
            "Generated reference, Select complete VCO History then detect the target MOS. "
            if generated else "Select an independent Noise TB and its complete History. "
        )
        roles = ("vco",) if generated else ("vco", "replica")
        if all(self.vars[f"{role}_{field}"].get() for role in roles for field in ("path", "history")):
            self._inspect_pair()
        elif not generated and self.vars["replica_path"].get():
            self._schedule_role_inspection("replica")

    def _render_target_mos(self, report: Mapping[str, Any]) -> None:
        row = next((row for row in mapping_review_rows(report)
                    if row["role"] == "vco" and row["key"] == "target_instance"), {})
        override = str(self._mapping_overrides["vco"].get("target_instance") or "")
        candidates = mapping_candidate_names(row, override or None)
        self.target_mos_box.configure(values=(AUTOMATIC_TARGET, *candidates),
                                      state="readonly" if candidates else "disabled")
        self.target_mos_var.set(override or str(row.get("value") or row.get("detected") or AUTOMATIC_TARGET))

    def _target_mos_chosen(self, _event: Any = None) -> None:
        target = self.target_mos_var.get()
        overrides = self._mapping_overrides["vco"]
        if target == AUTOMATIC_TARGET:
            overrides.pop("target_instance", None)
        else:
            overrides["target_instance"] = target
        self._mapping_changed()
        self._inspect_pair()

    def _start_from_setup(self) -> None:
        if self._generated_reference() and not self.vars["vco_history"].get():
            self.setup_canvas.yview_moveto(0)
            if self.vars["vco_path"].get():
                self.binding_status_var.set("Select a completed VCO History, then start analysis. ")
                self.vco_history_box.focus_set()
            else:
                self.binding_status_var.set("First select VCO of Maestro Directory. ")
                if self.maestro_context and not self.vco_source_controls.winfo_manager():
                    self._toggle_vco_source()
                self.vco_path_box.focus_set()
            return
        self._start_run()

    def _show_environment(self) -> None:
        from ..core.environment_report import format_environment_report, inspect_environment

        try:
            config = self._config_from_ui()
            config_error = ""
        except (TypeError, ValueError) as exc:
            config = deep_merge({}, self.raw_config)
            config_error = f"Settings cannot be read now, the last loaded configuration is used below. {exc}\n\n"
        context = deep_merge({}, self.maestro_context) if self._context_mode else None
        self.environment_button.state(("disabled",))

        def enable(_value: Any) -> None:
            self.environment_button.state(("!disabled",))

        def show(report: Any) -> None:
            enable(report)
            window = tk.Toplevel(self.root)
            window.title("ISF environment check")
            window.geometry("780x530")
            window.minsize(530, 340)
            window.transient(self.root)
            frame = ttk.Frame(window, padding=14)
            frame.pack(fill="both", expand=True)
            title = "Runtime tools found" if report.get("status") == "ready" else "Runtime requirements need attention"
            ttk.Label(frame, text=title, style="Setup.TLabel").pack(anchor="w", pady=(0, 6))
            body = ttk.Frame(frame)
            body.pack(fill="both", expand=True)
            palette = THEME_PALETTES[self.theme_var.get()]
            text = tk.Text(body, wrap="word", font=self.body_font, padx=8, pady=8,
                           background=palette["surface_alt"], foreground=palette["text"])
            scroll = ttk.Scrollbar(body, orient="vertical", command=text.yview)
            scroll.pack(side="right", fill="y")
            text.pack(side="left", fill="both", expand=True)
            text.configure(yscrollcommand=scroll.set)
            rendered = config_error + format_environment_report(report)
            text.insert("1.0", rendered)
            text.configure(state="disabled")

            def copy_report() -> None:
                window.clipboard_clear()
                window.clipboard_append(rendered)

            actions = ttk.Frame(frame)
            actions.pack(fill="x", pady=(8, 0))
            ttk.Button(actions, text="Copy report", command=copy_report).pack(side="left")
            ttk.Button(actions, text="Close", command=window.destroy).pack(side="right")
            window.bind("<Escape>", lambda _event: window.destroy())

        self._submit("Inspect runtime environment", lambda: inspect_environment(config, self.controller.workspace, context),
                     show, on_failure=enable)

    def _toggle_advanced(self) -> None:
        visible = not bool(self.advanced_visible_var.get())
        self.advanced_visible_var.set(visible)
        if visible:
            self.advanced_frame.pack(fill="both", expand=True, pady=(0, 8))
            self.advanced_button.configure(text="Hide advanced settings")
            self.root.after_idle(self._scroll_to_advanced)
        else:
            self.advanced_frame.pack_forget()
            self.advanced_button.configure(text="Advanced settings")
            self.setup_canvas.yview_moveto(0)

    def _scroll_to_advanced(self) -> None:
        if self.advanced_visible_var.get():
            bounds = self.setup_canvas.bbox("all")
            if bounds and bounds[3] > 0:
                self.setup_canvas.yview_moveto(self.advanced_frame.winfo_y() / bounds[3])

    def _open_catalog(self) -> None:
        if str(self.inventory_tab) not in self.advanced_tabs.tabs():
            self.advanced_tabs.add(self.inventory_tab, text="Diagnostic: All names")
        self._open_advanced_tab(self.inventory_tab)

    def _offset_inputs_changed(self, *_args: Any) -> None:
        if self._updating_form:
            return
        self._selection_revision += 1
        self.last_preflight = None
        self.preflight_status_var.set("Offset changed, Preflight required again")
        self._refresh_offset_preview()

    def _offset_preview(self) -> dict[str, Any]:
        vco = _mapping(_mapping(self._signal_report.get("roles")).get("vco"))
        history = _mapping(vco.get("selected_history"))
        count = len(_sequence(history.get("source_netlists"))) or None
        return offset_preview({key: variable.get() for key, variable in self.vars.items() if key.startswith("offset_")}, count)

    def _refresh_offset_preview(self) -> None:
        mode = str(self.vars["offset_mode"].get())
        for name, frame in self.offset_input_frames.items():
            frame.grid() if name == mode else frame.grid_remove()
        step = self.vars["offset_spacing"].get() == "step"
        self.offset_step_entry.configure(state="normal" if step else "disabled")
        self.offset_intervals_entry.configure(state="disabled" if step else "normal")
        try:
            preview = self._offset_preview()
            values = preview["values_hz"]
            shown = ", ".join(f"{value:.7g}" for value in values[:8]) + (", …" if len(values) > 8 else "")
            workload = (f"{preview['operating_point_count']} Operating point × {len(values)} Offset = {preview['total_samples']} operating points/offset results" if preview["total_samples"] else f"{len(values)} offsets; Binding TB then operating-point count and total workload are shown")
            endpoint = "; step does not divide the range exactly, endpoint appended" if preview["endpoint_appended"] else ""
            self.offset_preview_var.set(f"{workload}{endpoint}\nActual offsets (Hz): {shown}. curves show only completed points, no inferred or interpolated results. ")
        except Exception as exc:
            self.offset_preview_var.set("Offset input needs correction: " + str(exc))

    def _show_offset_points(self) -> None:
        try:
            preview = self._offset_preview()
        except Exception as exc:
            self._show_error(exc, "Invalid offset input")
            return
        window = tk.Toplevel(self.root)
        window.title("Actual offset list")
        window.geometry("470x470")
        self._help(window, text=self.offset_preview_var.get()).pack(fill="x", padx=12, pady=10)
        tree = self._tree(window, (("index", "Index", 80), ("hz", "Actual offsets (Hz)", 260)))
        self._with_scroll(window, tree).pack(fill="both", expand=True, padx=12, pady=(0, 12))
        for index, value in enumerate(preview["values_hz"], 1):
            tree.insert("", "end", values=(index, format(value, ".12g")))

    def _open_advanced_tab(self, tab: ttk.Frame) -> None:
        self.notebook.select(self.setup_tab)
        if not self.advanced_visible_var.get():
            self._toggle_advanced()
        self.advanced_tabs.select(tab)
        self.root.update_idletasks()
        self._scroll_to_advanced()

    def _scroll_setup(self, event: tk.Event[Any]) -> None:
        canvas = self._page_canvases.get(str(self.notebook.select()))
        if canvas is None:
            return
        if isinstance(event.widget, (ttk.Treeview, ttk.Combobox)) or (isinstance(event.widget, tk.Text) and not isinstance(event.widget, _HelpText)):
            return
        direction = -1 if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0 else 1
        widget = event.widget
        within_page = False
        while widget is not None:
            if widget is getattr(self, "mapping_rows_frame", None) or widget is getattr(self, "mapping_canvas", None):
                first, last = self.mapping_canvas.yview()
                if (direction < 0 and first > 0) or (direction > 0 and last < 1):
                    self.mapping_canvas.yview_scroll(direction * 3, "units")
                    return
            if widget is canvas:
                within_page = True
            widget = getattr(widget, "master", None)
        if not within_page:
            return
        if getattr(event, "state", 0) & 1:
            canvas.xview_scroll(direction * 3, "units")
        else:
            canvas.yview_scroll(direction * 3, "units")

    def _build_mapping_tabs(self, mapping_tab: ttk.Frame, physical_tab: ttk.Frame, inventory_tab: ttk.Frame) -> None:
        self._help(mapping_tab, text="Calculation scope: gm of flicker / thermal and LC of thermal. Thermal Static source spectra: the script uses the selected VCO History append to a private netlist copy noise Analysis, reads only enabled purely passive LC network noise sources, capacitor-bank internal noise is excluded; gm MOS periodic operating-point source spectra come from noise TB obtained. no changes are written to the original TB, no separate output needs to be named noise of Maestro Output. Static source spectra require periodic transfer and band folding to become calculated phase noise; VCO HBNoise remains the full-circuit simulation reference, unmodeled sources show N/A. ").pack(fill="x", pady=(0, 8))
        self._help(
            mapping_tab,
            text="calculation roles are listed on the left, select or enter actual names for both TB actual names. candidates come from this TB all names of the same kind; arbitrary aliases can also be entered manually. input is retained immediately, apply all to validate. each row“Automatic”restores automatic detection. ",
        ).pack(fill="x", pady=(0, 8))
        mapping_actions = ttk.Frame(mapping_tab)
        mapping_actions.pack(fill="x", pady=(0, 8))
        ttk.Button(mapping_actions, text="Apply all & validate", command=self._inspect_pair).pack(side="left")
        ttk.Button(mapping_actions, text="Clear overrides & detect again", command=self._reset_signal_overrides).pack(side="left", padx=8)
        ttk.Button(mapping_actions, text="View all actual names", command=self._open_catalog).pack(side="left")
        ttk.Checkbutton(mapping_tab, text="Show unused mappings, Family members and execution details (Diagnostic)", variable=self.show_all_mappings_var, command=lambda: self._render_mapping_report(self._signal_report)).pack(anchor="w", pady=(0, 8))
        ttk.Label(mapping_tab, text="cross- TB Bias mapping (only this section covers VGS / VDS / VBS)", font=self.heading_font).pack(anchor="w", pady=(4, 8))
        self.cross_tb_tree = self._tree(mapping_tab, (("meaning", "Physical meaning", 100), ("vco", "VCO Periodic waveform / Node difference", 240), ("replica", "Flicker Bias variable", 170), ("source", "Waveform source", 140), ("status", "Mapping status", 135)))
        self.cross_tb_tree.configure(height=3)
        self.cross_tb_tree.pack(fill="x", pady=(0, 10))
        self.cross_tb_tree.bind("<<TreeviewSelect>>", self._cross_tb_selected)
        ttk.Label(mapping_tab, text="Effective mappings; stale, errors and ambiguous items are always visible", font=self.heading_font).pack(anchor="w", pady=(4, 8))
        table = ttk.Frame(mapping_tab)
        table.pack(fill="both", expand=True)
        self.mapping_canvas = tk.Canvas(table, height=400, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.mapping_canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.mapping_canvas.pack(side="left", fill="both", expand=True)
        self.mapping_canvas.configure(yscrollcommand=scrollbar.set)
        self.mapping_rows_frame = ttk.Frame(self.mapping_canvas)
        self._mapping_window = self.mapping_canvas.create_window((0, 0), window=self.mapping_rows_frame, anchor="nw")
        self.mapping_rows_frame.bind("<Configure>", lambda _event: self.mapping_canvas.configure(scrollregion=self.mapping_canvas.bbox("all")))
        self.mapping_canvas.bind("<Configure>", lambda event: self.mapping_canvas.itemconfigure(self._mapping_window, width=event.width))
        self._help(mapping_tab, textvariable=self.mapping_detail_var).pack(fill="x", pady=(0, 6))
        ttk.Label(mapping_tab, text="VCO → Noise TB Waveform transfer check", font=self.heading_font).pack(anchor="w", pady=(12, 6))
        self._help(mapping_tab, textvariable=self.waveform_exchange_var).pack(fill="x")
        self.waveform_exchange_tree = self._tree(mapping_tab, (("semantic", "Quantity", 65), ("source", "VCO Source", 245), ("destination", "Noise TB Destination", 220), ("path", "Path policy", 160), ("status", "Check results", 100)))
        self.waveform_exchange_tree.configure(height=3)
        self._with_scroll(mapping_tab, self.waveform_exchange_tree).pack(fill="x", pady=(4, 12))

        self._help(physical_tab, text="These values come from the bound netlist, derived from outputs and variables. preflight reports ambiguous or missing items; Revalidate after manual edits. legacy custom configuration files can be selected in“Expert compatibility”Select. ").pack(fill="x", pady=(0, 8))
        ttk.Label(physical_tab, text="Selected History Netlist provenance (Read-only)", font=self.heading_font).pack(anchor="w")
        self.history_provenance_tree = self._tree(physical_tab, (("role", "TB", 80), ("current", "Maestro Current design", 245),
            ("history", "History Netlisted design", 245), ("point", "point / corner", 150), ("mtime", "Source netlist modification time", 215),
            ("sha256", "Source netlist SHA-256", 520), ("status", "Description", 260)))
        self.history_provenance_tree.configure(height=4)
        provenance_frame = self._with_scroll(physical_tab, self.history_provenance_tree)
        provenance_frame.configure(width=900, height=220)
        provenance_frame.pack_propagate(False)
        provenance_frame.pack(fill="x", pady=(6, 12))
        self.portable_tree = self._tree(physical_tab, (("label", "Purpose", 215), ("value", "Current value", 310), ("source", "Source", 155), ("status", "State", 100)))
        self.portable_tree.configure(height=12)
        self._with_scroll(physical_tab, self.portable_tree).pack(fill="both", expand=True)
        self.portable_tree.bind("<<TreeviewSelect>>", self._portable_selected)
        edit = ttk.Frame(physical_tab)
        edit.pack(fill="x", pady=8)
        ttk.Label(edit, text="Manual override (leave blank＝Automatic)").pack(side="left")
        self.portable_edit_box = ttk.Combobox(edit, textvariable=self.portable_edit_var)
        self.portable_edit_box.pack(side="left", fill="x", expand=True, padx=8)
        self.portable_apply_button = ttk.Button(edit, text="Apply & validate", command=self._apply_portable_override)
        self.portable_apply_button.pack(side="left")
        self.portable_reset_button = ttk.Button(edit, text="Restore automatic", command=lambda: self._apply_portable_override(reset=True))
        self.portable_reset_button.pack(side="left", padx=(6, 0))
        self._help(physical_tab, textvariable=self.portable_detail_var).pack(fill="x", pady=(0, 6))

        self._help(inventory_tab, text="This lists the selected TB discovered net names (Net), Maestro Output (Output), Design variable (Variable), and device, Analyses and saved signals. Net names come from the generated netlist and Maestro Reference, not read directly schematic Binary; first use Maestro generate a netlist and select the corresponding History. the inventory does not rename items, not every expression is executed. ").pack(fill="x", pady=(0, 8))
        ttk.Button(inventory_tab, text="Hide full name diagnostics", command=lambda: self.advanced_tabs.forget(inventory_tab)).pack(anchor="w", pady=(0, 8))
        ttk.Label(inventory_tab, textvariable=self.catalog_status_var, font=self.heading_font).pack(anchor="w", pady=(0, 8))
        filters = ttk.Frame(inventory_tab)
        filters.pack(fill="x", pady=(0, 8))
        self.catalog_kind_box = ttk.Combobox(filters, textvariable=self.catalog_kind_var, state="readonly", width=20,
                                           values=("All types", *CATALOG_KIND_LABELS.values()))
        self.catalog_kind_box.pack(side="left")
        self.catalog_kind_var.trace_add("write", lambda *_args: self._render_signal_catalog())
        ttk.Label(filters, text="Search names / Expression / TB").pack(side="left", padx=(8, 0))
        ttk.Entry(filters, textvariable=self.catalog_filter_var).pack(side="left", fill="x", expand=True, padx=8)
        self.catalog_filter_var.trace_add("write", lambda *_args: self._render_signal_catalog())
        ttk.Button(filters, text="Edit tool mappings", command=lambda: self._open_advanced_tab(self.mapping_tab)).pack(side="left")
        self.catalog_tree = self._tree(inventory_tab, (("role", "TB", 75), ("kind", "Type", 145), ("name", "Actual name", 220), ("expression", "Expression / Node / Value", 365)))
        self.catalog_tree.configure(height=15)
        self._with_scroll(inventory_tab, self.catalog_tree).pack(fill="both", expand=True)
        self.catalog_tree.bind("<<TreeviewSelect>>", self._catalog_selected)
        self._help(inventory_tab, textvariable=self.catalog_detail_var).pack(fill="x", pady=8)

    @staticmethod
    def _field_status(row: Mapping[str, Any]) -> str:
        status = str(row.get("status") or "")
        return {"auto": "Automatic", "detected": "Automatic", "derived": "Script derived", "resolved": "Detected", "manual": "Manual", "override": "Manual", "overridden": "Manual", "ambiguous": "multiple candidates", "missing": "Unresolved", "invalid": "Needs correction", "pending": "Awaiting validation", "optional": "optional", "error": "Needs correction"}.get(status, status)

    def _focus_mapping_input(self, iid: str) -> None:
        widget = self._mapping_inputs.get(iid)
        if widget is not None:
            self.root.update_idletasks()
            bounds = self.mapping_canvas.bbox("all")
            if bounds and bounds[3]:
                self.mapping_canvas.yview_moveto(max(0, widget.master.winfo_y() - 35) / bounds[3])
            widget.focus_set()
            self.mapping_detail_var.set(self._field_detail(self._mapping_rows[iid]))

    def _mapping_input_changed(self, iid: str) -> None:
        if self._rendering_mapping_inputs:
            return
        row = self._mapping_rows.get(iid)
        if not row:
            return
        value = self._mapping_input_vars[iid].get().strip()
        target = self._mapping_overrides[str(row["role"])]
        if value:
            target[str(row["key"])] = value
        else:
            target.pop(str(row["key"]), None)
        self._mapping_changed()
        self._mapping_status_labels[iid].configure(text="Manual · Awaiting validation" if value else "Automatic · Awaiting validation")
        self.mapping_status_var.set("Input retained, Not validated yet; Click“Apply all & validate”. ")
        self.mapping_detail_var.set(self._field_detail(row, value))

    def _reset_mapping_input(self, iid: str) -> None:
        row = self._mapping_rows.get(iid)
        if not row:
            return
        self._mapping_overrides[str(row["role"])].pop(str(row["key"]), None)
        self._rendering_mapping_inputs = True
        try:
            self._mapping_input_vars[iid].set(str(row.get("detected") or row.get("effective_value") or ""))
        finally:
            self._rendering_mapping_inputs = False
        self._mapping_changed()
        self._mapping_status_labels[iid].configure(text="Automatic · Awaiting validation")
        self.mapping_status_var.set("Automatic settings restored; Click“Apply all & validate”Confirm current mapping. ")
        self.mapping_detail_var.set(self._field_detail(row, self._mapping_input_vars[iid].get()))

    def _cross_tb_selected(self, _event: Any = None) -> None:
        selected = self.cross_tb_tree.selection()
        row = self._cross_tb_rows.get(selected[0], {}) if selected else {}
        if row:
            self.mapping_detail_var.set(f"{row['label']}: {row.get('help') or ''}\n{row.get('vco') or 'Unresolved'} → {row.get('replica') or 'Unresolved'}; Source: {row.get('source')}; State: {row.get('status')}\nExpression: {row.get('expression') or 'None'}")

    @staticmethod
    def _field_detail(row: Mapping[str, Any], selected: Any = None) -> str:
        candidates = ", ".join(mapping_candidate_names(row, selected)) or "None"
        issues = "; ".join(str(item) for item in _sequence(row.get("issues")) if item)
        detail = f"{row.get('label') or row.get('key')}: {row.get('help') or 'used for the current simulation mapping. '}\nAutomatic detection: {_display(row.get('detected'))}; Source: {mapping_source_text(row)}; Candidates: {candidates}"
        if row.get("used_by") or row.get("consumption_mode"):
            consumers = {"private Spectre preparation": "Prepare private simulation copy", "flicker-noise extraction": "Flicker-noise extraction", "signed HBXF transfer H[k]": "with direction and phase HBXF Transfer", "ISF calculation": "ISF calculated", "VCO periodic bias → current TB noise sampling": "VCO current under periodic bias TB noise samples", "target flicker extraction": "Target device flicker-noise extraction"}
            used_by = ", ".join(consumers.get(str(value), str(value)) for value in row["used_by"]) if isinstance(row.get("used_by"), list) else str(row.get("used_by") or "not used in the calculation")
            modes = {"source_binding_remeasured": "locate the current source from the selected output, remeasure the full complex response at this output", "private_netlist": "Private netlist settings", "raw_signal": "extract actual signals", "raw_voltage_pair": "uses the original node-voltage difference", "netlist_bias_parameter": "as noise TB Bias variable", "raw_noise_contributor": "extract the selected device noise contribution", "reference_only": "reference only, not used as calculation input", "private_drain_source": "use drain-to-source injection in the private copy", "invalid": "Invalid, Needs correction"}
            mode = str(row.get("consumption_mode") or "See role description")
            detail += f"\nEffective role: {used_by or 'not used in the calculation'}; Usage: {modes.get(mode, mode)}"
        evidence = mapping_selection_details(row, selected)
        return detail + ("\n" + "\n".join(evidence) if evidence else "") + ("\nValidation notes: " + issues if issues else "")

    def _reset_signal_overrides(self) -> None:
        self._mapping_overrides = {"vco": {}, "replica": {}}
        self._mapping_changed()
        self._render_mapping_report(self._signal_report)
        self._inspect_pair()

    def _portable_selected(self, _event: Any = None) -> None:
        selected = self.portable_tree.selection()
        row = self._portable_rows.get(selected[0], {}) if selected else {}
        if not row:
            return
        value = self._portable_overrides.get(str(row["key"]), "")
        self.portable_edit_var.set(_display(value) if value != "" else "")
        self.portable_edit_box.configure(values=[str(item) for item in _sequence(row.get("candidates"))])
        expert_file = bool(str(self.vars["engine_config"].get() or "").strip())
        editable = row.get("editable") is not False and not expert_file
        self.portable_edit_box.configure(state="normal" if editable else "disabled")
        self.portable_apply_button.configure(state="normal" if editable else "disabled")
        self.portable_reset_button.configure(state="normal" if editable else "disabled")
        detail = self._field_detail(row)
        if expert_file:
            detail = "Currently using the expert file; This table contains generated suggestions. restore automatic settings in Expert compatibility first, before applying these overrides. \n" + detail
        self.portable_detail_var.set(detail)

    def _apply_portable_override(self, reset: bool = False) -> None:
        selected = self.portable_tree.selection()
        row = self._portable_rows.get(selected[0], {}) if selected else {}
        if not row:
            self.portable_detail_var.set("Select an execution setting to edit first. ")
            return
        if row.get("editable") is False:
            self.portable_detail_var.set("This item is constrained by the executor, can only inspect; Follow the role guidance to correct the original connection or signal mapping. ")
            return
        if str(self.vars["engine_config"].get() or "").strip():
            self.portable_detail_var.set("Current execution settings come from the expert file. restore automatic settings in Expert compatibility first, then edit the generated settings. ")
            return
        key = str(row["key"])
        text = "" if reset else self.portable_edit_var.get().strip()
        if text:
            reference = row.get("detected")
            value: Any = text
            if isinstance(reference, (dict, list, bool, int, float)):
                try:
                    value = json.loads(text)
                except ValueError:
                    self.portable_detail_var.set("This item requires a numeric or structured value, use the detected value as a guide. ")
                    return
            self._portable_overrides[key] = value
        else:
            self._portable_overrides.pop(key, None)
        self._mapping_changed()
        self._inspect_pair()

    def _mapping_changed(self) -> None:
        self._selection_revision += 1
        self.last_preflight = None
        self.preflight_status_var.set("Mapping changed, Preflight required again")
        self.mapping_status_var.set("Manual edits applied, Revalidating; Complete history Binding unchanged. ")
        if self._auto_engine_path and str(self.vars["engine_config"].get()) == self._auto_engine_path:
            self.vars["engine_config"].set("")
        self._auto_engine_path = ""

    def _render_mapping_report(self, report: Mapping[str, Any]) -> None:
        provenance = history_provenance_rows(report)
        self._clear_tree(self.history_provenance_tree)
        for row in provenance:
            self.history_provenance_tree.insert("", "end", values=tuple(row[key] for key in ("role", "current", "history", "point", "mtime", "sha256", "status")))
        summaries = []
        for role in ("VCO", "Noise TB"):
            selected = [row for row in provenance if row["role"] == role]
            if selected:
                designs = sorted({row["history"] for row in selected})
                history = designs[0] if len(designs) == 1 else "multiple sources, See per-point list"
                summaries.append(f"{role} Current design: {selected[0]['current']}; History Source: {history}")
        summaries.append("simulation copies the selected History fixed netlist, does not follow the current schematic / config or device-disabled state; to use the new topology, first use Maestro rerun and select a new History. source differences are informational, does not block historical replay. ")
        self.binding_provenance_var.set("\n".join(summaries))
        self._signal_report = dict(report)
        if hasattr(self, "target_mos_box"):
            self._render_target_mos(report)
        self._mapping_rows = {}
        rows = active_mapping_rows(report, bool(self.show_all_mappings_var.get()))
        present = {(row["role"], row["key"]) for row in rows}
        for role, overrides in self._mapping_overrides.items():
            for key, value in overrides.items():
                if (role, key) not in present:
                    rows.append({"role": role, "key": key, "label": key, "value": value, "status": "pending", "source": "Retained manual input, Waiting for current TB Validate"})
        self._rendering_mapping_inputs = True
        for child in self.mapping_rows_frame.winfo_children():
            child.destroy()
        self._mapping_input_vars = {}
        self._mapping_inputs = {}
        self._mapping_status_labels = {}
        for column, (text, weight) in enumerate((("TB", 0), ("Calculation role", 2), ("TB actual names (optional / can enter)", 4), ("Source / Missing / Derived", 3), ("State", 1))):
            self.mapping_rows_frame.columnconfigure(column, weight=weight)
            ttk.Label(self.mapping_rows_frame, text=text, font=self.heading_font).grid(row=0, column=column, sticky="w", padx=6, pady=10)
        for row in rows:
            iid = f"{row['role']}:{row['key']}"
            self._mapping_rows[iid] = row
            index = len(self._mapping_rows)
            manual = self._mapping_overrides.get(str(row["role"]), {})
            current = manual.get(str(row["key"]), row.get("value") or row.get("effective_value") or row.get("detected") or "")
            variable = tk.StringVar(master=self.root, value=str(current))
            self._mapping_input_vars[iid] = variable
            ttk.Label(self.mapping_rows_frame, text="VCO" if row["role"] == "vco" else "Noise TB").grid(row=index, column=0, sticky="nw", padx=6, pady=14)
            label = ttk.Label(self.mapping_rows_frame, text=row.get("label") or row["key"], wraplength=150, justify="left")
            label.grid(row=index, column=1, sticky="new", padx=6, pady=14)
            cell = ttk.Frame(self.mapping_rows_frame)
            cell.grid(row=index, column=2, sticky="new", padx=6, pady=10)
            box = ttk.Combobox(cell, textvariable=variable, values=mapping_candidate_names(row, current, bool(self.show_all_mappings_var.get())), width=22,
                               state="normal" if row.get("editable") is not False else "disabled")
            box.pack(fill="x")
            self._mapping_inputs[iid] = box
            box.bind("<FocusIn>", lambda _event, field=row, key=iid: self.mapping_detail_var.set(self._field_detail(field, self._mapping_input_vars[key].get())))
            ttk.Button(cell, text="Automatic", command=lambda key=iid: self._reset_mapping_input(key)).pack(anchor="w", pady=(5, 0))
            source = ttk.Label(self.mapping_rows_frame, text=mapping_source_text(row) or "Awaiting detection", wraplength=190, justify="left")
            source.grid(row=index, column=3, sticky="new", padx=6, pady=14)
            pending = str(row["key"]) in manual and str(manual[str(row["key"])]) != str(row.get("value") or "")
            generated = bool(row.get("effective_value") and not row.get("value"))
            status = ttk.Label(self.mapping_rows_frame, text="Manual · Awaiting validation" if pending else "Generated by this script" if generated else self._field_status(row), wraplength=100, justify="left")
            status.grid(row=index, column=4, sticky="nw", padx=6, pady=14)
            self._mapping_status_labels[iid] = status
            variable.trace_add("write", lambda *_args, key=iid: self._mapping_input_changed(key))
        self._rendering_mapping_inputs = False
        exchange = waveform_exchange_view(report)
        exchange_lines = [exchange["summary"], exchange.get("path_policy", ""), *exchange.get("checks", []), "Binding validates file generation and simulation preparation only; existing history or old bindings do not prove this waveform loop has run. "]
        self.waveform_exchange_var.set("\n".join(line for line in exchange_lines if line))
        self._clear_tree(self.waveform_exchange_tree)
        for item in exchange["rows"]:
            self.waveform_exchange_tree.insert("", "end", values=tuple(item[key] for key in ("semantic", "source", "destination", "path_policy", "status")))
        self._clear_tree(self.cross_tb_tree)
        self._cross_tb_rows = {}
        for row in cross_tb_review_rows(report):
            if row["key"] == "vbs" and not self.show_all_mappings_var.get() and not any(field.get("key") in {"vbs_output", "vbs_variable"} and field.get("active", True) for field in rows):
                continue
            self._cross_tb_rows[row["key"]] = row
            self.cross_tb_tree.insert("", "end", iid=row["key"], values=(row["label"], row["vco"] or "Unresolved", row["replica"] or "Unresolved", row["source"], row["status"]))
        self._render_portable_report(_mapping(report.get("portable_setup")))
        self._render_signal_catalog()
        unresolved = [row for row in rows if row.get("required") and (not row.get("value") or str(row.get("status")) in {"ambiguous", "invalid", "error", "missing"})]
        manual = sum(len(values) for values in self._mapping_overrides.values()) + len(self._portable_overrides)
        setup = _mapping(report.get("portable_setup"))
        suffix = f"; {manual} manual overrides" if manual else ""
        if str(self.vars["engine_config"].get() or "").strip():
            suffix += "; Using the expert execution file"
        elif setup and not setup.get("ok"):
            suffix += "; Execution settings need input (Advanced settings)"
        elif setup.get("ok"):
            suffix += "; Execution settings generated"
        self.mapping_status_var.set((f"Currently showing {len(rows)} mappings; {len(unresolved)} required items await confirmation" if unresolved else f"Currently showing {len(rows)} mappings, Run preflight before execution") + suffix)
        self._refresh_offset_preview()

    def _render_portable_report(self, setup: Mapping[str, Any]) -> None:
        selected = self.portable_tree.selection()
        self._clear_tree(self.portable_tree)
        self._portable_rows = {}
        for item in _sequence(setup.get("fields")):
            row = _mapping(item)
            if not row.get("key"):
                continue
            iid = str(row["key"])
            self._portable_rows[iid] = row
            self.portable_tree.insert("", "end", iid=iid, values=(row.get("label") or iid, _display(row.get("value")), row.get("source") or "", self._field_status(row)))
        if selected and selected[0] in self._portable_rows:
            self.portable_tree.selection_set(selected[0])
        errors = [str(_mapping(item).get("message") or item) for item in _sequence(setup.get("errors"))]
        if errors:
            self.portable_detail_var.set("Missing input: " + "; ".join(errors))

    def _render_signal_catalog(self) -> None:
        if not hasattr(self, "catalog_tree"):
            return
        self._clear_tree(self.catalog_tree)
        self._catalog_rows = {}
        query = self.catalog_filter_var.get().strip().lower()
        kind = next((key for key, label in CATALOG_KIND_LABELS.items() if label == self.catalog_kind_var.get()), "")
        for row in catalog_review_rows(self._signal_report, query=query, kind=kind):
            iid = row["iid"]
            self._catalog_rows[iid] = row
            self.catalog_tree.insert("", "end", iid=iid, values=(row["role_label"], row["kind_label"], row.get("name") or "", row.get("expression") if row.get("expression") is not None else ""))
        self.catalog_status_var.set(catalog_summary(self._signal_report) + f" · Currently showing {len(self._catalog_rows)} items")

    def _catalog_selected(self, _event: Any = None) -> None:
        selected = self.catalog_tree.selection()
        row = self._catalog_rows.get(selected[0], {}) if selected else {}
        if row:
            self.catalog_detail_var.set(f"{row.get('name')}\nExpression / Content: {_display(row.get('expression'))}\nSource: {_display(row.get('source'))}\nDetails: {_display(row.get('details'))}")

    def _choose_engine(self) -> None:
        path = self._choose_file("Select existing execution configuration", str(self.vars["engine_config"].get()))
        if path:
            self.vars["engine_config"].set(path)
            self.last_preflight = None
            self.mapping_status_var.set("Expert execution file selected; This file takes precedence over generated settings, Preflight required again. ")

    def _engine_changed(self, *_args: Any) -> None:
        if self._updating_form:
            return
        self._selection_revision += 1
        self.last_preflight = None
        self.preflight_status_var.set("Execution configuration source changed, Preflight required again")
        if str(self.vars["engine_config"].get() or "").strip():
            self.mapping_status_var.set("Using the expert execution file, takes precedence over generated settings; Check Advanced settings, then run preflight. ")
        else:
            self.mapping_status_var.set("Automatic execution settings restored; Detect again or run preflight to inspect current results. ")
        self._portable_selected()

    def _formal_signoff_enabled(self) -> bool:
        variable = getattr(self, "formal_signoff_var", None)
        return bool(variable.get()) if variable is not None else False

    def _mode_changed(self) -> None:
        self.mode_var.set("live")
        self.allow_execute_var.set(False)
        if hasattr(self, "formal_signoff_check"):
            self.formal_signoff_check.configure(state="normal")
        if hasattr(self, "start_button"):
            self.start_button.configure(text="2  Start live run")
        if hasattr(self, "setup_start_button"):
            self.setup_start_button.configure(text="Start signoff analysis" if self._formal_signoff_enabled() else "Start analysis")
        if hasattr(self, "execution_summary_var"):
            suffix = "Formal Signoff" if self._formal_signoff_enabled() else "not signoff"
            self.execution_summary_var.set(f"Live execution · {suffix}")

    def _update_maestro_banner(self) -> None:
        context = getattr(self, "maestro_context", {})
        if not context:
            return
        identity = "/".join(str(context.get(k) or "?") for k in ("library", "cell", "view"))
        label = "Current Maestro" if self._context_mode else "Launch source (Switched VCO)"
        self.maestro_source_var.set(f"{label}  ·  {identity}")

    def _set_history_choices(self, role: str, histories: Sequence[Mapping[str, Any]]) -> None:
        choices: dict[str, str] = {}
        for item in histories:
            path = str(item.get("path") or item.get("rdb_file") or "")
            if not path:
                continue
            label = str(item.get("name") or Path(path).stem)
            if label in choices and choices[label] != path:
                label += " · " + path
            choices[label] = path
        if not hasattr(self, "_history_choices"):
            self._history_choices = {}
        self._history_choices[role] = choices
        box = getattr(self, f"{role}_history_box")
        # Old non-widget test fixtures keep the original path-valued contract.
        if role not in getattr(self, "_history_display_vars", {}):
            box.configure(values=list(choices.values()))
            return
        box.configure(values=list(choices))
        self._sync_history_label(role)

    def _sync_history_label(self, role: str) -> None:
        display = getattr(self, "_history_display_vars", {}).get(role)
        if display is None:
            return
        path = str(self.vars[f"{role}_history"].get() or "")
        choices = getattr(self, "_history_choices", {}).get(role, {})
        display.set(next((name for name, value in choices.items() if value == path), Path(path).stem if path else ""))

    def _history_chosen(self, role: str) -> None:
        label = self._history_display_vars[role].get()
        path = self._history_choices[role].get(label)
        if path:
            self.vars[f"{role}_history"].set(path)
            self._schedule_role_inspection(role)

    def _restore_maestro_context(self, *, initial: bool = False) -> None:
        if not self.maestro_context:
            return
        current = self._config_from_ui()
        clear_noise = initial and not flatten_config(current).get("replica_path")
        config = apply_maestro_context(current, self.maestro_context, clear_noise=clear_noise)
        self._context_mode = True
        self.raw_config = config
        self._populate_form(config)
        self._set_raw_text(config)
        self._set_history_choices("vco", self.maestro_context.get("histories") or [])
        self._update_maestro_banner()
        self.binding_status_var.set(
            "Imported from launch window VCO; Select a complete History. "
            if self._generated_reference() else "Imported from launch window VCO; Select History and device noise TB. "
        )
        self._schedule_role_inspection("vco")

    def _refresh_context_histories(self) -> None:
        if not self.maestro_context:
            return
        if not self._context_mode:
            self._schedule_role_inspection("vco", refresh=True)
            return
        context = deep_merge({}, self.maestro_context)
        revision = self._selection_revision
        selection = self._pair_selection()
        def refreshed(value: Any) -> None:
            if revision != self._selection_revision or selection != self._pair_selection():
                return
            self.maestro_context = deep_merge({}, _mapping(value))
            if not self._context_mode:
                self._update_maestro_banner()
                return
            config = self._config_from_ui()
            config["testbenches"]["vco"]["history_catalog"] = deep_merge({}, {"items": self.maestro_context.get("histories") or []})["items"]
            self.raw_config = config
            self._set_raw_text(config)
            self._set_history_choices("vco", self.maestro_context.get("histories") or [])
            self._schedule_role_inspection("vco", refresh=True)
        self._submit("Refresh current Maestro of History", lambda: self.controller.refresh_maestro_context(context), refreshed)

    def _schedule_role_inspection(self, role: str, *, refresh: bool = False) -> None:
        if getattr(self, "_updating_form", False):
            return
        if role == "replica" and self._generated_reference():
            return
        if not str(self.vars[f"{role}_path"].get() or "").strip():
            return
        selection = self._pair_selection()
        token = (role,) + selection
        if not refresh and token == getattr(self, "_last_auto_selection", None):
            return
        pending = getattr(self, "_inspection_after_id", None)
        if pending is not None:
            self.root.after_cancel(pending)
        def inspect() -> None:
            self._inspection_after_id = None
            if selection != self._pair_selection():
                return
            self._last_auto_selection = token
            self._inspect_role(role)
        self._inspection_after_id = self.root.after(250, inspect)

    def _inspect_role(self, role: str) -> None:
        config = self._config_from_ui()
        payload = maestro_pair_payload(config)["testbenches"][role]
        payload.update(role="vco" if role == "vco" else "flicker", include_histories=True,
                       offset_hz=flatten_config(config).get("offset_hz") or 10000.0)
        selection, revision = self._pair_selection(), self._selection_revision
        def inspected(value: Any) -> None:
            if selection != self._pair_selection() or revision != self._selection_revision:
                return
            report = _mapping(value)
            requested = str(self.vars[f"{role}_history"].get() or "")
            test = str(_mapping(report.get("selected_test")).get("name") or self.vars[f"{role}_test"].get() or "")
            choice = select_history_candidate(_sequence(report.get("histories")), requested, test,
                                              explicit=bool(payload.get("explicit_history_selection")))
            self._updating_form = True
            try:
                getattr(self, f"{role}_test_box").configure(values=_sequence(report.get("test_names")))
                self.vars[f"{role}_test"].set(test)
                selected = _mapping(choice.get("selected"))
                self.vars[f"{role}_history"].set(selected.get("path") or requested)
                self._set_history_choices(role, choice.get("complete_histories") or [])
            finally:
                self._updating_form = False
            self.raw_config = self._config_from_ui()
            self._set_raw_text(self.raw_config)
            self._last_auto_selection = (role,) + self._pair_selection()
            roles = ("vco",) if self._generated_reference() else ("vco", "replica")
            if all(str(self.vars[f"{r}_{field}"].get() or "") for r in roles for field in ("path", "history")):
                self._inspect_pair()
            elif not choice.get("complete_histories"):
                label = "VCO" if role == "vco" else "Noise TB"
                self.binding_status_var.set(f"{label} No complete candidate to bind for History; Use Maestro refresh after simulation completes. ")
            else:
                self.binding_status_var.set(
                    "Candidates read; Select a complete VCO History. "
                    if self._generated_reference() else "Candidates read; Select a complete History and device noise TB. "
                )
        self._submit("Read VCO History" if role == "vco" else "Read noise TB History",
                     lambda: self.controller.inspect_maestro(payload), inspected)

    def _choose_directory(self, key: str) -> None:
        initial = str(self.vars[key].get() or self.vars["project_root"].get() or Path.cwd())
        value = filedialog.askdirectory(parent=self.root, initialdir=initial, title="Choose directory")
        if value:
            self.vars[key].set(value)
            if key in ("vco_path", "replica_path"):
                self._schedule_role_inspection(key.split("_", 1)[0])

    def _choose_file(self, title: str, initial: str = "") -> str:
        return filedialog.askopenfilename(
            parent=self.root,
            title=title,
            initialdir=str(Path(initial).expanduser().parent) if initial else str(Path.cwd()),
            filetypes=(("JSON", "*.json"), ("All files", "*")),
        )

    def _mark_binding_dirty(self, role: str) -> None:
        if self._updating_form:
            return
        paths = getattr(self, "_observed_role_paths", None)
        current_path = str(self.vars[f"{role}_path"].get() or "")
        path_changed = paths is not None and paths.get(role) != current_path
        if path_changed:
            paths[role] = current_path
            self._updating_form = True
            try:
                self.vars[f"{role}_test"].set("")
                self.vars[f"{role}_history"].set("")
                self._set_history_choices(role, [])
            finally:
                self._updating_form = False
        self.vars[f"{role}_binding_fingerprint"].set("")
        self.vars[f"{role}_history_fingerprint"].set("")
        self._selection_revision += 1
        self._mapping_overrides[role] = {}
        self._portable_overrides = {}
        if self._auto_engine_path and str(self.vars["engine_config"].get()) == self._auto_engine_path:
            self.vars["engine_config"].set("")
        self._auto_engine_path = ""
        try:
            self.raw_config = invalidate_role(self._raw_from_editor(), role)
            self._set_raw_text(self.raw_config)
        except (ValueError, TypeError):
            self.raw_config = invalidate_role(self.raw_config, role)
        if path_changed:
            spec = self.raw_config.get("testbenches", {}).get(role, {})
            spec.pop("history_catalog", None)
            spec.pop("explicit_history_selection", None)
            self._set_raw_text(self.raw_config)
        if role == "vco" and getattr(self, "maestro_context", {}) and str(self.vars["vco_path"].get()) != self.maestro_context.get("maestro_dir"):
            self._context_mode = False
            spec = self.raw_config.get("testbenches", {}).get("vco", {})
            spec.pop("history_catalog", None)
            spec.pop("explicit_history_selection", None)
            self._set_raw_text(self.raw_config)
            self._update_maestro_banner()
        self._sync_history_label(role)
        self._render_mapping_report({})
        suffix = " an expert execution file is still selected, Check Advanced settings or restore automatic settings. " if str(self.vars["engine_config"].get() or "").strip() else ""
        self.mapping_status_var.set("TB / Test / History has changed, Legacy mappings cleared; Detect again. " + suffix)
        self.binding_status_var.set("Path, Test or History changed; Detection and binding required")
        self.last_preflight = None
        self.preflight_status_var.set("Configuration changed, Rerun preflight")

    def _raw_from_editor(self) -> dict[str, Any]:
        text = self.raw_text.get("1.0", "end").strip()
        if not text:
            return dict(self.raw_config)
        value = json.loads(text)
        if not isinstance(value, Mapping):
            raise ValueError("Advanced JSON top level must be object")
        return dict(value)

    def _collect_fields(self) -> dict[str, Any]:
        fields = {key: variable.get() for key, variable in self.vars.items()}
        fields["dry_run"] = False
        fields["allow_execute"] = False
        for role, overrides in getattr(self, "_mapping_overrides", {}).items():
            fields[f"{role}_mapping_overrides"] = clean_mapping_overrides(overrides)
        if hasattr(self, "_portable_overrides"):
            fields["portable_overrides"] = clean_mapping_overrides(self._portable_overrides)
        return fields

    def _config_from_ui(self) -> dict[str, Any]:
        config = merge_config(self._raw_from_editor(), self._collect_fields())
        config["run_kind"] = "signoff" if self._formal_signoff_enabled() else "replay"
        execution = config.setdefault("execution", {})
        if isinstance(execution, dict):
            execution["background"] = True
            execution["resume_enabled"] = True
            execution["require_preflight"] = True
        return config

    def _execution_config_from_ui(self) -> dict[str, Any]:
        """Only an explicit preflight/start click grants this request execution."""
        config = deep_merge({}, self._config_from_ui())
        execution = config.setdefault("execution", {})
        execution["dry_run"] = False
        execution["allow_execute"] = True
        if config.get("run_kind") == "plan":
            config["run_kind"] = "replay"
        return config

    def _config_still_current(self, submitted: Mapping[str, Any]) -> bool:
        """Fail closed when an asynchronous result targets an older edit."""

        try:
            current, pending = deep_merge({}, self._config_from_ui()), deep_merge({}, submitted)
            # Authorization is carried only by the submitted request, not by
            # editable/persisted configuration.  All substantive edits count.
            for value in (current, pending):
                execution = value.get("execution")
                if isinstance(execution, dict):
                    execution.pop("allow_execute", None)
            return current == pending
        except Exception:
            return False

    def _pair_selection(self) -> tuple[str, ...]:
        roles = ("vco",) if self._generated_reference() else ("vco", "replica")
        return (str(self._generated_reference()),) + tuple(
            str(self.vars[f"{role}_{field}"].get() or "").strip()
            for role in roles
            for field in ("path", "test", "history")
        )

    def _set_raw_text(self, config: Mapping[str, Any]) -> None:
        config = strip_retired_noise_objects(deep_merge({}, config))
        if isinstance(config.get("execution"), dict):
            config["execution"]["allow_execute"] = False
        self.raw_text.delete("1.0", "end")
        self.raw_text.insert("1.0", json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True))

    def _populate_form(self, config: Mapping[str, Any]) -> None:
        flat = flatten_config(config)
        if not flat["replica_source"]:
            flat["replica_source"] = (
                "maestro" if any(flat[key] for key in ("replica_path", "replica_test", "replica_history", "engine_config"))
                else "generated"
            )
        previous_paths = {
            role: str(self.vars[f"{role}_path"].get() or "")
            for role in ("vco", "replica") if f"{role}_path" in self.vars
        }
        self._mapping_overrides = {role: clean_mapping_overrides(flat.get(f"{role}_mapping_overrides")) for role in ("vco", "replica")}
        self._portable_overrides = clean_mapping_overrides(flat.get("portable_overrides"))
        self._selection_revision = getattr(self, "_selection_revision", 0) + 1
        self._auto_engine_path = ""
        self._updating_form = True
        try:
            for key in self.vars:
                value = flat.get(key, "")
                if value == N_A or value is None:
                    value = ""
                self.vars[key].set(value)
            self.mode_var.set("live")
            if hasattr(self, "formal_signoff_var"):
                self.formal_signoff_var.set(
                    str(config.get("run_kind") or "").lower() == "signoff"
                )
            # Consent is deliberately session/run scoped and is never restored
            # from a configuration file.
            self.allow_execute_var.set(False)
            self._mode_changed()
        finally:
            self._updating_form = False
        for role in ("vco", "replica"):
            spec = _mapping(_mapping(config.get("testbenches")).get(role))
            if hasattr(self, f"{role}_history_box") and (
                str(flat.get(f"{role}_path") or "") != previous_paths.get(role, "")
                or "history_catalog" in spec
            ):
                self._set_history_choices(role, _sequence(spec.get("history_catalog")))
            self._sync_history_label(role)
            if hasattr(self, "_observed_role_paths"):
                self._observed_role_paths[role] = str(self.vars[f"{role}_path"].get() or "")
        if getattr(self, "maestro_context", {}):
            self._context_mode = str(self.vars["vco_path"].get()) == self.maestro_context.get("maestro_dir")
            self._update_maestro_banner()
        if hasattr(self, "generated_reference_frame"):
            self._render_reference_controls()
        generated = self._generated_reference()
        roles = ("vco",) if generated else ("vco", "replica")
        ready = all(str(self.vars[f"{role}_{field}"].get() or "") for role in roles
                    for field in ("binding_fingerprint", "history_fingerprint"))
        self.binding_status_var.set(
            "Historical binding loaded; Preflight still runs before execution. " if ready
            else "Select a completed VCO History. " if generated
            else "Select VCO and Noise TB complete History. "
        )
        if hasattr(self, "offset_input_frames"):
            self._refresh_offset_preview()
        if hasattr(self, "mapping_rows_frame"):
            self._render_mapping_report({})
            self.mapping_status_var.set("Manual mappings loaded from configuration; Click“Check binding”Check actual names and sources. ")

    def _config_loaded(self, value: Any, initial: bool = False) -> None:
        config = strip_retired_noise_objects(_mapping(value))
        persisted = self.controller.workspace / "config.json"
        if initial and not persisted.is_file():
            # Keep the backend fail-safe dry default for API/CLI callers, while
            # making the desktop's ordinary analysis procedure an explicitly authorised
            # real run. Formal signoff remains a separate advanced choice.
            config = deep_merge(
                config,
                {
                    "execution": {"dry_run": False, "allow_execute": False},
                    "run_kind": "replay",
                },
            )
        self.raw_config = config
        self._populate_form(config)
        self._set_raw_text(config)
        if initial and getattr(self, "maestro_context", {}):
            self._restore_maestro_context(initial=True)

    def _read_initial_inputs(self) -> tuple[Any, dict[str, Any], str]:
        config = self.controller.load_config()
        if self._preferences_path:
            try:
                return config, read_preferences(self._preferences_path), ""
            except (OSError, ValueError) as exc:
                return config, {}, str(exc)
        return config, {}, ""

    def _initial_inputs_loaded(self, value: tuple[Any, dict[str, Any], str]) -> None:
        config, preferences, error = value
        self._preferences = preferences
        if preferences:
            try:
                config = merge_config(_mapping(config), preferences["fields"])
            except (TypeError, ValueError) as exc:
                error = str(exc)
            else:
                self.theme_var.set(preferences["theme"])
                self._apply_theme()
                self.preference_status_var.set("Preferences loaded for this source; Explicitly select the current History. ")
        if error:
            self.preference_status_var.set("Project preferences not loaded: " + error)
        self._config_loaded(config, initial=True)

    def _restore_project_mapping(self, role: Mapping[str, Any]) -> bool:
        if not getattr(self, "_preferences_path", None) or not self._context_mode:
            return False
        key = mapping_key(role)
        overrides = _mapping(self._preferences.get("bindings")).get(key)
        if not overrides or key in self._restored_mapping_keys or self._mapping_overrides["vco"]:
            return False
        if role.get("maestro_dir") != self.maestro_context.get("maestro_dir"):
            return False
        self._restored_mapping_keys.add(key)
        self._mapping_overrides["vco"] = clean_mapping_overrides(overrides)
        self._mapping_changed()
        self.binding_status_var.set("Revalidating this History project mappings. ")
        self._inspect_pair()
        return True

    def _remember_project_preferences(self, config: Mapping[str, Any], preflight: Mapping[str, Any]) -> None:
        path = getattr(self, "_preferences_path", None)
        if not path or not self._context_mode or self._preferences_saving or not preflight.get("ok"):
            return
        flat = flatten_config(config)
        if flat["vco_path"] != self.maestro_context.get("maestro_dir"):
            return
        pair = _mapping(_mapping(preflight.get("detected")).get("maestro_pair"))
        role = _mapping(_mapping(pair.get("roles")).get("vco"))
        key = mapping_key(role)
        bindings = _mapping(self._preferences.get("bindings"))
        if pair.get("ok") and key and role.get("maestro_dir") == self.maestro_context.get("maestro_dir"):
            confirmed = clean_mapping_overrides(role.get("mapping_overrides"))
            if confirmed == flat["vco_mapping_overrides"]:
                bindings.pop(key, None)
                if confirmed:
                    bindings[key] = confirmed
        preference = {
            "fields": {key: flat[key] for key in PREFERENCE_FIELDS if flat.get(key) not in (None, "", N_A)},
            "theme": self.theme_var.get(),
            "bindings": bindings,
        }
        revision = self._preferences.get("revision")
        if all(self._preferences.get(key) == value for key, value in preference.items()):
            return
        self._preferences_saving = True

        def save() -> tuple[dict[str, Any], str]:
            try:
                return write_preferences(path, preference, revision), ""
            except (OSError, ValueError) as exc:
                return {}, str(exc)

        def saved(value: tuple[dict[str, Any], str]) -> None:
            self._preferences_saving = False
            stored, error = value
            if error:
                self.preference_status_var.set("Project preferences not saved: " + error)
            else:
                self._preferences = stored
                self.preference_status_var.set("Saved this Maestro source project preferences. ")

        self._submit("Save project preferences", save, saved, quiet=True)

    def _render_merged_config(self) -> None:
        try:
            config = self._config_from_ui()
        except Exception as exc:
            self._show_error(exc, "Invalid configuration")
            return
        self.raw_config = config
        self._set_raw_text(config)
        self.status_var.set("Form merged into advanced JSON")

    def _apply_raw_editor(self) -> None:
        try:
            config = self._raw_from_editor()
        except Exception as exc:
            self._show_error(exc, "Advanced JSON Invalid")
            return
        self.raw_config = config
        self._populate_form(config)
        self._set_raw_text(config)
        self.last_preflight = None
        self.preflight_status_var.set("Configuration changed, Rerun preflight")

    def _load_config_dialog(self) -> None:
        path = self._choose_file("Load configuration JSON")
        if path:
            self._submit("Load configuration", lambda: self.controller.load_config(path), self._config_loaded)

    def _state_view(self) -> dict[str, Any]:
        tabs = (self.setup_tab, self.run_tab, self.result_tab, self.error_tab)
        selected_tab = next((name for name, tab in zip(("setup", "run", "results", "errors"), tabs)
                             if str(tab) == self.notebook.select()), "setup")
        summary = _mapping(_mapping(self.current_result_view).get("selected_summary"))
        return {"job_id": self.current_result_job_id or self.current_job_id,
                "point": self.point_var.get(), "operating_point_id": self.operating_point_var.get(),
                "offset_hz": summary.get("offset_Hz"), "noise_kind": self.noise_kind_var.get(),
                "x_scale": "log" if self.log_offset_var.get() else "linear",
                "log_offset": self.log_offset_var.get(),
                "theme": self.theme_var.get(), "selected_tab": selected_tab,
                "advanced_visible": self.advanced_visible_var.get()}

    def _save_state_dialog(self) -> None:
        if self._state_load_pending:
            messagebox.showwarning("Loading state", "Wait for state loading before saving. ", parent=self.root)
            return
        name = simpledialog.askstring("Save state", "State name (Save settings, manual mappings and result display selection, execution consent is not saved)", parent=self.root)
        if not name or not name.strip():
            return
        try:
            payload = {"name": name.strip(), "config": self._config_from_ui(), "view": self._state_view()}
        except Exception as exc:
            self._show_error(exc, "Cannot save state")
            return
        self._submit("Save state", lambda: self.controller.save_state(payload),
                     lambda report: messagebox.showinfo("State saved", str(report.get("path") or ""), parent=self.root))

    def _load_state_dialog(self) -> None:
        if self._state_load_pending or self._job_command_pending or self._foreground_operations:
            messagebox.showwarning("Current operation is incomplete", "Wait for binding, Load state after preflight or job submission finishes. ", parent=self.root)
            return

        def listed(report: Any) -> None:
            rows = [row for row in _sequence(_mapping(report).get("states")) if isinstance(row, Mapping)]
            if not rows:
                messagebox.showinfo("No saved states", "first click“Save state”. \n" + str(_mapping(report).get("directory") or ""), parent=self.root)
                return
            dialog = tk.Toplevel(self.root)
            dialog.title("Load state")
            dialog.transient(self.root)
            dialog.grab_set()
            ttk.Label(dialog, text="restores only settings and existing result selection; does not bind automatically, preflight or simulation. ", padding=12).pack(anchor="w")
            choices = {f"{row.get('name')} · {row.get('saved_at')} · {row.get('id')}": str(row["id"]) for row in rows}
            variable = tk.StringVar(value=next(iter(choices)))
            box = ttk.Combobox(dialog, state="readonly", textvariable=variable, values=tuple(choices), width=80)
            box.pack(fill="x", padx=12, pady=8)

            def chosen() -> None:
                identifier = choices.get(variable.get())
                dialog.destroy()
                if identifier:
                    self._load_saved_state(identifier)

            ttk.Button(dialog, text="Load selected state", command=chosen).pack(pady=12)

        self._submit("List states", self.controller.list_states, listed)

    def _state_edit_snapshot(self) -> dict[str, Any]:
        """Capture unfinished edits without requiring a currently valid config."""
        return {"raw_editor": self.raw_text.get("1.0", "end-1c"),
                "raw_config": deep_merge({}, self.raw_config),
                "fields": self._collect_fields(),
                "formal_signoff": self.formal_signoff_var.get(),
                "selection_revision": self._selection_revision}

    def _load_saved_state(self, identifier: str) -> None:
        if self._state_load_pending or self._job_command_pending or self._foreground_operations:
            messagebox.showwarning("Current operation is incomplete", "Wait for the current operation before loading state. ", parent=self.root)
            return
        before = self._state_edit_snapshot()
        self._state_load_pending = True
        self._job_command_pending = True

        def read() -> dict[str, Any]:
            if active_jobs(self.controller.jobs()):
                raise ValueError("has queued/running Job; Load state after the job finishes. Current settings are unchanged. ")
            report = dict(self.controller.load_state(identifier))
            job_id = str(_mapping(_mapping(report.get("state")).get("view")).get("job_id") or "")
            if job_id:
                try:
                    job = self.controller.job(job_id)
                    if job.get("status") not in TERMINAL_STATES:
                        raise ValueError("The saved job is still active")
                    report["job"] = job
                    report["result"] = self.controller.result(job_id)
                except Exception as exc:
                    report["result_warning"] = f"Saved result {job_id} unreadable: {exc}; not replaced with another job. "
            return report

        def finished(report: Any) -> None:
            self._state_load_pending = False
            self._job_command_pending = False
            if self._state_edit_snapshot() != before:
                messagebox.showwarning("Settings updated", "The form changed while loading, New settings were not overwritten, Reload. ", parent=self.root)
                return
            self._apply_saved_state(_mapping(report))

        def failed(_exc: Exception) -> None:
            self._state_load_pending = False
            self._job_command_pending = False

        self._submit("Load state", read, finished, on_failure=failed)

    def _apply_saved_state(self, report: Mapping[str, Any]) -> None:
        state = _mapping(report.get("state"))
        view = _mapping(state.get("view"))
        self._config_loaded(_mapping(state.get("config")))
        self.last_preflight = None
        self.preflight_status_var.set("State loaded; Detect, bind and run preflight again, Not executed automatically")
        self.binding_status_var.set("State restores settings only; Detect and bind both again TB")
        self.allow_execute_var.set(False)
        self.noise_kind_var.set(view.get("noise_kind") if view.get("noise_kind") in {"both", "thermal", "flicker"} else "both")
        self.log_offset_var.set(view.get("x_scale") != "linear")
        self.theme_var.set("dark" if view.get("theme") == "dark" else "light")
        self.advanced_visible_var.set(view.get("advanced_visible") is not True)
        self._toggle_advanced()
        self._apply_theme()
        if self._poll_after_id is not None:
            self.root.after_cancel(self._poll_after_id)
            self._poll_after_id = None
        self._clear_loaded_result()
        self.current_job_id = ""
        self.current_job = {}
        result = report.get("result")
        job_id = str(view.get("job_id") or "")
        warning = str(report.get("result_warning") or "")
        if isinstance(result, Mapping) and job_id:
            self.current_job_id = job_id
            self.current_job = _mapping(report.get("job"))
            self.operating_point_var.set(str(view.get("operating_point_id") or ""))
            requested = str(view.get("point") or "")
            if not requested:
                result = None
                warning += " No result sample was selected when saved, No other sample selected automatically. "
            else:
                self._result_loaded(result, job_id)
            choice = next((label for label, point in self._offset_point_choices.items() if point == requested), None)
            if requested and choice is None:
                # A saved coordinate is not permission to pick another sample.
                self._clear_loaded_result()
                self.current_job_id = ""
                self.current_job = {}
                warning += " Saved operating point / Offset not found, Other samples are not shown. "
            elif choice:
                self.result_offset_var.set(choice)
                self._result_offset_changed()
        tabs = {"setup": self.setup_tab, "run": self.run_tab, "results": self.result_tab, "errors": self.error_tab}
        self.notebook.select(tabs.get(str(view.get("selected_tab")), self.setup_tab))
        for canvas in self._page_canvases.values():
            canvas.yview_moveto(0)
        if warning:
            messagebox.showwarning("State loaded, Results need review", warning, parent=self.root)

    def _save_config_dialog(self) -> None:
        try:
            config = self._config_from_ui()
        except Exception as exc:
            self._show_error(exc, "Invalid configuration")
            return
        initial = str(self.controller.workspace / "config.json")
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save configuration JSON",
            initialdir=str(Path(initial).parent),
            initialfile=Path(initial).name,
            defaultextension=".json",
            filetypes=(("JSON", "*.json"), ("All files", "*")),
        )
        if not path:
            return

        def saved(report: Any) -> None:
            value = _mapping(report)
            self.raw_config = _mapping(value.get("config")) or config
            self._set_raw_text(self.raw_config)
            backup = value.get("backup")
            suffix = f"; Backup: {backup}" if backup else ""
            messagebox.showinfo("Configuration saved", f"{value.get('path', path)}{suffix}", parent=self.root)

        self._submit("Save configuration", lambda: self.controller.save_config(config, path), saved)

    @staticmethod
    def _unique_candidate(findings: Sequence[Any], score_key: str) -> dict[str, Any] | None:
        ranked = sorted(
            (dict(item) for item in findings if isinstance(item, Mapping)),
            key=lambda item: (-float(item.get(score_key) or 0), str(item.get("path") or "")),
        )
        if not ranked or float(ranked[0].get(score_key) or 0) <= 0:
            return None
        if len(ranked) > 1 and float(ranked[0].get(score_key) or 0) == float(ranked[1].get(score_key) or 0):
            return None
        return ranked[0]

    def _discover(self) -> None:
        root = str(self.vars["project_root"].get() or "").strip()
        if not root:
            messagebox.showwarning("Project root required", "Select the project root for a read-only scan first. ", parent=self.root)
            return
        payload = {"roots": [root], "max_depth": 6, "max_results": 100}

        def discovered(report: Any) -> None:
            data = _mapping(report)
            findings = _sequence(data.get("findings"))
            paths = [str(item.get("path")) for item in findings if isinstance(item, Mapping) and item.get("path")]
            self.vco_path_box.configure(values=paths)
            self.replica_path_box.configure(values=paths)
            vco = self._unique_candidate(findings, "vco_score")
            replica = self._unique_candidate(findings, "flicker_score")
            generated = self._generated_reference()
            if vco and (generated or replica and vco.get("path") != replica.get("path")):
                self._updating_form = True
                try:
                    if not self.vars["vco_path"].get():
                        self.vars["vco_path"].set(vco["path"])
                    if not generated and not self.vars["replica_path"].get():
                        self.vars["replica_path"].set(replica["path"])
                finally:
                    self._updating_form = False
                self.binding_status_var.set("Found a unique VCO Candidates, Read-only detection in progress…" if generated
                                            else "Found one candidate for each role, Read-only detection in progress…")
                if generated:
                    self._schedule_role_inspection("vco")
                else:
                    self._inspect_pair()
            else:
                detail = "Select manually VCO Path" if generated else "Select two different paths manually"
                self.binding_status_var.set(f"Discover {len(paths)} items Maestro; Ambiguous role, {detail}. ")

        self._submit("Discover Maestro", lambda: self.controller.discover_maestro(payload), discovered)

    def _inspect_pair(self) -> None:
        try:
            config = self._config_from_ui()
        except Exception as exc:
            self._show_error(exc, "Invalid configuration")
            return
        flat = flatten_config(config)
        generated = flat.get("replica_source") == "generated"
        if not flat.get("vco_path") or (not generated and not flat.get("replica_path")):
            detail = "Select VCO of Maestro Path. " if generated else "Select two independent Maestro Path. "
            messagebox.showwarning("Maestro Not selected", detail, parent=self.root)
            return
        if generated and not flat.get("vco_history"):
            self.binding_status_var.set("Select a complete VCO History; Reading available records. ")
            self._schedule_role_inspection("vco", refresh=True)
            return
        payload = maestro_pair_payload(config, include_fingerprints=False)
        submitted_selection = self._pair_selection()
        submitted_revision = getattr(self, "_selection_revision", 0)

        def inspected(value: Any) -> None:
            if self._pair_selection() != submitted_selection or getattr(self, "_selection_revision", 0) != submitted_revision:
                self.binding_status_var.set("Selection changed; Stale detection results discarded, Detect again")
                return
            self._pair_inspected(value)

        self._submit("Check VCO and device noise reference", lambda: self.controller.inspect_pair(payload), inspected)

    def _pair_inspected(self, value: Any) -> None:
        report = _mapping(value)
        roles = _mapping(report.get("roles"))
        bindings = _mapping(report.get("bindings"))
        if self._restore_project_mapping(_mapping(roles.get("vco"))):
            return
        generated = _mapping(report.get("pair")).get("replica_source") == "generated" or self._generated_reference()
        bound_roles = (("vco", "vco"),) if generated else (("vco", "vco"), ("replica", "flicker"))
        choices: dict[str, dict[str, Any]] = {}
        accepted = bool(report.get("ok"))
        self._updating_form = True
        try:
            for ui_role, report_role in bound_roles:
                role = _mapping(roles.get(report_role))
                tests = [str(name) for name in _sequence(role.get("test_names")) if name]
                getattr(self, f"{ui_role}_test_box").configure(values=tests)
                selected_test = _mapping(role.get("selected_test"))
                test_name = str(selected_test.get("name") or self.vars[f"{ui_role}_test"].get() or "")
                if test_name:
                    self.vars[f"{ui_role}_test"].set(test_name)
                histories = [dict(item) for item in _sequence(role.get("histories")) if isinstance(item, Mapping)]
                requested = str(self.vars[f"{ui_role}_history"].get() or "")
                spec = _mapping(_mapping(getattr(self, "raw_config", {}).get("testbenches")).get(ui_role))
                choice = select_history_candidate(histories, requested, test_name, explicit=bool(spec.get("explicit_history_selection")))
                choices[ui_role] = choice
                complete = choice.get("complete_histories", [])
                self._set_history_choices(ui_role, complete)
                selected = _mapping(choice.get("selected"))
                selected_value = str(selected.get("path") or selected.get("name") or "")
                self.vars[f"{ui_role}_history"].set(selected_value or requested)
                self._sync_history_label(ui_role)
                binding = _mapping(bindings.get(report_role))
                server_selected = _mapping(role.get("selected_history"))
                server_value = str(server_selected.get("path") or server_selected.get("name") or "")
                server_matches = bool(selected_value and selected_value == server_value)
                ready = bool(
                    report.get("ok")
                    and selected
                    and server_matches
                    and binding.get("binding_fingerprint")
                    and binding.get("history_fingerprint")
                )
                if ready:
                    self.vars[f"{ui_role}_path"].set(binding.get("maestro_dir") or role.get("maestro_dir") or "")
                    self.vars[f"{ui_role}_test"].set(binding.get("test") or test_name)
                    self.vars[f"{ui_role}_history"].set(binding.get("history") or selected_value)
                    self.vars[f"{ui_role}_binding_fingerprint"].set(binding.get("binding_fingerprint") or "")
                    self.vars[f"{ui_role}_history_fingerprint"].set(binding.get("history_fingerprint") or "")
                else:
                    self.vars[f"{ui_role}_binding_fingerprint"].set("")
                    self.vars[f"{ui_role}_history_fingerprint"].set("")
                    accepted = False
                if hasattr(self, "_observed_role_paths"):
                    self._observed_role_paths[ui_role] = str(self.vars[f"{ui_role}_path"].get() or "")
                self._sync_history_label(ui_role)
        finally:
            self._updating_form = False

        issues = [dict(item) for item in _sequence(report.get("issues")) if isinstance(item, Mapping)]
        manual = [role for role, choice in choices.items() if choice.get("requires_manual")]
        ambiguous_tests = ["VCO" if role == "vco" else "Noise TB" for role in ("vco", "flicker")
                           if len(_sequence(_mapping(roles.get(role)).get("test_names"))) > 1
                           and not _mapping(_mapping(roles.get(role)).get("selected_test"))]
        unresolved_fields = [row for row in mapping_review_rows(report) if row.get("status") in {"ambiguous", "invalid", "missing", "stale"} and row.get("required", True)]
        if accepted:
            self.binding_status_var.set("VCO History and target MOS Confirmed; Device reference is generated after starting. " if generated
                                        else "two TB and complete history are bound. ")
            self.raw_config = self._config_from_ui()
            self._set_raw_text(self.raw_config)
        elif ambiguous_tests:
            self.binding_status_var.set(", ".join(ambiguous_tests) + " has multiple Maestro Test; Use Test select from the list. select devices or current-sensing sources separately in advanced mappings. ")
        elif unresolved_fields:
            names = ", ".join(("VCO " if row["role"] == "vco" else "Noise TB ") + str(row.get("label") or row["key"]) for row in unresolved_fields[:4])
            if generated and any(row["key"] == "target_instance" for row in unresolved_fields):
                self.binding_status_var.set("Select the target in Device noise reference MOS. ")
            else:
                self.binding_status_var.set("Needs manual mapping: " + names + "; select or enter each advanced mapping, then apply and validate. ")
        elif manual:
            labels = ", ".join("VCO" if role == "vco" else "Flicker" for role in manual)
            self.binding_status_var.set(f"{labels} Multiple complete candidates exist for history; no guessing, Select manually, then detect again")
        elif issues:
            summary = "; ".join(f"{item.get('code', 'INFO')} {item.get('message') or item.get('title') or item.get('detail') or ''}" for item in issues[:3])
            self.binding_status_var.set(summary)
        else:
            self.binding_status_var.set("Could not obtain verifiable VCO History, Check the selection and retry. " if generated
                                        else "Could not obtain two verifiable complete History, Check the selection and retry. ")
        self.last_preflight = None
        self.preflight_status_var.set("Binding detected, Preflight required")
        self._render_mapping_report(report)

    # ---- preflight and jobs ----------------------------------------

    def _build_run_tab(self) -> None:
        content = self._scrollable_page(self.run_tab)
        readiness = ttk.LabelFrame(
            content, text="Ready to run", style="Card.TLabelframe", padding=(14, 12)
        )
        readiness.pack(fill="x", pady=(0, 10))
        readiness.columnconfigure(1, weight=1)
        ttk.Label(
            readiness, textvariable=self.execution_summary_var, font=self.heading_font
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            readiness, textvariable=self.preflight_status_var, style="Help.TLabel"
        ).grid(row=0, column=1, sticky="e")
        self._help(
            readiness,
            textvariable=self.preflight_note_var,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 9))
        actions = ttk.Frame(readiness)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew")
        ttk.Button(
            actions, text="1  Run static preflight", command=self._preflight
        ).pack(side="left")
        self.start_button = ttk.Button(
            actions, text="2  Start live run", style="Primary.TButton", command=self._start_run
        )
        self.start_button.pack(side="left", padx=8)
        ttk.Button(actions, text="Refresh jobs", command=self._refresh_jobs).pack(side="left", padx=(10, 4))
        ttk.Button(actions, text="Resume failed/Interrupt job", command=self._resume_job).pack(side="left", padx=4)
        ttk.Button(actions, text="Import Replay Bundle", command=self._import_bundle).pack(side="left", padx=4)
        stop_actions = ttk.Frame(readiness)
        stop_actions.grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.stop_offset_button = ttk.Button(stop_actions, text="Stop after current offset", command=self._stop_after_current_offset, state="disabled")
        self.partial_result_button = ttk.Button(stop_actions, text="View completed results", command=self._load_available_result, state="disabled")
        self.partial_result_button.pack(side="left", padx=8)
        self.preflight_help = self._help(readiness, textvariable=self.preflight_detail_var)
        self.preflight_help.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        self.preflight_issue_actions = ttk.Frame(readiness)
        self.preflight_issue_actions.grid(row=6, column=0, columnspan=2, sticky="w", pady=(3, 0))
        self.preflight_locate_button = ttk.Button(self.preflight_issue_actions, text="Locate missing items", command=self._locate_preflight_issue)
        self.preflight_locate_button.pack(side="left")
        ttk.Button(self.preflight_issue_actions, text="View all issues", command=self._show_preflight_issues).pack(side="left", padx=8)
        self.preflight_help.grid_remove()
        self.preflight_issue_actions.grid_remove()

        pane = ttk.Panedwindow(content, orient="vertical")
        pane.pack(fill="both", expand=True)
        upper = ttk.Panedwindow(pane, orient="horizontal")
        lower = ttk.LabelFrame(
            pane, text="Current job events", style="Card.TLabelframe", padding=8
        )
        pane.add(upper, weight=3)
        pane.add(lower, weight=2)

        jobs_frame = ttk.LabelFrame(
            upper, text="Job", style="Card.TLabelframe", padding=8
        )
        checks_frame = ttk.LabelFrame(
            upper, text="Preflight matrix", style="Card.TLabelframe", padding=8
        )
        upper.add(jobs_frame, weight=2)
        upper.add(checks_frame, weight=3)
        self.jobs_tree = self._tree(
            jobs_frame,
            (("id", "Job ID", 190), ("status", "State", 80), ("message", "Description", 220)),
        )
        self._with_scroll(jobs_frame, self.jobs_tree).pack(fill="both", expand=True)
        self.jobs_tree.bind("<<TreeviewSelect>>", self._job_selected)
        self.preflight_tree = self._tree(
            checks_frame,
            (("status", "State", 65), ("id", "Check", 140), ("detail", "Details", 360), ("code", "Code", 90)),
        )
        self._with_scroll(checks_frame, self.preflight_tree).pack(fill="both", expand=True)

        ttk.Label(lower, textvariable=self.job_status_var).pack(anchor="w")
        self.job_progress = ttk.Progressbar(lower, mode="determinate", maximum=100)
        self.job_progress.pack(fill="x", pady=(6, 7))
        event_frame = ttk.Frame(lower)
        event_frame.pack(fill="both", expand=True)
        self.job_log = tk.Text(
            event_frame, height=9, wrap="word", state="disabled", font=self.fixed_font, borderwidth=0, spacing1=2, spacing3=3
        )
        self.job_log.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(event_frame, orient="vertical", command=self.job_log.yview)
        log_scroll.pack(side="right", fill="y")
        self.job_log.configure(yscrollcommand=log_scroll.set)

    def _preflight(self) -> None:
        try:
            config = self._execution_config_from_ui()
        except Exception as exc:
            self._show_error(exc, "Invalid configuration")
            return
        def rendered(value: Any) -> None:
            if not self._config_still_current(config):
                self.last_preflight = None
                self.preflight_status_var.set("Configuration changed; Stale preflight results discarded")
                return
            self._render_preflight(value)
            self._remember_project_preferences(config, _mapping(value))

        self._submit("Run preflight", lambda: self.controller.preflight(config), rendered)

    def _render_preflight(self, value: Any) -> None:
        report = _mapping(value)
        self.last_preflight = report
        resolved = _mapping(report.get("resolved_config"))
        detected_engine = str(resolved.get("config_path") or "").strip()
        if detected_engine and not str(self.vars["engine_config"].get() or "").strip():
            # Auto-materialized paths must not become persistent overrides.
            self._auto_engine_path = detected_engine
        portable = _mapping(_mapping(resolved.get("detection")).get("portable_setup"))
        pair_report = _mapping(_mapping(report.get("detected")).get("maestro_pair"))
        if pair_report:
            self._render_mapping_report(pair_report)
        if portable:
            self._render_portable_report(portable)
        self._preflight_issues = portable_issue_rows(portable, pair_report or self._signal_report)
        self._clear_tree(self.preflight_tree)
        for index, item in enumerate(_sequence(report.get("checks"))):
            if not isinstance(item, Mapping):
                continue
            status = str(item.get("status") or "warn").lower()
            self.preflight_tree.insert(
                "",
                "end",
                iid=f"check-{index}",
                values=(
                    status.upper(),
                    item.get("label") or item.get("id") or "?",
                    item.get("detail") or item.get("message") or "",
                    item.get("code") or "",
                ),
                tags=(status,),
            )
        field_labels = {
            "config_path": "Advanced settings → Simulation execution settings (first detect and bind both TB)",
            "physical_profile": "Advanced settings → device indicated in the execution settings / Node mapping",
            "portable_setup": "Advanced settings → Simulation execution settings",
            "execution.allow_execute": "Current execution request was not authorized, Click Preflight or Start again",
        }
        required = [
            field_labels.get(str(item), str(item))
            for item in _sequence(report.get("required_user_fields"))
        ]
        summary = _mapping(report.get("summary"))
        warnings = int(summary.get("warn") or 0)
        failures = int(summary.get("fail") or 0)
        if report.get("ok"):
            suffix = f" · {warnings} warnings" if warnings else ""
            self.preflight_status_var.set(f"Static preflight passed{suffix}")
            self.preflight_detail_var.set("")
            self._preflight_messages = []
            self.preflight_help.grid_remove()
            self.preflight_issue_actions.grid_remove()
        else:
            self.preflight_status_var.set(f"Static preflight failed · {failures} failed items")
            details = [row["message"] for row in self._preflight_issues]
            for item in _sequence(report.get("checks")):
                if not isinstance(item, Mapping) or item.get("status") != "fail":
                    continue
                if self._preflight_issues and item.get("field") in {"portable_setup", "physical_profile", "config_path"}:
                    continue
                details.append(str(item.get("label") or item.get("id") or "Check") + ": " + str(item.get("detail") or item.get("message") or "Check this item"))
            if not details and required:
                details = ["Needs input: " + ", ".join(required)]
            self._preflight_messages = details
            text = "\n".join(f"{index + 1}. {detail}" for index, detail in enumerate(details[:3]))
            if len(details) > 3:
                text += f"\nalso {len(details) - 3} items, Click“View all issues”. "
            self.preflight_detail_var.set(text)
            self.preflight_help.grid()
            self.preflight_issue_actions.grid()
            self.preflight_locate_button.configure(state="normal" if self._preflight_issues else "disabled",
                                                   text="to select: " + self._preflight_issues[0]["target_label"] if self._preflight_issues else "Locate missing items")

    def _show_preflight_issues(self) -> None:
        if not self.last_preflight:
            self.preflight_status_var.set("Configuration changed, Run preflight again first")
            return
        lines = [row["message"] + "\nLocation: Advanced settings → " + ("Signal roles and cross- TB Mapping → " + row["target_label"] if row["tab"] == "mapping" else "Simulation execution settings → " + row["target_label"])
                 for row in self._preflight_issues]
        portable_messages = {row["message"] for row in self._preflight_issues}
        lines.extend(line for line in self._preflight_messages if line not in portable_messages)
        messagebox.showinfo("Preflight issues", "\n\n".join(f"{index + 1}. {line}" for index, line in enumerate(lines)) or self.preflight_detail_var.get(), parent=self.root)

    def _locate_preflight_issue(self) -> None:
        if not self.last_preflight or not self._preflight_issues:
            self.preflight_status_var.set("Configuration changed, Run preflight again first")
            return
        row = self._preflight_issues[0]
        if row["tab"] == "mapping" and row["mapping_iid"] in self._mapping_rows:
            self._open_advanced_tab(self.mapping_tab)
            self._focus_mapping_input(row["mapping_iid"])
        else:
            self._open_advanced_tab(self.physical_tab)
            if row["field"] in self._portable_rows:
                self.portable_tree.selection_set(row["field"])
                self.portable_tree.see(row["field"])
                self._portable_selected()
                self.portable_edit_box.focus_set()

    def _start_run(self) -> None:
        if self._close_check_pending:
            messagebox.showwarning("Checking close request", "Wait for the close check before submitting. ", parent=self.root)
            return
        if self._job_command_pending:
            messagebox.showwarning("Submitting job", "A run or resume command is already being submitted, Do not submit again. ", parent=self.root)
            return
        try:
            config = self._execution_config_from_ui()
        except Exception as exc:
            self._show_error(exc, "Invalid configuration")
            return
        self._job_command_pending = True

        def command_failed(_exc: Exception) -> None:
            self._job_command_pending = False

        def after_preflight(report: Any) -> None:
            if not self._config_still_current(config):
                self._job_command_pending = False
                self.last_preflight = None
                self.preflight_status_var.set("Configuration changed; Execution blocked, Run preflight again")
                messagebox.showwarning(
                    "Configuration changed",
                    "Preflight used an old configuration, No job created. Run preflight again before execution. ",
                    parent=self.root,
                )
                return
            try:
                self._render_preflight(report)
            except Exception:
                self._job_command_pending = False
                raise
            if not _mapping(report).get("ok"):
                self._job_command_pending = False
                messagebox.showerror("Execution blocked", "Preflight failed, No job created. \n\n" + self.preflight_detail_var.get(), parent=self.root)
                return
            submitted = flatten_config(config)
            run_root = str(submitted.get("run_root") or "").strip() or None
            bundle_path = str(submitted.get("bundle_path") or "").strip() or None

            def started(value: Any) -> None:
                self._run_started(value)
                self._remember_project_preferences(config, _mapping(report))

            self._submit(
                "Submit background job",
                lambda: self.controller.start_run(config, run_root=run_root, bundle_path=bundle_path),
                started,
                on_failure=command_failed,
            )

        def begin_preflight() -> None:
            self._submit(
                "Preflight before execution",
                lambda: self.controller.preflight(config),
                after_preflight,
                on_failure=command_failed,
            )

        def after_active_jobs(value: Any) -> None:
            running = active_jobs(_mapping(value))
            if running:
                self._job_command_pending = False
                identifiers = ", ".join(str(item.get("id") or "?") for item in running)
                messagebox.showwarning(
                    "An active job already exists",
                    "existing queued/running Job, No new job submitted: " + identifiers,
                    parent=self.root,
                )
                return
            begin_preflight()

        self._submit(
            "Check active job",
            self.controller.jobs,
            after_active_jobs,
            on_failure=command_failed,
        )

    def _run_started(self, value: Any) -> None:
        self._job_command_pending = False
        self._clear_loaded_result()
        self.current_job_id = str(value)
        self.job_status_var.set(f"Job {self.current_job_id} Submitted")
        self.notebook.select(self.run_tab)
        self._refresh_jobs(silent=True)
        self._schedule_poll(100)

    def _refresh_jobs(self, silent: bool = False) -> None:
        self._submit("Refresh jobs", self.controller.jobs, self._render_jobs, quiet=silent)

    def _render_jobs(self, value: Any) -> None:
        payload = _mapping(value)
        selected = self.current_job_id
        self._clear_tree(self.jobs_tree)
        for item in _sequence(payload.get("jobs")):
            if not isinstance(item, Mapping):
                continue
            job_id = str(item.get("id") or "")
            self.jobs_tree.insert(
                "",
                "end",
                iid=job_id,
                values=(job_id, item.get("status") or "", item.get("message") or ""),
            )
        if selected and self.jobs_tree.exists(selected):
            self.jobs_tree.selection_set(selected)
            self.jobs_tree.see(selected)

    def _job_selected(self, _event: tk.Event[Any] | None = None) -> None:
        selection = self.jobs_tree.selection()
        if not selection:
            return
        selected = str(selection[0])
        # A programmatic selection is emitted again whenever the list is
        # rebuilt. Ignore it so terminal jobs cannot enter a tight poll loop.
        if selected == self.current_job_id:
            return
        self._clear_loaded_result()
        self.current_job_id = selected
        self._schedule_poll(0)

    def _schedule_poll(self, delay_ms: int = 1200) -> None:
        if self._poll_after_id is not None:
            try:
                self.root.after_cancel(self._poll_after_id)
            except Exception:
                pass
        self._poll_after_id = self.root.after(delay_ms, self._poll_job)

    def _poll_job(self) -> None:
        self._poll_after_id = None
        if not self.current_job_id:
            return
        if self._poll_pending:
            self._schedule_poll(100)
            return
        job_id = self.current_job_id
        self._poll_pending = True

        def success(value: Any) -> None:
            self._poll_pending = False
            if job_id != self.current_job_id:
                if self.current_job_id:
                    self._schedule_poll(0)
                return
            self._render_job(value)

        def failed(exc: Exception) -> None:
            self._poll_pending = False
            if job_id != self.current_job_id:
                if self.current_job_id:
                    self._schedule_poll(0)
                return
            self._show_error(exc, "Failed to read job")

        self.queue.submit(lambda: self.controller.job(job_id), success, failed)

    def _render_job(self, value: Any) -> None:
        job = _mapping(value)
        rendered_job_id = str(job.get("id") or self.current_job_id)
        if rendered_job_id != self.current_job_id:
            return
        self.current_job = job
        status = str(job.get("status") or "unknown")
        if hasattr(self, "stop_offset_button"):
            if job.get("is_offset_sweep"):
                self.stop_offset_button.pack(side="left", before=self.partial_result_button)
            else:
                self.stop_offset_button.pack_forget()
            self.stop_offset_button.configure(state="normal" if job.get("is_offset_sweep") and status in ACTIVE_JOB_STATES and not job.get("cancel_requested") else "disabled")
            self.partial_result_button.configure(state="normal" if job.get("result_available") else "disabled")
        progress = float(job.get("progress") or 0)
        self.job_progress["value"] = max(0.0, min(progress, 100.0))
        self.job_status_var.set(f"{job.get('id', self.current_job_id)} · {status} · {job.get('message') or ''}")
        sweep = _mapping(job.get("offset_sweep"))
        if sweep:
            self.job_status_var.set(self.job_status_var.get() + f" · Completed {len(_sequence(sweep.get('completed_offsets_hz')))}/{len(_sequence(sweep.get('offsets_hz')))} offsets")
        lines = []
        for event in _sequence(job.get("events")):
            if not isinstance(event, Mapping):
                continue
            lines.append(
                "[{timestamp}] {status:<11} {stage}: {message}".format(
                    timestamp=event.get("timestamp") or "?",
                    status=str(event.get("status") or "?").upper(),
                    stage=event.get("stage") or "event",
                    message=event.get("message") or "",
                )
            )
        if job.get("error"):
            lines.append("ERROR " + json.dumps(job["error"], ensure_ascii=False, indent=2))
        self.job_log.configure(state="normal")
        self.job_log.delete("1.0", "end")
        self.job_log.insert("1.0", "\n".join(lines) or "No events yet")
        self.job_log.configure(state="disabled")
        self._refresh_jobs(silent=True)
        if status in ACTIVE_JOB_STATES:
            self._schedule_poll()
        elif (
            status in TERMINAL_STATES
            and job.get("result_available")
            and (self.current_result_job_id != rendered_job_id or not getattr(self, "_result_terminal_loaded", True))
            and self._result_load_pending_job_id != rendered_job_id
        ):
            job_id = rendered_job_id
            self._result_load_pending_job_id = job_id

            def loaded(result: Any) -> None:
                if self._result_load_pending_job_id == job_id:
                    self._result_load_pending_job_id = ""
                self._result_loaded(result, job_id)

            def failed(_exc: Exception) -> None:
                if self._result_load_pending_job_id == job_id:
                    self._result_load_pending_job_id = ""

            self._submit(
                "Read results",
                lambda: self.controller.result(job_id),
                loaded,
                quiet=True,
                on_failure=failed,
            )

    def _stop_after_current_offset(self) -> None:
        if not self.current_job_id:
            return
        job_id = self.current_job_id
        def stopped(_value: Any) -> None:
            if self.current_job_id == job_id:
                self.job_status_var.set("Stop requested: The current offset will finish completely, completed results are retained; remaining offsets can be resumed later. ")
                self.stop_offset_button.configure(state="disabled")
                self._schedule_poll(0)
        self._submit("Request stop after current offset", lambda: self.controller.stop_after_current_offset(job_id), stopped)

    def _load_available_result(self) -> None:
        if not self.current_job_id:
            return
        job_id = self.current_job_id
        self._submit("Read completed results", lambda: self.controller.result(job_id), lambda value: self._result_loaded(value, job_id))

    def _resume_job(self) -> None:
        if self._close_check_pending:
            messagebox.showwarning("Checking close request", "Wait for the close check before resuming. ", parent=self.root)
            return
        if self._job_command_pending:
            messagebox.showwarning("Submitting job", "A run or resume command is already being submitted, Do not submit again. ", parent=self.root)
            return
        if not self.current_job_id:
            messagebox.showwarning("No job selected", "Select failed or interrupted Job. ", parent=self.root)
            return
        job_id = self.current_job_id
        self._job_command_pending = True

        def resumed(_value: Any) -> None:
            self._job_command_pending = False
            if self.current_job_id == job_id:
                # A previously viewed interrupted result is a partial snapshot,
                # not the terminal result of this new execution attempt.
                self._result_terminal_loaded = False
                self.job_status_var.set(f"Job {job_id} Resume submitted")
            self._schedule_poll(100)

        def command_failed(_exc: Exception) -> None:
            self._job_command_pending = False

        def after_active_jobs(value: Any) -> None:
            running = active_jobs(_mapping(value))
            if running:
                self._job_command_pending = False
                identifiers = ", ".join(str(item.get("id") or "?") for item in running)
                messagebox.showwarning(
                    "An active job already exists",
                    "existing queued/running Job, No other job was resumed: " + identifiers,
                    parent=self.root,
                )
                return
            self._submit(
                "Resume job",
                lambda: self.controller.resume(job_id),
                resumed,
                on_failure=command_failed,
            )

        self._submit(
            "Check active job",
            self.controller.jobs,
            after_active_jobs,
            on_failure=command_failed,
        )

    def _import_bundle(self) -> None:
        path = filedialog.askdirectory(parent=self.root, title="Select canonical replay bundle Directory")
        if path:
            self._import_bundle_path(path)

    def _load_example(self) -> None:
        """Import the bundled synthetic ideal-LC bundle (generated on demand when not shipped)."""
        shipped = Path(__file__).resolve().parents[2] / "examples" / "ideal_lc"
        if (shipped / "vco_results.csv").is_file():
            path = shipped
        else:
            from ..example_data import build
            path = Path(build(str(Path(self.controller.workspace) / "examples" / "ideal_lc")))
        self.noise_kind_var.set("both")
        self._import_bundle_path(str(path), label="Example ideal-LC")

    def _import_bundle_path(self, path: str, label: str = "") -> None:
        def imported(value: Any) -> None:
            report = _mapping(value)
            self._clear_loaded_result()
            self.current_job_id = str(report.get("job_id") or "")
            self.job_status_var.set(f"Replay Imported: {self.current_job_id}")
            self._set_window_title(label or Path(path).name)
            self._refresh_jobs(silent=True)
            self._schedule_poll(0)

        self._submit("Import canonical replay", lambda: self.controller.import_bundle(path, "canonical"), imported)

    # ---- results ----------------------------------------------------

    def _clear_loaded_result(self) -> None:
        if hasattr(self, "results_view"):
            self.results_view.clear()
        self._offset_point_choices = {}
        self.current_result = None
        self.current_result_job_id = ""
        self.current_result_view = None
        self.point_var.set("")
        self.point_box.configure(values=())
        self._clear_tree(self.summary_tree)
        self._clear_tree(self.harmonic_tree)
        self._clear_tree(self.noise_contribution_tree)
        self.simulated_noise_pie.clear()
        self.calculated_noise_pie.clear()
        self.noise_pie_note_var.set("No current job results")
        self.noise_contribution_note_var.set("No current job results")
        self.result_quality_var.set("")
        self.isf_chart.clear("N/A")
        self.qk_chart.clear("N/A")
        self.metrics_var.set("No current job results")
        self._result_terminal_loaded = False
        self._curve_cache = {}
        if hasattr(self, "operating_point_var"):
            self.operating_point_var.set("")
            self.result_offset_var.set("")
            self.operating_point_box.configure(values=())
            self.result_offset_box.configure(values=())
            self.phase_noise_chart.clear("N/A")
            if hasattr(self, "phase_error_chart"):
                self.phase_error_chart.clear("No matched-scope HBnoise Reference")
                self._phase_note.set("Waiting for results")
                self._phase_menu.delete(0, "end")
                self._phase_visibility.clear()
                self._phase_data_key = None
                self._phase_full_x = None
                self._reset_phase_view()
            self.harmonic_noise_chart.clear("N/A")
            self._clear_tree(self.metric_values_tree)

    def _build_result_tab(self) -> None:
        from .results_view import ResultsView
        self.results_view = ResultsView(self, self.result_tab)

    def _result_loaded(self, value: Any, job_id: str) -> None:
        if getattr(self, "_result_load_pending_job_id", "") == str(job_id):
            self._result_load_pending_job_id = ""
        if str(job_id) != self.current_job_id:
            if self.current_job_id:
                self._schedule_poll(0)
            return
        self.current_result = _mapping(value)
        # The direct service lookup is authoritative.  Imported replay data may
        # legitimately retain an original source job_id inside its evidence.
        self.current_result_job_id = str(job_id)
        self._result_terminal_loaded = str(self.current_job.get("status")) in TERMINAL_STATES
        self._curve_cache = {}
        points = sorted(str(name) for name in _mapping(self.current_result.get("points")))
        self.point_box.configure(values=points)
        try:
            self._operating_points = operating_points(self.current_result)
        except Exception as exc:
            self._show_error(exc, "Result operating point / invalid offset identity")
            return
        if not self._operating_points:
            self._operating_points = [{"id": point, "label": point, "points": [point], "offsets_hz": []} for point in points]
        identities = [str(item["id"]) for item in self._operating_points]
        self.operating_point_box.configure(values=identities)
        if self.operating_point_var.get() not in identities:
            self.operating_point_var.set(identities[0] if identities else "")
        self._operating_point_changed()
        if points:
            self.notebook.select(self.result_tab)
        else:
            run_kind = str(self.current_result.get("run_kind") or "").lower()
            if run_kind == "plan":
                self.metrics_var.set("Planning complete · No simulation started, so no numerical results are available")
                self.isf_chart.clear("Execution plan only")
                self.qk_chart.clear("Shown after live execution")
                self.phase_noise_chart.clear("Plan only, no numerical results")
                self.harmonic_noise_chart.clear("Plan only, no numerical results")
                self.job_status_var.set(
                    f"Job {job_id} Planning complete; for numerical results, use a live run"
                )

    def _operating_point_changed(self, _event: Any = None) -> None:
        if self.current_result is None:
            return
        identity = self.operating_point_var.get()
        try:
            choices = result_sample_choices(self.current_result, identity)
        except Exception as exc:
            self._show_error(exc, "Cannot select result offset")
            return
        if not choices:
            selected = next((item for item in self._operating_points if item["id"] == identity), {})
            choices = [{"point": point, "label": "Offset not recorded"} for point in selected.get("points", [])]
        self._offset_point_choices = {item["label"]: item["point"] for item in choices}
        self.result_offset_box.configure(values=tuple(self._offset_point_choices))
        if self.result_offset_var.get() not in self._offset_point_choices:
            self.result_offset_var.set(next(iter(self._offset_point_choices), ""))
        self._result_offset_changed()

    def _result_offset_changed(self, _event: Any = None) -> None:
        self.point_var.set(self._offset_point_choices.get(self.result_offset_var.get(), ""))
        self._point_changed()

    def _bind_metric_columns(self, tree: ttk.Treeview, fields: Sequence[str]) -> None:
        tree._metric_fields = tuple(fields)
        tree._metric_last_field = fields[0] if fields else ""
        for column, field in zip(tree["columns"], fields):
            tree.heading(column, command=lambda current=field: self._show_metric(current))
        def column_selected(event: Any, show: bool = False) -> None:
            try:
                index = int(tree.identify_column(event.x).lstrip("#")) - 1
            except ValueError:
                return
            if 0 <= index < len(fields):
                tree._metric_last_field = fields[index]
                if show:
                    self._show_metric(fields[index])
        tree.bind("<ButtonRelease-1>", column_selected, add="+")
        tree.bind("<Double-1>", lambda event: column_selected(event, True), add="+")
        tree.bind("<Return>", lambda _event: self._show_metric(tree._metric_last_field), add="+")

    def _show_metric(self, field: str) -> None:
        definition = metric_definition(field)
        if not definition:
            messagebox.showinfo("Metric details", "Select a metric first. Undefined extension fields retain their original values, no physical meaning is inferred. ", parent=self.root)
            return
        text = "\n\n".join((f"{definition['label']} ({definition['unit']})", "Meaning: " + definition["meaning"], "Purpose: " + definition["purpose"], "Scope: " + definition["scope"], "Formula: " + definition["formula"], "N/A means missing, not applicable or undefined; does not mean zero, nor a quality pass. "))
        messagebox.showinfo("Metric details · " + definition["label"], text, parent=self.root)

    def _show_selected_raw_metric(self, _event: Any = None) -> None:
        selected = self.metric_values_tree.selection()
        if selected:
            self._show_metric(self._metric_row_fields.get(selected[0], ""))

    def _render_metric_values(self, harmonic: Any = None) -> None:
        if self.current_result_view is None:
            return
        summary = _mapping(self.current_result_view.get("selected_summary"))
        rows = [("Operating point", key, value) for key, value in summary.items()
                if key not in LEGACY_DEVICE_COMPARISON_FIELDS]
        point = _mapping(_mapping(self.current_result.get("points")).get(self.point_var.get()))
        harmonics = [item for item in _sequence(point.get("harmonics")) if isinstance(item, Mapping)]
        selected = next((item for item in harmonics if str(item.get("k")) == str(harmonic)), harmonics[0] if harmonics else {})
        rows.extend((f"k={selected.get('k')}", key, value) for key, value in selected.items()
                    if key not in LEGACY_DEVICE_COMPARISON_FIELDS)
        self._clear_tree(self.metric_values_tree)
        self._metric_row_fields = {}
        for index, (scope, key, value) in enumerate(rows):
            definition = metric_definition(key)
            iid = "metric-" + str(index)
            self._metric_row_fields[iid] = key
            self.metric_values_tree.insert("", "end", iid=iid, values=(f"{scope} · {definition['label'] if definition else key}", _display(value), definition["unit"] if definition else "—"))

    def _select_contribution(self, label: str) -> None:
        for iid in self.noise_contribution_tree.get_children():
            values = self.noise_contribution_tree.item(iid, "values")
            if values and values[0] == label:
                self.noise_contribution_tree.selection_set(iid)
                self.noise_contribution_tree.see(iid)
                return

    def _select_plot_sample(self, chart: LineChart, event: Any) -> None:
        chart._hover(event)
        sample = _mapping((chart.hover_sample or {}).get("sample"))
        label = next((label for label, point in self._offset_point_choices.items() if point == sample.get("point")), None)
        if label is not None:
            self.result_offset_var.set(label)
            self._result_offset_changed()

    def _export_chart(self, chart: LineChart) -> None:
        if not chart.find_withtag("plot-frame"):
            messagebox.showwarning("No curves to export", "Select an operating point or noise kind with data. ", parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="Export current chart", defaultextension=".eps",
                                           filetypes=(("EPS vector chart", "*.eps"),))
        if path:
            try:
                chart._clear_hover()
                chart.postscript(file=path, colormode="color", width=chart.winfo_width(), height=chart.winfo_height())
            except (OSError, tk.TclError) as exc:
                self._show_error(exc, "Chart export failed")

    def _set_phase_view(self, limits: tuple[float, float] | None) -> None:
        self._phase_view_x = limits
        self.phase_noise_chart.set_x_limits(limits)
        self.phase_error_chart.set_x_limits(limits)

    def _reset_phase_view(self) -> None:
        self._set_phase_view(self._phase_full_x)

    def _navigate_phase_view(self, scale: float, anchor: float) -> None:
        if self._phase_view_x is None or self._phase_full_x is None:
            return
        lower, upper = self._phase_view_x
        full_lower, full_upper = self._phase_full_x
        span = upper - lower
        full_span = full_upper - full_lower
        width = min(full_span, max(full_span * 1e-5, span * scale))
        lower += anchor * (span if scale == 1. else span - width)
        lower = min(max(lower, full_lower), full_upper - width)
        self._set_phase_view((lower, lower + width))

    def _apply_phase_visibility(self) -> None:
        self.phase_noise_chart.set_hidden([name for name, variable in self._phase_visibility.items() if not variable.get()])

    def _render_phase_comparison(self, overview: Sequence[Mapping[str, Any]], comparison: Sequence[Mapping[str, Any]]) -> None:
        error = next((item for item in comparison if item["id"] == "matched_error"), None)
        curves = [dict(item) for item in overview]
        for item in curves:
            if item["id"] == "spectre":
                item.update(name="HBnoise · VCO all sources", dash=True)
        for item in comparison:
            if item["id"] != "matched_error" and any(point[1] is not None for point in item["points"]):
                curves.append(dict(item, dash=item["id"] == "matched_reference"))
        if any(item["id"] == "matched_prediction" for item in curves):
            for item in curves:
                if item["id"] == "calculated":
                    item["name"] = "ISF · symmetric estimate +3.01 dB"
        self.phase_noise_chart.set_series(curves)
        self.phase_error_chart.set_series([error] if error else [])
        log_x = self.log_offset_var.get()
        self.phase_noise_chart.set_axes(x_label="Offset (Hz)", y_label="Phase noise (dBc/Hz)", log_x=log_x)
        self.phase_error_chart.set_axes(x_label="Offset (Hz)", y_label="Δ (dB)", log_x=log_x, include_zero=True)
        data_key = (self.current_result_job_id, self.operating_point_var.get(), self.noise_kind_var.get())
        changed = data_key != self._phase_data_key
        curve_names = tuple(item["id"] for item in curves)
        if changed or curve_names != tuple(self._phase_visibility):
            previous = self._phase_visibility if not changed else {}
            self._phase_menu.delete(0, "end")
            self._phase_visibility = {}
            for item in curves:
                identifier = item["id"]
                variable = previous.get(identifier)
                if variable is None:
                    variable = tk.BooleanVar(master=self.root, value=True)
                self._phase_visibility[identifier] = variable
                self._phase_menu.add_checkbutton(label=item["name"], variable=variable, command=self._apply_phase_visibility)
            self._apply_phase_visibility()
        offsets = [x for item in overview for x, _ in item["points"]]
        full_x = None
        if offsets:
            low, high = min(offsets), max(offsets)
            if log_x:
                low, high = math.log10(low), math.log10(high)
            margin = .05 * (high - low) if low != high else (.15 if log_x else .15 * low)
            full_x = (low - margin, high + margin)
        if changed or self._phase_log_x != log_x or self._phase_full_x != full_x:
            self._phase_data_key, self._phase_log_x, self._phase_full_x = data_key, log_x, full_x
            self._reset_phase_view()
        scope = error["scope"] if error and any(point[1] is not None for point in error["points"]) else "matched-scope reference missing"
        self._phase_note.set(f"Error scope · {scope}     Ctrl + wheel to zoom · Middle-drag · double-click a sample for details")

    def _render_noise_curves(self) -> None:
        if self.current_result is None or self.current_result_job_id != self.current_job_id:
            return
        identity, scope, noise_kind = self.operating_point_var.get(), "pair", self.noise_kind_var.get()
        try:
            cache_key = (identity, "phase:" + scope + ":" + noise_kind)
            if cache_key not in self._curve_cache:
                self._curve_cache[cache_key] = phase_noise_series(self.current_result, identity, scope=scope, noise_kind=noise_kind)
            comparison_key = (identity, "matched:" + noise_kind)
            if comparison_key not in self._curve_cache:
                comparison = phase_noise_comparison_series(self.current_result, identity, noise_kind)
                self._curve_cache[comparison_key] = [
                    item for item in comparison.values() if item is not None
                ]
            self._render_phase_comparison(self._curve_cache[cache_key], self._curve_cache[comparison_key])
        except Exception as exc:
            self.phase_noise_chart.clear("Result lacks explicit offset / operating-point coordinates")
            self.phase_error_chart.clear("Cannot construct a matched-scope error")
            self.result_quality_var.set("Result curves need review: " + str(exc))

    def _point_changed(self, _event: tk.Event[Any] | None = None) -> None:
        if self.current_result_job_id == self.current_job_id:
            self._render_result(self.current_result_job_id)

    def _render_result(self, job_id: str) -> None:
        if (
            self.current_result is None
            or not job_id
            or job_id != self.current_result_job_id
            or job_id != self.current_job_id
        ):
            return
        generated = is_generated_flicker(self.current_result)
        self.noise_kind_box.configure(values=("both", "flicker", "thermal"))
        try:
            view = build_result_view(
                self.current_result,
                job_id=job_id,
                point=str(self.point_var.get() or "") or None,
                noise_scope="pair",
                noise_kind=self.noise_kind_var.get(),
            )
        except Exception as exc:
            self._show_error(exc, "Cannot present results")
            return
        self.current_result_view = view
        kind_label = {"flicker": "two-device flicker", "thermal": "thermal", "both": "flicker + thermal"}.get(self.noise_kind_var.get(), "flicker + thermal")
        if generated:
            kind_label = "symmetric pair estimate"
        self.summary_tree.heading("calculated", text=f"ISF {kind_label} (dBc/Hz)")
        self.summary_tree.heading("pair", text="Single-device matched-scope error (dB)" if generated else "gm pair flicker matched-scope error (dB)")
        self._bind_metric_columns(self.summary_tree, ("point", "offset_Hz", "f0_Hz", "predicted_pair_dBc_Hz" if self.noise_kind_var.get() == "flicker" else "thermal_noise", "spectre_total_dBc_Hz", "calculated_vs_total_db", "dynamic_error_dB" if generated else "pair_error_dB"))
        self._clear_tree(self.summary_tree)
        for index, row in enumerate(view.get("summary_rows", [])):
            self.summary_tree.insert(
                "",
                "end",
                iid=f"summary-{index}",
                values=tuple(
                    _display(row.get(key))
                    for key in ("point", "offset_hz", "f0_hz", "calculated_dbc", "spectre_dbc", "calculated_vs_total_db", "dynamic_error_db" if generated else "pair_error_db")
                ),
            )
        self._clear_tree(self.harmonic_tree)
        for index, row in enumerate(view.get("harmonic_rows", [])):
            harmonic = _display(row.get("k"))
            iid = f"harmonic-{index}"
            self.harmonic_tree.insert(
                "",
                "end",
                iid=iid,
                values=tuple(
                    _display(row.get(key))
                    for key in (
                        "k",
                        "mdc_mag",
                        "mdyn_mag",
                        "amplitude",
                        "delta_deg",
                        "q_real",
                        "q_imag",
                        "q_mag",
                        "cumulative_real",
                        "cumulative_imag",
                        "harmonic_dbc",
                        "cumulative_dbc",
                    )
                ),
                tags=(f"k:{harmonic}",),
            )
        self.isf_chart.set_series(view.get("isf_series", []))
        self.qk_chart.set_points(view.get("qk_points", []))
        self._clear_tree(self.noise_contribution_tree)
        for index, row in enumerate(view.get("noise_contribution_rows", [])):
            self.noise_contribution_tree.insert("", "end", iid=f"contribution-{index}",
                values=tuple(_display(row.get(key)) for key in ("label", "device", "mechanism", "simulated_dbc", "calculated_dbc", "fraction_pct", "psd_V2_Hz")))
        bars = contribution_bars(view.get("noise_contribution_rows", []))
        self.simulated_noise_pie.set_slices(bars["simulated"], bars["order"], bars["axis_max"])
        self.calculated_noise_pie.set_slices(bars["calculated"], bars["order"], bars["axis_max"])
        self.noise_pie_note_var.set(bars["note"])
        self.noise_contribution_note_var.set(" ".join(str(view.get(key) or "") for key in ("reference_note", "contribution_note")).strip() or
            "This table uses the selected operating point, offset VCO Simulation PM-SSB Data. device/mechanism details sorted by PSD sorted in descending order. ")
        self._render_metric_values()
        metrics = _mapping(view.get("metrics"))
        trust_warning = _mapping(self.current_result.get("trust_warning"))
        trust_label = str(view.get("run_kind") or "replay").upper()
        if not view.get("signoff"):
            trust_label += " / NON-SIGNOFF"
        if trust_warning.get("code"):
            trust_label += f" {trust_warning['code']}"
        self.metrics_var.set(
            "{kind} · f0={f0} Hz · offset={offset} Hz · ISF {noise}={calculated} / VCO simulated total={reference} dBc/Hz".format(
                kind=trust_label,
                noise=kind_label,
                f0=_display(metrics.get("frequency_hz")),
                offset=_display(metrics.get("offset_hz")),
                calculated=_display(metrics.get("calculated_dbc")),
                reference=_display(metrics.get("spectre_dbc")),
            ) + ("\n" + str(view["reference_note"]) if view.get("reference_note") else "")
        )
        sample = _mapping(view.get("selected_summary"))
        quality = str(view.get("quality_text") or "")
        operating = next((item for item in self._operating_points if item.get("id") == self.operating_point_var.get()), {})
        if operating.get("parameters"):
            quality = "Operating parameters: " + _display(operating["parameters"]) + "\n" + quality
        quality += " two-device flicker Model: " + _display(sample.get("pair_model"))
        if generated:
            single = _display(sample.get("dynamic_dBc_Hz"))
            reference = _display(sample.get("spectre_fn_dBc_Hz"))
            self.metrics_var.set(self.metrics_var.get() + f"\nsingle device fn · ISF {single} / HBnoise {reference} dBc/Hz; two-device values assume independent symmetric devices and add 3.01 dB. ")
        if self.noise_kind_var.get() != "flicker" and _mapping(sample.get("thermal_noise")).get("status") != "available":
            quality += " Current results do not contain available independent thermal Calculated, thermal and combined totals both show N/A; select flicker view existing calculations. "
        elif self.noise_kind_var.get() != "flicker":
            limit = _mapping(sample.get("thermal_noise")).get("input_max_harmonic")
            quality += (f" Thermal independent input bands: |m| ≤ {limit} (finite-band budget, higher bands are excluded). "
                        if type(limit) is int and limit >= 0 else " Thermal legacy budget: actual band limits were not recorded, not inferred from the current harmonic setting. ")
        if self.current_result.get("offset_sweep") and not self.current_result.get("offset_sweep", {}).get("complete", True):
            quality += " Only the completed portion is shown. "
        self.result_quality_var.set(quality)
        self._render_noise_curves()
        self.results_view.refresh()

    def _select_harmonic(self, harmonic: Any) -> None:
        for iid in self.harmonic_tree.get_children():
            values = self.harmonic_tree.item(iid, "values")
            if values and str(values[0]) == str(harmonic):
                self.harmonic_tree.selection_set(iid)
                self.harmonic_tree.see(iid)
                break

    def _harmonic_selected(self, _event: tk.Event[Any] | None = None) -> None:
        selected = self.harmonic_tree.selection()
        if not selected:
            return
        values = self.harmonic_tree.item(selected[0], "values")
        if values:
            self.qk_chart.highlight(values[0])
            if hasattr(self, "metric_values_tree"):
                self._render_metric_values(values[0])

    def _export_result_json(self) -> None:
        if self.current_result is None or self.current_result_job_id != self.current_job_id:
            messagebox.showwarning("No results", "Complete a job or import canonical replay. ", parent=self.root)
            return
        result = deep_merge({}, self.current_result)
        owner = self.current_result_job_id
        path = filedialog.asksaveasfilename(parent=self.root, title="Export results JSON", defaultextension=".json", filetypes=(("JSON", "*.json"),))
        if path:
            self._submit(
                "Export results JSON",
                lambda: _write_json(path, result),
                lambda _value: messagebox.showinfo("Export complete", f"{path}\nJob: {owner}", parent=self.root),
            )

    def _export_summary_csv(self) -> None:
        if self.current_result is None or self.current_result_job_id != self.current_job_id:
            messagebox.showwarning("No results", "Complete a job or import canonical replay. ", parent=self.root)
            return
        rows = [deep_merge({}, item) for item in _sequence(self.current_result.get("summary")) if isinstance(item, Mapping)]
        path = filedialog.asksaveasfilename(parent=self.root, title="Export Summary CSV", defaultextension=".csv", filetypes=(("CSV", "*.csv"),))
        if path:
            self._submit("Export Summary CSV", lambda: _write_csv(path, rows), lambda _value: messagebox.showinfo("Export complete", path, parent=self.root))

    def _export_harmonics_csv(self) -> None:
        if self.current_result is None or self.current_result_job_id != self.current_job_id:
            messagebox.showwarning("No results", "Complete a job or import canonical replay. ", parent=self.root)
            return
        point = str(self.point_var.get() or "")
        point_data = _mapping(_mapping(self.current_result.get("points")).get(point))
        rows = [deep_merge({}, item) for item in _sequence(point_data.get("harmonics")) if isinstance(item, Mapping)]
        path = filedialog.asksaveasfilename(parent=self.root, title="Export Harmonics CSV", defaultextension=".csv", filetypes=(("CSV", "*.csv"),))
        if path:
            self._submit("Export Harmonics CSV", lambda: _write_csv(path, rows), lambda _value: messagebox.showinfo("Export complete", path, parent=self.root))

    # ---- error catalog and close guard ------------------------------

    def _build_error_tab(self) -> None:
        content = self._scrollable_page(self.error_tab)
        pane = ttk.Panedwindow(content, orient="horizontal")
        pane.pack(fill="both", expand=True)
        list_frame = ttk.Frame(pane)
        detail_frame = ttk.LabelFrame(pane, text="Troubleshooting details", padding=7)
        pane.add(list_frame, weight=2)
        pane.add(detail_frame, weight=3)
        self.error_tree = self._tree(
            list_frame,
            (("code", "Code", 95), ("severity", "Severity", 65), ("title", "Title", 240)),
        )
        self._with_scroll(list_frame, self.error_tree).pack(fill="both", expand=True)
        self.error_tree.bind("<<TreeviewSelect>>", self._error_selected)
        self.error_detail = tk.Text(detail_frame, wrap="word", state="disabled")
        self.error_detail.pack(fill="both", expand=True)

    def _render_error_catalog(self, value: Any) -> None:
        catalog = _mapping(value)
        self._error_entries = {}
        self._clear_tree(self.error_tree)
        for entry in _sequence(catalog.get("entries")):
            if not isinstance(entry, Mapping):
                continue
            item = dict(entry)
            code = str(item.get("code") or "")
            if not code:
                continue
            self._error_entries[code] = item
            self.error_tree.insert("", "end", iid=code, values=(code, item.get("severity") or "", item.get("title") or ""))

    def _error_selected(self, _event: tk.Event[Any] | None = None) -> None:
        selection = self.error_tree.selection()
        if not selection:
            return
        entry = self._error_entries.get(str(selection[0]), {})
        lines = [
            f"{entry.get('code', '')}  {entry.get('title', '')}",
            "",
            str(entry.get("summary") or ""),
        ]
        for label, key in (("Possible causes", "causes"), ("Suggested actions", "actions")):
            values = _sequence(entry.get(key))
            if values:
                lines.extend(("", label))
                lines.extend(f"• {item}" for item in values)
        self.error_detail.configure(state="normal")
        self.error_detail.delete("1.0", "end")
        self.error_detail.insert("1.0", "\n".join(lines))
        self.error_detail.configure(state="disabled")

    def _request_close(self) -> None:
        if self._job_command_pending:
            messagebox.showwarning(
                "Submitting job command",
                "The window will remain open. Run/resume command submission is incomplete, Wait until job status is visible before closing. ",
                parent=self.root,
            )
            return
        if self._close_check_pending:
            return
        self._close_check_pending = True

        def checked(value: Any) -> None:
            self._close_check_pending = False
            if self._job_command_pending:
                messagebox.showwarning(
                    "Submitting job command",
                    "The window will remain open. a new run request appeared during the close check/resume command, Wait until job status is visible. ",
                    parent=self.root,
                )
                return
            running = active_jobs(_mapping(value))
            if running:
                description = "\n".join(f"• {item.get('id')} ({item.get('status')})" for item in running[:8])
                messagebox.showwarning(
                    "Background jobs remain",
                    "The window will remain open. No safe stop API is available; these jobs have not been reported as stopped. \n\n"
                    + description
                    + "\n\nWait until the job enters succeeded / failed / interrupted before closing. ",
                    parent=self.root,
                )
                return
            self.queue.close()
            self.root.destroy()

        def failed(exc: Exception) -> None:
            self._close_check_pending = False
            messagebox.showerror(
                "Cannot confirm background job status",
                _error_message(exc) + "\n\nto avoid an incorrect status, The window remains open. ",
                parent=self.root,
            )

        self.queue.submit(self.controller.jobs, checked, failed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VCO ISF Explorer native Tk UI")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="workspace for config and job state",
    )
    parser.add_argument("--maestro-context-file", type=Path,
                        help="private context captured by the invoking Maestro window")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(
            "Cannot start Tk Window (No usable graphical environment detected for DISPLAY): %s\n"
            "use the compatibility Web UI: vco-isf-web"
            " (from the source tree use python run.py --web)" % exc,
            file=sys.stderr,
        )
        return 2
    try:
        context = None
        preferences_directory = None
        if args.maestro_context_file:
            from ..core.maestro_context import load_maestro_context
            context = load_maestro_context(args.maestro_context_file, consume=True)
        if args.workspace is not None:
            workspace = args.workspace.expanduser().resolve()
        elif context is not None:
            sessions = Path(os.environ.get("ISF_WORKSPACE", DEFAULT_WORKSPACE)).expanduser().resolve() / "maestro_sessions"
            sessions.mkdir(parents=True, exist_ok=True)
            workspace = Path(tempfile.mkdtemp(prefix="session-", dir=str(sessions)))
            preferences_directory = sessions.parent / "maestro_preferences"
        else:
            workspace = Path(os.environ.get("ISF_WORKSPACE", DEFAULT_WORKSPACE)).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        controller = IsfController(workspace) if context is None else IsfController(workspace, maestro_context=context)
        if context is None:
            IsfApplication(root, controller)
        else:
            IsfApplication(root, controller, maestro_context=context, preferences_directory=preferences_directory)
    except Exception as exc:
        try:
            root.destroy()
        except Exception:
            pass
        print("Tk UI Initialization failed: %s" % exc, file=sys.stderr)
        return 2
    root.mainloop()
    return 0


__all__ = ["DEFAULT_WORKSPACE", "IsfApplication", "active_jobs", "build_parser", "main"]
