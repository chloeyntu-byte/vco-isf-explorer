import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from isf_tool.core.maestro_pair import (
    discover_maestro_setups,
    inspect_maestro,
    inspect_maestro_pair,
)
from isf_tool.core.preflight import _detect_pair_engine_config
from isf_tool.core.physical_profile import (
    _flicker_netlist_semantic_errors,
    load_engine_config,
    resolve_physical_profile,
)


ROOT = Path(__file__).resolve().parents[1]


def _write_maestro(root, role, model_file, variable_value="0.6"):
    maestro = Path(root) / ("TB_VCO" if role == "vco" else "TB_FLICKER") / "maestro"
    maestro.mkdir(parents=True)
    test_name = "vco_nominal" if role == "vco" else "flicker_nominal"
    cell = "TB_VCO" if role == "vco" else "TB_FLICKER"
    variables = (
        '<var>VDD<value>1.0</value></var>'
        if role == "vco"
        else '<var>VGS<value>{}</value></var><var>VDS<value>1.0</value></var>'.format(variable_value)
    )
    (maestro / "maestro.sdb").write_text(
        """<?xml version="1.0"?>
<setupdb version="6"><active><tests><test enabled="1">{test}
<tooloptions>
<option>lib<value>PORTABLE</value></option>
<option>cell<value>{cell}</value></option>
<option>view<value>schematic</value></option>
<option>sim<value>spectre</value></option>
</tooloptions><vars>{variables}</vars>
</test></tests></active></setupdb>
""".format(test=test_name, cell=cell, variables=variables),
        encoding="utf-8",
    )
    if role == "vco":
        analyses = """
<analysis Name="hb"><field Name="enable">(t)</field><field Name="p">/OUTP</field><field Name="n">/OUTN</field><field Name="outType">voltage</field></analysis>
<analysis Name="hbnoise"><field Name="enable">(t)</field><field Name="p">/OUTP</field><field Name="n">/OUTN</field><field Name="start">1k</field><field Name="stop">100k</field></analysis>
"""
    else:
        analyses = """
<analysis Name="noise"><field Name="enable">(t)</field><field Name="start">1k</field><field Name="stop">100k</field></analysis>
"""
    (maestro / "active.state").write_text(
        """<?xml version="1.0"?>
<statedb version="5" ICVersion="IC6.1.8-64b.83"><Test Name="{test}">
<component Name="adeInfo"><field Name="designInfo">("PORTABLE" "{cell}" "schematic" "spectre")</field></component>
<component Name="analyses"><analyses Name="analysis">{analyses}</analyses></component>
<component Name="modelSetup"><field Name="modelFiles">(("{model}" "tt"))</field></component>
</Test></statedb>
""".format(test=test_name, cell=cell, analyses=analyses, model=model_file),
        encoding="utf-8",
    )
    return maestro


class MaestroPairContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        model_dir = self.root / "portable_pdk" / "models" / "spectre"
        model_dir.mkdir(parents=True)
        self.model = model_dir / "portable_model.scs"
        self.model.write_text("// portable test model\n", encoding="utf-8")
        self.vco = _write_maestro(self.root, "vco", self.model)
        self.flicker = _write_maestro(self.root, "flicker", self.model)

    def tearDown(self):
        self.temporary.cleanup()

    def _pair(self, **overrides):
        payload = {
            "testbenches": {
                "vco": {"maestro_dir": str(self.vco)},
                "replica": {"maestro_dir": str(self.flicker)},
            },
            "offset_hz": 10000,
            "include_histories": False,
        }
        payload.update(overrides)
        return inspect_maestro_pair(payload)

    def test_two_existing_maestros_are_mapped_without_project_scan(self):
        report = self._pair()
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual("hb", report["roles"]["vco"]["mapping"]["periodic_analysis"])
        self.assertEqual("hbnoise", report["roles"]["vco"]["mapping"]["noise_analysis"])
        self.assertEqual("/OUTP", report["roles"]["vco"]["mapping"]["output_p"])
        self.assertEqual("VGS", report["roles"]["flicker"]["mapping"]["vgs_variable"])
        self.assertEqual("VDS", report["roles"]["flicker"]["mapping"]["vds_variable"])
        self.assertEqual(str(self.root / "portable_pdk"), report["pair"]["pdk"]["root"])
        self.assertTrue(report["read_only"])

    def test_binding_ignores_sweep_values_but_detects_contract_changes(self):
        original = inspect_maestro({"path": str(self.flicker), "role": "flicker", "include_histories": False})
        text = (self.flicker / "maestro.sdb").read_text(encoding="utf-8")
        (self.flicker / "maestro.sdb").write_text(text.replace("0.6", "0.72"), encoding="utf-8")
        value_changed = inspect_maestro({"path": str(self.flicker), "role": "flicker", "include_histories": False})
        self.assertEqual(original["binding_fingerprint"], value_changed["binding_fingerprint"])

        changed = (self.flicker / "maestro.sdb").read_text(encoding="utf-8").replace("VDS", "VDS_NEW")
        (self.flicker / "maestro.sdb").write_text(changed, encoding="utf-8")
        guarded = inspect_maestro_pair(
            {
                "testbenches": {
                    "vco": {"maestro_dir": str(self.vco)},
                    "replica": {
                        "maestro_dir": str(self.flicker),
                        "binding_fingerprint": original["binding_fingerprint"],
                    },
                },
                "offset_hz": 10000,
                "include_histories": False,
            }
        )
        self.assertFalse(guarded["ok"])
        self.assertIn("ISF-E1204", [item["code"] for item in guarded["issues"]])

    def test_disabled_analysis_never_satisfies_vco_capability(self):
        state = (self.vco / "active.state").read_text(encoding="utf-8")
        state = state.replace(
            '<analysis Name="hbnoise"><field Name="enable">(t)</field>',
            '<analysis Name="hbnoise"><field Name="enable">(nil)</field>',
        )
        (self.vco / "active.state").write_text(state, encoding="utf-8")
        report = self._pair()
        self.assertFalse(report["ok"])
        self.assertIn("ISF-E1202", [item["code"] for item in report["issues"]])

    def test_pss_pnoise_is_detected_but_bundled_runner_fails_closed(self):
        state = (self.vco / "active.state").read_text(encoding="utf-8")
        state = state.replace('Name="hb"', 'Name="pss"').replace(
            'Name="hbnoise"', 'Name="pnoise"'
        )
        (self.vco / "active.state").write_text(state, encoding="utf-8")
        report = self._pair()
        self.assertFalse(report["ok"])
        self.assertTrue(
            any(
                item["code"] == "ISF-E1202"
                and "bundled runner" in item["title"]
                for item in report["issues"]
            ),
            report["issues"],
        )

    def test_history_is_pinned_and_full_netlist_tree_changes_fingerprint(self):
        history_root = self.flicker / "results" / "maestro"
        history_root.mkdir(parents=True)
        records = {}
        for index in (0, 1):
            rdb = history_root / "Interactive.{}.rdb".format(index)
            with sqlite3.connect(str(rdb)) as connection:
                connection.execute('CREATE TABLE fixture_history(value INTEGER)')
                connection.execute('INSERT INTO fixture_history VALUES (?)', (index,))
            point = history_root / "Interactive.{}".format(index) / "1" / "flicker_nominal"
            psf = point / "psf"
            netlist = point / "netlist"
            psf.mkdir(parents=True)
            netlist.mkdir()
            (psf / "spectre.out").write_text(
                "spectre completes with 0 errors\n", encoding="utf-8"
            )
            (netlist / "input.scs").write_text(
                "simulator lang=spectre\n", encoding="utf-8"
            )
            (netlist / "local.inc").write_text(
                "// sibling {}\n".format(index), encoding="utf-8"
            )
            records[str(rdb.resolve())] = {
                "tests": [{"name": "flicker_nominal"}],
                "rawDir": [str(psf)],
                "completion": {"complete": 1, "checked": 1},
                "runs": [
                    {
                        "complete": True,
                        "psfDir": str(psf),
                        "pointID": "1",
                        "designPointNumber": str(index + 1),
                        "corner": "tt",
                        "test": "flicker_nominal",
                    }
                ],
            }
            os.utime(str(rdb), (100 + index, 100 + index))

        def fake_inspect(payload):
            return records[str(Path(payload["path"]).resolve())]

        with mock.patch("isf_tool.core.maestro_pair.inspect_rdb", side_effect=fake_inspect):
            pinned = inspect_maestro(
                {
                    "path": str(self.flicker),
                    "role": "flicker",
                    "history": "Interactive.0",
                }
            )
            self.assertEqual("Interactive.0", pinned["selected_history"]["name"])
            original = pinned["history_fingerprint"]
            sibling = (
                history_root
                / "Interactive.0"
                / "1"
                / "flicker_nominal"
                / "netlist"
                / "local.inc"
            )
            sibling.write_text("// changed after preflight\n", encoding="utf-8")
            changed = inspect_maestro(
                {
                    "path": str(self.flicker),
                    "role": "flicker",
                    "history": "Interactive.0",
                }
            )
        self.assertNotEqual(original, changed["history_fingerprint"])

    def test_different_pdk_models_fail_closed(self):
        other = self.root / "other_pdk" / "models" / "spectre" / "other.scs"
        other.parent.mkdir(parents=True)
        other.write_text("// other\n", encoding="utf-8")
        state = (self.flicker / "active.state").read_text(encoding="utf-8")
        (self.flicker / "active.state").write_text(state.replace(str(self.model), str(other)), encoding="utf-8")
        report = self._pair()
        self.assertFalse(report["ok"])
        self.assertIn("ISF-E1203", [item["code"] for item in report["issues"]])

    def test_bounded_discovery_finds_only_maestro_setups(self):
        report = discover_maestro_setups({"roots": [str(self.root)], "max_depth": 4, "max_results": 10})
        self.assertEqual({str(self.vco), str(self.flicker)}, {item["path"] for item in report["findings"]})
        self.assertFalse(report["truncated"])

    def test_same_pdk_templates_are_selected_by_generated_netlist_topology(self):
        hierarchical = self.root / "hierarchical.scs"
        hierarchical.write_text(
            """simulator lang=spectre
subckt generic_stage VOUTN VOUTP VCOMMON
  XDEV (VOUTP VOUTN VCOMMON VCOMMON) generic_nmos_wrapper l=1u w=2u nf=4 m=1
ends generic_stage
hb (VOUTN VOUTP) hb annotate=status
hbnoise (VOUTN VOUTP) hbnoise values=[10k] annotate=status
""",
            encoding="utf-8",
        )
        flat = self.root / "flat.scs"
        flat.write_text(
            """simulator lang=spectre
M1 (VOUTP VOUTN VCOMMON VCOMMON) generic_nmos l=1u w=2u m=1
hb (VOUTP VOUTN) hb annotate=status
hbnoise (VOUTN VOUTP) hbnoise values=[10k] annotate=status
""",
            encoding="utf-8",
        )

        def report(path):
            model = self.root / "device_model.scs"
            return {
                "roles": {
                    "vco": {
                        "selected_test": {"models": [{"path": str(model)}]},
                        "selected_history": {
                            "source_netlists": [{"path": str(path)}]
                        },
                    },
                    "flicker": {
                        "selected_test": {"models": [{"path": str(model)}]}
                    },
                },
                "pair": {
                    "pdk": {
                        "name": "PDK_A",
                        "root": str(self.root / "PDK_A"),
                    }
                },
            }

        hierarchical_path, hierarchical_candidates = _detect_pair_engine_config(
            report(hierarchical)
        )
        flat_path, flat_candidates = _detect_pair_engine_config(report(flat))
        self.assertTrue(
            hierarchical_path.endswith("engine_pair_example_hierarchical.json"),
            hierarchical_candidates,
        )
        self.assertTrue(
            flat_path.endswith("engine_pair_example_flat.json"),
            flat_candidates,
        )

    def test_flicker_probe_chain_rejects_reversed_sense_direction(self):
        source = self.root / "flicker_probe.scs"
        source.write_text(
            """simulator lang=spectre
parameters VGS=0.5 VDS=1.0
include "{model}"
M1 (drain gate 0 0) generic_nmos_wrapper l=1u w=2u nf=4 m=1
V_SENSE_DC (drain supply) vsource dc=0 type=dc
VDS (supply 0) vsource dc=VDS type=dc
VGS (gate 0) vsource dc=VGS type=dc
H_NOISE (NOISE_OUT 0) ccvs rm=1 type=ccvs probe=V_SENSE_DC
noise (NOISE_OUT 0) noise values=[10k] annotate=status
""".format(model=self.model),
            encoding="utf-8",
        )
        engine = load_engine_config(
            ROOT / "configs" / "engine_pair_example_hierarchical.json"
        )
        profile = resolve_physical_profile(
            engine,
            config_path=ROOT / "configs" / "engine_pair_example_hierarchical.json",
            allow_legacy=False,
        )["profile"]
        errors = _flicker_netlist_semantic_errors(
            str(source),
            profile,
            {
                "noise_analysis": "noise",
                "vgs_variable": "VGS",
                "vds_variable": "VDS",
            },
        )
        self.assertIn(
            "physical_profile.replica.dc_sense_drain_terminal",
            [item["field"] for item in errors],
        )


class MaestroMaterializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "scripts" / "cadence_dual_tb_runner.py"
        spec = importlib.util.spec_from_file_location("pair_runner_test", str(path))
        cls.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.runner)

    def test_bound_history_netlist_is_copied_and_hash_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "run"
            run_root.mkdir()
            source = root / "history" / "netlist" / "input.scs"
            source.parent.mkdir(parents=True)
            source.write_text("simulator lang=spectre\n", encoding="utf-8")
            flicker_source = root / "flicker_history" / "netlist" / "input.scs"
            flicker_source.parent.mkdir(parents=True)
            model = root / "model.scs"
            model.write_text("// model\n", encoding="utf-8")
            flicker_source.write_text(
                'simulator lang=spectre\nparameters VGS=0.5 VDS=1.0\n'
                'include "{}"\nXDEV (d g 0 0) generic_nmos_wrapper l=1u w=2u nf=4 m=1\n'
                'noise (out 0) noise start=1k stop=100k annotate=status\n'.format(model),
                encoding="utf-8",
            )
            template = root / "engine.json"
            template.write_text(
                json.dumps(
                    {
                        "model_file": str(model),
                        "spectre": "/bin/true",
                        "psf": "/bin/true",
                        "points": [],
                    }
                ),
                encoding="utf-8",
            )
            app = {
                "source_mode": "maestro_pair",
                "analysis": {"offset": {"hz": 10000}, "max_harmonic": 3},
                "testbenches": {"vco": {}, "replica": {}},
                "detection": {
                    "environment": {
                        "executables": {"spectre": "/bin/true", "psf": "/bin/true"}
                    }
                },
            }
            self.runner.write_json(run_root / self.runner.APP_CONFIG_NAME, app)
            report = {
                "ok": True,
                "roles": {
                    "vco": {
                        "selected_test": {"models": [{"path": str(model)}]},
                        "mapping": {
                            "output_p": "/OUTN",
                            "output_n": "/OUTP",
                        },
                        "selected_history": {
                            "path": str(root / "history.rdb"),
                            "fully_complete": True,
                            "source_netlists": [
                                {
                                    "path": str(source),
                                    "point_id": "1",
                                    "design_point": "0",
                                    "corner": "tt",
                                    "test": "vco",
                                    "psf_dir": str(root / "history" / "psf"),
                                }
                            ],
                        },
                    },
                    "flicker": {
                        "selected_test": {"models": [{"path": str(model)}]},
                        "selected_history": {
                            "path": str(root / "flicker_history.rdb"),
                            "fully_complete": True,
                            "source_netlists": [
                                {
                                    "path": str(flicker_source),
                                    "point_id": "1",
                                    "design_point": "0",
                                    "corner": "tt",
                                    "test": "flicker",
                                    "psf_dir": str(root / "flicker_history" / "psf"),
                                }
                            ],
                        },
                        "mapping": {
                            "noise_analysis": "noise",
                            "vgs_variable": "VGS",
                            "vds_variable": "VDS",
                        },
                    },
                },
                "bindings": {
                    "vco": {
                        "fingerprint": "v",
                        "mapping": {
                            "output_p": "/OUTN",
                            "output_n": "/OUTP",
                        },
                    },
                    "flicker": {"fingerprint": "f"},
                },
            }

            class FakeEngine:
                @staticmethod
                def load_config(path):
                    with Path(path).open(encoding="utf-8") as stream:
                        return json.load(stream)

            with mock.patch.object(self.runner, "inspect_maestro_pair", return_value=report):
                materialized = self.runner.materialize_maestro_engine(
                    FakeEngine, template, run_root, app
                )
            value = json.loads(materialized.read_text(encoding="utf-8"))
            copied = Path(value["source_root"]) / value["points"][0]["source_dir"] / "vco" / "netlist" / "input.scs"
            self.assertTrue(copied.is_file())
            self.assertEqual(source.read_bytes(), copied.read_bytes())
            self.assertTrue(Path(value["flicker_template_netlist"]).is_file())
            self.assertEqual("VGS", value["flicker_mapping"]["vgs_variable"])
            self.assertEqual("/OUTN", value["vco_mapping"]["output_p"])
            manifest = json.loads((run_root / self.runner.PAIR_SOURCE_MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(manifest["sources"][0]["source_sha256"], manifest["sources"][0]["private_copy_sha256"])
            self.assertEqual(
                manifest["roles"]["flicker"][0]["source_tree"]["sha256"],
                manifest["roles"]["flicker"][0]["private_tree"]["sha256"],
            )
            self.assertTrue(manifest["read_only_sources"])

    def test_runner_accepts_resolved_pair_config_directly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "run"
            run_root.mkdir()
            engine_template = root / "engine.json"
            engine_template.write_text("{}\n", encoding="utf-8")
            resolved = root / "resolved.json"
            resolved.write_text(
                json.dumps(
                    {
                        "source_mode": "maestro_pair",
                        "config_path": str(engine_template),
                        "testbenches": {"vco": {}, "replica": {}},
                    }
                ),
                encoding="utf-8",
            )
            materialized = run_root / self.runner.PAIR_ENGINE_CONFIG_NAME
            materialized.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(
                self.runner,
                "materialize_maestro_engine",
                return_value=materialized,
            ) as called:
                result = self.runner.execution_config_path(
                    object(), resolved, run_root, "vco"
                )
            self.assertEqual(materialized, result)
            self.assertTrue(
                (run_root / self.runner.APP_CONFIG_NAME).is_file()
            )
            self.assertEqual(
                engine_template.resolve(),
                Path(called.call_args.args[1]),
            )

    def test_private_hbxf_is_inserted_in_target_scope_and_analysis_chain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.scs"
            target = root / "private" / "input.scs"
            source.write_text(
                """simulator lang=spectre
subckt generic_stage VOUTN VOUTP VCOMMON
  XDEV (VOUTP VOUTN VCOMMON VCOMMON) generic_nmos_wrapper l=1u w=2u nf=4 m=1
  XPEER (VOUTN VOUTP VCOMMON VCOMMON) generic_nmos_wrapper l=1u w=2u nf=4 m=1
ends generic_stage
hb (VOUTN VOUTP) hb annotate=status
hbnoise (VOUTN VOUTP) hbnoise start=100k stop=10M annotate=status
modelParameter info what=models where=rawfile
""",
                encoding="utf-8",
            )
            engine = self.runner.load_module(
                ROOT / "scripts" / "dynamic_isf_closure_engine.py",
                "pair_prepare_engine_test",
            )
            config = engine.load_config(
                ROOT / "configs" / "engine_pair_example_hierarchical.json"
            )
            config["vco_mapping"] = {
                "output_p": "/VOUTP",
                "output_n": "/VOUTN",
            }
            engine.prepare_main_netlist(config, source, target)
            text = target.read_text(encoding="utf-8")
            stage_block = text[
                text.index("subckt generic_stage"):text.index(
                    "ends generic_stage"
                )
            ]
            self.assertIn(
                "I_HBXF_XDEV (VOUTP VCOMMON) isource", stage_block
            )
            self.assertIn(
                "V_XDEV_D (VOUTP XDEV_D) vsource", stage_block
            )
            self.assertIn("hbnoise (VOUTP VOUTN)", text)
            self.assertLess(text.index("hbnoise"), text.index("hbxf"))
            self.assertLess(text.index("hbxf"), text.index("modelParameter"))

    def test_dc_samples_patch_the_bound_flicker_tb_instead_of_rebuilding_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "model.scs"
            model.write_text("// model\n", encoding="utf-8")
            template = root / "flicker" / "netlist" / "input.scs"
            template.parent.mkdir(parents=True)
            template.write_text(
                "simulator lang=spectre\n"
                "parameters VGS=0.1 VDS=0.2\n"
                'include "{}"\n'.format(model)
                + "XDEV (d g 0 0) generic_nmos_wrapper l=1u w=2u nf=4 m=1\n"
                "i_sense (x d) vsource dc=0 type=dc\n"
                "HKEEP (noise_out 0) ccvs rm=1 type=ccvs probe=i_sense\n"
                "noise (noise_out 0) noise start=1k stop=1G annotate=status\n",
                encoding="utf-8",
            )
            engine = self.runner.load_module(
                ROOT / "scripts" / "dynamic_isf_closure_engine.py",
                "pair_dc_template_test",
            )
            config = engine.load_config(
                ROOT / "configs" / "engine_pair_example_hierarchical.json"
            )
            config["flicker_template_netlist"] = str(template)
            config["flicker_mapping"] = {
                "noise_analysis": "noise",
                "vgs_variable": "VGS",
                "vds_variable": "VDS",
            }
            target = root / "sample" / "netlist" / "input.scs"
            engine.prepare_dc_noise_netlist(
                config,
                {
                    "VGS_V": 0.55,
                    "VDS_V": 1.05,
                    "VBS_V": 0.0,
                    "VG_V": 0.55,
                    "VD_V": 1.05,
                    "VS_V": 0.0,
                    "VB_V": 0.0,
                },
                target,
            )
            text = target.read_text(encoding="utf-8")
            self.assertIn("VGS=5.5000000000000004e-01", text)
            self.assertIn("VDS=1.0500000000000000e+00", text)
            self.assertIn("HKEEP", text)
            self.assertIn("probe=i_sense", text)
            self.assertIn("values=[1.0000000000000000e+04]", text)
            self.assertNotIn("IDC_SENSE", text)

    def test_runner_rejects_model_outside_bound_maestro_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configured = root / "configured.scs"
            bound = root / "bound.scs"
            configured.write_text("// configured\n", encoding="utf-8")
            bound.write_text("// bound\n", encoding="utf-8")
            report = {
                "roles": {
                    "vco": {
                        "selected_test": {"models": [{"path": str(bound)}]}
                    },
                    "flicker": {
                        "selected_test": {"models": [{"path": str(bound)}]}
                    },
                }
            }
            with self.assertRaisesRegex(RuntimeError, "differs from"):
                self.runner._choose_current_model(
                    {"model_file": str(configured)}, report
                )


if __name__ == "__main__":
    unittest.main()
