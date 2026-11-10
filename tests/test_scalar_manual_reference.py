"""Independent pencil-and-paper oracle for one corner and one offset.

This fixture deliberately does not obtain expected answers from another ISF
implementation.  The signed H coefficients are in kohm, M coefficients in
nA/sqrt(Hz), so their products below are in uV/sqrt(Hz):

    k=0: (1+j)*2                                      =  2+2j
    k=1: (2-j)*(1+2j) + (-1+2j)*(1-2j)               =  7+7j
    k=2: (1+j)*(2-j) + (-4-j)*(2+j)                  = -4-5j
    sum:                                                5+4j

Thus the coherent output PSD is 41e-12 V^2/Hz, NOT the sum of
the three per-harmonic powers (147e-12 V^2/Hz).  A differential
carrier of 0.6+0.8j V has a 1 V peak and 0.5 V^2 mean square.
The existing PM-SSB / differential-peak contract therefore gives
L = 10*log10(41e-12 / 0.5) dBc/Hz.

These are synthetic algebra/normalization checks, not a validation of the
physical PM projection or the engineering AM/PM phase-selection method.
The PSD test covers the canonical ASD boundary; it does not run Spectre.
"""

import copy
import math
import unittest

from isf_tool import calculation as calculation
from isf_tool.core.dual_tb import recompute_dual_tb
from isf_tool.core.errors import BundleImportError


TRANSFER_OHM = {
    -2: -4000.0 - 1000.0j,
    -1: -1000.0 + 2000.0j,
    0: 1000.0 + 1000.0j,
    1: 2000.0 - 1000.0j,
    2: 1000.0 + 1000.0j,
}
M_DYNAMIC = {0: 2.0e-9 + 0j, 1: 1.0e-9 + 2.0e-9j, 2: 2.0e-9 - 1.0e-9j}
M_DC = {0: 2.0e-9 + 0j, 1: 0.5e-9 + 1.0e-9j, 2: 1.0e-9 - 0.5e-9j}
CARRIER = 0.6 + 0.8j
OFFSET_HZ = 10000.0
F0_HZ = 5.0e9
POINT = "Fhigh_SW0_manual_reference"

# Explicit literals, not values calculated using the implementation under test.
MANUAL_Q = (2.0e-6 + 2.0e-6j, 7.0e-6 + 7.0e-6j, -4.0e-6 - 5.0e-6j)
MANUAL_CUMULATIVE = (2.0e-6 + 2.0e-6j, 9.0e-6 + 9.0e-6j, 5.0e-6 + 4.0e-6j)
MANUAL_ASD = math.sqrt(41.0) * 1.0e-6
MANUAL_DBC = 10.0 * math.log10(41.0e-12 / 0.5)


def scalar_result(transfer=None, mdc=None, dynamic=None):
    return calculation.calculate_point(
        TRANSFER_OHM if transfer is None else transfer,
        M_DC if mdc is None else mdc,
        M_DYNAMIC if dynamic is None else dynamic,
        1.0,
        OFFSET_HZ,
        reference_pm_asd=MANUAL_ASD,
        f0_hz=F0_HZ,
        carrier_phasor=CARRIER,
    )


def canonical_fixture():
    """Construct one design point with an explicitly normalized shared source."""
    vco = []
    for harmonic, transfer in TRANSFER_OHM.items():
        vco.append({
            "point": POINT,
            "corner": "Fhigh",
            "k": harmonic,
            "f0_Hz": F0_HZ,
            "offset_Hz": OFFSET_HZ,
            "carrier_real_V": 0.6,
            "carrier_imag_V": 0.8,
            "H_real_ohm": transfer.real,
            "H_imag_ohm": transfer.imag,
            "spectre_pm_asd_V_rtHz": MANUAL_ASD,
        })

    # A source PSD of 4e-18 A^2/Hz becomes 2e-9 A/sqrt(Hz) once.
    # Mdyn = Dk * sqrt(Sxi); the canonical boundary already contains ASD.
    normalized_d = {0: 1.0 + 0j, 1: 0.5 + 1.0j, 2: 1.0 - 0.5j}
    replica = []
    for harmonic in (0, 1, 2):
        dc = M_DC[harmonic]
        dynamic = M_DYNAMIC[harmonic]
        dk = normalized_d[harmonic]
        replica.append({
            "point": POINT,
            "k": harmonic,
            "Id_real_A": 1.0e-3 if harmonic == 0 else 1.0e-4,
            "Id_imag_A": 0.0,
            "Mdc_real_A_rtHz": dc.real,
            "Mdc_imag_A_rtHz": dc.imag,
            "Mdyn_real_A_rtHz": dynamic.real,
            "Mdyn_imag_A_rtHz": dynamic.imag,
            "sqrt_Sxi_A_rtHz": 2.0e-9,
            "Dk_real": dk.real,
            "Dk_imag": dk.imag,
        })
    return {
        "vco_results": vco,
        "replica_results": replica,
        "workpoint": [],
        "metadata": {
            "format": "canonical-two-tb-v2",
            "signoff": False,
            "canonical_noise_quantity": "ASD_A_per_sqrt_Hz",
            "source_noise_quantity": "PSD_A2_per_Hz",
            "psd_square_roots": 1,
            "carrier_amplitude": "differential_peak",
            "output_noise": "PM_SSB",
        },
        "options": {"signoff": False},
    }


