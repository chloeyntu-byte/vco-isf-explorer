"""Actual Tk gm tree/path/output/card behavior, with no EDA process."""
import os
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest import mock

from isf_tool.tk.app import IsfApplication
from isf_tool.core.signal_mapping import discover_signal_mapping


@unittest.skipUnless(os.environ.get('ISF_GUI_TEST')=='1','requires explicitly selected GUI display')
class ComponentSelectionGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=tk.Tk()
        self.root.geometry('1240x860')
        with mock.patch.object(IsfApplication,'_submit'),mock.patch.object(IsfApplication,'_refresh_jobs'):
            self.app=IsfApplication(self.root,mock.Mock(workspace=self.temp.name))
        source=Path(self.temp.name)/'input.scs'
        source.write_text('model nch bsim4\nsubckt core (d g s b)\nXgm (d g s b) nch l=1u w=2u\nends core\nA (OUTP OUTN 0 0) core\nB (OUTN OUTP 0 0) core\nhb (OUTP OUTN) hb\nhbnoise (OUTP OUTN) hbnoise\n')
        history={'source_netlists':[{'path':str(source)}]}
        test={'name':'vco','variables':[],'outputs':[],'analyses':[]}
        role=discover_signal_mapping(test,'vco',history,{'target_instance':'A.Xgm'})
        role.update(selected_history=history,selected_test=test)
        self.report={'roles':{'vco':role},'pair':{'replica_source':'generated'}}
        self.app._render_mapping_report(self.report)
        self.root.update()

    def tearDown(self):
        self.app.queue.close()
        self.root.update_idletasks()
        self.root.destroy()

    def test_tree_editable_path_independent_outputs_and_card(self):
        app=self.app
        self.assertEqual(str(app.target_mos_box.cget('state')),'normal')
        self.assertIn('A.Xgm',app.binding_review_var.get())
        self.assertIn('D=OUTP',app.binding_review_var.get())
        self.assertIn('Primitive: nch',app.binding_review_var.get())
        with mock.patch.object(app,'_inspect_pair') as inspect:
            app.target_mos_var.set('/B/Xgm')
            app._target_mos_chosen()
            self.assertEqual(app._mapping_overrides['vco']['target_instance'],'B.Xgm')
            inspect.assert_called_once()
        with mock.patch.object(app,'_inspect_pair'):
            app.output_net_vars['output_p'].set('OUTN')
            app.output_net_vars['output_n'].set('0')
            app._output_nets_chosen()
        self.assertEqual(app._mapping_overrides['vco']['output_n'],'0')
        self.assertEqual(app._mapping_overrides['vco']['output_p'],'OUTN')
        app._browse_target_instance()
        self.root.update()
        tree=app.target_browser_tree
        self.assertTrue(tree.exists('A.Xgm'))
        self.assertTrue(tree.exists('B.Xgm'))
        self.assertTrue(tree.bbox('A.Xgm'))
        app.target_browser.destroy()
        self.root.update()
        app.vars['replica_source'].set('maestro')
        app._render_reference_controls()
        self.root.update()
        self.assertTrue(app.target_mos_box.winfo_ismapped())
        self.assertTrue(app.output_net_boxes['output_p'].winfo_ismapped())

    def test_card_displays_effective_defaults_and_parameter_sources(self):
        source = Path(self.temp.name)/'defaults.scs'
        source.write_text('model nch bsim4\nsubckt rf (d g s b)\nparameters w=2u l=1u\n'
                          'm0 (d g s b) nch w=2*w l=l ad=3p\nends rf\nA (OUTP OUTN 0 0) rf\n')
        role = discover_signal_mapping({'name':'vco','variables':[],'outputs':[],'analyses':[]},'vco',
                                       {'source_netlists':[{'path':str(source)}]}, {'target_instance':'A'})
        self.app._render_mapping_report({'roles':{'vco':role},'pair':{'replica_source':'generated'}})
        card = self.app.binding_review_var.get()
        for expected in ('w=4e-06 (Parent expression)', 'l=1e-06 (Default)', 'm=1 (Default)',
                         'nf=1 (Default)', 'ad=3e-12 (Instance parameter)'):
            self.assertIn(expected,card)

    def test_narrow_layout_and_saved_overrides(self):
        app=self.app
        for geometry in ('1240x860','1020x720'):
            self.root.geometry(geometry)
            self.root.update()
            for widget in (app.target_mos_box,*app.output_net_boxes.values(),app.binding_review_card):
                self.assertTrue(widget.winfo_ismapped())
                self.assertGreater(widget.winfo_width(),20)
                self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
        app._mapping_overrides['vco']={'target_instance':'B.Xgm','output_p':'OUTN','output_n':'0'}
        app._render_mapping_report(self.report)
        self.assertEqual(app.target_mos_var.get(),'B.Xgm')
        self.assertEqual(app.output_net_vars['output_n'].get(),'0')
