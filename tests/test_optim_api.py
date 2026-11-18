"""Numerical/contract/guard tests for the independent optimizer; no solver."""
import copy
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import numpy as np
from isf_tool.optim import evaluate, calibrate, gate, rank_knobs
from isf_tool.optim.adapters import ask_tell, design_lab_evaluator
from isf_tool.optim.waveform import lookup_alpha, read_hb
from isf_tool.optim.worker import count_calls
from test_optimization_metrics import analytic


def reference():
    x=np.linspace(0,2*math.pi,513)
    r=analytic(g=lambda x:-np.cos(x),m=lambda x:1+.3*np.cos(x),theta=x)
    c=calibrate({'result':r,'offset_hz':1e6})
    ref=dict(hb_waveform=dict(theta_rad=x.tolist(),vdiff_v=np.sin(x).tolist(),f0_hz=1e9),alpha_dc=(1+.3*np.cos(x)).tolist())
    return ref,c


def qualified_metric(metric):
    """Explicit complete evidence for tests of unrelated gate/ranking rules."""
    metric.setdefault('quality', {}).update(numerical_quality_pass=True,
        pm_magnitude_projection_valid=True, thermal_available=True, flicker_available=True)
    flicker = metric.setdefault('flicker', {})
    flicker.setdefault('rho_dyn', .1)
    flicker.setdefault('c0_eff', 1.)
    flicker['c_k'] = dict({'0': 0., '1': 1., '2': 0., '3': 0.}, **flicker.get('c_k', {}))
    metric['closure'].update(spectre_total_dbc_hz=-100., modeled_total_dbc_hz=-101.)
    return metric


