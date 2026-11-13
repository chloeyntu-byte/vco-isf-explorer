"""Opt-in temporary Tk result controls; no real service or EDA execution."""

import os
import tkinter as tk
import unittest
from unittest import mock

from isf_tool.tk.app import IsfApplication
from test_tk_total_noise import result_fixture


@unittest.skipUnless(os.environ.get("ISF_GUI_TEST") == "1", "requires explicitly selected GUI display")
class TotalNoiseGuiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.lower()
        with mock.patch.object(IsfApplication, "_submit"), mock.patch.object(IsfApplication, "_refresh_jobs"):
            self.app = IsfApplication(self.root, mock.Mock(workspace="/tmp/isolated-total-noise-ui"))
        self.app.current_job_id = "fixture"
        self.app.current_job = {"status": "succeeded"}

    def tearDown(self):
        self.app.queue.close()
        self.root.update_idletasks()
        self.root.destroy()

    def load(self, result):
        with mock.patch.object(self.app, "_show_error") as error:
            self.app._result_loaded(result, "fixture")
            error.assert_not_called()

    def test_total_reference_scope_and_contribution_selection_stay_synchronized(self):
        self.app.noise_kind_var.set("flicker")
        self.load(result_fixture())
        app = self.app
        self.assertEqual("pair", app.noise_scope_var.get())
        before = app.summary_tree.item("summary-0", "values")
        reference_curve = next(row["points"] for row in app.phase_noise_chart._series if row["id"] == "spectre")
        # An obsolete scope value cannot reintroduce single/pair disagreement.
        app.noise_scope_var.set("single")
        app._point_changed()
        after = app.summary_tree.item("summary-0", "values")
        self.assertEqual(before[3], after[3])
        self.assertEqual(before[4], after[4])
        self.assertEqual(reference_curve, next(row["points"] for row in app.phase_noise_chart._series if row["id"] == "spectre"))
        self.assertIn("two-device", app.metrics_var.get())
        self.assertIn("VCO simulated total", app.summary_tree.heading("spectre", "text"))
        self.assertIn("Completion does not imply validation", app.result_quality_var.get())
        self.assertFalse(hasattr(app, "contribution_operating_box"))
        app.operating_point_var.set("ff")
        app._operating_point_changed()
        app.result_offset_var.set("10000 Hz")
        app._result_offset_changed()
        self.assertEqual("ff@10000", app.point_var.get())
        self.assertFalse(hasattr(app, "contribution_offset_box"))
        self.assertTrue(app.simulated_noise_pie._slices)
        devices = [app.noise_contribution_tree.item(iid, "values")[1] for iid in app.noise_contribution_tree.get_children()]
        self.assertIn("M0", devices)
        self.assertNotIn("R0", devices)  # flicker filter does not show rn
        self.assertIn("ISF two-device", app.summary_tree.heading("calculated", "text"))
        for geometry in ("1020x720", "1240x860"):
            self.root.geometry(geometry)
            self.root.update_idletasks()
            self.assertTrue(app.phase_noise_chart.find_all())

    def test_old_results_explicitly_report_missing_total_and_details(self):
        self.app.noise_kind_var.set("flicker")
        result = result_fixture()
        for row in result["summary"]:
            row.pop("spectre_noise_reference")
            row.pop("spectre_noise_contributions")
        self.load(result)
        app = self.app
        self.assertEqual("N/A", app.summary_tree.item("summary-0", "values")[4])
        self.assertIn("simulated total phase noise was not collected", app.metrics_var.get())
        self.assertIn("No collected per-device", app.noise_contribution_note_var.get())
        self.assertTrue(app.noise_contribution_tree.get_children())


if __name__ == "__main__":
    unittest.main()
