"""Opt-in actual Tk metrics view/export/link checks on private Xvnc."""
import copy
import math
import json
import os
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest import mock
from test_optimization_metrics import synthetic_points
from isf_tool.tk.app import IsfApplication


@unittest.skipUnless(os.environ.get('ISF_GUI_TEST')=='1','requires explicit private GUI display')
class MetricsGuiTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.errors=[]
        self.root.report_callback_exception=lambda *exc:self.errors.append(exc)
        self.tmp=tempfile.TemporaryDirectory(prefix='isf-metrics-gui-')
        with mock.patch.object(IsfApplication,'_submit'),mock.patch.object(IsfApplication,'_refresh_jobs'):
            self.app=IsfApplication(self.root,mock.Mock(workspace=Path(self.tmp.name)))
        self.record=synthetic_points()[0]
        point=self.record['point'];data=self.record['result']
        self.raw=dict(points={point:data},summary=data['summary'],run_kind='replay',signoff=False)
        self.app.current_job_id='fixture';self.app.current_job={'id':'fixture','status':'succeeded'}
        self.app._result_loaded(self.raw,'fixture');self.ui=self.app.results_view
        self.root.geometry('1600x1000');self.ui.show(8);self.root.update()

    def tearDown(self):
        self.app.queue.close()
        for timer in self.root.tk.call('after','info'):self.root.tk.call('after','cancel',timer)
        self.root.destroy();self.tmp.cleanup();self.assertFalse(self.errors,repr(self.errors))

    def test_both_sizes_themes_regions_selection_and_source_gate(self):
        original=copy.deepcopy(self.app.current_result)
        for size in ('1280x800','1600x1000'):
            for theme in ('light','dark'):
                self.root.geometry(size);self.app.theme_var.set(theme);self.app._apply_theme();self.root.update()
                self.assertTrue(self.ui.table.tree.winfo_ismapped())
                self.assertEqual(3,len(self.ui.chart.plots))
                self.assertIn('s^-1/√Hz',self.ui.chart.panels[2]['title'])
                self.assertIn('s^-1/√Hz',self.ui.chart.panels[2]['ylabel'])
                for plot in self.ui.chart.plots:
                    a,b,c,d=plot['rect'];self.assertGreater(c-a,300);self.assertGreater(d-b,20)
                    self.assertLessEqual(d,self.ui.chart.winfo_height())
                self.assertTrue(self.ui.diagnostic_frame.winfo_ismapped())
                self.assertIn('Source attribution required',self.ui.diagnostic_text.get())
                for key in ('opt:waveform.rise_max_slope_v_rad','opt:gamma.positive_fwhm_cycle','opt:flicker.area_positive_dyn'):
                    self.ui.table.tree.selection_set(key);self.root.update()
                    self.assertEqual(key,self.ui.chart.selected_key)
                    self.assertTrue(self.ui.table.tree.bbox(key))
                self.assertTrue(self.ui.chart.find_withtag('metric-region'))
                self.assertTrue(self.ui.chart.find_withtag('metric-marker'))
                self.assertTrue(self.ui.chart.find_withtag('metric-mean'))
                self.assertLessEqual(self.ui.note_label.winfo_width(),self.ui.plot_frame.winfo_width())
        self.assertIn(self.ui.optimization_metrics['sources'][0]['id'],self.ui.table.rows['opt:sources.0.share']['label'])
        for key,title in (('opt:flicker.c_k.1','Coherent projection'),('opt:thermal.band_frac.1','input band'),('opt:sources.0.share','source shares'),('opt:closure.gap_db','Closure')):
            self.ui.select_key(key,True);self.root.update()
            self.assertIn(title,self.ui.chart.panels[0]['title'])
            self.assertEqual(key,self.ui.chart.selected_key)
            if key=='opt:closure.gap_db':
                bounds=self.ui.chart.plots[0]['bounds']['left']
                self.assertLess(bounds[3]-bounds[2],10)
        self.ui.select_key('opt:gamma.positive_area',True);self.root.update()
        self.assertIn('Vdiff',self.ui.chart.panels[0]['title'])
        self.assertEqual(original,self.app.current_result)

    def test_reviewed_source_identity_and_single_closure_reach_ui_export(self):
        records=[r for r in synthetic_points() if r['name'] in ('A','B-final')]
        single=copy.deepcopy(synthetic_points()[0])
        row=single['result']['summary'][0]
        row['pair_model']='single_device_only'
        row['thermal_noise']=dict(schema_version=1,status='unavailable',reason='single-device regression')
        records.append(single)
        for rec in records:
            with self.subTest(name=rec['name'],point=rec['point']):
                raw=dict(points={rec['point']:rec['result']},summary=rec['result']['summary'],run_kind='replay',signoff=False)
                self.app._result_loaded(raw,'fixture');self.ui.show(8);self.root.update()
                m=self.ui.optimization_metrics
                self.assertIsNotNone(m['closure']['gap_db'])
                self.assertEqual(self.ui.table.rows['opt:closure.gap_db']['value'],m['closure']['gap_db'])
                if rec is single:
                    self.assertAlmostEqual(m['closure']['gap_db'],20 * math.log10(4),places=10)
                    self.assertIn('Source attribution required',self.ui.diagnostic_text.get())
                else:
                    identity=rec['result']['summary'][0]['flicker_pair_identity']
                    self.assertNotIn(m['closure']['dominant_unmodeled_source'],
                                     {identity[k]+':fn' for k in ('target_noise_trace','peer_noise_trace')})
                self.ui.select_key('opt:closure.gap_db',True);self.root.update()
                path=Path(self.tmp.name)/'reviewed-metrics.json'
                with mock.patch('isf_tool.tk.results_view.filedialog.asksaveasfilename',return_value=str(path)):
                    self.ui.export_metrics()
                exported=json.loads(path.read_text())
                self.assertEqual(exported['closure'],m['closure'])
                self.assertEqual(exported['sources'],m['sources'])

    def test_metric_json_export_and_original_views(self):
        path=Path(self.tmp.name)/'metrics.json'
        with mock.patch('isf_tool.tk.results_view.filedialog.asksaveasfilename',return_value=str(path)):
            self.ui.export_metrics()
        m=json.loads(path.read_text());self.assertEqual('vco.isf-metrics.v1',m['schema'])
        self.assertAlmostEqual(m['flicker']['rho_dyn'],-.20339,delta=.001)
        for view in (0,1,2,3,4,5,6,7,8):
            self.ui.show(view);self.root.update()
            self.assertEqual(view==8,bool(self.ui.diagnostic_frame.winfo_ismapped()))
        self.ui.table.highlight('opt:flicker.rho_dyn');self.ui.table.copy()
        self.assertIn('ρ_dyn',self.root.clipboard_get())
