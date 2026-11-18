"""Session protocol failures and private handoff; no real user session touched."""
import os
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock
from tests import test_testbench_api as fixtures
from isf_tool import api
from isf_tool.core import session_add,testbench


class SessionAddTests(unittest.TestCase):
    setUp=fixtures.ApiTests.setUp
    # Reuse the portable adapter fixture and fake dependency.
    def prepared_b(self):
        setup=self.root/'oa/PRIVATE/TB/maestro';setup.mkdir(parents=True)
        for name in ('maestro.sdb','active.state'):(setup/name).write_text('fixture')
        tb=copy.deepcopy(self.tb);tb['mode']='add_test_to_existing';tb['maestro']['source_test']='SOURCE'
        tb['saves']=[dict(name='/LEGACY',kind='net')];tb['outputs']=[dict(name='OLD_WAVE',expression='getData("/OP")')]
        r=testbench.draft('maestro',tb,workspace=str(self.root),overrides=dict(self.overrides,gm=dict(target_instance='M1',peer_instances=['M2'])),topology_file=str(self.net))
        return api.complete(r['spec'],confirmations=[dict(field='/execution/materialization',value_hash=api.digest('source_new_test'))])

    def handle(self):
        r=self.prepared_b();script=self.root/'transaction.il';script.write_text('printf("generated transaction")')
        context=dict(session='session1',window_id=1,setup_handle=11,dirty=False,generation='unique-1',bridge_dir=str(self.root/'bridge'),lcv=dict(lib='PRIVATE',cell='TB',view='maestro'))
        patch=mock.patch.object(session_add,'tbgen',return_value=self.backend);patch.start();self.addCleanup(patch.stop)
        self.backend.plan_session_add=mock.Mock(return_value={'schema':'vco-tbgen.session-plan.v1'})
        self.backend.materialize_session_add=mock.Mock(return_value=dict(skill_path=str(script),receipt_path=str(self.root/'receipt.json')))
        with mock.patch.dict('sys.modules',{'vco_tbgen.session':SimpleNamespace(disk_revision=lambda p:'revision')}):
            h=api.prepare_session_add(r['spec_path'],spec_hash=r['spec_hash'],context=context)
        return r,h

    def test_required_scope_and_explicit_peer_survive_completion(self):
        r=self.prepared_b();tb=r['spec']['extensions']['tbgen_spec']
        self.assertEqual(tb['required']['analyses'],['hb','hbnoise','hbxf'])
        self.assertNotIn('/LEGACY',tb['required']['saves']);self.assertTrue(tb['required']['outputs']);self.assertTrue(all(o['name'].startswith('ISF_') for o in tb['required']['outputs']))
        self.assertEqual(tb['outputs'][0]['name'],'OLD_WAVE')
        self.assertEqual(r['spec']['gm']['peer_instances'],['M2'])
        self.assertEqual(r['spec']['extensions']['new_test_review']['outputs'],tb['outputs'])

    def test_dispatch_rejects_changed_script_and_source(self):
        r,h=self.handle();p=Path(h['bridge_dir'])
        Path(self.backend.materialize_session_add.return_value['skill_path']).write_text('changed')
        with self.assertRaisesRegex(Exception,'script changed'):session_add.dispatch(p)
        self.assertFalse((p/'request.il').exists())

    def test_rejection_never_materializes_or_runs(self):
        r,h=self.handle();p=Path(h['bridge_dir']);(p/'native-status.txt').write_text('rejected\n')
        self.assertEqual(api.session_add_status(h)['status'],'rejected')
        with self.assertRaisesRegex(Exception,'did not succeed'):api.accept_session_add(h)
        self.backend.materialize.assert_not_called()
        self.assertFalse(api._materialization_path(r['spec']).exists())

    def test_receipt_identity_and_preservation_are_mandatory(self):
        r,h=self.handle();p=Path(h['bridge_dir']);pending=testbench.read(p/'pending.json')
        (p/'native-status.txt').write_text('executed\n')
        receipt=dict(schema='vco-tbgen.session-receipt.v1',status='added',context=pending['context'],new_test='ISF',original_tests_verified=True,session_closed=False)
        for change in [dict(context={}),dict(new_test='OTHER'),dict(original_tests_verified=False),dict(session_closed=True)]:
            testbench.write(self.root/'receipt.json',dict(receipt,**change))
            with self.assertRaises(Exception):api.session_add_status(h)
        testbench.write(self.root/'receipt.json',receipt);self.assertEqual(api.session_add_status(h)['status'],'added')

    def test_submit_wraps_guardian_for_library_and_sim_resources(self):
        r,h=self.handle()
        with mock.patch.dict(os.environ, VCO_ISF_COMMAND_TEMPLATE='guard {resource} -- {command}'), mock.patch.object(session_add.subprocess,'Popen',return_value=SimpleNamespace(pid=123)) as process:
            response=api.submit_session_add(h)
        command=process.call_args.args[0]
        self.assertEqual(command[:6], ['guard', 'lib:PRIVATE', '--', 'guard', 'sim:slots', '--'])
        self.assertEqual(response['status'],'awaiting_session_confirmation')
        self.assertFalse((Path(h['bridge_dir'])/'request.il').exists())
        with self.assertRaisesRegex(Exception,'already submitted'):api.submit_session_add(h)

    def added_receipt(self,handle):
        bridge=Path(handle['bridge_dir']);p=testbench.read(bridge/'pending.json')
        receipt=dict(schema='vco-tbgen.session-receipt.v1',status='added',context=p['context'],new_test='ISF',original_tests_verified=True,session_closed=False)
        testbench.write(self.root/'receipt.json',receipt)
        (bridge/'native-status.txt').write_text('executed\n')
        testbench.write(self.root/'verify-snapshot.json',{'saved':True})
        return p

    def test_dispatch_refuses_changed_source_before_publishing(self):
        r,h=self.handle();self.net.write_text(self.net.read_text()+'// source changed\n')
        with self.assertRaisesRegex(Exception,'SOURCE_CHANGED'):session_add.dispatch(h['bridge_dir'])
        self.assertFalse((Path(h['bridge_dir'])/'request.il').exists())

    def test_accept_checks_transaction_snapshot_before_private_copy(self):
        r,h=self.handle();self.added_receipt(h)
        provider=SimpleNamespace(logical_snapshot=lambda _: {'changed':True})
        with mock.patch.dict('sys.modules',{'vco_tbgen.session':provider}):
            with self.assertRaisesRegex(Exception,'after transaction readback'):api.accept_session_add(h)
        self.assertFalse(api._materialization_path(r['spec']).exists())

    def test_accept_copies_verified_setup_without_history_or_source_write(self):
        r,h=self.handle();p=self.added_receipt(h)
        source=Path(r['spec']['source']['maestro_dir'])
        (source/'maestro.sdb').write_text('<setup><history>external history</history><active>saved setup</active></setup>')
        original={n:(source/n).read_bytes() for n in ('maestro.sdb','active.state')}
        provider=SimpleNamespace(logical_snapshot=lambda _: {'saved':True})
        with mock.patch.dict('sys.modules',{'vco_tbgen.session':provider}):response=api.accept_session_add(h)
        target=Path(response['materialized']['setup_dir'])
        self.assertNotEqual(target,source);self.assertNotIn('<history>',(target/'maestro.sdb').read_text())
        self.assertEqual(original,{n:(source/n).read_bytes() for n in original})
        self.assertTrue(list(target.glob('maestro.sdb.bak-*')))
        self.assertNotEqual(response['materialized']['spec']['maestro']['lib'],'PRIVATE')
        self.assertEqual(api.materialize(r['spec_path'],spec_hash=r['spec_hash']),response)
        self.backend.materialize.assert_not_called()

    def test_guardian_imports_package_from_unrelated_working_directory(self):
        import os,sys,subprocess,time
        r,h=self.handle();bridge=Path(h['bridge_dir']);fakebin=self.root/'bin';fakebin.mkdir()
        wrapper=fakebin/'guard'
        wrapper.write_text('#!'+sys.executable+'\nimport os,sys\nargs=sys.argv[sys.argv.index("--")+1:]\nos.execvp(args[0],args)\n')
        wrapper.chmod(0o700)
        spawned=[];launch=subprocess.Popen
        def start(*args,**kwargs):
            kwargs['cwd']=self.root
            process=launch(*args,**kwargs);spawned.append(process);return process
        with mock.patch.dict(os.environ,{'PATH':str(fakebin)+os.pathsep+os.environ['PATH'],
                                             'VCO_ISF_COMMAND_TEMPLATE': 'guard {resource} -- {command}'}),mock.patch.object(session_add.subprocess,'Popen',side_effect=start):
            api.submit_session_add(h)
        process=spawned[0]
        try:
            deadline=time.monotonic()+8
            while not (bridge/'request.il').exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue((bridge/'request.il').exists(),(bridge/'guard.log').read_text())
            (bridge/'native-status.txt').write_text('rejected\n')
            self.assertEqual(process.wait(timeout=8),0,(bridge/'guard.log').read_text())
            self.assertEqual(api.session_add_status(h)['status'],'rejected')
        finally:
            if process.poll() is None:process.terminate();process.wait(timeout=8)
