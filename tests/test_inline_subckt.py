"""Inline model aliases must agree from discovery through private injection."""
import importlib.util
from pathlib import Path
import unittest

import test_portable_setup as fixtures
from isf_tool.core.physical_profile import inspect_engine_contract


class InlineSubcircuitTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PortableSetupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        # An inline wrapper gives its same-named MOS the wrapper's PSF name.
        self.fixture.model.write_text(
            'section cornerA\ninline subckt portable_mos (d g s b)\n'
            'RG (g gi) resistor r=10\n'
            'portable_mos (di gi si bi) mos_primitive l=l w=w\n'
            'ends portable_mos\nendsection cornerA\n')

    def test_original_inline_trace_and_dynamic_hierarchical_trace_are_distinct(self):
        before = self.fixture.model.read_bytes()
        setup = self.fixture.build()
        self.assertTrue(setup['ok'], setup['errors'])
        engine = setup['engine']
        profile = engine['physical_profile']
        self.assertEqual('DUT', profile['main']['traces']['target_noise'])
        self.assertEqual('PARTNER', profile['main']['traces']['peer_noise'])
        self.assertEqual('NOISE_DUT', profile['main']['traces']['dc_noise'])
        self.assertEqual('M_ISF_REPLICA.portable_mos', profile['replica']['noise_trace'])
        checked = inspect_engine_contract(engine, require_explicit=True, check_paths=True,
            source_from_maestro=True, maestro_source_netlists=[{'path': str(self.fixture.vco)}],
            maestro_vco_mapping=engine['vco_mapping'], maestro_flicker_netlists=[{'path': str(self.fixture.replica)}],
            maestro_flicker_mapping=engine['flicker_mapping'])
        self.assertTrue(checked['ok'], checked['errors'])
        path = Path(__file__).resolve().parents[1] / 'scripts/dynamic_isf_closure_engine.py'
        spec = importlib.util.spec_from_file_location('inline_engine_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        private = module.injected_nmos_subckt(engine)
        self.assertIn('subckt portable_mos_isf_dynamic (d g s b)', private)
        self.assertNotIn('inline subckt', private)
        self.assertIn('portable_mos (di gi si bi) mos_primitive l=l w=w', private)
        self.assertIn('I_ISF_NOISE (di si) isource', private)
        self.assertIn('RG (g gi) resistor r=10', private)
        self.assertIn('ends portable_mos_isf_dynamic', private)
        self.assertEqual(before, self.fixture.model.read_bytes())


if __name__ == '__main__':
    unittest.main()
