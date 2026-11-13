"""Synthetic pair comparison regression; no simulator data."""
import copy
import math
import unittest

from isf_tool.core.result_presentation import enrich_result
from isf_tool.tk.viewmodel import build_result_view, contribution_projection


def pair_snapshot_result():
    # Analytic powers: two independent sources and a small unmodeled source.
    # Legacy thermal payload must remain ineligible for prediction.
    summary = dict(point='synthetic@1000Hz', operating_point_id='synthetic', offset_Hz=1000.,
        carrier_peak_V=1.0, pair_model='symmetric_independent',
        dynamic_pm_asd_V_rtHz=math.sqrt(2e-8),
        predicted_pair_pm_asd_V_rtHz=math.sqrt(4e-8),
        spectre_pair_pm_asd_V_rtHz=math.sqrt(5e-8),
        spectre_total_pm_asd_V_rtHz=math.sqrt(6e-8),
        spectre_noise_reference=dict(scope='whole_vco', schema_version=1),
        spectre_noise_contributions=[
            dict(device='core_a', mechanism='fn', psd_V2_Hz=2e-8),
            dict(device='core_b', mechanism='fn', psd_V2_Hz=3e-8),
            dict(device='unrelated.core_b', mechanism='fn', psd_V2_Hz=1e-10)],
        thermal_noise=dict(schema_version=1, status='available', noise_quantity='PM_SSB',
            pm_validation='stored evidence', scope='gm_pair_and_passive_network',
            pair_model='symmetric_independent', target_noise_trace='core_b', peer_noise_trace='core_a'))
    return dict(summary=[summary], points={summary['point']:dict(workpoint=[], harmonics=[])})


class ContributionComparisonTests(unittest.TestCase):
    def test_synthetic_pair_first_row_values_and_member_scope_survive_enrichment(self):
        result=pair_snapshot_result();before=copy.deepcopy(result)
        for source in (result, enrich_result(result), enrich_result(enrich_result(result))):
            for kind in ('flicker','both'):
                view=build_result_view(source, noise_scope='pair', noise_kind=kind)
                rows=contribution_projection(view)['rows'];pair=rows[0]
                self.assertEqual('symmetric gm pair · flicker comparison',pair['label'])
                self.assertEqual('comparison:flicker_pair',pair['key'])
                self.assertAlmostEqual(10*math.log10(2*5e-8),pair['simulated_dbc'],places=10)
                self.assertAlmostEqual(10*math.log10(2*4e-8),pair['calculated_dbc'],places=10)
                self.assertAlmostEqual(100*5/6,pair['fraction_pct'],places=10)
                self.assertIn('not counted twice',pair['scope'])
                members=[r for r in rows[1:] if r.get('comparison_group')=='flicker_pair']
                self.assertEqual({'core_a','core_b'},{r['device'] for r in members})
                self.assertTrue(all(r['scope']=='included in symmetric gm pair comparison' for r in members))
                self.assertAlmostEqual(pair['fraction_pct'],sum(r['fraction_pct'] for r in members))
                # Only the aggregate comparison has a calculation. Do not
                # invent individual predictions or rehabilitate old thermal.
                self.assertTrue(all(r['calculated_dbc'] is None for r in members))
                self.assertIsNone(view['selected_summary']['predicted_total_dBc_Hz'])
                self.assertEqual('unavailable',view['selected_summary']['thermal_noise']['status'])
                self.assertIn('not modeled',rows[-1]['scope'])
                self.assertEqual(len(rows),len({r['key'] for r in rows}))
        self.assertEqual(before,result)

    def test_unknown_members_are_never_guessed_from_device_names(self):
        raw=pair_snapshot_result();raw['summary'][0].pop('thermal_noise')
        rows=contribution_projection(build_result_view(raw,noise_scope='pair'))['rows']
        self.assertTrue(all('not modeled' in r['scope'] for r in rows[1:]))
        self.assertEqual([],rows[0]['members'])

    def test_thermal_filter_omits_flicker_aggregate_and_fn_members(self):
        view=build_result_view(pair_snapshot_result(),noise_scope='pair',noise_kind='thermal')
        self.assertEqual([],contribution_projection(view)['rows'])




class ContributionRankingTests(unittest.TestCase):
    def test_dominant_unmodeled_source_ranks_above_weaker_modeled_sources(self):
        # Fictional logarithmic powers exercise ranking independently of scope.
        def source(device, mechanism, simulated, calculated=None):
            return dict(kind='source', device=device, mechanism=mechanism, label=device + ' · ' + mechanism,
                        simulated_dbc=simulated, calculated_dbc=calculated)
        view = dict(noise_contribution_rows=[
            source('core_a', 'id', -80., -81.),
            source('aux_b', 'id', -30.),
            source('core_a', 'fn', -60., -61.),
            source('aux_a', 'id', -40.),
            source('L0', 'rn', -90., -91.),
            source('missing', 'id', None)])
        rows = contribution_projection(view)['rows']
        self.assertEqual(['aux_b', 'aux_a', 'core_a', 'core_a', 'L0', 'missing'], [r['device'] for r in rows])
        self.assertFalse(rows[0]['modeled'])
        self.assertTrue(rows[2]['modeled'])
        self.assertEqual('not modeled · outside ISF calculation', rows[0]['scope'])


if __name__ == '__main__':
    unittest.main()
