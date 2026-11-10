"""Small dependency-free Tk Canvas charts for ISF and cumulative Qk data."""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from collections.abc import Callable, Mapping, Sequence
from typing import Any


Bounds = tuple[float, float, float, float]
Point = tuple[float, float]

DEFAULT_CHART_PALETTE = {
    "background": "#FFFFFF",
    "text": "#263638",
    "muted": "#667477",
    "grid": "#EDF0F0",
    "border": "#AEB9BB",
    "accent": "#176C68",
    "accent_alt": "#DC7140",
    "selection": "#111111",
    "series": ("#176C68", "#3D6F91", "#7660A5", "#DC7140", "#B97A21"),
}


def _blend(foreground: str, background: str, amount: float) -> str:
    """Mix two ``#RRGGBB`` colours; Tk canvases have no alpha, so dimming is a blend."""
    try:
        fg = [int(foreground[i:i + 2], 16) for i in (1, 3, 5)]
        bg = [int(background[i:i + 2], 16) for i in (1, 3, 5)]
    except (ValueError, TypeError, IndexError):
        return foreground
    return "#%02X%02X%02X" % tuple(int(f + (b - f) * amount) for f, b in zip(fg, bg))


def _finite(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _point(value: Any) -> Point | None:
    if isinstance(value, Mapping):
        x = _finite(value.get("x"))
        y = _finite(value.get("y", value.get("value")))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 2:
        x = _finite(value[0])
        y = _finite(value[1])
    else:
        return None
    return (x, y) if x is not None and y is not None else None


def line_segments(values: Sequence[Any] | None) -> list[list[Point]]:
    """Split a line at missing/non-finite samples instead of drawing zeros."""

    segments: list[list[Point]] = []
    current: list[Point] = []
    for value in values or ():
        point = _point(value)
        if point is None:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(point)
    if current:
        segments.append(current)
    return segments


def chart_series(series: Sequence[Mapping[str, Any]] | None, log_x: bool = False) -> list[dict[str, Any]]:
    """Transform only display coordinates, retaining every missing-data break."""
    output = []
    for item in series or ():
        row = dict(item)
        points = []
        for value in item.get("points", ()):
            point = _point(value)
            if point is None or (log_x and point[0] <= 0):
                points.append(None)
            else:
                points.append((math.log10(point[0]) if log_x else point[0], point[1]))
        row["points"] = points
        output.append(row)
    return output


def series_bounds(series: Sequence[Mapping[str, Any]] | None) -> Bounds | None:
    """Return finite data bounds across a line-series collection."""

    points: list[Point] = []
    for item in series or ():
        if not isinstance(item, Mapping):
            continue
        for segment in line_segments(item.get("points", ())):
            points.extend(segment)
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), max(xs), min(ys), max(ys)


def padded_bounds(bounds: Bounds, ratio: float = 0.08) -> Bounds:
    """Add stable padding and expand zero-span axes."""

    x_min, x_max, y_min, y_max = bounds
    x_span = x_max - x_min
    y_span = y_max - y_min
    if x_span == 0:
        x_span = max(abs(x_min), 1.0)
        x_min -= x_span * 0.5
        x_max += x_span * 0.5
    if y_span == 0:
        y_span = max(abs(y_min), 1.0)
        y_min -= y_span * 0.5
        y_max += y_span * 0.5
    return (
        x_min - (x_max - x_min) * ratio,
        x_max + (x_max - x_min) * ratio,
        y_min - (y_max - y_min) * ratio,
        y_max + (y_max - y_min) * ratio,
    )


def project_point(point: Point, bounds: Bounds, rectangle: tuple[float, float, float, float]) -> Point:
    """Project one data point into a Canvas rectangle."""

    x_min, x_max, y_min, y_max = bounds
    left, top, right, bottom = rectangle
    x_span = x_max - x_min or 1.0
    y_span = y_max - y_min or 1.0
    x = left + (point[0] - x_min) * (right - left) / x_span
    y = bottom - (point[1] - y_min) * (bottom - top) / y_span
    return x, y


def phasor_extent(points: Sequence[Mapping[str, Any]] | None) -> float:
    """Return a non-zero symmetric extent for cumulative complex points."""

    maximum = 0.0
    for item in points or ():
        if not isinstance(item, Mapping):
            continue
        real = _finite(item.get("real", item.get("re")))
        imag = _finite(item.get("imag", item.get("im")))
        if real is not None and imag is not None:
            maximum = max(maximum, abs(real), abs(imag))
    return (maximum if maximum > 0 else 1.0) * 1.15


