import copy
import math
import unittest

from isf_tool.tk.charts import (
    line_segments,
    padded_bounds,
    phasor_extent,
    project_point,
    series_bounds,
)
from isf_tool.tk.viewmodel import (
    N_A,
    binding_ready,
    build_result_view,
    deep_merge,
    flatten_config,
    history_is_complete,
    invalidate_role,
    maestro_pair_payload,
    merge_config,
    select_history_candidate,
)


class ConfigViewModelTests(unittest.TestCase):
    def test_deep_merge_is_recursive_and_does_not_mutate_inputs(self):
        base = {"nested": {"keep": 1, "replace": [1, 2]}, "vendor": {"revision": 7}}
        overlay = {"nested": {"replace": [3], "new": 2}}
        original_base = copy.deepcopy(base)
        original_overlay = copy.deepcopy(overlay)

        merged = deep_merge(base, overlay)

        self.assertEqual({"keep": 1, "replace": [3], "new": 2}, merged["nested"])
        self.assertEqual({"revision": 7}, merged["vendor"])
        merged["nested"]["replace"].append(4)
        self.assertEqual(original_base, base)
        self.assertEqual(original_overlay, overlay)

    def test_flatten_and_merge_cover_run_contract_and_preserve_hidden_fields(self):
        raw = {
            "schema_version": 2,
            "source_mode": "maestro_pair",
            "project_name": "portable",
            "project_root": "/project/old",
            "discovery": {"roots": ["/project/old"], "follow_symlinks": False},
            "testbenches": {
                "vco": {
                    "name": "vco",
                    "role": "vco",
                    "path": "/maestro/vco",
                    "maestro_dir": "/maestro/vco",
                    "test": "vco_nominal",
                    "history": "/rdb/vco.rdb",
                    "binding_fingerprint": "vco-binding",
                    "history_fingerprint": "vco-history",
                    "hidden_mapping": {"output": "/OUTP"},
                },
                "replica": {
                    "name": "replica",
                    "role": "flicker",
                    "path": "/maestro/flicker",
                    "maestro_dir": "/maestro/flicker",
                    "test": "noise_nominal",
                    "history": "/rdb/flicker.rdb",
                    "binding_fingerprint": "replica-binding",
                    "history_fingerprint": "replica-history",
                },
            },
            "analysis": {
                "offset": {"value": 25.0, "unit": "kHz", "hz": 25_000.0},
                "max_harmonic": 5,
                "phase_samples": 1024,
                "period_count": 12,
            },
            "execution": {
                "dry_run": True,
                "allow_execute": False,
                "adapter_mode": "protocol",
                "adapters": {"vco": {"submit_command": ["submit", "{job_id}"]}},
            },
            "profiles": {
                "auto_detect": False,
                "pdk": "auto",
                "environment": {
                    "mode": "manual",
                    "python": "/analysis/python",
                    "executables": {"spectre": "/eda/spectre", "ocean": "/eda/ocean"},
                },
                "design": {"variant": "class-f"},
            },
            "config_path": "/configs/engine.json",
            "run_root": "/runs/private",
            "bundle_path": "/runs/canonical",
            "auto": {
                "bundle_path": "/runs/canonical",
                "vco_command": ["python", "runner.py", "vco"],
            },
            "mapping": {"custom_unit_contract": "keep-me"},
            "manual_import": {"bundle_path": "/replay/source"},
            "vendor_extension": {"revision": 7},
        }
        original = copy.deepcopy(raw)
        flat = flatten_config(raw)
        self.assertEqual("/project/old", flat["project_root"])
        self.assertEqual("/maestro/vco", flat["vco_path"])
        self.assertEqual("vco-history", flat["vco_history_fingerprint"])
        self.assertEqual("/maestro/flicker", flat["replica_path"])
        self.assertEqual("replica-history", flat["replica_history_fingerprint"])
        self.assertEqual(25_000.0, flat["offset_hz"])
        self.assertEqual(5.0, flat["max_harmonic"])
        self.assertTrue(flat["dry_run"])
        self.assertFalse(flat["allow_execute"])
        self.assertEqual("/configs/engine.json", flat["engine_config"])
        self.assertEqual("/runs/private", flat["run_root"])
        self.assertEqual("/runs/canonical", flat["bundle_path"])

        flat.update(
            {
                "project_root": "/project/new",
                "vco_path": "/new/vco",
                "vco_test": "vco_new",
                "vco_history": "/new/vco.rdb",
                "vco_binding_fingerprint": "vco-new-binding",
                "vco_history_fingerprint": "vco-new-history",
                "replica_path": "/new/flicker",
                "replica_test": "flicker_new",
                "replica_history": "/new/flicker.rdb",
                "replica_binding_fingerprint": "replica-new-binding",
                "replica_history_fingerprint": "replica-new-history",
                "offset_hz": 10_000.0,
                "offset_value": 10.0,
                "offset_unit": "kHz",
                "max_harmonic": 3,
                "dry_run": False,
                "allow_execute": True,
                "engine_config": "/configs/new-engine.json",
                "run_root": "/runs/new-private",
                "bundle_path": "/runs/new-canonical",
            }
        )
        merged = merge_config(raw, flat)

        self.assertEqual("/project/new", merged["project_root"])
        self.assertEqual(["/project/new"], merged["discovery"]["roots"])
        self.assertFalse(merged["discovery"]["follow_symlinks"])
        self.assertEqual("/new/vco", merged["testbenches"]["vco"]["path"])
        self.assertEqual("/new/vco", merged["testbenches"]["vco"]["maestro_dir"])
        self.assertEqual("vco-new-history", merged["testbenches"]["vco"]["history_fingerprint"])
        self.assertEqual("replica-new-history", merged["testbenches"]["replica"]["history_fingerprint"])
        self.assertEqual(10_000.0, merged["analysis"]["offset"]["hz"])
        self.assertEqual(3, merged["analysis"]["max_harmonic"])
        self.assertFalse(merged["execution"]["dry_run"])
        self.assertTrue(merged["execution"]["allow_execute"])
        self.assertEqual("/configs/new-engine.json", merged["config_path"])
        self.assertEqual("/runs/new-private", merged["run_root"])
        self.assertEqual("/runs/new-canonical", merged["bundle_path"])
        self.assertEqual("/runs/new-canonical", merged["auto"]["bundle_path"])

        self.assertEqual("keep-me", merged["mapping"]["custom_unit_contract"])
        self.assertEqual(7, merged["vendor_extension"]["revision"])
        self.assertFalse(merged["profiles"]["auto_detect"])
        self.assertEqual("auto", merged["profiles"]["pdk"])
        self.assertEqual("class-f", merged["profiles"]["design"]["variant"])
        self.assertEqual("/eda/ocean", merged["profiles"]["environment"]["executables"]["ocean"])
        self.assertEqual(["submit", "{job_id}"], merged["execution"]["adapters"]["vco"]["submit_command"])
        self.assertEqual(["python", "runner.py", "vco"], merged["auto"]["vco_command"])
        self.assertEqual("/replay/source", merged["manual_import"]["bundle_path"])
        self.assertEqual(original, raw)

    def test_zero_offset_is_not_treated_as_missing(self):
        raw = {"analysis": {"offset": {"value": 0.0, "unit": "Hz", "hz": 0.0}}}
        flat = flatten_config(raw)
        self.assertEqual(0.0, flat["offset_hz"])
        self.assertEqual(0.0, flat["offset_value"])
        merged = merge_config(raw, {"offset_hz": 0.0})
        self.assertEqual(0.0, merged["analysis"]["offset"]["hz"])
        self.assertEqual(0.0, merged["analysis"]["offset"]["value"])

    def test_invalid_boolean_is_rejected_instead_of_becoming_truthy(self):
        with self.assertRaises(ValueError):
            merge_config({}, {"dry_run": "false"})

    def test_multiple_discovery_roots_survive_a_noop_form_round_trip(self):
        raw = {
            "discovery": {
                "roots": ["/projects/one", "/projects/two"],
                "max_depth": 4,
            }
        }
        flat = flatten_config(raw)
        self.assertEqual("", flat["project_root"])

        merged = merge_config(raw, flat)

        self.assertEqual(["/projects/one", "/projects/two"], merged["discovery"]["roots"])
        self.assertNotIn("project_root", merged)

        changed = merge_config(raw, {"project_root": "/projects/three"})
        self.assertEqual("/projects/three", changed["project_root"])
        self.assertEqual(["/projects/three"], changed["discovery"]["roots"])

        single = {"discovery": {"roots": ["/projects/only"]}}
        self.assertEqual("/projects/only", flatten_config(single)["project_root"])
        self.assertEqual(
            ["/projects/only"],
            merge_config(single, flatten_config(single))["discovery"]["roots"],
        )

    def test_single_project_root_can_still_be_cleared_explicitly(self):
        raw = {
            "project_root": "/single",
            "discovery": {"roots": ["/single"], "follow_symlinks": False},
        }
        merged = merge_config(raw, {"project_root": ""})
        self.assertEqual("", merged["project_root"])
        self.assertEqual([], merged["discovery"]["roots"])
        self.assertFalse(merged["discovery"]["follow_symlinks"])

    def test_legacy_web_top_level_fields_round_trip_without_empty_canonical_overrides(self):
        raw = {
            "main_tb": "/legacy/vco",
            "main_test": "vco_test",
            "main_history": "/legacy/vco.rdb",
            "main_fingerprint": "vco-binding",
            "main_history_fingerprint": "vco-history",
            "replica_tb": "/legacy/flicker",
            "replica_test": "flicker_test",
            "replica_history": "/legacy/flicker.rdb",
            "replica_fingerprint": "replica-binding",
            "replica_history_fingerprint": "replica-history",
            "engine_config_path": "/legacy/engine.json",
            "auto": {"run_root": "/legacy/run"},
        }

        flat = flatten_config(raw)

        self.assertEqual("/legacy/vco", flat["vco_path"])
        self.assertEqual("vco_test", flat["vco_test"])
        self.assertEqual("/legacy/vco.rdb", flat["vco_history"])
        self.assertEqual("vco-binding", flat["vco_binding_fingerprint"])
        self.assertEqual("vco-history", flat["vco_history_fingerprint"])
        self.assertEqual("/legacy/flicker", flat["replica_path"])
        self.assertEqual("flicker_test", flat["replica_test"])
        self.assertEqual("/legacy/flicker.rdb", flat["replica_history"])
        self.assertEqual("replica-binding", flat["replica_binding_fingerprint"])
        self.assertEqual("replica-history", flat["replica_history_fingerprint"])
        self.assertEqual("/legacy/engine.json", flat["engine_config"])
        self.assertEqual("/legacy/run", flat["run_root"])

        merged = merge_config(raw, flat)
        self.assertEqual("/legacy/vco", merged["main_tb"])
        self.assertEqual("/legacy/flicker", merged["replica_tb"])
        self.assertEqual("/legacy/vco", merged["testbenches"]["vco"]["path"])
        self.assertEqual("vco-binding", merged["testbenches"]["vco"]["binding_fingerprint"])
        self.assertEqual("/legacy/flicker", merged["testbenches"]["replica"]["path"])
        self.assertEqual(
            "replica-history",
            merged["testbenches"]["replica"]["history_fingerprint"],
        )
        self.assertNotIn("device", merged["testbenches"]["vco"])
        self.assertNotIn("device", merged["testbenches"]["replica"])
        self.assertEqual("/legacy/engine.json", merged["config_path"])
        self.assertEqual("/legacy/run", merged["run_root"])
        self.assertNotIn("bundle_path", merged)

    def test_invalidate_role_clears_both_fingerprints_only_on_copy(self):
        raw = {
            "testbenches": {
                "vco": {
                    "binding_fingerprint": "vco-binding",
                    "history_fingerprint": "vco-history",
                },
                "replica": {
                    "binding_fingerprint": "replica-binding",
                    "history_fingerprint": "replica-history",
                },
            }
        }
        changed = invalidate_role(raw, "vco")
        self.assertEqual("", changed["testbenches"]["vco"]["binding_fingerprint"])
        self.assertEqual("", changed["testbenches"]["vco"]["history_fingerprint"])
        self.assertEqual("replica-binding", changed["testbenches"]["replica"]["binding_fingerprint"])
        self.assertEqual("vco-binding", raw["testbenches"]["vco"]["binding_fingerprint"])


