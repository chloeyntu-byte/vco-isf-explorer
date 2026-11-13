"""Opt-in, isolated Tk checks. Never loads a real service or starts EDA."""

import os
import tkinter as tk
import unittest
from unittest import mock

from isf_tool.tk.app import IsfApplication


@unittest.skipUnless(os.environ.get("ISF_GUI_TEST") == "1", "requires explicitly selected GUI display")
class MultiOffsetGeometryTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.title("ISF stage B temporary UI verification")
        self.root.lower()
        with mock.patch.object(IsfApplication, "_submit"), mock.patch.object(IsfApplication, "_refresh_jobs"):
            self.app = IsfApplication(self.root, mock.Mock(workspace="/tmp/isolated-stage-b-ui"))

    def tearDown(self):
        self.app.queue.close()
        self.root.update_idletasks()
        self.root.destroy()

    def test_offset_modes_rearrange_and_preview_actual_sites(self):
        app = self.app
        for mode in ("single", "list", "linear", "log"):
            app.vars["offset_mode"].set(mode)
            if mode == "linear":
                app.vars["offset_spacing"].set("intervals")
            self.root.update_idletasks()
            self.assertEqual("grid", app.offset_input_frames[mode].winfo_manager())
            for other, frame in app.offset_input_frames.items():
                if other != mode:
                    self.assertEqual("", frame.winfo_manager())
        app.vars["offset_start"].set("1k")
        app.vars["offset_stop"].set("100M")
        self.assertIn("6 个频偏", app.offset_preview_var.get())
        app._toggle_advanced()
        self.assertEqual(str(app.mapping_tab), app.advanced_tabs.select())
        self.assertNotIn(str(app.inventory_tab), app.advanced_tabs.tabs())
        app._open_catalog()
        self.assertIn(str(app.inventory_tab), app.advanced_tabs.tabs())

    def test_real_result_controls_separate_operating_point_offset_and_render_curves(self):
        app = self.app
        app.noise_kind_var.set("flicker")
        result = {"summary": [], "points": {}, "run_kind": "replay"}
        for op in ("case_a-SW0", "case_b-SW1"):
            for hz in (1000, 10000):
                point = f"{op}@{hz}Hz"
                summary = {"point": point, "operating_point_id": op, "corner": "tt", "offset_Hz": hz, "operating_parameters": {"SW": int(op.endswith('1'))}, "carrier_peak_V": 1, "dynamic_pm_asd_V_rtHz": 1e-6, "spectre_fn_pm_asd_V_rtHz": 2e-6}
                result["summary"].append(summary)
                result["points"][point] = {"harmonics": [{"k": 0, "Q_real_V_rtHz": 1e-6, "Q_imag_V_rtHz": 0, "cum_real_V_rtHz": 1e-6, "cum_imag_V_rtHz": 0}], "workpoint": []}
        app.current_job_id = "fixture"
        app.current_job = {"status": "succeeded"}
        with mock.patch.object(app, "_show_error") as error:
            app._result_loaded(result, "fixture")
            error.assert_not_called()
        app.operating_point_var.set("case_b-SW1")
        app._operating_point_changed()
        app.result_offset_var.set("10000 Hz")
        app._result_offset_changed()
        self.assertEqual("case_b-SW1@10000Hz", app.point_var.get())
        self.assertEqual(2, len(app.phase_noise_chart._series))
        self.assertTrue(app.phase_noise_chart._log_x)
        self.assertEqual(2, len(app.phase_noise_chart._series[0]["points"]))
        app._render_noise_curves()
        self.assertFalse(app.harmonic_curve_box["values"])
        self.assertEqual([], app.harmonic_noise_chart._series)
        self.assertFalse(app.harmonic_noise_chart.winfo_ismapped())
        for geometry in ("1020x720", "1240x860"):
            self.root.geometry(geometry)
            self.root.update_idletasks()
            self.assertTrue(app.phase_noise_chart.find_all())
        with mock.patch("isf_tool.tk.app.messagebox.showinfo") as explanation:
            app._show_metric("Q_real_V_rtHz")
            self.assertIn("复数", explanation.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