class OptimTests(unittest.TestCase):
    def test_closed_form_proxy_and_zero_new_calls(self):
        ref,c=reference(); original=copy.deepcopy((ref,c))
        out=evaluate(ref,calibration=c,offsets_hz=[1e6])
        self.assertAlmostEqual(out['metrics'][0]['c0_hat'],-.15,places=11)
        self.assertEqual(out['cost']['spectre_calls'],0);self.assertTrue(out['cost']['reused'])
        self.assertEqual((ref,c),original);self.assertFalse(out['signoff'])
        self.assertFalse(out['gate']['isf_shaping_allowed']);json.dumps(out,allow_nan=False)

    def test_schema_response_and_calibration(self):
        from isf_tool.optim.schema import validate
        ref,c=reference()
        validate(evaluate(ref,calibration=c,offsets_hz=[1e6]));validate(c,'Calibration')
        bad=evaluate(ref,calibration=c,offsets_hz=[1e6]);bad['cost']['spectre_calls']=-1
        with self.assertRaisesRegex(ValueError,'invalid number'):validate(bad)

    def test_offset_and_identity_validation(self):
        ref,c=reference()
        for offsets in ([0],[float('nan')],[1e6,1e6],[]):
            with self.subTest(offsets=offsets),self.assertRaises(ValueError):evaluate(ref,calibration=c,offsets_hz=offsets)
        with self.assertRaises(ValueError):evaluate(ref,calibration=c)
        c['identity']='family-A'
        with self.assertRaises(ValueError):evaluate(ref,calibration=c,offsets_hz=[1e6])
        ref['identity']='family-A';evaluate(ref,calibration=c,offsets_hz=[1e6])

    def test_voltage_alone_cannot_fake_proxy(self):
        ref,c=reference();ref.pop('alpha_dc')
        with self.assertRaisesRegex(ValueError,'voltage alone'):evaluate(ref,calibration=c,offsets_hz=[1e6])

    def test_l1_requires_actual_noise_exact_offset(self):
        raw=dict(status='measured',metrics=dict(frequency_hz=1e9,pn_100000_dbc_hz=-100))
        m=evaluate({'result':raw},level='L1');self.assertEqual(m['metrics'][0]['pn_dbc_hz'],-100)
        with self.assertRaisesRegex(ValueError,'Exact HBnoise'):evaluate({'result':raw},level='L1',offsets_hz=[1e6])
        with self.assertRaises(ValueError):evaluate({'result':analytic()},level='L2',offsets_hz=[1e6])

    def test_g_gate_exact_boundaries_and_pair(self):
        for gap,error,allowed in [(3,0,True),(3.001,0,False),(2,.5,False),(2,-.49,True),(-.51,0,False),(None,0,False),(0,None,False)]:
            m=qualified_metric(dict(closure=dict(gap_db=gap,pair_error_db=error)))
            self.assertEqual(gate(m)['isf_shaping_allowed'],allowed,(gap,error))
        self.assertFalse(gate(dict(schema='vco.isf-optim-evaluation.v1',metrics=[]))['isf_shaping_allowed'])

    def test_rank_maps_actual_recipe_and_does_not_invent_gain(self):
        m=dict(closure=dict(gap_db=1,pair_error_db=.1),flicker=dict(rho_dyn=.2,rho_dc=0,c_k={'1':1,'2':.2,'3':.3},delta_k_deg={'1':-18}),
               single_ended=dict(psi2_deg=30),thermal=dict(band_frac={},overlap={}),potential_db=[])
        qualified_metric(m)
        r=rank_knobs(m,dict(two_f=dict(step='ratio',knobs=['aux_cap_scale','k_scale'])))
        self.assertIn('k_scale',[x['param'] for x in r]);self.assertTrue(all(x['potential_gain_db'] is None for x in r))
        self.assertNotIn('core_length',[x['param'] for x in r])
        m['closure']['gap_db']=4
        self.assertEqual([x['param'] for x in rank_knobs(m,{})],['source_attribution'])

    def test_empirical_lut_bounds_and_known_value(self):
        lut=dict(schema='vco.isf-dc-lut.v1',samples=[[x,0,0,x+1] for x in [0,.25,.5,.75,1]])
        w=dict(vgs_v=[.5],vds_v=[0],vbs_v=[0]);self.assertEqual(lookup_alpha(w,lut),[1.5])
        lut['samples']=[[x,0,0,-x-1] for x in [0,.25,.5,.75,1]]
        self.assertEqual(lookup_alpha(w,lut),[-1.5])
        w['vgs_v']=[2]
        with self.assertRaisesRegex(ValueError,'outside'):lookup_alpha(w,lut)

    def test_hb_peak_phasor_convention(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'hb.fd.pss_hb'
            p.write_text('HEADER\nVALUE\n"freq" 0\n"OUTP" (1 0)\n"OUTN" (1 0)\n"freq" 1e9\n"OUTP" (0.5 0)\n"OUTN" (-0.5 0)\nEND\n')
            w=read_hb(d);self.assertAlmostEqual(w['vdiff_v'][0],1);self.assertAlmostEqual(w['vdiff_v'][256],-1)
            p.write_text('not a PSF file')
            with self.assertRaisesRegex(ValueError,'ASCII PSF'):read_hb(d)

    def test_live_refs_need_explicit_workspace_and_do_not_launch(self):
        with mock.patch('subprocess.run',side_effect=AssertionError('must not execute')):
            with self.assertRaisesRegex(ValueError,'workspace'):evaluate(dict(kind='design_lab',project_path='p.json'),level='L1')

    def test_cost_deduplicates_engine_receipt_and_spectre_log(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'one/psf').mkdir(parents=True)
            (p/'one/run_environment.json').write_text(json.dumps(dict(simulation_completed=True,execution_mode='fresh_simulation')))
            (p/'one/psf/spectre.out').write_text('spectre completes with 0 errors')
            self.assertEqual(count_calls(p),1)
            (p/'two').mkdir()
            (p/'two/solver-process.json').write_text(json.dumps(dict(schema='vco.solver.process.v1',pid=123)))
            self.assertEqual(count_calls(p),2)
            (p/'mirror/psf').mkdir(parents=True)
            (p/'mirror/psf/spectre.out').write_text('Current working directory: '+str(p/'one/netlist')+'\nPID: 123')
            self.assertEqual(count_calls(p),2)

    def test_evaluator_requires_constraints_and_qualified_gate(self):
        ref,c=reference()
        factory=lambda p,d:ref
        # Default offset differs deliberately: use an offset-matched calibration.
        c['source']['offset_hz']=1e5
        e=design_lab_evaluator(factory,calibration=c)
        self.assertEqual(e({},'.')['status'],'not_evaluated')
        checks=lambda p,d:dict(status='completed',feasible=True,total_violation=0,isf_metrics=qualified_metric(dict(closure=dict(gap_db=1,pair_error_db=0))))
        e=design_lab_evaluator(factory,calibration=c,constraints=checks)
        self.assertTrue(e({},'.')['feasible'])
        checks2=lambda p,d:dict(status='completed',feasible=True,total_violation=0)
        e=design_lab_evaluator(factory,calibration=c,constraints=checks2)
        self.assertFalse(e({},'.')['feasible'])

    def test_ask_tell_rows_and_failed_exclusion(self):
        class Optimizer:
            def __init__(self):self.told=[]
            def ask(self,n):return [dict(k=i) for i in range(n)]
            def tell(self,rows):self.told.extend(rows)
        opt=Optimizer()
        e=lambda p,d:dict(status='completed',feasible=True,total_violation=0,merit=.1,objectives=[.1])
        history=ask_tell(opt,lambda k,i:({},str(i)),e,budget=3,batch_size=2)
        self.assertEqual(len(history),3);self.assertEqual(opt.told[0]['metrics']['objectives'],[{'score':.1}])
        with self.assertRaises(ValueError):ask_tell(opt,None,None,budget=0)

    def test_invalid_live_l0_is_rejected_before_acquisition(self):
        with mock.patch('subprocess.run',side_effect=AssertionError('must not execute')):
            with self.assertRaisesRegex(ValueError,'calibration'):
                evaluate(dict(kind='design_lab',project_path='project.json',workspace='/unused'))

    def test_explicit_numerical_failure_closes_gate(self):
        self.assertFalse(gate(dict(closure=dict(gap_db=1,pair_error_db=.1),
                              quality=dict(numerical_quality_pass=False)))['isf_shaping_allowed'])

    def test_l1_preserves_quality_and_cannot_gate_on_flicker_only(self):
        raw=analytic()
        raw['summary'][0].update(
            spectre_noise_reference=dict(scope='whole_vco',schema_version=1),
            spectre_total_dBc_Hz=-120, predicted_pair_dBc_Hz=-121,
            spectre_pair_dBc_Hz=-121, numerical_quality_pass=False)
        out=evaluate({'result':raw},level='L1',offsets_hz=[1e6])
        self.assertEqual(out['metrics'][0]['pn_dbc_hz'],-120)
        self.assertIs(out['metrics'][0].get('quality',{}).get('numerical_quality_pass'),False)
        self.assertFalse(out['gate']['isf_shaping_allowed'])
        raw['summary'][0]['numerical_quality_pass']=True
        out=evaluate({'result':raw},level='L1',offsets_hz=[1e6])
        self.assertFalse(out['gate']['isf_shaping_allowed'])
        self.assertIn('thermal',out['gate']['reason'])

    def test_cmr_recipe_uses_actual_component_variables(self):
        m=dict(closure=dict(gap_db=1,pair_error_db=.1),
               flicker=dict(rho_dyn=.2,rho_dc=.1,c_k={'1':1,'2':.2,'3':0},delta_k_deg={}),
               single_ended=dict(psi2_deg=30),thermal=dict(band_frac={},overlap={}),potential_db=[])
        qualified_metric(m)
        recipe=dict(two_f=dict(step='cmr_z11',vars={
            'LH1':['l',5e-11,8e-10],'LH2':['l',5e-11,8e-10],
            'KH1H2':['coupling',-.85,.85],'CH':['c',3e-14,4e-12]}))
        self.assertEqual({r['param'] for r in rank_knobs(m,recipe)}, {'LH1','LH2','KH1H2','CH'})

    def test_candidate_l2_failure_cannot_reuse_baseline_gate(self):
        baseline=dict(closure=dict(gap_db=1,pair_error_db=0))
        checks=lambda p,d:dict(status='completed',feasible=True,total_violation=0,isf_metrics=baseline)
        result=dict(schema='vco.isf-optim-evaluation.v1',metrics=[dict(
            flicker=dict(rho_dyn=.1),closure=dict(gap_db=1,pair_error_db=.6))])
        with mock.patch('isf_tool.optim.adapters.isf_evaluate',return_value=result):
            out=design_lab_evaluator(lambda p,d:{},level='L2',constraints=checks)({},'.')
        self.assertFalse(out['feasible'])
        self.assertIn('pair_error',out['violations'][0])

    def test_invalid_lut_domain_controls_cannot_disable_bounds(self):
        lut=dict(schema='vco.isf-dc-lut.v1',samples=[[x,0,0,x+1] for x in [0,.25,.5,.75,1]])
        wave=dict(vgs_v=[.375],vds_v=[0],vbs_v=[0])
        for field in ('voltage_tolerance_v','max_distance'):
            for value in (float('nan'),float('inf'),-1):
                with self.subTest(field=field,value=value), self.assertRaises(ValueError):
                    lookup_alpha(wave,dict(lut,**{field:value}))

    def test_cli_is_headless_and_errors_json(self):
        root=Path(__file__).parents[1]
        proc=subprocess.run([sys.executable,'-B','run.py','optim','evaluate','/does-not-exist'],cwd=root,text=True,capture_output=True)
        self.assertEqual(proc.returncode,2);self.assertEqual(json.loads(proc.stdout)['status'],'failed')


if __name__=='__main__':unittest.main()
