"""Invoking-window context, display labels and read-only automatic inspection."""
from __future__ import annotations
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from isf_tool.tk import app
from isf_tool.tk.viewmodel import apply_maestro_context, maestro_pair_payload, select_history_candidate
from tests.test_tk_launch import FakeRoot, FakeVariable, FakeWidget


def context(selected='Interactive.3'):
    return {'schema':'ISF_MAESTRO_CONTEXT_1', 'library':'lib_A', 'cell':'osc_A', 'view':'maestro',
            'work_dir':'/project/A', 'maestro_dir':'/project/A/osc_A/maestro', 'selected_history':selected,
            'histories':[{'name':'Interactive.3','rdb_file':'/outside/results/Interactive.3.rdb','results_dir':'/sim/A'}]}


class ContextViewModelTests(unittest.TestCase):
    def test_context_replaces_old_vco_and_stale_authority_but_preserves_analysis(self):
        old={'analysis':{'max_harmonic':6},'execution':{'allow_execute':True},
             'testbenches':{'vco':{'path':'/old','history':'/old.rdb','binding_fingerprint':'old','mapping_overrides':{'x':1}},
                           'replica':{'path':'/noise','history':'/noise.rdb'}}}
        original=copy.deepcopy(old)
        new=apply_maestro_context(old,context(),clear_noise=True)
        self.assertEqual(original,old)
        self.assertEqual('/outside/results/Interactive.3.rdb',new['testbenches']['vco']['history'])
        self.assertEqual('',new['testbenches']['vco']['binding_fingerprint'])
        self.assertNotIn('mapping_overrides',new['testbenches']['vco'])
        self.assertNotIn('path',new['testbenches']['replica'])
        self.assertFalse(new['execution']['allow_execute'])
        self.assertEqual(6,new['analysis']['max_harmonic'])
        payload=maestro_pair_payload(new)
        self.assertEqual(context()['histories'],payload['testbenches']['vco']['history_catalog'])
        self.assertTrue(payload['testbenches']['vco']['explicit_history_selection'])

    def test_no_current_history_or_missing_current_never_selects_another(self):
        history={'name':'Interactive.3','path':'/outside/results/Interactive.3.rdb','fully_complete':True,'tests':['osc']}
        for requested in ('','Interactive.4'):
            new=apply_maestro_context({},context(requested))
            self.assertEqual('',new['testbenches']['vco']['history'])
            selected=select_history_candidate([history],requested,'osc',explicit=True)
            self.assertIsNone(selected['selected'])
        # Unrelated manual workflow keeps its existing unique-history policy.
        self.assertEqual(history,select_history_candidate([history],test_name='osc')['selected'])
        # A vanished explicit selection must never silently become another run.
        self.assertIsNone(select_history_candidate([history],'missing.rdb','osc')['selected'])

    def test_restore_keeps_user_noise_tb_and_retains_real_rdb_mapping(self):
        cfg={'testbenches':{'replica':{'path':'/chosen/noise','history':'/noise.rdb'}}}
        restored=apply_maestro_context(cfg,context())
        self.assertEqual(cfg['testbenches']['replica'],restored['testbenches']['replica'])
        self.assertEqual('/outside/results/Interactive.3.rdb',restored['testbenches']['vco']['history'])


