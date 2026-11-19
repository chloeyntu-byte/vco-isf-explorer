"""Sweep point selection and numeric overview for native results."""
import math
import tkinter as tk
from tkinter import font as tkfont, ttk

from ..core.offsets import offset_label
from ..core.result_presentation import sweep_summary
from ..core.spectre_topology import evaluate
from ..core.storage import projected_bytes
from .process_charts import ProcessChart
from .viewmodel import engineering, hbac_warning_text


def choose_points(parent, points, offsets, apply, cancel):
    window = tk.Toplevel(parent)
    window.title('Select Maestro points')
    window.transient(parent)
    window.minsize(560, 300)
    body = ttk.Frame(window, padding=12)
    body.pack(fill='both', expand=True)
    body.columnconfigure(0, weight=1)
    body.rowconfigure(1, weight=1)
    ttk.Label(body, text='Select the points to analyze', font='TkHeadingFont').grid(row=0, column=0, sticky='w')
    frame = ttk.Frame(body)
    frame.grid(row=1, column=0, sticky='nsew', pady=8)
    has_maestro_id = any(point.get('point_id') not in (None, '') for point in points)
    tree = ttk.Treeview(frame, columns=('corner', 'variables', 'id'), show='headings',
                        displaycolumns=('corner', 'variables', 'id') if has_maestro_id else ('corner', 'variables'),
                        selectmode='extended', height=min(10, len(points)))
    for name, title, width in (('corner', 'Corner', 120), ('variables', 'Sweep variables', 240),
                               ('id', 'Maestro point ID', 145)):
        tree.heading(name, text=title)
        tree.column(name, width=width)
    scroll = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
    scroll.pack(side='right', fill='y')
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(fill='both', expand=True)
    for point in points:
        parameters = ', '.join(f'{k}={v}' for k, v in point.get('variables', {}).items())
        tree.insert('', 'end', iid=point['id'], values=(point.get('corner') or 'nominal',
                    parameters or '—', point['point_id'] if point.get('point_id') not in (None, '') else '—'))
    estimate = tk.StringVar()
    ttk.Label(body, textvariable=estimate).grid(row=2, column=0, sticky='w', pady=(0, 6))
    buttons = ttk.Frame(body)
    buttons.grid(row=3, column=0, sticky='ew')
    ttk.Button(buttons, text='Select all', command=lambda: tree.selection_set(tree.get_children())).pack(side='left')
    ttk.Button(buttons, text='Clear', command=lambda: tree.selection_remove(tree.selection())).pack(side='left', padx=5)

    def close(action, *args):
        window.destroy()
        action(*args)

    ttk.Button(buttons, text='Cancel', command=lambda: close(cancel)).pack(side='right')
    start = ttk.Button(buttons, text='Analyze selected', command=lambda: close(apply, list(tree.selection())))
    start.pack(side='right', padx=5)

    def changed(_event=None):
        count = len(tree.selection())
        size = projected_bytes(len(offsets), count) / (1024 * 1024)
        estimate.set(f'{count}/{len(points)} points · {len(offsets)} offsets · '
                     f'{count * len(offsets)} point/offset runs · ≈{size:g} MiB')
        start.configure(state='normal' if count else 'disabled')

    tree.bind('<<TreeviewSelect>>', changed)
    tree.selection_set(tree.get_children())
    changed()
    window.protocol('WM_DELETE_WINDOW', lambda: close(cancel))
    window.bind('<Escape>', lambda _: close(cancel))
    window.grab_set()
    tree.focus_set()
    return window


