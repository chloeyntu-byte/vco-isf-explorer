"""Source-only contribution pies: no simulator, GUI, or physical inference."""

import unittest

from isf_tool.tk.charts import contribution_bars, contribution_pies
from isf_tool.tk.app import history_provenance_rows


class ContributionPieTests(unittest.TestCase):
    def source(self, device, simulated, calculated):
        return {"kind": "source", "device": device, "mechanism": "rn", "label": device + " · rn",
                "simulated_dbc": simulated, "calculated_dbc": calculated}

    def test_linear_power_aggregation_excludes_overlapping_aggregates(self):
        rows = [self.source("R1", -100, -100), self.source("R2", -110, None),
                {"kind": "aggregate", "simulated_dbc": 40, "calculated_dbc": 40}]
        pies = contribution_pies(rows)
        self.assertEqual(2, len(pies["simulated"]))
        self.assertEqual(1, len(pies["calculated"]))
        self.assertAlmostEqual(10, pies["simulated"][0]["power"] / pies["simulated"][1]["power"])
        self.assertAlmostEqual(1000 / 11, pies["reference_coverage_pct"])
        self.assertEqual(["R2 · rn"], pies["missing_calculated"])
        self.assertIn("计算 N/A", pies["note"])
        self.assertEqual(pies["simulated"][0]["color_index"], pies["calculated"][0]["color_index"])

    def test_aligned_bars_share_total_reference_and_leave_unmodeled_sources_missing(self):
        reference = {"kind": "aggregate", "label": "VCO 总仿真相噪", "psd_V2_Hz": 1e-10, "simulated_dbc": -100}
        modeled = dict(self.source("R1", -110, -107), psd_V2_Hz=1e-11, calculated_psd_V2_Hz=2e-11)
        unmodeled = dict(self.source("R2", -113, None), psd_V2_Hz=5e-12)
        bars = contribution_bars([reference, modeled, unmodeled])
        self.assertEqual(["R1 · rn", "R2 · rn"], bars["order"])
        self.assertAlmostEqual(10., bars["simulated"][0]["percent"])
        self.assertAlmostEqual(20., bars["calculated"][0]["percent"])
        self.assertEqual(["R2 · rn"], bars["missing_calculated"])
        self.assertIsNone(contribution_bars([modeled])["simulated"][0]["percent"])

    def test_duplicate_source_identity_is_not_deduplicated_by_guessing(self):
        row = self.source("R1", -100, -100)
        pies = contribution_pies([row, dict(row)])
        self.assertFalse(pies["simulated"])
        self.assertFalse(pies["calculated"])
        self.assertIn("重复", pies["note"])

    def test_calculation_only_and_nonfinite_values_are_not_fabricated(self):
        pies = contribution_pies([self.source("R1", None, -100), self.source("R2", float("nan"), float("inf"))])
        self.assertFalse(pies["simulated"])
        self.assertEqual(["R1 · rn"], [row["id"] for row in pies["calculated"]])
        self.assertEqual(["R1 · rn", "R2 · rn"], pies["missing_simulated"])
        self.assertIsNone(pies["reference_coverage_pct"])

    def test_measured_zero_power_is_preserved_not_reported_missing(self):
        row = self.source("R0", None, None)
        row.update(psd_V2_Hz=0, calculated_psd_V2_Hz=0)
        pies = contribution_pies([row])
        self.assertEqual(0, pies["simulated"][0]["power"])
        self.assertEqual(0, pies["calculated"][0]["power"])
        self.assertFalse(pies["missing_simulated"])
        self.assertFalse(pies["missing_calculated"])


class HistoryProvenanceTests(unittest.TestCase):
    def test_current_config_and_historical_schematic_remain_distinct_per_point(self):
        report = {"roles": {"vco": {"selected_test": {"design": {"lib": "L", "cell": "OSC", "view": "config"}},
            "history_manifest": {"source_netlists": [
                {"point_id": "1", "corner": "tt", "source_design": {"library": "L", "cell": "OSC", "view": "schematic"}, "sha256": "a" * 64, "source_mtime": 100000},
                {"point_id": "2", "corner": "ff", "source_design": {"library": "L", "cell": "OSC", "view": "config"}, "sha256": "b" * 64, "source_mtime": "2026-09-21T10:00:00Z"},
            ]}}}}
        rows = history_provenance_rows(report)
        self.assertEqual(2, len(rows))
        self.assertEqual("L/OSC/config", rows[0]["current"])
        self.assertEqual("L/OSC/schematic", rows[0]["history"])
        self.assertIn("不一致", rows[0]["status"])
        self.assertIn("内容以 History 为准", rows[1]["status"])
        self.assertEqual("a" * 64, rows[0]["sha256"])
        self.assertEqual("1 / tt", rows[0]["point"])

    def test_unknown_history_is_not_filled_from_current_design(self):
        rows = history_provenance_rows({"roles": {"vco": {"selected_test": {"design": {"lib": "L", "cell": "OSC", "view": "config"}},
            "selected_history": {"history_manifest": {"source_netlists": [{"point_id": "1"}]}}}}})
        self.assertEqual("未知/未知/未知", rows[0]["history"])
        self.assertEqual("未知", rows[0]["sha256"])
        self.assertIn("不推测", rows[0]["status"])


if __name__ == "__main__":
    unittest.main()
