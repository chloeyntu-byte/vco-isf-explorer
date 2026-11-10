"""Keep local netlist pins distinct from proven, fully resolved PSF aliases."""
import copy
import importlib.util
import unittest
from pathlib import Path

from isf_tool.core.errors import ProfileError
from isf_tool.core.physical_profile import (
    _netlist_semantic_errors, inspect_engine_contract, validate_physical_profile,
)
import test_portable_setup as fixtures


class HierarchicalTerminalAliasTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PortableSetupTests()
        self.fixture.setUp()
        self.fixture.vco.write_text(
            'simulator lang=spectre\ninclude "%s" section=cornerA\n' % self.fixture.model
            + 'subckt gm D1 D2 TAIL\n'
            'DUT (D1 D2 TAIL TAIL) portable_mos l=1u w=4u\n'
            'PARTNER (D2 D1 TAIL TAIL) portable_mos l=1u w=4u\n'
            'ends gm\n'
            'subckt oscillator P N BIAS\n'
            'CORE (P N floating_source) gm\n'
            'ends oscillator\n'
            'TOP (VOUT_P VOUT_N BIAS) oscillator\n'
            'OTHER (VOUT_P VOUT_N OTHER_BIAS) oscillator\n'
            'hb (VOUT_P VOUT_N) hb fundfreqs=[1G] maxharms=[12] annotate=status\n'
            'hbnoise (VOUT_P VOUT_N) hbnoise start=1k stop=100k annotate=status\n'
        )
        self.report = self.fixture.reports(vco_overrides={
            'target_instance': 'TOP.CORE.DUT', 'peer_instance': 'TOP.CORE.PARTNER',
        })
        self.setup = self.fixture.build(self.report)
        self.engine = self.setup['engine']
        self.profile = self.engine['physical_profile']

    def tearDown(self):
        self.fixture.tearDown()

    def check_netlist(self, profile=None):
        return _netlist_semantic_errors(
            str(self.fixture.vco), profile or self.profile,
            require_private_supplements=False, vco_mapping=self.engine['vco_mapping'],
        )

    def test_multilevel_ports_resolve_without_changing_local_edit_pins(self):
        self.assertTrue(self.setup['ok'], self.setup['errors'])
        main = self.profile['main']
        self.assertEqual(main['terminals'], {'drain':'D1', 'gate':'D2', 'source':'TAIL', 'bulk':'TAIL'})
        self.assertEqual(main['resolved_terminals'], {
            'drain':'VOUT_P', 'gate':'VOUT_N',
            'source':'TOP.floating_source', 'bulk':'TOP.floating_source',
        })
        self.assertEqual(main['traces']['source'], 'TOP.floating_source')
        self.assertEqual(main['injection']['negative'], 'TAIL')
        self.assertEqual(self.check_netlist(), [])
        checked = inspect_engine_contract(
            self.engine, require_explicit=True, check_paths=True, source_from_maestro=True,
            maestro_source_netlists=[{'path':str(self.fixture.vco)}],
            maestro_vco_mapping=self.engine['vco_mapping'],
            maestro_flicker_netlists=[{'path':str(self.fixture.replica)}],
            maestro_flicker_mapping=self.engine['flicker_mapping'],
        )
        self.assertTrue(checked['ok'], checked['errors'])

    def test_alias_evidence_is_not_an_editable_override(self):
        config = dict(self.fixture.config, portable_setup={'overrides':{'main.resolved_terminals.source':'WRONG'}})
        setup = self.fixture.build(self.report, config)
        self.assertFalse(setup['ok'])
        self.assertTrue(any(item['field']=='main.resolved_terminals.source' for item in setup['errors']))
        self.assertEqual(setup['engine']['physical_profile']['main']['resolved_terminals']['source'], 'TOP.floating_source')

    def test_trace_must_match_resolved_evidence_not_just_local_spelling(self):
        bad = copy.deepcopy(self.profile)
        bad['main']['traces']['source'] = 'TAIL'
        with self.assertRaises(ProfileError):
            validate_physical_profile(bad)

    def test_self_consistent_forged_alias_is_rejected_by_actual_netlist(self):
        bad = copy.deepcopy(self.profile)
        bad['main']['resolved_terminals']['source'] = 'OTHER.floating_source'
        bad['main']['traces']['source'] = 'OTHER.floating_source'
        validate_physical_profile(bad)
        errors = self.check_netlist(bad)
        self.assertTrue(any(item['field']=='physical_profile.main.resolved_terminals' for item in errors))

    def test_target_cannot_jump_to_same_local_name_in_another_instance(self):
        bad = copy.deepcopy(self.profile)
        bad['main']['instance_path'] = 'OTHER.CORE.DUT'
        errors = self.check_netlist(bad)
        self.assertTrue(any(item['field']=='physical_profile.main.resolved_terminals' for item in errors))
        bad['main']['instance_path'] = 'ABSENT.CORE.DUT'
        errors = self.check_netlist(bad)
        self.assertTrue(any(item.get('matches') == 0 for item in errors))

    def test_local_terminal_or_model_forgery_is_still_rejected(self):
        for field, value in [('device_subckt','wrong_mos'), ('target_instance','PARTNER')]:
            bad = copy.deepcopy(self.profile)
            bad['main'][field] = value
            self.assertTrue(self.check_netlist(bad))
        bad = copy.deepcopy(self.profile)
        bad['main']['terminals']['source'] = 'TOP.floating_source'
        self.assertTrue(self.check_netlist(bad))

    def test_partial_or_extra_resolved_evidence_fails_closed(self):
        for pins in ({'drain':'VOUT_P'}, dict(self.profile['main']['resolved_terminals'], typo='x')):
            bad = copy.deepcopy(self.profile)
            bad['main']['resolved_terminals'] = pins
            with self.assertRaises(ProfileError):
                validate_physical_profile(bad)

    def test_legacy_profile_does_not_gain_unproven_alias_acceptance(self):
        bad = copy.deepcopy(self.profile)
        del bad['main']['resolved_terminals']
        with self.assertRaises(ProfileError):
            validate_physical_profile(bad)
        flat = fixtures.PortableSetupTests()
        flat.setUp()
        try:
            profile = flat.build()['engine']['physical_profile']
            del profile['main']['resolved_terminals']
            validate_physical_profile(profile)
        finally:
            flat.tearDown()

    def test_private_preparation_edits_local_pins_and_saves_resolved_trace(self):
        source = self.fixture.vco.read_text()
        spec = importlib.util.spec_from_file_location(
            'hierarchical_alias_engine_test',
            Path(__file__).resolve().parents[1] / 'scripts/dynamic_isf_closure_engine.py',
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = self.fixture.root / 'private' / 'input.scs'
        module.prepare_main_netlist(self.engine, self.fixture.vco, output)
        text = output.read_text()
        self.assertIn('DUT (ISF_DRAIN D2 TAIL TAIL)', text)
        self.assertIn('I_ISF_INJECT (D1 TAIL)', text)
        self.assertIn('TOP.floating_source', text)
        self.assertEqual(self.fixture.vco.read_text(), source)


if __name__ == '__main__':
    unittest.main()
