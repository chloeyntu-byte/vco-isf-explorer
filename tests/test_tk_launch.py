from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tkinter as tk

from isf_tool.tk import app


class FakeRoot:
    def __init__(self) -> None:
        self.mainloop_called = False
        self.destroy_called = False

    def mainloop(self):
        self.mainloop_called = True

    def destroy(self):
        self.destroy_called = True


class ImmediateQueue:
    def __init__(self) -> None:
        self.closed = False

    def submit(self, function, on_success=None, on_error=None):
        try:
            value = function()
        except Exception as exc:
            if on_error:
                on_error(exc)
        else:
            if on_success:
                on_success(value)

    def close(self):
        self.closed = True


class FakeVariable:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeWidget:
    def __init__(self):
        self.options = {}

    def configure(self, **options):
        self.options.update(options)


class FakeCloseController:
    def __init__(self, jobs):
        self._jobs = jobs

    def jobs(self):
        return {"jobs": self._jobs}


class TkLaunchTests(unittest.TestCase):
    def test_headless_launch_reports_web_command_without_starting_a_server(self):
        stderr = io.StringIO()
        with mock.patch.object(app.tk, "Tk", side_effect=tk.TclError("no display name")), mock.patch(
            "sys.stderr", stderr
        ):
            result = app.main(["--workspace", "/tmp/isf-headless-test"])
        self.assertEqual(2, result)
        self.assertIn("DISPLAY", stderr.getvalue())
        self.assertIn("vco-isf-web", stderr.getvalue())
        self.assertIn("python run.py --web", stderr.getvalue())

    def test_main_builds_native_application_and_enters_mainloop(self):
        root = FakeRoot()
        controller = object()
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(app.tk, "Tk", return_value=root), mock.patch.object(
            app, "IsfController", return_value=controller
        ) as controller_type, mock.patch.object(app, "IsfApplication") as application_type:
            result = app.main(["--workspace", temporary])
        self.assertEqual(0, result)
        self.assertTrue(root.mainloop_called)
        controller_type.assert_called_once_with(Path(temporary).resolve())
        application_type.assert_called_once_with(root, controller)

    def test_close_guard_keeps_window_open_for_active_fake_service_job(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.root = FakeRoot()
        instance.queue = ImmediateQueue()
        instance.controller = FakeCloseController([{"id": "job-a", "status": "running"}])
        instance._close_check_pending = False
        instance._job_command_pending = False
        with mock.patch.object(app.messagebox, "showwarning") as warning:
            instance._request_close()
        self.assertFalse(instance.root.destroy_called)
        self.assertFalse(instance.queue.closed)
        self.assertIn("不会假称", warning.call_args.args[1])

    def test_close_guard_closes_only_after_all_fake_jobs_are_terminal(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.root = FakeRoot()
        instance.queue = ImmediateQueue()
        instance.controller = FakeCloseController(
            [
                {"id": "job-a", "status": "succeeded"},
                {"id": "job-b", "status": "failed"},
            ]
        )
        instance._close_check_pending = False
        instance._job_command_pending = False
        instance._request_close()
        self.assertTrue(instance.queue.closed)
        self.assertTrue(instance.root.destroy_called)

    def test_active_jobs_only_returns_queued_or_running_rows(self):
        rows = app.active_jobs(
            {
                "jobs": [
                    {"id": "a", "status": "queued"},
                    {"id": "b", "status": "running"},
                    {"id": "c", "status": "succeeded"},
                    {"id": "d", "status": "interrupted"},
                ]
            }
        )
        self.assertEqual(["a", "b"], [row["id"] for row in rows])

    def test_close_guard_blocks_pending_preflight_to_start_transition(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.root = FakeRoot()
        instance.queue = ImmediateQueue()
        instance.controller = FakeCloseController([])
        instance._close_check_pending = False
        instance._job_command_pending = True
        with mock.patch.object(app.messagebox, "showwarning") as warning:
            instance._request_close()
        self.assertFalse(instance.root.destroy_called)
        self.assertFalse(instance.queue.closed)
        self.assertIn("尚未完成提交", warning.call_args.args[1])

    def test_form_does_not_overwrite_advanced_only_adapter_mode(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.vars = {"project_name": FakeVariable("portable")}
        instance.mode_var = FakeVariable("dry")
        instance.allow_execute_var = FakeVariable(False)
        instance.raw_config = {"execution": {"adapter_mode": "protocol"}}
        fields = instance._collect_fields()
        self.assertNotIn("adapter_mode", fields)
        self.assertEqual("portable", fields["project_name"])

    def test_config_load_never_restores_live_execution_consent(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.vars = {
            key: FakeVariable("")
            for key in (
                "vco_binding_fingerprint",
                "vco_history_fingerprint",
                "replica_binding_fingerprint",
                "replica_history_fingerprint",
            )
        }
        instance.mode_var = FakeVariable("dry")
        instance.allow_execute_var = FakeVariable(True)
        instance.allow_execute_check = FakeWidget()
        instance.binding_status_var = FakeVariable("")
        instance._updating_form = False
        instance._populate_form(
            {"execution": {"dry_run": False, "allow_execute": True}}
        )
        self.assertEqual("live", instance.mode_var.get())
        self.assertFalse(instance.allow_execute_var.get())

    def test_pair_inspection_discards_result_after_selection_changes(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.root = FakeRoot()
        instance.vars = {
            f"{role}_{field}": FakeVariable(value)
            for role, values in (
                ("vco", ("/vco", "vco_test", "/vco.rdb")),
                ("replica", ("/replica", "noise_test", "/replica.rdb")),
            )
            for field, value in zip(("path", "test", "history"), values)
        }
        config = {
            "testbenches": {
                "vco": {"path": "/vco", "test": "vco_test", "history": "/vco.rdb"},
                "replica": {"path": "/replica", "test": "noise_test", "history": "/replica.rdb"},
            }
        }
        instance._config_from_ui = lambda: config
        instance.binding_status_var = FakeVariable("")
        instance._pair_inspected = mock.Mock()
        captured = {}

        def submit(_label, _function, success, **_kwargs):
            captured["success"] = success

        instance._submit = submit
        instance._inspect_pair()
        instance.vars["vco_history"].set("/new.rdb")
        captured["success"]({"ok": True})
        instance._pair_inspected.assert_not_called()
        self.assertIn("过期", instance.binding_status_var.get())

    def test_async_config_guard_is_fail_closed(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance._config_from_ui = lambda: {"schema_version": 2, "value": 1}
        self.assertTrue(instance._config_still_current({"schema_version": 2, "value": 1}))
        self.assertFalse(instance._config_still_current({"schema_version": 2, "value": 2}))
        instance._config_from_ui = mock.Mock(side_effect=ValueError("invalid editor"))
        self.assertFalse(instance._config_still_current({"schema_version": 2, "value": 1}))

    def test_run_submission_is_single_flight_before_preflight_returns(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.root = FakeRoot()
        instance._close_check_pending = False
        instance._job_command_pending = False
        instance.mode_var = FakeVariable("dry")
        instance.allow_execute_var = FakeVariable(False)
        instance.controller = FakeCloseController([])
        instance._config_from_ui = lambda: {
            "schema_version": 2,
            "execution": {"dry_run": True, "allow_execute": False},
        }
        submissions = []
        instance._submit = lambda *args, **kwargs: submissions.append((args, kwargs))
        instance._start_run()
        self.assertTrue(instance._job_command_pending)
        self.assertEqual(1, len(submissions))
        with mock.patch.object(app.messagebox, "showwarning") as warning:
            instance._start_run()
        self.assertEqual(1, len(submissions))
        self.assertIn("请勿重复操作", warning.call_args.args[1])

    def test_live_run_is_blocked_while_another_job_is_active(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.root = FakeRoot()
        instance._close_check_pending = False
        instance._job_command_pending = False
        instance.mode_var = FakeVariable("live")
        instance.allow_execute_var = FakeVariable(True)
        instance.controller = FakeCloseController(
            [{"id": "live-a", "status": "running"}]
        )
        instance._config_from_ui = lambda: {
            "schema_version": 2,
            "execution": {"dry_run": False, "allow_execute": True},
        }
        labels = []

        def submit(label, function, success, **_kwargs):
            labels.append(label)
            success(function())

        instance._submit = submit
        with mock.patch.object(app.messagebox, "askyesno", return_value=True), mock.patch.object(
            app.messagebox, "showwarning"
        ) as warning:
            instance._start_run()

        self.assertFalse(instance._job_command_pending)
        self.assertEqual(["检查活动任务"], labels)
        self.assertIn("live-a", warning.call_args.args[1])

    def test_dry_run_is_also_blocked_while_another_job_is_active(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.root = FakeRoot()
        instance._close_check_pending = False
        instance._job_command_pending = False
        instance.mode_var = FakeVariable("dry")
        instance.allow_execute_var = FakeVariable(False)
        instance.controller = FakeCloseController(
            [{"id": "live-a", "status": "running"}]
        )
        instance._config_from_ui = lambda: {
            "schema_version": 2,
            "execution": {"dry_run": True, "allow_execute": False},
        }
        labels = []

        def submit(label, function, success, **_kwargs):
            labels.append(label)
            success(function())

        instance._submit = submit
        with mock.patch.object(app.messagebox, "showwarning") as warning:
            instance._start_run()

        self.assertFalse(instance._job_command_pending)
        self.assertEqual(["检查活动任务"], labels)
        self.assertIn("live-a", warning.call_args.args[1])

    def test_resume_is_blocked_while_another_job_is_active(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.root = FakeRoot()
        instance._close_check_pending = False
        instance._job_command_pending = False
        instance.current_job_id = "failed-a"
        instance.controller = FakeCloseController(
            [{"id": "live-a", "status": "queued"}]
        )
        labels = []

        def submit(label, function, success, **_kwargs):
            labels.append(label)
            success(function())

        instance._submit = submit
        with mock.patch.object(app.messagebox, "askyesno", return_value=True), mock.patch.object(
            app.messagebox, "showwarning"
        ) as warning:
            instance._resume_job()

        self.assertFalse(instance._job_command_pending)
        self.assertEqual(["检查活动任务"], labels)
        self.assertIn("live-a", warning.call_args.args[1])

    def test_stale_result_is_not_loaded_for_newly_selected_job(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.current_job_id = "job-b"
        instance.current_result = None
        instance.current_result_job_id = ""
        instance._schedule_poll = mock.Mock()
        instance._result_loaded({"job_id": "job-a"}, "job-a")
        self.assertIsNone(instance.current_result)
        self.assertEqual("", instance.current_result_job_id)
        instance._schedule_poll.assert_called_once_with(0)

    def test_pending_poll_retries_for_current_selection(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance._poll_after_id = "old"
        instance._poll_pending = True
        instance.current_job_id = "job-b"
        instance._schedule_poll = mock.Mock()
        instance._poll_job()
        instance._schedule_poll.assert_called_once_with(100)

    def test_json_export_freezes_selected_job_result_at_click_time(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.root = FakeRoot()
        instance.current_job_id = "job-a"
        instance.current_result_job_id = "job-a"
        instance.current_result = {"job_id": "job-a", "value": {"n": 1}}
        captured = {}

        def submit(_label, function, _success, **_kwargs):
            captured["function"] = function

        instance._submit = submit
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            with mock.patch.object(app.filedialog, "asksaveasfilename", return_value=str(output)):
                instance._export_result_json()
            instance.current_result = {"job_id": "job-b", "value": {"n": 2}}
            captured["function"]()
            self.assertEqual("job-a", json.loads(output.read_text(encoding="utf-8"))["job_id"])

    def test_atomic_exports_replace_complete_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            json_path = Path(temporary) / "result.json"
            csv_path = Path(temporary) / "summary.csv"
            json_path.write_text("old", encoding="utf-8")
            csv_path.write_text("old", encoding="utf-8")
            app._write_json(str(json_path), {"job_id": "job-a", "value": 1})
            app._write_csv(str(csv_path), [{"point": "p0", "value": 2}])
            self.assertEqual("job-a", json.loads(json_path.read_text(encoding="utf-8"))["job_id"])
            self.assertIn("p0,2", csv_path.read_text(encoding="utf-8"))
            self.assertEqual([], list(Path(temporary).glob(".*.tmp")))

    def test_programmatic_reselection_of_current_job_does_not_poll(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.current_job_id = "job-a"
        instance.jobs_tree = mock.Mock()
        instance.jobs_tree.selection.return_value = ("job-a",)
        instance._schedule_poll = mock.Mock()
        instance._clear_loaded_result = mock.Mock()

        instance._job_selected()

        instance._schedule_poll.assert_not_called()
        instance._clear_loaded_result.assert_not_called()

    def test_terminal_result_is_requested_only_once_while_load_is_pending(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.current_job_id = "job-a"
        instance.current_job = {}
        instance.current_result_job_id = ""
        instance._result_load_pending_job_id = ""
        instance.job_progress = {}
        instance.job_status_var = FakeVariable("")
        instance.job_log = mock.Mock()
        instance._refresh_jobs = mock.Mock()
        instance._schedule_poll = mock.Mock()
        submissions = []
        instance._submit = lambda *args, **kwargs: submissions.append((args, kwargs))
        payload = {
            "id": "job-a",
            "status": "succeeded",
            "progress": 100,
            "message": "done",
            "events": [],
            "result_available": True,
        }

        instance._render_job(payload)
        instance._render_job(payload)

        self.assertEqual("job-a", instance._result_load_pending_job_id)
        self.assertEqual(1, len(submissions))
        self.assertEqual("读取结果", submissions[0][0][0])
        instance._schedule_poll.assert_not_called()

    def test_empty_plan_result_explains_no_data_without_forcing_result_tab(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.current_job_id = "job-a"
        instance._result_load_pending_job_id = "job-a"
        instance.current_result = None
        instance.current_result_job_id = ""
        instance.current_job = {"status": "succeeded"}
        instance.operating_point_box = mock.Mock()
        instance.operating_point_var = FakeVariable("")
        instance._operating_point_changed = mock.Mock()
        instance.point_box = mock.Mock()
        instance.point_var = FakeVariable("")
        instance._render_result = mock.Mock()
        instance.notebook = mock.Mock()
        instance.result_tab = object()
        instance.metrics_var = FakeVariable("")
        instance.job_status_var = FakeVariable("")
        instance.isf_chart = mock.Mock()
        instance.qk_chart = mock.Mock()
        instance.phase_noise_chart = mock.Mock()
        instance.harmonic_noise_chart = mock.Mock()

        instance._result_loaded(
            {"job_id": "job-a", "run_kind": "plan", "summary": [], "points": {}},
            "job-a",
        )

        instance.notebook.select.assert_not_called()
        instance.operating_point_box.configure.assert_called_once_with(values=[])
        self.assertFalse(hasattr(instance, "contribution_operating_box"))
        self.assertIn("未启动仿真", instance.metrics_var.get())
        self.assertIn("真实运行", instance.job_status_var.get())
        instance.isf_chart.clear.assert_called_once_with("仅生成执行计划")

    def test_clear_result_clears_unified_coordinates_and_both_source_pies(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.current_result = {"old": "result"}
        instance.current_result_job_id = "old-job"
        instance.current_result_view = {"old": "view"}
        instance._offset_point_choices = {"1000 Hz": "old-point"}
        for name in ("point_var", "operating_point_var", "result_offset_var", "noise_pie_note_var",
                     "noise_contribution_note_var", "result_quality_var", "metrics_var"):
            setattr(instance, name, FakeVariable("old"))
        for name in ("point_box", "operating_point_box", "result_offset_box", "summary_tree",
                     "harmonic_tree", "noise_contribution_tree", "metric_values_tree",
                     "simulated_noise_pie", "calculated_noise_pie", "isf_chart", "qk_chart",
                     "phase_noise_chart", "harmonic_noise_chart"):
            setattr(instance, name, mock.Mock())
        instance._clear_tree = mock.Mock()

        instance._clear_loaded_result()

        self.assertIsNone(instance.current_result)
        self.assertIsNone(instance.current_result_view)
        self.assertEqual("", instance.current_result_job_id)
        self.assertEqual({}, instance._offset_point_choices)
        for name in ("point_var", "operating_point_var", "result_offset_var"):
            self.assertEqual("", getattr(instance, name).get())
        for name in ("point_box", "operating_point_box", "result_offset_box"):
            getattr(instance, name).configure.assert_called_once_with(values=())
        instance.simulated_noise_pie.clear.assert_called_once_with()
        instance.calculated_noise_pie.clear.assert_called_once_with()
        self.assertEqual("尚无当前任务结果", instance.noise_pie_note_var.get())
        self.assertFalse(hasattr(instance, "contribution_operating_box"))
        self.assertFalse(hasattr(instance, "contribution_offset_box"))

    def test_desktop_actual_run_and_formal_signoff_are_separate_contracts(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.mode_var = FakeVariable("live")
        instance.formal_signoff_var = FakeVariable(False)
        instance._raw_from_editor = lambda: {}
        instance._collect_fields = lambda: {"dry_run": False, "allow_execute": False}

        replay = instance._config_from_ui()
        self.assertEqual("replay", replay["run_kind"])
        self.assertFalse(replay["execution"]["dry_run"])

        instance.formal_signoff_var.set(True)
        formal = instance._config_from_ui()
        self.assertEqual("signoff", formal["run_kind"])
        self.assertFalse(formal["execution"]["dry_run"])

    def test_light_and_dark_themes_expose_complete_distinct_palettes(self):
        required = {
            "bg", "surface", "surface_alt", "text", "muted", "accent",
            "accent_hover", "accent_text", "border", "selection", "success",
            "warning", "danger", "grid",
        }
        light = app.THEME_PALETTES["light"]
        dark = app.THEME_PALETTES["dark"]
        self.assertTrue(required.issubset(light))
        self.assertTrue(required.issubset(dark))
        self.assertNotEqual(light["bg"], dark["bg"])
        self.assertNotEqual(light["text"], dark["text"])
        self.assertNotEqual(light["accent"], dark["accent"])



if __name__ == "__main__":
    unittest.main()
