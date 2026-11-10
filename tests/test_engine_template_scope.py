"""Offline preservation checks for private DC/dynamic replica templates."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from isf_tool.core.errors import ValidationError


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "test_engine_template_scope_module", ROOT / "scripts" / "dynamic_isf_closure_engine.py"
)
ENGINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENGINE)


class TextSource:
    def __init__(self, text):
        self.text = text

    def read_text(self, **_kwargs):
        return self.text


class EngineTemplateScopeTests(unittest.TestCase):
    def test_device_tail_preserves_all_continuation_styles_and_excludes_local_instances(self):
        config = ENGINE.load_config(ROOT / "configs" / "engine_pair_example_flat.json")
        for tail in ("l=1u w=2u m=3", "l=1u w=2u \\\n    m=3",
                     "l=1u w=2u\n+ m=3", "l=1u \\\n    w=2u\n+ m=3"):
            with self.subTest(tail=tail):
                source = TextSource("subckt unused D G S B\nM1 (D G S B) generic_nmos m=99\nends unused\n"
                    + "M1 (d g 0 0) generic_nmos " + tail + "\n"
                    + "simulatorOptions options temp=27\n")
                self.assertEqual(tail, ENGINE._template_device_tail(config, source))

    def test_device_tail_rejects_duplicate_root_or_local_only_target(self):
        config = ENGINE.load_config(ROOT / "configs" / "engine_pair_example_flat.json")
        for text in ("M1 (d g 0 0) generic_nmos m=1\n" * 2,
                     "subckt unused D G S B\nM1 (D G S B) generic_nmos m=99\nends unused\n"):
            with self.subTest(text=text), self.assertRaisesRegex(RuntimeError, "not unique"):
                ENGINE._template_device_tail(config, TextSource(text))

    def test_dc_bias_overrides_only_exact_root_parameter(self):
        source = (
            "parameters VGS=0.6 vgs=0.4 VDS=1.2\n"
            "// VGS=9 must remain a comment\n"
            "subckt inner D G S B\n"
            "parameters VGS=0.2\n"
            "MLOCAL (D G S B) mos VGS=0.3\n"
            "ends inner\n"
            "X1 (d g 0 0) inner VGS=0.7\n"
        )
        expected = source.replace("parameters VGS=0.6", "parameters VGS=8.0000000000000004e-01", 1)
        self.assertEqual(expected, ENGINE._replace_template_parameter(source, "VGS", 0.8))

    def test_complete_nested_root_expression_is_replaced(self):
        source = "parameters VGS=(1+(2*(3+4))) VDS=0.9\n"
        self.assertEqual(
            "parameters VGS=5.0000000000000000e-01 VDS=0.9\n",
            ENGINE._replace_template_parameter(source, "VGS", 0.5),
        )

    def test_local_only_or_duplicate_root_parameter_is_rejected(self):
        for source in (
            "subckt inner D G S B\nparameters VGS=0.2\nends inner\n",
            "parameters VGS=0.2\nparameters VGS=0.3\n",
            "parameters vgs=0.2\n",
        ):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                ENGINE._replace_template_parameter(source, "VGS", 0.5)

    def test_only_root_parameter_statements_reach_dynamic_replica(self):
        source = TextSource(
            "parameters VGS=0.6 \\\n"
            "    VDS=1.2\n"
            "subckt inner D G S B\nparameters VGS=0.2 HIDDEN=8\nends inner\n"
            "parameters WIDTH=2u\n+ LENGTH=1u\n"
        )
        self.assertEqual(
            ["parameters VGS=0.6 \\\n    VDS=1.2", "parameters WIDTH=2u\n+ LENGTH=1u"],
            ENGINE._template_parameter_statements(source),
        )

    def test_custom_root_options_preserve_temperature_and_ignore_local_options(self):
        source = TextSource(
            "subckt inner D G S B\nlocalSettings options temp=12\nends inner\n"
            "cornerSettings options temp=85 tnom=27 reltol=1e-3\n"
            "saveOptions options save=allpub currents=all subcktoppoint=yes\n"
        )
        self.assertEqual(
            "cornerSettings options temp=85 tnom=27 reltol=1e-3",
            ENGINE._template_simulator_options(source),
        )

    def test_only_root_model_includes_reach_dynamic_replica(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "root_model.scs"
            model.write_text("// synthetic root model\n", encoding="utf-8")
            template = root / "input.scs"
            original = (
                'include "root_model.scs" section=hot\n'
                'subckt unused D G S B\ninclude "nonexistent_local_model.scs" section=cold\nends unused\n'
            )
            template.write_text(original, encoding="utf-8")
            self.assertEqual(
                ['include "{}" section=hot'.format(model)],
                ENGINE._template_include_lines(template),
            )
            self.assertEqual(original, template.read_text(encoding="utf-8"))

    def test_local_only_model_include_cannot_replace_root_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "input.scs"
            template.write_text(
                'subckt unused D G S B\ninclude "nested.scs" section=cold\nends unused\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "no model include"):
                ENGINE._template_include_lines(template)

    def test_multiple_simulator_options_fail_instead_of_dropping_later_temperature(self):
        source = TextSource("simulatorOptions options temp=27\ncornerSettings options temp=85\n")
        with self.assertRaisesRegex(RuntimeError, "multiple simulator options"):
            ENGINE._template_simulator_options(source)

    def test_temperature_in_save_options_is_not_silently_discarded(self):
        source = TextSource("saveOptions options save=all temp=85\n")
        with self.assertRaisesRegex(RuntimeError, "non-save simulator settings"):
            ENGINE._template_simulator_options(source)

    def test_dynamic_replica_keeps_restored_temperature_and_nonbias_parameters(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = root / "model.scs"
            model.write_text("// synthetic include; no simulator is executed\n", encoding="utf-8")
            template = root / "template" / "input.scs"
            template.parent.mkdir()
            original = (
                "simulator lang=spectre\n"
                "parameters VGS=0.6 VDS=1.2 WIDTH=2u LENGTH=1u\n"
                "subckt unused D G S B\nparameters HIDDEN=99\nends unused\n"
                'include "{}" section=hot\n'.format(model)
                + "M1 (d g 0 0) generic_nmos l=LENGTH w=WIDTH \\\n    m=1\n"
                "cornerSettings options temp=85 tnom=27 reltol=1e-3\n"
                "saveOptions options save=allpub\n"
            )
            template.write_text(original, encoding="utf-8")
            config = ENGINE.load_config(ROOT / "configs" / "engine_pair_example_flat.json")
            config["flicker_template_netlist"] = str(template)
            wave = {
                "f0": 1e9, "period": 1e-9, "time": [0.0, 0.5e-9, 1e-9],
                "VOUTN": [0.2, 0.4, 0.2], "VOUTP": [1.0, 0.7, 1.0],
                "VCOMMON": [0.0, 0.0, 0.0],
            }
            target = root / "generated" / "input.scs"
            # The model clone is outside this test's scope; isolate it so no
            # PDK, external program, license or real library is consulted.
            with mock.patch.object(ENGINE, "injected_nmos_subckt", return_value="// synthetic cloned model"):
                ENGINE.prepare_replica_netlist(config, wave, target)
            text = target.read_text(encoding="utf-8")
            self.assertIn("cornerSettings options temp=85 tnom=27 reltol=1e-3", text)
            self.assertNotIn("temp=27 tnom=27", text)
            self.assertIn("WIDTH=2u LENGTH=1u", text)
            self.assertIn("generic_nmos_dynprobe l=LENGTH w=WIDTH \\\n    m=1\ncornerSettings options", text)
            self.assertIn("section=hot", text)
            self.assertNotIn("HIDDEN=99", text)
            self.assertEqual(original, template.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
