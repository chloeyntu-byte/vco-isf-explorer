"""Closed-form checks plus synthetic contract regression (no solver required)."""
import copy
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from isf_tool.metrics import compute_metrics,calibrate,proxy_metrics,knob_recommendations,potential_benefit

from synthetic_metrics_fixture import records as synthetic_points


def analytic(v=None,g=None,m=None,theta=None):
    x=np.linspace(0,2*math.pi,4097) if theta is None else theta
    v=np.sin(x) if v is None else v(x)
    g=np.sin(x) if g is None else g(x)
    m=np.ones_like(x) if m is None else m(x)
    return dict(summary=[dict(point='analytic',f0_Hz=1e9,offset_Hz=1e6)],
                workpoint=[dict(wt_rad=float(t),Vdiff_V=float(a),isf_i=float(b),m_dyn_A_rtHz=float(c),m_DC_A_rtHz=float(c)) for t,a,b,c in zip(x,v,g,m)],harmonics=[])


class AnalyticMetrics(unittest.TestCase):
    def test_sine_slopes_edges_widths_areas_and_rms(self):
        data=analytic();before=copy.deepcopy(data);m=compute_metrics(data)
        w=m['waveform'];g=m['gamma'];f=m['flicker']
        self.assertAlmostEqual(w['rise_max_slope_v_rad'],1,places=9)
        self.assertAlmostEqual(w['fall_max_slope_v_rad'],1,places=9)
        self.assertAlmostEqual(w['rise_max_slope_v_s'],2*math.pi*1e9,delta=1)
        phase90=math.asin(.8)
        self.assertAlmostEqual(w['rise_time_s'],2*phase90/(2*math.pi*1e9),delta=2e-16)
        self.assertAlmostEqual(w['rise_mean_slope_v_rad'],.8/phase90,places=6)
        self.assertAlmostEqual(w['duty_cycle'],.5,delta=1/4096)
        for sign in ('positive','negative'):
            self.assertAlmostEqual(g[sign+'_fwhm_cycle'],1/3,places=6)
            self.assertAlmostEqual(g[sign+'_area'],2,places=6)
        self.assertAlmostEqual(g['rms'],1/math.sqrt(2),places=12)
        self.assertAlmostEqual(w['gamma_crest'],math.sqrt(2),places=12)
        self.assertAlmostEqual(f['c0_eff'],0,places=12)
        self.assertAlmostEqual(f['rho_dyn'],0,places=12)
        self.assertEqual(data,before)

    def test_second_harmonic_analytic_phase_and_slope_asymmetry(self):
        m=compute_metrics(analytic(v=lambda x:np.cos(x)+.1*np.sin(2*x)))
        w=m['waveform']
        self.assertAlmostEqual(w['rise_max_slope_v_rad'],.8,places=9)
        self.assertAlmostEqual(w['fall_max_slope_v_rad'],1.2,places=9)
        self.assertAlmostEqual(w['max_slope_ratio'],2/3,places=9)
        self.assertAlmostEqual(w['psi2_deg'],-90,places=8)
        self.assertAlmostEqual(w['v2_over_v1'],.1,places=9)
        self.assertIsNone(w['psi3_deg'])

    def test_weighted_effective_isf(self):
        m=compute_metrics(analytic(m=lambda x:1+.4*np.sin(x)))
        f=m['flicker'];den=math.sqrt(.5*1.08)
        self.assertAlmostEqual(f['c0_eff'],.2,places=12)
        self.assertAlmostEqual(f['rho_dyn'],.2/den,places=12)
        self.assertAlmostEqual(f['area_asym'],.4*math.pi/4,places=6)
        self.assertAlmostEqual(f['area_positive_dyn'],2+.2*math.pi,places=6)
        self.assertAlmostEqual(f['area_negative_dyn'],2-.2*math.pi,places=6)

    def test_seam_lobe_and_nonuniform_trapezoid(self):
        m=compute_metrics(analytic(g=np.cos))
        a,b=m['curves']['positive_fwhm_rad']
        self.assertLess(a,0);self.assertGreater(b,0)
        self.assertAlmostEqual((b-a)/(2*math.pi),1/3,places=6)
        x=np.linspace(0,1,1001)**1.2*2*math.pi
        m=compute_metrics(analytic(theta=x))
        self.assertAlmostEqual(m['gamma']['rms'],1/math.sqrt(2),places=5)
        self.assertEqual(m['metric_info']['gamma.rms']['status'],'measured')

    def test_missing_and_incomplete_data_are_na_and_strict_json(self):
        r=analytic();r['workpoint']=r['workpoint'][:-1];m=compute_metrics(r)
        self.assertIsNone(m['flicker']['rho_dyn'])
        self.assertEqual(m['metric_info']['flicker.rho_dyn']['status'],'N/A')
        r=analytic();r['workpoint'][3]['isf_i']=None;m=compute_metrics(r)
        self.assertIsNone(m['gamma']['rms'])
        json.dumps(m,allow_nan=False)
        m=compute_metrics(analytic(g=lambda x:np.zeros_like(x)))
        self.assertIsNone(m['waveform']['gamma_crest'])

    def test_closed_trapezoid_uses_actual_endpoint(self):
        r=analytic();r['workpoint'][-1]['isf_i']=1
        m=compute_metrics(r)
        self.assertAlmostEqual(m['flicker']['c0_eff'],.5/4096,places=12)

    def test_calibration_voltage_selection_matches_metrics_and_proxy(self):
        # Unequal drain amplitudes exposed a 33.3% Gamma/c0 scale error when
        # calibration reconstructed VDN while metrics used the measured VDN.
        x=np.linspace(0,2*math.pi,1025)
        for source in ('dual_drain','explicit_diff','single_drain'):
            with self.subTest(source=source):
                r=analytic(g=lambda t:-np.cos(t),m=lambda t:1+.2*np.cos(t),theta=x)
                for row,t in zip(r['workpoint'],x):
                    row.pop('Vdiff_V')
                    row['VD_V']=float(np.sin(t))
                    if source!='single_drain':row['VDN_V']=float(-.5*np.sin(t))
                    if source=='explicit_diff':row['Vdiff_V']=float(3*np.sin(t))
                original=copy.deepcopy(r)
                m=compute_metrics(r);c=calibrate(r)
                p=proxy_metrics(dict(theta_rad=m['curves']['theta_rad'],
                                     vdiff_v=m['curves']['vdiff_v'],f0_hz=m['f0_hz']),
                                alpha_dc=m['curves']['m_dc'],calibration=c)
                np.testing.assert_allclose(p['gamma_hat'],m['curves']['gamma'],atol=2e-12,rtol=2e-12)
                self.assertAlmostEqual(m['flicker']['c0_eff'],-.1,places=12)
                self.assertAlmostEqual(p['c0_hat'],-.1,places=12)
                self.assertAlmostEqual(p['rho_hat'],m['flicker']['rho_dyn'],places=12)
                self.assertEqual(c['waveform']['source'],m['metric_info']['vpk_v']['source'])
                self.assertEqual(c['waveform']['status'],'approximate' if source=='single_drain' else 'measured')
                json.dumps(c,allow_nan=False)
                self.assertEqual(r,original)

    def test_gate_and_benefit(self):
        m=compute_metrics(analytic())
        m['closure'].update(gap_db=3.0001,dominant_unmodeled_source='bank:id')
        self.assertTrue(all(r['kind'] in ('gate','attribution') for r in knob_recommendations(m)))
        self.assertAlmostEqual(potential_benefit(.27,1.15,.5),.719,delta=.01)
        self.assertIsNone(potential_benefit(2,1,.5))


