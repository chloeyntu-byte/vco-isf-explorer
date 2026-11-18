import os
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import font as tkfont, ttk
import unittest
from unittest import mock

from isf_tool.example_data import result
from isf_tool.tk.app import IsfApplication
from isf_tool.tk.sweep_view import choose_points


@unittest.skipUnless(os.environ.get('ISF_GUI_TEST') == '1', 'requires a private GUI display')
class SweepGuiTests(unittest.TestCase):
    def test_select_table_row_step_into_later_view_and_choose_run_subset(self):
        root = tk.Tk()
        errors = []
        root.report_callback_exception = lambda *args: errors.append(args[1])
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(IsfApplication, '_submit'), mock.patch.object(IsfApplication, '_refresh_jobs'):
                app = IsfApplication(root, mock.Mock(workspace=Path(directory)))
            try:
                root.geometry('1280x800')
                app.current_job_id = 'sweep'
                app.current_job = {'id': 'sweep', 'status': 'succeeded'}
                app._result_loaded(result(sweep=True), 'sweep')
                root.update()
                ui = app.results_view
                table = ui.overview.table.tree
                self.assertTrue(table.winfo_ismapped())
                self.assertEqual(len(table.get_children()), 6)
                target = table.get_children()[-1]
                table.selection_set(target)
                root.update()
                self.assertEqual(app.operating_point_var.get(), target)
                ui.show(3)
                ui.step_point(-1)
                root.update()
                self.assertNotEqual(app.operating_point_var.get(), target)
                self.assertTrue(ui.point_nav.winfo_ismapped())
                self.assertIn('VBIAS=', ui.point_identity.get())
                selection = []
                dialog = choose_points(root, app._operating_points, [10000, 100000],
                                       selection.extend, lambda: None)
                dialog.geometry('655x345')
                root.update()
                body = dialog.winfo_children()[0]
                frame = body.winfo_children()[1]
                tree = next(w for w in frame.winfo_children() if w.winfo_class() == 'Treeview')
                self.assertEqual(tuple(tree['displaycolumns']), ('corner', 'variables'))
                tree.selection_set(target)
                root.update()
                buttons = body.winfo_children()[-1]
                start = next(w for w in buttons.winfo_children() if w.cget('text') == 'Analyze selected')
                self.assertTrue(start.winfo_ismapped())
                self.assertLessEqual(start.winfo_rooty() + start.winfo_reqheight(),
                                     dialog.winfo_rooty() + dialog.winfo_height())
                start.invoke()
                self.assertEqual(selection, [target])
            finally:
                app.queue.close()
                root.destroy()
        self.assertFalse(errors, errors)

    def test_single_overview_cards_and_sweep_headings_at_release_sizes(self):
        root = tk.Tk()
        errors = []
        root.report_callback_exception = lambda *args: errors.append(args[1])
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(IsfApplication, '_submit'), mock.patch.object(IsfApplication, '_refresh_jobs'):
                app = IsfApplication(root, mock.Mock(workspace=Path(directory)))
            try:
                overview = app.results_view.overview
                single, sweep = result(single=True), result(sweep=True)
                for geometry in ('1280x800', '1600x1000'):
                    root.geometry(geometry)
                    for theme in ('dark', 'light'):
                        with self.subTest(geometry=geometry, theme=theme):
                            app.theme_var.set(theme)
                            app._apply_theme()
                            app.current_job_id = 'single'
                            app.current_job = {'id': 'single', 'status': 'succeeded'}
                            app._result_loaded(single, 'single')
                            app.results_view.show(0)
                            root.update()
                            self.assertTrue(overview.headlines.winfo_ismapped())
                            self.assertFalse(overview.sweep.winfo_ismapped())
                            self.assertFalse(app.results_view.pane.winfo_ismapped())
                            self.assertEqual([title.cget('text') for title, _, _ in overview.cards],
                                             ['Carrier f0', 'ISF total', 'Flicker pair error',
                                              'Whole-VCO closure', 'Flicker / thermal'])
                            self.assertIn('HBnoise', overview.cards[1][2].cget('text'))
                            self.assertIn('Δ', overview.cards[1][2].cget('text'))
                            self.assertIn('Pass', overview.cards[2][2].cget('text'))
                            self.assertIn('not evaluated (synthetic replay)',
                                          [value.get() for _, value, *_ in overview.values])
                            self.assertGreater(overview.core.winfo_rooty(), overview.headlines.winfo_rooty())
                            for labels in overview.cards:
                                for label in labels:
                                    self.assertLessEqual(label.winfo_rootx() + label.winfo_reqwidth(),
                                                         overview.headlines.winfo_rootx() + overview.headlines.winfo_width())
                            app.current_job_id = 'sweep'
                            app.current_job = {'id': 'sweep', 'status': 'succeeded'}
                            app._result_loaded(sweep, 'sweep')
                            root.update()
                            self.assertFalse(overview.headlines.winfo_ismapped())
                            tree = overview.table.tree
                            self.assertTrue(tree.winfo_ismapped())
                            self.assertTrue(overview.chart.winfo_ismapped())
                            font = tkfont.Font(root=root, font=app.style.lookup('Result.Treeview.Heading', 'font'))
                            for field in ('flicker_pct', 'thermal_pct', 'pair_error'):
                                caption = tree.heading(field, 'text')
                                self.assertGreaterEqual(tree.column(field, 'width'), font.measure(caption) + 8)
                            self.assertEqual(tree.xview(), (0.0, 1.0))
                            self.assertEqual(tree.column('top_source', 'stretch'), 0)
            finally:
                app.queue.close()
                root.destroy()
        self.assertFalse(errors, errors)

    def test_point_picker_shows_maestro_column_when_any_id_is_present(self):
        root = tk.Tk()
        try:
            points = [
                dict(id='sweep-1', corner='TT', variables={'VBIAS': 1.0}),
                dict(id='sweep-2', corner='TT', variables={'VBIAS': 1.1}, point_id=0),
            ]
            dialog = choose_points(root, points, [10000], lambda _: None, lambda: None)
            root.update()
            frame = dialog.winfo_children()[0].winfo_children()[1]
            tree = next(w for w in frame.winfo_children() if isinstance(w, ttk.Treeview))
            self.assertEqual(tuple(tree['displaycolumns']), ('corner', 'variables', 'id'))
            self.assertEqual(tree.set('sweep-2', 'id'), '0')
        finally:
            root.destroy()