def sweep_traces(rows, axis):
    from collections import defaultdict
    by_corner = defaultdict(list)
    for row in rows:
        value = row['variables'].get(axis)
        try:
            x = evaluate(str(value), {})
        except (TypeError, ValueError):
            return []
        if not math.isfinite(x):
            return []
        by_corner[row['corner'] or 'nominal'].append((x, row))
    series = []
    for corner, values in sorted(by_corner.items()):
        values.sort(key=lambda item: item[0])
        if len({x for x, _ in values}) != len(values):
            return []
        for field, name in (('isf', 'ISF'), ('hbnoise', 'HBnoise')):
            series.append(dict(id=f'{corner}:{field}', name=f'{corner} · {name}', points=[(x, r[field]) if r[field] is not None else None
                                                                 for x, r in values],
                               keys=[r['key'] for _, r in values], unit='dBc/Hz',
                               style='line', dash=field == 'hbnoise'))
    return series


def quality_flags(summary, synthetic=False):
    flags = ((summary.get('numerical_quality_pass'), 'Numerical', 'Fail'),
             (summary.get('pm_magnitude_projection_valid'), 'PM projection', 'Fail'),
             (summary.get('hbac_secondary_validation_pass'), 'HBAC phase', 'Warning'),
             (((summary.get('thermal_noise') or {}).get('numerical_quality') or {}).get('pass'), 'Thermal', 'Fail'))
    if synthetic and all(flag is None for flag, _, _ in flags):
        return 'not evaluated (synthetic replay)'
    status = ' · '.join(f'{name} {"Pass" if flag is True else negative if flag is False else "—"}'
                        for flag, name, negative in flags)
    warning = hbac_warning_text(summary)
    return status + ('\n' + warning if warning else '')


def _db(value, unit='dB'):
    return f'{value:+.2f} {unit}' if value is not None else '—'


def _gate(value, limit):
    if value is None:
        return '—'
    return f'{_db(value)} · {"Pass" if abs(value) <= limit else "Fail"} (≤ {limit:g} dB)'


def core_data(result, row):
    summary = row['sample']
    if not summary:
        return [('Status', row.get('error') or row['status'], None)]
    from ..metrics import compute_metrics

    try:
        metrics = compute_metrics(result, point=summary['point'])
    except (ImportError, ValueError):
        metrics = {}
    f = metrics.get('flicker', {})
    metadata = (result.get('dual_tb') or {}).get('metadata') or result.get('metadata') or {}
    synthetic = metadata.get('fixture_origin') == 'analytic_ideal_lc_synthetic'
    swing = metrics.get('vpk_v')
    if swing is None and summary.get('carrier_peak_V') is not None:
        swing = summary['carrier_peak_V']
    fraction = row['flicker_pct']
    corner_hz = None
    if fraction is not None and 0 < fraction < 100:
        corner_hz = summary['offset_Hz'] * fraction / (100 - fraction)
    return [
        ('Carrier f0 / differential swing', f'{engineering(row["f0"], "Hz")} · '
         f'{engineering(2 * swing if swing is not None else None, "Vpp")}', 1),
        ('ISF total / HBnoise total / Δ', f'{engineering(row["isf"])} / {engineering(row["hbnoise"])} dBc/Hz · {_db(row["delta"])}', 3),
        ('Flicker / thermal', f'{fraction:.1f}% / {100-fraction:.1f}%' if fraction is not None else '—', 7),
        ('1/f³ corner estimate', engineering(corner_hz, 'Hz'), 6),
        ('Flicker pair error', _gate(row['pair_error'], 2), 3),
        ('Whole-VCO closure error', _gate(row['delta'], 3), 4),
        ('Γrms / c0_eff / ρ_dyn', ' / '.join((engineering(metrics.get('gamma', {}).get('rms'), 'C^-1'),
          engineering(f.get('c0_eff'), 's^-1/√Hz'), '—' if f.get('rho_dyn') is None else f"{f['rho_dyn']:.4g}")), 8),
        ('Numerical quality', quality_flags(summary, synthetic), 5),
        ('Top HBnoise sources', '  ·  '.join(f'{r["label"]} {r["simulated_dbc"]:.2f} dBc/Hz'
                                                for r in row['sources'][:3]) or '—', 4),
    ]


