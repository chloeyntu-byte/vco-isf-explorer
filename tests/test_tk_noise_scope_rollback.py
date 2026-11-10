"""Retired array controls cannot return through an old Tk config or state."""

import copy
import os
from pathlib import Path
import tkinter as tk
import unittest
from unittest import mock

from isf_tool.tk.app import IsfApplication
from isf_tool.tk.viewmodel import flatten_config, maestro_pair_payload, merge_config, result_quality_text


def old_config():
    return {
        "testbenches": {"vco": {"mapping_overrides": {"target_instance": "MOS_MAIN"}}, "replica": {}},
        "analysis": {"offsets": {"mode": "list", "values": "1k,1M"}, "max_harmonic": 3},
        "additional_noise_objects": [{"id": "bank", "enabled": True}],
        "additionalNoiseObjects": [{"id": "alias", "enabled": True}],
        "auto": {"additional_noise_object_config": [{"enabled": True}], "timeout_s": 180},
    }


class NoiseScopeRollbackTests(unittest.TestCase):
    def test_config_and_pair_round_trip_drop_retired_fields_without_mutating_input(self):
        config = old_config()
        before = copy.deepcopy(config)
        flat = flatten_config(config)
        merged = merge_config(config, flat)
        self.assertEqual(before, config)
        for result in (flat, merged, maestro_pair_payload(merged)):
            self.assertNotIn("additional_noise_objects", result)
            self.assertNotIn("additionalNoiseObjects", result)
        self.assertNotIn("additional_noise_object_config", merged["auto"])
        self.assertEqual(180, merged["auto"]["timeout_s"])
        self.assertEqual("MOS_MAIN", merged["testbenches"]["vco"]["mapping_overrides"]["target_instance"])
        self.assertEqual(3, merged["analysis"]["max_harmonic"])

    def test_legacy_array_budget_does_not_claim_available_thermal(self):
        for scope in ("gm_pair_and_passive_network", "gm_pair_passives_and_selected_objects"):
            summary = {"thermal_noise": {"schema_version": 1, "status": "available", "scope": scope,
                       "noise_quantity": "PM_SSB", "pm_validation": {"fixture": "synthetic"},
                       "phase_noise_dBc_Hz": -100, "additional_noise_objects": [{"id": "bank", "status": "available"}]}}
            text = result_quality_text(summary)
            self.assertNotIn("已匹配附加对象", text)
            self.assertNotIn("Thermal：已验证", text)
            self.assertIn("Thermal 未完成", text)

    def test_native_source_has_no_object_editor(self):
        path = Path(__file__).parents[1] / "isf_tool" / "tk" / "app.py"
        source = path.read_text(encoding="utf-8")
        for marker in ("_build_noise_objects_tab", "_add_noise_object", "noise_object_tree", "附加噪声对象（可选）"):
            self.assertNotIn(marker, source)


@unittest.skipUnless(os.environ.get("ISF_GUI_TEST") == "1", "requires explicitly selected GUI display")
class NativeRollbackGuiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.controller = mock.Mock(workspace=Path("/tmp/isolated-noise-rollback-ui"))
        with mock.patch.object(IsfApplication, "_submit"), mock.patch.object(IsfApplication, "_refresh_jobs"):
            self.app = IsfApplication(self.root, self.controller)

    def tearDown(self):
        self.app.queue.close()
        self.root.update_idletasks()
        self.root.destroy()

    def test_old_config_cannot_recreate_editor_or_execution_mapping(self):
        self.app._config_loaded(old_config())
        self.assertNotIn("additional_noise_objects", self.app.raw_text.get("1.0", "end"))
        self.assertFalse(hasattr(self.app, "noise_objects_tab"))
        self.assertFalse(hasattr(self.app, "noise_object_tree"))
        config = self.app._execution_config_from_ui()
        self.assertNotIn("additional_noise_objects", config)
        self.assertNotIn("additionalNoiseObjects", config)
        self.assertNotIn("additional_noise_object_config", config["auto"])
        self.assertEqual("MOS_MAIN", config["testbenches"]["vco"]["mapping_overrides"]["target_instance"])
        self.assertTrue(hasattr(self.app, "save_state_button"))
        self.assertTrue(hasattr(self.app, "simulated_noise_pie"))
        self.assertEqual(4, len(self.app._page_canvases))


if __name__ == "__main__":
    unittest.main()
