"""English display contract across native views, dialogs and packaged Web text."""
import ast
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest import mock

CJK = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]')
ROOT = Path(__file__).resolve().parents[1]


def display_texts(widget):
    """Include unmapped tabs, menus, table headings/cells and Canvas labels."""
    if isinstance(widget, (tk.Tk, tk.Toplevel)):
        yield widget.title()
    for option in ('text', 'label', 'values'):
        try:
            yield str(widget.cget(option))
        except tk.TclError:
            pass
    if isinstance(widget, tk.Text):
        yield widget.get('1.0', 'end-1c')
    if isinstance(widget, tk.Canvas):
        for item in widget.find_all():
            if widget.type(item) == 'text':
                yield widget.itemcget(item, 'text')
    if isinstance(widget, ttk.Notebook):
        for tab in widget.tabs():
            yield widget.tab(tab, 'text')
    if isinstance(widget, ttk.Treeview):
        for column in widget['columns']:
            yield widget.heading(column, 'text')
        def rows(parent=''):
            for row in widget.get_children(parent):
                yield str(widget.item(row, 'values'))
                yield from rows(row)
        yield from rows()
    if isinstance(widget, tk.Menu):
        end = widget.index('end')
        for entry in range((end if end is not None else -1) + 1):
            if widget.type(entry) not in ('separator', 'tearoff'):
                yield str(widget.entrycget(entry, 'label'))
    for child in widget.winfo_children():
        yield from display_texts(child)


class EnglishResourcesTests(unittest.TestCase):
    def test_packaged_ui_and_core_have_no_cjk_outside_legacy_mapping(self):
        for path in (ROOT / 'isf_tool').rglob('*'):
            if path.suffix not in ('.py', '.js', '.html', '.css', '.json'):
                continue
            source = path.read_text(encoding='utf-8')
            if path.name == 'ui_state.py':
                tree = ast.parse(source)
                lines = source.splitlines()
                for node in tree.body:
                    if isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id in
                        {'LEGACY_HARMONIC_CURVES', 'LEGACY_CUMULATIVE_LABEL'}
                        for target in node.targets
                    ):
                        lines[node.lineno - 1:node.end_lineno] = [''] * (node.end_lineno - node.lineno + 1)
                source = '\n'.join(lines)
            self.assertIsNone(CJK.search(source), str(path.relative_to(ROOT)))
        for path in (ROOT / 'isf_tool/ui').glob('*.html'):
            self.assertIn('<html lang="en">', path.read_text(encoding='utf-8'))
        for name in ('error_catalog.json', 'metric_catalog.json'):
            json.loads((ROOT / 'isf_tool/ui' / name).read_text(encoding='utf-8'))


@unittest.skipUnless(os.environ.get('ISF_GUI_TEST') == '1', 'requires an explicit GUI display')
class EnglishNativeTests(unittest.TestCase):
    def setUp(self):
        from isf_tool.tk.app import IsfApplication
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = tk.Tk()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(str(args[1]))
        self.controller = mock.Mock(workspace=Path(self.workspace.name))
        with mock.patch.object(IsfApplication, '_submit'), mock.patch.object(IsfApplication, '_refresh_jobs'):
            self.app = IsfApplication(self.root, self.controller)
        self.root.geometry('1600x1000')
        self.root.update()

    def tearDown(self):
        self.app.queue.close()
        for timer in self.root.tk.call('after', 'info'):
            self.root.tk.call('after', 'cancel', timer)
        self.root.destroy()
        self.assertEqual([], self.errors)

    def assert_english(self):
        for text in display_texts(self.root):
            self.assertIsNone(CJK.search(text), text)

    def test_all_tabs_eight_views_noise_kinds_overlay_and_dialog_titles(self):
        from isf_tool.example_data import result
        from isf_tool.core.metrics import metric_catalog
        from isf_tool.tk.results_view import VIEWS
        self.assertEqual(8, len(VIEWS))
        self.assert_english()  # Empty state, including every constructed tab.
        app = self.app
        app.current_job_id = 'synthetic'
        app.current_job = {'id': 'synthetic', 'status': 'succeeded'}
        app._result_loaded(result(), 'synthetic')
        app._open_catalog()
        for tab in app.notebook.tabs():
            app.notebook.select(tab)
            self.root.update()
            self.assert_english()
        for tab in app.advanced_tabs.tabs():
            app.advanced_tabs.select(tab)
            self.root.update()
            self.assert_english()
        app.notebook.select(app.result_tab)
        for kind in ('both', 'flicker', 'thermal'):
            app.noise_kind_var.set(kind)
            app._point_changed()
            for view in range(8):
                app.results_view.show(view)
                self.root.update()
                self.assert_english()
                self.assertTrue(app.results_view.chart.find_all() or view == 5)
        for button in app.results_view.nav_buttons:
            self.assertGreaterEqual(button.winfo_width(), app.body_font.measure(button.cget("text")) + 12)
        app.results_view.overlay.set(True)
        app.results_view.show(1)
        self.root.update()
        self.assert_english()
        app._show_offset_points()
        self.root.update()
        self.assert_english()
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel):
                child.destroy()

        # Real dialog entry points; intercept modal/native calls so no click is
        # needed and no filesystem export or runtime probe is performed.
        dialogs = []
        def modal(title=None, message=None, **kwargs):
            dialogs.append((title or kwargs.get('title', ''), message or ''))
            return ''
        with ExitStack() as stack:
            for name in ('showinfo', 'showwarning', 'showerror'):
                stack.enter_context(mock.patch('isf_tool.tk.app.messagebox.' + name, side_effect=modal))
            stack.enter_context(mock.patch('isf_tool.tk.app.simpledialog.askstring', side_effect=modal))
            stack.enter_context(mock.patch('isf_tool.tk.app.filedialog.asksaveasfilename', side_effect=modal))
            stack.enter_context(mock.patch('isf_tool.tk.app.filedialog.askopenfilename', side_effect=modal))
            stack.enter_context(mock.patch('isf_tool.tk.app.filedialog.askdirectory', side_effect=modal))
            app._show_metric(next(iter(metric_catalog())))
            app._show_metric('unknown_extension')
            app._save_state_dialog()
            app._show_preflight_issues()
            app._show_error(ValueError('Synthetic diagnostic'))
            app._choose_directory('project_root')
            app._choose_file('Open configuration')
            app._export_result_json()
            app._export_summary_csv()
            app._export_harmonics_csv()
            app.results_view.export_eps()
            app.results_view.export_png()
        self.assertGreaterEqual(len(dialogs), 10)
        for title, message in dialogs:
            self.assertIsNone(CJK.search(str(title)), str(title))
            self.assertIsNone(CJK.search(str(message)), str(message))

        # Exercise the saved-state Toplevel, including its title and controls.
        self.controller.list_states.return_value = {'states': [
            {'id': 'sample.json', 'name': 'Synthetic state', 'saved_at': '2026-01-01'}]}
        app._submit = lambda label, action, success, **kw: success(action())
        app._load_state_dialog()
        self.root.update()
        self.assertTrue(any(isinstance(w, tk.Toplevel) for w in self.root.winfo_children()))
        self.assert_english()
        with mock.patch('isf_tool.core.environment_report.inspect_environment', return_value={'status': 'ready'}), \
             mock.patch('isf_tool.core.environment_report.format_environment_report', return_value='Synthetic environment. No simulator started.'):
            app._show_environment()
        self.root.update()
        titles = [w.title() for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)]
        self.assertIn('Load state', titles)
        self.assertIn('ISF environment check', titles)
        self.assert_english()
