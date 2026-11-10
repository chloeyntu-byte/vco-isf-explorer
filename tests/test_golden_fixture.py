import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

from isf_tool.calculation import dBc_from_pm_asd, error_dB
from isf_tool.calculation import calculate_point

try:
    from .synthetic_fixture import ERROR_TARGETS_DB, write_closure_bundle
except ImportError:  # unittest discovery loads test modules as top-level names
    from synthetic_fixture import ERROR_TARGETS_DB, write_closure_bundle


class SyntheticFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(
            write_closure_bundle(Path(cls.temporary.name) / "six_points")
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        with (self.root / "summary.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            self.rows = list(csv.DictReader(handle))

    def test_six_points_match_gate(self):
        self.assertEqual(len(self.rows), 6)
        dynamic = [float(row["dynamic_error_dB"]) for row in self.rows]
        pair = [float(row["pair_error_dB"]) for row in self.rows]
        self.assertLessEqual(max(abs(x) for x in dynamic), 3.0)
        self.assertLessEqual(max(abs(x) for x in pair), 3.0)
        for expected, actual in zip(ERROR_TARGETS_DB, dynamic):
            self.assertAlmostEqual(expected, actual, places=12)
        for single_error, pair_error in zip(dynamic, pair):
            self.assertAlmostEqual(single_error, pair_error, places=12)

    def test_each_point_detail_matches_summary(self):
        for row in self.rows:
            detail = json.loads(
                (self.root / row["point"] / "summary.json").read_text(
                    encoding="utf-8"
                )
            )
            for key in (
                "carrier_peak_V",
                "dynamic_pm_asd_V_rtHz",
                "spectre_fn_pm_asd_V_rtHz",
                "dynamic_error_dB",
                "pair_error_dB",
            ):
                self.assertAlmostEqual(float(row[key]), float(detail[key]), places=14)

    def test_normalization_recomputes(self):
        for row in self.rows:
            predicted = float(row["dynamic_pm_asd_V_rtHz"])
            reference = float(row["spectre_fn_pm_asd_V_rtHz"])
            carrier = float(row["carrier_peak_V"])
            self.assertAlmostEqual(error_dB(predicted, reference), float(row["dynamic_error_dB"]), places=10)
            self.assertAlmostEqual(dBc_from_pm_asd(predicted, carrier), float(row["dynamic_dBc_Hz"]), places=10)

    def test_q_cumulative_reaches_physical_sum(self):
        for row in self.rows:
            with (self.root / row["point"] / "harmonic_closure.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                harmonics = list(csv.DictReader(handle))
            final = harmonics[-1]
            physical = complex(
                float(row["physical_vdyn_real_V_rtHz"]),
                float(row["physical_vdyn_imag_V_rtHz"]),
            )
            cumulative = complex(float(final["cum_real_V_rtHz"]), float(final["cum_imag_V_rtHz"]))
            self.assertLess(abs(cumulative - physical), 1e-15)

    def test_single_shared_source_normalization(self):
        for row in self.rows:
            sxi = float(row["sqrt_Sxi_A_rtHz"])
            with (self.root / row["point"] / "harmonic_closure.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                harmonics = list(csv.DictReader(handle))
            for harmonic in harmonics:
                mdyn = float(harmonic["Mdyn_mag_A_rtHz"])
                d = math.hypot(float(harmonic["Dk_real"]), float(harmonic["Dk_imag"]))
                self.assertAlmostEqual(mdyn, d * sxi, places=20)

    def test_canonical_signed_inputs_recompute_all_points(self):
        canonical = self.root / "canonical"
        with (canonical / "vco_results.csv").open(newline="", encoding="utf-8") as handle:
            vco = list(csv.DictReader(handle))
        with (canonical / "replica_results.csv").open(newline="", encoding="utf-8") as handle:
            replica = list(csv.DictReader(handle))
        by_summary = {row["point"]: row for row in self.rows}
        for point, expected in by_summary.items():
            vrows = [row for row in vco if row["point"] == point]
            rrows = [row for row in replica if row["point"] == point]
            transfer = {
                int(row["k"]): complex(float(row["H_real_ohm"]), float(row["H_imag_ohm"]))
                for row in vrows
            }
            mdc = {
                int(row["k"]): complex(float(row["Mdc_real_A_rtHz"]), float(row["Mdc_imag_A_rtHz"]))
                for row in rrows
            }
            mdyn = {
                int(row["k"]): complex(float(row["Mdyn_real_A_rtHz"]), float(row["Mdyn_imag_A_rtHz"]))
                for row in rrows
            }
            carrier = complex(float(vrows[0]["carrier_real_V"]), float(vrows[0]["carrier_imag_V"]))
            result = calculate_point(
                transfer,
                mdc,
                mdyn,
                abs(carrier),
                float(vrows[0]["offset_Hz"]),
                float(vrows[0]["spectre_pm_asd_V_rtHz"]),
                f0_hz=float(vrows[0]["f0_Hz"]),
                carrier_phasor=carrier,
            )
            self.assertAlmostEqual(
                result["methods"]["dynamic"]["pm_asd_V_rtHz"],
                float(expected["dynamic_pm_asd_V_rtHz"]),
                places=15,
            )
            self.assertAlmostEqual(
                result["methods"]["dynamic"]["error_dB"],
                float(expected["dynamic_error_dB"]),
                places=9,
            )
            self.assertAlmostEqual(result["gamma_equivalence_error_dB"], 0.0, places=9)


if __name__ == "__main__":
    unittest.main()
