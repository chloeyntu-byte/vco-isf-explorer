"""Isolated Tk checks for state, page scrolling, and source-only pies."""

import copy
import os
from pathlib import Path
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest import mock

from isf_tool.tk.app import IsfApplication
from isf_tool.tk.controller import IsfController
from test_tk_noise_kinds_gui import thermal_fixture


@unittest.skipUnless(os.environ.get("ISF_GUI_TEST") == "1", "requires explicitly selected GUI display")
class FollowupGuiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.lower()
        self.controller = mock.Mock(workspace=Path("/tmp/isolated-followup-ui"))
        with mock.patch.object(IsfApplication, "_submit"), mock.patch.object(IsfApplication, "_refresh_jobs"):
            self.app = IsfApplication(self.root, self.controller)
        self.app.current_job_id = "fixture"
        self.app.current_job = {"status": "succeeded", "id": "fixture"}

    def tearDown(self):
        self.app.queue.close()
        self.root.update_idletasks()
        self.root.destroy()

    def load(self):
        with mock.patch.object(self.app, "_show_error") as error:
            self.app._result_loaded(thermal_fixture(), "fixture")
            error.assert_not_called()

    def test_single_selector_drives_source_pies_and_no_aggregate_slices(self):
        self.load()
        app = self.app
        self.assertFalse(hasattr(app, "contribution_operating_box"))
        self.assertFalse(hasattr(app, "contribution_offset_box"))
        for identity in ("tt", "ff"):
            app.operating_point_var.set(identity)
            app._operating_point_changed()
            for offset in (1000, 10000):
                app.result_offset_var.set(f"{offset} Hz")
                app._result_offset_changed()
                self.assertEqual(f"{identity}@{offset}", app.current_result_view["point"])
                rows = app.current_result_view["noise_contribution_rows"]
                labels = {row["label"] for row in rows if row["kind"] == "source"}
                self.assertTrue(set(row["id"] for row in app.simulated_noise_pie._slices) <= labels)
                self.assertTrue(set(row["id"] for row in app.calculated_noise_pie._slices) <= labels)
        self.assertFalse(any("累计" in item for item in app.harmonic_curve_box["values"]))

    def test_narrow_window_all_pages_keep_vertical_and_horizontal_reachability(self):
        self.load()
        self.root.geometry("760x540")
        app = self.app
        for tab in (app.setup_tab, app.run_tab, app.result_tab, app.error_tab):
            app.notebook.select(tab)
            self.root.update_idletasks()
            canvas = app._page_canvases[str(tab)]
            bounds = canvas.bbox("all")
            if tab == app.setup_tab:
                self.assertEqual((0.0, 1.0), canvas.xview())
            else:
                self.assertGreater(bounds[2], canvas.winfo_width())
            self.assertGreater(bounds[3], canvas.winfo_height())
            canvas.yview_moveto(1)
            canvas.xview_moveto(1)
            self.root.update_idletasks()
            self.assertAlmostEqual(1, canvas.yview()[1])
            self.assertAlmostEqual(1, canvas.xview()[1])
        app.notebook.select(app.result_tab)
        canvas = app._page_canvases[str(app.result_tab)]
        canvas.yview_moveto(0)
        event = SimpleNamespace(widget=app.phase_noise_chart, num=5, delta=0, state=0)
        app._scroll_setup(event)
        self.assertGreater(canvas.yview()[0], 0)
        before = canvas.yview()
        app._scroll_setup(SimpleNamespace(widget=app.save_state_button, num=5, delta=0, state=0))
        self.assertEqual(before, canvas.yview())

    def test_state_snapshot_and_restore_exact_sample_without_execution(self):
        self.load()
        app = self.app
        app.operating_point_var.set("ff")
        app._operating_point_changed()
        app.result_offset_var.set("10000 Hz")
        app._result_offset_changed()
        app.noise_kind_var.set("flicker")
        app.theme_var.set("dark")
        view = app._state_view()
        config = app._config_from_ui()
        config["execution"]["allow_execute"] = False
        config["testbenches"]["vco"]["mapping_overrides"] = {"vgs_output": "my_waveform"}
        report = {"state": {"config": config, "view": view}, "result": copy.deepcopy(app.current_result), "job": {"id": "fixture", "status": "succeeded"}}
        with mock.patch.object(app, "_submit") as submit, mock.patch("isf_tool.tk.app.messagebox.showwarning") as warning:
            app._apply_saved_state(report)
            submit.assert_not_called()
            warning.assert_not_called()
        self.assertEqual("ff@10000", app.point_var.get())
        self.assertEqual("flicker", app.noise_kind_var.get())
        self.assertEqual("dark", app.theme_var.get())
        self.assertFalse(app.advanced_visible_var.get())
        self.assertFalse(app.allow_execute_var.get())
        self.assertIsNone(app.last_preflight)
        self.assertEqual("my_waveform", app._mapping_overrides["vco"]["vgs_output"])
        self.controller.start_run.assert_not_called()
        self.controller.inspect_pair.assert_not_called()
        self.controller.preflight.assert_not_called()

    def test_state_load_refuses_active_jobs_and_newer_edit(self):
        app = self.app
        submitted = {}
        app._submit = lambda label, function, callback, **kwargs: submitted.update(function=function, callback=callback, **kwargs)
        self.controller.jobs.return_value = {"jobs": [{"id": "busy", "status": "running"}]}
        app._load_saved_state("chosen")
        self.assertTrue(app._job_command_pending)
        with self.assertRaisesRegex(ValueError, "queued/running"):
            submitted["function"]()
        self.controller.load_state.assert_not_called()
        submitted["on_failure"](ValueError("busy"))
        self.assertFalse(app._job_command_pending)
        self.controller.jobs.return_value = {"jobs": []}
        self.controller.load_state.return_value = {"state": {"config": app._config_from_ui(), "view": {}}}
        app._load_saved_state("chosen")
        report = submitted["function"]()
        self.controller.load_state.assert_called_once_with("chosen")
        app.vars["max_harmonic"].set("7")
        with mock.patch("isf_tool.tk.app.messagebox.showwarning") as warning, mock.patch.object(app, "_apply_saved_state") as apply:
            submitted["callback"](report)
            apply.assert_not_called()
            warning.assert_called_once()
        self.assertEqual("7", app.vars["max_harmonic"].get())

    def test_missing_saved_result_coordinate_does_not_select_first_point(self):
        self.load()
        app = self.app
        report = {"state": {"config": app._config_from_ui(), "view": {"job_id": "fixture", "point": "missing", "operating_point_id": "ff"}},
                  "result": copy.deepcopy(app.current_result), "job": {"status": "succeeded"}}
        with mock.patch("isf_tool.tk.app.messagebox.showwarning") as warning:
            app._apply_saved_state(report)
            warning.assert_called_once()
        self.assertIsNone(app.current_result)
        self.assertEqual("", app.point_var.get())

    def test_saved_state_recovers_invalid_editor_and_incomplete_offset(self):
        app = self.app
        valid = app._config_from_ui()
        valid["testbenches"]["vco"]["mapping_overrides"] = {"vgs_output": "saved_wave"}
        self.controller.jobs.return_value = {"jobs": []}
        self.controller.load_state.return_value = {"state": {"config": valid, "view": {}}}
        app.raw_text.delete("1.0", "end")
        app.raw_text.insert("1.0", '{"unfinished":')
        app.vars["offset_value"].set("not-a-frequency")
        submitted = {}
        app._submit = lambda label, function, callback, **kwargs: submitted.update(function=function, callback=callback)
        with mock.patch.object(app, "_show_error") as error:
            app._load_saved_state("chosen")
            self.assertTrue(app._state_load_pending)
            submitted["callback"](submitted["function"]())
            error.assert_not_called()
        self.assertFalse(app._state_load_pending)
        self.assertEqual("saved_wave", app._mapping_overrides["vco"]["vgs_output"])
        self.assertNotEqual("not-a-frequency", app.vars["offset_value"].get())
        self.assertFalse(app._config_from_ui()["execution"]["allow_execute"])
        self.controller.start_run.assert_not_called()

    def test_new_invalid_raw_edit_during_state_load_is_not_overwritten(self):
        app = self.app
        submitted = {}
        app._submit = lambda label, function, callback, **kwargs: submitted.update(callback=callback)
        app._load_saved_state("chosen")
        app.raw_text.insert("end", "unfinished newer edit")
        with mock.patch("isf_tool.tk.app.messagebox.showwarning") as warning, mock.patch.object(app, "_apply_saved_state") as apply:
            submitted["callback"]({"state": {"config": {}, "view": {}}})
            warning.assert_called_once()
            apply.assert_not_called()
        self.assertIn("unfinished newer edit", app.raw_text.get("1.0", "end"))

    def test_thermal_band_notice_uses_budget_not_current_form(self):
        result = thermal_fixture()
        for row in result["summary"]:
            row["thermal_noise"].update(input_max_harmonic=1, requested_max_harmonic=1)
        self.app.vars["max_harmonic"].set("7")
        self.app._result_loaded(result, "fixture")
        self.assertIn("|m| ≤ 1", self.app.result_quality_var.get())
        self.assertNotIn("|m| ≤ 7", self.app.result_quality_var.get())
        self.app.noise_kind_var.set("flicker")
        self.app._point_changed()
        self.assertNotIn("Thermal 独立输入频带", self.app.result_quality_var.get())
        self.app.noise_kind_var.set("both")
        self.app._result_loaded(thermal_fixture(), "fixture")
        self.assertIn("实际频带范围未记录", self.app.result_quality_var.get())

    def test_real_controller_state_roundtrip_under_workspace_without_running(self):
        with tempfile.TemporaryDirectory(prefix="isf-tk-state-") as directory:
            controller = IsfController(directory)
            config = self.app._config_from_ui()
            config["execution"]["allow_execute"] = True
            config["testbenches"]["vco"]["mapping_overrides"] = {"transfer_magnitude_output": "harm_mag"}
            saved = controller.save_state({"name": "my restored setup", "config": config, "view": self.app._state_view()})
            self.assertEqual(Path(directory) / "states", Path(saved["path"]).parent)
            self.assertEqual(saved["id"], controller.list_states()["states"][0]["id"])
            restored = controller.load_state(saved["id"])
            self.assertFalse(restored["state"]["config"]["execution"]["allow_execute"])
            self.assertEqual("harm_mag", restored["state"]["config"]["testbenches"]["vco"]["mapping_overrides"]["transfer_magnitude_output"])
            self.assertFalse(controller.jobs()["jobs"])

    def test_binding_provenance_renders_mixed_histories_without_changing_selection(self):
        app = self.app
        app.vars["vco_history"].set("frozen-history")
        report = {"roles": {"vco": {"selected_test": {"design": {"lib": "L", "cell": "OSC", "view": "config"}},
            "history_manifest": {"source_netlists": [
                {"point_id": "1", "corner": "tt", "source_design": {"library": "L", "cell": "OSC", "view": "schematic"}, "sha256": "a" * 64},
                {"point_id": "2", "corner": "ff", "source_design": {"library": "L", "cell": "OSC", "view": "config"}, "sha256": "b" * 64},
            ]}}}}
        app._render_mapping_report(report)
        self.assertEqual(2, len(app.history_provenance_tree.get_children()))
        self.assertIn("多种来源", app.binding_provenance_var.get())
        self.assertIn("不会自动跟随", app.binding_provenance_var.get())
        self.assertEqual("frozen-history", app.vars["vco_history"].get())
        self.controller.preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
