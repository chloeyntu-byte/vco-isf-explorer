"""Actual mapped Tk preparation page; explicit private Xvnc required."""
import os
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest import mock
from isf_tool.tk.app import IsfApplication

@unittest.skipUnless(os.environ.get('ISF_GUI_TEST')=='1','requires explicitly selected GUI display')
class TestbenchGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=tk.Tk();self.root.geometry('1240x860');self.addCleanup(self.root.destroy)
        with mock.patch.object(IsfApplication,'_submit'),mock.patch.object(IsfApplication,'_refresh_jobs'):
            self.app=IsfApplication(self.root,mock.Mock(workspace=Path(self.temp.name)))
        self.addCleanup(self.app.queue.close)
        self.page=self.app.testbench_page;self.app.notebook.select(self.page);self.root.update()

    def test_three_entries_visible_and_ports_table_mapped(self):
        self.assertTrue(self.page.winfo_ismapped())
        self.page.described(dict(descriptor=dict(ports=[dict(name='VDD',direction='inputOutput'),dict(name='OP',direction='inputOutput')]),classification=dict(ports=[dict(name='VDD',suggested_role='supply'),dict(name='OP',suggested_role='output_p')])))
        self.root.update();self.assertTrue(self.page.table.bbox('VDD'))
        with mock.patch('isf_tool.tk.testbench_page.simpledialog.askstring',return_value='supply'),mock.patch('isf_tool.tk.testbench_page.simpledialog.askfloat',return_value=1.8):
            self.page.table.selection_set('VDD');self.page.edit_port()
        self.assertEqual(self.page.request['supplies'][0]['value'],1.8)
        self.assertEqual(self.page.request['ports'][0]['role'],'supply')
        self.assertEqual(str(self.page.start_button['state']),'disabled')

    def test_prepare_uses_actual_queue_signature_and_edit_invalidates_result(self):
        self.page.frequency.set('1e9');self.page.offsets.set('10000');self.page.gm.set('M1');self.page.p.set('OP');self.page.n.set('ON')
        with mock.patch.object(self.app,'_submit') as submit:
            self.page.prepare();self.assertEqual(submit.call_count,1)
            label,worker,callback=submit.call_args.args
            self.assertIsInstance(label,str);self.assertTrue(callable(worker))
            self.page.gm.set('M2')
            with mock.patch.object(self.page,'render') as render:callback({'old':True});render.assert_not_called()

    def test_nested_request_uses_current_identity_and_editable_ports(self):
        nested=dict(cds_lib='old.cds',dut=dict(lib='OLD',cell='OLD',view='symbol'),ports=[dict(name='OLD',net='OLD',role='unknown')])
        self.page.request=dict(tbgen_spec=nested)
        self.page.cds.set('new.cds');self.page.lib.set('NEW');self.page.cell.set('VCO');self.page.view.set('schematic')
        self.page.described(dict(descriptor=dict(ports=[dict(name='VDD',direction='inputOutput')]),classification=dict(ports=[dict(name='VDD',suggested_role='supply')])))
        with mock.patch('isf_tool.tk.testbench_page.simpledialog.askstring',return_value='supply'),mock.patch('isf_tool.tk.testbench_page.simpledialog.askfloat',return_value=1.8):
            self.page.table.selection_set('VDD');self.page.edit_port()
        self.assertEqual(nested['ports'][0]['role'],'supply')
        self.assertEqual(nested['supplies'][0]['value'],1.8)
        with mock.patch.object(self.app,'_submit') as submit,mock.patch('isf_tool.tk.testbench_page.api.prepare_from_cell') as prepare:
            self.page.prepare();submit.call_args.args[1]()
        prepared=prepare.call_args.args[0]['tbgen_spec']
        self.assertEqual(prepared['cds_lib'],'new.cds')
        self.assertEqual(prepared['dut'],dict(lib='NEW',cell='VCO',view='schematic'))
        self.assertEqual(nested['cds_lib'],'old.cds')  # draft remains separate from dispatched snapshot

    def test_describe_discards_response_after_cell_selection_changes(self):
        self.page.prepared={'status':'ready'}
        with mock.patch.object(self.app,'_submit') as submit:
            self.page.describe();callback=submit.call_args.args[2]
        self.assertIsNone(self.page.prepared)
        self.page.cell.set('OTHER')
        with mock.patch.object(self.page,'described') as described:
            callback({'stale':True});described.assert_not_called()

    def test_new_page_fits_narrow_window_and_sources_switch_without_execution(self):
        for kind in ('design-lab','maestro','cell'):
            self.page.entry.set(kind);self.assertIsNone(self.page.prepared)
        self.root.geometry('1024x760');self.root.update()
        self.assertGreater(self.page.table.winfo_height(),100)
        self.assertLess(self.page.start_button.winfo_rootx()+self.page.start_button.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())


    def test_session_button_uses_exact_context_and_reject_does_not_start(self):
        from isf_tool.core.testbench import digest
        spec=dict(source=dict(kind='maestro',maestro_dir='/private/source'),extensions=dict(new_test_review={}))
        r=dict(status='ready',spec=spec,spec_path='/private/spec.json',spec_hash='a'*64)
        self.page.prepared=r
        self.app.maestro_context=dict(maestro_dir='/private/source',session_add=dict(dirty=False))
        self.assertIsNotNone(self.page.session_context(r))
        self.app.maestro_context['maestro_dir']='/other/source'
        self.assertIsNone(self.page.session_context(r))
        self.app.maestro_context['maestro_dir']='/private/source'
        with mock.patch.object(self.page,'review_settings'),mock.patch.object(self.app,'_submit') as submit:
            self.page.start_session()
            preview = submit.call_args.args
            with mock.patch('isf_tool.tk.testbench_page.api.planned_points', return_value=[]):
                preview[2](preview[1]())
            with mock.patch('isf_tool.tk.testbench_page.api.prepare_session_add',return_value={'handle':1}) as prepare,mock.patch('isf_tool.tk.testbench_page.api.submit_session_add') as send:
                submit.call_args.args[1]()
                self.assertEqual(prepare.call_args.kwargs['context'],self.app.maestro_context)
        self.page.session_handle={'bridge_dir':'/nonexistent/private'}
        with mock.patch.object(self.app,'_submit') as submit,mock.patch('isf_tool.tk.testbench_page.api.run') as run:
            self.page.poll_session();submit.call_args.args[2]({'status':'rejected'})
            run.assert_not_called();self.assertIn('rejected',self.page.status.get())

    def test_session_actions_fit_narrow_window_and_full_review_is_readable(self):
        self.root.geometry('1024x760');self.root.update()
        right=self.root.winfo_rootx()+self.root.winfo_width()
        for button in (self.page.session_button,self.page.review_button):
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),right)
        def close_review():
            windows=[w for w in self.page.winfo_children() if isinstance(w,tk.Toplevel)]
            self.assertEqual(len(windows),1)
            texts=[w for w in windows[0].winfo_children() if isinstance(w,tk.Text)]
            self.assertIn('legacy settings',texts[0].get('1.0','end'))
            self.assertTrue(texts[0].cget('yscrollcommand'))
            self.assertTrue(texts[0].cget('xscrollcommand'))
            windows[0].destroy()
        self.root.after(50,close_review)
        self.page.review_settings(dict(spec=dict(extensions=dict(new_test_review={'preserved':'legacy settings'}))))

if __name__=='__main__':unittest.main()