class ScalarManualReferenceTests(unittest.TestCase):
    def assertComplexClose(self, actual, expected):
        self.assertTrue(math.isclose(actual.real, expected.real, rel_tol=1e-12, abs_tol=1e-20), (actual, expected))
        self.assertTrue(math.isclose(actual.imag, expected.imag, rel_tol=1e-12, abs_tol=1e-20), (actual, expected))

    def test_signed_harmonics_match_written_oracle_with_cancellation(self):
        rows = scalar_result()["harmonic_contributions"]
        self.assertEqual([0, 1, 2], [row["k"] for row in rows])
        for row, contribution, cumulative in zip(rows, MANUAL_Q, MANUAL_CUMULATIVE):
            self.assertComplexClose(complex(row["Q_real"], row["Q_imag"]), contribution)
            self.assertComplexClose(complex(row["cum_real"], row["cum_imag"]), cumulative)
        # The last harmonic reduces the noise, which a stacked-power sum loses.
        self.assertLess(rows[2]["cum_mag"], rows[1]["cum_mag"])
        self.assertFalse(math.isclose(rows[2]["cum_mag"] ** 2, 147.0e-12, rel_tol=1e-6, abs_tol=0.0))

    def test_single_corner_offset_asd_dbc_and_error_match_hand_calculation(self):
        result = scalar_result()
        self.assertComplexClose(complex(**result["physical_vdyn"]), 5.0e-6 + 4.0e-6j)
        dynamic = result["methods"]["dynamic"]
        self.assertTrue(math.isclose(dynamic["pm_asd_V_rtHz"], MANUAL_ASD, rel_tol=1e-12))
        self.assertAlmostEqual(dynamic["dBc_Hz"], MANUAL_DBC, places=11)
        self.assertAlmostEqual(dynamic["error_dB"], 0.0, places=11)
        # Static harmonics are half as large except k=0: 3.5+3j uV/sqrt(Hz).
        self.assertTrue(math.isclose(result["methods"]["dc"]["pm_asd_V_rtHz"], math.sqrt(21.25) * 1e-6, rel_tol=1e-12))
        self.assertEqual(0.0, result["uniform_delay_s"])

    def test_peak_carrier_and_ssb_psd_convention_has_no_extra_three_db(self):
        # sqrt(2) V peak -> 1 V^2 mean-square carrier.  A 1e-12 V^2/Hz
        # PM-SSB noise density has an exactly -120 dBc/Hz power ratio.
        self.assertAlmostEqual(calculation.dBc_from_pm_asd(1e-6, math.sqrt(2.0)), -120.0, places=11)
        # Halving the peak voltage quarters carrier power, raising L by 6.02 dB.
        self.assertAlmostEqual(calculation.dBc_from_pm_asd(1e-6, math.sqrt(2.0) / 2.0), -120.0 + 10.0 * math.log10(4.0), places=11)

    def test_carrier_referenced_iq_matches_independent_complex_projection(self):
        # (0.6-0.8j)*(5+4j) = 6.2-1.6j, in uV/sqrt(Hz).
        result = scalar_result()
        scale = math.sqrt(2.0) * 2.0 * math.pi * OFFSET_HZ
        self.assertAlmostEqual(result["heff_carrier_i_signed"], scale * 6.2e-6, places=12)
        self.assertAlmostEqual(result["heff_carrier_q_signed"], scale * -1.6e-6, places=12)
        self.assertAlmostEqual(result["heff_branch_rss"], scale * MANUAL_ASD, places=12)

    def test_canonical_asd_is_not_square_rooted_again(self):
        result = recompute_dual_tb(canonical_fixture())
        self.assertEqual([POINT], list(result["points"]))
        self.assertEqual(1, len(result["summary"]))
        summary = result["summary"][0]
        self.assertEqual((POINT, "Fhigh", OFFSET_HZ), (summary["point"], summary["corner"], summary["offset_Hz"]))
        self.assertEqual(2e-9, summary["sqrt_Sxi_A_rtHz"])
        self.assertTrue(math.isclose(summary["dynamic_pm_asd_V_rtHz"], MANUAL_ASD, rel_tol=1e-12))
        self.assertAlmostEqual(summary["dynamic_dBc_Hz"], MANUAL_DBC, places=11)
        self.assertAlmostEqual(summary["dynamic_error_dB"], 0.0, places=11)
        # A single device does not silently become a symmetric pair.
        self.assertIsNone(summary["predicted_pair_pm_asd_V_rtHz"])

    def test_psd_cannot_masquerade_as_shared_source_asd(self):
        fixture = canonical_fixture()
        for row in fixture["replica_results"]:
            row["sqrt_Sxi_A_rtHz"] = 4e-18
        with self.assertRaisesRegex(BundleImportError, "shared Dk.*normalization"):
            recompute_dual_tb(fixture)

    def test_scalar_result_and_identity_do_not_depend_on_input_order(self):
        self.assertEqual(
            scalar_result(),
            scalar_result(
                dict(reversed(list(TRANSFER_OHM.items()))),
                dict(reversed(list(M_DC.items()))),
                dict(reversed(list(M_DYNAMIC.items()))),
            ),
        )
        fixture = canonical_fixture()
        reordered = copy.deepcopy(fixture)
        reordered["vco_results"].reverse()
        reordered["replica_results"].reverse()
        self.assertEqual(recompute_dual_tb(fixture), recompute_dual_tb(reordered))

    def test_independent_devices_do_not_use_coherent_harmonic_combination(self):
        # Equal but opposite phasors from different devices cannot cancel.
        combined = calculation.combine_independent_device_noise(
            [5.0e-6 + 4.0e-6j, -5.0e-6 - 4.0e-6j], carrier_peak=1.0
        )
        self.assertEqual("independent_power_sum", combined["combination"])
        self.assertTrue(math.isclose(combined["total_pm_psd_V2_Hz"], 82e-12, rel_tol=1e-12))
        self.assertAlmostEqual(combined["dBc_Hz"], MANUAL_DBC + 10.0 * math.log10(2.0), places=11)


if __name__ == "__main__":
    unittest.main()
