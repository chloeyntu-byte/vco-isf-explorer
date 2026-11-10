"""Grounded single-ended voltages need electrical evidence, not a VSS alias."""
import copy
import unittest

import test_portable_setup as fixtures
from isf_tool.core.signal_mapping import discover_signal_mapping, cross_tb_mapping, voltage_pair


class SingleEndedMappingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PortableSetupTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def report(self, source='GROUND_LINK (COMMON 0) vsource dc=0 type=dc', extra_history=None):
        report = self.fixture.reports()
        test = copy.deepcopy(report['roles']['vco']['selected_test'])
        test['outputs'][:2] = [
            {'name': 'gate_sample', 'expression': 'vtime(\'hb "GATE_A")'},
            {'name': 'drain_sample', 'expression': 'vtime(\'hb "VOUT_P")'},
        ]
        self.fixture.vco.write_text(self.fixture.vco.read_text() + source + '\n')
        history = copy.deepcopy(report['roles']['vco']['selected_history'])
        if extra_history:
            path = self.fixture.root / 'second.scs'
            path.write_text(self.fixture.vco.read_text().replace(source, extra_history))
            history['source_netlists'].append({'path': str(path)})
        role = discover_signal_mapping(test, 'vco', history)
        role.update(selected_test=test, selected_history=history)
        report['roles']['vco'] = role
        return report

    def test_single_ended_time_voltage_is_explicitly_ground_referenced(self):
        self.assertEqual(['gate', '0'], voltage_pair('vtime(\'hb "/gate")'))
        self.assertEqual(['gate', '0'], voltage_pair('VT("/gate")'))
        self.assertIsNone(voltage_pair('vh(\'hb "/gate")'))
        self.assertIsNone(voltage_pair('v("/gate" ?result "ac")'))
        self.assertIsNone(voltage_pair('vtime(\'ac "/gate")'))
        self.assertIsNone(voltage_pair('v(\'ac "/gate")'))
        self.assertIsNone(voltage_pair('v("/g" ?result "ac") - v("/s" ?result "ac")'))
        self.assertIsNone(voltage_pair('VT("/g" "/s" ?result "hb_fd")'))
        self.assertIsNone(voltage_pair('abs(vtime(\'hb "/gate"))'))
        self.assertIsNone(voltage_pair('-vtime(\'hb "/gate")'))

    def test_verified_grounded_source_selects_device_without_renaming_nodes(self):
        report = self.report()
        role = report['roles']['vco']
        self.assertEqual('DUT', role['mapping']['target_instance'])
        self.assertEqual('PARTNER', role['mapping']['peer_instance'])
        self.assertEqual('COMMON', role['mapping']['source'])
        self.assertEqual('gate_sample', role['mapping']['vgs_output'])
        self.assertEqual('mapped', cross_tb_mapping(role, report['roles']['flicker'])[0]['status'])
        self.assertEqual('GROUND_LINK', role['topology']['voltage_equivalence']['evidence'][0]['name'])
        self.assertNotIn('COMMON', role['topology']['net_aliases'])
        setup = self.fixture.build(report)
        self.assertTrue(setup['ok'], setup['errors'])
        self.assertEqual('COMMON', setup['engine']['physical_profile']['main']['terminals']['source'])

    def test_nonzero_time_varying_and_unknown_sources_cannot_prove_ground(self):
        for params in ('dc=0.1 type=dc', 'dc=0 type=sine ampl=1', 'dc=0 type=pwl wave=[0 0 1n 1]',
                       'dc=0 acmag=1', 'type=dc', 'dc=zero_parameter', 'dc=0 r=10'):
            with self.subTest(params=params):
                # Each report gets the pristine fixture netlist.
                initial = self.fixture.vco.read_text()
                try:
                    report = self.report('GROUND_LINK (COMMON 0) vsource ' + params)
                    self.assertIsNone(report['roles']['vco']['mapping']['target_instance'])
                    self.assertFalse(self.fixture.build(report)['ok'])
                finally:
                    self.fixture.vco.write_text(initial)

    def test_zero_proof_must_hold_across_history_points(self):
        report = self.report(extra_history='GROUND_LINK (COMMON 0) vsource dc=0.2 type=dc')
        self.assertIsNone(report['roles']['vco']['mapping']['target_instance'])
        self.assertFalse(report['roles']['vco']['topology']['voltage_equivalence']['evidence'])

    def test_chained_zero_branches_are_supported(self):
        report = self.report('G1 (COMMON MID) vsource dc=0.0 type=dc\nG2 (MID 0) vsource dc=0e0')
        self.assertEqual('DUT', report['roles']['vco']['mapping']['target_instance'])


if __name__ == '__main__':
    unittest.main()
