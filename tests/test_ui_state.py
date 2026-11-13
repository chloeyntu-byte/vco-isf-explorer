import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from isf_tool.core import ui_state
from isf_tool.core.service import ToolService


class UiStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.payload = {"name": "测试状态", "config": {"execution": {"allow_execute": True},
            "nested": [{"allowExecute": True}], "analysis": {"max_harmonic": 3}},
            "view": {"job_id": "saved-job", "point": "corner@1000", "noise_kind": "both"}}

    def test_roundtrip_default_directory_no_authority_or_mutation(self):
        before = copy.deepcopy(self.payload)
        saved = ui_state.save_state(self.workspace, self.payload)
        self.assertEqual(self.payload, before)
        self.assertEqual(Path(saved["path"]).parent, self.workspace / "states")
        self.assertEqual(0o600, Path(saved["path"]).stat().st_mode & 0o777)
        loaded = ui_state.load_state(self.workspace, saved["id"])["state"]
        self.assertFalse(loaded["config"]["execution"]["allow_execute"])
        self.assertFalse(loaded["config"]["nested"][0]["allowExecute"])
        self.assertEqual(loaded["view"], self.payload["view"])
        self.assertEqual(ui_state.list_states(self.workspace)["states"][0]["id"], saved["id"])

    def test_old_harmonic_labels_load_as_stable_keys_without_mutating_input(self):
        cases = dict(ui_state.LEGACY_HARMONIC_CURVES)
        cases["累计至 k=2 · 相干总和"] = "cumulative_2"
        cases.update({"coherent_total": "coherent_total", "cumulative_3": "cumulative_3",
                      "extension_curve": "extension_curve"})
        for old, expected in cases.items():
            with self.subTest(label=old):
                saved = ui_state.save_state(self.workspace, self.payload)
                legacy = copy.deepcopy(saved["state"])
                legacy["view"]["harmonic_curve"] = old
                Path(saved["path"]).write_text(json.dumps(legacy), encoding="utf-8")
                before = copy.deepcopy(legacy)
                loaded = ui_state.load_state(self.workspace, saved["id"])["state"]
                self.assertEqual(expected, loaded["view"]["harmonic_curve"])
                self.assertEqual(before, legacy)
                self.assertEqual(self.payload["view"]["point"], loaded["view"]["point"])
                self.assertFalse(loaded["config"]["execution"]["allow_execute"])

    def test_same_name_never_overwrites_previous_state(self):
        first = ui_state.save_state(self.workspace, self.payload)
        original = Path(first["path"]).read_bytes()
        second = ui_state.save_state(self.workspace, self.payload)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(Path(first["path"]).read_bytes(), original)

    def test_loading_revokes_binding_confirmation_but_retains_manual_mapping(self):
        config = self.payload["config"]
        config["testbenches"] = {"vco": {"binding_fingerprint": "old", "history_fingerprint": "oldhistory",
            "history_name": "Interactive.2", "mapping_overrides": {"output_pos": "/OUTP"}}}
        config["mainFingerprint"] = "legacy"
        saved = ui_state.save_state(self.workspace, self.payload)
        restored = ui_state.load_state(self.workspace, saved["id"])["state"]["config"]
        self.assertEqual(restored["testbenches"]["vco"]["binding_fingerprint"], "")
        self.assertEqual(restored["testbenches"]["vco"]["history_fingerprint"], "")
        self.assertEqual(restored["mainFingerprint"], "")
        self.assertEqual(restored["testbenches"]["vco"]["mapping_overrides"], {"output_pos": "/OUTP"})
        self.assertEqual(restored["testbenches"]["vco"]["history_name"], "Interactive.2")
        self.assertEqual(config["mainFingerprint"], "legacy")

    def test_traversal_and_symlink_directory_or_file_are_rejected(self):
        for value in ("../config.json", "/config.json", "bad.json", None):
            with self.assertRaises(ValueError):
                ui_state.load_state(self.workspace, value)
        target = self.workspace / "elsewhere"
        target.mkdir()
        states = self.workspace / "states"
        states.symlink_to(target)
        with self.assertRaises(ValueError):
            ui_state.save_state(self.workspace, self.payload)
        states.unlink()
        saved = ui_state.save_state(self.workspace, self.payload)
        path = Path(saved["path"])
        data = path.read_bytes()
        path.unlink()
        external = target / "state.json"
        external.write_bytes(data)
        path.symlink_to(external)
        with self.assertRaises(ValueError):
            ui_state.load_state(self.workspace, saved["id"])

    def test_fifo_and_corrupt_file_do_not_block_list_or_loading(self):
        states = self.workspace / "states"
        states.mkdir()
        fifo = states / "20260921-120000-0123456789ab.json"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            ui_state.load_state(self.workspace, fifo.name)
        fifo.unlink()
        fifo.write_text("bad")
        result = ui_state.list_states(self.workspace)
        self.assertEqual([], result["states"])
        self.assertEqual(1, len(result["warnings"]))

    def test_invalid_schema_fields_nonfinite_and_oversize_rejected(self):
        for payload in (dict(self.payload, path="arbitrary.json"),
                        dict(self.payload, view={"not_a_preference": True}),
                        dict(self.payload, view={"offset_hz": float("nan")}),
                        dict(self.payload, config={"blob": "x" * ui_state.MAX_STATE_BYTES})):
            with self.assertRaises(ValueError):
                ui_state.save_state(self.workspace, payload)
        saved = ui_state.save_state(self.workspace, self.payload)
        state = saved["state"]
        state["schema_version"] = 2
        Path(saved["path"]).write_text(json.dumps(state))
        with self.assertRaises(ValueError):
            ui_state.load_state(self.workspace, saved["id"])

    def test_service_state_load_never_applies_config_or_runs_probes(self):
        service = ToolService(workspace=str(self.workspace))
        before = service.load_config()
        with mock.patch.object(service, "start_run", side_effect=AssertionError("must not run")), \
             mock.patch.object(service, "preflight", side_effect=AssertionError("must not inspect")):
            saved = service.save_state(self.payload)
            service.load_state({"id": saved["id"]})
        self.assertEqual(before, service.load_config())
        self.assertFalse((self.workspace / "config.json").exists())


if __name__ == "__main__":
    unittest.main()
