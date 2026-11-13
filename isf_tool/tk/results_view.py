"""Native ISF process explorer. Layout and interaction, separate from app/core."""
from __future__ import annotations

import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont

from .charts import LineChart, PhasorChart, ContributionBars
from .process_charts import ProcessChart
from .viewmodel import (engineering, finite_number, process_projection, harmonic_projection,
                        offset_projection, contribution_projection, noise_scope_note, load_thermal_coefficients, stored_thermal_diagnostic, thermal_workpoint_series, stored_thermal_series, offset_table_projection, band_noise_projection, thermal_band_projection, isf_overlay_projection, HARMONIC_NOTE)

VIEWS = ('总览', 'ISF 全过程', '谐波分解', '相噪 vs 仿真', '噪声贡献', '指标与质量', '频带 → 相噪', 'thermal 频带')


def maximize(root):
    root.minsize(1100, 700)
    width, height = root.winfo_screenwidth(), root.winfo_screenheight()
    # Respect EWMH workarea when available. Xvnc without a WM uses geometry.
    root.geometry(f'{width}x{height}+0+0')
    try:
        root.attributes('-zoomed', True)
    except tk.TclError:
        try:
            root.state('zoomed')
        except tk.TclError:
            pass


class LinkedTable(ttk.Frame):
    """Autosized values, complete-precision hover and copy with raw units."""
    def __init__(self, master, on_select):
        super().__init__(master, width=200, height=100)
        # Column visibility depends on allocated space. Do not feed the
        # Treeview's new requested width back into its parent's pane/grid.
        self.grid_propagate(False)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(self, show='headings', selectmode='browse', height=3)
        self.tree.grid(row=0, column=0, sticky='nsew')
        sy = ttk.Scrollbar(self, orient='vertical', command=self.tree.yview)
        sx = ttk.Scrollbar(self, orient='horizontal', command=self.tree.xview)
        sy.grid(row=0, column=1, sticky='ns')
        sx.grid(row=1, column=0, sticky='ew')
        self.tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.on_select = on_select
        self.rows, self.specs = {}, []
        self._selected = None
        self._tip = None
        self._tip_cell = None
        self.tree.bind('<<TreeviewSelect>>', self._select)
        self.tree.bind('<Control-c>', self.copy)
        self.tree.bind('<Control-C>', self.copy)
        self.tree.bind('<Motion>', self._hover)
        self.tree.bind('<Leave>', self._hide_tip)
        self.tree.bind('<Destroy>', self._hide_tip)
        self.bind('<Configure>', self._fit_columns, add='+')
        self._show_all = False
        menu = tk.Menu(self.tree, tearoff=False)
        menu.add_command(label='复制选中行（完整精度）', command=self.copy)
        menu.add_command(label='全部列 / 自适应列', command=self.show_all_columns)
        self.tree.bind('<Button-3>', lambda e: menu.tk_popup(e.x_root, e.y_root))

    def fill(self, rows, specs):
        self._hide_tip()
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self.rows = {str(r['key']): r for r in rows}
        self.specs = specs
        self._selected = None
        self.tree.configure(displaycolumns='#all')
        self.tree.configure(columns=[s[0] for s in specs])
        font = tkfont.nametofont('TkDefaultFont')
        for field, label in specs:
            values = [engineering(r.get(field)) for r in rows]
            # Full compact values fit their column; long content is scrollable.
            text_limit=240 if field=='label' else 180 if field=='value' else 100 if field=='unit' else 280
            raw_widths = [font.measure(v) if finite_number(r.get(field)) is not None else min(text_limit,font.measure(v)) for r,v in zip(rows,values)]
            width = max([font.measure(label)] + raw_widths) + 24
            self.tree.heading(field, text=label)
            self.tree.column(field, width=width, minwidth=width, stretch=False)
        self._fit_columns()
        for key, row in self.rows.items():
            self.tree.insert('', 'end', iid=key, values=[engineering(row.get(f)) for f, _ in specs], tags=('chosen',) if row.get('chosen') else ())

    def _fit_columns(self, _event=None):
        if not self.specs:
            return
        if self._show_all:
            self.tree.configure(displaycolumns=[f for f,_ in self.specs])
            return
        remaining = max(50, self.winfo_width() - 22)
        fields = []
        for field, label in self.specs:
            width = int(self.tree.column(field, 'width'))
            if width <= remaining or not fields:
                fields.append(field)
                remaining -= width
        if tuple(self.tree['displaycolumns']) != tuple(fields):
            self.tree.configure(displaycolumns=fields)

    def show_all_columns(self):
        self._show_all = not self._show_all
        self._fit_columns()

    def highlight(self, key):
        if key not in self.rows:
            return
        self._selected = key
        if self.tree.selection() != (key,):
            self.tree.selection_set(key)
        self.tree.focus(key)
        self.tree.see(key)

    def _select(self, _event=None):
        selected = self.tree.selection()
        if selected and selected[0] != self._selected:
            self._selected = selected[0]
            self.on_select(selected[0], True)

    def copy(self, _event=None):
        keys = self.tree.selection()
        if keys:
            header = '\t'.join(label for _, label in self.specs)
            body = '\n'.join('\t'.join(str(self.rows[k].get(f, '')) for f, _ in self.specs) for k in keys)
            self.clipboard_clear()
            self.clipboard_append(header + '\n' + body)
        return 'break'

    def _hide_tip(self, _event=None):
        if self._tip is not None:
            self._tip.destroy()
        self._tip, self._tip_cell = None, None

    def _hover(self, event):
        key, column = self.tree.identify_row(event.y), self.tree.identify_column(event.x)
        if not key or not column or column == '#0':
            self._hide_tip()
            return
        cell = (key, column)
        if cell == self._tip_cell:
            return
        self._hide_tip()
        index = int(column[1:]) - 1
        if not 0 <= index < len(self.specs):
            return
        field = self.tree.column(column, 'id')
        label = dict(self.specs)[field]
        value = self.rows[key].get(field)
        full = json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, (dict, list)) else repr(value)
        self._tip = tk.Toplevel(self)
        self._tip.overrideredirect(True)
        text = label + '\n' + full
        ttk.Label(self._tip, text=text, padding=8, wraplength=650).pack()
        self._tip.update_idletasks()
        x = min(event.x_root + 14, self.winfo_screenwidth() - self._tip.winfo_reqwidth() - 8)
        y = min(event.y_root + 18, self.winfo_screenheight() - self._tip.winfo_reqheight() - 8)
        self._tip.geometry(f'+{max(0,x)}+{max(0,y)}')
        self._tip_cell = cell


