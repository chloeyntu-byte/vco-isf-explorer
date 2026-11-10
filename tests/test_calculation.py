import cmath
import json
import math
import unittest

from isf_tool import calculation as c
from isf_tool.core.models import validate_json_compatible


class CalculationTests(unittest.TestCase):
    def test_asymmetric_devices_combine_in_power(self):
        result = c.combine_independent_device_noise([3.0e-6, 4.0e-6j], 1.0)
        self.assertAlmostEqual(5.0e-6, result["total_pm_asd_V_rtHz"], places=18)
        self.assertAlmostEqual(25.0e-12, result["total_pm_psd_V2_Hz"], places=24)
        self.assertEqual("independent_power_sum", result["combination"])

    def test_fourier_round_trip(self):
        expected = 0.3 - 0.2j
        wt = [2 * math.pi * i / 1000 for i in range(1001)]
        values = [1.2 + 2 * (expected * cmath.exp(1j * x)).real for x in wt]
        result = c.fourier_coefficients(wt, values, 1)
        self.assertAlmostEqual(result[0].real, 1.2, places=8)
        self.assertAlmostEqual(result[1].real, expected.real, places=8)
        self.assertAlmostEqual(result[1].imag, expected.imag, places=8)

    def test_coherent_cancellation(self):
        h = {-1: 1 + 0j, 0: -1 + 0j}
        m = {-1: 1 + 0j, 0: 1 + 0j}
        self.assertAlmostEqual(abs(c.coherent_sum(h, m)), 0.0, places=12)

    def test_exact_gamma_matches_physical_units(self):
        h = {-1: 0.3 + 0.2j, 0: -0.1 + 0.4j, 1: 0.7 - 0.5j}
        mdc = {0: 0.8 + 0j, 1: 0.1 - 0.1j}
        mdyn = {0: 0.8 + 0j, 1: 0.2 - 0.3j}
        carrier = 1.2 + 0.9j
        result = c.calculate_point(
            h, mdc, mdyn, abs(carrier), 1e4, carrier_phasor=carrier
        )
        self.assertAlmostEqual(
            result["heff_dynamic_exact"],
            result["heff_from_physical_units"],
            places=8,
        )
        self.assertAlmostEqual(result["gamma_equivalence_error_dB"], 0.0, places=8)
        self.assertEqual(
            "carrier_referenced_signed_H_iq_v1",
            result["gamma_reference"],
        )
        scale = math.sqrt(2.0) * 2.0 * math.pi * 1e4 / abs(carrier)
        rotated = carrier.conjugate() / abs(carrier) * complex(
            result["physical_vdyn"]["real"], result["physical_vdyn"]["imag"]
        )
        self.assertAlmostEqual(result["heff_carrier_i_signed"], scale * rotated.real, places=8)
        self.assertAlmostEqual(result["heff_carrier_q_signed"], scale * rotated.imag, places=8)

    def test_gamma_is_independent_of_noise_modulation(self):
        h = {-1: 0.3 + 0.2j, 0: -0.1 + 0.4j, 1: 0.7 - 0.5j}
        mdc = {0: 0.8 + 0j, 1: 0.1 - 0.1j}
        carrier = 1.2 + 0.9j
        first = c.calculate_point(
            h,
            mdc,
            {0: 0.8 + 0j, 1: 0.2 - 0.3j},
            abs(carrier),
            1e4,
            carrier_phasor=carrier,
        )
        second = c.calculate_point(
            h,
            mdc,
            {0: 0.2 + 0j, 1: -0.5 + 0.1j},
            abs(carrier),
            1e4,
            carrier_phasor=carrier,
        )
        self.assertEqual(first["curves"]["gamma_i"], second["curves"]["gamma_i"])
        self.assertEqual(first["curves"]["gamma_q"], second["curves"]["gamma_q"])

    def test_carrier_iq_gamma_is_invariant_to_common_phase_rotation(self):
        h = {-1: 0.3 + 0.2j, 0: -0.1 + 0.4j, 1: 0.7 - 0.5j}
        carrier = 1.2 + 0.9j
        phase = cmath.exp(0.73j)
        first = c.carrier_referenced_gamma_branches(h, carrier, 1e4, 1)
        second = c.carrier_referenced_gamma_branches(
            {k: value * phase for k, value in h.items()},
            carrier * phase,
            1e4,
            1,
        )
        for branch in ("i", "q"):
            for harmonic in first[branch]:
                self.assertAlmostEqual(first[branch][harmonic].real, second[branch][harmonic].real, places=10)
                self.assertAlmostEqual(first[branch][harmonic].imag, second[branch][harmonic].imag, places=10)

    def test_pure_imaginary_output_has_positive_q_branch(self):
        result = c.calculate_point(
            {0: 1j}, {0: 1.0}, {0: 1.0}, 1.0, 1e4,
            carrier_phasor=1.0 + 0j,
        )
        self.assertAlmostEqual(result["heff_carrier_i_signed"], 0.0, places=10)
        self.assertGreater(result["heff_carrier_q_signed"], 0.0)
        self.assertAlmostEqual(
            result["heff_branch_rss"], result["heff_from_physical_units"], places=10
        )
        self.assertIsNone(result["gamma_i_algebra_error_dB"])
        validate_json_compatible(result, "pure-Q calculation result")
        json.dumps(result, allow_nan=False)

    def test_zero_db_ratios_are_null_with_explicit_status(self):
        self.assertIsNone(c.dBc_from_pm_asd(0.0, 1.0))
        self.assertIsNone(c.dBc_from_pm_asd(float("nan"), 1.0))
        self.assertIsNone(c.error_dB(0.0, 0.0))
        combined = c.combine_independent_device_noise([0.0], 1.0)
        self.assertIsNone(combined["dBc_Hz"])
        self.assertEqual("not_applicable", combined["dBc_Hz_status"])
        validate_json_compatible(combined, "combined zero noise")

    def test_zero_point_is_strict_json_compatible(self):
        result = c.calculate_point(
            {0: 0j},
            {0: 0.0},
            {0: 0.0},
            1.0,
            1e4,
            reference_pm_asd=0.0,
            carrier_phasor=1.0 + 0j,
        )
        for method in result["methods"].values():
            self.assertIsNone(method["dBc_Hz"])
            self.assertEqual("not_applicable", method["dBc_Hz_status"])
            self.assertIsNone(method["error_dB"])
            self.assertEqual("not_applicable", method["error_dB_status"])
        self.assertIsNone(result["gamma_equivalence_error_dB"])
        validate_json_compatible(result, "zero calculation result")
        json.dumps(result, allow_nan=False)

    def test_complex_dc_coefficient_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "k=0"):
            c.signed_real_coefficients({0: 1.0 + 1.0e-3j})

    def test_pm_dbc_matches_heff_normalization(self):
        pm_asd = 2.3e-6
        carrier = 0.8
        offset = 1e4
        heff = math.sqrt(2.0) * 2.0 * math.pi * offset * pm_asd / carrier
        self.assertAlmostEqual(
            c.dBc_from_pm_asd(pm_asd, carrier),
            20.0 * math.log10(heff / (2.0 * math.pi * offset)),
            places=12,
        )

    def test_signed_real_coefficients(self):
        result = c.signed_real_coefficients({0: 1 + 0j, 1: 2 + 3j})
        self.assertEqual(result[-1], 2 - 3j)


if __name__ == "__main__":
    unittest.main()
