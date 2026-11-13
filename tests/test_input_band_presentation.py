"""Fig.12 frequency bands and Fig.14 presentation-only invariants."""
import copy
import math
import unittest
from isf_tool.core.result_presentation import (input_band_noise_series, harmonic_noise_series,
    harmonic_projection_fractions, fit_noise_slope, band_color, enrich_summary)
from isf_tool.tk.viewmodel import (band_noise_projection, harmonic_projection, build_result_view,
    process_projection, isf_overlay_projection, thermal_band_projection)
from synthetic_fixture import make_band_result


class InputBandPresentationTests(unittest.TestCase):
    def test_total_matches_existing_predicted_total_and_is_power_upper_bound(self):
        result=make_band_result();before=copy.deepcopy(result)
        data=input_band_noise_series(result,'SYNTHETIC','both')
        bands=[s for s in data['series'] if s.get('n') is not None]
        total=next(s for s in data['series'] if s['id']=='calculated_total')
        self.assertEqual([0,1,2],[s['n'] for s in bands])
        for i,row in enumerate(result['summary']):
            self.assertLess(abs(total['points'][i][1]-enrich_summary(row)['predicted_total_dBc_Hz']),.01)
            self.assertTrue(all(s['points'][i][1]<=total['points'][i][1]+1e-10 for s in bands))
            self.assertAlmostEqual(1,sum(s['fraction'][i] for s in bands),places=12)
        self.assertEqual(before,result)
        self.assertTrue(any(s['id']=='hbnoise_total' and s['dash'] for s in data['series']))
        self.assertTrue(all('flicker' not in s['name'] and 'thermal' not in s['name'] for s in data['series']))

    def test_flicker_has_only_one_band_and_never_distributes_k_to_n(self):
        result=make_band_result();data=input_band_noise_series(result,'SYNTHETIC','flicker')
        bands=[s for s in data['series'] if s.get('n') is not None]
        self.assertEqual([0],[s['n'] for s in bands])
        self.assertAlmostEqual(result['summary'][0]['predicted_pair_pm_asd_V_rtHz']**2,bands[0]['power_V2_Hz'][0])
        both=harmonic_noise_series(result,'SYNTHETIC',scope='pair',noise_kind='both')
        self.assertEqual(['input_band_0','input_band_1','input_band_2','calculated_total','hbnoise_total'],[s['id'] for s in both])

    def test_both_without_thermal_keeps_baseline_and_explicit_partial_note(self):
        result=make_band_result()
        for row in result['summary']:row.pop('thermal_noise')
        data=input_band_noise_series(result,'SYNTHETIC','both')
        self.assertEqual([0],[s['n'] for s in data['series'] if s.get('n') is not None])
        self.assertTrue(data['partial']);self.assertIn('no valid thermal',';'.join(data['notes']))
        self.assertIsNotNone(data['series'][0]['points'][0][1])
        self.assertIsNone(enrich_summary(result['summary'][0])['predicted_total_dBc_Hz'])

    def test_single_offset_keeps_points_and_explains_slope(self):
        data=input_band_noise_series(make_band_result((1e4,)),'SYNTHETIC','thermal')
        self.assertTrue(data['single_offset']);self.assertIn('Only one offset; slope unavailable',data['notes'])
        self.assertTrue(all(s['slope'] is None and len(s['points'])==1 for s in data['series']))

    def test_projection_negative_cancellation_and_sum_to_one(self):
        data=harmonic_projection_fractions([dict(k=0,Q_real_V_rtHz=2.,Q_imag_V_rtHz=1.),
                                            dict(k=1,Q_real_V_rtHz=-1.,Q_imag_V_rtHz=-.5)])
        self.assertAlmostEqual(1.,sum(r['projection_fraction'] for r in data),delta=1e-9)
        self.assertTrue(data[0]['cancellation']);self.assertLess(data[1]['projection_fraction'],0)
        zero=harmonic_projection_fractions([dict(Q_real_V_rtHz=0,Q_imag_V_rtHz=0)])
        self.assertIsNone(zero[0]['projection_fraction'])

    def test_harmonic_bar_projection_uses_complex_sum(self):
        result=make_band_result();view=build_result_view(result,point=result['summary'][0]['point'],noise_kind='flicker',noise_scope='pair')
        data=harmonic_projection(view)
        self.assertAlmostEqual(100,sum(r['fraction_pct'] for r in data['rows']),delta=1e-7)
        self.assertEqual('bar',data['panels'][-1]['series'][0]['style'])

    def test_slope_uses_lowest_two_decades_and_skips_missing(self):
        self.assertAlmostEqual(-30,fit_noise_slope([(1e3,-30),(1e4,-60),(1e5,-90),(1e6,900)]))
        self.assertAlmostEqual(-20,fit_noise_slope([(1e3,-30),(1e4,None),(1e5,-70)]))
        self.assertIsNone(fit_noise_slope([(1e3,-30)]))
        self.assertIsNone(fit_noise_slope([(0,-30),(1e3,None)]))

    def test_palette_unique_and_fixed(self):
        self.assertEqual(32,len({band_color(n) for n in range(32)}))
        self.assertEqual(band_color(1),band_color(1))

    def test_reference_toggle_and_table_share_band_keys(self):
        result=make_band_result();data=band_noise_projection(result,'SYNTHETIC')
        self.assertEqual(2,sum(s['id'].startswith('slope_') for s in data['panels'][0]['series']))
        self.assertFalse(any(s['id'].startswith('slope_') for s in band_noise_projection(result,'SYNTHETIC',references=False)['panels'][0]['series']))
        self.assertEqual({r['key'] for r in data['table_rows']},{s['row_key'] for s in data['series']})

    def test_overlay_normalization_means_and_summary_consistency(self):
        result=make_band_result();point=result['summary'][0]['point']
        process=process_projection(result,point);before=copy.deepcopy(process)
        overlay=isf_overlay_projection(process,enrich_summary(result['summary'][0]))
        self.assertEqual(5,len(overlay['panels'][0]['series']))
        for s in overlay['panels'][0]['series']:
            self.assertAlmostEqual(1,max(abs(p[1]) for p in s['points'] if p is not None))
        self.assertAlmostEqual(overlay['ratio_db'],overlay['summary_delta_db'],delta=.01)
        # Waveform-derived means must not be replaced by the stored summary
        # delta. A deliberately discrepant reference stays discrepant.
        summary=enrich_summary(result['summary'][0])
        summary['dc_dBc_Hz']+=2.5
        changed=isf_overlay_projection(process,summary)
        self.assertEqual(overlay['ratio_db'],changed['ratio_db'])
        self.assertAlmostEqual(2.5,changed['summary_delta_db']-overlay['summary_delta_db'])
        self.assertEqual(process,before)

    def test_overlay_zero_means_are_explicitly_unavailable(self):
        # Constant analytic waveforms isolate the zero numerator/denominator.
        for dc, dyn in ((0., 1.), (1., 0.), (0., 0.)):
            with self.subTest(dc=dc, dyn=dyn):
                process = {'rows': [dict(key=str(i), phase=i, isf_i=1.,
                    m_DC_A_rtHz=dc, m_dyn_A_rtHz=dyn) for i in (0, 1)]}
                overlay = isf_overlay_projection(process, {})
                self.assertEqual(dc, overlay['means']['product_dc'])
                self.assertEqual(dyn, overlay['means']['product_overlay_dyn'])
                self.assertIsNone(overlay['ratio_db'])
                self.assertIn('the period mean is zero', overlay['note'])

    def test_overlay_signed_means_use_magnitudes(self):
        process = {'rows': [dict(key=str(i), phase=i, isf_i=1.,
            m_DC_A_rtHz=-2., m_dyn_A_rtHz=1.) for i in (0, 1)]}
        overlay = isf_overlay_projection(process, {})
        self.assertAlmostEqual(20 * math.log10(2), overlay['ratio_db'])

    def test_thermal_source_band_bars_are_independent(self):
        result=make_band_result();view=build_result_view(result,point=result['summary'][0]['point'],noise_kind='thermal',noise_scope='pair')
        data=thermal_band_projection(view)
        self.assertEqual(9,len(data['rows']));self.assertEqual(3,len(data['panels'][0]['series']))

if __name__=='__main__':unittest.main()