class ResultsView(ttk.Frame):
    def __init__(self, app, parent):
        super().__init__(parent)
        self.app = app
        self.view_id = 0
        self._identity = None
        self._thermal = {}
        self._thermal_identity = None
        self._selecting = False
        self._nav_visible = True
        self._drawer_visible = True
        self._sash = 120
        self.pack(fill='both', expand=True)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, sticky='ew', pady=(0, 4))
        ttk.Button(toolbar, text='☰ 流程', command=self.toggle_nav).pack(side='left', padx=(0, 8))
        ttk.Label(toolbar, text='工况').pack(side='left')
        app.operating_point_box = ttk.Combobox(toolbar, textvariable=app.operating_point_var, state='readonly', width=24)
        app.operating_point_box.pack(side='left', padx=(4, 8))
        app.operating_point_box.bind('<<ComboboxSelected>>', app._operating_point_changed)
        ttk.Label(toolbar, text='频偏').pack(side='left')
        app.result_offset_box = ttk.Combobox(toolbar, textvariable=app.result_offset_var, state='readonly', width=13)
        app.result_offset_box.pack(side='left', padx=(4, 8))
        app.result_offset_box.bind('<<ComboboxSelected>>', app._result_offset_changed)
        ttk.Label(toolbar, text='噪声').pack(side='left')
        app.noise_kind_box = ttk.Combobox(toolbar, textvariable=app.noise_kind_var, state='readonly', values=('both', 'flicker', 'thermal'), width=8)
        app.noise_kind_box.pack(side='left', padx=4)
        app.noise_kind_box.bind('<<ComboboxSelected>>', app._point_changed)
        exports = ttk.Menubutton(toolbar, text='导出…')
        menu = tk.Menu(exports, tearoff=False)
        exports.configure(menu=menu)
        for label, command in (('结果 JSON…', app._export_result_json), ('Summary CSV…', app._export_summary_csv),
                               ('当前谐波 CSV…', app._export_harmonics_csv), ('当前图 EPS…', self.export_eps), ('当前图 PNG…', self.export_png)):
            menu.add_command(label=label, command=command)
        exports.pack(side='right')
        self.chips = []
        chiprow = ttk.Frame(self)
        chiprow.grid(row=1, column=0, sticky='ew', pady=(0, 4))
        for i in range(6):
            chiprow.columnconfigure(i, weight=1, uniform='chip')
            var = tk.StringVar(value='—')
            label = ttk.Label(chiprow, textvariable=var, style='Chip.TLabel', padding=(8, 4), anchor='w')
            label.grid(row=0, column=i, sticky='ew', padx=(0, 4))
            self.chips.append(var)
        self.controls = ttk.Frame(self)
        self.controls.grid(row=2, column=0, sticky='ew', pady=(0, 4))
        self.title = tk.StringVar(value='00 / 总览')
        ttk.Label(self.controls, textvariable=self.title, font=app.heading_font).pack(side='left', padx=(0, 8))
        ttk.Button(self.controls, text='复位', command=lambda: self.chart.reset()).pack(side='right')
        ttk.Button(self.controls, text='表格 ▾', command=self.toggle_drawer).pack(side='right', padx=4)
        ttk.Button(self.controls, text='全部列 / 自适应', command=lambda: self.table.show_all_columns()).pack(side='right', padx=4)
        self.overlay = tk.BooleanVar(value=False)
        self.overlay_check = ttk.Checkbutton(self.controls, text='叠加显示 · Fig.14', variable=self.overlay, command=self.refresh)
        self.reference_slopes = tk.BooleanVar(value=True)
        self.slopes_check = ttk.Checkbutton(self.controls, text='−30 / −20 参考斜率', variable=self.reference_slopes, command=self.refresh)
        self.decompose = tk.BooleanVar(value=False)
        self.decompose_check = ttk.Checkbutton(self.controls, text='dc / delay / dynamic（单管）', variable=self.decompose, command=self.refresh)
        self.log_check = ttk.Checkbutton(self.controls, text='log 频偏', variable=app.log_offset_var, command=self.refresh)
        self.source_var = tk.StringVar()
        self.source_box = ttk.Combobox(self.controls, textvariable=self.source_var, state='readonly', width=20)
        self.source_box.bind('<<ComboboxSelected>>', lambda e: self.refresh())
        body = ttk.Frame(self)
        body.grid(row=3, column=0, sticky='nsew')
        body.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        self.nav = ttk.Frame(body, width=154)
        self.nav.grid(row=0, column=0, sticky='ns', padx=(0, 8))
        ttk.Label(self.nav, text='分析流程', style='Help.TLabel').pack(anchor='w', pady=(4, 8))
        self.nav_buttons = []
        for i, name in enumerate(VIEWS):
            button = ttk.Button(self.nav, text=f'{i:02}  {name}', width=21, command=lambda v=i: self.show(v))
            button.pack(fill='x', pady=(0, 8))
            self.nav_buttons.append(button)
        helpbox = ttk.Frame(self.nav)
        helpbox.pack(anchor='w',pady=16)
        for line in ('Ctrl+滚轮：缩放','中键拖动：平移','点图例：选中 / 显隐','悬停点选：联动','Ctrl+C：复制表行'):
            ttk.Label(helpbox,text=line,style='Help.TLabel').pack(anchor='w',pady=4)
        self.pane = ttk.Panedwindow(body, orient='vertical')
        self.pane.grid(row=0, column=1, sticky='nsew')
        self.plot_frame = ttk.Frame(self.pane)
        self.plot_frame.columnconfigure(0, weight=1)
        self.plot_frame.rowconfigure(1, weight=1)
        self.note = tk.StringVar(value='选择结果后显示分析过程')
        self.note_label = ttk.Label(self.plot_frame, textvariable=self.note, style='Help.TLabel', width=1, anchor='w')
        self.note_label.grid(row=0, column=0, sticky='ew', pady=(0, 4))
        self.chart = ProcessChart(self.plot_frame, on_select=self.select_key, on_open=self.show)
        self.chart.grid(row=1, column=0, sticky='nsew')
        self.quality_frame = ttk.Frame(self.plot_frame)
        self.quality_frame.rowconfigure(1, weight=1)
        self.quality_frame.columnconfigure(0, weight=3)
        self.quality_frame.columnconfigure(1, weight=2)
        self.quality_text = tk.StringVar()
        self.quality_note = ttk.Label(self.quality_frame, textvariable=self.quality_text, style='Help.TLabel', width=1, anchor='w')
        self.quality_note.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 8))
        self.plot_frame.bind('<Configure>', self._wrap_notes, add='+')
        self.quality_table = LinkedTable(self.quality_frame, self._quality_selected)
        self.quality_table.grid(row=1, column=0, sticky='nsew', padx=(0, 4))
        candidate_box = ttk.LabelFrame(self.quality_frame, text='AM/PM phase_candidates · chosen 高亮')
        candidate_box.grid(row=1, column=1, sticky='nsew')
        candidate_box.rowconfigure(0, weight=1)
        candidate_box.columnconfigure(0, weight=1)
        self.candidates = LinkedTable(candidate_box, lambda k,c: None)
        self.candidates.grid(row=0, column=0, sticky='nsew')
        ttk.Label(candidate_box, text='选择候选只查看证据；不会重算或改变 chosen。', style='Help.TLabel', wraplength=270).grid(row=1, column=0, sticky='ew')
        self.table = LinkedTable(self.pane, self.select_key)
        self.pane.add(self.plot_frame, weight=5)
        self.pane.add(self.table, weight=1)
        self.readout = tk.StringVar(value='表格与图共用采样 key；悬停单元格查看完整精度')
        self.footer = ttk.Label(self, textvariable=self.readout, style='Help.TLabel', anchor='w')
        self.footer.grid(row=4, column=0, sticky='ew', pady=(4, 0))
        self.bind('<Map>', self._initial_sash, add='+')
        self._sash_initialized = False
        self._sash_after = None
        self.bind('<Destroy>', self._cleanup, add='+')
        self._build_presenter_contract()
        self.quality_table.tree.bind('<Double-1>', self._show_metric)
        self.quality_table.tree.bind('<Return>', self._show_metric)

    def _wrap_notes(self, event):
        # Follow the allocated parent width, never the label's requested width.
        # width=1 keeps long text out of horizontal geometry negotiation; a
        # wrap change may alter height but cannot feed back into requested width.
        width = max(100, event.width - 5)
        for label in (self.note_label, self.quality_note):
            if int(label.cget('wraplength') or 0) != width:
                label.configure(wraplength=width)

    def _initial_sash(self, _event=None):
        if not self._sash_initialized and self.pane.winfo_height() > 200:
            self.pane.sashpos(0, self.pane.winfo_height() - 120)
            self._sash_initialized = True
            self._sash_after = self.after(80, self._settle_sash)

    def _settle_sash(self):
        self._sash_after = None
        if self._drawer_visible:
            self.pane.sashpos(0, max(100, self.pane.winfo_height()-110))

    def _cleanup(self, event):
        if event.widget == self and self._sash_after is not None:
            self.after_cancel(self._sash_after)
            self._sash_after = None

    def _build_presenter_contract(self):
        """Keep existing result/export/state presenter contracts during UI migration.

        These data-bearing widgets are not geometry-managed. The new chart
        consumes the presenter's already scoped series, avoiding a second
        implementation of phase-noise scope and reference selection.
        """
        app = self.app
        host = ttk.Frame(self)
        self.presenter = host
        app.point_box = ttk.Combobox(host, textvariable=app.point_var)
        for name, cols in (
            ('summary_tree', ('point', 'offset', 'f0', 'calculated', 'spectre', 'dynamic', 'pair')),
            ('harmonic_tree', ('k', 'mdc', 'mdyn', 'a', 'delta', 'qr', 'qi', 'qmag', 'cumr', 'cumi', 'hdbc', 'cdbc')),
            ('noise_contribution_tree', ('label', 'device', 'mechanism', 'simulated', 'calculated', 'fraction', 'psd')),
            ('metric_values_tree', ('field', 'value', 'unit'))):
            setattr(app, name, ttk.Treeview(host, columns=cols, show='headings'))
        app.summary_tree.heading('spectre', text='VCO 总仿真相噪 (dBc/Hz)')
        app._bind_metric_columns(app.harmonic_tree, ('k', 'Mdc_mag_A_rtHz', 'Mdyn_mag_A_rtHz', 'A_k', 'delta_k_deg', 'Q_real_V_rtHz', 'Q_imag_V_rtHz', 'Q_mag_V_rtHz', 'cum_real_V_rtHz', 'cum_imag_V_rtHz', 'pair_harmonic_equivalent_dBc_Hz', 'pair_cumulative_dBc_Hz'))
        app.harmonic_tree.bind('<<TreeviewSelect>>', app._harmonic_selected)
        app.simulated_noise_pie = ContributionBars(host, title='HBnoise', on_select=app._select_contribution)
        app.calculated_noise_pie = ContributionBars(host, title='ISF', on_select=app._select_contribution)
        app.isf_chart = LineChart(host)
        app.qk_chart = PhasorChart(host, on_select=app._select_harmonic)
        app.phase_noise_chart = LineChart(host)
        app.phase_error_chart = LineChart(host)
        app.harmonic_noise_chart = LineChart(host)
        app.harmonic_curve_box = ttk.Combobox(host, textvariable=app.harmonic_curve_var)
        app._phase_menu = tk.Menu(host, tearoff=False)
        app._phase_visibility = {}
        app._phase_full_x, app._phase_view_x, app._phase_data_key = None, None, None
        app._phase_log_x = True
        app._phase_note = tk.StringVar()
        for chart in (app.phase_noise_chart, app.phase_error_chart):
            chart.enable_x_navigation(app._navigate_phase_view)
        from ..core.metrics import metric_catalog
        from .app import LEGACY_DEVICE_COMPARISON_FIELDS
        app._metric_choices = {f'{d["label"]} · {key}': key for key, d in metric_catalog().items() if key not in LEGACY_DEVICE_COMPARISON_FIELDS}
        app.result_content = self

    def set_palette(self, palette):
        self.chart.set_palette(palette)
        self.app.style.configure('Chip.TLabel', background=palette['grid'], foreground=palette['text'])
        style = self.app.style
        family = self.app.body_font.actual('family')
        style.configure('Result.TLabel', font=(family, 10), background=palette['background'], foreground=palette['text'])
        style.configure('ResultHelp.TLabel', font=(family, 10), background=palette['background'], foreground=palette['muted'])
        style.configure('Chip.TLabel', font=(family, 10))
        style.configure('Result.TButton', font=(family, 10), padding=(8, 4))
        style.configure('ResultActive.TButton', font=(family,10,'bold'), padding=(8,4), background=palette['grid'], foreground=palette['accent'])
        style.configure('Result.TCheckbutton', font=(family, 10), padding=4)
        style.configure('Result.TMenubutton', font=(family, 10), padding=(8, 4))
        style.configure('Result.Treeview', font=(family, 10), rowheight=25)
        style.configure('Result.Treeview.Heading', font=(family, 10, 'bold'), padding=(4, 4))
        def compact(widget):
            for child in widget.winfo_children():
                kind = child.winfo_class()
                if kind == 'TLabel' and child.cget('style') != 'Chip.TLabel':
                    child.configure(style='ResultHelp.TLabel' if 'Help' in child.cget('style') else 'Result.TLabel')
                elif kind in ('TButton', 'TCheckbutton', 'TMenubutton'):
                    child.configure(style='Result.' + kind)
                elif kind == 'TCombobox':
                    child.configure(font=(family, 10))
                elif kind == 'Treeview':
                    child.configure(style='Result.Treeview')
                compact(child)
        compact(self)
        for i, button in enumerate(self.nav_buttons):
            button.configure(style='ResultActive.TButton' if i==self.view_id else 'Result.TButton')
        for table in (self.table, self.quality_table, self.candidates):
            table.tree.tag_configure('chosen', background=palette['selection'], foreground=palette['background'])

    def toggle_nav(self):
        self._nav_visible = not self._nav_visible
        self.nav.grid() if self._nav_visible else self.nav.grid_remove()

    def toggle_drawer(self):
        if self._drawer_visible:
            self._sash = self.pane.winfo_height() - self.pane.sashpos(0)
            self.pane.forget(self.table)
        else:
            self.pane.add(self.table, weight=1)
            self.update_idletasks()
            self.pane.sashpos(0, max(100, self.pane.winfo_height() - self._sash))
        self._drawer_visible = not self._drawer_visible

    def clear(self):
        self.chart.set_panels([], reset=True)
        self.table.fill([], [])
        self.quality_table.fill([], [])
        self.candidates.fill([], [])
        self._identity, self._thermal_identity = None, None
        self.note.set('尚无当前任务结果')
        for var in self.chips:
            var.set('—')

    def show(self, view_id):
        self.view_id = int(view_id)
        self.chart.limits.clear()
        self.chart.hidden.clear()
        self.refresh()

    def _thermal_data(self, result, point):
        identity = (self.app.current_result_job_id, point)
        if identity != self._thermal_identity:
            job_dir = Path(self.app.controller.workspace) / 'jobs' / identity[0]
            self._thermal = load_thermal_coefficients(result, point, job_dir)
            self._thermal_identity = identity
        return self._thermal

    def refresh(self):
        app, view = self.app, self.app.current_result_view
        if not view or not app.current_result:
            return
        point, kind = app.point_var.get(), app.noise_kind_var.get()
        identity = (app.current_result_job_id, app.operating_point_var.get() if self.view_id in (0,3,6) else point, kind, self.view_id, app.log_offset_var.get())
        reset = identity != self._identity
        self._identity = identity
        thermal = self._thermal_data(app.current_result, point)
        process = process_projection(app.current_result, point, thermal, kind)
        sources = thermal.get('sources', [])
        if self.view_id != 7:
            self.source_box.configure(values=[s['id'] for s in sources])
            if self.source_var.get() not in [s['id'] for s in sources]:
                self.source_var.set(sources[0]['id'] if sources else '')
        if sources and kind in ('thermal', 'both'):
            # View 07 selects a display name; workpoint views select a source id.
            selected = next((s for s in sources if s['id'] == self.source_var.get()), sources[0])
            series = thermal_workpoint_series(selected,process['rows'])
            process['panels'][1]['series'] = ([series] if kind == 'thermal' else process['panels'][1]['series'][:2] + [series])
        harmonic = harmonic_projection(view)
        offsets = offset_projection(app.current_result, app.operating_point_var.get(), app.phase_noise_chart._series,
                                    app.phase_error_chart._series, self.decompose.get() and kind == 'flicker')
        diagnostic = stored_thermal_diagnostic(app.current_result,point)
        unverified_thermal = bool(diagnostic['sources']) and view.get('selected_summary',{}).get('thermal_noise',{}).get('status') != 'available'
        if unverified_thermal and kind in ('thermal','both'):
            offsets['series'].append(stored_thermal_series(app.current_result,offsets['rows']))
        labels = {'calculated': 'ISF 合计' if kind == 'both' else 'ISF fn · 双管', 'flicker': 'ISF fn · 双管', 'thermal': 'ISF thermal', 'spectre': 'HBnoise 全部源', 'matched_prediction': 'ISF 同口径', 'matched_reference': 'HBnoise 同口径', 'matched_error': 'Δ 同口径'}
        short = {'calculated': 'ISFΣ' if kind == 'both' else 'ISFfn', 'flicker':'fn', 'thermal':'th', 'spectre':'HB全', 'matched_prediction':'ISF匹配', 'matched_reference':'HB匹配', 'matched_error':'Δ'}
        for series in offsets['series']+offsets['error_series']:
            series['name'] = labels.get(series['id'],series['name'])
            series['short_name'] = short.get(series['id'],series['name'])
            series['unit'] = 'dB' if series['id']=='matched_error' else 'dBc/Hz'
        contrib = contribution_projection(view,diagnostic if kind!='flicker' else None)
        bands = band_noise_projection(app.current_result, app.operating_point_var.get(), kind, self.reference_slopes.get())
        thermal_bands = thermal_band_projection(view, diagnostic)
        metrics = view.get('metrics', {})
        calc, reference = finite_number(metrics.get('calculated_dbc')), finite_number(metrics.get('spectre_dbc'))
        missing_calculation = ('合计缺 thermal' if kind == 'both' and (view.get('selected_summary', {}).get('thermal_noise') or {}).get('status') != 'available' else '缺计算')
        for var, text in zip(self.chips, (
            'f₀  ' + engineering(metrics.get('frequency_hz'), 'Hz'),
            'offset  ' + engineering(metrics.get('offset_hz'), 'Hz'),
            'ISF  ' + (f'{calc:.4g} dBc/Hz' if calc is not None else missing_calculation),
            'HBnoise  ' + (f'{reference:.4g} dBc/Hz' if reference is not None else '缺参考'),
            'Δ总参考  ' + (f'{calc-reference:+.3g} dB' if calc is not None and reference is not None else '缺同点数值'),
            'SIGNOFF' if view.get('signoff') else 'REPLAY · NON-SIGNOFF')):
            var.set(text)
        self.title.set(f'{self.view_id:02} / {VIEWS[self.view_id]}')
        def available(panel):
            return any(any(p is not None and finite_number(p[1]) is not None for p in series['points']) for series in panel['series'])
        process_ready = all(available(p) for p in process['panels'])
        ready = [bool(contrib['rows']) and bool(harmonic['phasors']) and available(process['panels'][2]) and available(process['panels'][3]),
                 process_ready, all(available(p) for p in harmonic['panels']),
                 any(any(p is not None and finite_number(p[1]) is not None for p in s['points']) for s in offsets['error_series']),
                 bool(contrib['rows']) and (kind=='flicker' or bool(contrib['band_rows'])), bool(view.get('selected_summary')), bool(bands['table_rows']), bool(thermal_bands['rows'])]
        for i, button in enumerate(self.nav_buttons):
            button.configure(text=f'{i:02}  {({3: "相噪对比",5: "指标质量"}).get(i,VIEWS[i])}  {"就绪" if ready[i] else "缺数据"}', style='ResultActive.TButton' if i==self.view_id else 'Result.TButton')
        for widget in (self.decompose_check, self.log_check, self.source_box, self.overlay_check, self.slopes_check):
            widget.pack_forget()
        if self.view_id in (0, 3):
            self.log_check.pack(side='left')
        if self.view_id == 1:
            self.overlay_check.pack(side='left')
        if self.view_id in (0,6):
            self.slopes_check.pack(side='left')
        if self.view_id == 3:
            self.decompose_check.pack(side='left')
        if (self.view_id == 1 and kind in ('thermal', 'both')) or self.view_id == 7:
            self.source_box.pack(side='left')
        scope = noise_scope_note(view, kind)
        if unverified_thermal and kind!='flicker':
            scope += f' · 旧预算 {len(diagnostic["sources"])} 源仅作未验证诊断（可能含阵列）；不参与当前计算 / 误差 / SIGNOFF'
        note = scope
        phase_panels = [
            {'title': '相噪 vs 频偏 · ISF / HBnoise', 'ylabel': 'L (dBc/Hz)', 'xlabel': '频偏 (Hz)', 'group': 'offset', 'series': offsets['series'], 'log_x': app.log_offset_var.get(), 'reason': '缺少同口径计算 / 参考；缺值不连线'},
            {'title': '计算 − 同口径 HBnoise', 'ylabel': 'Δ (dB)', 'xlabel': '频偏 (Hz)', 'group': 'offset', 'series': offsets['error_series'], 'log_x': app.log_offset_var.get(), 'zero': True, 'reason': '未保存同口径 HBnoise 参考；不能用 VCO 总噪声代替'}]
        # Share full x range including missing y samples across both panels.
        hz = [finite_number(r.get('hz')) for r in offsets['rows']]
        hx = [v for v in hz if v is not None and (not app.log_offset_var.get() or v > 0)]
        if hx:
            import math
            xs = [math.log10(v) if app.log_offset_var.get() else v for v in hx]
            lo, hi = min(xs), max(xs)
            margin = (hi-lo)*.04 if hi != lo else .1 if app.log_offset_var.get() else max(.1,lo*.05)
            for p in phase_panels:
                p['xlimits'] = (lo-margin, hi+margin)
                if any(any(v is not None and finite_number(v[1]) is not None for v in s['points']) for s in p['series']):
                    p['reason'] = ''
        phasor = {'kind': 'phasor', 'title': 'ΣQk 复平面 · 单管原值', 'phasors': harmonic['phasors']}
        source_panel = {'kind': 'sources', 'title': '单频点噪声贡献 · 聚合对比 / 源明细', 'rows': contrib['rows']}
        if self.view_id == 0:
            panels = [dict(bands['panels'][0], layout=(0, 0, .62, .62), open=6),
                      dict(source_panel, layout=(.62, 0, 1, .62), open=4),
                      dict(phasor, layout=(0, .62, .55, 1), open=2)]
            for i, p in enumerate((process['panels'][2], process['panels'][3])):
                panels.append(dict(p, layout=(.55, .6 + i*.2, 1, .8+i*.2), statistics={}, open=1))
            rows, specs = self._offset_table(offsets)
            note = bands['note'] + ' · 点击缩略图展开'
        elif self.view_id == 1:
            panels = process['panels']
            columns = ('sample', 'phase', 'VG_V', 'VD_V', 'VS_V', 'VB_V', 'ID_main_A', 'ID_replica_A', 'm_DC_A_rtHz', 'm_dyn_A_rtHz', 'isf_i', 'isf_q', 'product_i', 'product_q')
            labels = ('sample', 'φ (周期)', 'VG (V)', 'VD (V)', 'VS (V)', 'VB (V)', 'ID_main (A)', 'ID_replica (A)', 'm_DC (A/√Hz)', 'm_dyn (A/√Hz)', 'Γ-I (C^-1)', 'Γ-Q (C^-1)', 'ΓI×m (s^-1/√Hz)', 'ΓQ×m (s^-1/√Hz)')
            rows, specs = process['rows'], list(zip(columns, labels))
            note = ('flicker 目标单器件周期原值；Γ 采用载波参考 · DC / RMS 为整周期梯形积分显示诊断')
            if kind in ('thermal', 'both'):
                note += ' · thermal 保留原始源采样，相位按最近工作点联动；④仍为 flicker Γ×m_dyn'
                if not sources:
                    note += ' · ' + thermal['reason']
            if process['derived']:
                note += ' · 派生诊断：由 canonical 谐波重建，未覆盖原始输入'
            if self.overlay.get():
                overlay = isf_overlay_projection(process, view.get('selected_summary', {}))
                panels, note = overlay['panels'], overlay['note']
        elif self.view_id == 2:
            panels = []
            for i, p in enumerate(harmonic['panels'][-2:]):
                panels.append(dict(p, layout=(0, i/2, .66, (i+1)/2)))
            panels.append(dict(phasor, layout=(.66, 0, 1, 1)))
            rows = harmonic['rows']
            specs = [('k','k'), ('fraction_pct','投影占比 (%)'), ('cancellation','相消干涉'), ('mdc_mag','|Mdc| (A/√Hz)'), ('mdyn_mag','|Mdyn| (A/√Hz)'), ('amplitude','A_k (1)'), ('delta_deg','δ_k (°)'), ('q_real','Q Re (V/√Hz)'), ('q_imag','Q Im (V/√Hz)'), ('q_mag','|Qk| (V/√Hz)'), ('harmonic_dbc','双管单阶 (dBc/Hz)'), ('cumulative_dbc','双管累计 (dBc/Hz)')]
            note = HARMONIC_NOTE + ' · 所选频偏 '+engineering(metrics.get('offset_hz'),'Hz')
        elif self.view_id == 3:
            panels = [dict(phase_panels[0], layout=(0, 0, 1, .7)), dict(phase_panels[1], layout=(0, .7, 1, 1))]
            rows, specs = self._offset_table(offsets)
        elif self.view_id == 4:
            panels = [dict(source_panel, layout=(0, 0, .62, 1)),
                      {'title': 'thermal B_m · 原存储未验证诊断' if contrib.get('diagnostic_bands') else 'thermal 独立输入频带 B_m', 'ylabel': 'PSD (V²/Hz)', 'xlabel': '输入频带 m', 'series': contrib['band_series'], 'layout': (.62, 0, 1, 1), 'reason': ('当前筛选为 flicker；请切换 thermal / both 查看原存储频带诊断。flicker 使用 Qk 相干累计。' if kind=='flicker' and diagnostic['sources'] else contrib['reason'])}]
            rows = contrib['rows']
            specs = [('label','源 / 聚合对比'), ('simulated_dbc','HBnoise (dBc/Hz)'), ('calculated_dbc','ISF (dBc/Hz)'), ('fraction_pct','占总仿真 (%)'), ('scope','建模范围'), ('device','器件'), ('mechanism','机制'), ('psd_V2_Hz','PSD (V²/Hz)')]
            if contrib.get('diagnostic_bands'):
                specs.insert(5,('stored_thermal_dbc','旧预算诊断·未验证 (dBc/Hz)'))
            if any(r.get('kind')=='aggregate' for r in rows):
                note += ' · 聚合对比置顶，与源明细重叠、不重复计数；标为“计入对比”的 fn 源参与该聚合。'
            else:
                note += ' · 独立源与输入频带按功率相加；未建模源不参与 ISF 计算。'
        elif self.view_id == 6:
            panels, rows, specs = bands['panels'], bands['table_rows'], bands['specs']
            note = bands['note']
        elif self.view_id == 7:
            panels, rows = thermal_bands['panels'], thermal_bands['rows']
            self._thermal_band_series = list(panels[0]['series'])
            options = [s['name'] for s in self._thermal_band_series]
            self.source_box.configure(values=options)
            if self.source_var.get() not in options:
                self.source_var.set(options[0] if options else '')
            panels[0]['series'] = [s for s in self._thermal_band_series if s['name']==self.source_var.get()]
            specs = [('device','源'),('mechanism','机制'),('n','|m|'),('dbc','单带 (dBc/Hz)'),('psd','PSD (V²/Hz)')]
            note = thermal_bands['note'] + ' · 每次显示一个源；下拉切源，选中表行也会切源（全部数据保留）'
        else:
            panels = []
            self.quality_text.set(app.result_quality_var.get().replace('N/A','缺数据'))
            raw = view.get('selected_summary', {})
            from .app import LEGACY_DEVICE_COMPARISON_FIELDS
            metrics_rows = [{'key': 'metric:' + k, 'field': k, 'value': v} for k, v in raw.items() if k not in LEGACY_DEVICE_COMPARISON_FIELDS]
            point_data = app.current_result.get('points',{}).get(point,{})
            first_harmonic = next(iter(point_data.get('harmonics',[])),{})
            metrics_rows.extend({'key':'harmonic-metric:'+k,'field':k,'value':v} for k,v in first_harmonic.items() if k not in LEGACY_DEVICE_COMPARISON_FIELDS)
            from ..core.metrics import metric_definition
            for r in metrics_rows:
                definition = metric_definition(r['field'])
                r['label'] = definition['label'] if definition else r['field']
                r['unit'] = definition['unit'] if definition else '—'
            self.quality_table.fill(metrics_rows, [('label','全部指标与质量'), ('value','值'), ('unit','单位')])
            point_data = app.current_result.get('points', {}).get(point, {})
            candidates = [dict(r, key='candidate:' + str(i)) for i,r in enumerate(point_data.get('phase_candidates', []))]
            self.candidates.fill(candidates, [('rank','rank'), ('chosen','chosen'), ('signs','signs'), ('score','score'), ('phase_cost','phase cost'), ('delay_cost','delay cost'), ('continuity_cost','continuity')])
            rows, specs = self._offset_table(offsets)
            note = '运行完成不代表验证通过；REPLAY 不授予 SIGNOFF。双击指标可查看定义；原始结构化值支持悬停 / 复制。'
        if self.view_id in (0,3) and len(offsets['rows'])==1:
            note += ' · 本工况仅保存 1 个频偏，不插值生成扫频曲线'
        self.note.set(note)
        if self.view_id == 5:
            self.chart.grid_remove()
            self.quality_frame.grid(row=1, column=0, sticky='nsew')
        else:
            self.quality_frame.grid_remove()
            self.chart.grid()
        self.chart.key_positions = {r['key']: r['phase'] for r in process['rows']} if self.view_id in (0, 1) else {}
        self.chart.set_panels(panels, reset=reset)
        self.table.fill(rows, specs)
        self.readout.set('表格与图共用 key · 悬停显示同步读数 · 单元格悬停完整精度 · Ctrl+C 复制（原始单位）')
        if self.view_id == 3:
            key = 'offset:' + point
            self.table.highlight(key)
            self.chart.highlight(key)
        self._initial_sash()

    _offset_table = staticmethod(offset_table_projection)

    def select_key(self, key, clicked=False):
        if not key or self._selecting:
            return
        self._selecting = True
        try:
            self.table.highlight(key)
            if self.view_id==7:
                selected=[s for s in getattr(self,'_thermal_band_series',[]) if key in s['keys']]
                if selected:
                    self.chart.panels[0]['series']=selected
                    self.source_var.set(selected[0]['name'])
            self.chart.highlight(key)
            if self.view_id in (2,6,7):
                self.chart.schedule()
            if self.view_id==4 and key in self.table.rows:
                row=self.table.rows[key]
                self.readout.set(key+' · '+row.get('scope','')+' · HBnoise '+engineering(row.get('simulated_dbc'),'dBc/Hz')+' / ISF '+engineering(row.get('calculated_dbc'),'dBc/Hz'))
            else:
                row=self.table.rows.get(key,{})
                if self.view_id==2:
                    text='k='+str(row.get('k'))+' · 单阶 '+engineering(row.get('harmonic_dbc'),'dBc/Hz')+' · 投影占比 '+engineering(row.get('fraction_pct'),'%')
                    if row.get('cancellation'):text+=' · 相消干涉：|Qk| > |ΣQk|'
                elif self.view_id==6:
                    text=row.get('label',key)+' · 各频偏相噪和占比见表格；右键表格显示全部列'
                elif self.view_id==1:
                    fields=(('phase','φ'),('m_DC_A_rtHz','m_DC'),('m_dyn_A_rtHz','m_dyn'),('isf_i','Γ-I'),('product_i','Γ×m_dyn'))
                    text=key+' · '+' / '.join(label+' '+engineering(row.get(field)) for field,label in fields)
                else:text='选中 '+key+' · 单元格悬停查看完整精度'
                self.readout.set(text)
            if self.view_id == 4:
                self.chart.schedule()  # selected low-ranked source enters viewport
            if clicked and self.view_id in (0,3) and key.startswith('offset:'):
                point = key[len('offset:'):]
                if point != self.app.point_var.get():
                    label = next((k for k,v in self.app._offset_point_choices.items() if v == point), None)
                    if label:
                        self.app.result_offset_var.set(label)
                        self.app._result_offset_changed()
                        self.table.highlight(key)
                        self.chart.highlight(key)
        finally:
            self._selecting = False

    def _quality_selected(self, key, clicked):
        if clicked:
            self.readout.set('指标 ' + key.removeprefix('metric:') + ' · 双击查看定义；Ctrl+C 复制完整值')

    def _show_metric(self, _event=None):
        selection = self.quality_table.tree.selection()
        if selection:
            self.app._show_metric(self.quality_table.rows[selection[0]]['field'])

    def export_eps(self):
        path = filedialog.asksaveasfilename(parent=self, title='导出当前过程图', defaultextension='.eps', filetypes=(('EPS', '*.eps'),))
        if path:
            try:
                self.chart.postscript(file=path, colormode='color', width=self.chart.winfo_width(), height=self.chart.winfo_height())
            except (OSError, tk.TclError) as exc:
                messagebox.showerror('导出失败', str(exc), parent=self)

    def export_png(self):
        path = filedialog.asksaveasfilename(parent=self, title='导出当前过程图', defaultextension='.png', filetypes=(('PNG', '*.png'),))
        if path:
            try:
                from PIL import ImageGrab
                x,y = self.chart.winfo_rootx(), self.chart.winfo_rooty()
                ImageGrab.grab(bbox=(x,y,x+self.chart.winfo_width(),y+self.chart.winfo_height())).save(path)
            except (OSError, tk.TclError) as exc:
                messagebox.showerror('导出失败', str(exc), parent=self)
