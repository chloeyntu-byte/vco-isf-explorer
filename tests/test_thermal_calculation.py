"""Independent white-noise algebra/normalization oracles, no EDA execution."""

import cmath
import math
import unittest

from isf_tool import thermal as t


class ThermalWhiteTests(unittest.TestCase):
    def test_constant_white_source_sums_independent_band_powers(self):
        result = t.calculate_white_thermal({-1: 1, 0: 0, 1: -1}, {0: 2}, carrier_peak=2)
        self.assertEqual(8, result["total_psd_V2_Hz"])
        self.assertAlmostEqual(10 * math.log10(4), result["dBc_Hz"])
        self.assertIsNone(result["phase_noise_dBc_Hz"])
        self.assertEqual([4, 0, 4], [row["psd_V2_Hz"] for row in result["input_band_contributions"]])

    def test_paths_within_one_source_band_remain_coherent(self):
        # a(t)=1-cos(t), h(t)=1+cos(t): product=sin^2(t), mean square=3/8.
        result = t.calculate_white_thermal({-1: .5, 0: 1, 1: .5}, {-1: -.5, 0: 1, 1: -.5})
        self.assertEqual(.375, result["total_psd_V2_Hz"])
        middle = next(row for row in result["input_band_contributions"] if row["input_band"] == 0)
        self.assertEqual(.25, middle["psd_V2_Hz"])

    def test_parseval_matches_independent_analytic_integral(self):
        # (1+cos t)(1+.5cos t)=1.25+1.5cos t+.25cos 2t.
        expected = 1.25**2 + 1.5**2/2 + .25**2/2
        h = {-1: .5, 0: 1, 1: .5}
        a = {-1: .25, 0: 1, 1: .25}
        spectral = t.calculate_white_thermal(h, a)
        times = [2 * math.pi * i/2048 for i in range(2049)]
        time_result = t.time_weighted_white_psd(times, [1 + math.cos(x) for x in times],
                        [(1 + .5 * math.cos(x))**2 for x in times], period=2*math.pi)
        self.assertEqual(expected, spectral["total_psd_V2_Hz"])
        self.assertAlmostEqual(expected, time_result["total_psd_V2_Hz"], places=12)
        recovered = t.fourier_amplitude_from_psd(times, [(1+.5*math.cos(x))**2 for x in times], period=2*math.pi, max_harmonic=1)
        for k in a:
            self.assertAlmostEqual(0, abs(a[k] - recovered[k]), places=12)

    def test_shared_time_origin_shift_does_not_change_power(self):
        h, a, delta = {-1: 2j, 0: 1+3j, 1: -2}, {-1: .3-.2j, 0: 2, 1: .3+.2j}, .73
        initial = t.calculate_white_thermal(h, a)["total_psd_V2_Hz"]
        shifted = t.calculate_white_thermal({k:v*cmath.exp(-1j*k*delta) for k,v in h.items()},
                                            {k:v*cmath.exp(1j*k*delta) for k,v in a.items()})
        self.assertAlmostEqual(initial, shifted["total_psd_V2_Hz"], places=12)

    def test_nonuniform_time_weights_are_used(self):
        result = t.time_weighted_white_psd([0, .1, 1], [1, 1, 1], [1, 3, 1], period=1)
        self.assertEqual(2, result["total_psd_V2_Hz"])

    def test_convolution_support_cannot_silently_drop_edges(self):
        with self.assertRaisesRegex(ValueError, "omits"):
            t.calculate_white_thermal({-1: 1, 0: 1, 1: 1}, {-1: 1, 0: 3, 1: 1}, input_bands=[-1,0,1])

    def test_explicit_harmonic_limit_bounds_budget_and_reports_omitted_power(self):
        h, a = {-1: .5, 0: 1, 1: .5}, {-1: -.5, 0: 1, 1: -.5}
        for limit in (0, 1, 2):
            result = t.calculate_white_thermal(h, a, max_input_harmonic=limit)
            self.assertTrue(all(abs(row["input_band"]) <= limit for row in result["input_band_contributions"]))
            self.assertAlmostEqual(.375, result["total_psd_V2_Hz"] + result["input_band_truncation"]["omitted_psd_V2_Hz"])
        self.assertEqual(.25, t.calculate_white_thermal(h, a, max_input_harmonic=0)["total_psd_V2_Hz"])
        for bad in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                t.calculate_white_thermal(h, a, max_input_harmonic=bad)

    def test_finite_units_shapes_and_real_waveform_symmetry(self):
        bad_calls = [lambda: t.calculate_white_thermal({}, {0: 1}),
            lambda: t.calculate_white_thermal({-1: 1, 1: 1}, {0: 1}),
            lambda: t.calculate_white_thermal({0: float('nan')}, {0: 1}),
            lambda: t.calculate_white_thermal({0: 1}, {0: 1j}),
            lambda: t.calculate_white_thermal({0: 1}, {-1: 1j, 0: 1, 1: 1j}),
            lambda: t.calculate_white_thermal({0: 1}, {0: 1}, source_psd_units='V^2/Hz'),
            lambda: t.time_weighted_white_psd([0,1], [1,1], [-1,-1], period=1),
            lambda: t.time_weighted_white_psd([0,.5], [1,1], [1,1], period=1),
            lambda: t.time_weighted_white_psd([0,1], [1], [1,1], period=1),
            lambda: t.time_weighted_white_psd([0,1], [1,2], [1,1], period=1)]
        for call in bad_calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()

    def test_independent_sources_do_not_cancel_and_ids_are_unique(self):
        first = t.calculate_white_thermal({0: 2}, {0: 3}, source_id='a')
        second = t.calculate_white_thermal({0: -2}, {0: 3}, source_id='b')
        self.assertEqual(72, t.combine_independent_sources([first, second])["total_psd_V2_Hz"])
        with self.assertRaises(ValueError):
            t.combine_independent_sources([first, first])


