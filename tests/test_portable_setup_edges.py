"""Auto-setup must not hide local-name or internal-model ambiguity."""
import copy
import unittest

import test_portable_setup as fixtures


class PortableSetupEdgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PortableSetupTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_multiple_model_files_are_narrowed_by_declared_target_subckt(self):
        unrelated = self.fixture.root / "passives.scs"
        unrelated.write_text("subckt resistor (p n)\nR0 (p n) resistor r=1k\nends resistor\n")
        report = self.fixture.reports()
        report["roles"]["vco"]["selected_test"]["models"].append({"path": str(unrelated), "section": "passives"})
        result = self.fixture.build(report)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(str(self.fixture.model), result["engine"]["model_file"])

    def test_generated_probes_avoid_same_scope_local_names(self):
        report = self.fixture.reports()
        vco = report["roles"]["vco"]
        target = next(item for item in vco["topology"]["devices"] if item["name"] == "DUT")
        target.update(name="X.DUT", scope="amplifier", local_name="DUT")
        vco["mapping"]["target_instance"] = "X.DUT"
        vco["topology"]["devices"].append({"name": "X.V_ISF_DRAIN", "local_name": "V_ISF_DRAIN", "scope": "amplifier", "local_terminals": ["ISF_DRAIN", "COMMON"], "terminals": ["X.ISF_DRAIN", "COMMON"], "model": "vsource"})
        result = self.fixture.build(report)
        self.assertTrue(result["ok"], result["errors"])
        probe = result["engine"]["physical_profile"]["main"]["drain_probe"]
        self.assertEqual("ISF_DRAIN_1", probe["node"])
        self.assertEqual("V_ISF_DRAIN_1", probe["source"])

    def test_manual_generated_name_cannot_collide_with_existing_net(self):
        config = dict(self.fixture.config, portable_setup={"overrides": {"main.drain_probe.node": "COMMON"}})
        result = self.fixture.build(config=config)
        self.assertFalse(result["ok"])
        self.assertTrue(any(item["field"] == "main.drain_probe.node" for item in result["errors"]))

    def test_same_internal_instance_requires_an_unambiguous_primitive(self):
        self.fixture.model.write_text("subckt portable_mos (d g s b)\ncoreMOS (di gi si bi) modelA l=l w=w\nends portable_mos\nsubckt portable_mos (d g s b)\ncoreMOS (dx gx sx bx) modelB l=l w=w\nends portable_mos\n")
        ambiguous = self.fixture.build()
        self.assertFalse(ambiguous["ok"])
        self.assertIsNone(ambiguous["engine"]["physical_profile"]["replica"]["intrinsic"]["primitive"])
        config = dict(self.fixture.config, portable_setup={"overrides": {"replica.intrinsic.primitive": "modelB"}})
        chosen = self.fixture.build(config=config)
        self.assertTrue(chosen["ok"], chosen["errors"])
        self.assertEqual(["dx", "gx", "sx", "bx"], chosen["engine"]["physical_profile"]["replica"]["intrinsic"]["terminals"])

    def test_two_internal_mos_instances_are_not_selected_by_order(self):
        self.fixture.model.write_text("subckt portable_mos (d g s b)\ncoreOne (di gi si bi) modelA l=l w=w\ncoreTwo (dx gx sx bx) modelA l=l w=w\nends portable_mos\n")
        result = self.fixture.build()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["engine"]["physical_profile"]["replica"]["intrinsic"]["instance"])

    def test_internal_injection_source_avoids_model_scope_collision(self):
        text = self.fixture.model.read_text().replace("ends portable_mos", "I_ISF_NOISE (di si) isource dc=0\nends portable_mos")
        self.fixture.model.write_text(text)
        result = self.fixture.build()
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual("I_ISF_NOISE_1", result["engine"]["physical_profile"]["replica"]["intrinsic"]["injection_source"])


if __name__ == "__main__":
    unittest.main()