def _headline_metrics(row):
    def noise(value):
        return _db(value, 'dBc/Hz')

    def closure(key, limit):
        value = row[key]
        if value is None:
            return '—', 'Unavailable'
        verdict = 'Pass' if abs(value) <= limit else 'Fail'
        return _db(value), f'{verdict} · ±{limit:g} dB limit'

    fraction = row['flicker_pct']
    pair, pair_status = closure('pair_error', 2)
    total, total_status = closure('delta', 3)
    return (
        ('Carrier f0', engineering(row['f0'], 'Hz'), ''),
        ('ISF total', noise(row['isf']), f'HBnoise {noise(row["hbnoise"])}\nΔ {_db(row["delta"])}'),
        ('Flicker pair error', pair, pair_status),
        ('Whole-VCO closure', total, total_status),
        ('Flicker / thermal', f'{fraction:.1f}% / {100-fraction:.1f}%' if fraction is not None else '—', ''),
    )


class Overview(ttk.Frame):
    def __init__(self, master, table_type, select, show):
        super().__init__(master)
        self.select = select
        self.sort = ('corner', False)
        self.sweep = ttk.Frame(self)
        self.sweep.pack(fill='both', expand=True)
        self.sweep.columnconfigure(0, weight=4)
        self.sweep.rowconfigure(0, weight=1)
        self.table = table_type(self.sweep, lambda key, _: select(key))
        self.table._show_all = True
        self.table.grid(row=0, column=0, sticky='nsew')
        self.chart = ProcessChart(self.sweep, on_select=lambda key, clicked: select(key) if clicked else None)
        self.chart.configure(width=225, height=185)
        self.chart.grid(row=0, column=1, sticky='nsew', padx=(8, 0))
        self.sweep.bind('<Configure>', self._layout_sweep)
        self.headlines = ttk.Frame(self, padding=(0, 4, 0, 10))
        self.cards = []
        for column in range(5):
            self.headlines.columnconfigure(column, weight=1, uniform='metric')
            card = ttk.Frame(self.headlines, padding=(10, 8), style='OverviewCard.TFrame')
            card.grid(row=0, column=column, sticky='nsew', padx=(0, 6) if column < 4 else 0)
            labels = []
            for style in ('OverviewCardTitle', 'OverviewCardValue', 'OverviewCardDetail'):
                label = ttk.Label(card, style=style + '.TLabel', justify='left')
                label.pack(anchor='w')
                labels.append(label)
            self.cards.append(labels)
        self.core = ttk.Frame(self, padding=(5, 4))
        self.core.pack(fill='x')
        self.core.columnconfigure(1, weight=1)
        self.values = []
        self.links = []
        for index in range(9):
            caption, value = tk.StringVar(), tk.StringVar()
            caption_label = ttk.Label(self.core, textvariable=caption, style='Help.TLabel')
            caption_label.grid(row=index, column=0, sticky='nw', padx=(0, 10), pady=2)
            value_label = ttk.Label(self.core, textvariable=value, width=1)
            value_label.grid(row=index, column=1, sticky='ew', pady=2)
            link = ttk.Button(self.core, command=show)
            link.grid(row=index, column=2, sticky='ne', padx=(6, 0))
            self.values.append((caption, value, caption_label, value_label))
            self.links.append(link)
        self.core.bind('<Configure>', self._wrap)
        self.rows, self.specs = [], []

    def _layout_sweep(self, _event=None):
        if self.chart.winfo_manager() != 'grid':
            return
        tree = self.table.tree
        scrollbar_width = max(0, self.table.winfo_width() - tree.winfo_width())
        side_by_side_width = tree.winfo_reqwidth() + scrollbar_width + self.chart.winfo_reqwidth() + 8
        if self.sweep.winfo_width() >= side_by_side_width:
            self.chart.grid_configure(row=0, column=1, padx=(8, 0), pady=0)
        else:
            self.chart.grid_configure(row=1, column=0, padx=0, pady=(8, 0))

    def _wrap(self, event):
        width = max(150, event.width - 375)
        for _, _, _, label in self.values:
            label.configure(wraplength=width)

    def clear(self):
        self.sweep.pack_forget()
        self.headlines.pack_forget()
        for caption, value, *_ in self.values:
            caption.set('')
            value.set('')
        for link in self.links:
            link.grid_remove()

    def _sort(self, field):
        current, descending = self.sort
        self.sort = field, not descending if current == field else False
        self.render_table()

    def render_table(self):
        field, descending = self.sort

        def key(row):
            value = row[field]
            try:
                return 0, float(value)
            except (TypeError, ValueError):
                return 1, str(value).casefold()

        available = sorted((r for r in self.rows if r[field] is not None), key=key, reverse=descending)
        self.table.fill(available + [r for r in self.rows if r[field] is None], self.specs)
        tree = self.table.tree
        style = ttk.Style(tree)
        style_name = tree.cget('style') or 'Treeview'
        cell_font = tkfont.Font(root=tree, font=style.lookup(style_name, 'font') or 'TkDefaultFont')
        heading_font = tkfont.Font(root=tree, font=style.lookup(style_name + '.Heading', 'font') or cell_font)
        for field, title in self.specs:
            tree.heading(field, text=title, command=lambda f=field: self._sort(f))
            heading_width = heading_font.measure(title)
            value_width = max((cell_font.measure(self.table.display_value(row, field, title))
                               for row in self.rows), default=0)
            if field == 'top_source':
                # Long instance names stay available via the cell tooltip and row copy.
                value_width = min(value_width, heading_width + cell_font.measure('000'))
            width = max(heading_width, value_width) + 12
            tree.column(field, minwidth=heading_width + 12, width=width, stretch=False)
        self.table.highlight(self.current)

    def render(self, result, points, identity, offset, show):
        rows = sweep_summary(result, points, offset)
        current = next((row for row in rows if row['key'] == identity), None)
        self.current = identity
        self.headlines.pack_forget()
        if len(rows) > 1:
            self.sweep.pack(fill='both', expand=True, before=self.core)
            axes = sorted({key for row in rows for key in row['variables']})
            self.rows = [dict(r, **{'var ' + axis: r['variables'].get(axis) for axis in axes}) for r in rows]
            self.specs = [('corner','Corner')] + [('var ' + axis, axis) for axis in axes] + [
                ('f0','f0 (Hz)'), ('isf','ISF'), ('hbnoise','HBnoise'),
                ('delta','Δ dB'), ('pair_error','Pair Δ dB'), ('flicker_pct','Flicker %'),
                ('thermal_pct','Thermal %'), ('top_source','Top source'), ('status','Status')]
            if not axes:
                self.specs.insert(1, ('label', 'Point'))
            if self.sort[0] not in {field for field, _ in self.specs}:
                self.sort = ('corner', False)
            self.render_table()
            series = sweep_traces(rows, axes[0]) if len(axes) == 1 and offset is not None else []
            if series:
                self.chart.grid()
                self.chart.set_panels([dict(title='Phase noise at ' + offset_label(offset),
                    xlabel=axes[0], ylabel='dBc/Hz', series=series, legend_full=True)], reset=False)
                self.chart.highlight(identity)
                self._layout_sweep()
            else:
                self.chart.grid_remove()
        else:
            self.sweep.pack_forget()
        if len(rows) == 1 and current and current['sample']:
            for labels, metric in zip(self.cards, _headline_metrics(current)):
                for label, text in zip(labels, metric):
                    label.configure(text=text)
            self.headlines.pack(fill='x', before=self.core)
        data = core_data(result, current) if current else []
        for index, (caption, value, caption_label, value_label) in enumerate(self.values):
            link = self.links[index]
            if index < len(data):
                label, text, view = data[index]
                caption.set(label)
                value.set(text)
                caption_label.grid()
                value_label.grid()
                if view is not None:
                    link.configure(text=f'Open in view {view:02}', command=lambda v=view: show(v))
                    link.grid()
                else:
                    link.grid_remove()
            else:
                caption_label.grid_remove()
                value_label.grid_remove()
                link.grid_remove()
