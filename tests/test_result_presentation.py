"""Offline result-unit, identity, grouping and glossary coverage regressions."""

import copy
import importlib.util
import json
import math
import unittest
from pathlib import Path

from isf_tool.core.dual_tb import recompute_dual_tb
from isf_tool.core.errors import BundleImportError
from isf_tool.core.metrics import metric_catalog, metric_definition
from isf_tool.core.models import validate_json_compatible
from isf_tool.core.result_presentation import (
    enrich_harmonic, enrich_result, enrich_summary,
    cumulative_noise_series, harmonic_noise_series, operating_points, phase_noise_series,
)
from test_scalar_manual_reference import canonical_fixture, MANUAL_DBC, MANUAL_ASD, POINT


ROOT = Path(__file__).resolve().parents[1]


def sample_result(identity="design_A", offset=10000.0, scale=1.0):
    """Synthetic already-computed rows; no frontend interpolation or EDA."""
    point = identity + "@" + str(offset)
    summary = {
        "point": point, "operating_point_id": identity, "corner": "same_corner",
        "offset_Hz": offset, "carrier_peak_V": 1.0,
        "dynamic_pm_asd_V_rtHz": MANUAL_ASD * scale,
        "spectre_fn_pm_asd_V_rtHz": MANUAL_ASD,
        "pm_magnitude_projection_valid": False,
    }
    return {"summary": [summary], "points": {point: {
        "summary": [dict(summary)], "workpoint": [], "phase_candidates": [],
        "harmonics": [{"k": 0, "Qk_real_V_rtHz": 2e-6 * scale, "Qk_imag_V_rtHz": 2e-6 * scale,
                       "cum_real_V_rtHz": 2e-6 * scale, "cum_imag_V_rtHz": 2e-6 * scale},
                      {"k": 1, "Qk_real_V_rtHz": 3e-6 * scale, "Qk_imag_V_rtHz": 2e-6 * scale,
                       "cum_real_V_rtHz": 5e-6 * scale, "cum_imag_V_rtHz": 4e-6 * scale}],
    }}}


def merge_samples(*results):
    merged = {"summary": [], "points": {}}
    for result in results:
        merged["summary"].extend(copy.deepcopy(result["summary"]))
        merged["points"].update(copy.deepcopy(result["points"]))
    return merged


