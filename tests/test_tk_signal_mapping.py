"""Portable mapping round trips and stale-selection protection; no EDA or GUI."""

import copy
import unittest
from unittest import mock

from isf_tool.tk.app import IsfApplication, _ui_font_family
from isf_tool.tk.viewmodel import (
    catalog_review_rows, catalog_summary,
    cross_tb_review_rows, flatten_config, invalidate_role,
    maestro_pair_payload, mapping_review_rows, merge_config,
    portable_issue_rows,
)


class Variable:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class PortableMappingTests(unittest.TestCase):
    def test_execution_failures_name_the_field_and_route_to_the_actual_editor(self):
        pair = {"roles": {"vco": {"mapping_fields": [
            {"key": "target_instance", "label": "VCO target device", "status": "ambiguous", "value": None},
            {"key": "vgs_output", "label": "VGS time-domain output", "value": "CUSTOM_GATE"},
        ]}}}
        setup = {"fields": [
            {"key": "engine.spectre", "label": "Spectre Simulator"},
            {"key": "main.instance_path", "label": "VCO Target device full hierarchy"},
        ], "errors": [
            {"field": "engine.spectre", "message": "Executable not found"},
            {"field": "main.instance_path", "message": "Not uniquely detected"},
            {"field": "testbenches.vco.mapping_overrides.vgs_output", "message": "CUSTOM_GATE is not a calculable waveform"},
        ]}
        original = copy.deepcopy(setup)
        rows = portable_issue_rows(setup, pair)
        self.assertEqual("vco:target_instance", rows[0]["mapping_iid"])
        self.assertEqual("VCO Target device full hierarchy: Not uniquely detected", rows[0]["message"])
        self.assertEqual("physical", rows[1]["tab"])
        self.assertEqual("Spectre Simulator: Executable not found", rows[1]["message"])
        self.assertEqual("vco:vgs_output", rows[2]["mapping_iid"])
        self.assertEqual(original, setup)

    def test_latin_sans_is_preferred_and_missing_faces_use_default(self):
        self.assertEqual("DejaVu Sans", _ui_font_family(["DejaVu Sans", "WenQuanYi Micro Hei"], "DejaVu Sans"))
        self.assertEqual("DejaVu Sans", _ui_font_family(["Noto Sans CJK SC", "WenQuanYi Micro Hei"], "DejaVu Sans"))
        self.assertEqual("DejaVu Sans", _ui_font_family(["DejaVu Sans"], "DejaVu Sans"))
        self.assertEqual("liberation sans", _ui_font_family(["gothic", "song ti", "liberation sans"], "gothic"))

    def test_full_catalog_keeps_nets_outputs_and_variables_separate_from_voltage_semantics(self):
        report = {"roles": {"vco": {"signal_catalog": [
            {"kind": "net", "name": "VOUT_N", "source": "netlist"},
            {"kind": "output", "name": "HB_PN_VCO_10k", "expression": "value(pn(\"hbnoise\") 10000)"},
            {"kind": "output", "name": "HB_TIME_VDS", "expression": "v(\"D\")-v(\"S\")"},
        ]}, "flicker": {"signal_catalog": [
            {"kind": "variable", "name": "VDS", "details": {"value": 0}},
            {"kind": "net", "name": "i_out"},
        ]}}}
        original = copy.deepcopy(report)
        rows = catalog_review_rows(report)
        self.assertEqual(5, len(rows))
        self.assertEqual("Net name 2 items · Outputs 2 items · Variables 1 items · All 5 items", catalog_summary(report))
        self.assertEqual(["HB_PN_VCO_10k", "HB_TIME_VDS"], [row["name"] for row in catalog_review_rows(report, kind="output")])
        self.assertEqual(["VOUT_N"], [row["name"] for row in catalog_review_rows(report, query="vout_n")])
        self.assertEqual(["VDS"], [row["name"] for row in catalog_review_rows(report, query="Variable")])
        self.assertEqual(0, catalog_review_rows(report, kind="variable")[0]["expression"])
        rows[0]["name"] = "changed"
        self.assertEqual(original, report)

    def config(self):
        return {
            "testbenches": {
                "vco": {"path": "/project/vco", "test": "osc", "history": "one.rdb", "binding_fingerprint": "vco-binding", "history_fingerprint": "vco-history", "mapping_overrides": {"vgs_output": "HB_GATE", "output_p": "VP"}},
                "replica": {"path": "/project/noise", "test": "noise", "history": "two.rdb", "binding_fingerprint": "noise-binding", "history_fingerprint": "noise-history", "mapping_overrides": {"vgs_variable": "bias_gate"}},
            },
            "portable_setup": {"mode": "auto", "overrides": {"main.terminals.source": "TAIL"}},
            "vendor": {"keep": True},
        }

    def test_overrides_reach_inspection_and_round_trip_without_loss(self):
        config = self.config()
        config["profiles"] = {"environment": {"python": "/private/python"}}
        config["analysis"] = {"max_harmonic": 5, "offset": {"hz": 12000}}
        original = copy.deepcopy(config)
        payload = maestro_pair_payload(config)
        self.assertEqual({"vgs_output": "HB_GATE", "output_p": "VP"}, payload["testbenches"]["vco"]["mapping_overrides"])
        self.assertEqual({"vgs_variable": "bias_gate"}, payload["testbenches"]["replica"]["mapping_overrides"])
        self.assertEqual("TAIL", payload["portable_setup"]["overrides"]["main.terminals.source"])
        self.assertEqual("/private/python", payload["profiles"]["environment"]["python"])
        self.assertEqual(5, payload["analysis"]["max_harmonic"])
        merged = merge_config(config, flatten_config(config))
        self.assertEqual(config["portable_setup"], merged["portable_setup"])
        self.assertEqual(config["vendor"], merged["vendor"])
        self.assertEqual(original, config)

    def test_blank_manual_value_restores_detection_not_stale_raw_mapping(self):
        config = self.config()
        merged = merge_config(config, {"vco_mapping_overrides": {"vgs_output": "  "}, "portable_overrides": {}})
        self.assertEqual({}, merged["testbenches"]["vco"]["mapping_overrides"])
        self.assertEqual({}, merged["portable_setup"]["overrides"])
        self.assertEqual("vco-binding", merged["testbenches"]["vco"]["binding_fingerprint"])
        self.assertEqual("bias_gate", merged["testbenches"]["replica"]["mapping_overrides"]["vgs_variable"])

    def test_tb_change_invalidates_only_its_binding_and_all_generated_correspondence(self):
        config = self.config()
        config["mapping"] = {"old": "node"}
        config["detection"] = {"portable_setup": {"engine": "old"}}
        changed = invalidate_role(config, "vco")
        self.assertEqual("", changed["testbenches"]["vco"]["binding_fingerprint"])
        self.assertNotIn("mapping_overrides", changed["testbenches"]["vco"])
        self.assertEqual("noise-binding", changed["testbenches"]["replica"]["binding_fingerprint"])
        self.assertEqual({}, changed["portable_setup"]["overrides"])
        self.assertNotIn("mapping", changed)
        self.assertNotIn("detection", changed)
        self.assertEqual("vco-binding", config["testbenches"]["vco"]["binding_fingerprint"])

    def test_cross_tb_rows_pair_voltage_meaning_even_with_unfamiliar_names(self):
        report = {"roles": {
            "vco": {"mapping_fields": [{"key": "vds_output", "value": "drain_time"}, {"key": "vgs_output", "value": "gate_time"}]},
            "flicker": {"mapping_fields": [{"key": "vgs_variable", "value": "x_bias"}, {"key": "vds_variable", "value": "y_bias"}]},
        }}
        rows = {row["key"]: row for row in cross_tb_review_rows(report)}
        self.assertEqual(("gate_time", "x_bias"), (rows["vgs"]["vco"], rows["vgs"]["replica"]))
        self.assertEqual(("drain_time", "y_bias"), (rows["vds"]["vco"], rows["vds"]["replica"]))
        report["roles"]["vco"]["mapping_fields"][1]["status"] = "invalid"
        self.assertEqual("Needs confirmation", cross_tb_review_rows(report)[0]["status"])
        mapped = mapping_review_rows(report)
        mapped[0]["value"] = "changed"
        self.assertEqual("drain_time", report["roles"]["vco"]["mapping_fields"][0]["value"])

    def test_async_detection_cannot_erase_a_new_manual_override(self):
        instance = IsfApplication.__new__(IsfApplication)
        config = self.config()
        instance._config_from_ui = lambda: config
        instance._pair_selection = lambda: ("same-selection",)
        instance._selection_revision = 1
        instance.binding_status_var = Variable()
        instance._pair_inspected = mock.Mock()
        captured = {}
        instance._submit = lambda _label, _function, success: captured.update(success=success)
        instance._inspect_pair()
        instance._selection_revision += 1
        captured["success"]({"ok": True})
        instance._pair_inspected.assert_not_called()
        self.assertIn("Stale", instance.binding_status_var.get())

    def test_cross_tb_derived_nodes_are_usable_without_a_named_maestro_output(self):
        report = {"roles": {
            "vco": {"mapping_fields": [{"key": "vgs_output", "value": None, "status": "missing"}]},
            "flicker": {"mapping_fields": [{"key": "vgs_variable", "value": "VGS"}]},
        }, "cross_tb_mapping": [{
            "semantic": "VGS", "vco_value": "V(net6)-V(net3)",
            "vco_expression": "V(net6)-V(net3)", "vco_source": "pin_derived",
            "replica_value": "VGS", "status": "derived", "help": "Calculate from raw node waveforms. ",
        }]}
        row = cross_tb_review_rows(report)[0]
        self.assertEqual("V(net6)-V(net3)", row["vco"])
        self.assertEqual("VGS", row["replica"])
        self.assertEqual("Derived from raw nodes", row["source"])
        self.assertEqual("Mapped by nodes", row["status"])
        self.assertIn("raw node", row["help"])

    def test_complex_expression_is_reference_only_not_an_executable_pair(self):
        report = {"cross_tb_mapping": [{
            "semantic": "VDS", "vco_value": "CALC_COMPLEX", "vco_expression": "customCalc(data)",
            "vco_source": "reference_only", "replica_value": "VDS", "status": "reference",
        }]}
        row = cross_tb_review_rows(report)[1]
        self.assertEqual("CALC_COMPLEX", row["vco"])
        self.assertEqual("customCalc(data)", row["expression"])
        self.assertEqual("reference only / not calculable", row["status"])
        self.assertEqual("Expression for reference only", row["source"])


if __name__ == "__main__":
    unittest.main()
