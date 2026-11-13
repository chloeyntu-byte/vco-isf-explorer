"""Portable names must reach private Spectre preparation, not just UI text."""
import copy
import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from isf_tool.core.signal_mapping import discover_signal_mapping
from isf_tool.core.portable_setup import build_portable_setup, engine_content_sha256
from isf_tool.core.physical_profile import inspect_engine_contract
from isf_tool.core.service import _deep_merge, ToolService, _file_sha256
from isf_tool.core.preflight import resolve_config, _runtime_identity
from isf_tool.core.errors import ResumeError
from isf_tool.core.run_state import STATE_FILENAME
from isf_tool.core.waveform_exchange import inspect_waveform_exchange


ROOT = Path(__file__).resolve().parents[1]


class PortableSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.model = self.root / "portable_model.scs"
        self.model.write_text("section cornerA\nsubckt portable_mos (d g s b)\ncoreMOS (di gi si bi) mos_primitive l=l w=w\nends portable_mos\nendsection cornerA\n")
        self.vco = self.root / "main" / "netlist" / "input.scs"
        self.replica = self.root / "replica" / "netlist" / "input.scs"
        self.vco.parent.mkdir(parents=True)
        self.replica.parent.mkdir(parents=True)
        self.vco.write_text('simulator lang=spectre\ninclude "' + str(self.model) + '" section=cornerA\n'
            'DUT (VOUT_P GATE_A COMMON COMMON) portable_mos l=1u w=4u\n'
            'PARTNER (VOUT_N GATE_B COMMON COMMON) portable_mos l=1u w=4u\n'
            'hb (VOUT_P VOUT_N) hb fundfreqs=[1G] maxharms=[12] annotate=status\n'
            'hbnoise (VOUT_P VOUT_N) hbnoise start=1k stop=100k annotate=status\n')
        self.replica.write_text('simulator lang=spectre\ninclude "' + str(self.model) + '" section=cornerA\n'
            'parameters G_BIAS=0.6 D_BIAS=0.8\n'
            'NOISE_DUT (D_INT G_INT VSS VSS) portable_mos l=1u w=4u\n'
            'D_SOURCE (D_EXT VSS) vsource dc=D_BIAS type=dc\n'
            'G_SOURCE (G_INT VSS) vsource dc=G_BIAS type=dc\n'
            'READ_CURRENT (D_EXT D_INT) vsource dc=0 type=dc\n'
            'TO_VOLTAGE (i_out VSS) ccvs rm=1 probe=READ_CURRENT\n'
            'noise (i_out VSS) noise start=1k stop=100k annotate=status\n')
        self.environment = {"executables": {"spectre": "/bin/true", "psf": "/bin/true"}}
        self.config = {"analysis": {"offset": {"hz": 10000.0}, "max_harmonic": 3}}

    def tearDown(self):
        self.temp.cleanup()

    def reports(self, vco_overrides=None, replica_overrides=None):
        model = [{"path": str(self.model), "section": "cornerA"}]
        vco_test = {"name": "vco", "models": model, "variables": [], "analyses": [
            {"name": "hb", "enabled": True, "fields": {"p": "VOUT_P", "n": "VOUT_N"}},
            {"name": "hbnoise", "enabled": True, "fields": {"p": "VOUT_P", "n": "VOUT_N"}}],
            "outputs": [
                {"name": "HB_TIME_VGS", "expression": 'vtime(\'hb "GATE_A") - vtime(\'hb "COMMON")'},
                {"name": "HB_TIME_VDS", "expression": 'vtime(\'hb "VOUT_P") - vtime(\'hb "COMMON")'},
                {"name": "HB_PN_VCO_10k", "expression": "value(pn('hbnoise) 10000)"}]}
        replica_test = {"name": "replica", "models": model, "variables": [
            {"name": "G_BIAS", "expression": "0.6"}, {"name": "D_BIAS", "expression": "0.8"}],
            "analyses": [{"name": "noise", "enabled": True, "fields": {"p": "i_out", "n": "VSS"}}],
            "outputs": [{"name": "noise", "expression": 'getData("out" ?result "noise")'}]}
        roles = {}
        for role, test, path, overrides in (("vco", vco_test, self.vco, vco_overrides), ("flicker", replica_test, self.replica, replica_overrides)):
            history = {"source_netlists": [{"path": str(path)}]}
            result = discover_signal_mapping(test, role, history, overrides=overrides)
            result.update(selected_test=test, selected_history=history)
            roles[role] = result
        return {"roles": roles}

    def build(self, report=None, config=None):
        return build_portable_setup(report or self.reports(), config or self.config, self.environment)

    def test_names_and_nonzero_noise_reference_make_valid_contract(self):
        report = self.reports()
        setup = self.build(report)
        self.assertTrue(setup["ok"], setup["errors"])
        engine = setup["engine"]
        profile = engine["physical_profile"]
        self.assertEqual("DUT", profile["main"]["target_instance"])
        self.assertEqual("VSS", profile["replica"]["dc_noise_reference"])
        self.assertEqual("NOISE_DUT.coreMOS", profile["main"]["traces"]["dc_noise"])
        self.assertEqual("G_BIAS", engine["flicker_mapping"]["vgs_variable"])
        self.assertEqual("D_BIAS", engine["flicker_mapping"]["vds_variable"])
        checked = inspect_engine_contract(engine, require_explicit=True, check_paths=True,
            source_from_maestro=True, maestro_source_netlists=[{"path": str(self.vco)}],
            maestro_vco_mapping=engine["vco_mapping"], maestro_flicker_netlists=[{"path": str(self.replica)}],
            maestro_flicker_mapping=engine["flicker_mapping"])
        self.assertTrue(checked["ok"], checked["errors"])

    def test_detector_is_passive_and_does_not_write_configuration(self):
        before = sorted(str(path) for path in self.root.rglob("*"))
        with mock.patch("subprocess.Popen", side_effect=AssertionError("no processes during detection")):
            self.assertTrue(self.build()["ok"])
        self.assertEqual(before, sorted(str(path) for path in self.root.rglob("*")))

    def test_overrides_are_visible_and_change_execution_identity(self):
        original = self.build()
        changed = self.build(config=dict(self.config, portable_setup={"overrides": {"replica.sense_source": "MY_PRIVATE_PROBE", "engine.hb_max_harmonic": "16"}}))
        self.assertTrue(changed["ok"], changed["errors"])
        self.assertEqual("MY_PRIVATE_PROBE", changed["engine"]["physical_profile"]["replica"]["sense_source"])
        self.assertEqual(16, changed["engine"]["hb_max_harmonic"])
        self.assertNotEqual(engine_content_sha256(original["engine"]), engine_content_sha256(changed["engine"]))
        self.assertEqual("Manual override", next(f for f in changed["fields"] if f["key"] == "replica.sense_source")["source"])

    def test_unresolved_device_is_not_filled_from_legacy_M1(self):
        report = self.reports()
        report["roles"]["vco"]["mapping"]["target_instance"] = None
        result = self.build(report)
        self.assertFalse(result["ok"])
        self.assertFalse(result["engine"]["physical_profile"]["main"]["target_instance"])

    def test_reset_override_replaces_saved_values_in_service_merge(self):
        old = {"testbenches": {"vco": {"mapping_overrides": {"target_instance": "OLD"}}},
               "portable_setup": {"mode": "auto", "overrides": {"replica.sense_source": "OLD"}}}
        new = {"testbenches": {"vco": {"mapping_overrides": {}}}, "portable_setup": {"mode": "auto", "overrides": {}}}
        merged = _deep_merge(old, new)
        self.assertEqual({}, merged["testbenches"]["vco"]["mapping_overrides"])
        self.assertEqual({}, merged["portable_setup"]["overrides"])

    def test_selected_cross_tb_variables_are_used_in_private_dc_sample(self):
        module_spec = importlib.util.spec_from_file_location("portable_engine_test", ROOT / "scripts" / "dynamic_isf_closure_engine.py")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        cfg = self.build()["engine"]
        cfg["flicker_template_netlist"] = str(self.replica)
        output = self.root / "run" / "netlist" / "input.scs"
        original = self.replica.read_text()
        module.prepare_dc_noise_netlist(cfg, {"VGS_V": 0.123, "VDS_V": 0.789, "VBS_V": 0.0}, output)
        text = output.read_text()
        self.assertRegex(text, r"G_BIAS=1\.2300")
        self.assertRegex(text, r"D_BIAS=7\.8900")
        self.assertIn("noise (i_out VSS)", text)
        self.assertEqual(original, self.replica.read_text())

    def test_generated_fixture_reuses_the_bound_mos_and_preserves_source_bias(self):
        from scripts import cadence_dual_tb_runner as runner
        from scripts import dynamic_isf_closure_engine as engine

        self.model.write_text(
            "section inactive\nsubckt portable_mos (d g s b)\n"
            "WRONG (di gi si bi) wrong_mos l=l w=w\n"
            "ends portable_mos\nendsection inactive\n" + self.model.read_text()
        )
        original = self.vco.read_text().replace(
            "PARTNER (VOUT_N GATE_B COMMON COMMON) portable_mos l=1u w=4u\n", ""
        ) + "simulatorOptions options temp=46 tnom=27\n"
        self.vco.write_text(original)
        report = self.reports()
        report.update(ok=True, pair={"replica_source": "generated"}, bindings={})
        report["roles"]["flicker"] = None
        history = report["roles"]["vco"]["selected_history"]
        history["fully_complete"] = True
        setup = self.build(report)
        self.assertTrue(setup["ok"], setup["errors"])
        cfg = setup["engine"]
        self.assertTrue(inspect_waveform_exchange(report, cfg)["ok"])
        self.assertIsNone(cfg["physical_profile"]["main"]["traces"]["peer_noise"])
        self.assertEqual("DUT.coreMOS", cfg["physical_profile"]["main"]["traces"]["target_noise"])
        self.assertEqual("cornerA", cfg["device_subckt_section"])

        run = self.root / "run"
        run.mkdir()
        app = {"source_mode": "maestro_pair", "testbenches": {"replica": {"source": "generated"}}}
        runner.write_json(run / runner.APP_CONFIG_NAME, app)
        template = self.root / "engine.json"
        runner.write_json(template, cfg)
        with mock.patch.object(runner, "inspect_maestro_pair", return_value=report):
            materialized = runner.materialize_maestro_engine(engine, template, run, app)
        cfg = engine.load_config(materialized)
        point = cfg["points"][0]
        private = Path(point["flicker_template_netlist"])
        fixture = private.read_text()
        self.assertIn("M_ISF_DC (D G S B) portable_mos l=1u w=4u", fixture)
        self.assertIn("simulatorOptions options temp=46 tnom=27", fixture)
        self.assertIn("coreMOS (di gi si bi) mos_primitive", engine.injected_nmos_subckt(cfg))
        self.assertNotIn("WRONG", engine.injected_nmos_subckt(cfg))

        output = run / "dc_sample" / "netlist" / "input.scs"
        engine.prepare_dc_noise_netlist(
            cfg, {"VGS_V": 0.7, "VDS_V": -0.2, "VBS_V": -0.1, "VS_V": 0.2}, output, point=point
        )
        sample = output.read_text()
        self.assertRegex(sample, r"ISF_DC_VS=2\.000")
        self.assertRegex(sample, r"ISF_DC_VDS=-2\.000")
        self.assertIn("noise (ISF_DC_NOISE_OUT 0) noise", sample)
        self.assertEqual(fixture, private.read_text())
        self.assertEqual(original, self.vco.read_text())
        private.write_text("modified")
        with self.assertRaisesRegex(RuntimeError, "fixture changed"):
            runner.execution_config_path(engine, template, run, "exchange")

    def test_user_named_analysis_is_used_then_normalized_in_private_copy(self):
        spec = importlib.util.spec_from_file_location("portable_named_engine_test", ROOT / "scripts" / "dynamic_isf_closure_engine.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cfg = self.build()["engine"]
        cfg["vco_mapping"].update(periodic_analysis="oscillator_hb", noise_analysis="oscillator_noise")
        text = self.vco.read_text().replace("\nhb (", "\noscillator_hb (").replace("\nhbnoise (", "\noscillator_noise (")
        self.vco.write_text(text)
        output = self.root / "private" / "input.scs"
        module.prepare_main_netlist(cfg, self.vco, output)
        self.assertIn("\nhb (", output.read_text())
        self.assertIn("hbnoise (", output.read_text())
        self.assertEqual(text, self.vco.read_text())

    def test_saved_auto_engine_matches_checked_snapshot_and_can_resume(self):
        setup = self.build()
        workspace = self.root / "workspace"
        service = ToolService(workspace=str(workspace))
        snapshot = resolve_config({"source_mode": "maestro_pair", "execution": {"dry_run": True}}, workspace=str(workspace))
        snapshot["execution"].update(dry_run=False, allow_execute=True)
        snapshot["detected_engine"] = setup["engine"]
        identity = _runtime_identity(None, setup["engine"])
        identity["detected_engine_sha256"] = engine_content_sha256(setup["engine"])
        identity["maestro_pair"] = {"bindings": {}}
        for role, path in (("vco", self.vco), ("flicker", self.replica)):
            identity["files"]["maestro_%s_source_000" % role] = {"path": str(path), "sha256": _file_sha256(path)}
            identity["maestro_pair"]["bindings"][role] = {"binding_fingerprint": "fixture", "history_fingerprint": "history"}
        snapshot["runtime_identity"] = identity
        with mock.patch("isf_tool.core.service.run_preflight", return_value={"ok": True, "resolved_config": snapshot}), mock.patch.object(service, "_execute_job") as execute:
            job = service.start_run({"config": {"execution": {"dry_run": True}}, "require_preflight": True, "background": False})
            captured = execute.call_args.args[2]
        path = Path(captured["config_path"])
        self.assertEqual(setup["engine"], json.loads(path.read_text()))
        self.assertTrue(path.is_relative_to(workspace / "jobs" / job))
        self.assertEqual(_file_sha256(path), captured["runtime_identity"]["engine_config_sha256"])
        self.assertEqual(str(path), service._jobs[job]["_request"]["config_path"])
        run_root = Path(service._jobs[job]["_request"]["run_root"])
        (run_root / STATE_FILENAME).write_text("{}")
        service._jobs[job]["status"] = "interrupted"
        with mock.patch.object(service, "_execute_job") as execute:
            service.resume_job(job, {"background": False})
            execute.assert_called_once()
        service._jobs[job]["status"] = "interrupted"
        self.vco.write_text(self.vco.read_text() + "// topology changed\n")
        with mock.patch.object(service, "_execute_job") as execute:
            with self.assertRaises(ResumeError):
                service.resume_job(job, {"background": False})
            execute.assert_not_called()

    def test_expert_file_cannot_silently_bind_another_device(self):
        cfg = self.build()["engine"]
        mapping = dict(cfg["vco_mapping"], target_instance="PARTNER")
        result = inspect_engine_contract(cfg, require_explicit=True, source_from_maestro=True,
            maestro_vco_mapping=mapping, maestro_flicker_mapping=cfg["flicker_mapping"])
        self.assertFalse(result["ok"])
        self.assertIn("testbenches.vco.mapping.target_instance", [error["field"] for error in result["errors"]])


if __name__ == "__main__":
    unittest.main()