class CanonicalPresentationTests(unittest.TestCase):
    def test_single_and_pair_dbc_use_same_carrier_contract(self):
        fixture = canonical_fixture()
        for row in fixture["vco_results"]:
            row.update(second_device_predicted_pm_asd_V_rtHz=3e-6,
                       spectre_pair_pm_asd_V_rtHz=4e-6)
        result = recompute_dual_tb(fixture)
        summary = result["summary"][0]
        self.assertAlmostEqual(summary["spectre_fn_dBc_Hz"], MANUAL_DBC, places=11)
        self.assertAlmostEqual(summary["dynamic_dBc_Hz"], MANUAL_DBC, places=11)
        self.assertAlmostEqual(summary["predicted_pair_dBc_Hz"], -100.0, places=11)
        self.assertAlmostEqual(summary["spectre_pair_dBc_Hz"], 10 * math.log10(16e-12 / 0.5), places=11)
        self.assertEqual("target_device_flicker", summary["noise_scope"])
        validate_json_compatible(result)

    def test_harmonic_aliases_equivalent_and_cumulative_noise_match_oracle(self):
        result = recompute_dual_tb(canonical_fixture())
        rows = result["points"][POINT]["harmonics"]
        for row in rows:
            self.assertEqual(row["Q_real_V_rtHz"], row["Qk_real_V_rtHz"])
            self.assertEqual(row["Q_imag_V_rtHz"], row["Qk_imag_V_rtHz"])
            self.assertEqual("coherent_complex_sum", row["harmonic_combination"])
            self.assertEqual(POINT, row["operating_point_id"])
            self.assertEqual(10000.0, row["offset_hz"])
        self.assertAlmostEqual(rows[0]["harmonic_equivalent_dBc_Hz"], 10 * math.log10(8e-12 / 0.5), places=11)
        self.assertAlmostEqual(rows[-1]["cumulative_dBc_Hz"], MANUAL_DBC, places=11)
        self.assertAlmostEqual(rows[-1]["cumulative_error_dB"], 0.0, places=11)
        self.assertLess(rows[-1]["cumulative_dBc_Hz"], rows[-2]["cumulative_dBc_Hz"])

    def test_quality_and_effective_parameters_survive_canonical_recompute(self):
        fixture = canonical_fixture()
        fixture["metadata"]["operating_points"] = {POINT: {"params": {"SW": 1, "temp": 85}}}
        for row in fixture["vco_results"]:
            row.update(pm_magnitude_projection_valid="False", numerical_quality_pass="True",
                       hbac_secondary_validation_pass="False", hbac_max_phase_delta_deg="67.5")
        fixture["replica_results"][1]["HBAC_vs_Mdyn_phase_deg"] = -67.5
        result = recompute_dual_tb(fixture)
        summary = result["summary"][0]
        self.assertEqual({"SW": 1, "temp": 85}, summary["operating_parameters"])
        self.assertIs(summary["pm_magnitude_projection_valid"], False)
        self.assertIs(summary["numerical_quality_pass"], True)
        self.assertIs(summary["hbac_secondary_validation_pass"], False)
        harmonic = result["points"][POINT]["harmonics"][1]
        self.assertEqual(-67.5, harmonic["HBAC_vs_Mdyn_phase_deg"])
        self.assertEqual(67.5, harmonic["hbac_max_phase_delta_deg"])
        self.assertIs(harmonic["pm_magnitude_projection_valid"], False)

    def test_zero_and_missing_values_remain_unavailable(self):
        summary = enrich_summary({"point": "zero", "carrier_peak_V": 1.0,
                                  "dynamic_pm_asd_V_rtHz": 0.0, "spectre_fn_pm_asd_V_rtHz": 0.0})
        self.assertIsNone(summary["dynamic_dBc_Hz"])
        self.assertIsNone(summary["spectre_fn_dBc_Hz"])
        self.assertIsNone(summary["predicted_pair_dBc_Hz"])
        self.assertIsNone(summary["numerical_quality_pass"])
        harmonic = enrich_harmonic({"k": 0, "Q_real_V_rtHz": 0.0, "Q_imag_V_rtHz": 0.0,
                                    "cum_real_V_rtHz": 0.0, "cum_imag_V_rtHz": 0.0}, summary)
        self.assertIsNone(harmonic["harmonic_equivalent_dBc_Hz"])
        self.assertIsNone(harmonic["cumulative_dBc_Hz"])
        self.assertIsNone(harmonic["cumulative_error_dB"])
        validate_json_compatible(harmonic)

    def test_conflicting_q_aliases_are_not_silently_selected(self):
        with self.assertRaisesRegex(ValueError, "conflicting result aliases"):
            enrich_harmonic({"Q_real_V_rtHz": 1e-6, "Qk_real_V_rtHz": 2e-6}, {})


