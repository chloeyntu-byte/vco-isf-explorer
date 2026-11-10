"""Total VCO reference and device-only comparisons must never be conflated."""

import copy
import unittest

from isf_tool.tk.viewmodel import build_result_view, result_quality_text


def result_fixture():
    result = {"run_kind": "replay", "summary": [], "points": {}}
    for corner, scale in (("tt", 1), ("ff", 2)):
        for hz in (1000, 10000):
            point = f"{corner}@{hz}"
            row = {
                "point": point, "operating_point_id": corner, "corner": corner,
                "offset_Hz": hz, "f0_Hz": 5e9, "carrier_peak_V": 1,
                "dynamic_pm_asd_V_rtHz": scale * 1e-6,
                "predicted_pair_pm_asd_V_rtHz": scale * 2e-6,
                "spectre_fn_pm_asd_V_rtHz": scale * 1.1e-6,
                "spectre_pair_pm_asd_V_rtHz": scale * 2.1e-6,
                "spectre_total_pm_asd_V_rtHz": scale * 8e-6,
                "spectre_noise_reference": {"scope": "whole_vco", "schema_version": 1},
                "spectre_noise_contributions": [
                    {"device": "M0", "mechanism": "fn", "psd_V2_Hz": scale ** 2 * 1.21e-12},
                    {"device": "R0", "mechanism": "rn", "psd_V2_Hz": scale ** 2 * 2e-12},
                ],
                "numerical_quality_pass": False,
                "numerical_quality_issues": "main_vco:MODEL_RESULTS_DECLARED_INCORRECT",
                "hbac_secondary_validation_pass": False,
                "hbac_max_phase_delta_deg": 150, "hbac_phase_warn_threshold_deg": 45,
                "pm_magnitude_projection_valid": True,
            }
            result["summary"].append(row)
            result["points"][point] = {"harmonics": [], "workpoint": []}
    return result


class TotalNoiseViewTests(unittest.TestCase):
    def test_scope_changes_calculation_never_total_reference(self):
        source = result_fixture()
        original = copy.deepcopy(source)
        single = build_result_view(source, point="ff@10000")
        pair = build_result_view(source, point="ff@10000", noise_scope="pair")
        self.assertEqual(original, source)
        self.assertEqual("ff@10000", pair["point"])
        for first, second in zip(single["summary_rows"], pair["summary_rows"]):
            self.assertNotEqual(first["calculated_dbc"], second["calculated_dbc"])
            self.assertEqual(first["spectre_dbc"], second["spectre_dbc"])
        self.assertEqual(3, len(pair["noise_contribution_rows"]))
        self.assertEqual("M0", pair["noise_contribution_rows"][2]["device"])
        self.assertEqual("fn", pair["noise_contribution_rows"][2]["mechanism"])
        self.assertFalse(any("单器件" in row["label"] for row in pair["noise_contribution_rows"]))
        self.assertIsNone(pair["noise_contribution_rows"][0]["calculated_dbc"])

    def test_thermal_filter_does_not_display_fn_as_thermal(self):
        view = build_result_view(result_fixture(), noise_scope="pair", noise_kind="thermal")
        self.assertFalse(any(row["mechanism"] == "fn" for row in view["noise_contribution_rows"]))
        self.assertTrue(any(row["device"] == "R0" and row["mechanism"] == "rn" for row in view["noise_contribution_rows"]))
        self.assertEqual("N/A", view["metrics"]["calculated_dbc"])

    def test_old_result_without_total_never_falls_back_to_device(self):
        source = result_fixture()
        for row in source["summary"]:
            row.pop("spectre_noise_reference")
            row.pop("spectre_noise_contributions")
            # Historical raw summaries used this total spelling for ONE device.
            row["spectre_total_dBc_Hz"] = -123
        view = build_result_view(source)
        self.assertEqual("N/A", view["summary_rows"][0]["spectre_dbc"])
        self.assertIn("旧结果", view["reference_note"])
        self.assertIn("未采集", view["contribution_note"])
        self.assertIsNone(view["noise_contribution_rows"][0]["simulated_dbc"])
        self.assertIsInstance(view["noise_contribution_rows"][1]["simulated_dbc"], float)

    def test_quality_cannot_become_pass_from_small_curve_error(self):
        row = result_fixture()["summary"][0]
        row["dynamic_error_dB"] = 0
        note = result_quality_text(row)
        self.assertIn("运行完成不代表验证通过", note)
        self.assertIn("数值质量：未通过", note)
        self.assertIn("150°", note)
        self.assertIn("45°", note)
        self.assertIn("MODEL_RESULTS_DECLARED_INCORRECT", note)
        self.assertIn("未提供", result_quality_text({}))
        self.assertIn("曲线接近不能", result_quality_text({"numerical_quality_pass": True}))

    def test_invalid_scope_is_not_silently_mislabeled(self):
        with self.assertRaises(ValueError):
            build_result_view(result_fixture(), noise_scope="whole_vco")


if __name__ == "__main__":
    unittest.main()
