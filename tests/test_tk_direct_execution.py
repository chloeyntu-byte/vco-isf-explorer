"""Explicit desktop actions authorize one request, never a saved binding."""

import copy
import unittest
from unittest import mock

from isf_tool.tk.app import IsfApplication
from test_tk_launch import FakeRoot, FakeVariable


class DirectExecutionTests(unittest.TestCase):
    def setUp(self):
        self.app = IsfApplication.__new__(IsfApplication)
        self.app.root = FakeRoot()
        self.base = {"schema_version": 2, "run_kind": "replay", "execution": {
            "dry_run": False, "allow_execute": False, "adapter_mode": "bundled"},
            "analysis": {"offset_Hz": 10000}}
        self.app._config_from_ui = lambda: self.base
        self.app._close_check_pending = False
        self.app._job_command_pending = False
        self.app.controller = mock.Mock()
        self.app.controller.jobs.return_value = {"jobs": []}
        self.app.controller.preflight.return_value = {"ok": True}
        self.app._render_preflight = mock.Mock()
        self.app._run_started = mock.Mock()
        self.app.preflight_status_var = FakeVariable()

        def immediate(_label, function, success=None, **_kwargs):
            value = function()
            if success:
                success(value)
        self.app._submit = immediate

    def test_active_preflight_authorizes_only_the_copied_request(self):
        before = copy.deepcopy(self.base)
        self.app._preflight()
        submitted = self.app.controller.preflight.call_args.args[0]
        self.assertTrue(submitted["execution"]["allow_execute"])
        self.assertFalse(submitted["execution"]["dry_run"])
        self.assertEqual(before, self.base)
        self.app._render_preflight.assert_called_once_with({"ok": True})
        self.app.controller.start_run.assert_not_called()

    def test_start_uses_the_same_ephemeral_payload_without_confirmation(self):
        before = copy.deepcopy(self.base)
        with mock.patch("isf_tool.tk.app.messagebox.askyesno") as confirmation:
            self.app._start_run()
        confirmation.assert_not_called()
        submitted = self.app.controller.start_run.call_args.args[0]
        self.assertEqual(self.app.controller.preflight.call_args.args[0], submitted)
        self.assertTrue(submitted["execution"]["allow_execute"])
        self.assertFalse(submitted["execution"]["dry_run"])
        self.assertEqual(before, self.base)

    def test_async_guard_ignores_only_consent_and_does_not_mutate_either_input(self):
        submitted = self.app._execution_config_from_ui()
        before = copy.deepcopy(submitted)
        self.assertTrue(self.app._config_still_current(submitted))
        self.assertEqual(before, submitted)
        self.assertFalse(self.base["execution"]["allow_execute"])
        self.base["analysis"]["offset_Hz"] = 20000
        self.assertFalse(self.app._config_still_current(submitted))
        self.base["analysis"]["offset_Hz"] = 10000
        self.base["execution"]["adapter_mode"] = "protocol"
        self.assertFalse(self.app._config_still_current(submitted))

    def test_binding_and_save_fields_never_grant_execution(self):
        self.app.vars = {"project_name": FakeVariable("test")}
        self.app.mode_var = FakeVariable("dry")  # obsolete imported value
        self.app.allow_execute_var = FakeVariable(True)
        fields = self.app._collect_fields()
        self.assertFalse(fields["dry_run"])
        self.assertFalse(fields["allow_execute"])

    def test_raw_editor_does_not_retain_imported_authorization(self):
        self.app.raw_text = mock.Mock()
        value = self.app._execution_config_from_ui()
        self.app._set_raw_text(value)
        import json
        displayed = json.loads(self.app.raw_text.insert.call_args.args[1])
        self.assertFalse(displayed["execution"]["allow_execute"])
        self.assertTrue(value["execution"]["allow_execute"])


if __name__ == "__main__":
    unittest.main()