class ContextLaunchTests(unittest.TestCase):
    def test_context_default_creates_distinct_workspaces_and_does_not_start_runs(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ,{'ISF_WORKSPACE':temp}), \
             mock.patch.object(app.tk,'Tk',side_effect=lambda:FakeRoot()), \
             mock.patch('isf_tool.core.maestro_context.load_maestro_context',return_value=context()) as load, \
             mock.patch.object(app,'IsfController') as controller, mock.patch.object(app,'IsfApplication') as ui:
            for _ in range(2):self.assertEqual(0,app.main(['--maestro-context-file','/tmp/private-context']))
            paths=[x.args[0] for x in controller.call_args_list]
            self.assertNotEqual(paths[0],paths[1])
            self.assertTrue(all(p.is_dir() and p.parent==Path(temp)/'maestro_sessions' for p in paths))
            self.assertEqual(context(),ui.call_args.kwargs['maestro_context'])
            load.assert_called_with(Path('/tmp/private-context'),consume=True)
            controller.return_value.start_run.assert_not_called()

    def test_context_explicit_workspace_remains_explicit(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(app.tk,'Tk',return_value=FakeRoot()), \
             mock.patch('isf_tool.core.maestro_context.load_maestro_context',return_value=context()), \
             mock.patch.object(app,'IsfController') as controller,mock.patch.object(app,'IsfApplication'):
            self.assertEqual(0,app.main(['--workspace',temp,'--maestro-context-file','/tmp/context']))
            controller.assert_called_once_with(Path(temp).resolve(), maestro_context=context())


class ContextSelectionTests(unittest.TestCase):
    def instance(self, *, noise=False, selected=''):
        instance=app.IsfApplication.__new__(app.IsfApplication)
        instance.vars={f'{role}_{field}':FakeVariable('') for role in ('vco','replica') for field in ('path','test','history')}
        instance.vars['vco_path'].set(context()['maestro_dir']);instance.vars['vco_history'].set(selected)
        if noise:
            instance.vars['replica_path'].set('/noise');instance.vars['replica_history'].set('/noise.rdb')
        instance.raw_config=apply_maestro_context({},context(''))
        instance._selection_revision=1;instance._updating_form=False
        instance._history_display_vars={r:FakeVariable() for r in ('vco','replica')}
        instance._history_choices={r:{} for r in ('vco','replica')}
        for role in ('vco','replica'):
            setattr(instance,f'{role}_history_box',FakeWidget());setattr(instance,f'{role}_test_box',FakeWidget())
        instance.binding_status_var=FakeVariable('');instance._set_raw_text=mock.Mock();instance._inspect_pair=mock.Mock()
        instance.controller=mock.Mock()
        def config():
            c=copy.deepcopy(instance.raw_config)
            for r in ('vco','replica'):
                s=c.setdefault('testbenches',{}).setdefault(r,{})
                for f in ('path','test','history'):s[f]=instance.vars[f'{r}_{f}'].get()
            return c
        instance._config_from_ui=config
        return instance

    def test_history_displays_name_and_commits_absolute_rdb_path(self):
        i=self.instance();i._schedule_role_inspection=mock.Mock()
        i._set_history_choices('vco',context()['histories'])
        self.assertEqual(['Interactive.3'],i.vco_history_box.options['values'])
        i._history_display_vars['vco'].set('Interactive.3');i._history_chosen('vco')
        self.assertEqual('/outside/results/Interactive.3.rdb',i.vars['vco_history'].get())
        i._schedule_role_inspection.assert_called_once_with('vco')

    def test_single_vco_inspection_is_available_before_noise_and_does_not_guess_history(self):
        i=self.instance();pending={}
        i._submit=lambda label,fn,success:pending.update(fn=fn,success=success)
        i._inspect_role('vco')
        pending['fn']();payload=i.controller.inspect_maestro.call_args.args[0]
        self.assertTrue(payload['explicit_history_selection']);self.assertEqual('vco',payload['role'])
        pending['success']({'test_names':['osc'],'selected_test':{'name':'osc'},'histories':[{'name':'Interactive.3','path':'/outside/results/Interactive.3.rdb','fully_complete':True,'tests':['osc']}]})
        self.assertEqual('',i.vars['vco_history'].get());i._inspect_pair.assert_not_called();i.controller.start_run.assert_not_called()

    def test_complete_explicit_pair_only_triggers_readonly_binding(self):
        i=self.instance(noise=True,selected='/outside/results/Interactive.3.rdb');pending={}
        i._submit=lambda label,fn,success:pending.update(success=success)
        i._inspect_role('vco')
        pending['success']({'test_names':['osc'],'selected_test':{'name':'osc'},'histories':[{'name':'Interactive.3','path':'/outside/results/Interactive.3.rdb','fully_complete':True,'tests':['osc']}]})
        i._inspect_pair.assert_called_once_with();i.controller.start_run.assert_not_called()

    def test_changed_selection_discards_pending_single_role_result(self):
        i=self.instance();pending={};i._submit=lambda label,fn,success:pending.update(success=success)
        i._inspect_role('vco');i.vars['vco_path'].set('/other/window')
        pending['success']({'selected_test':{'name':'wrong'},'test_names':['wrong']})
        self.assertEqual('',i.vars['vco_test'].get());self.assertEqual({},i.vco_test_box.options);i._inspect_pair.assert_not_called()

    def test_pair_keeps_missing_explicit_history_visible_and_unbound(self):
        i=self.instance(noise=True,selected='/outside/removed.rdb')
        for role in ('vco','replica'):
            for field in ('binding_fingerprint','history_fingerprint'):
                i.vars[f'{role}_{field}']=FakeVariable('stale')
        i.preflight_status_var=FakeVariable();i._render_mapping_report=mock.Mock()
        app.IsfApplication._pair_inspected(i,{'ok':False,'roles':{'vco':{
            'test_names':['osc'],'selected_test':{'name':'osc'},
            'histories':[{'name':'Interactive.3','path':'/outside/results/Interactive.3.rdb',
                          'fully_complete':True,'tests':['osc']}]}},'bindings':{}})
        self.assertEqual('/outside/removed.rdb',i.vars['vco_history'].get())
        self.assertEqual('',i.vars['vco_binding_fingerprint'].get())
        self.assertEqual('',i.vars['vco_history_fingerprint'].get())
        self.assertEqual('removed',i._history_display_vars['vco'].get())

    def test_loaded_other_tb_clears_old_history_choices_and_uses_its_catalog(self):
        for catalog in (None,[{'name':'New_B','rdb_file':'/B/New_B.rdb'}]):
            with self.subTest(catalog=catalog):
                i=self.instance(selected='/A/Old_A.rdb')
                for role in ('vco','replica'):
                    for field in ('binding_fingerprint','history_fingerprint'):
                        i.vars[f'{role}_{field}']=FakeVariable('')
                i.mode_var=FakeVariable();i.allow_execute_var=FakeVariable()
                i._mode_changed=mock.Mock();i._schedule_role_inspection=mock.Mock()
                i._observed_role_paths={'vco':i.vars['vco_path'].get(),'replica':''}
                i._set_history_choices('vco',[{'name':'Old_A','path':'/A/Old_A.rdb'}])
                spec={'path':'/B/maestro','history':'/B/New_B.rdb'}
                if catalog is not None:spec['history_catalog']=catalog
                i._populate_form({'testbenches':{'vco':spec,'replica':{}}})
                self.assertEqual('/B/maestro',i.vars['vco_path'].get())
                self.assertNotIn('/A/Old_A.rdb',i._history_choices['vco'].values())
                self.assertEqual({} if catalog is None else {'New_B':'/B/New_B.rdb'},i._history_choices['vco'])
                self.assertEqual('/B/New_B.rdb',i.vars['vco_history'].get())
                i._schedule_role_inspection.assert_not_called();i._inspect_pair.assert_not_called()
                i.controller.start_run.assert_not_called()

    def test_canonical_pair_paths_do_not_erase_next_history_selection(self):
        i=self.instance(noise=True);i.maestro_context={}
        i.vars['engine_config']=FakeVariable('');i._auto_engine_path=''
        i._mapping_overrides={'vco':{},'replica':{}};i._portable_overrides={}
        i.preflight_status_var=FakeVariable();i.mapping_status_var=FakeVariable()
        i._render_mapping_report=mock.Mock();i._raw_from_editor=lambda:i.raw_config
        i._observed_role_paths={};report={'ok':True,'roles':{},'bindings':{}}
        for ui_role,role,base in (('vco','vco','/A/cell'),('replica','flicker','/B/cell')):
            history={'name':'Old','path':base+'/Old.rdb','fully_complete':True,'tests':['test']}
            i.vars[f'{ui_role}_path'].set(base);i.vars[f'{ui_role}_history'].set(history['path'])
            i._observed_role_paths[ui_role]=base
            for field in ('binding_fingerprint','history_fingerprint'):
                i.vars[f'{ui_role}_{field}']=FakeVariable('')
            report['roles'][role]={'test_names':['test'],'selected_test':{'name':'test'},
                                   'histories':[history],'selected_history':history}
            report['bindings'][role]={'maestro_dir':base+'/maestro','test':'test','history':history['path'],
                                      'binding_fingerprint':'bound','history_fingerprint':'history-bound'}
        app.IsfApplication._pair_inspected(i,report)
        for role,base in (('vco','/A/cell'),('replica','/B/cell')):
            self.assertEqual(base+'/maestro',i.vars[f'{role}_path'].get())
            i.vars[f'{role}_history'].set(base+'/Another.rdb');i._mark_binding_dirty(role)
            self.assertEqual(base+'/Another.rdb',i.vars[f'{role}_history'].get())
            self.assertEqual('test',i.vars[f'{role}_test'].get())
            self.assertEqual('',i.vars[f'{role}_binding_fingerprint'].get())

if __name__=='__main__':unittest.main()