class HistorySelectionTests(unittest.TestCase):
    def setUp(self):
        self.first = {
            "name": "Interactive.0",
            "path": "/rdb/Interactive.0.rdb",
            "fully_complete": True,
            "complete_runs": 2,
            "checked_runs": 2,
            "tests": ["vco_nominal"],
        }
        self.second = {
            "name": "Interactive.1",
            "path": "/rdb/Interactive.1.rdb",
            "fully_complete": True,
            "complete_runs": 3,
            "checked_runs": 3,
            "tests": [{"name": "vco_nominal"}],
        }
        self.incomplete = {
            "name": "Interactive.2",
            "path": "/rdb/Interactive.2.rdb",
            "complete_runs": 1,
            "checked_runs": 2,
            "source_netlists": [{}],
            "tests": ["vco_nominal"],
        }

    def test_only_unique_complete_history_is_auto_selected(self):
        choice = select_history_candidate([self.first, self.incomplete], test_name="vco_nominal")
        self.assertEqual(self.first["path"], choice["selected"]["path"])
        self.assertEqual(1, choice["complete_count"])
        self.assertFalse(choice["requires_manual"])

        choice = select_history_candidate([self.first, self.second], test_name="vco_nominal")
        self.assertIsNone(choice["selected"])
        self.assertEqual(2, choice["complete_count"])
        self.assertTrue(choice["requires_manual"])

    def test_requested_complete_history_is_respected(self):
        choice = select_history_candidate(
            [self.first, self.second], requested=self.second["path"], test_name="vco_nominal"
        )
        self.assertEqual(self.second["path"], choice["selected"]["path"])
        self.assertFalse(choice["requires_manual"])

    def test_stale_requested_history_never_becomes_another_history(self):
        choice = select_history_candidate(
            [self.first, self.incomplete],
            requested="/rdb/removed.rdb",
            test_name="vco_nominal",
        )
        self.assertIsNone(choice["selected"])
        self.assertFalse(choice["requires_manual"])

        ambiguous = select_history_candidate(
            [self.first, self.second],
            requested="/rdb/removed.rdb",
            test_name="vco_nominal",
        )
        self.assertIsNone(ambiguous["selected"])
        self.assertTrue(ambiguous["requires_manual"])

    def test_wrong_test_or_incomplete_history_is_never_bindable(self):
        self.assertFalse(history_is_complete(self.first, "different_test"))
        self.assertFalse(history_is_complete(self.incomplete, "vco_nominal"))
        choice = select_history_candidate([self.first], test_name="different_test")
        self.assertIsNone(choice["selected"])
        self.assertEqual(0, choice["complete_count"])

    def test_pair_payload_and_binding_gate_keep_both_fingerprints(self):
        flat = {
            "project_root": "/project",
            "vco_path": "/maestro/vco",
            "vco_test": "vco_nominal",
            "vco_history": "/rdb/vco.rdb",
            "vco_binding_fingerprint": "vco-binding",
            "vco_history_fingerprint": "vco-history",
            "replica_path": "/maestro/flicker",
            "replica_test": "flicker_nominal",
            "replica_history": "/rdb/flicker.rdb",
            "replica_binding_fingerprint": "replica-binding",
            "replica_history_fingerprint": "replica-history",
            "offset_hz": 10_000.0,
        }
        self.assertTrue(binding_ready(flat))
        payload = maestro_pair_payload(flat, include_fingerprints=True)
        self.assertEqual("vco-history", payload["testbenches"]["vco"]["history_fingerprint"])
        self.assertEqual("replica-history", payload["testbenches"]["replica"]["history_fingerprint"])
        self.assertEqual(10_000.0, payload["offset_hz"])


