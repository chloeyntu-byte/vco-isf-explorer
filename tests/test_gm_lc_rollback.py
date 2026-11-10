"""Rollback contract: gm + LC only, no array activation from stale state."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from isf_tool.core.preflight import resolve_config
from isf_tool.core.service import ToolService
from isf_tool.core.thermal_budget import thermal_evidence, load_thermal_evidence
from isf_tool.core.thermal_execution import lc_passive_roots
from isf_tool.core.ui_state import strip_retired_noise_objects, save_state, load_state
from test_thermal_pipeline import thermal_fixture


class LCPassiveOwnershipTests(unittest.TestCase):
    def test_names_do_not_decide_lc_ownership(self):
        terminals = {
            "renamed_tank": {"master": "inductor"},
            "X7.cap": {"master": "capacitor"},
            "X7.series": {"master": "resistor"},
            "X7.parasitic": {"master": "inductor"},
            "I3": {"master": "capacitor"},
            "standalone_R": {"master": "resistor"},
        }
        self.assertEqual(["I3", "X7", "renamed_tank"], lc_passive_roots(terminals))

    def test_active_or_unknown_root_excludes_all_its_resistors_and_caps(self):
        for unknown in ("bsim3v3", "diode", "vsource", "bsource", "custom_unknown", ""):
            terminals = {"arbitrary.C": {"master": "capacitor"},
                         "arbitrary.R": {"master": "resistor"},
                         "arbitrary.child.leaf": {"master": unknown},
                         "tank.cap": {"master": "capacitor"},
                         "tank.r": {"master": "resistor"}}
            with self.subTest(primitive=unknown):
                self.assertEqual(["tank"], lc_passive_roots(terminals))

    def test_mixed_hierarchy_never_promotes_a_nested_passive_child(self):
        terminals = {"Xmain.child.cap": {"master": "capacitor"},
                     "Xmain.child.r": {"master": "resistor"},
                     "Xmain.mos": {"master": "bsim3v3"},
                     "Xind.L": {"master": "inductor"},
                     "Xind.R": {"master": "resistor"},
                     "Xind.K": {"master": "mutual_inductor"}}
        self.assertEqual(["Xind"], lc_passive_roots(terminals))
        self.assertEqual([], lc_passive_roots({}))


class RetiredConfigTests(unittest.TestCase):
    def legacy(self):
        return {"additional_noise_objects": [{"id": "old_array", "enabled": True}],
                "testbenches": {"vco": {"path": "/not/a/real/tb"}},
                "engine": {"additionalNoiseObjects": [{"enabled": True}],
                           "additional_noise_objects_report": {"ok": True}},
                "misc": [{"additional_noise_object_config": [{"enabled": True}], "keep": 7}]}

    def test_pure_recursive_cleaner_does_not_mutate_input(self):
        old = self.legacy()
        before = copy.deepcopy(old)
        clean = strip_retired_noise_objects(old)
        self.assertEqual(before, old)
        self.assertEqual({"testbenches": old["testbenches"], "engine": {}, "misc": [{"keep": 7}]}, clean)

    def test_service_load_save_and_snapshot_drop_removed_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = self.legacy()
            service = ToolService(workspace=directory, config=legacy)
            self.assertNotIn("additional_noise_objects", service.load_config())
            saved = service.save_config(legacy)
            self.assertNotIn("additional_noise_objects", saved["config"])
            self.assertNotIn("additional_noise_objects", json.loads(Path(saved["path"]).read_text()))
            service = ToolService(workspace=directory)
            self.assertEqual({}, service.load_config()["engine"])
            state = save_state(directory, {"name": "legacy", "config": legacy})
            loaded = load_state(directory, state["id"])["state"]["config"]
            self.assertEqual(strip_retired_noise_objects(legacy), loaded)

    def test_resolve_config_strips_legacy_controls_before_execution(self):
        raw = self.legacy()
        raw.update(source_mode="manual", execution={"dry_run": True})
        resolved = resolve_config(raw, detected={"selected": {}, "roots": []})
        self.assertNotIn("additional_noise_objects", resolved)
        self.assertEqual({}, resolved["engine"])


class RetiredBudgetTests(unittest.TestCase):
    def test_old_budgets_require_rerun_not_relabel_or_zero(self):
        for scope in ("gm_pair_and_passive_network", "gm_pair_passives_and_selected_objects"):
            old = thermal_fixture()
            old["scope"] = scope
            before = copy.deepcopy(old)
            row = {"thermal_noise": old, "hbnoise_total_pm_asd_V_rtHz": 9e-6}
            with self.subTest(scope=scope):
                result = thermal_evidence(row)
                self.assertEqual("unavailable", result["status"])
                self.assertIn("rerun", result["reason"])
                self.assertNotIn("predicted_psd_V2_Hz", result)
                self.assertEqual(before, old)
                self.assertEqual(9e-6, row["hbnoise_total_pm_asd_V_rtHz"])
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "thermal" / "budget.json"
                    path.parent.mkdir()
                    path.write_text(json.dumps(old))
                    loaded = load_thermal_evidence({"additional_noise_objects": [{"enabled": True}]}, directory, {})
                    self.assertEqual("unavailable", loaded["status"])
                    self.assertEqual(before, json.loads(path.read_text()))

    def test_current_budget_ignores_retired_config_in_analyze_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "thermal" / "budget.json"
            path.parent.mkdir()
            path.write_text(json.dumps(thermal_fixture()))
            result = load_thermal_evidence({"additional_noise_objects": [{"enabled": True}]}, directory, {})
            self.assertEqual("available", result["status"])
            self.assertEqual("gm_pair_and_lc_network", result["scope"])

    def test_object_contributions_cannot_be_relabelled_as_current_scope(self):
        for evidence in ({"object_id": "old"}, {"source_basis": "paired_noise_TB_transient_state_white_PSD"}):
            budget = thermal_fixture()
            budget["sources"][0].update(evidence)
            result = thermal_evidence({"thermal_noise": budget})
            self.assertEqual("unavailable", result["status"])
            self.assertIn("rerun", result["reason"])

    def test_lc_scope_requires_marker_and_static_source_owner(self):
        budget = thermal_fixture()
        del budget["passive_scope"]
        self.assertEqual("unavailable", thermal_evidence({"thermal_noise": budget})["status"])
        budget = thermal_fixture()
        budget["sources"][0]["source_basis"] = "VCO_static_intrinsic_PSD_at_actual_input_bands"
        for roots in (None, [], ["other_root"]):
            budget["passive_roots"] = roots
            with self.subTest(roots=roots), self.assertRaisesRegex(ValueError, "owner"):
                thermal_evidence({"thermal_noise": budget})
        budget["passive_roots"] = ["X1"]
        self.assertEqual("available", thermal_evidence({"thermal_noise": budget})["status"])


if __name__ == "__main__":
    unittest.main()
