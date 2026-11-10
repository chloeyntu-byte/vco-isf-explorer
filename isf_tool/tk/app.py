"""Native Tk front end for the portable VCO ISF workflow.

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
    cumulative_noise_series, harmonic_noise_series, is_generated_flicker, operating_points, phase_noise_comparison_series, phase_noise_series,
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
AUTOMATIC_TARGET = "自动识别"

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
    """Use an installed CJK sans face instead of relying on mismatched fallback metrics."""
    available = {name.casefold(): name for name in families}
    for family in ("Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", "PingFang SC", "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "Liberation Sans", "DejaVu Sans"):
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
    for key, label in (("vco", "VCO"), ("flicker", "噪声 TB")):
        role = _mapping(_mapping(report.get("roles")).get(key))
        if not role:
            continue
        design = _mapping(_mapping(role.get("selected_test")).get("design"))
        current = tuple(str(design.get(name) or (design.get("library") if name == "lib" else "") or "未知") for name in ("lib", "cell", "view"))
        history = _mapping(role.get("selected_history"))
        manifest = _mapping(role.get("history_manifest")) or _mapping(history.get("history_manifest")) or _mapping(history.get("manifest"))
        entries = manifest.get("source_netlists", history.get("source_netlists", []))
        sources = [item for item in _sequence(entries) if isinstance(item, Mapping)] or [{}]
        for source in sources:
            design = _mapping(source.get("source_design"))
            old = tuple(str(design.get(name) or (design.get("library") if name == "lib" else "") or "未知") for name in ("lib", "cell", "view"))
            modified = source.get("source_mtime")
            if isinstance(modified, (int, float)) and not isinstance(modified, bool):
                try:
                    modified = datetime.fromtimestamp(modified).astimezone().isoformat(timespec="seconds")
                except (ValueError, OverflowError, OSError):
                    modified = "未知"
            known = "未知" not in old and "未知" not in current
            status = "视图名一致；内容以 History 为准" if known and current == old else "与当前设计不一致；仍按 History 运行" if known else "来源未完整记录，不推测"
            rows.append({"role": label, "current": "/".join(current), "history": "/".join(old),
                         "point": f"{source.get('point_id', source.get('point', '未知'))} / {source.get('corner', '未知')}",
                         "mtime": str(modified or "未知"), "sha256": str(source.get("sha256") or "未知"), "status": status})
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
        self.preference_status_var = tk.StringVar(value="项目偏好仅保存频偏、采样、主题与已验证的 VCO 映射。")
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
        self.status_var = tk.StringVar(value="正在读取本地配置…")
        self.execution_summary_var = tk.StringVar(value="实际运行 · 非正式签核")
        self.preflight_note_var = tk.StringVar(
            value="点击预检会检查绑定并短时探测所选 Cadence 环境与分析 Python，不启动仿真；"
            "点击开始会预检后实际调用 Spectre。绑定和保存配置不会启动仿真，也不会保存执行授权。"
        )
        self.binding_status_var = tk.StringVar(value="选择一个已完成的 VCO History。")
        self.binding_provenance_var = tk.StringVar(value="实际仿真复制所选 History 的固定网表，不会自动改用当前 schematic / config。绑定后可查看来源视图、时间与哈希。")
        self.mapping_status_var = tk.StringVar(value="绑定后自动识别信号与执行设置；详情和手动修改位于高级设置。")
        self.mapping_detail_var = tk.StringVar(value="每行可以直接选择候选或输入实际名称；输入会保留，点击“应用全部并验证”完成校验。")
        self.waveform_exchange_var = tk.StringVar()
        self.portable_edit_var = tk.StringVar()
        self.portable_detail_var = tk.StringVar(value="绑定后自动生成执行设置；选择一行可手动覆盖。")
        self.catalog_filter_var = tk.StringVar()
        self.catalog_kind_var = tk.StringVar(value="全部类型")
        self.catalog_status_var = tk.StringVar(value="绑定后可查看两个 TB 的网名、Maestro Outputs 和 Variables；不只包含 VGS / VDS / VBS。")
        self.catalog_detail_var = tk.StringVar(value="清单保留发现的实际名字；选择一行查看完整表达式和来源。")
        self.preflight_status_var = tk.StringVar(value="未运行预检")
        self.preflight_detail_var = tk.StringVar()
        self.job_status_var = tk.StringVar(value="未选择任务")
        self.point_var = tk.StringVar()
        self.operating_point_var = tk.StringVar()
        self.result_offset_var = tk.StringVar()
        # Results always use the pair model.  Keep the variable only as an
        # internal compatibility adapter, not as a user-selectable scope.
        self.noise_scope_var = tk.StringVar(value="pair")
        self.noise_kind_var = tk.StringVar(value="both")
        self.log_offset_var = tk.BooleanVar(value=True)
        self.harmonic_curve_var = tk.StringVar(value="全部各阶 + 相干总和")
        self.metric_choice_var = tk.StringVar()
        self.offset_preview_var = tk.StringVar(value="正在展开频偏点表…")
        self.show_all_mappings_var = tk.BooleanVar(value=False)
        self._operating_points: list[dict[str, Any]] = []
        self._offset_point_choices: dict[str, str] = {}
        self.metrics_var = tk.StringVar(value="尚无结果")
        self.result_quality_var = tk.StringVar(value="指标说明：点击表格单元格/标题，或使用下方指标下拉与 Enter。")
        self.noise_contribution_note_var = tk.StringVar(value="选择结果工况和频偏后显示本次仿真贡献。")
        self.noise_pie_note_var = tk.StringVar(value="饼图跟随页面顶部的工况、频偏和噪声类型。")
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
            "读取配置",
            self._read_initial_inputs,
            self._initial_inputs_loaded,
        )
        self._submit("读取错误目录", self.controller.error_catalog, self._render_error_catalog)
        self._refresh_jobs(silent=True)

    # ---- general UI -------------------------------------------------

    def _configure_fonts(self) -> None:
        """Keep the kit's sizes but prefer an installed CJK-capable family."""
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
        self.root.minsize(760, 540)
        fw.apply(self.root, product="isf")
        self._configure_fonts()
        self.style = ttk.Style(self.root)
        self._set_window_title()

        def header_actions(box: tk.Frame) -> None:
            self.theme_button = ttk.Button(box, text="切换明亮", style="Ghost.TButton", command=self._toggle_theme)
            self.theme_button.pack(side="left")
            self.save_state_button = ttk.Button(box, text="保存状态…", command=self._save_state_dialog)
            self.save_state_button.pack(side="left", padx=(8, 0))
            self.load_state_button = ttk.Button(box, text="加载状态…", command=self._load_state_dialog)
            self.load_state_button.pack(side="left", padx=(8, 0))

        self.header_bar = fw.header(
            self.root, "VCO ISF Explorer", "振荡器相噪分析 · ISF 与谐波贡献",
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
        self.result_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=12)
        self.error_tab = ttk.Frame(self.notebook, style="App.TFrame", padding=12)
        self.notebook.add(self.setup_tab, text="设置")
        self.notebook.add(self.run_tab, text="任务与预检")
        self.notebook.add(self.result_tab, text="分析结果")
        self.notebook.add(self.error_tab, text="排障")
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
        elif text.endswith("失败") or "失败：" in text:
            state = "error"
        elif text.endswith("完成"):
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
            rowheight=max(28, self.body_font.metrics("linespace") + 10),
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
        for chart in (getattr(self, "isf_chart", None), getattr(self, "qk_chart", None), getattr(self, "phase_noise_chart", None), getattr(self, "phase_error_chart", None), getattr(self, "harmonic_noise_chart", None), getattr(self, "simulated_noise_pie", None), getattr(self, "calculated_noise_pie", None)):
            if chart is not None:
                chart.set_palette(chart_palette)
        self.theme_button.configure(
            text="切换明亮" if self.theme_var.get() == "dark" else "切换暗色"
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
                    self.status_var.set(f"{label}完成")
            except Exception as exc:
                self._show_error(exc, f"{label}结果处理失败")

        def failure(exc: Exception) -> None:
            if not quiet:
                self._foreground_operations -= 1
            if on_failure is not None:
                try:
                    on_failure(exc)
                except Exception as callback_error:
                    self._show_error(callback_error, f"{label}失败处理异常")
            if not quiet:
                self.status_var.set(f"{label}失败")
            self._show_error(exc, f"{label}失败")

        self.queue.submit(function, success, failure)

    def _show_error(self, exc: Exception, title: str = "操作失败") -> None:
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
        self.load_example_button = ttk.Button(example, text="载入示例", command=self._load_example)
        self.load_example_button.pack(side="right")
        example_text = ttk.Label(
            example, text="没有 Cadence 环境？载入合成 ideal-LC 示例，离线查看全部结果视图。",
            style="Help.TLabel", justify="left", wraplength=360,
        )
        example_text.pack(side="left", fill="x", expand=True)
        example_text.bind("<Configure>", lambda event: example_text.configure(wraplength=max(200, event.width - 8)))

        location = ttk.LabelFrame(
            content, text="可选：自动发现项目中的 Testbench", style="Card.TLabelframe", padding=(14, 12)
        )
        location.columnconfigure(1, weight=1)
        ttk.Label(location, text="项目根目录").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 6)
        )
        ttk.Entry(location, textvariable=self.vars["project_root"]).grid(
            row=0, column=1, sticky="ew", pady=(0, 6)
        )
        ttk.Button(
            location,
            text="选择目录",
            style="Secondary.TButton",
            command=lambda: self._choose_directory("project_root"),
        ).grid(row=0, column=2, padx=(8, 0), pady=(0, 6))
        ttk.Button(
            location,
            text="自动发现 Testbench",
            command=self._discover,
        ).grid(row=0, column=3, padx=(8, 0), pady=(0, 6))
        ttk.Label(
            location,
            text="仅扫描该目录并识别 Maestro，不会修改原工程。",
            style="Help.TLabel",
        ).grid(row=1, column=1, columnspan=3, sticky="w")
        ttk.Label(
            location,
            text=f"应用数据：{self.controller.workspace}",
            style="Help.TLabel",
        ).grid(row=2, column=1, columnspan=3, sticky="w", pady=(3, 0))

        pair = ttk.LabelFrame(content, text="01  VCO 来源", style="Card.TLabelframe", padding=(14, 9))
        pair.pack(fill="x", pady=(0, 8))
        pair.columnconfigure(1, weight=1)
        if self.maestro_context:
            ttk.Label(pair, textvariable=self.maestro_source_var, style="Setup.TLabel", wraplength=350).grid(
                row=0, column=0, columnspan=2, sticky="w", pady=(0, 6)
            )
            source_actions = ttk.Frame(pair)
            source_actions.grid(row=0, column=2, columnspan=2, sticky="e", pady=(0, 6))
            ttk.Button(source_actions, text="刷新 History", command=self._refresh_context_histories).pack(side="left")
            ttk.Button(source_actions, text="更换 VCO…", command=self._toggle_vco_source).pack(side="left", padx=(6, 0))
            self._update_maestro_banner()
        ttk.Label(pair, text="完整 History").grid(row=1, column=0, sticky="w", padx=(0, 10))
        self._maestro_box(pair, "vco", "history").grid(row=1, column=1, sticky="ew")
        ttk.Label(pair, text="Test").grid(row=1, column=2, sticky="w", padx=10)
        self._maestro_box(pair, "vco", "test", width=18).grid(row=1, column=3, sticky="ew")
        self.vco_source_controls = ttk.Frame(pair)
        self.vco_source_controls.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        self._build_maestro_path(self.vco_source_controls, "vco")
        if self.maestro_context:
            ttk.Button(self.vco_source_controls, text="恢复启动来源", command=self._restore_maestro_context).pack(
                side="right", padx=(6, 0)
            )
            self.vco_source_controls.grid_remove()
        reference = ttk.LabelFrame(content, text="02  器件噪声参考", style="Card.TLabelframe", padding=(14, 9))
        reference.pack(fill="x", pady=(0, 8))
        reference_mode = ttk.Frame(reference)
        reference_mode.pack(fill="x", pady=(0, 6))
        for label, mode in (("自动生成", "generated"), ("手动绑定 Noise TB", "maestro")):
            ttk.Radiobutton(reference_mode, text=label, value=mode, variable=self.vars["replica_source"],
                            command=self._replica_source_changed).pack(side="left", padx=(0, 14))

        self.generated_reference_frame = ttk.Frame(reference)
        self.generated_reference_frame.columnconfigure(1, weight=1)
        ttk.Label(self.generated_reference_frame, text="目标 MOS").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.target_mos_var = tk.StringVar(value=AUTOMATIC_TARGET)
        self.target_mos_box = ttk.Combobox(self.generated_reference_frame, textvariable=self.target_mos_var,
                                          width=23, state="disabled")
        self.target_mos_box.grid(row=0, column=1, sticky="w")
        self.target_mos_box.bind("<<ComboboxSelected>>", self._target_mos_chosen)
        ttk.Label(self.generated_reference_frame, text="单管建模 · 默认对称双管 +3 dB",
                  style="Help.TLabel").grid(row=1, column=1, sticky="w", pady=(3, 0))

        self.manual_reference_frame = ttk.Frame(reference)
        replica_path = ttk.Frame(self.manual_reference_frame)
        replica_path.pack(fill="x", pady=(0, 6))
        self._build_maestro_path(replica_path, "replica")
        replica_inputs = ttk.Frame(self.manual_reference_frame)
        replica_inputs.pack(fill="x")
        replica_inputs.columnconfigure(1, weight=1)
        ttk.Label(replica_inputs, text="完整 History").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self._maestro_box(replica_inputs, "replica", "history").grid(row=0, column=1, sticky="ew")
        ttk.Label(replica_inputs, text="Test").grid(row=0, column=2, sticky="w", padx=10)
        self._maestro_box(replica_inputs, "replica", "test", width=15).grid(row=0, column=3, sticky="ew")
        self._render_reference_controls()

        analysis = ttk.LabelFrame(
            content, text="03  分析目标", style="Card.TLabelframe", padding=(14, 9)
        )
        analysis.pack(fill="x", pady=(0, 10))
        analysis.columnconfigure(1, weight=1)
        analysis.columnconfigure(3, weight=1)
        modes = ttk.Frame(analysis)
        modes.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(modes, text="频偏方式").pack(side="left", padx=(0, 12))
        for text, mode in (("单频偏", "single"), ("多个单点", "list"), ("线性", "linear"), ("对数", "log")):
            ttk.Radiobutton(modes, text=text, variable=self.vars["offset_mode"], value=mode).pack(side="left", padx=(0, 14))
        self.offset_input_frames = {}
        for mode in ("single", "list", "linear", "log"):
            frame = ttk.Frame(analysis)
            frame.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(0, 8))
            self.offset_input_frames[mode] = frame
        offset = self.offset_input_frames["single"]
        ttk.Label(offset, text="目标频偏").pack(side="left", padx=(0, 12))
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
        ttk.Label(multiple, text="点列表").pack(side="left", padx=(0, 12))
        ttk.Entry(multiple, textvariable=self.vars["offset_values"]).pack(side="left", fill="x", expand=True)
        for mode in ("linear", "log"):
            frame = self.offset_input_frames[mode]
            for name, label in (("start", "起始"), ("stop", "终止")):
                ttk.Label(frame, text=label).pack(side="left", padx=(0, 6))
                ttk.Entry(frame, textvariable=self.vars["offset_" + name], width=12).pack(side="left", padx=(0, 16))
        linear = self.offset_input_frames["linear"]
        for label, value in (("步长", "step"), ("间隔数", "intervals")):
            ttk.Radiobutton(linear, text=label, variable=self.vars["offset_spacing"], value=value).pack(side="left")
        self.offset_step_entry = ttk.Entry(linear, textvariable=self.vars["offset_step"], width=12)
        self.offset_step_entry.pack(side="left", padx=6)
        self.offset_intervals_entry = ttk.Entry(linear, textvariable=self.vars["offset_intervals"], width=8)
        self.offset_intervals_entry.pack(side="left", padx=6)
        logarithmic = self.offset_input_frames["log"]
        ttk.Label(logarithmic, text="每 decade 点数").pack(side="left", padx=(0, 6))
        ttk.Entry(logarithmic, textvariable=self.vars["offset_points_per_decade"], width=8).pack(side="left")
        ttk.Label(
            analysis,
            text=f"最大谐波（≤{MAX_PHASE_CANDIDATE_HARMONICS}）",
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
        ttk.Button(analysis, text="查看实际频偏点表", command=self._show_offset_points).grid(row=2, column=2, columnspan=2, sticky="w", padx=16)
        self._help(analysis, textvariable=self.offset_preview_var).grid(row=3, column=0, columnspan=4, sticky="ew")
        self._refresh_offset_preview()

        self.basic_setup_frames = (pair, analysis)
        status = self._help(self.setup_tab, textvariable=self.binding_status_var)
        status.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        actions = ttk.Frame(self.setup_tab, style="App.TFrame")
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.setup_start_button = ttk.Button(actions, text="开始分析", style="Primary.TButton", command=self._start_from_setup)
        self.setup_start_button.pack(side="left")
        self.setup_inspect_button = ttk.Button(actions, text="检查绑定", command=self._inspect_pair)
        self.setup_inspect_button.pack(side="left", padx=8)
        self.environment_button = ttk.Button(actions, text="环境检测…", command=self._show_environment)
        self.environment_button.pack(side="left")
        self.advanced_button = ttk.Button(
            actions,
            text="高级设置",
            style="Secondary.TButton",
            command=self._toggle_advanced,
        )
        self.advanced_button.pack(side="right")
        self.advanced_frame = ttk.LabelFrame(
            content, text="高级设置", style="Card.TLabelframe", padding=(14, 12)
        )
        self.advanced_tabs = ttk.Notebook(self.advanced_frame)
        self.advanced_tabs.pack(fill="both", expand=True)
        mapping_tab = self.mapping_tab = ttk.Frame(self.advanced_tabs, padding=10)
        physical_tab = self.physical_tab = ttk.Frame(self.advanced_tabs, padding=10)
        inventory_tab = self.inventory_tab = ttk.Frame(self.advanced_tabs, padding=10)
        options_tab = ttk.Frame(self.advanced_tabs, padding=10)
        expert_tab = ttk.Frame(self.advanced_tabs, padding=10)
        for frame, label in ((mapping_tab, "本次使用的对应"), (physical_tab, "执行细节"), (options_tab, "发现与运行选项"), (expert_tab, "专家兼容入口")):
            self.advanced_tabs.add(frame, text=label)
        self._build_mapping_tabs(mapping_tab, physical_tab, inventory_tab)
        diagnostics = ttk.Frame(options_tab)
        diagnostics.pack(fill="x", pady=(0, 8))
        ttk.Button(diagnostics, text="查看信号清单", command=self._open_catalog).pack(side="left")
        ttk.Button(diagnostics, text="查看 History 网表来源",
                   command=lambda: self._open_advanced_tab(self.physical_tab)).pack(side="left", padx=8)
        self._help(options_tab, textvariable=self.mapping_status_var).pack(fill="x")
        self._help(options_tab, textvariable=self.binding_provenance_var).pack(fill="x")
        self._help(options_tab, text="频偏以 Hz 为默认单位，k=千、M=百万。每次最多 256 点。").pack(fill="x")
        if self._preferences_path:
            self._help(options_tab, textvariable=self.preference_status_var).pack(fill="x")
        location.pack(in_=options_tab, fill="x", pady=(0, 10))
        settings = ttk.Frame(options_tab)
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)

        advanced_fields = (
            (0, 0, "项目名", "project_name"),
            (1, 0, "相位采样点数", "phase_samples"),
            (1, 2, "分析周期数", "period_count"),
            (2, 0, "PDK 名称覆盖", "pdk_name"),
            (2, 2, "PDK 根目录覆盖", "pdk_root"),
            (3, 0, "运行目录（可选）", "run_root"),
            (3, 2, "结果 Bundle 目录（可选）", "bundle_path"),
            (4, 0, "分析 Python（留空自动识别）", "analysis_python"),
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
                "通常无需填写文件路径：执行设置根据已选 TB 自动生成。只有复用已有专用配置时才使用此入口；"
                "手工文件优先于自动生成，切换 TB 后请重新确认适用性。"
            ),
        ).pack(fill="x", pady=(4, 8))
        engine_row = ttk.Frame(expert_tab)
        engine_row.pack(fill="x", pady=(0, 10))
        ttk.Label(engine_row, text="专用执行配置文件（engine JSON，可留空）").pack(side="left")
        ttk.Entry(engine_row, textvariable=self.vars["engine_config"]).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(engine_row, text="选择文件", command=self._choose_engine).pack(side="left")
        ttk.Button(engine_row, text="恢复自动", command=lambda: self.vars["engine_config"].set("")).pack(side="left", padx=(6, 0))

        mode_row = ttk.Frame(options_tab)
        mode_row.pack(fill="x", pady=(12, 8))
        self.formal_signoff_check = ttk.Checkbutton(
            mode_row,
            text="正式 Signoff（至少 6 个真实设计点并执行完整质量门）",
            variable=self.formal_signoff_var,
            command=self._mode_changed,
        )
        self.formal_signoff_check.pack(side="left")

        fingerprint = ttk.LabelFrame(
            expert_tab,
            text="绑定完整性指纹（自动生成）",
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
            (("绑定指纹", "binding_fingerprint"), ("History 指纹", "history_fingerprint")),
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
            text="用于确认运行时仍是同一 Maestro、Test 和 History，无需手动填写。",
            style="Help.TLabel",
        ).grid(row=3, column=1, columnspan=2, sticky="w", pady=(4, 0))

        config_actions = ttk.Frame(expert_tab)
        config_actions.pack(fill="x", pady=(0, 6))
        ttk.Button(config_actions, text="加载配置 JSON", command=self._load_config_dialog).pack(side="left")
        ttk.Button(config_actions, text="保存配置 JSON", command=self._save_config_dialog).pack(side="left", padx=6)
        ttk.Button(config_actions, text="从表单刷新 JSON", command=self._render_merged_config).pack(side="left", padx=6)
        ttk.Button(config_actions, text="应用下方 JSON", command=self._apply_raw_editor).pack(side="left")
        ttk.Label(
            config_actions,
            text="未在表单展示的兼容字段会保留。",
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
        ttk.Label(parent, text="Maestro 目录").pack(side="left", padx=(0, 10))
        ttk.Button(parent, text="选择目录…", command=lambda: self._choose_directory(f"{role}_path")).pack(
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
            "自动生成参考，选择完整 VCO History 后识别目标 MOS。"
            if generated else "请选择独立 Noise TB 与其完整 History。"
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
                self.binding_status_var.set("请选择一个已完成的 VCO History，再开始分析。")
                self.vco_history_box.focus_set()
            else:
                self.binding_status_var.set("请先选择 VCO 的 Maestro 目录。")
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
            config_error = f"设置暂时无法读取，以下使用上次加载的配置。{exc}\n\n"
        context = deep_merge({}, self.maestro_context) if self._context_mode else None
        self.environment_button.state(("disabled",))

        def enable(_value: Any) -> None:
            self.environment_button.state(("!disabled",))

        def show(report: Any) -> None:
            enable(report)
            window = tk.Toplevel(self.root)
            window.title("ISF 环境检测")
            window.geometry("780x530")
            window.minsize(530, 340)
            window.transient(self.root)
            frame = ttk.Frame(window, padding=14)
            frame.pack(fill="both", expand=True)
            title = "已找到运行工具" if report.get("status") == "ready" else "有运行条件需要检查"
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
            ttk.Button(actions, text="复制报告", command=copy_report).pack(side="left")
            ttk.Button(actions, text="关闭", command=window.destroy).pack(side="right")
            window.bind("<Escape>", lambda _event: window.destroy())

        self._submit("检测运行环境", lambda: inspect_environment(config, self.controller.workspace, context),
                     show, on_failure=enable)

    def _toggle_advanced(self) -> None:
        visible = not bool(self.advanced_visible_var.get())
        self.advanced_visible_var.set(visible)
        if visible:
            self.advanced_frame.pack(fill="both", expand=True, pady=(0, 8))
            self.advanced_button.configure(text="收起高级设置")
            self.root.after_idle(self._scroll_to_advanced)
        else:
            self.advanced_frame.pack_forget()
            self.advanced_button.configure(text="高级设置")
            self.setup_canvas.yview_moveto(0)

    def _scroll_to_advanced(self) -> None:
        if self.advanced_visible_var.get():
            bounds = self.setup_canvas.bbox("all")
            if bounds and bounds[3] > 0:
                self.setup_canvas.yview_moveto(self.advanced_frame.winfo_y() / bounds[3])

    def _open_catalog(self) -> None:
        if str(self.inventory_tab) not in self.advanced_tabs.tabs():
            self.advanced_tabs.add(self.inventory_tab, text="诊断：全部名称")
        self._open_advanced_tab(self.inventory_tab)

    def _offset_inputs_changed(self, *_args: Any) -> None:
        if self._updating_form:
            return
        self._selection_revision += 1
        self.last_preflight = None
        self.preflight_status_var.set("频偏已改变，需重新预检")
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
            workload = (f"{preview['operating_point_count']} 工况 × {len(values)} 频偏 = {preview['total_samples']} 个工况/频偏结果" if preview["total_samples"] else f"{len(values)} 个频偏；绑定 TB 后显示工况数和总任务量")
            endpoint = "；步长不能整除，已追加终点" if preview["endpoint_appended"] else ""
            self.offset_preview_var.set(f"{workload}{endpoint}\n实际频偏（Hz）：{shown}。曲线只显示实际完成的点，不补算或插值结果。")
        except Exception as exc:
            self.offset_preview_var.set("频偏输入待修正：" + str(exc))

    def _show_offset_points(self) -> None:
        try:
            preview = self._offset_preview()
        except Exception as exc:
            self._show_error(exc, "频偏输入无效")
            return
        window = tk.Toplevel(self.root)
        window.title("实际频偏点表")
        window.geometry("470x470")
        self._help(window, text=self.offset_preview_var.get()).pack(fill="x", padx=12, pady=10)
        tree = self._tree(window, (("index", "序号", 80), ("hz", "实际频偏（Hz）", 260)))
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
        self._help(mapping_tab, text="计算范围：gm 的 flicker / thermal 与 LC 的 thermal。Thermal 静态源谱：脚本在所选 VCO History 的私有网表副本追加 noise 分析，只读取已启用的纯被动 LC 网络噪声源，不读取电容阵列内部噪声；gm MOS 周期工况源谱由噪声 TB 获取。不会写回原 TB，也不要求另建名为 noise 的 Maestro Output。静态源谱经过周期传递与频带折叠才是计算相噪；VCO HBNoise 仍为全电路仿真参考，未建模的源显示 N/A。").pack(fill="x", pady=(0, 8))
        self._help(
            mapping_tab,
            text="左侧列出计算用途，右侧直接选择或输入两个 TB 的实际名称。下拉候选来自该 TB 的同类全部名称；任意别名也可手工输入。输入即时保留，批量应用后验证。每行“自动”可恢复自动识别。",
        ).pack(fill="x", pady=(0, 8))
        mapping_actions = ttk.Frame(mapping_tab)
        mapping_actions.pack(fill="x", pady=(0, 8))
        ttk.Button(mapping_actions, text="应用全部并验证", command=self._inspect_pair).pack(side="left")
        ttk.Button(mapping_actions, text="清除全部信号覆盖并重新识别", command=self._reset_signal_overrides).pack(side="left", padx=8)
        ttk.Button(mapping_actions, text="查看所有实际名称", command=self._open_catalog).pack(side="left")
        ttk.Checkbutton(mapping_tab, text="显示未使用映射、系列成员和执行细节（诊断）", variable=self.show_all_mappings_var, command=lambda: self._render_mapping_report(self._signal_report)).pack(anchor="w", pady=(0, 8))
        ttk.Label(mapping_tab, text="跨 TB 偏置对应（仅这一部分是 VGS / VDS / VBS）", font=self.heading_font).pack(anchor="w", pady=(4, 8))
        self.cross_tb_tree = self._tree(mapping_tab, (("meaning", "物理含义", 100), ("vco", "VCO 周期波形 / 节点差", 240), ("replica", "Flicker 偏置变量", 170), ("source", "波形来源", 140), ("status", "对应状态", 135)))
        self.cross_tb_tree.configure(height=3)
        self.cross_tb_tree.pack(fill="x", pady=(0, 10))
        self.cross_tb_tree.bind("<<TreeviewSelect>>", self._cross_tb_selected)
        ttk.Label(mapping_tab, text="本次实际使用的对应；过期、错误与歧义项始终可见", font=self.heading_font).pack(anchor="w", pady=(4, 8))
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
        ttk.Label(mapping_tab, text="VCO → 噪声 TB 波形传递检查", font=self.heading_font).pack(anchor="w", pady=(12, 6))
        self._help(mapping_tab, textvariable=self.waveform_exchange_var).pack(fill="x")
        self.waveform_exchange_tree = self._tree(mapping_tab, (("semantic", "物理量", 65), ("source", "VCO 来源", 245), ("destination", "噪声 TB 接收端", 220), ("path", "路径策略", 160), ("status", "检查结果", 100)))
        self.waveform_exchange_tree.configure(height=3)
        self._with_scroll(mapping_tab, self.waveform_exchange_tree).pack(fill="x", pady=(4, 12))

        self._help(physical_tab, text="这些值由绑定的网表、输出和变量推导。歧义或缺项会在预检中提示；手动修改后重新验证。旧专用配置文件可在“专家兼容入口”选择。").pack(fill="x", pady=(0, 8))
        ttk.Label(physical_tab, text="所选 History 网表来源（只读）", font=self.heading_font).pack(anchor="w")
        self.history_provenance_tree = self._tree(physical_tab, (("role", "TB", 80), ("current", "Maestro 当前设计", 245),
            ("history", "History 网表设计", 245), ("point", "point / corner", 150), ("mtime", "源网表修改时间", 215),
            ("sha256", "源网表 SHA-256", 520), ("status", "说明", 260)))
        self.history_provenance_tree.configure(height=4)
        provenance_frame = self._with_scroll(physical_tab, self.history_provenance_tree)
        provenance_frame.configure(width=900, height=220)
        provenance_frame.pack_propagate(False)
        provenance_frame.pack(fill="x", pady=(6, 12))
        self.portable_tree = self._tree(physical_tab, (("label", "用途", 215), ("value", "当前值", 310), ("source", "来源", 155), ("status", "状态", 100)))
        self.portable_tree.configure(height=12)
        self._with_scroll(physical_tab, self.portable_tree).pack(fill="both", expand=True)
        self.portable_tree.bind("<<TreeviewSelect>>", self._portable_selected)
        edit = ttk.Frame(physical_tab)
        edit.pack(fill="x", pady=8)
        ttk.Label(edit, text="手动覆盖（留空＝自动）").pack(side="left")
        self.portable_edit_box = ttk.Combobox(edit, textvariable=self.portable_edit_var)
        self.portable_edit_box.pack(side="left", fill="x", expand=True, padx=8)
        self.portable_apply_button = ttk.Button(edit, text="应用并验证", command=self._apply_portable_override)
        self.portable_apply_button.pack(side="left")
        self.portable_reset_button = ttk.Button(edit, text="恢复自动", command=lambda: self._apply_portable_override(reset=True))
        self.portable_reset_button.pack(side="left", padx=(6, 0))
        self._help(physical_tab, textvariable=self.portable_detail_var).pack(fill="x", pady=(0, 6))

        self._help(inventory_tab, text="这里列出所选 TB 中发现的网名（Net）、Maestro 输出（Output）、设计变量（Variable），以及器件、分析和保存信号。网名来自生成网表及 Maestro 引用，不直接读取 schematic 二进制；请先在 Maestro 生成网表并选择对应 History。清单不会改名，也不表示每个表达式都会执行。").pack(fill="x", pady=(0, 8))
        ttk.Button(inventory_tab, text="收起完整名称诊断", command=lambda: self.advanced_tabs.forget(inventory_tab)).pack(anchor="w", pady=(0, 8))
        ttk.Label(inventory_tab, textvariable=self.catalog_status_var, font=self.heading_font).pack(anchor="w", pady=(0, 8))
        filters = ttk.Frame(inventory_tab)
        filters.pack(fill="x", pady=(0, 8))
        self.catalog_kind_box = ttk.Combobox(filters, textvariable=self.catalog_kind_var, state="readonly", width=20,
                                           values=("全部类型", *CATALOG_KIND_LABELS.values()))
        self.catalog_kind_box.pack(side="left")
        self.catalog_kind_var.trace_add("write", lambda *_args: self._render_signal_catalog())
        ttk.Label(filters, text="搜索名称 / 表达式 / TB").pack(side="left", padx=(8, 0))
        ttk.Entry(filters, textvariable=self.catalog_filter_var).pack(side="left", fill="x", expand=True, padx=8)
        self.catalog_filter_var.trace_add("write", lambda *_args: self._render_signal_catalog())
        ttk.Button(filters, text="修改工具对应关系", command=lambda: self._open_advanced_tab(self.mapping_tab)).pack(side="left")
        self.catalog_tree = self._tree(inventory_tab, (("role", "TB", 75), ("kind", "类型", 145), ("name", "实际名称", 220), ("expression", "表达式 / 节点 / 数值", 365)))
        self.catalog_tree.configure(height=15)
        self._with_scroll(inventory_tab, self.catalog_tree).pack(fill="both", expand=True)
        self.catalog_tree.bind("<<TreeviewSelect>>", self._catalog_selected)
        self._help(inventory_tab, textvariable=self.catalog_detail_var).pack(fill="x", pady=8)

    @staticmethod
    def _field_status(row: Mapping[str, Any]) -> str:
        status = str(row.get("status") or "")
        return {"auto": "自动", "detected": "自动", "derived": "脚本推导", "resolved": "已识别", "manual": "手动", "override": "手动", "overridden": "手动", "ambiguous": "多个候选", "missing": "未识别", "invalid": "需修正", "pending": "待验证", "optional": "可选", "error": "需修正"}.get(status, status)

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
        self._mapping_status_labels[iid].configure(text="手动 · 待验证" if value else "自动 · 待验证")
        self.mapping_status_var.set("输入已保留，尚未验证；点击“应用全部并验证”。")
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
        self._mapping_status_labels[iid].configure(text="自动 · 待验证")
        self.mapping_status_var.set("已恢复自动；点击“应用全部并验证”确认当前对应。")
        self.mapping_detail_var.set(self._field_detail(row, self._mapping_input_vars[iid].get()))

    def _cross_tb_selected(self, _event: Any = None) -> None:
        selected = self.cross_tb_tree.selection()
        row = self._cross_tb_rows.get(selected[0], {}) if selected else {}
        if row:
            self.mapping_detail_var.set(f"{row['label']}：{row.get('help') or ''}\n{row.get('vco') or '未识别'} → {row.get('replica') or '未识别'}；来源：{row.get('source')}；状态：{row.get('status')}\n表达式：{row.get('expression') or '无'}")

    @staticmethod
    def _field_detail(row: Mapping[str, Any], selected: Any = None) -> str:
        candidates = "、".join(mapping_candidate_names(row, selected)) or "无"
        issues = "；".join(str(item) for item in _sequence(row.get("issues")) if item)
        detail = f"{row.get('label') or row.get('key')}：{row.get('help') or '用于当前仿真对应关系。'}\n自动识别：{_display(row.get('detected'))}；来源：{mapping_source_text(row)}；候选：{candidates}"
        if row.get("used_by") or row.get("consumption_mode"):
            consumers = {"private Spectre preparation": "准备私有仿真副本", "flicker-noise extraction": "闪烁噪声提取", "signed HBXF transfer H[k]": "带方向和相位的 HBXF 传递", "ISF calculation": "ISF 计算", "VCO periodic bias → current TB noise sampling": "VCO 周期偏置对应电流 TB 的噪声采样", "target flicker extraction": "目标器件闪烁噪声提取"}
            used_by = "、".join(consumers.get(str(value), str(value)) for value in row["used_by"]) if isinstance(row.get("used_by"), list) else str(row.get("used_by") or "不用于计算")
            modes = {"source_binding_remeasured": "按用户选择的输出定位电流源，按本次输出端重测完整复数响应", "private_netlist": "私有网表设置", "raw_signal": "提取真实信号", "raw_voltage_pair": "使用原始节点电压差", "netlist_bias_parameter": "作为噪声 TB 偏置变量", "raw_noise_contributor": "提取指定器件噪声贡献", "reference_only": "仅对照，不作为计算输入", "private_drain_source": "在私有副本使用漏源注入", "invalid": "无效，需要修正"}
            mode = str(row.get("consumption_mode") or "见用途说明")
            detail += f"\n实际用途：{used_by or '不用于计算'}；使用方式：{modes.get(mode, mode)}"
        evidence = mapping_selection_details(row, selected)
        return detail + ("\n" + "\n".join(evidence) if evidence else "") + ("\n校验提示：" + issues if issues else "")

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
            detail = "当前使用专家指定文件；此表为自动生成建议。先在专家入口恢复自动，才可应用这里的覆盖。\n" + detail
        self.portable_detail_var.set(detail)

    def _apply_portable_override(self, reset: bool = False) -> None:
        selected = self.portable_tree.selection()
        row = self._portable_rows.get(selected[0], {}) if selected else {}
        if not row:
            self.portable_detail_var.set("请先选择要修改的执行设置。")
            return
        if row.get("editable") is False:
            self.portable_detail_var.set("此项为执行器约束，只能查看；请根据用途提示在原始连接或信号对应处处理。")
            return
        if str(self.vars["engine_config"].get() or "").strip():
            self.portable_detail_var.set("当前执行配置来自专家指定文件。请先在专家入口恢复自动，再编辑自动生成设置。")
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
                    self.portable_detail_var.set("此项需要数值或结构化值，请参照自动识别值填写。")
                    return
            self._portable_overrides[key] = value
        else:
            self._portable_overrides.pop(key, None)
        self._mapping_changed()
        self._inspect_pair()

    def _mapping_changed(self) -> None:
        self._selection_revision += 1
        self.last_preflight = None
        self.preflight_status_var.set("对应关系已修改，需重新预检")
        self.mapping_status_var.set("手动修改已应用，正在重新验证；完整 history 绑定保持不变。")
        if self._auto_engine_path and str(self.vars["engine_config"].get()) == self._auto_engine_path:
            self.vars["engine_config"].set("")
        self._auto_engine_path = ""

    def _render_mapping_report(self, report: Mapping[str, Any]) -> None:
        provenance = history_provenance_rows(report)
        self._clear_tree(self.history_provenance_tree)
        for row in provenance:
            self.history_provenance_tree.insert("", "end", values=tuple(row[key] for key in ("role", "current", "history", "point", "mtime", "sha256", "status")))
        summaries = []
        for role in ("VCO", "噪声 TB"):
            selected = [row for row in provenance if row["role"] == role]
            if selected:
                designs = sorted({row["history"] for row in selected})
                history = designs[0] if len(designs) == 1 else "多种来源，见逐点列表"
                summaries.append(f"{role} 当前设计：{selected[0]['current']}；History 来源：{history}")
        summaries.append("实际仿真复制所选 History 的固定网表，不会自动跟随当前 schematic / config 或器件禁用状态；若需使用新拓扑，请先在 Maestro 重跑并选择新 History。来源不同只提示，不阻止历史回放。")
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
                    rows.append({"role": role, "key": key, "label": key, "value": value, "status": "pending", "source": "已保留的手动输入，等待当前 TB 验证"})
        self._rendering_mapping_inputs = True
        for child in self.mapping_rows_frame.winfo_children():
            child.destroy()
        self._mapping_input_vars = {}
        self._mapping_inputs = {}
        self._mapping_status_labels = {}
        for column, (text, weight) in enumerate((("TB", 0), ("计算用途", 2), ("TB 中实际名称（可选 / 可输入）", 4), ("来源 / 缺失 / 推导", 3), ("状态", 1))):
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
            ttk.Label(self.mapping_rows_frame, text="VCO" if row["role"] == "vco" else "噪声 TB").grid(row=index, column=0, sticky="nw", padx=6, pady=14)
            label = ttk.Label(self.mapping_rows_frame, text=row.get("label") or row["key"], wraplength=150, justify="left")
            label.grid(row=index, column=1, sticky="new", padx=6, pady=14)
            cell = ttk.Frame(self.mapping_rows_frame)
            cell.grid(row=index, column=2, sticky="new", padx=6, pady=10)
            box = ttk.Combobox(cell, textvariable=variable, values=mapping_candidate_names(row, current, bool(self.show_all_mappings_var.get())), width=22,
                               state="normal" if row.get("editable") is not False else "disabled")
            box.pack(fill="x")
            self._mapping_inputs[iid] = box
            box.bind("<FocusIn>", lambda _event, field=row, key=iid: self.mapping_detail_var.set(self._field_detail(field, self._mapping_input_vars[key].get())))
            ttk.Button(cell, text="自动", command=lambda key=iid: self._reset_mapping_input(key)).pack(anchor="w", pady=(5, 0))
            source = ttk.Label(self.mapping_rows_frame, text=mapping_source_text(row) or "待检测", wraplength=190, justify="left")
            source.grid(row=index, column=3, sticky="new", padx=6, pady=14)
            pending = str(row["key"]) in manual and str(manual[str(row["key"])]) != str(row.get("value") or "")
            generated = bool(row.get("effective_value") and not row.get("value"))
            status = ttk.Label(self.mapping_rows_frame, text="手动 · 待验证" if pending else "本次脚本生成" if generated else self._field_status(row), wraplength=100, justify="left")
            status.grid(row=index, column=4, sticky="nw", padx=6, pady=14)
            self._mapping_status_labels[iid] = status
            variable.trace_add("write", lambda *_args, key=iid: self._mapping_input_changed(key))
        self._rendering_mapping_inputs = False
        exchange = waveform_exchange_view(report)
        exchange_lines = [exchange["summary"], exchange.get("path_policy", ""), *exchange.get("checks", []), "绑定阶段仅验证文件生成与仿真准备；已有 history 或旧绑定不证明本次波形闭环已运行。"]
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
            self.cross_tb_tree.insert("", "end", iid=row["key"], values=(row["label"], row["vco"] or "未识别", row["replica"] or "未识别", row["source"], row["status"]))
        self._render_portable_report(_mapping(report.get("portable_setup")))
        self._render_signal_catalog()
        unresolved = [row for row in rows if row.get("required") and (not row.get("value") or str(row.get("status")) in {"ambiguous", "invalid", "error", "missing"})]
        manual = sum(len(values) for values in self._mapping_overrides.values()) + len(self._portable_overrides)
        setup = _mapping(report.get("portable_setup"))
        suffix = f"；{manual} 项手动覆盖" if manual else ""
        if str(self.vars["engine_config"].get() or "").strip():
            suffix += "；使用专家指定的执行文件"
        elif setup and not setup.get("ok"):
            suffix += "；执行设置待补充（高级设置）"
        elif setup.get("ok"):
            suffix += "；执行设置已自动生成"
        self.mapping_status_var.set((f"当前显示 {len(rows)} 项对应关系；{len(unresolved)} 项必需信息待确认" if unresolved else f"当前显示 {len(rows)} 项对应关系，运行前请预检") + suffix)
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
            self.portable_detail_var.set("待补充：" + "；".join(errors))

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
        self.catalog_status_var.set(catalog_summary(self._signal_report) + f" · 当前显示 {len(self._catalog_rows)} 项")

    def _catalog_selected(self, _event: Any = None) -> None:
        selected = self.catalog_tree.selection()
        row = self._catalog_rows.get(selected[0], {}) if selected else {}
        if row:
            self.catalog_detail_var.set(f"{row.get('name')}\n表达式 / 内容：{_display(row.get('expression'))}\n来源：{_display(row.get('source'))}\n详情：{_display(row.get('details'))}")

    def _choose_engine(self) -> None:
        path = self._choose_file("选择已有专用执行配置", str(self.vars["engine_config"].get()))
        if path:
            self.vars["engine_config"].set(path)
            self.last_preflight = None
            self.mapping_status_var.set("已指定专家执行文件；该文件优先于自动生成设置，需重新预检。")

    def _engine_changed(self, *_args: Any) -> None:
        if self._updating_form:
            return
        self._selection_revision += 1
        self.last_preflight = None
        self.preflight_status_var.set("执行配置来源已改变，需重新预检")
        if str(self.vars["engine_config"].get() or "").strip():
            self.mapping_status_var.set("使用专家指定的执行文件，优先于自动生成设置；请在高级设置核对后预检。")
        else:
            self.mapping_status_var.set("执行设置已恢复自动生成；请重新识别或预检查看当前结果。")
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
            self.start_button.configure(text="2  开始实际运行")
        if hasattr(self, "setup_start_button"):
            self.setup_start_button.configure(text="开始正式分析" if self._formal_signoff_enabled() else "开始分析")
        if hasattr(self, "execution_summary_var"):
            suffix = "正式 Signoff" if self._formal_signoff_enabled() else "非正式签核"
            self.execution_summary_var.set(f"实际运行 · {suffix}")

    def _update_maestro_banner(self) -> None:
        context = getattr(self, "maestro_context", {})
        if not context:
            return
        identity = "/".join(str(context.get(k) or "?") for k in ("library", "cell", "view"))
        label = "当前 Maestro" if self._context_mode else "启动来源（已切换 VCO）"
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
            "已带入启动窗口的 VCO；请选择完整 History。"
            if self._generated_reference() else "已带入启动窗口的 VCO；请选择 History 与器件噪声 TB。"
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
        self._submit("刷新当前 Maestro 的 History", lambda: self.controller.refresh_maestro_context(context), refreshed)

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
                label = "VCO" if role == "vco" else "噪声 TB"
                self.binding_status_var.set(f"{label} 没有可绑定的完整 History；请在 Maestro 完成仿真后刷新。")
            else:
                self.binding_status_var.set(
                    "已读取候选；请选择完整 VCO History。"
                    if self._generated_reference() else "已读取候选；请选择完整 History 与器件噪声 TB。"
                )
        self._submit("读取 VCO History" if role == "vco" else "读取噪声 TB History",
                     lambda: self.controller.inspect_maestro(payload), inspected)

    def _choose_directory(self, key: str) -> None:
        initial = str(self.vars[key].get() or self.vars["project_root"].get() or Path.cwd())
        value = filedialog.askdirectory(parent=self.root, initialdir=initial, title="选择目录")
        if value:
            self.vars[key].set(value)
            if key in ("vco_path", "replica_path"):
                self._schedule_role_inspection(key.split("_", 1)[0])

    def _choose_file(self, title: str, initial: str = "") -> str:
        return filedialog.askopenfilename(
            parent=self.root,
            title=title,
            initialdir=str(Path(initial).expanduser().parent) if initial else str(Path.cwd()),
            filetypes=(("JSON", "*.json"), ("所有文件", "*")),
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
        suffix = " 专家执行文件仍已指定，请在高级设置核对或恢复自动。" if str(self.vars["engine_config"].get() or "").strip() else ""
        self.mapping_status_var.set("TB / 测试 / 仿真记录已改变，旧对应已清除；请重新识别。" + suffix)
        self.binding_status_var.set("路径、测试或仿真记录已改变；需重新识别并绑定")
        self.last_preflight = None
        self.preflight_status_var.set("配置已改变，需重跑预检")

    def _raw_from_editor(self) -> dict[str, Any]:
        text = self.raw_text.get("1.0", "end").strip()
        if not text:
            return dict(self.raw_config)
        value = json.loads(text)
        if not isinstance(value, Mapping):
            raise ValueError("高级 JSON 顶层必须是 object")
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
            "已加载历史绑定；运行前仍会预检。" if ready
            else "选择一个已完成的 VCO History。" if generated
            else "请选择 VCO 和 Noise TB 的完整 History。"
        )
        if hasattr(self, "offset_input_frames"):
            self._refresh_offset_preview()
        if hasattr(self, "mapping_rows_frame"):
            self._render_mapping_report({})
            self.mapping_status_var.set("已加载配置中的手动对应；点击“检查绑定”核对实际名称与来源。")

    def _config_loaded(self, value: Any, initial: bool = False) -> None:
        config = strip_retired_noise_objects(_mapping(value))
        persisted = self.controller.workspace / "config.json"
        if initial and not persisted.is_file():
            # Keep the backend fail-safe dry default for API/CLI callers, while
            # making the desktop's ordinary workflow an explicitly authorised
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
                self.preference_status_var.set("已加载本来源偏好；请明确选择本次 History。")
        if error:
            self.preference_status_var.set("项目偏好未加载：" + error)
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
        self.binding_status_var.set("正在重新验证该 History 的项目映射。")
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
                self.preference_status_var.set("项目偏好未保存：" + error)
            else:
                self._preferences = stored
                self.preference_status_var.set("已保存本 Maestro 来源的项目偏好。")

        self._submit("保存项目偏好", save, saved, quiet=True)

    def _render_merged_config(self) -> None:
        try:
            config = self._config_from_ui()
        except Exception as exc:
            self._show_error(exc, "配置无效")
            return
        self.raw_config = config
        self._set_raw_text(config)
        self.status_var.set("表单已合并到高级 JSON")

    def _apply_raw_editor(self) -> None:
        try:
            config = self._raw_from_editor()
        except Exception as exc:
            self._show_error(exc, "高级 JSON 无效")
            return
        self.raw_config = config
        self._populate_form(config)
        self._set_raw_text(config)
        self.last_preflight = None
        self.preflight_status_var.set("配置已改变，需重跑预检")

    def _load_config_dialog(self) -> None:
        path = self._choose_file("加载配置 JSON")
        if path:
            self._submit("加载配置", lambda: self.controller.load_config(path), self._config_loaded)

    def _state_view(self) -> dict[str, Any]:
        tabs = (self.setup_tab, self.run_tab, self.result_tab, self.error_tab)
        selected_tab = next((name for name, tab in zip(("setup", "run", "results", "errors"), tabs)
                             if str(tab) == self.notebook.select()), "setup")
        summary = _mapping(_mapping(self.current_result_view).get("selected_summary"))
        return {"job_id": self.current_result_job_id or self.current_job_id,
                "point": self.point_var.get(), "operating_point_id": self.operating_point_var.get(),
                "offset_hz": summary.get("offset_Hz"), "noise_kind": self.noise_kind_var.get(),
                "x_scale": "log" if self.log_offset_var.get() else "linear",
                "log_offset": self.log_offset_var.get(), "harmonic_curve": self.harmonic_curve_var.get(),
                "theme": self.theme_var.get(), "selected_tab": selected_tab,
                "advanced_visible": self.advanced_visible_var.get()}

    def _save_state_dialog(self) -> None:
        if self._state_load_pending:
            messagebox.showwarning("正在加载状态", "请等状态加载完成后再保存。", parent=self.root)
            return
        name = simpledialog.askstring("保存状态", "状态名称（保存设置、手动对应和结果显示选择，不保存执行授权）", parent=self.root)
        if not name or not name.strip():
            return
        try:
            payload = {"name": name.strip(), "config": self._config_from_ui(), "view": self._state_view()}
        except Exception as exc:
            self._show_error(exc, "状态无法保存")
            return
        self._submit("保存状态", lambda: self.controller.save_state(payload),
                     lambda report: messagebox.showinfo("状态已保存", str(report.get("path") or ""), parent=self.root))

    def _load_state_dialog(self) -> None:
        if self._state_load_pending or self._job_command_pending or self._foreground_operations:
            messagebox.showwarning("当前操作尚未完成", "请等待绑定、预检或任务提交完成后加载状态。", parent=self.root)
            return

        def listed(report: Any) -> None:
            rows = [row for row in _sequence(_mapping(report).get("states")) if isinstance(row, Mapping)]
            if not rows:
                messagebox.showinfo("尚无已保存状态", "可先点击“保存状态”。\n" + str(_mapping(report).get("directory") or ""), parent=self.root)
                return
            dialog = tk.Toplevel(self.root)
            dialog.title("加载状态")
            dialog.transient(self.root)
            dialog.grab_set()
            ttk.Label(dialog, text="只恢复设置和已有结果选择；不会自动绑定、预检或仿真。", padding=12).pack(anchor="w")
            choices = {f"{row.get('name')} · {row.get('saved_at')} · {row.get('id')}": str(row["id"]) for row in rows}
            variable = tk.StringVar(value=next(iter(choices)))
            box = ttk.Combobox(dialog, state="readonly", textvariable=variable, values=tuple(choices), width=80)
            box.pack(fill="x", padx=12, pady=8)

            def chosen() -> None:
                identifier = choices.get(variable.get())
                dialog.destroy()
                if identifier:
                    self._load_saved_state(identifier)

            ttk.Button(dialog, text="加载所选状态", command=chosen).pack(pady=12)

        self._submit("列出状态", self.controller.list_states, listed)

    def _state_edit_snapshot(self) -> dict[str, Any]:
        """Capture unfinished edits without requiring a currently valid config."""
        return {"raw_editor": self.raw_text.get("1.0", "end-1c"),
                "raw_config": deep_merge({}, self.raw_config),
                "fields": self._collect_fields(),
                "formal_signoff": self.formal_signoff_var.get(),
                "selection_revision": self._selection_revision}

    def _load_saved_state(self, identifier: str) -> None:
        if self._state_load_pending or self._job_command_pending or self._foreground_operations:
            messagebox.showwarning("当前操作尚未完成", "请等待当前操作完成后加载状态。", parent=self.root)
            return
        before = self._state_edit_snapshot()
        self._state_load_pending = True
        self._job_command_pending = True

        def read() -> dict[str, Any]:
            if active_jobs(self.controller.jobs()):
                raise ValueError("有 queued/running 任务；请在任务结束后加载状态。当前设置未改变。")
            report = dict(self.controller.load_state(identifier))
            job_id = str(_mapping(_mapping(report.get("state")).get("view")).get("job_id") or "")
            if job_id:
                try:
                    job = self.controller.job(job_id)
                    if job.get("status") not in TERMINAL_STATES:
                        raise ValueError("保存的任务尚未结束")
                    report["job"] = job
                    report["result"] = self.controller.result(job_id)
                except Exception as exc:
                    report["result_warning"] = f"保存的结果 {job_id} 不可读取：{exc}；没有替换成其他任务。"
            return report

        def finished(report: Any) -> None:
            self._state_load_pending = False
            self._job_command_pending = False
            if self._state_edit_snapshot() != before:
                messagebox.showwarning("设置已更新", "加载期间表单已变化，未覆盖新的设置，请重新加载。", parent=self.root)
                return
            self._apply_saved_state(_mapping(report))

        def failed(_exc: Exception) -> None:
            self._state_load_pending = False
            self._job_command_pending = False

        self._submit("加载状态", read, finished, on_failure=failed)

    def _apply_saved_state(self, report: Mapping[str, Any]) -> None:
        state = _mapping(report.get("state"))
        view = _mapping(state.get("view"))
        self._config_loaded(_mapping(state.get("config")))
        self.last_preflight = None
        self.preflight_status_var.set("已加载状态；请重新识别绑定并预检，未自动执行")
        self.binding_status_var.set("状态仅恢复设置；需要重新识别并绑定两个 TB")
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
            self.harmonic_curve_var.set(str(view.get("harmonic_curve") or "全部贡献 + 总和"))
            requested = str(view.get("point") or "")
            if not requested:
                result = None
                warning += " 保存时没有选定结果采样点，未自动选择其他采样点。"
            else:
                self._result_loaded(result, job_id)
            choice = next((label for label, point in self._offset_point_choices.items() if point == requested), None)
            if requested and choice is None:
                # A saved coordinate is not permission to pick another sample.
                self._clear_loaded_result()
                self.current_job_id = ""
                self.current_job = {}
                warning += " 保存的工况 / 频偏不存在，未展示其他采样点。"
            elif choice:
                self.result_offset_var.set(choice)
                self._result_offset_changed()
        tabs = {"setup": self.setup_tab, "run": self.run_tab, "results": self.result_tab, "errors": self.error_tab}
        self.notebook.select(tabs.get(str(view.get("selected_tab")), self.setup_tab))
        for canvas in self._page_canvases.values():
            canvas.yview_moveto(0)
        if warning:
            messagebox.showwarning("状态已加载，结果待核对", warning, parent=self.root)

    def _save_config_dialog(self) -> None:
        try:
            config = self._config_from_ui()
        except Exception as exc:
            self._show_error(exc, "配置无效")
            return
        initial = str(self.controller.workspace / "config.json")
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="保存配置 JSON",
            initialdir=str(Path(initial).parent),
            initialfile=Path(initial).name,
            defaultextension=".json",
            filetypes=(("JSON", "*.json"), ("所有文件", "*")),
        )
        if not path:
            return

        def saved(report: Any) -> None:
            value = _mapping(report)
            self.raw_config = _mapping(value.get("config")) or config
            self._set_raw_text(self.raw_config)
            backup = value.get("backup")
            suffix = f"；备份：{backup}" if backup else ""
            messagebox.showinfo("配置已保存", f"{value.get('path', path)}{suffix}", parent=self.root)

        self._submit("保存配置", lambda: self.controller.save_config(config, path), saved)

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
            messagebox.showwarning("需要项目根目录", "请先选择只读扫描的项目根目录。", parent=self.root)
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
                self.binding_status_var.set("找到唯一 VCO 候选，正在只读检测…" if generated
                                            else "找到两个唯一角色候选，正在只读检测…")
                if generated:
                    self._schedule_role_inspection("vco")
                else:
                    self._inspect_pair()
            else:
                detail = "请手动选择 VCO 路径" if generated else "请手动选择两个不同路径"
                self.binding_status_var.set(f"发现 {len(paths)} 个 Maestro；角色不唯一，{detail}。")

        self._submit("发现 Maestro", lambda: self.controller.discover_maestro(payload), discovered)

    def _inspect_pair(self) -> None:
        try:
            config = self._config_from_ui()
        except Exception as exc:
            self._show_error(exc, "配置无效")
            return
        flat = flatten_config(config)
        generated = flat.get("replica_source") == "generated"
        if not flat.get("vco_path") or (not generated and not flat.get("replica_path")):
            detail = "请选择 VCO 的 Maestro 路径。" if generated else "请选择两个独立 Maestro 路径。"
            messagebox.showwarning("Maestro 未选择", detail, parent=self.root)
            return
        if generated and not flat.get("vco_history"):
            self.binding_status_var.set("请选择完整 VCO History；正在读取可用记录。")
            self._schedule_role_inspection("vco", refresh=True)
            return
        payload = maestro_pair_payload(config, include_fingerprints=False)
        submitted_selection = self._pair_selection()
        submitted_revision = getattr(self, "_selection_revision", 0)

        def inspected(value: Any) -> None:
            if self._pair_selection() != submitted_selection or getattr(self, "_selection_revision", 0) != submitted_revision:
                self.binding_status_var.set("选择已改变；已丢弃过期检测结果，请重新检测")
                return
            self._pair_inspected(value)

        self._submit("检查 VCO 与器件噪声参考", lambda: self.controller.inspect_pair(payload), inspected)

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
        ambiguous_tests = ["VCO" if role == "vco" else "噪声 TB" for role in ("vco", "flicker")
                           if len(_sequence(_mapping(roles.get(role)).get("test_names"))) > 1
                           and not _mapping(_mapping(roles.get(role)).get("selected_test"))]
        unresolved_fields = [row for row in mapping_review_rows(report) if row.get("status") in {"ambiguous", "invalid", "missing", "stale"} and row.get("required", True)]
        if accepted:
            self.binding_status_var.set("VCO History 与目标 MOS 已确认；开始后生成器件参考。" if generated
                                        else "两个 TB 与完整仿真记录已绑定。")
            self.raw_config = self._config_from_ui()
            self._set_raw_text(self.raw_config)
        elif ambiguous_tests:
            self.binding_status_var.set("、".join(ambiguous_tests) + " 有多个 Maestro Test；请在 Test 下拉中选择。器件或电流检测源请在高级对应表单独选择。")
        elif unresolved_fields:
            names = "、".join(("VCO " if row["role"] == "vco" else "噪声 TB ") + str(row.get("label") or row["key"]) for row in unresolved_fields[:4])
            if generated and any(row["key"] == "target_instance" for row in unresolved_fields):
                self.binding_status_var.set("请在器件噪声参考中选择目标 MOS。")
            else:
                self.binding_status_var.set("需手动对应：" + names + "；在高级设置逐行选择或输入后应用验证。")
        elif manual:
            labels = "、".join("VCO" if role == "vco" else "Flicker" for role in manual)
            self.binding_status_var.set(f"{labels} 存在多个完整 history；不会猜测，请手动选择后再次检测")
        elif issues:
            summary = "；".join(f"{item.get('code', 'INFO')} {item.get('message') or item.get('title') or item.get('detail') or ''}" for item in issues[:3])
            self.binding_status_var.set(summary)
        else:
            self.binding_status_var.set("未获得可验证的 VCO History，请检查选择后重试。" if generated
                                        else "未获得两个可验证的完整 History，请检查选择后重试。")
        self.last_preflight = None
        self.preflight_status_var.set("绑定已检测，需运行预检")
        self._render_mapping_report(report)

    # ---- preflight and jobs ----------------------------------------

    def _build_run_tab(self) -> None:
        content = self._scrollable_page(self.run_tab)
        readiness = ttk.LabelFrame(
            content, text="运行就绪", style="Card.TLabelframe", padding=(14, 12)
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
            actions, text="1  运行静态预检", command=self._preflight
        ).pack(side="left")
        self.start_button = ttk.Button(
            actions, text="2  开始实际运行", style="Primary.TButton", command=self._start_run
        )
        self.start_button.pack(side="left", padx=8)
        ttk.Button(actions, text="刷新任务", command=self._refresh_jobs).pack(side="left", padx=(10, 4))
        ttk.Button(actions, text="恢复失败/中断任务", command=self._resume_job).pack(side="left", padx=4)
        ttk.Button(actions, text="导入 Replay Bundle", command=self._import_bundle).pack(side="left", padx=4)
        stop_actions = ttk.Frame(readiness)
        stop_actions.grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.stop_offset_button = ttk.Button(stop_actions, text="当前频偏完成后停止", command=self._stop_after_current_offset, state="disabled")
        self.partial_result_button = ttk.Button(stop_actions, text="查看已完成的结果", command=self._load_available_result, state="disabled")
        self.partial_result_button.pack(side="left", padx=8)
        self.preflight_help = self._help(readiness, textvariable=self.preflight_detail_var)
        self.preflight_help.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        self.preflight_issue_actions = ttk.Frame(readiness)
        self.preflight_issue_actions.grid(row=6, column=0, columnspan=2, sticky="w", pady=(3, 0))
        self.preflight_locate_button = ttk.Button(self.preflight_issue_actions, text="定位待补充项", command=self._locate_preflight_issue)
        self.preflight_locate_button.pack(side="left")
        ttk.Button(self.preflight_issue_actions, text="查看全部具体问题", command=self._show_preflight_issues).pack(side="left", padx=8)
        self.preflight_help.grid_remove()
        self.preflight_issue_actions.grid_remove()

        pane = ttk.Panedwindow(content, orient="vertical")
        pane.pack(fill="both", expand=True)
        upper = ttk.Panedwindow(pane, orient="horizontal")
        lower = ttk.LabelFrame(
            pane, text="当前任务事件", style="Card.TLabelframe", padding=8
        )
        pane.add(upper, weight=3)
        pane.add(lower, weight=2)

        jobs_frame = ttk.LabelFrame(
            upper, text="任务", style="Card.TLabelframe", padding=8
        )
        checks_frame = ttk.LabelFrame(
            upper, text="预检矩阵", style="Card.TLabelframe", padding=8
        )
        upper.add(jobs_frame, weight=2)
        upper.add(checks_frame, weight=3)
        self.jobs_tree = self._tree(
            jobs_frame,
            (("id", "Job ID", 190), ("status", "状态", 80), ("message", "说明", 220)),
        )
        self._with_scroll(jobs_frame, self.jobs_tree).pack(fill="both", expand=True)
        self.jobs_tree.bind("<<TreeviewSelect>>", self._job_selected)
        self.preflight_tree = self._tree(
            checks_frame,
            (("status", "状态", 65), ("id", "检查", 140), ("detail", "详情", 360), ("code", "代码", 90)),
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
            self._show_error(exc, "配置无效")
            return
        def rendered(value: Any) -> None:
            if not self._config_still_current(config):
                self.last_preflight = None
                self.preflight_status_var.set("配置已改变；已丢弃过期预检结果")
                return
            self._render_preflight(value)
            self._remember_project_preferences(config, _mapping(value))

        self._submit("运行预检", lambda: self.controller.preflight(config), rendered)

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
            "config_path": "高级设置 → 仿真执行设置（先识别并绑定两个 TB）",
            "physical_profile": "高级设置 → 仿真执行设置中提示的器件 / 节点对应",
            "portable_setup": "高级设置 → 仿真执行设置",
            "execution.allow_execute": "当前执行请求未获得权限，请重新点击预检或开始",
        }
        required = [
            field_labels.get(str(item), str(item))
            for item in _sequence(report.get("required_user_fields"))
        ]
        summary = _mapping(report.get("summary"))
        warnings = int(summary.get("warn") or 0)
        failures = int(summary.get("fail") or 0)
        if report.get("ok"):
            suffix = f" · {warnings} 项警告" if warnings else ""
            self.preflight_status_var.set(f"静态预检通过{suffix}")
            self.preflight_detail_var.set("")
            self._preflight_messages = []
            self.preflight_help.grid_remove()
            self.preflight_issue_actions.grid_remove()
        else:
            self.preflight_status_var.set(f"静态预检未通过 · {failures} 项失败")
            details = [row["message"] for row in self._preflight_issues]
            for item in _sequence(report.get("checks")):
                if not isinstance(item, Mapping) or item.get("status") != "fail":
                    continue
                if self._preflight_issues and item.get("field") in {"portable_setup", "physical_profile", "config_path"}:
                    continue
                details.append(str(item.get("label") or item.get("id") or "检查") + "：" + str(item.get("detail") or item.get("message") or "请检查该项"))
            if not details and required:
                details = ["需补充：" + "、".join(required)]
            self._preflight_messages = details
            text = "\n".join(f"{index + 1}. {detail}" for index, detail in enumerate(details[:3]))
            if len(details) > 3:
                text += f"\n另有 {len(details) - 3} 项，点击“查看全部具体问题”。"
            self.preflight_detail_var.set(text)
            self.preflight_help.grid()
            self.preflight_issue_actions.grid()
            self.preflight_locate_button.configure(state="normal" if self._preflight_issues else "disabled",
                                                   text="去选择：" + self._preflight_issues[0]["target_label"] if self._preflight_issues else "定位待补充项")

    def _show_preflight_issues(self) -> None:
        if not self.last_preflight:
            self.preflight_status_var.set("配置已改变，请先重新预检")
            return
        lines = [row["message"] + "\n位置：高级设置 → " + ("信号用途与跨 TB 对应 → " + row["target_label"] if row["tab"] == "mapping" else "仿真执行设置 → " + row["target_label"])
                 for row in self._preflight_issues]
        portable_messages = {row["message"] for row in self._preflight_issues}
        lines.extend(line for line in self._preflight_messages if line not in portable_messages)
        messagebox.showinfo("预检具体问题", "\n\n".join(f"{index + 1}. {line}" for index, line in enumerate(lines)) or self.preflight_detail_var.get(), parent=self.root)

    def _locate_preflight_issue(self) -> None:
        if not self.last_preflight or not self._preflight_issues:
            self.preflight_status_var.set("配置已改变，请先重新预检")
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
            messagebox.showwarning("正在确认关闭", "请等待关闭检查完成后再提交任务。", parent=self.root)
            return
        if self._job_command_pending:
            messagebox.showwarning("任务提交中", "已有运行或恢复命令正在提交，请勿重复操作。", parent=self.root)
            return
        try:
            config = self._execution_config_from_ui()
        except Exception as exc:
            self._show_error(exc, "配置无效")
            return
        self._job_command_pending = True

        def command_failed(_exc: Exception) -> None:
            self._job_command_pending = False

        def after_preflight(report: Any) -> None:
            if not self._config_still_current(config):
                self._job_command_pending = False
                self.last_preflight = None
                self.preflight_status_var.set("配置已改变；运行已阻止，请重新预检")
                messagebox.showwarning(
                    "配置已改变",
                    "运行前预检对应的是旧配置，未创建任务。请重新预检后再运行。",
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
                messagebox.showerror("运行被阻止", "预检未通过，未创建任务。\n\n" + self.preflight_detail_var.get(), parent=self.root)
                return
            submitted = flatten_config(config)
            run_root = str(submitted.get("run_root") or "").strip() or None
            bundle_path = str(submitted.get("bundle_path") or "").strip() or None

            def started(value: Any) -> None:
                self._run_started(value)
                self._remember_project_preferences(config, _mapping(report))

            self._submit(
                "提交后台任务",
                lambda: self.controller.start_run(config, run_root=run_root, bundle_path=bundle_path),
                started,
                on_failure=command_failed,
            )

        def begin_preflight() -> None:
            self._submit(
                "运行前预检",
                lambda: self.controller.preflight(config),
                after_preflight,
                on_failure=command_failed,
            )

        def after_active_jobs(value: Any) -> None:
            running = active_jobs(_mapping(value))
            if running:
                self._job_command_pending = False
                identifiers = "、".join(str(item.get("id") or "?") for item in running)
                messagebox.showwarning(
                    "已有活动任务",
                    "已有 queued/running 任务，未提交新任务：" + identifiers,
                    parent=self.root,
                )
                return
            begin_preflight()

        self._submit(
            "检查活动任务",
            self.controller.jobs,
            after_active_jobs,
            on_failure=command_failed,
        )

    def _run_started(self, value: Any) -> None:
        self._job_command_pending = False
        self._clear_loaded_result()
        self.current_job_id = str(value)
        self.job_status_var.set(f"任务 {self.current_job_id} 已提交")
        self.notebook.select(self.run_tab)
        self._refresh_jobs(silent=True)
        self._schedule_poll(100)

    def _refresh_jobs(self, silent: bool = False) -> None:
        self._submit("刷新任务", self.controller.jobs, self._render_jobs, quiet=silent)

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
            self._show_error(exc, "读取任务失败")

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
            self.job_status_var.set(self.job_status_var.get() + f" · 已完成 {len(_sequence(sweep.get('completed_offsets_hz')))}/{len(_sequence(sweep.get('offsets_hz')))} 个频偏")
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
        self.job_log.insert("1.0", "\n".join(lines) or "暂无事件")
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
                "读取结果",
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
                self.job_status_var.set("已请求停止：当前频偏会完整结束，已完成结果保留；之后可恢复剩余频偏。")
                self.stop_offset_button.configure(state="disabled")
                self._schedule_poll(0)
        self._submit("请求频偏完成后停止", lambda: self.controller.stop_after_current_offset(job_id), stopped)

    def _load_available_result(self) -> None:
        if not self.current_job_id:
            return
        job_id = self.current_job_id
        self._submit("读取已完成结果", lambda: self.controller.result(job_id), lambda value: self._result_loaded(value, job_id))

    def _resume_job(self) -> None:
        if self._close_check_pending:
            messagebox.showwarning("正在确认关闭", "请等待关闭检查完成后再恢复任务。", parent=self.root)
            return
        if self._job_command_pending:
            messagebox.showwarning("任务提交中", "已有运行或恢复命令正在提交，请勿重复操作。", parent=self.root)
            return
        if not self.current_job_id:
            messagebox.showwarning("未选择任务", "请选择 failed 或 interrupted 任务。", parent=self.root)
            return
        job_id = self.current_job_id
        self._job_command_pending = True

        def resumed(_value: Any) -> None:
            self._job_command_pending = False
            if self.current_job_id == job_id:
                # A previously viewed interrupted result is a partial snapshot,
                # not the terminal result of this new execution attempt.
                self._result_terminal_loaded = False
                self.job_status_var.set(f"任务 {job_id} 已提交恢复")
            self._schedule_poll(100)

        def command_failed(_exc: Exception) -> None:
            self._job_command_pending = False

        def after_active_jobs(value: Any) -> None:
            running = active_jobs(_mapping(value))
            if running:
                self._job_command_pending = False
                identifiers = "、".join(str(item.get("id") or "?") for item in running)
                messagebox.showwarning(
                    "已有活动任务",
                    "已有 queued/running 任务，未恢复另一任务：" + identifiers,
                    parent=self.root,
                )
                return
            self._submit(
                "恢复任务",
                lambda: self.controller.resume(job_id),
                resumed,
                on_failure=command_failed,
            )

        self._submit(
            "检查活动任务",
            self.controller.jobs,
            after_active_jobs,
            on_failure=command_failed,
        )

    def _import_bundle(self) -> None:
        path = filedialog.askdirectory(parent=self.root, title="选择 canonical replay bundle 目录")
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
        self.noise_kind_var.set("flicker")  # the synthetic example carries no thermal source data
        self._import_bundle_path(str(path), label="示例 ideal-LC")

    def _import_bundle_path(self, path: str, label: str = "") -> None:
        def imported(value: Any) -> None:
            report = _mapping(value)
            self._clear_loaded_result()
            self.current_job_id = str(report.get("job_id") or "")
            self.job_status_var.set(f"Replay 已导入：{self.current_job_id}")
            self._set_window_title(label or Path(path).name)
            self._refresh_jobs(silent=True)
            self._schedule_poll(0)

        self._submit("导入 canonical replay", lambda: self.controller.import_bundle(path, "canonical"), imported)

    # ---- results ----------------------------------------------------

    def _clear_loaded_result(self) -> None:
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
        self.noise_pie_note_var.set("尚无当前任务结果")
        self.noise_contribution_note_var.set("尚无当前任务结果")
        self.result_quality_var.set("")
        self.isf_chart.clear("N/A")
        self.qk_chart.clear("N/A")
        self.metrics_var.set("尚无当前任务结果")
        self._result_terminal_loaded = False
        self._curve_cache = {}
        if hasattr(self, "operating_point_var"):
            self.operating_point_var.set("")
            self.result_offset_var.set("")
            self.operating_point_box.configure(values=())
            self.result_offset_box.configure(values=())
            self.phase_noise_chart.clear("N/A")
            if hasattr(self, "phase_error_chart"):
                self.phase_error_chart.clear("无同口径 HBnoise 参考")
                self._phase_note.set("等待结果")
                self._phase_menu.delete(0, "end")
                self._phase_visibility.clear()
                self._phase_data_key = None
                self._phase_full_x = None
                self._reset_phase_view()
            self.harmonic_noise_chart.clear("N/A")
            self._clear_tree(self.metric_values_tree)

    def _build_result_tab(self) -> None:
        content = self._scrollable_page(self.result_tab, minimum_width=780, fit_width=True)
        self.result_content = content
        toolbar = ttk.Frame(content)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Label(toolbar, text="工况").pack(side="left")
        self.operating_point_box = ttk.Combobox(toolbar, textvariable=self.operating_point_var, state="readonly", width=24)
        self.operating_point_box.pack(side="left", padx=(5, 12))
        self.operating_point_box.bind("<<ComboboxSelected>>", self._operating_point_changed)
        ttk.Label(toolbar, text="频偏").pack(side="left")
        self.result_offset_box = ttk.Combobox(toolbar, textvariable=self.result_offset_var, state="readonly", width=15)
        self.result_offset_box.pack(side="left", padx=(5, 12))
        self.result_offset_box.bind("<<ComboboxSelected>>", self._result_offset_changed)
        # Exports retain the exact selected sample.
        self.point_box = ttk.Combobox(toolbar, textvariable=self.point_var, state="readonly")
        ttk.Label(toolbar, text="噪声").pack(side="left")
        self.noise_kind_box = ttk.Combobox(toolbar, textvariable=self.noise_kind_var,
                                         state="readonly", values=("both", "flicker", "thermal"), width=9)
        self.noise_kind_box.pack(side="left", padx=5)
        self.noise_kind_box.bind("<<ComboboxSelected>>", self._point_changed)
        exports = ttk.Menubutton(toolbar, text="导出数据…")
        export_menu = tk.Menu(exports, tearoff=False)
        exports.configure(menu=export_menu)
        for label, command in (("结果 JSON…", self._export_result_json),
                               ("Summary CSV…", self._export_summary_csv),
                               ("当前采样点谐波 CSV…", self._export_harmonics_csv)):
            export_menu.add_command(label=label, command=command)
        exports.pack(side="right")
        summary = ttk.Label(content, textvariable=self.metrics_var, style="Help.TLabel", justify="left")
        summary.pack(fill="x", pady=(0, 5))
        summary.bind("<Configure>", lambda event: summary.configure(wraplength=max(250, event.width - 12)))

        charts = ttk.Notebook(content)
        charts.pack(fill="both", expand=True)
        tables = ttk.Notebook(charts)
        self.result_tables = tables
        self.result_charts = charts

        summary_frame = ttk.Frame(tables, padding=4)
        harmonic_frame = ttk.Frame(tables, padding=4)
        tables.add(summary_frame, text="Summary")
        tables.add(harmonic_frame, text="Flicker 原始谐波诊断")
        contribution_frame = ttk.Frame(tables, padding=4)
        tables.add(contribution_frame, text="单频点噪声贡献 · 仿算对比")
        metric_frame = ttk.Frame(tables, padding=4)
        tables.add(metric_frame, text="全部指标与质量")
        self.summary_tree = self._tree(
            summary_frame,
            (
                ("point", "工况 / 采样点", 170),
                ("offset", "频偏 (Hz)", 100),
                ("f0", "f0 (Hz)", 110),
                ("calculated", "ISF 计算 (dBc/Hz)", 200),
                ("spectre", "VCO 总仿真相噪 (dBc/Hz)", 195),
                ("dynamic", "计算 − 总仿真 (dB)", 175),
                ("pair", "gm pair flicker 同口径误差 (dB)", 220),
            ),
        )
        self._with_scroll(summary_frame, self.summary_tree).pack(fill="both", expand=True)
        self.harmonic_tree = self._tree(
            harmonic_frame,
            (
                ("k", "k", 45),
                ("mdc", "|Mdc|", 85),
                ("mdyn", "|Mdyn|", 85),
                ("a", "Ak", 75),
                ("delta", "δk (deg)", 80),
                ("qr", "Qk Re", 90),
                ("qi", "Qk Im", 90),
                ("qmag", "|Qk|", 90),
                ("cumr", "ΣQ Re", 95),
                ("cumi", "ΣQ Im", 95),
                ("hdbc", "双器件单阶等效 dBc/Hz", 180),
                ("cdbc", "双器件相干累计 dBc/Hz", 180),
            ),
        )
        self._with_scroll(harmonic_frame, self.harmonic_tree).pack(fill="both", expand=True)
        self._help(harmonic_frame, text="本表固定展示 flicker 原始诊断，不随噪声筛选变成 thermal。Mdc、Mdyn、Ak、Qk 保留目标器件原值；末两列为双器件 flicker 等效相噪。thermal 独立频带功率见曲线与噪声贡献页，不混入相干 Qk。").pack(fill="x", pady=(5, 0))
        self.harmonic_tree.bind("<<TreeviewSelect>>", self._harmonic_selected)
        self._bind_metric_columns(self.summary_tree, ("point", "offset_Hz", "f0_Hz", "thermal_noise", "spectre_total_dBc_Hz", "calculated_vs_total_db", "pair_error_dB"))
        self._bind_metric_columns(self.harmonic_tree, ("k", "Mdc_mag_A_rtHz", "Mdyn_mag_A_rtHz", "A_k", "delta_k_deg", "Q_real_V_rtHz", "Q_imag_V_rtHz", "Q_mag_V_rtHz", "cum_real_V_rtHz", "cum_imag_V_rtHz", "pair_harmonic_equivalent_dBc_Hz", "pair_cumulative_dBc_Hz"))
        ttk.Label(contribution_frame, text="跟随页面顶部的设计工况、固定频偏和噪声类型。", style="Help.TLabel").pack(anchor="w", pady=(0, 6))
        pies = ttk.Frame(contribution_frame)
        pies.pack(fill="x")
        pies.columnconfigure(0, weight=1)
        pies.columnconfigure(1, weight=1)
        self.simulated_noise_pie = ContributionBars(pies, title="HBnoise · 仿真源", on_select=self._select_contribution)
        self.calculated_noise_pie = ContributionBars(pies, title="ISF · 已建模源", on_select=self._select_contribution)
        self.simulated_noise_pie.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.calculated_noise_pie.grid(row=0, column=1, sticky="nsew")
        self._help(contribution_frame, textvariable=self.noise_pie_note_var).pack(fill="x", pady=(4, 6))
        self.noise_contribution_tree = self._tree(contribution_frame, (
            ("label", "仿真贡献 / 对比项", 250), ("device", "器件", 150),
            ("mechanism", "噪声机制", 160), ("simulated", "仿真 (dBc/Hz)", 135),
            ("calculated", "ISF 计算 (dBc/Hz)", 155), ("fraction", "占总仿真 PSD (%)", 155),
            ("psd", "PSD (V²/Hz)", 140),
        ))
        self._with_scroll(contribution_frame, self.noise_contribution_tree).pack(fill="both", expand=True)
        self._help(contribution_frame, textvariable=self.noise_contribution_note_var).pack(fill="x", pady=(4, 0))
        self._help(contribution_frame, text="汇总行与器件明细有重叠，不能再次相加。占比按线性 PSD / VCO 总 PSD 计算；dB 数字不可相加。未建模或缺少数据的噪声不填 ISF 计算值。").pack(fill="x")
        self.metric_values_tree = self._tree(metric_frame, (("field", "当前采样点 / 谐波指标", 240), ("value", "值", 320), ("unit", "单位", 130)))
        self._with_scroll(metric_frame, self.metric_values_tree).pack(fill="both", expand=True)
        self._help(metric_frame, textvariable=self.result_quality_var).pack(fill="x", pady=(6, 0))
        self.metric_values_tree.bind("<Double-1>", self._show_selected_raw_metric)
        self.metric_values_tree.bind("<Return>", self._show_selected_raw_metric)

        noise_frame = ttk.Frame(charts, padding=4)
        harmonic_plot_frame = ttk.Frame(charts, padding=4)
        diagnostics = ttk.Panedwindow(charts, orient="horizontal")
        charts.add(noise_frame, text="相噪与误差")
        charts.add(tables, text="明细与贡献")
        charts.add(harmonic_plot_frame, text="谐波 / 输入频带")
        charts.add(diagnostics, text="Flicker ISF / 复数累计")
        noise_controls = ttk.Frame(noise_frame)
        noise_controls.pack(fill="x", pady=(2, 4))
        ttk.Checkbutton(noise_controls, text="对数频偏轴", variable=self.log_offset_var, command=self._render_noise_curves).pack(side="left")
        ttk.Button(noise_controls, text="复位视图", command=self._reset_phase_view).pack(side="left", padx=6)
        series_button = ttk.Menubutton(noise_controls, text="曲线显隐")
        self._phase_menu = tk.Menu(series_button, tearoff=False)
        series_button.configure(menu=self._phase_menu)
        series_button.pack(side="left")
        figure_button = ttk.Menubutton(noise_controls, text="导出图…")
        figure_menu = tk.Menu(figure_button, tearoff=False)
        figure_button.configure(menu=figure_menu)
        figure_button.pack(side="left", padx=6)
        self.phase_noise_chart = LineChart(noise_frame, height=215)
        self.phase_noise_chart.pack(fill="x")
        self.phase_error_chart = LineChart(noise_frame, height=145, empty_text="无同口径 HBnoise 参考")
        self.phase_error_chart.pack(fill="x", pady=(3, 0))
        self._phase_visibility: dict[str, tk.BooleanVar] = {}
        self._phase_full_x: tuple[float, float] | None = None
        self._phase_view_x: tuple[float, float] | None = None
        self._phase_data_key: tuple[str, str, str] | None = None
        self._phase_log_x = True
        self._phase_note = tk.StringVar(value="等待结果")
        for title, chart in (("相噪图", self.phase_noise_chart), ("误差图", self.phase_error_chart)):
            chart.enable_x_navigation(self._navigate_phase_view)
            chart.bind("<Double-Button-1>", lambda event, plot=chart: self._select_plot_sample(plot, event), add="+")
            figure_menu.add_command(label=f"{title}（EPS）…", command=lambda plot=chart: self._export_chart(plot))
        self._help(noise_frame, textvariable=self._phase_note).pack(fill="x")
        harmonic_controls = ttk.Frame(harmonic_plot_frame)
        harmonic_controls.pack(fill="x")
        ttk.Label(harmonic_controls, text="显示").pack(side="left")
        self.harmonic_curve_box = ttk.Combobox(harmonic_controls, textvariable=self.harmonic_curve_var, state="readonly", width=33)
        self.harmonic_curve_box.pack(side="left", padx=8)
        self.harmonic_curve_box.bind("<<ComboboxSelected>>", lambda _event: self._render_noise_curves())
        self.harmonic_noise_chart = LineChart(harmonic_plot_frame, height=285)
        self.harmonic_noise_chart.pack(fill="both", expand=True)
        self._help(harmonic_plot_frame, text="flicker：各阶 Qk 复数相干相加；thermal：同一独立输入频带内的路径先相干合成，再对独立频带与独立源加功率。两者不能混用相加规则；所有 dB 值均不可直接相加。").pack(fill="x")

        isf_frame = ttk.LabelFrame(diagnostics, text="ISF vs normalized phase", padding=4)
        qk_frame = ttk.LabelFrame(diagnostics, text="Flicker 目标器件原始 ΣQk", padding=4)
        diagnostics.add(isf_frame, weight=1)
        diagnostics.add(qk_frame, weight=1)
        self.isf_chart = LineChart(isf_frame, height=270)
        self.isf_chart.pack(fill="both", expand=True)
        self.qk_chart = PhasorChart(qk_frame, height=270, on_select=self._select_harmonic)
        self.qk_chart.pack(fill="both", expand=True)
        glossary = ttk.Frame(content)
        glossary.pack(fill="x", pady=(6, 0))
        ttk.Label(glossary, text="指标说明").pack(side="left")
        self._metric_choices = {f"{definition['label']} · {key}": key for key, definition in metric_catalog().items()
                                if key not in LEGACY_DEVICE_COMPARISON_FIELDS}
        self.metric_choice_box = ttk.Combobox(glossary, textvariable=self.metric_choice_var, state="readonly", values=tuple(self._metric_choices), width=43)
        self.metric_choice_box.pack(side="left", padx=8)
        self.metric_choice_box.bind("<Return>", lambda _event: self._show_metric(self._metric_choices.get(self.metric_choice_var.get(), "")))
        ttk.Button(glossary, text="查看含义与用途", command=lambda: self._show_metric(self._metric_choices.get(self.metric_choice_var.get(), ""))).pack(side="left")

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
            self._show_error(exc, "结果工况 / 频偏身份无效")
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
                self.metrics_var.set("规划完成 · 未启动仿真，因此没有数值结果")
                self.isf_chart.clear("仅生成执行计划")
                self.qk_chart.clear("真实运行完成后显示")
                self.phase_noise_chart.clear("仅生成计划，没有数值结果")
                self.harmonic_noise_chart.clear("仅生成计划，没有数值结果")
                self.job_status_var.set(
                    f"任务 {job_id} 已完成规划；如需结果，请使用真实运行"
                )

    def _operating_point_changed(self, _event: Any = None) -> None:
        if self.current_result is None:
            return
        identity = self.operating_point_var.get()
        try:
            choices = result_sample_choices(self.current_result, identity)
        except Exception as exc:
            self._show_error(exc, "结果频偏无法选择")
            return
        if not choices:
            selected = next((item for item in self._operating_points if item["id"] == identity), {})
            choices = [{"point": point, "label": "频偏未记录"} for point in selected.get("points", [])]
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
            messagebox.showinfo("指标说明", "请先选择指标。未定义的原始扩展字段会保留其原值，不推断物理含义。", parent=self.root)
            return
        text = "\n\n".join((f"{definition['label']}（{definition['unit']}）", "含义：" + definition["meaning"], "用途：" + definition["purpose"], "口径：" + definition["scope"], "公式：" + definition["formula"], "N/A 表示缺失、不适用或无法定义；不代表零，也不代表质量通过。"))
        messagebox.showinfo("指标说明 · " + definition["label"], text, parent=self.root)

    def _show_selected_raw_metric(self, _event: Any = None) -> None:
        selected = self.metric_values_tree.selection()
        if selected:
            self._show_metric(self._metric_row_fields.get(selected[0], ""))

    def _render_metric_values(self, harmonic: Any = None) -> None:
        if self.current_result_view is None:
            return
        summary = _mapping(self.current_result_view.get("selected_summary"))
        rows = [("工况", key, value) for key, value in summary.items()
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
            messagebox.showwarning("没有可导出的曲线", "请选择有数据的工况或噪声类型。", parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="导出当前图形", defaultextension=".eps",
                                           filetypes=(("EPS 矢量图", "*.eps"),))
        if path:
            try:
                chart._clear_hover()
                chart.postscript(file=path, colormode="color", width=chart.winfo_width(), height=chart.winfo_height())
            except (OSError, tk.TclError) as exc:
                self._show_error(exc, "图形导出失败")

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
                item.update(name="HBnoise · VCO 全部源", dash=True)
        for item in comparison:
            if item["id"] != "matched_error" and any(point[1] is not None for point in item["points"]):
                curves.append(dict(item, dash=item["id"] == "matched_reference"))
        if any(item["id"] == "matched_prediction" for item in curves):
            for item in curves:
                if item["id"] == "calculated":
                    item["name"] = "ISF · 对称估算 +3.01 dB"
        self.phase_noise_chart.set_series(curves)
        self.phase_error_chart.set_series([error] if error else [])
        log_x = self.log_offset_var.get()
        self.phase_noise_chart.set_axes(x_label="频偏 (Hz)", y_label="相噪 (dBc/Hz)", log_x=log_x)
        self.phase_error_chart.set_axes(x_label="频偏 (Hz)", y_label="Δ (dB)", log_x=log_x, include_zero=True)
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
        scope = error["scope"] if error and any(point[1] is not None for point in error["points"]) else "同口径参考缺失"
        self._phase_note.set(f"误差范围 · {scope}     Ctrl + 滚轮缩放 · 中键拖动 · 双击采样点查看明细")

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
            key = (identity, "harmonics:" + scope + ":" + noise_kind)
            if key not in self._curve_cache:
                self._curve_cache[key] = harmonic_noise_series(self.current_result, identity, scope=scope, noise_kind=noise_kind)
            series = self._curve_cache[key]
            orders = sorted({int(item["k"]) for item in series if item.get("k") is not None})
            flicker_orders = sorted({int(item["k"]) for item in series
                                    if str(item.get("id", "")).startswith("harmonic_") and item.get("k") is not None})
            band_label = {"flicker": "k", "thermal": "|m|", "both": "k"}[noise_kind]
            choices = {"全部贡献 + 总和": ("all", None)}
            choices.update({f"仅 {band_label}={order} + 总和": ("one", order) for order in orders})
            if noise_kind == "flicker":
                choices.update({f"flicker 累计 0…k={order} + 总和": ("cumulative", order) for order in flicker_orders})
            self.harmonic_curve_box.configure(values=tuple(choices))
            if self.harmonic_curve_var.get() not in choices:
                self.harmonic_curve_var.set("全部贡献 + 总和")
            mode, order = choices[self.harmonic_curve_var.get()]
            if mode == "one":
                series = [item for item in series if item.get("k") == order or item.get("k") is None]
            elif mode == "cumulative":
                key = (identity, "cumulative:" + scope + ":" + noise_kind + ":" + str(order))
                if key not in self._curve_cache:
                    self._curve_cache[key] = cumulative_noise_series(self.current_result, identity, order, scope=scope, noise_kind=noise_kind)
                series = self._curve_cache[key] + [item for item in series if item.get("k") is None]
            self.harmonic_noise_chart.set_axes(x_label="频偏 (Hz)", y_label="等效相噪 (dBc/Hz)", log_x=self.log_offset_var.get())
            self.harmonic_noise_chart.set_series(series)
        except Exception as exc:
            self.phase_noise_chart.clear("结果缺少明确的频偏 / 工况坐标")
            self.phase_error_chart.clear("无法构造同口径误差")
            self.harmonic_noise_chart.clear("无法构造谐波频偏曲线")
            self.result_quality_var.set("结果曲线待核对：" + str(exc))

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
        self.noise_kind_box.configure(values=("flicker",) if generated else ("both", "flicker", "thermal"))
        if generated and self.noise_kind_var.get() != "flicker":
            self.noise_kind_var.set("flicker")
        try:
            view = build_result_view(
                self.current_result,
                job_id=job_id,
                point=str(self.point_var.get() or "") or None,
                noise_scope="pair",
                noise_kind=self.noise_kind_var.get(),
            )
        except Exception as exc:
            self._show_error(exc, "结果无法呈现")
            return
        self.current_result_view = view
        kind_label = {"flicker": "双器件 flicker", "thermal": "thermal", "both": "flicker + thermal"}.get(self.noise_kind_var.get(), "flicker + thermal")
        if generated:
            kind_label = "对称双管估算"
        self.summary_tree.heading("calculated", text=f"ISF {kind_label} (dBc/Hz)")
        self.summary_tree.heading("pair", text="单管同口径误差 (dB)" if generated else "gm pair flicker 同口径误差 (dB)")
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
            "本表使用所选工况、频偏的 VCO 仿真 PM-SSB 数据。各器件噪声机制明细按 PSD 从高到低排列。")
        self._render_metric_values()
        metrics = _mapping(view.get("metrics"))
        trust_warning = _mapping(self.current_result.get("trust_warning"))
        trust_label = str(view.get("run_kind") or "replay").upper()
        if not view.get("signoff"):
            trust_label += " / NON-SIGNOFF"
        if trust_warning.get("code"):
            trust_label += f" {trust_warning['code']}"
        self.metrics_var.set(
            "{kind} · f0={f0} Hz · offset={offset} Hz · ISF {noise}={calculated} / VCO 总仿真={reference} dBc/Hz".format(
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
            quality = "工况参数：" + _display(operating["parameters"]) + "\n" + quality
        quality += " 双器件 flicker 模型：" + _display(sample.get("pair_model"))
        if generated:
            single = _display(sample.get("dynamic_dBc_Hz"))
            reference = _display(sample.get("spectre_fn_dBc_Hz"))
            self.metrics_var.set(self.metrics_var.get() + f"\n单管 fn · ISF {single} / HBnoise {reference} dBc/Hz；双器件值按独立对称假设加 3.01 dB。")
        if self.noise_kind_var.get() != "flicker" and _mapping(sample.get("thermal_noise")).get("status") != "available":
            quality += " 当前结果未包含可用的独立 thermal 计算，thermal 与两者合计均显示 N/A；可选择 flicker 查看已有计算。"
        elif self.noise_kind_var.get() != "flicker":
            limit = _mapping(sample.get("thermal_noise")).get("input_max_harmonic")
            quality += (f" Thermal 独立输入频带：|m| ≤ {limit}（有限带预算，更高频带不计）。"
                        if type(limit) is int and limit >= 0 else " Thermal 旧预算：实际频带范围未记录，不按当前表单阶数推测。")
        if self.current_result.get("offset_sweep") and not self.current_result.get("offset_sweep", {}).get("complete", True):
            quality += " 当前仅展示已完成部分。"
        self.result_quality_var.set(quality)
        self._render_noise_curves()

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
            messagebox.showwarning("没有结果", "请先完成任务或导入 canonical replay。", parent=self.root)
            return
        result = deep_merge({}, self.current_result)
        owner = self.current_result_job_id
        path = filedialog.asksaveasfilename(parent=self.root, title="导出结果 JSON", defaultextension=".json", filetypes=(("JSON", "*.json"),))
        if path:
            self._submit(
                "导出结果 JSON",
                lambda: _write_json(path, result),
                lambda _value: messagebox.showinfo("导出完成", f"{path}\n任务：{owner}", parent=self.root),
            )

    def _export_summary_csv(self) -> None:
        if self.current_result is None or self.current_result_job_id != self.current_job_id:
            messagebox.showwarning("没有结果", "请先完成任务或导入 canonical replay。", parent=self.root)
            return
        rows = [deep_merge({}, item) for item in _sequence(self.current_result.get("summary")) if isinstance(item, Mapping)]
        path = filedialog.asksaveasfilename(parent=self.root, title="导出 Summary CSV", defaultextension=".csv", filetypes=(("CSV", "*.csv"),))
        if path:
            self._submit("导出 Summary CSV", lambda: _write_csv(path, rows), lambda _value: messagebox.showinfo("导出完成", path, parent=self.root))

    def _export_harmonics_csv(self) -> None:
        if self.current_result is None or self.current_result_job_id != self.current_job_id:
            messagebox.showwarning("没有结果", "请先完成任务或导入 canonical replay。", parent=self.root)
            return
        point = str(self.point_var.get() or "")
        point_data = _mapping(_mapping(self.current_result.get("points")).get(point))
        rows = [deep_merge({}, item) for item in _sequence(point_data.get("harmonics")) if isinstance(item, Mapping)]
        path = filedialog.asksaveasfilename(parent=self.root, title="导出 Harmonics CSV", defaultextension=".csv", filetypes=(("CSV", "*.csv"),))
        if path:
            self._submit("导出 Harmonics CSV", lambda: _write_csv(path, rows), lambda _value: messagebox.showinfo("导出完成", path, parent=self.root))

    # ---- error catalog and close guard ------------------------------

    def _build_error_tab(self) -> None:
        content = self._scrollable_page(self.error_tab)
        pane = ttk.Panedwindow(content, orient="horizontal")
        pane.pack(fill="both", expand=True)
        list_frame = ttk.Frame(pane)
        detail_frame = ttk.LabelFrame(pane, text="排障说明", padding=7)
        pane.add(list_frame, weight=2)
        pane.add(detail_frame, weight=3)
        self.error_tree = self._tree(
            list_frame,
            (("code", "代码", 95), ("severity", "级别", 65), ("title", "标题", 240)),
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
        for label, key in (("可能原因", "causes"), ("建议操作", "actions")):
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
                "任务命令正在提交",
                "窗口不会关闭。运行/恢复命令尚未完成提交，请等待任务状态可见后再关闭。",
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
                    "任务命令正在提交",
                    "窗口不会关闭。关闭检查期间出现了新的运行/恢复命令，请等待任务状态可见。",
                    parent=self.root,
                )
                return
            running = active_jobs(_mapping(value))
            if running:
                description = "\n".join(f"• {item.get('id')} ({item.get('status')})" for item in running[:8])
                messagebox.showwarning(
                    "仍有后台任务",
                    "窗口不会关闭。当前没有安全停止 API，本 UI 不会假称这些任务已停止。\n\n"
                    + description
                    + "\n\n请等待任务进入 succeeded / failed / interrupted 后再关闭。",
                    parent=self.root,
                )
                return
            self.queue.close()
            self.root.destroy()

        def failed(exc: Exception) -> None:
            self._close_check_pending = False
            messagebox.showerror(
                "无法确认后台任务状态",
                _error_message(exc) + "\n\n为避免误判，窗口保持打开。",
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
            "无法启动 Tk 窗口（未检测到可用图形 DISPLAY）：%s\n"
            "可显式使用兼容 Web UI：vco-isf-web"
            "（源码树也可用 python run.py --web）" % exc,
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
        print("Tk UI 初始化失败：%s" % exc, file=sys.stderr)
        return 2
    root.mainloop()
    return 0


__all__ = ["DEFAULT_WORKSPACE", "IsfApplication", "active_jobs", "build_parser", "main"]
