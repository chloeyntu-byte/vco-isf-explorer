"""Portable naming, electrical semantics and explicit-override contracts."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from isf_tool.core.maestro_pair import inspect_maestro, inspect_maestro_pair
from isf_tool.core.signal_mapping import (
    canonical_net,
    cross_tb_mapping,
    discover_signal_mapping,
    parse_netlist,
    voltage_pair,
)


class PortableSignalMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vco_text = """simulator lang=spectre
global 0
parameters supply_bias=1
Mleft (VOUT_P gate_left tail tail) portable_nmos l=1u w=2u
Mright (VOUT_N gate_right tail tail) portable_nmos l=1u w=2u
supply (supply 0) vsource dc=supply_bias
periodic_run (VOUT_N VOUT_P) hb fundfreqs=[5G] maxharms=[10]
pn_measure (VOUT_P VOUT_N) hbnoise start=1k stop=1M
save VOUT_P VOUT_N Mleft:d
"""
        self.replica_text = """simulator lang=spectre
parameters drive_alpha=0.6 drive_beta=1
Mcandidate (drain gate 0 0) portable_nmos l=1u w=2u
current_meter (drain_bias drain) vsource dc=0
dc_gate (gate 0) vsource dc=drive_alpha
dc_drain (drain_bias 0) vsource dc=drive_beta
convert_current (i_out VSS) ccvs rm=1 probe=current_meter
noise_custom (i_out VSS) noise start=1k stop=1M
"""
        self.vco_test = {
            "name": "renamed_oscillator",
            "variables": [{"name": "supply_bias", "expression": "1"}],
            "analyses": [],
            "outputs": [
                {"name": "alpha_wave", "expression": '(v("/gate_left" ?result "hb_td") - v("/tail" ?result "hb_td"))'},
                {"name": "beta_wave", "expression": '(vtime(\'hb "/VOUT_P") - vtime(\'hb "/tail"))'},
                {"name": "PN_raw", "expression": "pn('hbnoise)"},
                {"name": "HB_PN_VCO_10k", "expression": "value(PN_raw 1e4)"},
                {"name": "unused_power", "expression": 'VAR("supply_bias") * average(getData("supply:p" ?result "hb_td"))'},
            ],
        }
        self.replica_test = {
            "name": "renamed_noise_tb",
            "analyses": [],
            "variables": [{"name": "drive_alpha", "expression": "0.6"}, {"name": "drive_beta", "expression": "1"}],
            "outputs": [{"name": "noise", "expression": 'getData("out" ?result "noise")'}, {"name": "read_at_offset", "expression": "value(noise 10000)"}],
        }

    def tearDown(self):
        self.temp.cleanup()

    def _report(self, role, overrides=None, text=None, test=None):
        selected = test or (self.vco_test if role == "vco" else self.replica_test)
        path = self.root / (role + ".scs")
        path.write_text(text if text is not None else (self.vco_text if role == "vco" else self.replica_text), encoding="utf-8")
        history = {"source_netlists": [{"path": str(path), "test": selected["name"]}]}
        return discover_signal_mapping(selected, role, history, overrides)

    def test_names_can_all_change_without_changing_electrical_roles(self):
        vco = self._report("vco")
        replica = self._report("replica")
        self.assertEqual("alpha_wave", vco["mapping"]["vgs_output"])
        self.assertEqual("beta_wave", vco["mapping"]["vds_output"])
        self.assertEqual("Mleft", vco["mapping"]["target_instance"])
        self.assertEqual("Mright", vco["mapping"]["peer_instance"])
        self.assertEqual("drive_alpha", replica["mapping"]["vgs_variable"])
        self.assertEqual("drive_beta", replica["mapping"]["vds_variable"])
        self.assertEqual("current_meter", replica["mapping"]["dc_sense_source"])
        self.assertEqual("convert_current", replica["mapping"]["dc_transducer"])
        self.assertEqual("VSS", replica["mapping"]["output_n"])
        self.assertEqual("HB_PN_VCO_10k", vco["mapping"]["noise_output_at_offset"])
        self.assertEqual("read_at_offset", replica["mapping"]["noise_output_at_offset"])
        self.assertEqual("hb", vco["mapping"]["periodic_analysis_type"])
        self.assertEqual("periodic_run", vco["mapping"]["periodic_analysis"])
        self.assertEqual("VOUT_P", vco["mapping"]["output_p"], "HBNOISE polarity has priority over HB polarity")
        rows = cross_tb_mapping(vco, replica)
        self.assertEqual(("VGS", "alpha_wave", "drive_alpha"), (rows[0]["semantic"], rows[0]["vco_value"], rows[0]["replica_value"]))
        self.assertEqual("mapped", rows[0]["status"])

    def test_unreferenced_names_and_expressions_remain_visible(self):
        report = self._report("vco")
        catalog = {(item["kind"], item["name"]): item for item in report["signal_catalog"]}
        self.assertIn(("output", "unused_power"), catalog)
        self.assertIn(("trace", "supply:p"), catalog)
        self.assertIn(("variable", "supply_bias"), catalog)
        self.assertIn(("parameter", "supply_bias"), catalog)
        self.assertIn(("save", "Mleft:d"), catalog)
        self.assertIn(("device", "Mright"), catalog)
        self.assertEqual(self.vco_test["outputs"][-1]["expression"], catalog[("output", "unused_power")]["expression"])

    def test_missing_and_unknown_overrides_are_not_silently_accepted(self):
        report = self._report("vco", {"vgs_output": "deleted_wave", "old_magic_key": "Mleft"})
        self.assertIsNone(report["mapping"]["vgs_output"])
        errors = [issue for issue in report["mapping_issues"] if issue["severity"] == "error"]
        self.assertEqual({"vgs_output", "old_magic_key"}, {issue["key"] for issue in errors})

    def test_empty_override_returns_to_automatic_detection(self):
        report = self._report("vco", {"vgs_output": ""})
        field = next(item for item in report["mapping_fields"] if item["key"] == "vgs_output")
        self.assertEqual("alpha_wave", field["value"])
        self.assertEqual("auto", field["status"])

    def test_multiple_device_candidates_require_a_selection(self):
        test = dict(self.vco_test, outputs=[])
        report = self._report("vco", test=test)
        self.assertIsNone(report["mapping"]["target_instance"])
        field = next(item for item in report["mapping_fields"] if item["key"] == "target_instance")
        self.assertEqual("ambiguous", field["status"])
        self.assertEqual(["Mleft", "Mright"], field["candidates"])
        chosen = self._report("vco", {"target_instance": "Mright"}, test=test)
        self.assertEqual("gate_right", chosen["mapping"]["gate"])

    def test_multiple_semantic_variables_do_not_pick_first(self):
        test = {"name": "no_history", "outputs": [], "analyses": [], "variables": [{"name": "VGS_A"}, {"name": "VGS_B"}, {"name": "VDS"}]}
        report = discover_signal_mapping(test, "replica")
        self.assertIsNone(report["mapping"]["vgs_variable"])
        self.assertTrue(any(issue["key"] == "vgs_variable" for issue in report["mapping_issues"]))

    def test_cross_tb_vgs_vds_swap_is_rejected_by_connectivity(self):
        report = self._report("replica", {"vgs_variable": "drive_beta", "vds_variable": "drive_alpha"})
        errors = {issue["key"] for issue in report["mapping_issues"] if issue["severity"] == "error"}
        self.assertTrue({"vgs_variable", "vds_variable"}.issubset(errors))
        wrong_wave = self._report("vco", {"vgs_output": "beta_wave"})
        self.assertTrue(any(issue["severity"] == "error" for issue in wrong_wave["mapping_issues"]))

    def test_arbitrary_misleading_name_does_not_override_electrical_meaning(self):
        test = dict(self.vco_test, outputs=[dict(item) for item in self.vco_test["outputs"]])
        test["outputs"][1]["name"] = "HB_TIME_VGS"
        report = self._report("vco", {"vds_output": "HB_TIME_VGS"}, test=test)
        self.assertEqual("HB_TIME_VGS", report["mapping"]["vds_output"])
        self.assertFalse(any(issue["severity"] == "error" for issue in report["mapping_issues"]))

    def test_cross_tb_can_use_verified_pins_without_named_maestro_outputs(self):
        vco = self._report("vco", {"target_instance": "Mleft"}, test=dict(self.vco_test, outputs=[]))
        replica = self._report("replica")
        row = cross_tb_mapping(vco, replica)[0]
        self.assertEqual("V(gate_left)-V(tail)", row["vco_value"])
        self.assertEqual("drive_alpha", row["replica_value"])
        self.assertEqual("derived", row["status"])
        self.assertEqual("pin_derived", row["vco_source"])

    def test_cross_tb_does_not_claim_complex_expression_is_executable(self):
        test = dict(self.vco_test, outputs=[{"name": "HB_TIME_VGS", "expression": 'abs(v("/gate_left") - v("/tail"))'}])
        vco = self._report("vco", {"vgs_output": "HB_TIME_VGS", "target_instance": "Mleft"}, test=test)
        row = cross_tb_mapping(vco, self._report("replica"))[0]
        self.assertEqual("reference", row["status"])
        self.assertEqual("reference_only", row["vco_source"])

    def test_conflicting_port_override_is_rejected(self):
        report = self._report("vco", {"gate": "gate_right"})
        self.assertTrue(any(issue["key"] == "gate" and issue["severity"] == "error" for issue in report["mapping_issues"]))

    def test_calculator_and_frequency_domain_expressions_are_not_executed(self):
        self.assertIsNone(voltage_pair('abs(v("/g") - v("/s"))'))
        self.assertIsNone(voltage_pair('(vh(\'hb "/g") - vh(\'hb "/s"))'))
        self.assertEqual(["g", "s"], voltage_pair('VT("/g" "/s")'))
        test = dict(self.vco_test, outputs=[{"name": "HB_TIME_VGS", "expression": 'someUserFunction("/gate_left", "/tail")'}])
        report = self._report("vco", test=test)
        self.assertTrue(any("calculator" in issue["message"] for issue in report["mapping_issues"]))

    def test_hierarchy_names_and_local_ports_are_both_available(self):
        topology = parse_netlist("""subckt pair PLUS MINUS COMMON