class SyntheticMetrics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.records=synthetic_points()

    def test_analytic_synthetic_points(self):
        expected = {('new', 'band_high'): (.3, .2), ('new', 'band_low'): (.33, .2),
                    ('old', 'band_high'): (.4, -.2), ('old', 'band_low'): (.44, -.2)}
        for rec in self.records:
            r = rec['result']
            m = compute_metrics(r)
            json.dumps(m, allow_nan=False)
            for info in m['metric_info'].values():
                self.assertIn(info['status'], ('measured', 'approximate', 'N/A'))
                if info['status'] != 'measured':
                    self.assertTrue(info['reason'])
            corner = r['summary'][0]['corner']
            if (rec['name'], corner) in expected:
                modulation, phase = expected[rec['name'], corner]
                denom = math.sqrt(.5 * (1 + modulation**2/2))
                self.assertAlmostEqual(m['flicker']['rho_dyn'], -modulation*math.cos(phase)/(2*denom), places=11)
                self.assertAlmostEqual(m['flicker']['rho_dc'], -modulation/(2*denom), places=11)
            self.assertAlmostEqual(sum(m['flicker']['c_k'].values()), 1, places=12)
            self.assertEqual(m['metric_info']['waveform.psi2_deg']['status'], 'N/A')
            self.assertAlmostEqual(m['waveform']['v2_over_v1'], 0, places=9)

    def test_pair_source_membership_uses_explicit_identity_not_per_source_psd(self):
        from isf_tool.core.result_presentation import enrich_summary,noise_contribution_rows
        for rec in self.records:
            with self.subTest(name=rec['name'],point=rec['point']):
                r=rec['result'];s=r['summary'][0];m=compute_metrics(r)
                identity=enrich_summary(s)['flicker_pair_identity']
                members={identity[k] for k in ('target_noise_trace','peer_noise_trace')}
                pair_sources=[src for src in m['sources'] if src['device'] in members and src['mechanism']=='fn']
                self.assertEqual(len(pair_sources),2)
                self.assertTrue(all(src['modeled'] for src in pair_sources))
                self.assertNotIn(m['closure']['dominant_unmodeled_source'],{src['id'] for src in pair_sources})
                if rec['name'] in ('A','B-final'):
                    peer=identity['peer_noise_trace']
                    row=next(src for src in noise_contribution_rows(s) if src['kind']=='source' and src['device']==peer and src['mechanism']=='fn')
                    self.assertIsNone(row['calculated_psd_V2_Hz'])  # Membership needs no invented prediction.
        r=copy.deepcopy(next(rec['result'] for rec in self.records if rec['name']=='A'))
        s=r['summary'][0];peer=s['flicker_pair_identity']['peer_noise_trace']
        # Sharing a device name does not qualify another noise mechanism.
        s['spectre_noise_contributions'].append(dict(device=peer,mechanism='other',psd_V2_Hz=1))
        m=compute_metrics(r)
        self.assertEqual(m['closure']['dominant_unmodeled_source'],peer+':other')
        self.assertFalse(next(src for src in m['sources'] if src['id']==peer+':other')['modeled'])
        # Loss of explicit peer identity must not guess one from pair power.
        s.pop('flicker_pair_identity');s['thermal_noise']=dict(schema_version=1,status='unavailable',reason='no peer identity')
        m=compute_metrics(r)
        self.assertFalse(next(src for src in m['sources'] if src['id']==peer+':fn')['modeled'])

    def test_single_device_and_partial_modeled_closure(self):
        from isf_tool.core.result_presentation import enrich_summary
        r=copy.deepcopy(self.records[0]['result']);s=r['summary'][0]
        identity=enrich_summary(s)['flicker_pair_identity'];target=identity['target_noise_trace'];peer=identity['peer_noise_trace']
        s['pair_model']='single_device_only';s['target_noise_trace']=target
        # Enrichment must reject stale pair fields while retaining single fn.
        for thermal_mode in ('missing','unavailable','available'):
            with self.subTest(thermal=thermal_mode):
                sample=copy.deepcopy(r);row=sample['summary'][0]
                if thermal_mode=='missing':row.pop('thermal_noise')
                elif thermal_mode=='unavailable':row['thermal_noise']=dict(schema_version=1,status='unavailable',reason='test')
                enriched=enrich_summary(row);m=compute_metrics(sample)
                expected=enriched['dynamic_dBc_Hz']
                if thermal_mode=='available':expected=10*math.log10(10**(expected/10)+10**(enriched['thermal_dBc_Hz']/10))
                self.assertAlmostEqual(m['closure']['modeled_total_dbc_hz'],expected,places=12)
                self.assertAlmostEqual(m['closure']['gap_db'],enriched['spectre_total_dBc_Hz']-expected,places=12)
                if thermal_mode!='available':self.assertAlmostEqual(m['closure']['gap_db'],20 * math.log10(4),places=10)
                sources={src['id']:src for src in m['sources']}
                self.assertTrue(sources[target+':fn']['modeled'])
                self.assertFalse(sources[peer+':fn']['modeled'])
                self.assertNotEqual(m['closure']['dominant_unmodeled_source'],target+':fn')
                self.assertIn('dynamic_dBc_Hz',m['metric_info']['closure.modeled_total_dbc_hz']['source'])
                self.assertEqual(m['metric_info']['closure.modeled_total_dbc_hz']['status'],'measured')
        # Qualified thermal still counts if no flicker estimate is available.
        s['dynamic_dBc_Hz']=s['dynamic_pm_asd_V_rtHz']=None
        m=compute_metrics(r)
        self.assertAlmostEqual(m['closure']['modeled_total_dbc_hz'],enrich_summary(s)['thermal_dBc_Hz'],places=12)
        self.assertFalse(next(src for src in m['sources'] if src['id']==target+':fn')['modeled'])
        s['thermal_noise']=dict(schema_version=1,status='unavailable',reason='test')
        m=compute_metrics(r)
        self.assertIsNone(m['closure']['modeled_total_dbc_hz'])
        self.assertIsNone(m['closure']['gap_db'])
        self.assertEqual(m['closure']['gate'],'attribute_sources')
        self.assertTrue(all(not src['modeled'] for src in m['sources']))

    def test_crosscalibration_report_error_matrix(self):
        records=[r for r in self.records if r['name'] in ('new','old')]
        for target in records:
            m=compute_metrics(target['result']);x=np.asarray(m['curves']['theta_rad']);dc=np.asarray(m['curves']['m_dc']);g=np.asarray(m['curves']['gamma'])
            from isf_tool.metrics import _mean,_fourier
            truth=m['flicker']['c0_eff']
            for source in records:
                c=calibrate(source['result']);corrected=dc.copy()
                for k in range(1,4):
                    C=c['A_k'][str(k)]*np.exp(1j*math.radians(c['delta_k_deg'][str(k)]))
                    corrected+=2*np.real(_fourier(dc,x,k)*(C-1)*np.exp(1j*k*x))
                corrected+=_mean(dc,x)*(c['A_k']['0']-1)
                error=20*math.log10(abs(_mean(g*corrected,x)/truth))
                self.assertLess(abs(error),.95)

    def test_l0_derivative_correlation_and_code_order(self):
        from isf_tool.metrics import derivative
        predictions={}
        calibrations={name:calibrate(next(r['result'] for r in self.records if r['name']==name)) for name in ('new','old')}
        for rec in self.records:
            if rec['name'] not in ('new','old'):continue
            m=compute_metrics(rec['result']);c=calibrations[rec['name']]
            hb=dict(theta_rad=m['curves']['theta_rad'],vdiff_v=m['curves']['vdiff_v'],f0_hz=m['f0_hz'])
            proxy=proxy_metrics(hb,alpha_dc=m['curves']['m_dc'],calibration=c)
            corr=np.corrcoef(proxy['gamma_hat'][:-1],m['curves']['gamma'][:-1])[0,1]
            self.assertGreater(corr,.90)
            if rec['name']=='new':self.assertGreater(corr,.96)
            self.assertTrue(-1<=proxy['rho_hat']<=1)
            predictions[(rec['name'],rec['result']['summary'][0]['corner'])]=proxy['c0_hat']
        # Hold one band_high calibration per design; no target-code calibration leak.
        # Original full L0 script was absent; allow 0.12 dB for the report's
        # rounded +0.77/+0.76 code differences and explicit trapezoid convention.
        for design,limit in (('new',1.6),('old',1.6)):
            values=[]
            for rec in self.records:
                if rec['name']==design:
                    op=rec['result']['summary'][0]['corner'];truth=compute_metrics(rec['result'])['flicker']['c0_eff']
                    values.append((op,20*math.log10(abs(predictions[(design,op)]/truth))))
            self.assertLess(abs(values[1][1]-values[0][1]),limit)
            predicted=20*math.log10(abs(predictions[(design,'band_low')]/predictions[(design,'band_high')]))
            self.assertAlmostEqual(predicted,20 * math.log10(1.1),delta=.02)

    def test_global_delta1_report_and_schema_metadata(self):
        from isf_tool.metrics import _mean,_fourier
        expected=[20 * math.log10(math.cos(math.pi/10) / math.cos(.2))] * 4
        historical=[r for r in self.records if r['name'] in ('new','old')]
        schema=json.loads((Path(__file__).parents[1]/'isf_tool/schemas/vco.isf-metrics.v1.schema.json').read_text())
        for rec,error in zip(historical,expected):
            m=compute_metrics(rec['result']);x=np.asarray(m['curves']['theta_rad']);dc=np.asarray(m['curves']['m_dc']);g=np.asarray(m['curves']['gamma'])
            correction=np.exp(-1j*math.pi/10)
            corrected=dc+2*np.real(_fourier(dc,x,1)*(correction-1)*np.exp(1j*x))
            self.assertAlmostEqual(20*math.log10(abs(_mean(g*corrected,x)/m['flicker']['c0_eff'])),error,delta=.03)
            for field in schema['required']:self.assertIn(field,m)
            for path,info in m['metric_info'].items():
                for field in schema['$defs']['metricInfo']['required']:self.assertIn(field,info)
                value=m
                for part in path.split('.'):value=value[int(part)] if isinstance(value,list) else value[part]
                self.assertEqual(value is None,info['status']=='N/A')
            for prefix in ('waveform','gamma','single_ended'):
                self.assertTrue(all(prefix+'.'+k in m['metric_info'] for k in m[prefix]))

    def test_api_cli_selection_and_json(self):
        from isf_tool import api
        sample=self.records[0]['result']
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'result.json';path.write_text(json.dumps(sample))
            self.assertEqual('L2',api.metrics(path)['level'])
            result=subprocess.run([sys.executable,'run.py','metrics',str(path),'--json'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual('vco.isf-metrics.v1',json.loads(result.stdout)['schema'])
            raw=dict(points={'a':sample,'b':sample})
            with self.assertRaises(ValueError):compute_metrics(raw)
            self.assertEqual('a',compute_metrics(raw,point='a')['source']['point'])
            job=Path(tmp)/'jobs'/'job';job.mkdir(parents=True);(job/'result.json').write_text(json.dumps(sample))
            self.assertEqual('job',api.metrics('job',workspace=tmp)['source']['isf_job'])
            manifest=Path(tmp)/'api-jobs'/'native'/'manifest.json';manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps(dict(canonical_path=str(job))))
            self.assertEqual('native',api.metrics('native',workspace=tmp)['source']['isf_job'])
            manifest.write_text(json.dumps(dict(result=sample,canonical_path='missing')))
            self.assertEqual('native',api.metrics('native',workspace=tmp)['source']['isf_job'])
            self.assertEqual('a',compute_metrics(raw,point='a',offset_hz=1e6)['source']['point'])