def _tick(value: float) -> str:
    absolute = abs(value)
    if absolute and (absolute < 1e-3 or absolute >= 1e4):
        return f"{value:.2e}"
    return f"{value:.3g}"


def _frequency_tick(hz: float) -> str:
    for scale, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if hz >= scale:
            return f"{hz / scale:.3g}{suffix}"
    return _tick(hz)


def contribution_pies(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Comparable source-only pies; overlapping summary rows are never slices.

    dBc/Hz is converted to linear carrier-relative power, not added as dB.
    A missing prediction is not a zero-power slice. Duplicate identities make
    both pies unavailable because their ownership cannot safely be inferred.
    """
    sources = [dict(row) for row in rows if row.get("kind") == "source"]
    identities = [(str(row.get("device") or ""), str(row.get("mechanism") or "")) for row in sources]
    result: dict[str, Any] = {"simulated": [], "calculated": [], "missing_simulated": [],
                              "missing_calculated": [], "note": ""}
    if any(not all(identity) for identity in identities) or len(set(identities)) != len(identities):
        result["note"] = "N/A：器件 / 噪声机制身份缺失或重复，未绘制可能重复计数的饼图。"
        return result
    sources.sort(key=lambda row: (str(row.get("device")), str(row.get("mechanism"))))
    for index, row in enumerate(sources):
        label = str(row.get("label") or f"{row['device']} · {row['mechanism']}")
        for key in ("simulated", "calculated"):
            value = _finite(row.get(key + "_dbc"))
            psd = _finite(row.get("psd_V2_Hz" if key == "simulated" else "calculated_psd_V2_Hz"))
            if value is None and psd == 0:
                result[key].append({"id": label, "label": label, "power": 0., "dbc": None, "color_index": index, "psd": psd})
                continue
            if value is None:
                result["missing_" + key].append(label)
                continue
            try:
                power = 10 ** (value / 10)
            except OverflowError:
                power = float("inf")
            if not math.isfinite(power) or power <= 0:
                result["missing_" + key].append(label)
                continue
            result[key].append({"id": label, "label": label, "power": power, "dbc": value, "color_index": index, "psd": psd})
    simulated = math.fsum(row["power"] for row in result["simulated"])
    modeled = {row["id"] for row in result["calculated"]}
    covered = math.fsum(row["power"] for row in result["simulated"] if row["id"] in modeled)
    result["reference_coverage_pct"] = 100 * covered / simulated if simulated else None
    result["note"] = ("两图分别按已展示的独立源线性功率归一化；颜色对应相同器件 / 机制，面积不是绝对相噪。"
                      "计算图仅含已建模且有数据的源，缺失项不视为零；汇总行不重复计入。")
    if result["reference_coverage_pct"] is not None:
        result["note"] += f" 已建模源覆盖所选仿真明细功率 {result['reference_coverage_pct']:.3g}%。"
    for key, label in (("missing_calculated", "计算 N/A"), ("missing_simulated", "仿真 N/A")):
        if result[key]:
            result["note"] += f" {label}：" + "、".join(result[key]) + "。"
    return result


def contribution_bars(rows: Sequence[Mapping[str, Any]], limit: int = 8) -> dict[str, Any]:
    """Align leading source contributions on the same whole-VCO PSD scale."""
    data = contribution_pies(rows)
    reference = next((row for row in rows if row.get("kind") == "aggregate"
                      and row.get("label") == "VCO 总仿真相噪"), {})
    reference_psd = _finite(reference.get("psd_V2_Hz"))
    reference_db = _finite(reference.get("simulated_dbc"))
    by_source: dict[str, float] = {}
    for key in ("simulated", "calculated"):
        for row in data[key]:
            if reference_psd and reference_psd > 0 and row["psd"] is not None and row["psd"] >= 0:
                fraction = 100 * (row["psd"] / reference_psd)
            elif reference_db is not None:
                try:
                    fraction = 0. if row["power"] == 0 else 100 * 10 ** ((row["dbc"] - reference_db) / 10)
                except OverflowError:
                    fraction = None
            else:
                fraction = None
            row["percent"] = _finite(fraction)
            by_source[row["id"]] = max(by_source.get(row["id"], 0.), row["percent"] or 0.)
    data["order"] = sorted(by_source, key=lambda name: (-by_source[name], name))[:limit]
    data["axis_max"] = max(100., *(by_source[name] for name in data["order"])) if data["order"] else 100.
    if not by_source:
        return data
    data["note"] = "双图共用 VCO 总仿真 PSD 作分母。N/A 表示缺测或未建模。"
    if not any(row["percent"] is not None for key in ("simulated", "calculated") for row in data[key]):
        data["note"] += " 当前 History 缺少可用的总仿真 PSD，无法绘制占比。"
    if len(by_source) > len(data["order"]):
        data["note"] += f" 图中显示占比最高的 {len(data['order'])} 项，全部 {len(by_source)} 项见表格。"
    if data["reference_coverage_pct"] is not None:
        data["note"] += f" 已建模源覆盖所选仿真明细功率 {data['reference_coverage_pct']:.3g}%。"
    return data


class ContributionBars(tk.Canvas):
    """One panel of an aligned pair of source contribution charts."""

    def __init__(self, master: tk.Misc, *, title: str, on_select: Callable[[str], None], **kwargs: Any) -> None:
        kwargs.setdefault("height", 244)
        kwargs.setdefault("width", 320)
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(master, **kwargs)
        self.title = title
        self._on_select = on_select
        self._slices: list[dict[str, Any]] = []
        self._order: Sequence[str] = ()
        self._axis_max = 100.
        self._palette = dict(DEFAULT_CHART_PALETTE)
        self._font = tkfont.nametofont("TkDefaultFont", root=self).copy()
        self._font.configure(size=9)
        self.bind("<Configure>", self._redraw, add="+")
        self.bind("<Leave>", lambda _event: self.delete("tooltip"), add="+")

    def set_palette(self, palette: Mapping[str, Any]) -> None:
        self._palette.update(palette)
        self.configure(background=self._palette["background"])
        self._redraw()

    def set_slices(self, slices: Sequence[Mapping[str, Any]], order: Sequence[str], axis_max: float) -> None:
        self._slices = [dict(row) for row in slices]
        self._order = tuple(order)
        self._axis_max = axis_max
        self._redraw()

    def clear(self) -> None:
        self.set_slices([], (), 100.)

    def _show_details(self, row: Mapping[str, Any]) -> None:
        self.delete("tooltip")
        value = row.get("percent")
        description = f"{value:.5g}% 总仿真 PSD" if value is not None else "N/A"
        if row.get("dbc") is not None:
            description += f"   {row['dbc']:.6g} dBc/Hz"
        label = self.create_text(9, 21, text=description, anchor="w", font=self._font,
                                 fill=self._palette["text"], tags="tooltip")
        box = self.bbox(label)
        if box:
            background = self.create_rectangle(box[0] - 4, box[1] - 3, box[2] + 4, box[3] + 3,
                                               fill=self._palette["background"], outline=self._palette["border"],
                                               tags="tooltip")
            self.tag_lower(background, label)

    def _redraw(self, _event: Any = None) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width < 100 or height < 65:
            return
        color = self._palette
        self.create_text(9, 17, text=self.title, anchor="w", font=self._font, fill=color["text"])
        if not self._order:
            self.create_text(width / 2, height / 2, text="暂无独立源数据", font=self._font, fill=color["muted"])
            return
        left, right = max(60, width * .40), width - 52
        span = max(1, right - left)
        for tick in (0., .5, 1.):
            x = left + span * tick
            self.create_line(x, 31, x, height - 24, fill=color["grid"])
            self.create_text(x, height - 12, text=f"{tick * self._axis_max:.3g}%",
                             font=self._font, fill=color["muted"])
        available = left - 19
        row_height = min(26, (height - 59) / len(self._order))
        source = {row["id"]: row for row in self._slices}
        for index, identifier in enumerate(self._order):
            row = source.get(identifier, {})
            value = row.get("percent")
            y = 37 + (index + .5) * row_height
            label = identifier
            while label and self._font.measure(label + "…") > available:
                label = label[:-1]
            if label != identifier:
                label += "…"
            tag = f"source-{index}"
            self.create_text(9, y, text=label, anchor="w", font=self._font, fill=color["text"], tags=tag)
            if value is not None and value > 0:
                bar_color = color["series"][int(row["color_index"]) % len(color["series"])]
                self.create_rectangle(left, y - 4, left + span * value / self._axis_max, y + 4,
                                      fill=bar_color, outline="", tags=tag)
            self.create_text(right + 6, y, text=f"{value:.3g}%" if value is not None else "N/A",
                             anchor="w", font=self._font, fill=color["muted"], tags=tag)
            self.tag_bind(tag, "<Enter>", lambda _event, item=row: self._show_details(item))
            self.tag_bind(tag, "<Leave>", lambda _event: self.delete("tooltip"))
            self.tag_bind(tag, "<Button-1>", lambda _event, name=identifier: self._on_select(name))


def _clip_x(points: Sequence[Point], lower: float, upper: float) -> list[list[Point]]:
    """Clip visible line strokes at the viewport edge without adding data samples."""
    if all(lower <= x <= upper for x, _ in points):
        return [list(points)]
    paths: list[list[Point]] = []
    for first, second in zip(points, points[1:]):
        x, y = first
        dx, dy = second[0] - x, second[1] - y
        if dx == 0.:
            if lower <= x <= upper:
                paths.append([first, second])
            continue
        enter, exit_ = sorted(((lower - x) / dx, (upper - x) / dx))
        start, stop = max(0., enter), min(1., exit_)
        if start < stop:
            paths.append([(x + start * dx, y + start * dy), (x + stop * dx, y + stop * dy)])
    return paths


class LineChart(tk.Canvas):
    """Responsive multi-series line chart using only ``tkinter.Canvas``."""

    DEFAULT_COLORS = DEFAULT_CHART_PALETTE["series"]

    def __init__(self, master: tk.Misc, *, empty_text: str = "N/A", **kwargs: Any) -> None:
        self._palette = dict(DEFAULT_CHART_PALETTE)
        kwargs.setdefault("background", self._palette["background"])
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(master, **kwargs)
        self._tick_font = tkfont.nametofont("TkDefaultFont", root=self).copy()
        self._tick_font.configure(size=10)
        self._axis_font = self._tick_font.copy()
        self._axis_font.configure(size=11)
        self._series: list[dict[str, Any]] = []
        self._empty_text = empty_text
        self._redraw_pending = False
        self._redraw_id: str | None = None
        self._log_x = False
        self._x_label = ""
        self._y_label = ""
        self._x_limits: tuple[float, float] | None = None
        self._include_zero = False
        self._hidden: set[str] = set()
        self._plot: tuple[float, float, float, float] | None = None
        self._x_navigate: Callable[[float, float], None] | None = None
        self._drag_x: float | None = None
        self.selected_series_id: str | None = None
        self._hit_samples: list[dict[str, Any]] = []
        self.hover_sample: dict[str, Any] | None = None
        self._series_selector = ttk.Combobox(self, state="readonly", font=self._tick_font)
        self._series_selector.bind("<<ComboboxSelected>>", self._select_from_list)
        self._selector_ids: list[str | None] = []
        self.bind("<Configure>", self._schedule_redraw, add="+")
        self.bind("<Destroy>", self._cancel_redraw, add="+")
        self.bind("<Motion>", self._hover, add="+")
        self.bind("<Leave>", self._clear_hover, add="+")
        self.bind("<Escape>", lambda _event: self.select_series(None), add="+")

    def enable_x_navigation(self, callback: Callable[[float, float], None]) -> None:
        self._x_navigate = callback
        for sequence in ("<Control-MouseWheel>", "<Control-Button-4>", "<Control-Button-5>"):
            self.bind(sequence, self._zoom, add="+")
        self.bind("<ButtonPress-2>", self._pan_start, add="+")
        self.bind("<B2-Motion>", self._pan, add="+")
        self.bind("<ButtonRelease-2>", self._pan_end, add="+")

    def set_x_limits(self, limits: tuple[float, float] | None) -> None:
        self._x_limits = limits
        self._schedule_redraw()

    def set_hidden(self, series_ids: Sequence[str]) -> None:
        self._hidden = set(series_ids)
        if self.selected_series_id in self._hidden:
            self.selected_series_id = None
        self._schedule_redraw()

    def _plot_fraction(self, x: float) -> float | None:
        if self._plot is None:
            return None
        left, _top, right, _bottom = self._plot
        return (x - left) / (right - left) if left <= x <= right else None

    def _zoom(self, event: Any) -> str:
        anchor = self._plot_fraction(event.x)
        if self._x_navigate is not None and anchor is not None:
            zoom_in = getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0
            self._x_navigate(.72 if zoom_in else 1 / .72, anchor)
        return "break"

    def _pan_start(self, event: Any) -> None:
        if self._plot_fraction(event.x) is not None:
            self._drag_x = event.x
            self.configure(cursor="fleur")

    def _pan(self, event: Any) -> None:
        if self._drag_x is not None and self._plot is not None and self._x_navigate is not None:
            self._x_navigate(1., (self._drag_x - event.x) / (self._plot[2] - self._plot[0]))
            self._drag_x = event.x

    def _pan_end(self, _event: Any) -> None:
        self._drag_x = None
        self.configure(cursor="")

    def set_palette(self, palette: Mapping[str, Any]) -> None:
        for key in ("background", "text", "muted", "grid", "border", "accent", "accent_alt", "selection"):
            if palette.get(key):
                self._palette[key] = str(palette[key])
        configured_series = palette.get("series")
        if isinstance(configured_series, Sequence) and not isinstance(configured_series, (str, bytes)):
            colors = tuple(str(value) for value in configured_series if value)
        else:
            colors = (
                self._palette["accent"],
                "#6EA8FE",
                self._palette["accent_alt"],
                "#A78BFA",
                "#EF6C9E",
            )
        if colors:
            self._palette["series"] = colors
        for index, item in enumerate(self._series):
            if item.get("uses_default_color"):
                item["color"] = self._palette["series"][index % len(self._palette["series"])]
        self.configure(background=self._palette["background"])
        self._schedule_redraw()

    def set_series(self, series: Sequence[Mapping[str, Any]] | None) -> None:
        self._series = []
        for index, raw in enumerate(series or ()):
            if not isinstance(raw, Mapping):
                continue
            self._series.append(
                {
                    **dict(raw),
                    "id": str(raw.get("id") or f"series-{index}"),
                    "name": str(raw.get("name") or f"Series {index + 1}"),
                    "color": str(
                        raw.get("color")
                        or self._palette["series"][index % len(self._palette["series"])]
                    ),
                    "uses_default_color": not bool(raw.get("color")),
                    "dash": bool(raw.get("dash", False)),
                    "points": list(raw.get("points", ())),
                    "samples": list(raw.get("samples", ())),
                }
            )
        if self.selected_series_id not in {item["id"] for item in self._series}:
            self.selected_series_id = None
        self._schedule_redraw()

    def select_series(self, series_id: str | None) -> None:
        """Highlight one curve without removing or changing any samples."""
        if series_id is not None and series_id not in {item["id"] for item in self._series}:
            return
        self.selected_series_id = series_id
        self._schedule_redraw()

    def _toggle_series(self, series_id: str) -> None:
        self.select_series(None if self.selected_series_id == series_id else series_id)

    def _select_from_list(self, _event: Any = None) -> None:
        index = self._series_selector.current()
        if 0 <= index < len(self._selector_ids):
            self.select_series(self._selector_ids[index])

    def _clear_hover(self, _event: Any = None) -> None:
        self.delete("hover")
        self.hover_sample = None

    def _hover(self, event: Any) -> None:
        self._clear_hover()
        if not self._hit_samples:
            return
        candidates = self._hit_samples
        if self.selected_series_id is not None:
            candidates = [row for row in candidates if row["series_id"] == self.selected_series_id]
        if not candidates:
            return
        nearest = min(candidates, key=lambda row: (row["canvas_x"] - event.x) ** 2 + (row["canvas_y"] - event.y) ** 2)
        if math.hypot(nearest["canvas_x"] - event.x, nearest["canvas_y"] - event.y) > 18:
            return
        self.hover_sample = nearest
        raw_x, raw_y = nearest["point"]
        x_text = f"{raw_x:.10g} Hz" if "Hz" in self._x_label else f"x={raw_x:.10g}"
        if "dBc" in self._y_label:
            y_text = f"{raw_y:.10g} dBc/Hz"
        elif "dB" in self._y_label:
            y_text = f"{raw_y:+.7g} dB"
        else:
            y_text = f"y={raw_y:.10g}"
        text = nearest["name"] + "\n" + x_text + " · " + y_text
        x, y = nearest["canvas_x"], nearest["canvas_y"]
        self.create_oval(x - 5, y - 5, x + 5, y + 5, outline=self._palette["text"], width=2, tags="hover")
        label = self.create_text(event.x + 13, event.y - 13, text=text, anchor="sw", fill=self._palette["text"], font="TkDefaultFont", tags="hover")
        box = self.bbox(label)
        if box is None:
            return
        dx = min(0, self.winfo_width() - box[2] - 8) + max(0, 8 - box[0])
        dy = max(0, 8 - box[1])
        self.move(label, dx, dy)
        box = self.bbox(label)
        background = self.create_rectangle(box[0] - 6, box[1] - 4, box[2] + 6, box[3] + 4,
                                           fill=self._palette["background"], outline=self._palette["border"], tags="hover")
        self.tag_lower(background, label)

    def set_axes(self, *, x_label: str = "", y_label: str = "", log_x: bool = False, include_zero: bool = False) -> None:
        self._x_label, self._y_label, self._log_x = x_label, y_label, bool(log_x)
        self._include_zero = include_zero
        self._schedule_redraw()

    def clear(self, message: str | None = None) -> None:
        self._series = []
        self.selected_series_id = None
        self._hit_samples = []
        self._clear_hover()
        if message is not None:
            self._empty_text = str(message)
        self._schedule_redraw()

    def _schedule_redraw(self, _event: tk.Event[Any] | None = None) -> None:
        if self._redraw_pending:
            return
        self._redraw_pending = True
        self._redraw_id = self.after_idle(self._redraw)

    def _cancel_redraw(self, _event: Any = None) -> None:
        if self._redraw_id is not None:
            self.after_cancel(self._redraw_id)
            self._redraw_id = None
        self._redraw_pending = False

    def _redraw(self) -> None:
        self._redraw_pending = False
        self._redraw_id = None
        self.delete("all")
        self._hit_samples = []
        self._selector_ids = []
        self.hover_sample = None
        self._plot = None
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        display_series = chart_series(self._series, self._log_x)
        bounds = series_bounds([item for item in display_series if item["id"] not in self._hidden])
        if bounds is None:
            self.create_text(width / 2, height / 2, text=self._empty_text, fill=self._palette["muted"])
            return
        if self._include_zero:
            bounds = bounds[:2] + (min(0., bounds[2]), max(0., bounds[3]))
        bounds = padded_bounds(bounds)
        if self._x_limits is not None:
            bounds = self._x_limits + bounds[2:]
        left, right, bottom = 88.0, width - 22.0, height - (49.0 if self._x_label else 38.0)
        legend_x, legend_y = left, 17.0
        # The legend must never consume the plot. Keep two rows at most and
        # expose every remaining curve through a list, without hiding its data.
        max_legend_rows = 2 if bottom >= 184 else 1
        legend_row, visible_count = 0, 0
        legend_order = sorted(enumerate(display_series), key=lambda row: row[1]["id"] != self.selected_series_id)
        for index, item in legend_order:
            valid = [point for point in item["points"] if point is not None]
            selected = item["id"] == self.selected_series_id
            label_text = ("● " if selected else "") + item["name"] + (" = " + _tick(valid[0][1]) if len(valid) == 1 else "")
            label_budget = max(20, right - left - 43)
            if self._tick_font.measure(label_text) > label_budget:
                while label_text and self._tick_font.measure(label_text + "…") > label_budget:
                    label_text = label_text[:-1]
                label_text += "…"
            estimated = 43 + self._tick_font.measure(label_text)
            if legend_x > left and legend_x + estimated > right:
                legend_row += 1
                if legend_row >= max_legend_rows:
                    break
                legend_x, legend_y = left, legend_y + 29
            tag = f"legend-{index}"
            stroke = self._palette["muted"] if item["id"] in self._hidden else item["color"]
            self.create_line(legend_x, legend_y, legend_x + 18, legend_y, fill=stroke, width=4 if selected else 2, dash=(5, 3) if item["dash"] else (), tags=tag)
            label = self.create_text(legend_x + 23, legend_y, text=label_text, anchor="w", fill=self._palette["text"], font=self._tick_font, tags=tag)
            self.tag_bind(tag, "<Button-1>", lambda _event, sid=item["id"]: self._toggle_series(sid))
            box = self.bbox(label)
            legend_x = box[2] + 18 if box else legend_x + estimated
            visible_count += 1
        if visible_count < len(display_series):
            legend_y += 29
            self._selector_ids = [None] + [item["id"] for item in display_series]
            self._series_selector.configure(values=[f"全部 {len(display_series)} 条（另 {len(display_series) - visible_count} 条图例已折叠）"] + [item["name"] for item in display_series])
            self._series_selector.current(self._selector_ids.index(self.selected_series_id))
            self.create_text(left, legend_y, text="选择曲线：", anchor="w", fill=self._palette["text"], font=self._tick_font, tags="legend-overflow")
            self.create_window(left + 90, legend_y, window=self._series_selector, anchor="w", width=max(50, right - left - 90), tags="legend-overflow")
        else:
            self._selector_ids = []
        top = legend_y + 29
        if right <= left or bottom <= top:
            return
        rectangle = (left, top, right, bottom)
        self._plot = rectangle
        self.create_rectangle(left, top, right, bottom, outline=self._palette["border"], width=1, tags="plot-frame")
        x_ticks = range(math.ceil(bounds[0]), math.floor(bounds[1]) + 1) if self._log_x else ()
        if not 2 <= len(x_ticks) <= 7:
            x_ticks = [bounds[0] + index / 5 * (bounds[1] - bounds[0]) for index in range(6)]
        for value in x_ticks:
            x, _ = project_point((value, bounds[2]), bounds, rectangle)
            self.create_line(x, top, x, bottom, fill=self._palette["grid"])
            label = _frequency_tick(10 ** value) if self._log_x and "Hz" in self._x_label else _tick(value)
            self.create_text(x, bottom + 15, text=label, fill=self._palette["muted"], font=self._tick_font)
        tick_count = 6 if bottom - top > 125 else 4
        for index in range(tick_count):
            ratio = index / (tick_count - 1)
            y = top + ratio * (bottom - top)
            value = bounds[3] - ratio * (bounds[3] - bounds[2])
            self.create_line(left, y, right, y, fill=self._palette["grid"])
            self.create_text(left - 8, y, text=_tick(value), anchor="e", fill=self._palette["muted"], font=self._tick_font)
        if self._x_label:
            self.create_text((left + right) / 2, height - 14, text=self._x_label + ("（log）" if self._log_x else ""), fill=self._palette["text"], font=self._axis_font)
        if self._y_label:
            self.create_text(14, (top + bottom) / 2, text=self._y_label, angle=90, fill=self._palette["text"], font=self._axis_font)
        if self._include_zero:
            _, y_zero = project_point((bounds[0], 0.), bounds, rectangle)
            self.create_line(left, y_zero, right, y_zero, fill=self._palette["muted"], dash=(4, 4))
        # Selected curves draw last so that coincident curves remain inspectable.
        ordered = sorted(enumerate(display_series), key=lambda row: row[1]["id"] == self.selected_series_id)
        for index, item in ordered:
            if item["id"] in self._hidden:
                continue
            tag = f"curve-{index}"
            selected = item["id"] == self.selected_series_id
            stroke = 3.5 if selected else (1.4 if self.selected_series_id else 2)
            # Selected curve: product accent, thick.  Others fade towards the plot background.
            if selected:
                curve_color = self._palette["accent"]
            elif self.selected_series_id:
                curve_color = _blend(item["color"], self._palette["background"], 0.6)
            else:
                curve_color = item["color"]
            for segment in line_segments(item["points"]):
                for path in _clip_x(segment, bounds[0], bounds[1]):
                    if len(path) >= 2:
                        coordinates = [coordinate for point in path for coordinate in project_point(point, bounds, rectangle)]
                        self.create_line(*coordinates, fill=curve_color, width=stroke,
                                         dash=(5, 3) if item["dash"] else (), tags=tag)
                for point in segment:
                    if bounds[0] <= point[0] <= bounds[1]:
                        x, y = project_point(point, bounds, rectangle)
                        self.create_oval(x - 3, y - 3, x + 3, y + 3, fill=curve_color, outline="", tags=tag)
            self.tag_bind(tag, "<Button-1>", lambda _event, sid=item["id"]: self._toggle_series(sid))
            raw = self._series[index]
            for sample_index, point in enumerate(item["points"]):
                if point is None or not bounds[0] <= point[0] <= bounds[1]:
                    continue
                canvas_x, canvas_y = project_point(point, bounds, rectangle)
                self._hit_samples.append({
                    "series_id": item["id"], "name": item["name"],
                    "point": _point(raw["points"][sample_index]),
                    "sample": raw["samples"][sample_index] if sample_index < len(raw["samples"]) else None,
                    "canvas_x": canvas_x, "canvas_y": canvas_y,
                })


class PhasorChart(tk.Canvas):
    """Equal-scale cumulative Qk complex-plane chart."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        on_select: Callable[[Any], None] | None = None,
        empty_text: str = "N/A",
        **kwargs: Any,
    ) -> None:
        self._palette = dict(DEFAULT_CHART_PALETTE)
        kwargs.setdefault("background", self._palette["background"])
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(master, **kwargs)
        self._points: list[dict[str, Any]] = []
        self._selected: str | None = None
        self._on_select = on_select
        self._empty_text = empty_text
        self._redraw_pending = False
        self._redraw_id: str | None = None
        self.bind("<Configure>", self._schedule_redraw, add="+")
        self.bind("<Destroy>", self._cancel_redraw, add="+")

    def set_palette(self, palette: Mapping[str, Any]) -> None:
        for key in ("background", "text", "muted", "grid", "border", "accent", "accent_alt", "selection"):
            if palette.get(key):
                self._palette[key] = str(palette[key])
        self.configure(background=self._palette["background"])
        self._schedule_redraw()

    def set_points(self, points: Sequence[Mapping[str, Any]] | None) -> None:
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(points or ()):
            if not isinstance(raw, Mapping):
                continue
            real = _finite(raw.get("real", raw.get("re")))
            imag = _finite(raw.get("imag", raw.get("im")))
            if real is None or imag is None:
                continue
            normalized.append({"k": raw.get("k", index), "real": real, "imag": imag})
        self._points = normalized
        if self._selected is not None and not any(str(item["k"]) == self._selected for item in normalized):
            self._selected = None
        self._schedule_redraw()

    def highlight(self, harmonic: Any | None) -> None:
        self._selected = None if harmonic is None else str(harmonic)
        self._schedule_redraw()

    def clear(self, message: str | None = None) -> None:
        self._points = []
        self._selected = None
        if message is not None:
            self._empty_text = str(message)
        self._schedule_redraw()

    def _schedule_redraw(self, _event: tk.Event[Any] | None = None) -> None:
        if self._redraw_pending:
            return
        self._redraw_pending = True
        self._redraw_id = self.after_idle(self._redraw)

    def _cancel_redraw(self, _event: Any = None) -> None:
        if self._redraw_id is not None:
            self.after_cancel(self._redraw_id)
            self._redraw_id = None
        self._redraw_pending = False

    def _select(self, harmonic: Any) -> None:
        self._selected = str(harmonic)
        self._schedule_redraw()
        if self._on_select is not None:
            self._on_select(harmonic)

    def _redraw(self) -> None:
        self._redraw_pending = False
        self._redraw_id = None
        self.delete("all")
        width = max(self.winfo_width(), int(float(self.cget("width") or 1)), 120)
        height = max(self.winfo_height(), int(float(self.cget("height") or 1)), 100)
        if not self._points:
            self.create_text(width / 2, height / 2, text=self._empty_text, fill=self._palette["muted"])
            return
        left, top, right, bottom = 50.0, 24.0, width - 30.0, height - 38.0
        cx = (left + right) / 2.0
        cy = (top + bottom) / 2.0
        extent = phasor_extent(self._points)
        scale = min(right - left, bottom - top) / (2.0 * extent)
        for ratio in (0.25, 0.5, 0.75, 1.0):
            radius = extent * ratio * scale
            self.create_oval(cx - radius, cy - radius, cx + radius, cy + radius, outline=self._palette["grid"])
        self.create_line(left, cy, right, cy, fill=self._palette["border"])
        self.create_line(cx, top, cx, bottom, fill=self._palette["border"])
        self.create_text(right, cy - 10, text="Re{ΣQk}", anchor="e", fill=self._palette["muted"])
        self.create_text(cx + 8, top, text="Im{ΣQk}", anchor="nw", fill=self._palette["muted"])

        projected = [(cx + item["real"] * scale, cy - item["imag"] * scale) for item in self._points]
        if len(projected) > 1:
            self.create_line(*[coordinate for point in projected for coordinate in point], fill=self._palette["accent"], width=2)
        for index, (item, (x, y)) in enumerate(zip(self._points, projected)):
            selected = str(item["k"]) == self._selected
            final = index == len(self._points) - 1
            radius = 6 if selected else (5 if final else 3.5)
            marker = self.create_oval(
                x - radius,
                y - radius,
                x + radius,
                y + radius,
                fill=self._palette["accent_alt"] if final else self._palette["accent"],
                outline=self._palette["selection"] if selected else "",
                width=2 if selected else 1,
            )
            self.create_text(
                x + 7,
                y - 8,
                text=f"k{item['k']}",
                anchor="sw",
                fill=self._palette["text"],
                font=("TkDefaultFont", 8),
            )
            self.tag_bind(
                marker,
                "<Button-1>",
                lambda _event, harmonic=item["k"]: self._select(harmonic),
            )


LineChartCanvas = LineChart
PhasorChartCanvas = PhasorChart


__all__ = [
    "DEFAULT_CHART_PALETTE",
    "LineChart",
    "LineChartCanvas",
    "PhasorChart",
    "PhasorChartCanvas",
    "line_segments",
    "padded_bounds",
    "phasor_extent",
    "project_point",
    "series_bounds",
]