class ThermalCovarianceTests(unittest.TestCase):
    def test_correlated_modes_cancel_but_independent_modes_do_not(self):
        correlated = {(0,0):1,(0,1):1,(1,0):1,(1,1):1}
        independent = dict(correlated, **{})
        independent[0,1]=independent[1,0]=0
        self.assertEqual(0, t.calculate_covariance_thermal({0:1,1:-1}, correlated)["total_psd_V2_Hz"])
        self.assertEqual(2, t.calculate_covariance_thermal({0:1,1:-1}, independent)["total_psd_V2_Hz"])

    def test_complex_cross_spectra_are_not_discarded(self):
        result = t.calculate_covariance_thermal({0:1,1:1j}, {(0,0):1,(0,1):1j,(1,0):-1j,(1,1):1})
        self.assertAlmostEqual(4, result["total_psd_V2_Hz"])
        self.assertAlmostEqual(4, sum(row['real_V2_Hz'] for row in result['covariance_terms']))

    def test_tiny_independent_variance_is_not_thresholded_away(self):
        result = t.calculate_covariance_thermal({0:0,1:1}, {(0,0):1,(0,1):0,(1,0):0,(1,1):1e-24})
        self.assertEqual(1e-24, result["total_psd_V2_Hz"])

    def test_missing_nonhermitian_or_indefinite_covariance_is_rejected(self):
        for c in ({(0,0):1,(1,1):1}, {(0,0):1,(0,1):1j,(1,0):1j,(1,1):1},
                  {(0,0):1,(0,1):2,(1,0):2,(1,1):1}, {(0,0):-1,(0,1):0,(1,0):0,(1,1):1}):
            with self.subTest(c=c), self.assertRaises(ValueError):
                t.calculate_covariance_thermal({0:1,1:1}, c)


class ThermalProjectionTests(unittest.TestCase):
    def test_pure_am_has_zero_pm(self):
        projected = t.project_am_pm({0:3}, {0:3}, 2)
        self.assertEqual(0j, projected['pm_transfer'][0])
        self.assertEqual(3, projected['am_transfer'][0])

    def test_pure_pm_has_correct_half_factor_without_reference_fitting(self):
        projected = t.project_am_pm({0:3j}, {0:-3j}, 2, validated=True, validation_evidence='analytic-fixture')
        result = t.calculate_white_thermal(projected['pm_transfer'], {0:2e-9}, carrier_peak=2,
                                          transfer_quantity='PM_SSB', pm_contract=projected['contract'])
        self.assertAlmostEqual(3.6e-17, result['total_psd_V2_Hz'], delta=1e-30)
        self.assertAlmostEqual(10*math.log10(1.8e-17), result['phase_noise_dBc_Hz'])

    def test_carrier_phase_is_applied_to_both_output_responses(self):
        phase = .61
        projected = t.project_am_pm({0:3j*cmath.exp(1j*phase)}, {0:-3j*cmath.exp(-1j*phase)}, 2*cmath.exp(1j*phase))
        self.assertAlmostEqual(0, abs(projected['pm_transfer'][0]-3), places=12)

    def test_unvalidated_raw_projection_cannot_claim_pm(self):
        p = t.project_am_pm({0:3j}, {0:-3j}, 2)
        with self.assertRaises(ValueError):
            t.calculate_white_thermal(p['pm_transfer'], {0:1}, transfer_quantity='PM_SSB', pm_contract=p['contract'])
        with self.assertRaises(ValueError):
            t.project_am_pm({0:1}, {1:1}, 2)


if __name__ == '__main__':
    unittest.main()