class ResultViewModelTests(unittest.TestCase):
    def test_missing_result_values_are_na_and_never_zero_filled(self):
        result = {
            "summary": [
                {
                    "point": "p0",
                    "f0_Hz": 0,
                    "offset_Hz": 0,
                    "dynamic_error_dB": None,
                }
            ],
            "points": {
                "p0": {
                    "workpoint": [
                        {"wt_rad": 0.0, "isf": None},
                        {"wt_rad": math.pi, "isf": 2.0},
                    ],
                    "harmonics": [{"k": 0, "Mdc_mag_A_rtHz": None}],
                }
            },
        }
        view = build_result_view(result, "job-0", config={"analysis": {"offset": {"hz": 0.0}}})
        self.assertEqual(0.0, view["metrics"]["frequency_hz"])
        self.assertEqual(N_A, view["metrics"]["period_s"])
        self.assertEqual(0.0, view["metrics"]["offset_hz"])
        self.assertEqual(N_A, view["metrics"]["dynamic_error_db"])
        self.assertEqual(N_A, view["metrics"]["mdc"])
        self.assertEqual(N_A, view["harmonic_rows"][0]["q_mag"])
        self.assertEqual([], view["qk_points"])
        self.assertEqual([None, (0.5, 2.0)], view["isf_series"][0]["points"])

    def test_explicit_point_without_matching_summary_never_uses_another_point(self):
        result = {
            "summary": [{"point": "p0", "f0_Hz": 5.0e9, "dynamic_error_dB": 0.2}],
            "points": {
                "p0": {"workpoint": [], "harmonics": []},
                "p1": {"workpoint": [], "harmonics": []},
            },
        }

        explicit = build_result_view(result, point="p1")
        self.assertEqual("p1", explicit["point"])
        self.assertEqual({}, explicit["selected_summary"])
        self.assertEqual(N_A, explicit["metrics"]["frequency_hz"])
        self.assertEqual(N_A, explicit["metrics"]["dynamic_error_db"])

        missing = build_result_view(result, point="removed")
        self.assertEqual(N_A, missing["point"])
        self.assertEqual({}, missing["selected_summary"])
        self.assertEqual(N_A, missing["metrics"]["frequency_hz"])

        defaulted = build_result_view(result)
        self.assertEqual("p0", defaulted["point"])
        self.assertEqual(5.0e9, defaulted["metrics"]["frequency_hz"])

    def test_offset_uses_owned_result_evidence_not_current_workspace_config(self):
        result = {
            "summary": [{"point": "p0"}],
            "points": {"p0": {"workpoint": [], "harmonics": []}},
        }
        current_workspace = {"analysis": {"offset": {"hz": 99_000.0}}}
        self.assertEqual(
            N_A,
            build_result_view(result, point="p0", config=current_workspace)["metrics"]["offset_hz"],
        )

        with_snapshot = copy.deepcopy(result)
        with_snapshot["job_snapshot"] = {
            "config": {"analysis": {"offset": {"value": 25.0, "unit": "kHz"}}}
        }
        self.assertEqual(
            25_000.0,
            build_result_view(with_snapshot, point="p0", config=current_workspace)["metrics"]["offset_hz"],
        )

    def test_phase_series_preserves_missing_samples_as_line_breaks(self):
        result = {
            "summary": [{"point": "p0"}],
            "points": {
                "p0": {
                    "workpoint": [
                        {"wt_rad": 0.0, "isf": 1.0},
                        {"wt_rad": math.pi, "isf": None},
                        {"wt_rad": 2.0 * math.pi, "isf": -1.0},
                    ],
                    "harmonics": [],
                }
            },
        }
        points = build_result_view(result, point="p0")["isf_series"][0]["points"]
        self.assertEqual([(0.0, 1.0), None, (1.0, -1.0)], points)
        self.assertEqual([[(0.0, 1.0)], [(1.0, -1.0)]], line_segments(points))

    def test_canonical_contract_maps_summary_point_isf_and_qk(self):
        result = {
            "summary": [
                {
                    "point": "p0",
                    "f0_Hz": "5.1e9",
                    "dynamic_error_dB": "-0.25",
                    "pair_error_dB": "0.4",
                    "spectre_fn_pm_asd_V_rtHz": "1e-5",
                },
                {"point": "p1", "f0_Hz": "5.2e9", "dynamic_error_dB": "0.1"},
            ],
            "points": {
                "p0": {
                    "workpoint": [
                        {"wt_rad": 0.0, "isf": 1.0},
                        {"wt_rad": math.pi, "isf": -0.5},
                        {"wt_rad": 2 * math.pi, "isf": 1.0},
                    ],
                    "harmonics": [
                        {
                            "k": "0",
                            "Mdc_mag_A_rtHz": "2e-10",
                            "Mdyn_mag_A_rtHz": "2.2e-10",
                            "Qk_real_V_rtHz": "1e-6",
                            "Qk_imag_V_rtHz": "2e-6",
                            "cum_real_V_rtHz": "1e-6",
                            "cum_imag_V_rtHz": "2e-6",
                        },
                        {
                            "k": "1",
                            "Mdc_mag_A_rtHz": "1e-10",
                            "Mdyn_mag_A_rtHz": "0.9e-10",
                            "Qk_real_V_rtHz": "2e-6",
                            "Qk_imag_V_rtHz": "-1e-6",
                            "cum_real_V_rtHz": "3e-6",
                            "cum_imag_V_rtHz": "1e-6",
                        },
                    ],
                },
                "p1": {"workpoint": [], "harmonics": []},
            },
        }
        view = build_result_view(result, "canonical-job", point="p0")

        self.assertEqual(2, len(view["summary_rows"]))
        self.assertEqual("p0", view["point"])
        self.assertEqual(2, len(view["harmonic_rows"]))
        self.assertEqual(2, len(view["qk_points"]))
        self.assertEqual(1, len(view["isf_series"]))
        self.assertEqual(3, len(view["isf_series"][0]["points"]))
        self.assertNotEqual(N_A, view["metrics"]["frequency_hz"])
        self.assertNotEqual(N_A, view["metrics"]["dynamic_error_db"])
        self.assertNotEqual(N_A, view["metrics"]["qsum_real"])
        self.assertFalse(view["signoff"])
        self.assertEqual("replay", view["run_kind"])

    def test_signoff_requires_owned_provenance_not_metadata_claim(self):
        untrusted = {
            "run_kind": "signoff",
            "dual_tb": {"signoff": True, "metadata": {"signoff": True}},
            "summary": [{"point": "p0"}],
            "points": {"p0": {"workpoint": [], "harmonics": []}},
        }
        self.assertFalse(build_result_view(untrusted, "job-1")["signoff"])

        trusted = copy.deepcopy(untrusted)
        trusted["provenance"] = {
            "job_id": "job-1",
            "manifest_sha256": "a" * 64,
            "psf_runs": 2,
        }
        view = build_result_view(trusted, "job-1")
        self.assertTrue(view["signoff"])
        self.assertEqual("signoff", view["run_kind"])


class CanvasMathTests(unittest.TestCase):
    def test_line_math_splits_missing_samples_and_projects_bounds(self):
        segments = line_segments([(0, 1), (1, 2), (2, None), (3, 4), (4, float("nan"))])
        self.assertEqual([[(0.0, 1.0), (1.0, 2.0)], [(3.0, 4.0)]], segments)
        bounds = series_bounds([{"points": [(0, 1), (1, 2)]}, {"points": [(2, -1)]}])
        self.assertEqual((0.0, 2.0, -1.0, 2.0), bounds)
        padded = padded_bounds(bounds, ratio=0.0)
        self.assertEqual((50.0, 50.0), project_point((1.0, 0.5), padded, (0, 0, 100, 100)))

    def test_phasor_extent_is_symmetric_and_nonzero(self):
        self.assertEqual(1.15, phasor_extent([]))
        self.assertAlmostEqual(4.6, phasor_extent([{"real": -4, "imag": 2}]))
        self.assertEqual(1.15, phasor_extent([{"real": N_A, "imag": 0}]))


if __name__ == "__main__":
    unittest.main()
