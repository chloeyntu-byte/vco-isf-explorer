"""Source-scoped menu preferences do not select a history or grant execution."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from isf_tool.tk.app import IsfApplication
from isf_tool.tk.preferences import mapping_key, preference_path, read_preferences, write_preferences
from tests.test_tk_maestro_context import context


class MaestroPreferencesTests(unittest.TestCase):
    def test_store_is_private_source_scoped_and_rejects_stale_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "preferences"
            source = context()
            path = preference_path(directory, source)
            self.assertEqual(path, preference_path(directory, context("OtherHistory")))
            self.assertNotEqual(path, preference_path(directory, {**source, "work_dir": "/other"}))
            self.assertEqual({}, read_preferences(path))
            result = write_preferences(path, {
                "fields": {"offset_mode": "single", "offset_value": 17, "allow_execute": True,
                           "vco_history": "/private/history"},
                "theme": "dark",
                "allow_execute": True,
            }, None)
            loaded = read_preferences(path)
            self.assertEqual({"offset_mode": "single", "offset_value": 17}, loaded["fields"])
            self.assertEqual("dark", loaded["theme"])
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            with self.assertRaisesRegex(ValueError, "another window"):
                write_preferences(path, {"theme": "light"}, None)
            self.assertEqual(loaded, read_preferences(path))
            self.assertEqual("light", write_preferences(path, {"theme": "light"}, result["revision"])["theme"])

    def test_mapping_revalidates_once_and_only_for_matching_binding_and_history(self):
        application = IsfApplication.__new__(IsfApplication)
        application.maestro_context = context()
        application._context_mode = True
        application._preferences_path = Path("/fixture/preference.json")
        application._mapping_overrides = {"vco": {}}
        application._restored_mapping_keys = set()
        application._mapping_changed = mock.Mock()
        application.binding_status_var = mock.Mock()
        application._inspect_pair = mock.Mock()
        role = {"maestro_dir": context()["maestro_dir"],
                "binding_fingerprint": "a" * 64, "history_fingerprint": "b" * 64}
        application._preferences = {"bindings": {mapping_key(role): {"target_instance": "M1"}}}
        self.assertFalse(application._restore_project_mapping({**role, "history_fingerprint": "c" * 64}))
        self.assertFalse(application._restore_project_mapping({**role, "maestro_dir": "/other"}))
        self.assertTrue(application._restore_project_mapping(role))
        self.assertEqual({"target_instance": "M1"}, application._mapping_overrides["vco"])
        application._mapping_overrides["vco"].clear()
        self.assertFalse(application._restore_project_mapping(role))
        application._inspect_pair.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
