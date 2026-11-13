"""Total VCO reference and source contributions remain distinct from ISF."""
import copy
import math
import unittest

from isf_tool.core.result_presentation import enrich_summary, noise_contribution_rows, phase_noise_comparison_series, phase_noise_series
from test_result_presentation import sample_result


def comparison_sample():
    sources = [dict(device=device, mechanism=mechanism, psd_V2_Hz=power,
                    input_band_contributions=[dict(input_band=0, psd_V2_Hz=power)])
               for device, mechanism, power in (("Xpair.left", "id", 2e-12),
                                                 ("Xpair.right", "id", 2e-12),
                                                 ("Rtank", "rn", 1e-12))]
    return dict(carrier_peak_V=1, offset_Hz=1e4, f0_Hz=5e9,
                dynamic_pm_asd_V_rtHz=1e-6, predicted_pair_pm_asd_V_rtHz=math.sqrt(2) * 1e-6,
                pair_model="symmetric_independent", spectre_total_pm_asd_V_rtHz=1e-5,
                spectre_noise_reference=dict(scope="whole_vco", schema_version=1),
                spectre_noise_contributions=[
                    dict(device="Xpair.right", mechanism="id", psd_V2_Hz=4e-12),
                    dict(device="Rtank", mechanism="rn", psd_V2_Hz=8e-12),
                    dict(device="Xpair.left", mechanism="id", psd_V2_Hz=3e-12),
                    dict(device="unmodeled", mechanism="rn", psd_V2_Hz=9e-12),
                    dict(device="Xpair.left", mechanism="fn", psd_V2_Hz=1e-12),
                    dict(device="Xpair.right", mechanism="fn", psd_V2_Hz=2e-12),
                    dict(device="M0", mechanism="fn", psd_V2_Hz=3e-12),
                    dict(device="M1", mechanism="fn", psd_V2_Hz=4e-12)],
                thermal_noise=dict(schema_version=1, status="available", scope="gm_pair_and_lc_network",
                    passive_scope="passive_only_lc_roots_v1",
                    noise_quantity="PM_SSB", pm_validation="independent_test_fixture",
                    pair_model="symmetric_independent", carrier_peak_V=1, offset_Hz=1e4, f0_Hz=5e9,
                    predicted_psd_V2_Hz=5e-12, sources=sources,
                    target_noise_trace="Xpair.left", peer_noise_trace="Xpair.right"))


