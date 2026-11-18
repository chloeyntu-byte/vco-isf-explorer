"""Portable API/CLI contracts; no Cadence installation or native process required."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from contextlib import redirect_stdout
from isf_tool import api
from isf_tool.core import testbench
from isf_tool.core.contracts import validate
from isf_tool.commands import main


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'cds.lib').write_text('')
        self.net=self.root/'input.scs';self.net.write_text('model nch bsim4\nM1 (OP ON 0 0) nch l=1u w=2u\nM2 (ON OP 0 0) nch l=1u w=2u\n')
        self.tb=dict(schema='vco-tbgen.spec.v1',mode='create_new',workspace=str(self.root),cds_lib=str(self.root/'cds.lib'),library_path=str(self.root/'oa'/'PRIVATE'),dut=dict(lib='DUT',cell='VCO',view='schematic'),ports=[dict(name='OP',net='OP',role='output_p'),dict(name='ON',net='ON',role='output_n')],analyses=[],maestro=dict(lib='PRIVATE',cell='TB',view='maestro',test='ISF'))
        def plan(spec):return dict(schema='vco-tbgen.plan.v1',status='ready',spec_hash=api.digest(spec),operations=[])
        # Resolve TB Generator exactly like the runtime (VCO_TBGEN_ROOT or an
        # installed package); without it these contract tests cannot run.
        try:
            generator=testbench.tbgen()
        except ImportError:
            self.skipTest('VCO TB Generator not available: set VCO_TBGEN_ROOT or install vco_tbgen')
        self.backend=SimpleNamespace(isf=generator.isf,lc_hb_defaults=lambda f,**kw:dict(oscana=True,p=kw['p'],n=kw['n'],fundfreqs=[f],maxharms=['15'],oscic='lin',oscmethod='twotier',errpreset='conservative',pinnode=kw['p'],pinnodeminus=kw['n'],pinnoderank='1'),plan=mock.Mock(side_effect=plan),materialize=mock.Mock(return_value={'status':'prepared'}),__version__='test')
        self.patch=mock.patch('isf_tool.core.testbench.tbgen',return_value=self.backend);self.patch.start();self.addCleanup(self.patch.stop)
        self.api_patch=mock.patch('isf_tool.api.tbgen',return_value=self.backend);self.api_patch.start();self.addCleanup(self.api_patch.stop)
        self.overrides=dict(gm=dict(target_instance='M1'),analysis=dict(frequency_guess_hz=1e9),output=dict(positive='OP',negative='ON'))

    def prepare(self):
        return api.prepare_from_cell(dict(tbgen_spec=self.tb,topology_file=str(self.net)),workspace=str(self.root),overrides=self.overrides)

    def test_cell_schema_defaults_and_four_terminal_proof(self):
        r=self.prepare();validate(r,'PrepareResponse');self.assertEqual(r['status'],'ready')
        self.assertEqual(r['contract_version'],api.CONTRACT_VERSION)
        self.assertEqual(set(r['spec']['gm']['terminals']),set('DGSB'))
        self.assertEqual(r['spec']['gm']['terminals']['D']['resolved_net'],'OP')
        self.assertEqual(r['spec']['analysis']['noise_max_sideband'],10)
        analyses=r['spec']['extensions']['tbgen_spec']['analyses']
        self.assertEqual([a['kind'] for a in analyses],['hb','hbnoise','hbxf'])
        self.assertEqual(analyses[1]['parameters']['values'],['10000.0', '20000.0'])
        self.assertTrue(analyses[1]['parameters']['useDiscrete'])
        self.assertEqual(analyses[1]['parameters']['start'],'')
        self.assertEqual(analyses[0]['parameters']['oscmethod'],'twotier')
        self.assertEqual(analyses[0]['parameters']['maxharms'],['15'])
        self.assertEqual(analyses[0]['parameters']['pinnode'],'/OP')

    def test_complementary_override_binds_both_devices_and_saves_both_drains(self):
        self.net.write_text(
            'model nch bsim4 type=n\nmodel pch bsim4 type=p\n'
            'M1 (OP ON 0 0) nch l=1u w=2u\n'
            'P1 (ON OP VDD VDD) pch l=1u w=2u\n')
        self.overrides['gm']['complementary'] = {'target_instance': None}
        draft = self.prepare()
        self.assertEqual(draft['status'], 'needs_input')
        self.assertIn('/gm/complementary/target_instance', [item['field'] for item in draft['issues']])
        result = api.complete(draft['spec'], overrides={'/gm/complementary/target_instance': 'P1'})
        self.assertEqual(result['status'], 'ready')
        validate(result, 'PrepareResponse')
        gm = result['spec']['gm']
        self.assertEqual((gm['polarity'], gm['complementary']['polarity']), ('nmos', 'pmos'))
        self.assertEqual(gm['complementary']['terminals']['S']['resolved_net'], 'VDD')
        self.assertTrue({'/M1/D', '/P1/D', '/VDD'} <= set(result['spec']['measurements']['save']))

    def test_missing_fguess_and_gm_never_guessed(self):
        r=api.prepare_from_cell(dict(tbgen_spec=self.tb),workspace=str(self.root))
        self.assertEqual(r['status'],'needs_input');self.assertIn('/analysis/frequency_guess_hz',[i['field'] for i in r['issues']]);validate(r,'PrepareResponse')

    def test_hashes_changed_sources_unknown_major_and_invalid_output_block(self):
        r=self.prepare()
        self.net.write_text(self.net.read_text()+'//changed\n')
        with self.assertRaises(Exception):api.complete(r['spec'])
        changed=copy.deepcopy(r['spec']);changed['schema']='isf.testbench.v2'
        with self.assertRaises(Exception):api.complete(changed)
        self.overrides['output']['negative']='OP'
        r=api.prepare_from_cell(dict(tbgen_spec=self.tb),workspace=str(self.root),overrides=self.overrides)
        self.assertEqual(r['status'],'needs_input')

    def test_completed_spec_is_stable_and_materializer_gets_frozen_plan(self):
        r=self.prepare();r2=api.complete(r['spec'])
        self.assertEqual(r['spec_hash'],r2['spec_hash'])
        api.materialize(r['spec_path'],spec_hash=r['spec_hash'])
        self.backend.materialize.assert_called_once_with(r['spec']['extensions']['tbgen_spec'],r['spec']['extensions']['tbgen_plan'])
        with self.assertRaises(Exception):api.materialize(r['spec_path'],spec_hash='0'*64)

    def test_schema_validator_rejects_nonfinite_missing_and_unknown_fields(self):
        r=self.prepare()
        for key,value in [('unexpected',True),('schema','isf.testbench.v2')]:
            s=copy.deepcopy(r['spec']);s[key]=value
            with self.assertRaises(Exception):validate(s)
        s=copy.deepcopy(r['spec']);s['analysis']['offsets_hz']=[float('nan')]
        with self.assertRaises(Exception):validate(s)

    def test_idempotent_request_and_conflict_without_duplicate_process(self):
        r=self.prepare()
        with mock.patch('isf_tool.api.subprocess.Popen') as launch:
            launch.return_value.pid=12345
            first=api.run(r['spec_path'],spec_hash=r['spec_hash'],workspace=str(self.root),request_id='same')
            second=api.run(r['spec_path'],spec_hash=r['spec_hash'],workspace=str(self.root),request_id='same')
            self.assertEqual(first,second);self.assertEqual(launch.call_count,1)
            validate(first,'RunHandle')
            r2=api.complete(r['spec'],overrides={'analysis':{'offsets_hz':[20000.]}})
            with self.assertRaisesRegex(Exception,'conflict'):api.run(r2['spec_path'],spec_hash=r2['spec_hash'],workspace=str(self.root),request_id='same')
            validate(api.results(first['job_id'],workspace=str(self.root)),'ResultsResponse')

    def test_versioned_status_results_and_retry_preserve_saved_legacy_job(self):
        r=self.prepare();key=api.digest('legacy')
        path=self.root/'api-jobs'/key/'manifest.json'
        job=dict(schema='isf.run.v1',job_id=key,state='completed',spec_hash=r['spec_hash'],manifest_path=str(path),coverage=dict(expected=1,completed=1,failed=0))
        testbench.write(path,dict(job=job,issues=[]))
        original=path.read_bytes()
        retry=api.run(r['spec_path'],spec_hash=r['spec_hash'],workspace=str(self.root),request_id='legacy')
        status=api.status(key,workspace=str(self.root))
        results=api.results(key,workspace=str(self.root))
        for response in (retry,status,results,results['job']):
            self.assertEqual(response['contract_version'],api.CONTRACT_VERSION)
        validate(retry,'RunHandle');validate(results,'ResultsResponse')
        self.assertEqual(path.read_bytes(),original)
        invalid=dict(results,contract_version='2.0.0')
        with self.assertRaises(Exception):validate(invalid,'ResultsResponse')

    def test_open_ui_version_in_ready_and_failed_responses(self):
        def launch(command,**kwargs):
            ready=Path(command[command.index('--ready-file')+1]);ready.write_text('{}')
            return SimpleNamespace(pid=12345,poll=lambda:None)
        with mock.patch.object(api.subprocess,'Popen',side_effect=launch):
            ready=api.open_ui(workspace=str(self.root))
        with mock.patch.object(api.subprocess,'Popen',return_value=SimpleNamespace(pid=12346,poll=lambda:1)):
            failed=api.open_ui(workspace=str(self.root))
        for response,status in ((ready,'opened'),(failed,'failed')):
            self.assertEqual(response['contract_version'],api.CONTRACT_VERSION)
            self.assertEqual(response['status'],status);validate(response,'OpenUiResponse')

    def test_cli_exactly_one_json_object_on_missing_input_and_source_change(self):
        for args,code in [(['prepare','cell','--json'],2),(['run','--spec','missing','--json'],2),(['capabilities','--json'],0),(['prepare','bad','--json'],2)]:
            out=io.StringIO()
            with redirect_stdout(out):actual=main(args)
            self.assertEqual(actual,code);self.assertIsInstance(json.loads(out.getvalue()),dict)

    def test_b_requires_source_bound_confirmation_and_preserves_native_hb(self):
        setup=self.root/'oa'/'PRIVATE'/'TB'/'maestro';setup.mkdir(parents=True)
        for name in ('maestro.sdb','active.state'):(setup/name).write_text('<state/>')
        tb=copy.deepcopy(self.tb);tb['mode']='add_test_to_existing';tb['maestro']['source_test']='SOURCE'
        tb['analyses']=[dict(kind='hb',parameters=dict(oscana=True,p='/OP',n='/ON',fundfreqs=['1G'],maxharms=['15']))]
        r=testbench.draft('maestro',tb,workspace=str(self.root),overrides=self.overrides,topology_file=str(self.net))
        self.assertEqual('needs_input',r['status'])
        r2=api.complete(r['spec'],confirmations=[dict(field='/execution/materialization',value_hash=api.digest('source_new_test'))])
        self.assertEqual('ready',r2['status']);validate(r2,'PrepareResponse')
        self.assertIn('hb',[a['kind'] for a in r2['spec']['extensions']['tbgen_spec']['analyses']])
        r3=api.complete(r2['spec'],overrides={'analysis':{'offsets_hz':[20000.]}})
        self.assertEqual('needs_input',r3['status'])

    def test_saved_b_increment_still_inherits_authoritative_source_hb(self):
        setup=self.root/'oa/PRIVATE/TB/maestro';setup.mkdir(parents=True)
        for name in ('maestro.sdb','active.state'):(setup/name).write_text('<state/>')
        tb=copy.deepcopy(self.tb);tb['maestro']['source_test']='SOURCE'
        tb['analyses']=[dict(kind='hbnoise',parameters={})]
        options=dict(fundfreqs='("2G")',maxharms='("11")',oscmethod='"onetier"')
        selected=dict(analyses=[dict(name='hb',enabled=True,fields=options)])
        decode=lambda value:{'("2G")':['2G'],'("11")':['11'],'"onetier"':'onetier'}[value]
        with mock.patch('isf_tool.core.maestro_pair.inspect_maestro',return_value=dict(selected_test=selected)),mock.patch.dict('sys.modules',{'vco_tbgen.effective':SimpleNamespace(literal=decode)}):
            r=api.prepare_from_maestro(dict(tbgen_spec=tb,topology_file=str(self.net)),workspace=str(self.root),overrides=self.overrides)
        self.assertEqual(r['spec']['analysis']['effective'][0]['options']['oscmethod'],'onetier')
        self.assertIn('hb',[a['kind'] for a in r['spec']['extensions']['tbgen_spec']['analyses']])
        review=r['spec']['extensions']['new_test_review']
        self.assertEqual(review['inherited']['saved_setup_xml']['active.state'],'<state/>')

    def test_disabled_native_hb_does_not_supply_frequency_or_skip_completion(self):
        setup=self.root/'oa'/'PRIVATE'/'TB'/'maestro';setup.mkdir(parents=True)
        for name in ('maestro.sdb','active.state'):(setup/name).write_text('<state/>')
        tb=copy.deepcopy(self.tb);tb['maestro']['source_test']='SOURCE'
        selected=dict(analyses=[dict(name='hb',enabled=False,fields=dict(fundfreqs='("9G")'))])
        from vco_tbgen.effective import numeric
        native=SimpleNamespace(numeric=numeric,literal=mock.Mock(side_effect=AssertionError('disabled options must not be read')))
        with mock.patch('isf_tool.core.maestro_pair.inspect_maestro',return_value=dict(selected_test=selected)),mock.patch.dict('sys.modules',{'vco_tbgen.effective':native}):
            r=api.prepare_from_maestro(dict(tbgen_spec=tb,topology_file=str(self.net)),workspace=str(self.root),overrides=self.overrides)
        self.assertEqual(r['spec']['analysis']['frequency_guess_hz'],1e9)
        self.assertEqual(r['spec']['analysis']['existing'],[dict(name='hb',type='hb',enabled=False,options={})])
        self.assertIn('hb',[a['kind'] for a in r['spec']['extensions']['tbgen_spec']['analyses']])
        self.assertIn('/analysis_bundle/hb',[p['field'] for p in r['plan'] if p['op']=='add'])
        validate(r,'PrepareResponse')

    def test_disabled_tbgen_hb_requires_explicit_frequency(self):
        tb=copy.deepcopy(self.tb)
        tb['analyses']=[dict(kind='hb',enabled=False,parameters=dict(fundfreqs=['9G'],maxharms=['15']))]
        r=api.prepare_from_cell(dict(tbgen_spec=tb,topology_file=str(self.net)),workspace=str(self.root),overrides=dict(gm=self.overrides['gm'],output=self.overrides['output']))
        self.assertEqual(r['status'],'needs_input')
        self.assertIsNone(r['spec']['analysis']['frequency_guess_hz'])
        self.assertIn('/analysis/frequency_guess_hz',[i['field'] for i in r['issues']])

    def test_manual_replica_and_undersampled_noise_cannot_be_ready(self):
        r=self.prepare()
        for overrides in ({'replica':{'source':'maestro'}},{'analysis':{'dc_noise_samples':6}}):
            completed=api.complete(r['spec'],overrides=overrides)
            self.assertEqual(completed['status'],'needs_input')

    def test_cli_exit_codes_for_capability_timeout_source_and_execution(self):
        from isf_tool.core.errors import ValidationError
        cases=[(ValidationError('unsupported',code='UNSUPPORTED_CAPABILITY'),3),(TimeoutError('deadline'),5),(ValidationError('changed',code='SOURCE_CHANGED'),6),(RuntimeError('native failed'),4)]
        args=['run','--spec','unused','--spec-hash','0'*64,'--workspace',str(self.root),'--request-id','r','--json']
        for exc,expected in cases:
            out=io.StringIO()
            with mock.patch.object(api,'run',side_effect=exc),redirect_stdout(out):actual=main(args)
            self.assertEqual(actual,expected);self.assertIsInstance(json.loads(out.getvalue()),dict)

    def test_design_lab_adapter_delegates_to_tbgen(self):
        tb=copy.deepcopy(self.tb);tb['source']=dict(generated_dir=str(self.root),manifest={'graph_hash':'1'*64})
        (self.root/'tb.scs').write_text(self.net.read_text())
        self.backend.from_design_lab=mock.Mock(return_value=tb)
        r=api.prepare_from_design_lab(str(self.root),workspace=str(self.root),dut=self.tb['dut'],cds_lib=self.tb['cds_lib'],library_path=self.tb['library_path'],maestro=self.tb['maestro'],overrides=self.overrides)
        self.backend.from_design_lab.assert_called_once()
        self.assertEqual(self.backend.from_design_lab.call_args.kwargs['completeness'], 'L-b')
        self.assertEqual('ready',r['status']);validate(r,'PrepareResponse')



    def test_mismatched_source_group_aligns_copied_test_and_lists_outputs(self):
        setup=self.root/'oa/PRIVATE/TB/maestro';setup.mkdir(parents=True)
        for name in ('maestro.sdb','active.state'):(setup/name).write_text('<state/>')
        tb=copy.deepcopy(self.tb);tb['mode']='add_test_to_existing';tb['maestro']['source_test']='SOURCE'
        tb['analyses']=[dict(kind='hb',parameters=dict(fundfreqs=['1G'],maxharms=['2'],oscic='lin',oscmethod='onetier',p='/OLD',n='/ON')),dict(kind='hbnoise',parameters=dict(p='/DIFFERENT',maxsideband='3',start='1M')),dict(kind='hbxf',parameters=dict(maxsideband='19',freqaxis='in'))]
        original=copy.deepcopy(tb)
        r=testbench.draft('maestro',tb,workspace=str(self.root),overrides=self.overrides,topology_file=str(self.net))
        b=r['spec']['extensions']['analysis_bundle'];self.assertEqual(tb,original)
        self.assertEqual(b['shared']['sidebands'],19);self.assertEqual(b['shared']['positive'],'/OP')
        self.assertIn('hbnoise/p',[c['field'] for c in b['changes']]);self.assertEqual(len(b['analyses']),3)
        self.assertTrue(r['spec']['measurements']['outputs']);self.assertTrue(r['spec']['extensions']['tbgen_spec']['required']['outputs'])
        r2=api.complete(r['spec'],confirmations=[dict(field='/execution/materialization',value_hash=api.digest('source_new_test'))])
        self.assertEqual(r2['status'],'ready')
        self.assertEqual(r2['spec_hash'],api.complete(r2['spec'])['spec_hash'])

    def test_shared_temperature_is_applied_and_b_override_requires_input(self):
        r=self.prepare()
        r=api.complete(r['spec'],overrides={'conditions':{'temperature_c':45}})
        self.assertEqual(r['spec']['extensions']['tbgen_spec']['simulator_options']['temp'],'45')
        self.assertEqual(r['spec']['extensions']['analysis_bundle']['shared']['conditions']['temperature_c'],45)
        self.assertEqual(r['spec_hash'],api.complete(r['spec'])['spec_hash'])
        setup=self.root/'oa/PRIVATE/TB/maestro';setup.mkdir(parents=True)
        for name in ('maestro.sdb','active.state'):(setup/name).write_text('<state/>')
        tb=copy.deepcopy(self.tb);tb['mode']='add_test_to_existing';tb['maestro']['source_test']='SOURCE'
        b=testbench.draft('maestro',tb,workspace=str(self.root),overrides=self.overrides,topology_file=str(self.net))
        b['spec']['extensions']['inherited_conditions']=dict(temperature_c=27)
        changed=api.complete(b['spec'],overrides={'conditions':{'temperature_c':45}})
        self.assertIn('/conditions/temperature_c',[i['field'] for i in changed['issues']])

    def test_injection_is_bound_to_selected_drain_source_not_supply(self):
        r=self.prepare();tb=r['spec']['extensions']['tbgen_spec']
        self.assertEqual(tb['hbxf_injection'],dict(name='ISF_TB_INJECT',positive='OP',negative='0',target_instance='M1',probe='iprobe'))
        raw=[o for o in tb['outputs'] if 'HBXF' in o['name']]
        self.assertEqual([o['unit'] for o in raw],['V/A'])
        self.assertIn('/ISF_TB_INJECT',raw[0]['expression'])
        self.assertEqual(raw[0]['name'],'ISF_HBXF_RAW_INJECTION')
        self.assertEqual(api.complete(r['spec'])['spec_hash'],r['spec_hash'])

    def test_low_N_worker_forwards_the_reconciled_sidebands(self):
        from isf_tool import api_worker
        for sidebands in (4,10,15,19):
            with self.subTest(sidebands=sidebands):
                r=self.prepare();spec=r['spec']
                spec['analysis'].update(isf_max_harmonic=3,noise_max_sideband=sidebands)
                spec['extensions']['app_config']={'portable_setup':{'overrides':{'engine.main_max_sideband':1}}}
                path=self.root/('worker-'+str(sidebands))/'api-jobs/job/manifest.json'
                testbench.write(path,dict(spec=spec,tbgen_run={'rdb':'history.rdb'},native_results={'points':[]},
                    materialized={'materialized':{'spec':spec['extensions']['tbgen_spec']}},issues=[],stage='isf',
                    job=dict(state='running',coverage=dict(expected=1,completed=0,failed=0))))
                service=mock.Mock();service.inspect_maestro_pair.side_effect=RuntimeError('stop before any native execution')
                with mock.patch('isf_tool.core.service.ToolService',return_value=service):
                    self.assertEqual(api_worker.execute_isf(path),4)
                forwarded=service.inspect_maestro_pair.call_args.args[0]['portable_setup']['overrides']
                self.assertEqual(forwarded['engine.main_max_sideband'],sidebands)

class WorkerStageTests(unittest.TestCase):
    def test_native_and_isf_have_separate_resource_wrappers(self):
        from isf_tool import api_worker
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp);path=work/'api-jobs'/'job'/'manifest.json'
            spec=dict(extensions=dict(tbgen_root=None,tbgen_spec=dict(maestro=dict(lib='PRIVATE'))))
            job=dict(state='queued',spec_hash='0'*64,coverage=dict(expected=0,completed=0,failed=0))
            testbench.write(path,dict(spec=spec,job=job,issues=[]))
            setup=work/'setup';setup.mkdir()
            for name in ('maestro.sdb','active.state'):(setup/name).write_text('<state/>')
            rdb=work/'history.rdb';rdb.write_text('<state/>')
            backend=SimpleNamespace(run=mock.Mock(return_value={'status':'completed','rdb':str(rdb)}),results=mock.Mock(return_value={'status':'completed','points':[{'id':1}]}))
            def complete(command,**kwargs):
                self.assertEqual(command[:6], ['guard', 'lib:PRIVATE', '--', 'guard', 'sim:slots', '--'])
                self.assertIn('--isf-only', command)
                row=testbench.read(path);row['job']['state']='completed';testbench.write(path,row);return 0
            with mock.patch.dict(os.environ, VCO_ISF_COMMAND_TEMPLATE='guard {resource} -- {command}'), mock.patch.object(api,'_prepared_source'),mock.patch.object(api,'materialize',return_value=dict(status='prepared',materialized={'setup_dir':str(setup)})),mock.patch.object(api_worker,'tbgen',return_value=backend),mock.patch.object(api_worker.subprocess,'call',side_effect=complete):
                self.assertEqual(api_worker.execute(path),0)
            self.assertEqual(backend.run.call_count,1)
