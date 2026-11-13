"""Opt-in process explorer interaction and actual mapped layout checks."""
import copy
import os
import tkinter as tk
from tkinter import ttk
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from isf_tool.tk.app import IsfApplication
from test_tk_noise_kinds_gui import thermal_fixture


@unittest.skipUnless(os.environ.get('ISF_GUI_TEST')=='1','requires explicitly selected GUI display')
class ProcessGuiTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk()
        self.errors=[]
        self.root.report_callback_exception=lambda *args:self.errors.append(args)
        with mock.patch.object(IsfApplication,'_submit'),mock.patch.object(IsfApplication,'_refresh_jobs'):
            self.app=IsfApplication(self.root,mock.Mock(workspace=Path('/tmp/process-gui-fixture')))
        raw=thermal_fixture()
        for point,data in raw['points'].items():
            import math
            data['workpoint']=[{'sample':i,'wt_rad':i/16*2*math.pi, 'VG_V':i/16, 'VD_V':1-i/16,
                'VS_V':0.,'VB_V':0.,'ID_main_A':.001*i, 'ID_replica_A':-.002*i,
                'm_DC_A_rtHz':1e-12*i, 'm_dyn_A_rtHz':2e-12*i,
                'isf_i':1e9*i,'isf_q':-1e8*i,'product_i':.001*i,'product_q':-.002*i} for i in range(17)]
            data['phase_candidates']=[{'rank':1,'chosen':True,'signs':'++','score':.001}]
        self.app.current_job_id='fixture'
        self.app.current_job={'id':'fixture','status':'succeeded'}
        self.app.noise_kind_var.set('flicker')
        self.app._result_loaded(raw,'fixture')
        self.ui=self.app.results_view
        self.root.geometry('1280x800')
        self.root.update()

    def tearDown(self):
        self.app.queue.close()
        self.root.update_idletasks()
        for timer in self.root.tk.call('after','info'):
            self.root.tk.call('after','cancel',timer)
        self.root.destroy()
        self.assertFalse(self.errors,repr(self.errors))

    def test_00_quality_layout_returns_from_update_in_fresh_process(self):
        # A main-loop timer cannot catch update() spinning in geometry/idle
        # events. Run the real resize path in a child with an external watchdog.
        import subprocess
        import sys
        script = """
import sys
sys.path.insert(0, 'tests')
from test_tk_process_gui import ProcessGuiTests
case = ProcessGuiTests('test_quality_candidates_chosen_and_raw_metric_copy')
case.setUp()
try:
    for kind in ('both', 'thermal', 'flicker'):
        case.app.noise_kind_var.set(kind)
        case.app._point_changed()
        for size in ('1280x800', '1600x1000', '1100x700', '1280x800'):
            case.root.geometry(size)
            for view in (5, 1, 5, 0):
                case.ui.show(view)
                case.root.update()
                case.root.update_idletasks()
                assert case.ui.note_label.winfo_width() <= case.ui.plot_frame.winfo_width()
                if view == 5:
                    assert case.ui.quality_frame.winfo_ismapped()
                    assert case.ui.quality_note.winfo_width() <= case.ui.plot_frame.winfo_width()
    print('quality resize settled')
finally:
    case.tearDown()
"""
        run = subprocess.run([sys.executable, '-c', script], cwd=str(Path(__file__).resolve().parents[1]),
                             stdout=subprocess.PIPE, # Allow slow virtual displays while retaining an external deadlock watchdog.
                             stderr=subprocess.PIPE, text=True, timeout=120)
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertIn('quality resize settled', run.stdout)

    def test_no_nested_notebook_and_four_real_panels_at_supported_sizes(self):
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        self.assertFalse(any(isinstance(w,ttk.Notebook) for w in descendants(self.app.result_tab)))
        for size in ('1600x1000','1280x800'):
            self.root.geometry(size)
            self.ui.show(1)
            self.root.update()
            self.assertEqual(4,len(self.ui.chart.plots))
            self.assertTrue(all(p['rect'][3]-p['rect'][1]>15 for p in self.ui.chart.plots))
            w,h=self.ui.chart.winfo_width(),self.ui.chart.winfo_height()
            for p in self.ui.chart.plots:
                left,top,right,bottom=p['rect']
                self.assertTrue(0<=left<right<=w)
                self.assertTrue(0<=top<bottom<=h)
        self.assertEqual((1280,800),self.root.minsize())

    def test_workpoint_table_to_all_panels_and_chart_hover_to_table(self):
        self.ui.show(1);self.root.update()
        key='sample:5'
        self.ui.table.tree.selection_set(key);self.root.update()
        self.assertEqual(key,self.ui.chart.selected_key)
        self.assertTrue(self.ui.table.tree.bbox(key))
        cursor_lines=[self.ui.chart.coords(item) for item in self.ui.chart.find_withtag('cursor') if self.ui.chart.type(item)=='line']
        self.assertEqual(4,len(cursor_lines))
        self.assertLess(max(c[0] for c in cursor_lines)-min(c[0] for c in cursor_lines),.01)
        self.assertGreaterEqual(len(self.ui.chart.find_withtag('cursor')),8)
        hit=next(h for h in self.ui.chart.hits if h['key']=='sample:12' and h['series']['id']=='isf_i')
        self.ui.chart._motion(SimpleNamespace(x=hit['x'],y=hit['y']))
        self.root.update()
        self.assertEqual(('sample:12',),self.ui.table.tree.selection())
        self.assertTrue(self.ui.table.tree.bbox('sample:12'))
        self.assertEqual('sample:12',self.ui.chart.selected_key)

    def test_harmonic_source_and_offset_selection_both_directions(self):
        for view in (2,3,4):
            self.ui.show(view);self.root.update()
            hits=[h for h in self.ui.chart.hits if h['key'] in self.ui.table.rows]
            self.assertTrue(hits)
            hit=hits[-1]
            self.ui.chart._click(SimpleNamespace(x=hit['x'],y=hit['y']))
            self.root.update()
            key=self.ui.chart.selected_key
            self.assertIn(key,self.ui.table.rows)
            self.assertEqual((key,),self.ui.table.tree.selection())
            first=next(iter(self.ui.table.rows))
            self.ui.table.tree.selection_set(first);self.root.update()
            self.assertEqual(first,self.ui.chart.selected_key)
            if view==3:
                self.assertEqual(first[len('offset:'):],self.app.point_var.get())

    def test_quality_candidates_chosen_and_raw_metric_copy(self):
        self.ui.show(5);self.root.update()
        self.assertTrue(self.ui.quality_frame.winfo_ismapped())
        self.assertTrue(self.ui.candidates.tree.get_children())
        iid=self.ui.candidates.tree.get_children()[0]
        self.assertIn('chosen',self.ui.candidates.tree.item(iid,'tags'))
        first=next(iter(self.ui.quality_table.rows))
        self.ui.quality_table.tree.selection_set(first)
        self.ui.quality_table.copy()
        self.assertIn('All metrics & quality',self.root.clipboard_get())

    def test_drawer_navigation_toggle_and_full_precision_copy(self):
        self.ui.show(1);self.root.update()
        before=self.ui.chart.winfo_height()
        self.ui.toggle_drawer();self.root.update()
        self.assertGreater(self.ui.chart.winfo_height(),before)
        self.ui.toggle_drawer();self.root.update()
        self.ui.toggle_nav();self.root.update()
        self.assertFalse(self.ui.nav.winfo_ismapped())
        self.ui.toggle_nav();self.root.update()
        key='sample:3';self.ui.table.highlight(key)
        self.ui.table.copy()
        self.assertIn('3e-12',self.root.clipboard_get())
        for column in self.ui.table.tree['displaycolumns']:
            self.assertGreater(int(self.ui.table.tree.column(column,'width')),0)
        self.ui.table.show_all_columns()
        self.assertEqual(len(self.ui.table.specs),len(self.ui.table.tree['displaycolumns']))

    def test_zoom_pan_reset_and_curve_visibility_do_not_change_result(self):
        self.ui.show(1);self.root.update()
        before=copy.deepcopy(self.app.current_result)
        plot=self.ui.chart.plots[0]
        x=(plot['rect'][0]+plot['rect'][2])/2;y=(plot['rect'][1]+plot['rect'][3])/2
        event=SimpleNamespace(x=x,y=y)
        self.ui.chart._zoom(event,.8);self.root.update()
        self.assertAlmostEqual(.8,self.ui.chart.limits['phase'][1]-self.ui.chart.limits['phase'][0])
        self.ui.chart._pan_start(event)
        self.ui.chart._pan_move(SimpleNamespace(x=x+25,y=y));self.root.update()
        self.ui.chart.toggle(0,'VG_V');self.root.update()
        self.assertIn((0,'VG_V'),self.ui.chart.hidden)
        self.ui.chart.reset();self.root.update()
        self.assertEqual({},self.ui.chart.limits)
        self.assertEqual(before,self.app.current_result)
        self.app.theme_var.set('dark');self.app._apply_theme();self.root.update()
        self.assertEqual(before,self.app.current_result)
        self.ui.show(3);self.root.update()
        plot=self.ui.chart.plots[0]
        event=SimpleNamespace(x=(plot['rect'][0]+plot['rect'][2])/2,y=(plot['rect'][1]+plot['rect'][3])/2)
        self.ui.chart._zoom(event,.8);self.root.update()
        limits=dict(self.ui.chart.limits)
        self.ui.select_key('offset:tt@10000',True);self.root.update()
        self.assertEqual(limits,self.ui.chart.limits)

    def test_thermal_native_grid_uses_one_shared_workpoint_cursor(self):
        import json,tempfile
        with tempfile.TemporaryDirectory(prefix='isf-thermal-ui-') as directory:
            self.app.controller.workspace=Path(directory)
            self.ui._thermal_identity=None
            identity=self.app.operating_point_var.get()
            summary=self.app.current_result['summary'][0]
            f0=summary.get('f0_Hz',1e9)
            target=Path(directory)/'jobs'/'fixture'/'simulation'/identity/'thermal'/'source_coefficients.json'
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps([{'device':'M0','mechanism':'id','source_time_s':[0,.5/f0,1/f0], 'source_psd_A2_Hz':[1e-24,4e-24,1e-24]}]),encoding='utf8')
            self.app.noise_kind_var.set('thermal');self.app._point_changed()
            self.ui.show(1);self.root.update()
            self.ui.table.tree.selection_set('sample:3');self.root.update()
            lines=[self.ui.chart.coords(i) for i in self.ui.chart.find_withtag('cursor') if self.ui.chart.type(i)=='line']
            self.assertEqual(4,len(lines))
            self.assertLess(max(c[0] for c in lines)-min(c[0] for c in lines),.01)
            self.assertEqual(3,len(self.ui.chart.panels[1]['series'][0]['points']))

    def test_load_coefficients_while_empty_thermal_band_view_is_selected(self):
        import json, tempfile
        raw = copy.deepcopy(self.app.current_result)
        with tempfile.TemporaryDirectory(prefix='isf-band-load-') as directory:
            self.app.controller.workspace = Path(directory)
            self.app.current_result = None
            self.app.current_result_view = None
            self.ui.clear()
            self.ui.source_var.set('')
            self.ui.show(7)
            self.app.noise_kind_var.set('both')
            for summary in raw['summary']:
                identity = summary.get('operating_point_id') or summary['point']
                target = Path(directory)/'jobs'/'fixture'/'simulation'/identity/'thermal'/'source_coefficients.json'
                target.parent.mkdir(parents=True, exist_ok=True)
                f0 = summary['f0_Hz']
                target.write_text(json.dumps([dict(device='M0', mechanism='id',
                    source_time_s=[0., .5/f0, 1/f0],
                    source_psd_A2_Hz=[1e-24, 4e-24, 1e-24])]))
            self.app._result_loaded(raw, 'fixture')
            self.root.update()
            self.assertTrue(self.ui._thermal['sources'])
            options = self.ui.source_box['values']
            self.assertTrue(options)
            self.assertIn(self.ui.source_var.get(), options)
            # Display-name selection survives refresh and converts to an id
            # when returning to the workpoint view.
            self.ui.refresh()
            self.ui.show(1)
            self.root.update()
            self.assertIn(self.ui.source_var.get(), [s['id'] for s in self.ui._thermal['sources']])

    def test_eps_and_png_exports_have_actual_chart_dimensions(self):
        import tempfile
        from PIL import Image
        self.ui.show(1);self.root.update()
        with tempfile.TemporaryDirectory(prefix='isf-export-ui-') as directory:
            eps=Path(directory)/'chart.eps';png=Path(directory)/'chart.png'
            with mock.patch('isf_tool.tk.results_view.filedialog.asksaveasfilename',return_value=str(eps)):
                self.ui.export_eps()
            self.assertTrue(eps.read_bytes().startswith(b'%!PS-Adobe'))
            with mock.patch('isf_tool.tk.results_view.filedialog.asksaveasfilename',return_value=str(png)):
                self.ui.export_png()
            with Image.open(png) as image:
                self.assertEqual((self.ui.chart.winfo_width(),self.ui.chart.winfo_height()),image.size)

    def test_source_bar_values_and_range_use_engineering_format(self):
        self.ui.chart.key_positions = {}
        self.ui.chart.set_panels([{'kind':'sources', 'title':'SI display', 'rows':[
            {'key':'source:large', 'device':'M0', 'mechanism':'id', 'modeled':True,
             'simulated_dbc':357e9, 'calculated_dbc':None, 'scope':'display fixture'}]}])
        self.root.update()
        texts = [self.ui.chart.itemcget(i, 'text') for i in self.ui.chart.find_all() if self.ui.chart.type(i)=='text']
        self.assertIn('357 G', texts)
        self.assertTrue(any('range 357 G' in t for t in texts))

    def test_overview_allocates_more_height_to_phase_noise_and_bounds_readouts(self):
        for size, minimum in (('1600x1000', 160), ('1280x800', 80)):
            self.root.geometry(size)
            self.ui.show(0)
            self.root.update()
            rect = self.ui.chart.plots[0]['rect']
            self.assertGreater(rect[3]-rect[1], minimum)
            first = next(iter(self.ui.table.rows))
            self.ui.select_key(first)
            self.root.update()
            for item in self.ui.chart.find_withtag('cursor'):
                if self.ui.chart.type(item) == 'text':
                    left, top, right, bottom = self.ui.chart.bbox(item)
                    self.assertTrue(0 <= left <= right <= self.ui.chart.winfo_width())
                    self.assertTrue(0 <= top <= bottom <= self.ui.chart.winfo_height())

    def test_overview_keeps_thumbnails_readable_on_short_canvas(self):
        # Real fonts (DejaVu, Noto) leave the overview canvas around 220 px
        # tall at 1280x800; stacked thumbnails must collapse, not squeeze.
        self.ui.show(0)
        self.root.update()
        chart = self.ui.chart
        chart.winfo_height = lambda: 600
        chart.draw()
        full = len(chart.plots)
        self.assertEqual(3, full)
        for height in (220, 300, 449):
            chart.winfo_height = lambda h=height: h
            chart.draw()
            self.assertEqual(full - 1, len(chart.plots))
            for plot in chart.plots:
                self.assertGreater(plot['rect'][3] - plot['rect'][1], 15, 'canvas %d px: %r' % (height, plot['rect']))
        del chart.winfo_height
        chart.draw()

    def test_all_six_views_switch_without_stale_columns(self):
        for view in (0,1,2,3,4,5,1,0):
            self.ui.show(view);self.root.update()
            self.assertTrue(self.ui.table.tree.get_children())
            for column in self.ui.table.tree['displaycolumns']:
                self.assertIn(column,[s[0] for s in self.ui.table.specs])


if __name__=='__main__':unittest.main()
