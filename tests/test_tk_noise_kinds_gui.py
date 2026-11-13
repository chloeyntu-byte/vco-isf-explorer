"""Fake result-only Tk integration; no simulator, fit, or real workspace."""

import copy
import math
import os
import tkinter as tk
import unittest
from unittest import mock

from isf_tool.core.metrics import metric_definition
from isf_tool.tk.app import IsfApplication, LEGACY_DEVICE_COMPARISON_FIELDS
from test_tk_total_noise import result_fixture


def thermal_fixture():
    result = result_fixture()
    for row in result["summary"]:
        scale = (2 if row["corner"] == "ff" else 1) ** 2 * 1000 / row["offset_Hz"]
        sources = []
        for device, mechanism, psd in (("M0", "id", 0.5e-12 * scale), ("M1", "id", 0.5e-12 * scale), ("R0", "rn", 2e-12 * scale)):
            sources.append({"device": device, "mechanism": mechanism, "psd_V2_Hz": psd,
                            "input_band_contributions": [{"input_band": -1, "psd_V2_Hz": psd / 4},
                                                         {"input_band": 0, "psd_V2_Hz": psd / 2},
                                                         {"input_band": 1, "psd_V2_Hz": psd / 4}]})
        row["thermal_noise"] = {"schema_version": 1, "status": "available", "noise_quantity": "PM_SSB",
            "scope": "gm_pair_and_lc_network", "pair_model": "symmetric_independent",
            "passive_scope": "passive_only_lc_roots_v1",
            "pm_validation": {"fixture": "synthetic unit test only"}, "carrier_peak_V": 1,
            "offset_Hz": row["offset_Hz"], "f0_Hz": row["f0_Hz"], "predicted_psd_V2_Hz": 3e-12 * scale,
            "phase_noise_dBc_Hz": 999, "sources": sources}
        result["points"][row["point"]]["harmonics"] = [{"k": 0, "Q_real_V_rtHz": 1e-6,
            "Q_imag_V_rtHz": 0, "cum_real_V_rtHz": 1e-6, "cum_imag_V_rtHz": 0}]
    return result


@unittest.skipUnless(os.environ.get("ISF_GUI_TEST") == "1", "requires explicitly selected GUI display")
class NoiseKindGuiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.lower()
        with mock.patch.object(IsfApplication, "_submit"), mock.patch.object(IsfApplication, "_refresh_jobs"):
            self.app = IsfApplication(self.root, mock.Mock(workspace="/tmp/isolated-thermal-ui"))
        self.app.current_job_id = "fixture"
        self.app.current_job = {"status": "succeeded"}

    def tearDown(self):
        self.app.queue.close()
        self.root.update_idletasks()
        self.root.destroy()

    def load(self, value):
        with mock.patch.object(self.app, "_show_error") as error:
            self.app._result_loaded(value, "fixture")
            error.assert_not_called()

    def test_both_thermal_flicker_controls_match_top_table_and_raw_curve_samples(self):
        result = thermal_fixture()
        self.load(result)
        self.assertEqual("both", self.app.noise_kind_var.get())
        for identity in ("tt", "ff"):
            self.app.operating_point_var.set(identity)
            self.app._operating_point_changed()
            for offset in (1000, 10000):
                self.app.result_offset_var.set(f"{offset} Hz")
                self.app._result_offset_changed()
                selected = next(row for row in result["summary"] if row["point"] == f"{identity}@{offset}")
                for kind in ("both", "thermal", "flicker"):
                    self.app.noise_kind_var.set(kind)
                    self.app._point_changed()
                    power = (selected["predicted_pair_pm_asd_V_rtHz"] ** 2 if kind != "thermal" else 0)
                    power += selected["thermal_noise"]["predicted_psd_V2_Hz"] if kind != "flicker" else 0
                    expected = 10 * math.log10(2 * power)
                    self.assertAlmostEqual(expected, self.app.current_result_view["metrics"]["calculated_dbc"], places=10)
                    row = next(row for row in self.app.current_result_view["summary_rows"] if row["point"] == selected["point"])
                    self.assertAlmostEqual(expected, row["calculated_dbc"], places=10)
                    curve = next(item for item in self.app.phase_noise_chart._series if item["id"] == ("thermal" if kind == "thermal" else "calculated"))
                    sample = next(item for item in curve["samples"] if item["point"] == selected["point"])
                    self.assertAlmostEqual(expected, sample["value"], places=10)
                    self.assertIn(f"{expected:.7g}", self.app.metrics_var.get())
                    if kind == "thermal":
                        self.assertFalse(any(item["id"].startswith("harmonic_") for item in self.app.harmonic_noise_chart._series))
                        self.assertFalse(any("Cumulative" in value for value in self.app.harmonic_curve_box["values"]))
        self.assertEqual(1e-6, result["points"]["tt@1000"]["harmonics"][0]["Q_real_V_rtHz"])

    def test_legacy_results_do_not_fill_missing_thermal_or_both_from_flicker(self):
        self.load(result_fixture())
        self.assertEqual("N/A", self.app.current_result_view["metrics"]["calculated_dbc"])
        for item in self.app.phase_noise_chart._series:
            if item["id"] in {"thermal", "calculated"}:
                self.assertTrue(all(point[1] is None for point in item["points"]))
        self.app.noise_kind_var.set("flicker")
        self.app._point_changed()
        self.assertIsInstance(self.app.current_result_view["metrics"]["calculated_dbc"], float)

    def test_pair_only_metric_views_preserve_raw_coefficients_and_export_evidence(self):
        result = thermal_fixture()
        for summary in result["summary"]:
            summary.update({"dynamic_error_dB": 0.25, "dc_dBc_Hz": -130,
                            "second_device_predicted_pm_asd_V_rtHz": 1e-6})
        for point in result["points"].values():
            point["harmonics"][0].update({"Mdc_mag_A_rtHz": 3e-8, "A_k": 1.2,
                "harmonic_equivalent_dBc_Hz": -123, "cumulative_dBc_Hz": -123,
                "cumulative_error_dB": 0.25})
        before = copy.deepcopy(result)
        self.load(result)
        visible = set(self.app._metric_row_fields.values())
        self.assertFalse(visible & LEGACY_DEVICE_COMPARISON_FIELDS)
        self.assertTrue({"Mdc_mag_A_rtHz", "A_k", "Q_real_V_rtHz", "cum_real_V_rtHz"} <= visible)
        self.assertFalse(set(self.app._metric_choices.values()) & LEGACY_DEVICE_COMPARISON_FIELDS)
        self.assertNotIn("err", self.app.harmonic_tree["columns"])
        self.assertEqual(("pair_harmonic_equivalent_dBc_Hz", "pair_cumulative_dBc_Hz"),
                         self.app.harmonic_tree._metric_fields[-2:])
        for field in self.app.harmonic_tree._metric_fields[-2:]:
            self.assertIn("gm pair", metric_definition(field)["scope"])
        self.assertEqual(before, result)

    def test_thermal_quality_is_explicitly_three_state_in_actual_view(self):
        for value, label in ((None, "Not provided"), (False, "Failed"), (True, "Passed"), ("false", "Not provided")):
            with self.subTest(value=value):
                result = thermal_fixture()
                for row in result["summary"]:
                    row["thermal_noise"]["numerical_quality"] = {"pass": value}
                self.load(result)
                self.assertIn("Thermal Simulation numerical quality: " + label, self.app.result_quality_var.get())


if __name__ == "__main__":
    unittest.main()