class TotalNoisePresentationTests(unittest.TestCase):
    def test_total_is_independent_of_selected_calculation_scope(self):
        result = sample_result()
        summary = result['summary'][0]
        summary.update(spectre_total_pm_asd_V_rtHz=2e-5,
                       spectre_noise_reference=dict(scope='whole_vco', schema_version=1),
                       predicted_pair_pm_asd_V_rtHz=1e-5,
                       spectre_pair_pm_asd_V_rtHz=1.1e-5)
        result['points'][summary['point']]['summary'] = [dict(summary)]
        a, b = phase_noise_series(result), phase_noise_series(result, scope='pair')
        self.assertEqual(a[1]['points'], b[1]['points'])
        self.assertNotEqual(a[0]['points'], b[0]['points'])
        self.assertAlmostEqual(a[1]['points'][0][1], 10 * math.log10(8e-10))
        self.assertIn('simulated total', a[1]['name'])

    def test_generated_flicker_compares_same_device_while_preserving_pair_estimate(self):
        result = sample_result()
        result["dual_tb"] = {"metadata": {"replica_source": "generated", "target_noise_trace": "M0"}}
        row = result["summary"][0]
        row.update(dynamic_pm_asd_V_rtHz=1e-6, spectre_fn_pm_asd_V_rtHz=1.2e-6,
                   predicted_pair_pm_asd_V_rtHz=math.sqrt(2) * 1e-6, spectre_pair_pm_asd_V_rtHz=1e-4)
        comparison = phase_noise_comparison_series(result)
        self.assertAlmostEqual(comparison["error"]["points"][0][1], 20 * math.log10(1 / 1.2))
        self.assertEqual("dB", comparison["error"]["unit"])
        single = comparison["prediction"]["points"][0][1]
        pair = phase_noise_series(result, scope="pair")[0]["points"][0][1]
        self.assertAlmostEqual(pair - single, 10 * math.log10(2))
        row.pop("spectre_fn_pm_asd_V_rtHz")
        self.assertIsNone(phase_noise_comparison_series(result)["error"]["points"][0][1])

    def test_thermal_and_combined_error_require_complete_modeled_reference(self):
        row = dict(comparison_sample(), point="tt", operating_point_id="tt",
                   spectre_pair_pm_asd_V_rtHz=math.sqrt(3e-12))
        result = {"summary": [row], "points": {}}
        thermal = phase_noise_comparison_series(result, noise_kind="thermal")
        self.assertAlmostEqual(thermal["error"]["points"][0][1], 10 * math.log10(5 / 15))
        combined = phase_noise_comparison_series(result, noise_kind="both")
        self.assertAlmostEqual(combined["error"]["points"][0][1], 10 * math.log10(7 / 18))
        row["spectre_noise_contributions"].pop(0)
        for kind in ("thermal", "both"):
            self.assertIsNone(phase_noise_comparison_series(result, noise_kind=kind)["error"]["points"][0][1])

    def test_legacy_reference_does_not_fill_total(self):
        source = sample_result()
        self.assertIsNone(phase_noise_series(source)[1]['points'][0][1])
        rows = noise_contribution_rows(source['summary'][0])
        self.assertIsNone(rows[0]['simulated_dbc'])
        self.assertIsNotNone(rows[1]['simulated_dbc'])
        self.assertIsNone(rows[1]['fraction_pct'])

    def test_contributions_keep_psd_units_and_aggregate_overlap_explicit(self):
        summary = dict(carrier_peak_V=1, spectre_total_pm_asd_V_rtHz=2e-6,
                       spectre_noise_reference=dict(scope='whole_vco', schema_version=1),
                       spectre_fn_pm_asd_V_rtHz=1e-6,
                       dynamic_pm_asd_V_rtHz=1.2e-6,
                       spectre_noise_contributions=[
                           dict(device='M1', mechanism='fn', psd_V2_Hz=1e-12),
                           dict(device='M1', mechanism='id', psd_V2_Hz=3e-12)])
        before = copy.deepcopy(summary)
        rows = noise_contribution_rows(summary)
        self.assertEqual(summary, before)
        self.assertEqual([r['kind'] for r in rows], ['aggregate'] * 3 + ['source'] * 2)
        self.assertEqual([r['fraction_pct'] for r in rows[3:]], [75, 25])
        self.assertAlmostEqual(rows[-1]['simulated_dbc'], 10 * math.log10(2e-12))
        self.assertIsNotNone(rows[1]['calculated_dbc'])
        self.assertIsNone(rows[-1]['calculated_dbc'])

    def test_invalid_or_zero_sources_do_not_invent_finite_dbc(self):
        rows = noise_contribution_rows(dict(carrier_peak_V=1, spectre_noise_contributions=[
            dict(device='zero', mechanism='fn', psd_V2_Hz=0),
            dict(device='invalid', mechanism='fn', psd_V2_Hz=-1)],
            spectre_noise_reference=dict(scope='whole_vco', schema_version=1)))
        self.assertEqual(len(rows), 4)
        self.assertIsNone(rows[-1]['simulated_dbc'])
        self.assertIsNone(enrich_summary({})['spectre_total_dBc_Hz'])

    def test_historical_device_total_field_is_not_whole_vco(self):
        old = dict(carrier_peak_V=1, spectre_total_pm_asd_V_rtHz=2e-6, spectre_total_dBc_Hz=-90)
        clean = enrich_summary(old)
        self.assertIsNone(clean['spectre_total_pm_asd_V_rtHz'])
        self.assertIsNone(clean['spectre_total_dBc_Hz'])

    def test_thermal_comparison_requires_exact_complete_sources_not_order(self):
        summary = comparison_sample()
        before = copy.deepcopy(summary)
        rows = noise_contribution_rows(summary, scope="pair")
        aggregate = next(row for row in rows if row["mechanism"] == "thermal")
        self.assertTrue(aggregate["reference_complete"])
        self.assertEqual(aggregate["reference_issues"], [])
        self.assertAlmostEqual(aggregate["psd_V2_Hz"], 15e-12, delta=1e-25)
        self.assertAlmostEqual(aggregate["simulated_dbc"], 10 * math.log10(30e-12))
        self.assertAlmostEqual(aggregate["calculated_dbc"], 10 * math.log10(10e-12))
        self.assertAlmostEqual(aggregate["fraction_pct"], 15)
        summary["spectre_noise_contributions"].reverse()
        self.assertEqual(rows, noise_contribution_rows(summary, scope="pair"))
        summary["spectre_noise_contributions"].reverse()
        self.assertEqual(summary, before)

    def test_partial_missing_or_inexact_reference_is_na_not_subset_sum(self):
        for replacement in (None, dict(device="Xpair.right", mechanism="rd", psd_V2_Hz=4e-12),
                            dict(device="other.Xpair.right", mechanism="id", psd_V2_Hz=4e-12),
                            dict(device="XPAIR.RIGHT", mechanism="id", psd_V2_Hz=4e-12)):
            with self.subTest(replacement=replacement):
                summary = comparison_sample()
                summary["spectre_noise_contributions"].pop(0)
                if replacement is not None:
                    summary["spectre_noise_contributions"].append(replacement)
                rows = noise_contribution_rows(summary, scope="pair")
                aggregate = next(row for row in rows if row["mechanism"] == "thermal")
                self.assertFalse(aggregate["reference_complete"])
                self.assertIn("reference sources incomplete", aggregate["label"])
                self.assertEqual(aggregate["reference_issues"], [
                    dict(device="Xpair.right", mechanism="id", reason="missing")])
                for field in ("simulated_dbc", "psd_V2_Hz", "asd_V_rtHz", "fraction_pct"):
                    self.assertIsNone(aggregate[field])
                self.assertAlmostEqual(aggregate["calculated_dbc"], 10 * math.log10(10e-12))
                target = next(row for row in rows if row["device"] == "Xpair.left" and row["mechanism"] == "id")
                self.assertIsNotNone(target["calculated_dbc"])

    def test_duplicate_modeled_reference_never_adds_twice_or_claims_same_source(self):
        summary = comparison_sample()
        summary["spectre_noise_contributions"].append(dict(summary["spectre_noise_contributions"][0]))
        rows = noise_contribution_rows(summary, scope="pair")
        aggregate = next(row for row in rows if row["mechanism"] == "thermal")
        self.assertIsNone(aggregate["simulated_dbc"])
        self.assertEqual(aggregate["reference_issues"], [
            dict(device="Xpair.right", mechanism="id", reason="duplicate")])
        duplicate_rows = [row for row in rows if row["device"] == "Xpair.right" and row["mechanism"] == "id"]
        self.assertEqual(len(duplicate_rows), 2)
        self.assertTrue(all(row["calculated_dbc"] is None for row in duplicate_rows))
        # An unrelated duplicate does not invalidate a complete modeled subset.
        summary = comparison_sample()
        summary["spectre_noise_contributions"].append(dict(summary["spectre_noise_contributions"][3]))
        aggregate = next(row for row in noise_contribution_rows(summary) if row["mechanism"] == "thermal")
        self.assertTrue(aggregate["reference_complete"])
        self.assertAlmostEqual(aggregate["psd_V2_Hz"], 15e-12, delta=1e-25)

    def test_invalid_reference_psd_is_missing_but_measured_zero_is_valid(self):
        for power in (None, -1, True, float("nan"), float("inf")):
            with self.subTest(power=power):
                summary = comparison_sample()
                summary["spectre_noise_contributions"][0]["psd_V2_Hz"] = power
                aggregate = next(row for row in noise_contribution_rows(summary) if row["mechanism"] == "thermal")
                self.assertIsNone(aggregate["simulated_dbc"])
                self.assertEqual(aggregate["reference_issues"][0]["reason"], "invalid_psd")
        summary = comparison_sample()
        for item in summary["spectre_noise_contributions"]:
            item["psd_V2_Hz"] = 0
        aggregate = next(row for row in noise_contribution_rows(summary) if row["mechanism"] == "thermal")
        self.assertTrue(aggregate["reference_complete"])
        self.assertEqual(aggregate["psd_V2_Hz"], 0)
        self.assertIsNone(aggregate["simulated_dbc"])

    def test_missing_or_unowned_reference_cannot_claim_complete_thermal(self):
        for evidence in (None, {}, dict(scope="target_device", schema_version=1),
                         dict(scope="whole_vco", schema_version=True),
                         dict(scope="whole_vco", schema_version=2)):
            with self.subTest(evidence=evidence):
                summary = comparison_sample()
                summary["spectre_noise_reference"] = evidence
                rows = noise_contribution_rows(summary)
                aggregate = next(row for row in rows if row["mechanism"] == "thermal")
                self.assertFalse(aggregate["reference_complete"])
                self.assertEqual(len(aggregate["reference_issues"]), 3)
                self.assertTrue(all(item["reason"] == "missing" for item in aggregate["reference_issues"]))
                self.assertIsNone(aggregate["simulated_dbc"])
                predicted_only = [row for row in rows if row["kind"] == "source"]
                self.assertEqual(len(predicted_only), 3)
                self.assertTrue(all(row["psd_V2_Hz"] is None and row["simulated_dbc"] is None
                                    for row in predicted_only))
                self.assertTrue(all(row["calculated_psd_V2_Hz"] is not None for row in predicted_only))
        for missing in (None, [], {}):
            summary = comparison_sample()
            summary["spectre_noise_contributions"] = missing
            aggregate = next(row for row in noise_contribution_rows(summary) if row["mechanism"] == "thermal")
            self.assertFalse(aggregate["reference_complete"])

    def test_invalid_unique_reference_keeps_independent_prediction_and_zero(self):
        for power in (None, -1, float("nan")):
            summary = comparison_sample()
            summary["spectre_noise_contributions"][0]["psd_V2_Hz"] = power
            row = next(row for row in noise_contribution_rows(summary)
                       if row["device"] == "Xpair.right" and row["mechanism"] == "id")
            self.assertIsNone(row["psd_V2_Hz"])
            self.assertIsNone(row["simulated_dbc"])
            self.assertEqual(row["calculated_psd_V2_Hz"], 2e-12)
        summary = comparison_sample()
        source = summary["thermal_noise"]["sources"][1]
        source["psd_V2_Hz"] = source["input_band_contributions"][0]["psd_V2_Hz"] = 0
        summary["thermal_noise"]["predicted_psd_V2_Hz"] = 3e-12
        row = next(row for row in noise_contribution_rows(summary)
                   if row["device"] == "Xpair.right" and row["mechanism"] == "id")
        self.assertIsNone(row["calculated_dbc"])
        self.assertEqual(row["calculated_psd_V2_Hz"], 0)

    def test_fn_names_require_explicit_evidence_never_m0_m1_or_first_rows(self):
        rows = noise_contribution_rows(comparison_sample(), scope="pair")
        fn = {row["device"]: row for row in rows if row["kind"] == "source" and row["mechanism"] == "fn"}
        expected = 10 * math.log10(2e-12)
        self.assertAlmostEqual(fn["Xpair.left"]["calculated_dbc"], expected)
        self.assertAlmostEqual(fn["Xpair.right"]["calculated_dbc"], expected)
        self.assertIsNone(fn["M0"]["calculated_dbc"])
        self.assertIsNone(fn["M1"]["calculated_dbc"])
        summary = comparison_sample()
        summary["thermal_noise"] = dict(schema_version=1, status="unavailable",
            target_noise_trace="Xpair.left", peer_noise_trace="Xpair.right", pair_model="symmetric_independent")
        rows = noise_contribution_rows(summary)
        self.assertTrue(all(row["calculated_dbc"] is None for row in rows if row["kind"] == "source"))

    def test_fn_target_does_not_require_peer_but_peer_requires_full_pair_contract(self):
        for mutation in (lambda s: s["thermal_noise"].pop("peer_noise_trace"),
                         lambda s: s["thermal_noise"].update(peer_noise_trace=""),
                         lambda s: s["thermal_noise"].update(peer_noise_trace="Xpair.left"),
                         lambda s: s.pop("pair_model"), lambda s: s.update(pair_model="unverified"),
                         lambda s: s.pop("predicted_pair_pm_asd_V_rtHz"),
                         lambda s: s.update(predicted_pair_pm_asd_V_rtHz=0),
                         lambda s: s.update(predicted_pair_pm_asd_V_rtHz=float("nan"))):
            with self.subTest(mutation=mutation):
                summary = comparison_sample()
                mutation(summary)
                rows = noise_contribution_rows(summary)
                fn = {row["device"]: row for row in rows if row["kind"] == "source" and row["mechanism"] == "fn"}
                self.assertIsNotNone(fn["Xpair.left"]["calculated_dbc"])
                self.assertIsNone(fn["Xpair.right"]["calculated_dbc"])
        for invalid_target in (None, "", " ", 1):
            summary = comparison_sample()
            summary["thermal_noise"]["target_noise_trace"] = invalid_target
            rows = noise_contribution_rows(summary)
            self.assertTrue(all(row["calculated_dbc"] is None for row in rows
                                if row["kind"] == "source" and row["mechanism"] == "fn"))

    def test_duplicate_fn_reference_does_not_assign_prediction_to_ambiguous_rows(self):
        summary = comparison_sample()
        summary["spectre_noise_contributions"].append(dict(summary["spectre_noise_contributions"][4]))
        rows = noise_contribution_rows(summary)
        duplicate = [row for row in rows if row["device"] == "Xpair.left" and row["mechanism"] == "fn"]
        self.assertEqual(len(duplicate), 2)
        self.assertTrue(all(row["calculated_dbc"] is None for row in duplicate))


if __name__ == '__main__':
    unittest.main()
