"""Responsive Canvas process plots with shared coordinates and stable row keys.

No numerical analysis lives here. Panels receive original-unit display series
from viewmodel; zoom, visibility and selection affect drawing only.
"""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import font as tkfont

from .charts import DEFAULT_CHART_PALETTE, line_segments, padded_bounds, project_point
from .viewmodel import engineering, finite_number

from .fieldwork_tk import SERIES

COLORS = SERIES


class ProcessChart(tk.Canvas):
    def __init__(self, master, *, on_select=None, on_open=None, **kwargs):
        kwargs.setdefault('highlightthickness', 0)
        kwargs.setdefault('width', 300)
        kwargs.setdefault('height', 220)
        super().__init__(master, **kwargs)
        self.palette = dict(DEFAULT_CHART_PALETTE)
        self.panels = []
        self.selected_key = None
        self.key_positions = {}
        self.on_select, self.on_open = on_select, on_open
        self.hidden = set()
        self.limits = {}
        self.hits, self.plots = [], []
        self._pending = None
        self._drag = None
        self.small_font = tkfont.Font(self, family=tkfont.nametofont('TkDefaultFont').actual('family'), size=-11)
        self.title_font = tkfont.Font(self, family=tkfont.nametofont('TkDefaultFont').actual('family'), size=-12, weight='bold')
        self.bind('<Configure>', self.schedule, add='+')
        self.bind('<Destroy>', self._destroy, add='+')
        self.bind('<Motion>', self._motion)
        self.bind('<Button-1>', self._click)
        self.bind('<Control-Button-4>', lambda e: self._zoom(e, .8))
        self.bind('<Control-Button-5>', lambda e: self._zoom(e, 1.25))
        self.bind('<Control-MouseWheel>', lambda e: self._zoom(e, .8 if e.delta > 0 else 1.25))
        self.bind('<Button-2>', self._pan_start)
        self.bind('<B2-Motion>', self._pan_move)
        self.bind('<ButtonRelease-2>', lambda e: setattr(self, '_drag', None))

    def set_palette(self, palette):
        self.palette.update(palette)
        self.configure(background=self.palette['background'])
        self.schedule()

    def set_panels(self, panels, *, reset=False):
        self.panels = panels
        if reset:
            self.limits.clear()
            self.hidden.clear()
            self.selected_key = None
        self.schedule()

    def highlight(self, key):
        self.selected_key = key
        self._cursor()

    def reset(self):
        self.limits.clear()
        self.schedule()

    def toggle(self, panel_index, series_id):
        identity = (panel_index, series_id)
        self.hidden.symmetric_difference_update({identity})
        self.schedule()

    def schedule(self, _event=None):
        if self._pending is None:
            self._pending = self.after_idle(self.draw)

    def _destroy(self, event):
        if event.widget == self and self._pending is not None:
            self.after_cancel(self._pending)
            self._pending = None

    def _text(self, x, y, text, **kwargs):
        return self.create_text(x, y, text=text, fill=kwargs.pop('fill', self.palette['text']),
                                font=kwargs.pop('font', self.small_font), **kwargs)

    def _message(self, x, y, text, width, *, tag):
        # Canvas multiline spacing can use Latin font metrics even when CJK
        # fallback glyphs are taller. Draw separate lines and use their actual
        # bounding boxes, so mixed Chinese/Latin messages cannot overlap.
        lines = []
        for paragraph in str(text).split('\n'):
            line = ''
            for char in paragraph:
                if line and self.small_font.measure(line + char) > width:
                    lines.append(line)
                    line = ''
                line += char
            lines.append(line)
        items = [self._text(x, 0, line, tags=tag, fill=self.palette['muted']) for line in lines]
        heights = [self.bbox(item)[3] - self.bbox(item)[1] for item in items]
        step = max(heights, default=0) + 6
        for i, item in enumerate(items):
            self.move(item, 0, y + (i - (len(items) - 1) / 2) * step)

    def draw(self):
        self._pending = None
        self.delete('all')
        self.hits, self.plots = [], []
        width, height = self.winfo_width(), self.winfo_height()
        if width < 120 or height < 60:
            return
        if not self.panels:
            self._text(width / 2, height / 2, '选择任务结果后查看 ISF 分析链路')
            return
        for index, panel in enumerate(self.panels):
            layout = panel.get('layout', (0, index / len(self.panels), 1, (index + 1) / len(self.panels)))
            box = (layout[0] * width, layout[1] * height, layout[2] * width, layout[3] * height)
            self._panel(index, panel, box)
        self._cursor()

    def _color(self, series, index):
        if self.selected_key and (series.get('row_key') == self.selected_key or self.selected_key in series.get('keys', [])):
            return self.palette['accent']
        if series.get('role')=='total':
            return self.palette['text']
        return series.get('color', COLORS[index % len(COLORS)])

    def _legend_select(self, panel_index, series):
        key=series.get('row_key')
        if key:
            self.highlight(key)
            if self.on_select:
                self.on_select(key,True)
            self.schedule()
        else:
            self.toggle(panel_index,series['id'])

    def _panel(self, index, panel, box):
        x0, y0, x1, y1 = box
        self.create_rectangle(x0 + 2, y0 + 2, x1 - 2, y1 - 2, fill=self.palette['background'], outline=self.palette['grid'])
        self._text(x0 + 10, y0 + 10, panel.get('title', ''), anchor='w', font=self.title_font, tags=f'title:{index}')
        if panel.get('open') is not None:
            self._text(x1 - 12, y0 + 10, '展开 ↗', anchor='e', fill=self.palette['accent'])
        if panel.get('kind') == 'sources':
            self._sources(index, panel, box)
            return
        if panel.get('kind') == 'phasor':
            self._phasor(index, panel, box)
            return
        visible = [s for s in panel.get('series', []) if (index, s['id']) not in self.hidden]
        compact = y1 - y0 < 130
        tiny = y1 - y0 < 85
        # Legends and readouts get their own header rows, never cover titles.
        legend_x, legend_y = x0 + 10, y0 + 28
        legend_step = max(17, self.small_font.metrics('linespace') + 3)
        full = panel.get('legend_full', False) and not compact
        if full and not tiny:
            # A wrapped legend must not eat the plot: with wide glyphs (for
            # example Noto CJK) fall back to one truncated row when the full
            # legend would leave the plot less than half of the panel height.
            rows, cursor = 1, x0 + 10
            for series in panel.get('series', []):
                length = 26 + self.small_font.measure(series['name'])
                if cursor + length > x1 - 10 and cursor > x0 + 10:
                    rows, cursor = rows + 1, x0 + 10
                cursor += length
            plot_height = (y1 - y0) - (28 + (rows - 1) * legend_step + 36) - (34 if panel.get('show_xlabel', True) else 9)
            if plot_height < (y1 - y0) / 2:
                full = False
        for si, series in enumerate(panel.get('series', []) if not tiny else []):
            color = self._color(series, si)
            label = series['name']
            budget = (x1-x0-50 if full else max(40, (x1-x0-28)/max(1,len(panel['series']))-18))
            while self.small_font.measure(label) > budget and len(label)>5:
                label=label[:-2]+'…'
            length=26+self.small_font.measure(label)
            if full and legend_x+length > x1-10 and legend_x>x0+10:
                legend_x,legend_y=x0+10,legend_y+legend_step
            tag=f'legend:{index}:{si}'
            self.create_line(legend_x,legend_y,legend_x+12,legend_y,fill=color,width=series.get('width',2),
                             dash=(4,3) if series.get('dash') else (),tags=tag)
            self._text(legend_x+16,legend_y,label,anchor='w',fill=color,tags=tag)
            self.tag_bind(tag,'<Button-1>',lambda e,i=index,s=series:self._legend_select(i,s))
            legend_x+=length
        right_axis=bool(panel.get('right_label'))
        header = 22 if tiny else 34 if compact else legend_y-y0+36
        rect=(x0+76,y0+header,x1-(80 if right_axis or panel.get('reserve_right') else 22),y1-(34 if panel.get('show_xlabel',True) else 9))
        if rect[3] <= rect[1] + 5 or rect[2] <= rect[0] + 10:
            return
        left, top, right, bottom = rect
        log = panel.get('log_x', False)
        all_points = [p for s in visible for p in s['points'] if p is not None and finite_number(p[1]) is not None and (not log or p[0] > 0)]
        if not all_points:
            reason = panel.get('reason') or '本结果未保存该阶段的数据'
            self._message((left + right) / 2, (top + bottom) / 2, reason, max(100, right - left - 12), tag=f'reason:{index}')
            self._text((left + right) / 2, y1 - 10, panel.get('xlabel', ''), fill=self.palette['muted'])
            return
        xs = [math.log10(p[0]) if log else p[0] for p in all_points]
        group = panel.get('group', index)
        xlim = self.limits.get(group) or panel.get('xlimits')
        if xlim is None:
            low, high = min(xs), max(xs)
            margin = (high - low) * .04 if high != low else max(abs(low) * .05, .1)
            xlim = (low - margin, high + margin)
        bounds_by_side = {}
        for side in ('left', 'right'):
            ys = [p[1] for s in visible if s.get('side', 'left') == side for p in s['points'] if p is not None and finite_number(p[1]) is not None]
            if not ys:
                ys = [0., 1.]
            low, high = min(ys), max(ys)
            if panel.get('zero') or any(s.get('style') in ('fill', 'stem') for s in visible if s.get('side', 'left') == side):
                low, high = min(0., low), max(0., high)
            if high == low:
                margin = abs(high) * .08 or 1.
            else:
                margin = (high - low) * .08
            bounds_by_side[side] = tuple(xlim) + (low - margin, high + margin)
        self.create_rectangle(*rect, outline=self.palette['border'], tags='plot-frame')
        ticks = 2 if bottom - top < 30 else 3 if bottom - top < 95 else 5
        for tick in range(ticks):
            ratio = tick / (ticks - 1)
            y = bottom - (bottom - top) * ratio
            self.create_line(left, y, right, y, fill=self.palette['grid'])
            for side in ('left', 'right') if right_axis else ('left',):
                b = bounds_by_side[side]
                val = b[2] + ratio * (b[3] - b[2])
                self._text(left - 7 if side == 'left' else right + 7, y, engineering(val, digits=3), anchor='e' if side == 'left' else 'w', fill=self.palette['muted'])
        xticks = [xlim[0] + (xlim[1] - xlim[0]) * t / 4 for t in range(5)]
        if panel.get('group') == 'k':
            orders=list(range(math.ceil(xlim[0]),math.floor(xlim[1])+1))
            xticks=orders[::max(1,math.ceil(len(orders)/10))]
        if log:
            decades = list(range(math.ceil(xlim[0]), math.floor(xlim[1]) + 1))
            if 1 < len(decades) < 8:
                xticks = decades
        for tick in xticks:
            x = left + (tick - xlim[0]) / (xlim[1] - xlim[0]) * (right - left)
            self.create_line(x, top, x, bottom, fill=self.palette['grid'])
            if panel.get('show_xlabel', True):
                label = f'{tick:.3g}' if panel.get('group') in ('phase', 'k') else engineering(10 ** tick if log else tick, digits=3)
                self._text(x, bottom + 11, label, fill=self.palette['muted'])
        self._text(x0 + 12, (top + bottom) / 2, panel.get('short_ylabel', panel.get('ylabel', '')) if compact else panel.get('ylabel', ''), angle=90, fill=self.palette['muted'])
        if right_axis:
            self._text(x1 - 12, (top + bottom) / 2, panel['right_label'], angle=90, fill=self.palette['muted'])
        if panel.get('show_xlabel', True):
            self._text((left + right) / 2, y1 - 9, panel.get('xlabel', ''), fill=self.palette['muted'])
        if panel.get('zero'):
            b = bounds_by_side['left']
            if b[2] <= 0 <= b[3]:
                _, y = project_point((xlim[0], 0), b, rect)
                self.create_line(left, y, right, y, fill=self.palette['muted'], dash=(3, 3))
        plot = {'index': index, 'panel': panel, 'rect': rect, 'bounds': bounds_by_side, 'group': group, 'log': log}
        self.plots.append(plot)
        for si, series in enumerate(panel.get('series', [])):
            if series not in visible:
                continue
            color = self._color(series, si)
            b = bounds_by_side[series.get('side', 'left')]
            transformed = [(math.log10(p[0]) if log else p[0], p[1]) if p is not None and finite_number(p[1]) is not None and (not log or p[0] > 0) else None for p in series['points']]
            # Clip path segments to the visible x interval. A gap always stays a gap.
            from .charts import _clip_x
            for segment in line_segments(transformed):
                for path in _clip_x(segment, *xlim):
                    coords = [project_point(p, b, rect) for p in path]
                    if series.get('style') == 'fill':
                        _, base = project_point((xlim[0], 0), b, rect)
                        # Per-segment trapezoids preserve the signed zero crossings.
                        for a, c in zip(coords, coords[1:]):
                            self.create_polygon(a[0], base, *a, *c, c[0], base, fill=color, stipple='gray25', outline='')
                    if len(coords) > 1 and series.get('style') not in ('stem','bar'):
                        self.create_line(*[v for p in coords for v in p], fill=color, width=(series.get('width', 1.6)+ (1.5 if self.selected_key and self.selected_key in series.get('keys',[]) else 0)), dash=(5, 3) if series.get('dash') else ())
            for sample, p in enumerate(transformed):
                if p is None or not xlim[0] <= p[0] <= xlim[1]:
                    continue
                x, y = project_point(p, b, rect)
                if series.get('style') in ('stem','bar'):
                    _, base = project_point((p[0], 0), b, rect)
                    # Offset paired stems slightly for comparison without changing keys.
                    x += (si - (len(visible) - 1) / 2) * 4
                    if series.get('style')=='bar':
                        if series.get('unit')=='dBc/Hz':
                            base=bottom
                        barwidth=max(5,min(26,(right-left)/max(2,len(transformed))* .4/max(1,len(visible))))
                        self.create_rectangle(x-barwidth/2,min(base,y),x+barwidth/2,max(base,y)+.5,fill=color,outline=color)
                    else:
                        self.create_line(x,base,x,y,fill=color,width=3)
                if len(transformed) < 100 or series.get('style') == 'stem':
                    self.create_oval(x-3,y-3,x+3,y+3,fill=self.palette['background'] if sample<len(series.get('hollow',[])) and series['hollow'][sample] else color,outline=color)
                key = series.get('keys', [])[sample] if sample < len(series.get('keys', [])) else None
                self.hits.append({'key': key, 'x': x, 'y': y, 'value': series['points'][sample],
                                  'series': series, 'plot': plot, 'color': color, 'sample_index': sample})
        if panel.get('statistics'):
            for si, series in enumerate(visible):
                mean = panel['statistics'].get(series['name'], {}).get('mean')
                if mean is not None:
                    _, ym = project_point((xlim[0], mean), bounds_by_side['left'], rect)
                    self.create_line(left, ym, right, ym, fill=COLORS[si % len(COLORS)], dash=(5, 4), tags='dc-mean')
            stats = '  ·  '.join(f'{name}: DC {engineering(s["mean"])} / RMS {engineering(s["rms"])}' for name, s in panel['statistics'].items())
            if not compact:
                self._text(left,rect[1]-24,stats,anchor='w',width=right-left,fill=self.palette['muted'],tags='statistics')
        if panel.get('reason'):
            # Full missing-field details are also exposed in the readout / table.
            self._text(right, rect[1] + 5, panel['reason'], anchor='e', width=max(80, right - left), fill=self.palette['muted'], tags='missing-reason')

    def _phasor(self, index, panel, box):
        x0, y0, x1, y1 = box
        points = panel.get('phasors', [])
        left, top, right, bottom = x0 + 80, y0 + 39, x1 - 32, y1 - 33
        if not points:
            self._text((x0 + x1) / 2, (y0 + y1) / 2, '本结果没有可用的 ΣQk 复数累计')
            return
        extent = max(max(abs(p['real']), abs(p['imag'])) for p in points) * 1.18 or 1.
        side = min(right - left, bottom - top)
        cx, cy = (left + right) / 2, (top + bottom) / 2
        rect = (cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2)
        b = (-extent, extent, -extent, extent)
        for ratio in (.25, .5, .75, 1.):
            radius = side / 2 * ratio
            self.create_oval(cx - radius, cy - radius, cx + radius, cy + radius, outline=self.palette['grid'])
        self.create_line(left, cy, right, cy, fill=self.palette['border'])
        self.create_line(cx, top, cx, bottom, fill=self.palette['border'])
        self._text((left + right) / 2, y1 - 12, 'Re{ΣQk} (V/√Hz) · 半径 ' + engineering(extent, 'V/√Hz'))
        self._text(x0 + 16, (top + bottom) / 2, 'Im{ΣQk} (V/√Hz)', angle=90)
        path = [(cx, cy)] + [project_point((p['real'], p['imag']), b, rect) for p in points]
        self.create_line(*[v for p in path for v in p], fill=COLORS[0], width=2)
        label_boxes = []
        for node_index, (p, (x, y)) in enumerate(zip(points, path[1:])):
            self.create_oval(x - 3, y - 3, x + 3, y + 3, fill=COLORS[1], outline='')
            if self.selected_key == p['key']:
                self._text(min(x1-28,x+8),max(y0+31,y-10),'k'+str(p['k']),anchor='w')
            self.hits.append({'key': p['key'], 'x': x, 'y': y, 'value': (p['real'], p['imag']), 'series': {'name': 'ΣQk'}, 'color': COLORS[1], 'plot': {'index': index, 'rect': rect, 'panel': panel}})

    def _sources(self, index, panel, box):
        x0, y0, x1, y1 = box
        rows = panel.get('rows', [])
        # The viewport shows a clearly labelled ranked subset. All rows remain
        # in the table; selecting a low-ranked source brings it into the plot.
        count = max(3, int((y1 - y0 - 57) / 30))
        displayed = rows[:count]
        selected = next((r for r in rows if r['key'] == self.selected_key), None)
        if selected and selected not in displayed:
            displayed = displayed[:-1] + [selected]
        left, right = x0 + min(210, (x1 - x0) * .34), x1 - 74
        values = [finite_number(r.get(k)) for r in rows for k in ('simulated_dbc', 'calculated_dbc', 'stored_thermal_dbc')]
        values = [v for v in values if v is not None]
        if not values:
            self._text((x0 + x1) / 2, (y0 + y1) / 2, '未采集各器件 / 噪声机制贡献')
            return
        low = min(values) - 5
        high = max(values) + 6
        self._text(x0 + 10, y0 + 31, f'聚合置顶 / 源功率排序 {len(displayed)}/{len(rows)} 行 · 蓝 HBnoise / 橙 ISF / 紫 未验诊断', anchor='w', width=x1-x0-20, fill=self.palette['muted'])
        for i, row in enumerate(displayed):
            y = y0 + 48 + i * ((y1 - y0 - 75) / max(1, len(displayed)))
            label = (str(row.get('label')) if row.get('kind') == 'aggregate'
                     else str(row.get('device')) + '/' + str(row.get('mechanism')))
            while len(label) > 1 and self.small_font.measure(label) > max(10, left - x0 - 14):
                label = '…' + label[2:]
            self._text(left - 10, y + 3, label, anchor='e', fill=self.palette['text'] if row['modeled'] else self.palette['muted'])
            for j, field in enumerate(('simulated_dbc', 'calculated_dbc', 'stored_thermal_dbc')):
                value = finite_number(row.get(field))
                if value is None:
                    continue
                x = left + (value - low) / (high - low) * max(1, right - left)
                self.create_rectangle(left, y + j * 6 - 4, x, y + j * 6 + 1, fill=COLORS[j], outline='')
                self._text(x + 5, y + j * 11 - 4, engineering(value), anchor='w', fill=COLORS[j])
                self.hits.append({'key': row['key'], 'x': x, 'y': y, 'value': (i, value),
                                  'series': {'name': label + ' · ' + row['scope'], 'unit':'dBc/Hz'}, 'color': COLORS[j], 'plot': {'index': index, 'rect': (left, y - 6, right, y + 12), 'panel': panel}})
        self._text((left + right) / 2, y1 - 13, f'相噪 (dBc/Hz) · 范围 {engineering(low, digits=3)} … {engineering(high, digits=3)}', fill=self.palette['muted'])

    def _cursor(self):
        self.delete('cursor')
        target = self.key_positions.get(self.selected_key)
        matches = [h for h in self.hits if h['key'] is not None and h['key'] == self.selected_key]
        if target is not None:
            # thermal stays on its native grid. Cursor is at the selected
            # workpoint phase; the marker/readout names the nearest raw sample.
            for plot in self.plots:
                if plot['panel'].get('group') != 'phase':
                    continue
                for series in plot['panel'].get('series', []):
                    if not series.get('native_grid'):
                        continue
                    matches = [h for h in matches if h['series'] is not series]
                    phases = series.get('sample_phases', [])
                    indexes = [i for i,p in enumerate(phases) if p is not None]
                    if indexes:
                        nearest = min(indexes, key=lambda i: abs(phases[i] - target))
                        matches.extend(h for h in self.hits if h['plot'] is plot and h['series'] is series and h.get('sample_index') == nearest)
        used = set()
        for hit in matches:
            x, y = hit['x'], hit['y']
            plot = hit['plot']
            rect = plot['rect']
            self.create_oval(x - 4, y - 4, x + 4, y + 4, outline=self.palette['selection'], width=2, tags='cursor')
            if plot['panel'].get('kind')=='sources':
                self.create_rectangle(rect[0],rect[1],rect[2],rect[3]+4,outline=self.palette['selection'],tags='cursor')
                continue
            if plot['index'] not in used:
                if 'bounds' in plot:
                    cursor_x = project_point((target,0), plot['bounds']['left'], rect)[0] if target is not None and plot['panel'].get('group') == 'phase' else x
                    self.create_line(cursor_x, rect[1], cursor_x, rect[3], fill=self.palette['selection'], dash=(3,3), tags='cursor')
                used.add(plot['index'])
        if target is not None:
            for plot in self.plots:
                if plot['panel'].get('group') == 'phase' and plot['index'] not in used:
                    rect=plot['rect']
                    x=project_point((target,0),plot['bounds']['left'],rect)[0]
                    self.create_line(x,rect[1],x,rect[3],fill=self.palette['selection'],dash=(3,3),tags='cursor')
                    self._text(rect[2],rect[1]-8,'该相位缺少有效采样',anchor='e',fill=self.palette['muted'],tags='cursor')

    def _nearest(self, event):
        in_panel = [h for h in self.hits if h['plot']['rect'][0] - 8 <= event.x <= h['plot']['rect'][2] + 8 and h['plot']['rect'][1] - 10 <= event.y <= h['plot']['rect'][3] + 10]
        if not in_panel:
            return None
        return min(in_panel, key=lambda h: (h['x'] - event.x) ** 2 + (h['y'] - event.y) ** 2 * (.05 if h['plot']['panel'].get('group') == 'phase' else 1))

    def _motion(self, event):
        hit = self._nearest(event)
        if hit and hit['key'] != self.selected_key:
            self.highlight(hit['key'])
            if self.on_select:
                self.on_select(hit['key'], False)

    def _click(self, event):
        # A mini-panel opens its full view. Legends handle their own clicks.
        current = self.find_withtag('current')
        if current and any(t.startswith('legend:') for t in self.gettags(current[0])):
            return
        for panel in self.panels:
            layout = panel.get('layout')
            if layout and panel.get('open') is not None and layout[0] * self.winfo_width() <= event.x <= layout[2] * self.winfo_width() and layout[1] * self.winfo_height() <= event.y <= layout[3] * self.winfo_height():
                if self.on_open:
                    self.on_open(panel['open'])
                return
        hit = self._nearest(event)
        if hit:
            self.highlight(hit['key'])
            if self.on_select:
                self.on_select(hit['key'], True)

    def _plot_at(self, event):
        return next((p for p in self.plots if p['rect'][0] <= event.x <= p['rect'][2] and p['rect'][1] <= event.y <= p['rect'][3]), None)

    def _zoom(self, event, scale):
        plot = self._plot_at(event)
        if plot:
            low, high = plot['bounds']['left'][:2]
            anchor = (event.x - plot['rect'][0]) / (plot['rect'][2] - plot['rect'][0])
            span = max(1e-12, (high - low) * scale)
            low += (high - low - span) * anchor
            self.limits[plot['group']] = (low, low + span)
            self.schedule()
        return 'break'

    def _pan_start(self, event):
        plot = self._plot_at(event)
        self._drag = (plot, event.x, plot['bounds']['left'][:2]) if plot else None

    def _pan_move(self, event):
        if self._drag:
            plot, origin, limits = self._drag
            delta = (origin - event.x) / (plot['rect'][2] - plot['rect'][0]) * (limits[1] - limits[0])
            self.limits[plot['group']] = (limits[0] + delta, limits[1] + delta)
            self.schedule()