Malpha (PLUS MINUS COMMON COMMON) portable_nmos l=1u \\
 w=2u
ends pair
Xstage (P N B) pair
custom_hb (P N) hb
+ fundfreqs=[5G] maxharms=[10]
""")
        device = next(item for item in topology["devices"] if item["name"] == "Xstage.Malpha")
        self.assertEqual(["P", "N", "B", "B"], device["terminals"])
        self.assertEqual(["PLUS", "MINUS", "COMMON", "COMMON"], device["local_terminals"])
        self.assertEqual("Malpha", device["local_name"])
        self.assertEqual({"l": "1u", "w": "2u"}, device["parameters"])
        self.assertEqual("[5G]", topology["analyses"][0]["parameters"]["fundfreqs"])
        self.assertEqual("0", canonical_net("/gnd!"))

    def test_parameter_expressions_are_preserved_without_evaluation(self):
        topology = parse_netlist('parameters base=1u scaling=2\nMtest (d g s b) renamed_nmos l=(base * (scaling + 1)) w={2 * base} note="two words"\n')
        self.assertEqual({"l": "(base * (scaling + 1))", "w": "{2 * base}", "note": '"two words"'}, topology["devices"][0]["parameters"])

    def test_history_of_another_test_does_not_supply_names(self):
        path = self.root / "other.scs"
        path.write_text(self.vco_text, encoding="utf-8")
        history = {"source_netlists": [{"path": str(path), "test": "different_test"}]}
        report = discover_signal_mapping(self.vco_test, "vco", history)
        self.assertFalse(report["topology"]["devices"])

    def test_local_includes_are_discovered_but_external_models_are_not_read(self):
        local = self.root / "local_cells.scs"
        local.write_text("subckt branch P N S\nMportable (P N S S) custom_nmos l=1u w=2u\nends branch\n", encoding="utf-8")
        text = 'include "local_cells.scs"\ninclude "/external/pdk/models.scs"\nXbranch (P N S) branch\n'
        report = self._report("vco", text=text, test=dict(self.vco_test, outputs=[]))
        self.assertIn("Xbranch.Mportable", [item["name"] for item in report["topology"]["devices"]])
        self.assertIn("/external/pdk/models.scs", [item["name"] for item in report["topology"]["includes"]])
        self.assertFalse(report["topology"]["warnings"])

    def test_ground_alias_is_evidenced_without_changing_display_name(self):
        test = dict(self.replica_test, analyses=[{"name": "noise_custom", "enabled": True, "fields": {"p": "/i_out", "n": "/gnd!"}}])
        # State normally calls this analysis 'noise'; use a matching custom
        # name/type from the generated netlist below to exercise actual aliases.
        test["analyses"][0]["name"] = "noise"
        text = self.replica_text.replace("noise_custom (i_out VSS)", "noise (i_out 0)")
        report = self._report("replica", text=text, test=test)
        self.assertEqual("/gnd!", report["mapping"]["output_n"])
        self.assertEqual("0", report["mapping"]["output_n_netlist"])
        self.assertEqual("0", report["topology"]["net_aliases"]["/gnd!"])
        self.assertNotIn("VSS", report["topology"]["net_aliases"])

    def test_different_current_and_history_nodes_are_not_treated_as_aliases(self):
        test = dict(self.replica_test, analyses=[{"name": "noise", "enabled": True, "fields": {"p": "/new_output", "n": "/VSS"}}])
        text = self.replica_text.replace("noise_custom (", "noise (")
        report = self._report("replica", text=text, test=test)
        self.assertTrue(any(issue["key"] == "output_p" and issue["severity"] == "error" for issue in report["mapping_issues"]))
        self.assertNotIn("/new_output", report["topology"]["net_aliases"])


class MaestroMappingIntegrationTests(unittest.TestCase):
    def test_only_explicit_overrides_are_applied_and_stale_names_fail_pair(self):
        # Existing binding.mapping is a snapshot, not a perpetual user override.
        from test_maestro_pair import _write_maestro
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.scs"
            model.write_text("// fixture", encoding="utf-8")
            vco = _write_maestro(directory, "vco", model)
            replica = _write_maestro(directory, "flicker", model)
            payload = {"include_histories": False, "testbenches": {"vco": {"maestro_dir": str(vco), "mapping": {"output_p": "/stale_node"}}, "replica": {"maestro_dir": str(replica)}}}
            report = inspect_maestro_pair(payload)
            self.assertEqual("/OUTP", report["roles"]["vco"]["mapping"]["output_p"])
            self.assertIn("cross_tb_mapping", report)
            payload["testbenches"]["vco"]["mapping_overrides"] = {"output_p": "/stale_node"}
            rejected = inspect_maestro_pair(payload)
            self.assertFalse(rejected["ok"])
            self.assertTrue(any("mapping_overrides.output_p" in str(issue.get("field")) for issue in rejected["issues"]))


if __name__ == "__main__":
    unittest.main()
