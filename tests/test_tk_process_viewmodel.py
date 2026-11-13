"""Process views are lossless projections, never a second signoff calculation."""
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from isf_tool.tk.viewmodel import (engineering, linked_key, process_projection, cycle_statistics,
    thermal_cycle_projection, load_thermal_coefficients, harmonic_projection, offset_projection,
    contribution_projection, build_result_view, stored_thermal_diagnostic, noise_scope_note)
from isf_tool.tk.charts import line_segments
from isf_tool.core.dual_tb import recompute_dual_tb
from synthetic_fixture import make_canonical
from test_tk_noise_kinds_gui import thermal_fixture


class ProcessViewModelTests(unittest.TestCase):
    def test_engineering_values_units_rounding_and_missing(self):
        for value, expected in ((357e9, '357 G'), (7.96e-10, '796 p'),
                                (-2.64e-6, '-2.64 µ'), (0, '0'), (999999.9, '1 M')):
            self.assertEqual(expected, engineering(value))
        self.assertEqual('8.192 pA/√Hz', engineering(8.192e-12, 'A/√Hz'))
        for value in (None, float('nan'), float('inf'), 'N/A'):
            self.assertEqual('—', engineering(value))
        self.assertIn('字段', engineering({'one': 1}))
        self.assertEqual('True', engineering(True))

    def test_workpoint_original_units_keys_and_hard_breaks(self):
        rows = [{'sample': i, 'wt_rad': i*math.pi, 'VG_V': v, 'isf_i': v, 'product_i': v}
                for i,v in enumerate((3.,None,5.))]
        result = {'points': {'p': {'workpoint': rows}}}
        before = copy.deepcopy(result)
        projection = process_projection(result,'p')
        self.assertEqual(before,result)
        self.assertEqual(['sample:0','sample:1','sample:2'],[r['key'] for r in projection['rows']])
        trace = projection['panels'][0]['series'][0]
        self.assertEqual([(0.,3.),None,(1.,5.)],trace['points'])
        self.assertEqual(2,len(line_segments(trace['points'])))
        self.assertIn('1/3',trace['reason'])
        self.assertEqual('C^-1',projection['panels'][2]['series'][0]['unit'])
        self.assertTrue(all(p['group']=='phase' for p in projection['panels']))
        self.assertEqual(1,sum(p['show_xlabel'] for p in projection['panels']))
        self.assertIsNone(projection['panels'][3]['statistics']['Γ-I × m_dyn']['mean'])

    def test_statistics_duplicate_endpoint_and_nonuniform_sampling(self):
        stats = cycle_statistics([(0.,2.),(.25,2.),(1.,2.)])
        self.assertEqual(2.,stats['mean']);self.assertEqual(2.,stats['rms'])
        self.assertEqual(0.,cycle_statistics([(0.,-1.),(1.,1.)])['mean'])
        for points in ([],[(0.,1.)],[(0.,1.),None,(1.,1.)],[(0.,1.),(.5,1.)],[(0.,1.),(0.,1.),(1.,1.)]):
            self.assertIsNone(cycle_statistics(points)['mean'])
            self.assertTrue(cycle_statistics(points)['reason'])

    def test_missing_phase_is_not_fabricated_from_row_number(self):
        projected=process_projection({'points':{'p':{'workpoint':[{'sample':9,'isf_i':1.}]}}},'p')
        self.assertEqual([None],projected['panels'][2]['series'][0]['points'])
        self.assertIn('缺',projected['panels'][2]['reason'])

    def test_thermal_sqrt_psd_preserves_native_grid_and_missing(self):
        raw=[{'device':'M0','mechanism':'id','source_time_s':[0.,.5,1.], 'source_psd_A2_Hz':[4e-24,-1,None]}]
        before=copy.deepcopy(raw)
        projected=thermal_cycle_projection(raw,1.)
        self.assertEqual(before,raw)
        self.assertEqual('M0/id',projected['sources'][0]['id'])
        self.assertEqual([(0.,2e-12),None,None],projected['sources'][0]['series']['points'])
        self.assertIn('2/3',projected['sources'][0]['series']['reason'])
        self.assertEqual('本结果没有 thermal 源系数',thermal_cycle_projection([],1.)['reason'])

    def test_thermal_loader_exact_corner_explicit_path_missing_and_bad_file(self):
        result={'summary':[{'point':'p','operating_point_id':'ff','f0_Hz':1.}], 'points':{'p':{}}}
        with tempfile.TemporaryDirectory() as root:
            job=Path(root)
            target=job/'simulation'/'ff'/'thermal'/'source_coefficients.json'
            wrong=job/'simulation'/'tt'/'thermal'/'source_coefficients.json'
            wrong.parent.mkdir(parents=True)
            data=[{'device':'M0','mechanism':'id','source_time_s':[0.,1.], 'source_psd_A2_Hz':[1.,4.]}]
            wrong.write_text(json.dumps(data),encoding='utf8')
            self.assertFalse(load_thermal_coefficients(result,'p',job)['sources'])
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps(data),encoding='utf8')
            self.assertEqual(2,len(load_thermal_coefficients(result,'p',job)['sources'][0]['rows']))
            result['summary'][0]['thermal_noise']={'source_coefficients_path':str(target)}
            self.assertTrue(load_thermal_coefficients(result,'p')['sources'])
            target.write_text('{bad',encoding='utf8')
            self.assertIn('本结果没有 thermal 源系数',load_thermal_coefficients(result,'p',job)['reason'])

    def test_thermal_missing_mode_has_explicit_reason_keeps_flicker_product_scope(self):
        raw={'points':{'p':{'workpoint':[]}}}
        projection=process_projection(raw,'p',noise_kind='thermal')
        self.assertEqual([],projection['panels'][1]['series'])
        self.assertIn('没有 thermal 源系数',projection['panels'][1]['reason'])
        self.assertIn('flicker',projection['panels'][3]['title'])

    def test_harmonic_and_offset_keys_match_all_chart_series(self):
        raw=thermal_fixture()
        point=raw['summary'][0]['point']
        view=build_result_view(raw,point=point,noise_scope='pair',noise_kind='both')
        h=harmonic_projection(view)
        for panel in h['panels']:
            self.assertEqual([r['key'] for r in h['rows']],panel['series'][0]['keys'])
        choices=[r for r in raw['summary'] if r.get('operating_point_id')=='tt']
        if not choices: choices=[r for r in raw['summary'] if r.get('corner')=='tt']
        series=[{'id':'test','name':'test','points':[(r['offset_Hz'],None if i else -100) for i,r in enumerate(choices)]}]
        projected=offset_projection(raw,'tt',series,[])
        self.assertTrue(projected['rows'])
        self.assertEqual([r['key'] for r in projected['rows']],projected['series'][0]['keys'])
        self.assertIsNone(projected['series'][0]['points'][1][1])

    def test_contributions_keep_pair_comparison_without_counting_aggregates_as_sources(self):
        raw=thermal_fixture();view=build_result_view(raw,point=raw['summary'][0]['point'],noise_scope='pair',noise_kind='both')
        before=copy.deepcopy(view)
        projected=contribution_projection(view)
        self.assertEqual(before,view)
        self.assertTrue(projected['rows'])
        keys=[r['key'] for r in projected['rows']]
        self.assertEqual(len(keys),len(set(keys)))
        self.assertEqual('comparison:flicker_pair',projected['rows'][0]['key'])
        self.assertEqual('对称 gm pair · flicker 对比',projected['rows'][0]['label'])
        self.assertTrue(all(r['kind']=='source' for r in projected['rows'][1:]))
        for row in projected['rows']:
            if not row['modeled']:self.assertIn('不参与 ISF 计算',row['scope'])
        self.assertTrue(projected['band_rows'])
        self.assertIn('没有 thermal',contribution_projection({})['reason'])

    def test_legacy_thermal_evidence_is_visible_but_cannot_restore_eligibility(self):
        raw=thermal_fixture()
        point=raw['summary'][0]['point']
        for row in raw['summary']:
            row['thermal_noise']['scope']='gm_pair_and_passive_network'
            for source in row['thermal_noise']['sources']:
                source['phase_noise_dBc_Hz']=-100.
        before=copy.deepcopy(raw)
        view=build_result_view(raw,point=point,noise_scope='pair',noise_kind='both')
        diagnostic=stored_thermal_diagnostic(raw,point)
        projected=contribution_projection(view,diagnostic)
        self.assertTrue(diagnostic['unverified'])
        self.assertTrue(projected['diagnostic_bands'])
        self.assertTrue(projected['band_rows'])
        self.assertTrue(any(r.get('stored_thermal_dbc') is not None for r in projected['rows']))
        self.assertEqual('unavailable',view['selected_summary']['thermal_noise']['status'])
        self.assertEqual('N/A',view['metrics']['calculated_dbc'])
        self.assertEqual(before,raw)
        self.assertEqual(view,build_result_view(raw,point=point,noise_scope='pair',noise_kind='both'))

    def test_missing_thermal_scope_explains_unavailable_total_without_changing_filter(self):
        raw = thermal_fixture()
        for row in raw['summary']:
            row['thermal_noise']['scope'] = 'gm_pair_and_passive_network'
        before = copy.deepcopy(raw)
        view = build_result_view(raw, point=raw['summary'][0]['point'], noise_scope='pair', noise_kind='both')
        self.assertIn('合计不可用', noise_scope_note(view, 'both'))
        self.assertIn('切换 flicker', noise_scope_note(view, 'both'))
        self.assertNotIn('合计不可用', noise_scope_note(view, 'flicker'))
        self.assertNotIn('合计不可用', noise_scope_note({'selected_summary': {'thermal_noise': {'status': 'available'}}}, 'both'))
        self.assertEqual('N/A', view['metrics']['calculated_dbc'])
        self.assertEqual(before, raw)

    def test_dual_tb_derived_diagnostics_do_not_overwrite_input_or_signoff(self):
        canonical=make_canonical(signoff=False)
        before=copy.deepcopy(canonical)
        result=recompute_dual_tb(canonical)
        self.assertEqual(before,canonical)
        self.assertTrue(result['derived_diagnostics'])
        point=next(iter(result['points']))
        self.assertTrue(result['derived_diagnostics'][point]['curves']['gamma_i'])
        originals=[r for r in canonical['workpoint'] if r['point']==point]
        for original, displayed in zip(originals,result['points'][point]['workpoint']):
            for key, value in original.items():
                self.assertEqual(value, displayed[key])
        self.assertFalse(result['summary'][0]['signoff'])
        no_diagnostic=copy.deepcopy(result);no_diagnostic.pop('derived_diagnostics')
        self.assertEqual(build_result_view(no_diagnostic),build_result_view(result))
        missing=copy.deepcopy(result)
        missing['points'][point]['workpoint']=[]
        projection=process_projection(missing,point)
        self.assertTrue(projection['rows'])
        self.assertTrue(projection['derived'])
        self.assertIn('派生',projection['panels'][2]['series'][0]['name'])


if __name__=='__main__':unittest.main()