class ResultSeriesTests(unittest.TestCase):
    def test_offsets_are_numeric_sorted_and_design_points_never_cross_join(self):
        result = merge_samples(sample_result("A", 1e6, 2), sample_result("B", 1e3, 4), sample_result("A", 1e3))
        groups = operating_points(result)
        self.assertEqual(["A", "B"], [row["id"] for row in groups])
        self.assertEqual([1e3, 1e6], groups[0]["offsets_hz"])
        curves = phase_noise_series(result, "A")
        self.assertEqual([1e3, 1e6], [point[0] for point in curves[0]["points"]])
        self.assertAlmostEqual(curves[0]["points"][0][1], MANUAL_DBC, places=11)
        self.assertAlmostEqual(curves[0]["points"][1][1], MANUAL_DBC + 20 * math.log10(2), places=11)
        self.assertEqual(2, len(curves))
        self.assertTrue(all(sample["operating_point_id"] == "A" for sample in curves[0]["samples"]))
        self.assertIs(curves[0]["samples"][0]["quality"]["pm_magnitude_projection_valid"], False)
        self.assertEqual([], phase_noise_series(result, "unknown"))

    def test_harmonic_curves_keep_missing_samples_and_label_coherent_total(self):
        first, second = sample_result("A", 1e3), sample_result("A", 1e4)
        second["points"][second["summary"][0]["point"]]["harmonics"].pop()
        result = merge_samples(first, second)
        curves = harmonic_noise_series(result, "A")
        self.assertEqual(["harmonic_0", "harmonic_1", "coherent_total"], [row["id"] for row in curves])
        self.assertIsNone(curves[1]["points"][1][1])
        self.assertIn("coherent sum", curves[-1]["name"])
        self.assertIn("single device", curves[-1]["name"])
        self.assertAlmostEqual(curves[-1]["points"][0][1], MANUAL_DBC, places=11)
        validate_json_compatible(curves)

    def test_duplicate_identity_or_harmonic_rejected(self):
        source = sample_result()
        duplicate = copy.deepcopy(source)
        duplicate["summary"].append(dict(duplicate["summary"][0]))
        with self.assertRaisesRegex(ValueError, "ambiguous|duplicate"):
            phase_noise_series(duplicate)
        point = source["summary"][0]["point"]
        source["points"][point]["harmonics"].append(dict(source["points"][point]["harmonics"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate harmonic"):
            harmonic_noise_series(source)

    def test_cumulative_curves_use_stored_coherent_values_and_keep_gaps(self):
        first, second = sample_result("A", 1e3), sample_result("A", 1e4)
        second["points"][second["summary"][0]["point"]]["harmonics"].pop()
        curve = cumulative_noise_series(merge_samples(first, second), "A", 1)[0]
        self.assertEqual("cumulative_1", curve["id"])
        self.assertEqual(1, curve["k"])
        self.assertAlmostEqual(MANUAL_DBC, curve["points"][0][1], places=11)
        self.assertIsNone(curve["points"][1][1])
        for invalid in (True, -1, 0.5, "bad"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "non-negative integer"):
                cumulative_noise_series(first, "A", invalid)

    def test_legacy_scalar_uses_only_result_owned_offset(self):
        result = sample_result("legacy_100M", 1e4)
        for row in result["summary"]:
            row.pop("operating_point_id")
            row.pop("offset_Hz")
        point = next(iter(result["points"]))
        result["points"][point]["summary"] = [dict(result["summary"][0])]
        self.assertEqual([], phase_noise_series(result))
        result["metadata"] = {"offset_hz": 12345.0}
        curves = phase_noise_series(result)
        self.assertEqual([12345.0], [value[0] for value in curves[0]["points"]])
        self.assertEqual(point, operating_points(result)[0]["id"])

    def test_pair_missing_is_not_guessed_and_other_scopes_are_rejected(self):
        curves = phase_noise_series(sample_result(), scope="pair")
        self.assertTrue(all(point[1] is None for curve in curves for point in curve["points"]))
        with self.assertRaisesRegex(ValueError, "single or pair"):
            phase_noise_series(sample_result(), scope="whole_vco")

    def test_presentation_helpers_do_not_mutate_result_or_read_new_form(self):
        result = sample_result()
        saved = copy.deepcopy(result)
        enrich_result(result)
        phase_noise_series(result)
        harmonic_noise_series(result)
        cumulative_noise_series(result)
        operating_points(result)
        self.assertEqual(saved, result)


class ScalarIdentityGateTests(unittest.TestCase):
    def test_nonoverlapping_harmonics_cannot_hide_mixed_offsets(self):
        fixture = canonical_fixture()
        self.assertEqual(len(fixture["vco_results"]), len({row["k"] for row in fixture["vco_results"]}))
        fixture["vco_results"][1]["offset_Hz"] = 100000.0
        with self.assertRaisesRegex(BundleImportError, "inconsistent offset_Hz"):
            recompute_dual_tb(fixture)

    def test_replica_and_workpoint_offsets_must_match_vco(self):
        for table in ("replica_results", "workpoint"):
            with self.subTest(table=table):
                fixture = canonical_fixture()
                if table == "workpoint":
                    fixture[table] = [{"point": POINT}]
                fixture[table][0]["offset_hz"] = 100000.0
                with self.assertRaisesRegex(BundleImportError, "inconsistent offset_Hz"):
                    recompute_dual_tb(fixture)

    def test_offset_alias_conflict_and_close_but_distinct_offsets_rejected(self):
        for field, value in (("offset_hz", 100000.0), ("offset_Hz", math.nextafter(10000.0, math.inf))):
            with self.subTest(field=field, value=value):
                fixture = canonical_fixture()
                fixture["vco_results"][1][field] = value
                with self.assertRaisesRegex(BundleImportError, "inconsistent offset_Hz"):
                    recompute_dual_tb(fixture)

    def test_corner_frequency_and_operating_identity_are_not_mixed(self):
        for field, value in (("f0_Hz", 9e9), ("corner", "another_corner"), ("operating_point_id", "another_design")):
            with self.subTest(field=field):
                fixture = canonical_fixture()
                fixture["vco_results"][1][field] = value
                with self.assertRaisesRegex(BundleImportError, "inconsistent " + field):
                    recompute_dual_tb(fixture)

    def test_legacy_metadata_offset_and_absent_optional_columns_still_work(self):
        fixture = canonical_fixture()
        for row in fixture["vco_results"]:
            row.pop("offset_Hz")
        fixture["metadata"]["offset_hz"] = 10000.0
        result = recompute_dual_tb(fixture)
        self.assertAlmostEqual(MANUAL_DBC, result["summary"][0]["dynamic_dBc_Hz"], places=11)
        fixture["replica_results"][1]["offset_Hz"] = 100000.0
        with self.assertRaisesRegex(BundleImportError, "inconsistent offset_Hz"):
            recompute_dual_tb(fixture)

    def test_consistent_lowercase_alias_and_explicit_identity_supported(self):
        fixture = canonical_fixture()
        for row in fixture["vco_results"]:
            row["offset_hz"] = str(row.pop("offset_Hz"))
            row["operating_point_id"] = "selected_design"
        fixture["replica_results"][0]["offset_hz"] = "1e4"
        result = recompute_dual_tb(fixture)
        self.assertEqual("selected_design", result["summary"][0]["operating_point_id"])
        self.assertAlmostEqual(MANUAL_DBC, result["summary"][0]["dynamic_dBc_Hz"], places=11)


class MetricGlossaryTests(unittest.TestCase):
    def test_all_visible_summary_and_harmonic_fields_have_complete_help(self):
        visible = (
            "point", "operating_point_id", "operating_parameters", "corner", "f0_Hz", "period_s", "offset_Hz",
            "carrier_peak_V", "dynamic_error_dB", "pair_error_dB", "predicted_pair_asd", "reference_pair_asd",
            "dynamic_dBc_Hz", "spectre_fn_dBc_Hz", "predicted_pair_dBc_Hz", "spectre_pair_dBc_Hz",
            "mDC_cycle_mean_A_rtHz", "mDyn_rms_A_rtHz", "k", "Mdc_mag_A_rtHz", "Mdyn_mag_A_rtHz",
            "A_k", "delta_k_deg", "Ck_real", "Ck_imag", "Qk_real_V_rtHz", "Qk_imag_V_rtHz", "Q_mag_V_rtHz",
            "Q_phase_deg", "cum_real_V_rtHz", "cum_imag_V_rtHz", "harmonic_equivalent_dBc_Hz", "cumulative_dBc_Hz",
            "cumulative_error_dB", "pm_magnitude_projection_valid", "hbac_secondary_validation_pass", "numerical_quality_pass",
        )
        for field in visible:
            with self.subTest(field=field):
                definition = metric_definition(field)
                self.assertIsNotNone(definition)
                for key in ("label", "unit", "meaning", "purpose", "scope", "formula"):
                    self.assertTrue(definition[key])
        self.assertIsNone(metric_definition("not_a_metric"))

    def test_native_and_web_use_same_catalog_and_return_defensive_copies(self):
        payload = json.loads((ROOT / "isf_tool" / "ui" / "metric_catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["metrics"], metric_catalog())
        definition = metric_definition("Qk_real_V_rtHz")
        self.assertEqual("Q_real_V_rtHz", definition["key"])
        definition["aliases"].append("mutated")
        catalog = metric_catalog()
        catalog["point"]["label"] = "mutated"
        self.assertNotEqual("mutated", metric_definition("point")["label"])
        self.assertIsNone(metric_definition("mutated"))

    def test_exporter_retains_effective_operating_parameters_without_offsets(self):
        spec = importlib.util.spec_from_file_location("presentation_exporter_test", ROOT / "scripts" / "export_audited_bundle.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = {"offset_hz": 1e4, "points": [{"name": "A", "corner": "Fhigh", "operating_point": {"params": {"SW": 0}}}]}
        original = copy.deepcopy(config)
        result = module.operating_point_metadata(config, lambda point: point["name"])
        self.assertEqual({"SW": 0}, result["A"]["params"])
        self.assertEqual("Fhigh", result["A"]["corner"])
        self.assertNotIn("offset_hz", result["A"])
        result["A"]["params"]["SW"] = 1
        self.assertEqual(original, config)


if __name__ == "__main__":
    unittest.main()
