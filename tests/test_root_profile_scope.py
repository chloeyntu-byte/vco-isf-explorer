"""Local subcircuit names must not impersonate the root MOS probe contract."""

from pathlib import Path
import tempfile
import unittest

from isf_tool.core.physical_profile import (
    _flicker_netlist_semantic_errors, load_engine_config, resolve_physical_profile,
)


ROOT = Path(__file__).resolve().parents[1]


class RootProfileScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.model = self.directory / "generic_model.scs"
        self.model.write_text("// synthetic model include; no simulator invoked\n", encoding="utf-8")
        config = ROOT / "configs" / "engine_pair_example_hierarchical.json"
        self.profile = resolve_physical_profile(load_engine_config(config), config_path=config, allow_legacy=False)["profile"]
        self.mapping = {"noise_analysis": "noise", "vgs_variable": "VGS", "vds_variable": "VDS", "vbs_variable": "VBS"}
        self.source = self.directory / "input.scs"
        self.root_text = (
            'simulator lang=spectre\n'
            'parameters VGS=0.5 VDS=1.0 VBS=0\n'
            'include "%s"\n' % self.model
            + 'M1 (drain gate 0 0) generic_nmos_wrapper l=1u w=2u nf=4 m=1\n'
            'V_SENSE_DC (supply drain) vsource dc=0 type=dc\n'
            'VDS (supply 0) vsource dc=VDS type=dc\n'
            'VGS (gate 0) vsource dc=VGS type=dc\n'
            'H_NOISE (NOISE_OUT 0) ccvs rm=1 type=ccvs probe=V_SENSE_DC\n'
            'noise (NOISE_OUT 0) noise values=[10k] annotate=status\n'
        )

    def tearDown(self):
        self.temporary.cleanup()

    def check(self, root=None, child=""):
        self.source.write_text(child + (self.root_text if root is None else root), encoding="utf-8")
        return _flicker_netlist_semantic_errors(str(self.source), self.profile, self.mapping)

    @staticmethod
    def child(body):
        return "subckt optional_bank P N R\n" + body + "ends optional_bank\nXbank (a b 0) optional_bank\n"

    def assert_field(self, errors, field):
        self.assertIn(field, [row["field"] for row in errors], errors)

    def test_existing_public_root_probe_fixture_still_passes(self):
        self.assertEqual([], self.check())

    def test_root_mos_and_same_local_name_in_multiple_children_are_not_duplicates(self):
        children = ("subckt child_a D G S B\n"
                    "M1 (D G S B) generic_nmos_wrapper l=180n w=8u nf=20 m=16\n"
                    "ends child_a\nsubckt child_b D G S B\n"
                    "M1 (D G S B) generic_nmos_wrapper l=180n w=2u nf=1 m=1\n"
                    "ends child_b\nXA (a b 0 0) child_a\nXB (c d 0 0) child_b\n")
        self.assertEqual([], self.check(child=children))

    def test_root_duplicate_mos_is_still_rejected_even_with_child_names(self):
        duplicate = "M1 (another_d another_g 0 0) generic_nmos_wrapper l=1u w=2u m=1\n"
        errors = self.check(root=self.root_text + duplicate, child=self.child(duplicate))
        matches = [row for row in errors if row.get("instance") == "M1"]
        self.assertEqual(1, len(matches), errors)
        self.assertEqual(2, matches[0]["count"], errors)

    def test_child_sense_transducer_and_noise_names_do_not_pollute_root_contract(self):
        # Deliberately wrong *local* ports and gains would fail if searched as
        # root records. Neither a local duplicate nor its data may be selected.
        child = self.child(
            "V_SENSE_DC (P N) vsource dc=0.2 type=dc\n"
            "H_NOISE (N R) ccvs rm=9 probe=unrelated_local_probe\n"
            "noise (N R) noise start=1k stop=1M\n")
        self.assertEqual([], self.check(child=child))

    def test_actual_root_sense_and_transducer_duplicates_are_still_rejected(self):
        for statement, field in (
            ("V_SENSE_DC (supply drain) vsource dc=0 type=dc\n", "physical_profile.replica.dc_sense_source"),
            ("H_NOISE (NOISE_OUT 0) ccvs rm=1 probe=V_SENSE_DC\n", "physical_profile.replica.dc_transducer"),
        ):
            with self.subTest(field=field):
                errors = self.check(root=self.root_text + statement, child=self.child(statement))
                self.assertTrue(any(row["field"] == field and row.get("count") == 2 for row in errors), errors)

    def test_child_only_target_cannot_replace_a_missing_root_mos(self):
        statement = next(line for line in self.root_text.splitlines(keepends=True) if line.startswith("M1 "))
        errors = self.check(root=self.root_text.replace(statement, ""), child=self.child(statement))
        self.assertTrue(any(row.get("instance") == "M1" and row.get("count") == 0 for row in errors), errors)

    def test_child_only_sense_transducer_noise_or_variables_cannot_satisfy_root_requirements(self):
        for prefix, field in (
            ("V_SENSE_DC ", "physical_profile.replica.dc_sense_source"),
            ("H_NOISE ", "physical_profile.replica.dc_transducer"),
            ("noise ", "testbenches.replica.mapping.noise_analysis"),
            ("parameters ", "testbenches.replica.mapping.vgs_variable"),
        ):
            with self.subTest(field=field):
                statement = next(line for line in self.root_text.splitlines(keepends=True) if line.startswith(prefix))
                errors = self.check(root=self.root_text.replace(statement, ""), child=self.child(statement))
                self.assert_field(errors, field)

    def test_root_probe_direction_gain_and_output_guards_are_unchanged(self):
        for old, new, field in (
            ("(supply drain)", "(drain supply)", "physical_profile.replica.dc_sense_drain_terminal"),
            ("vsource dc=0 type=dc", "isource dc=0 type=dc", "physical_profile.replica.dc_sense_source"),
            ("vsource dc=0 type=dc", "vsource dc=0.1 type=dc", "physical_profile.replica.dc_sense_source"),
            ("ccvs rm=1", "ccvs rm=2", "physical_profile.replica.dc_transducer"),
            ("probe=V_SENSE_DC", "probe=another_source", "physical_profile.replica.dc_transducer"),
            ("noise (NOISE_OUT 0)", "noise (wrong_output 0)", "testbenches.replica.mapping.noise_output"),
        ):
            with self.subTest(field=field, replacement=new):
                errors = self.check(root=self.root_text.replace(old, new), child=self.child(self.root_text))
                self.assert_field(errors, field)


if __name__ == "__main__":
    unittest.main()
