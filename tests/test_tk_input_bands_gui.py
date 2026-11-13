import math
"""Visible frequency-band and selected-offset decomposition interactions."""
import os
from types import SimpleNamespace
import unittest
import test_tk_process_gui as process_tests
from synthetic_fixture import make_band_result

@unittest.skipUnless(os.environ.get('ISF_GUI_TEST')=='1','requires explicitly selected GUI display')
class BandGuiTests(unittest.TestCase):
    setUp=process_tests.ProcessGuiTests.setUp
    tearDown=process_tests.ProcessGuiTests.tearDown

    def test_band_table_curve_and_legend_bidirectional_selection(self):
        self.app.noise_kind_var.set('both')
        self.app._result_loaded(make_band_result(),'fixture')
        self.ui.show(6);self.root.update()
        key='band:1'
        self.ui.table.tree.selection_set(key);self.root.update()
        self.assertEqual(key,self.ui.chart.selected_key)
        target=next(h for h in self.ui.chart.hits if h['key']=='band:2')
        self.ui.chart._click(SimpleNamespace(x=target['x'],y=target['y']));self.root.update()
        self.assertEqual(('band:2',),self.ui.table.tree.selection())
        # Dispatch a real Canvas legend click through its bound tag.
        bounds=self.ui.chart.bbox('legend:0:0')
        x,y=(bounds[0]+bounds[2])/2,(bounds[1]+bounds[3])/2
        self.ui.chart.event_generate('<Motion>',x=int(x),y=int(y));self.root.update()
        self.ui.chart.event_generate('<Button-1>',x=int(x),y=int(y));self.root.update()
        self.assertEqual(('band:0',),self.ui.table.tree.selection())
        self.assertFalse(self.ui.chart.hidden)
        self.ui.reference_slopes.set(False);self.ui.refresh();self.root.update()
        self.assertFalse(any(s['id'].startswith('slope_') for s in self.ui.chart.panels[0]['series']))

    def test_harmonic_bars_and_phasor_share_selection(self):
        self.ui.show(2);self.root.update()
        rows=list(self.ui.table.rows.values())
        self.assertAlmostEqual(100,sum(r['fraction_pct'] for r in rows),places=8)
        self.assertEqual(3,len(self.ui.chart.panels))
        key=rows[0]['key'];self.ui.table.tree.selection_set(key);self.root.update()
        self.assertEqual(key,self.ui.chart.selected_key)
        self.assertTrue(any(h['key']==key for h in self.ui.chart.hits))
        self.assertIn('fraction_pct',[f for f,_ in self.ui.table.specs])

    def test_thermal_source_table_selection_switches_plotted_source(self):
        self.app._result_loaded(make_band_result(),'fixture')
        self.ui.show(7);self.root.update()
        self.assertTrue(self.ui.source_box.winfo_ismapped())
        self.assertEqual(1,len(self.ui.chart.panels[0]['series']))
        key=list(self.ui.table.rows)[-1]
        self.ui.table.tree.selection_set(key);self.root.update()
        self.assertIn(key,self.ui.chart.panels[0]['series'][0]['keys'])
        self.assertEqual((key,),self.ui.table.tree.selection())
        self.assertEqual(9,len(self.ui.table.rows))

    def test_views_at_two_sizes_and_dark_font_and_figure14(self):
        self.app._result_loaded(make_band_result(),'fixture')
        self.app.theme_var.set('dark');self.app._apply_theme()
        for size in ('1280x800','1600x1000'):
            self.root.geometry(size)
            for view in (0,1,2,6,7):
                self.ui.show(view);self.root.update()
                self.assertTrue(self.ui.table.winfo_ismapped())
                self.assertTrue(self.ui.chart.plots)
                for p in self.ui.chart.plots:
                    self.assertGreater(p['rect'][3]-p['rect'][1],15)
                if view==6:
                    self.assertEqual(self.app.body_font.actual('family'),self.ui.chart.small_font.actual('family'))
            self.ui.overlay.set(True);self.ui.show(1);self.root.update()
            self.assertEqual(5,len(self.ui.chart.panels[0]['series']))
            self.assertIn('summary dc−dynamic',self.ui.note.get())
            self.ui.overlay.set(False)

    def test_pair_comparison_is_first_drawn_and_linked_at_three_sizes(self):
        from test_contribution_comparison import pair_snapshot_result
        self.app._result_loaded(pair_snapshot_result(),'fixture')
        for size in ('1280x800','1440x900','1600x1000'):
            self.root.geometry(size)
            for kind in ('flicker','both'):
                self.app.noise_kind_var.set(kind);self.app._point_changed()
                self.ui.show(4);self.root.update()
                rows=list(self.ui.table.rows.values())
                key=rows[0]['key']
                self.assertEqual('对称 gm pair · flicker 对比',rows[0]['label'])
                hits=[h for h in self.ui.chart.hits if h['key']==key]
                self.assertEqual(2,len(hits))
                self.assertAlmostEqual(10*math.log10(2*4e-8),hits[1]['value'][1],places=10)
                self.assertTrue(any('聚合' in str(self.ui.chart.itemcget(i,'text'))
                    for i in self.ui.chart.find_all() if self.ui.chart.type(i)=='text'))
                self.ui.table.tree.selection_set(key);self.root.update()
                self.assertEqual(key,self.ui.chart.selected_key)
                target=next(h for h in self.ui.chart.hits if h['key']=='source:core_a/fn')
                self.ui.chart._click(SimpleNamespace(x=target['x'],y=target['y']));self.root.update()
                self.assertEqual(('source:core_a/fn',),self.ui.table.tree.selection())
                self.assertIn('计入对称 gm pair 对比',self.ui.readout.get())

                messages=self.ui.chart.find_withtag('reason:1')
                self.assertGreaterEqual(len(messages),2)
                boxes=sorted((self.ui.chart.bbox(item) for item in messages),key=lambda b:b[1])
                self.assertTrue(all(a[3] < b[1] for a,b in zip(boxes,boxes[1:])))
