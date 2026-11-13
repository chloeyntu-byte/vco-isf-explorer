"""Opt-in native geometry checks: ISF_GUI_TEST=1 with a valid desktop DISPLAY."""

import os
import unittest
from unittest import mock
import tkinter as tk
from tkinter import font as tkfont

from isf_tool.tk.app import IsfApplication, _HelpText


@unittest.skipUnless(os.environ.get("ISF_GUI_TEST") == "1", "requires an explicitly selected GUI display")
class NativeReadabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.title("ISF temporary layout verification")
        self.root.lower()

    def tearDown(self):
        self.root.update_idletasks()
        self.root.destroy()

    def test_wrapped_english_guidance_has_spacing_and_grows_on_narrower_width(self):
        font = tkfont.Font(self.root, family="DejaVu Sans", size=11)
        variable = tk.StringVar(value="First line: Confirm Maestro net names and output expressions. \nSecond line: Select a row to view its source or edit the tool mapping. " * 3)
        widget = _HelpText(self.root, font=font, textvariable=variable)
        widget.pack(fill="x")
        self.root.geometry("850x700")
        self.root.update_idletasks()
        wide_height = widget.winfo_height()
        self.root.geometry("430x700")
        self.root.update_idletasks()
        self.assertGreater(widget.winfo_height(), wide_height)
        self.assertGreaterEqual(int(widget.cget("spacing2")), 3)
        self.assertEqual("disabled", str(widget.cget("state")))
        self._assert_text_fits(widget)
        variable.set("One line of guidance")
        self.root.update_idletasks()
        self.assertLess(widget.winfo_height(), wide_height)
        self._assert_text_fits(widget)

    def _assert_text_fits(self, widget):
        pixels = widget.count("1.0", "end", "ypixels")[0]
        self.assertGreaterEqual(widget.winfo_height(), pixels + 2 * int(widget.cget("pady")))

    def test_all_advanced_pages_and_main_help_fit_at_supported_window_sizes(self):
        controller = mock.Mock(workspace="/tmp/isolated-ui-layout-test")
        with mock.patch.object(IsfApplication, "_submit"), mock.patch.object(IsfApplication, "_refresh_jobs"):
            app = IsfApplication(self.root, controller)
        try:
            app._render_mapping_report({"roles": {"vco": {"signal_catalog": [
                {"kind": "net", "name": "VOUT_N"},
                {"kind": "output", "name": "HB_TIME_VGS", "expression": 'v("G")-v("S")'},
            ]}, "flicker": {"signal_catalog": [{"kind": "variable", "name": "VGS"}]}}})
            app._open_catalog()
            self.assertEqual(str(app.inventory_tab), app.advanced_tabs.select())
            self.assertIn("Net name 1 items", app.catalog_status_var.get())
            app.catalog_kind_var.set("Output")
            self.assertEqual(1, len(app.catalog_tree.get_children()))
            for geometry in ("1020x720", "1240x860"):
                self.root.geometry(geometry)
                for tab in app.advanced_tabs.tabs():
                    app.advanced_tabs.select(tab)
                    self.root.update_idletasks()
                    for widget in app._help_widgets:
                        if widget.winfo_ismapped():
                            self._assert_text_fits(widget)
                            self.assertGreater(widget.winfo_width(), 100)
            self.assertGreaterEqual(int(app.style.lookup("Treeview", "rowheight")), app.body_font.metrics("linespace") + 12)
            self.assertEqual("normal", app.body_font.actual("weight"))
            for tree in (app.catalog_tree, app.portable_tree, app.preflight_tree, app.jobs_tree):
                container = tree.pack_info()["in"]
                stacking = [str(child) for child in tree.master.winfo_children()]
                self.assertGreater(stacking.index(str(tree)), stacking.index(str(container)),
                                   "scroll container must not paint over its sibling table")
        finally:
            app.queue.close()

    def test_execution_has_no_mode_or_consent_controls_and_result_defaults_to_pair_both(self):
        with mock.patch.object(IsfApplication, "_submit"), mock.patch.object(IsfApplication, "_refresh_jobs"):
            app = IsfApplication(self.root, mock.Mock(workspace="/tmp/isolated-ui-controls-test"))
        try:
            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)
            self.assertFalse(hasattr(app, "allow_execute_check"))
            self.assertFalse(any(widget.winfo_class() == "TRadiobutton" for widget in descendants(app.run_tab)))
            self.assertFalse(any(widget.winfo_class() == "TRadiobutton" for widget in descendants(app.result_tab)))
            self.assertEqual("pair", app.noise_scope_var.get())
            self.assertEqual("both", app.noise_kind_var.get())
            self.assertEqual(("both", "flicker", "thermal"), app.noise_kind_box["values"])
            self.assertFalse(app._config_from_ui()["execution"]["allow_execute"])
            self.root.update_idletasks()
        finally:
            app.queue.close()

    def test_preflight_failure_shows_concrete_errors_and_locates_mapping_without_accepting(self):
        controller = mock.Mock(workspace="/tmp/isolated-ui-layout-test")
        with mock.patch.object(IsfApplication, "_submit"), mock.patch.object(IsfApplication, "_refresh_jobs"):
            app = IsfApplication(self.root, controller)
        try:
            pair = {"roles": {"vco": {"mapping_fields": [{"key": "target_instance", "label": "VCO target device", "status": "ambiguous", "candidates": ["M0", "M1"]}]}}}
            setup = {"fields": [{"key": "main.target_instance", "label": "VCO target device (Netlist-local name)", "status": "missing"}],
                     "errors": [{"field": "main.target_instance", "message": "Two candidates exist, Select"}]}
            report = {"ok": False, "summary": {"fail": 2}, "required_user_fields": ["portable_setup", "execution.allow_execute"],
                      "checks": [{"status": "fail", "field": "portable_setup", "label": "Simulation execution settings", "detail": "Needs input"},
                                 {"status": "fail", "field": "execution.allow_execute", "label": "Execution consent", "detail": "Authorize this Live consent"}],
                      "detected": {"maestro_pair": pair}, "resolved_config": {"detection": {"portable_setup": setup}}}
            app._render_preflight(report)
            self.assertFalse(app.last_preflight["ok"])
            self.assertIn("VCO target device (Netlist-local name): Two candidates exist", app.preflight_detail_var.get())
            self.assertIn("Authorize this Live consent", app.preflight_detail_var.get())
            app._locate_preflight_issue()
            self.assertEqual(str(app.mapping_tab), app.advanced_tabs.select())
            self.assertEqual(("M0", "M1"), app._mapping_inputs["vco:target_instance"].cget("values"))
            self.assertEqual("normal", str(app._mapping_inputs["vco:target_instance"].cget("state")))
            self.assertEqual({}, app._mapping_overrides["vco"])
            self.assertFalse(app.allow_execute_var.get())
        finally:
            app.queue.close()

    def test_each_required_name_is_directly_editable_and_unapplied_input_survives_redraw(self):
        controller = mock.Mock(workspace="/tmp/isolated-ui-layout-test")
        with mock.patch.object(IsfApplication, "_submit"), mock.patch.object(IsfApplication, "_refresh_jobs"):
            app = IsfApplication(self.root, controller)
        try:
            app.vars["replica_source"].set("maestro")
            app.vars["vco_path"].set("/test/vco/maestro")
            app.vars["replica_path"].set("/test/noise/maestro")
            report = {"roles": {"vco": {"mapping_fields": [
                {"key": "vgs_output", "label": "Gate-source time waveform", "value": "A1", "detected": "A1", "status": "detected", "candidates": ["A1", "completely_unrelated_alias"], "maestro_presence": "named_output"},
                {"key": "vbs_output", "label": "Body-source time waveform", "required": True, "active": False, "status": "missing", "maestro_presence": "script_derived", "effective_derivation": "V(B)-V(S)"}
            ]}, "flicker": {"mapping_fields": [
                {"key": "dc_sense_source", "label": "Current-sensing voltage source", "status": "ambiguous", "candidates": ["V0", "branch_q7"]},
                {"key": "vgs_pwl_variable", "label": "Gate-source waveform file variable", "value": "file_a", "detected": "file_a", "selected_details": {"scope": "global"}}
            ]}}}
            app._render_mapping_report(report)
            self.assertIn("vco:vbs_output", app._mapping_inputs)
            self.assertEqual("A1", app._mapping_inputs["vco:vgs_output"].get())
            app._mapping_inputs["vco:vgs_output"].set("completely_unrelated_alias")
            app._mapping_inputs["replica:dc_sense_source"].set("branch_q7")
            app._mapping_inputs["replica:vgs_pwl_variable"].set("portable_file_alias")
            payload = app._config_from_ui()
            self.assertEqual("completely_unrelated_alias", payload["testbenches"]["vco"]["mapping_overrides"]["vgs_output"])
            self.assertEqual("branch_q7", payload["testbenches"]["replica"]["mapping_overrides"]["dc_sense_source"])
            self.assertEqual("portable_file_alias", payload["testbenches"]["replica"]["mapping_overrides"]["vgs_pwl_variable"])
            app._render_mapping_report(report)
            self.assertEqual("completely_unrelated_alias", app._mapping_inputs["vco:vgs_output"].get())
            self.assertIn("Awaiting validation", app._mapping_status_labels["vco:vgs_output"].cget("text"))
            captured = {}
            app._submit = lambda _label, _function, success: captured.update(success=success)
            app._pair_inspected = mock.Mock()
            app._inspect_pair()
            app._mapping_inputs["vco:vgs_output"].set("newer_alias")
            captured["success"](report)
            app._pair_inspected.assert_not_called()
            app._reset_mapping_input("vco:vgs_output")
            self.assertEqual("A1", app._mapping_inputs["vco:vgs_output"].get())
            self.assertNotIn("vgs_output", app._mapping_overrides["vco"])
            app._open_advanced_tab(app.mapping_tab)
            for geometry in ("1020x720", "1240x860"):
                self.root.geometry(geometry)
                self.root.update_idletasks()
                self.assertGreaterEqual(app.mapping_canvas.winfo_width(), app.mapping_rows_frame.winfo_reqwidth())
                for box in app._mapping_inputs.values():
                    self.assertGreaterEqual(box.winfo_height(), app.body_font.metrics("linespace") + 8)
        finally:
            app.queue.close()

    @unittest.skipIf(os.environ.get('CI'), 'pixel layout depends on the host font set; checked on the reference desktop')
    def test_compact_setup_selects_one_mos_and_retains_manual_reference(self):
        controller = mock.Mock(workspace="/tmp/isolated-ui-layout-test")
        context = {"library": "L", "cell": "VCO", "view": "maestro", "maestro_dir": "/test/vco"}
        with mock.patch.object(IsfApplication, "_submit"), mock.patch.object(IsfApplication, "_refresh_jobs"):
            app = IsfApplication(self.root, controller, maestro_context=context)
        try:
            app.vars["vco_path"].set("/test/vco")
            app.vars["vco_history"].set("/test/vco/H1.rdb")
            app._render_mapping_report({"roles": {"vco": {"mapping_fields": [{
                "key": "target_instance", "required": True, "status": "ambiguous", "candidates": ["M0", "M1"]
            }]}}})
            self.root.geometry("1020x720")
            self.root.update_idletasks()
            self.assertEqual((0.0, 1.0), app.setup_canvas.xview())
            self.assertFalse(app.manual_reference_frame.winfo_ismapped())
            canvas_bottom = app.setup_canvas.winfo_rooty() + app.setup_canvas.winfo_height()
            for widget in (app.vco_history_box, app.target_mos_box, app.offset_value_entry, app.max_harmonic_entry):
                self.assertTrue(widget.winfo_ismapped())
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), canvas_bottom)
            self.assertTrue(app.setup_start_button.winfo_ismapped())
            self.assertTrue(app.environment_button.winfo_ismapped())
            with mock.patch.object(app, "_inspect_pair") as inspect:
                app.target_mos_box.set("M1")
                app._target_mos_chosen()
                inspect.assert_called_once_with()
            config = app._config_from_ui()
            self.assertEqual("generated", config["testbenches"]["replica"]["source"])
            self.assertEqual("M1", config["testbenches"]["vco"]["mapping_overrides"]["target_instance"])
            self.assertFalse(config["execution"]["allow_execute"])
            app._populate_form({"testbenches": {"vco": {"path": "/test/vco"}, "replica": {
                "path": "/test/manual", "history": "/test/manual/H1.rdb"
            }}})
            self.root.update_idletasks()
            self.assertTrue(app.manual_reference_frame.winfo_ismapped())
            self.assertFalse(app.generated_reference_frame.winfo_ismapped())
            self.assertEqual("maestro", app._config_from_ui()["testbenches"]["replica"]["source"])
        finally:
            app.queue.close()

    def test_environment_button_works_with_unapplied_invalid_advanced_json(self):
        controller = mock.Mock(workspace="/tmp/isolated-ui-layout-test")
        with mock.patch.object(IsfApplication, "_submit"), mock.patch.object(IsfApplication, "_refresh_jobs"):
            app = IsfApplication(self.root, controller)
        try:
            app.raw_text.insert("1.0", "{invalid")
            app._submit = lambda _label, check, show, **_options: show(check())
            report = {"status": "attention"}
            with mock.patch("isf_tool.core.environment_report.inspect_environment", return_value=report) as inspect, \
                 mock.patch("isf_tool.core.environment_report.format_environment_report", return_value="Spectre Awaiting check"):
                app.environment_button.invoke()
            inspect.assert_called_once_with({}, controller.workspace, None)
            dialogs = [window for window in self.root.winfo_children() if isinstance(window, tk.Toplevel)]
            self.assertEqual(["ISF environment check"], [window.title() for window in dialogs])
            self.assertFalse(app.environment_button.instate(("disabled",)))
        finally:
            app.queue.close()


if __name__ == "__main__":
    unittest.main()
