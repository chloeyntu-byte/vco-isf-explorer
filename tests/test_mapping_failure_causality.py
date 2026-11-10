"""Root mapping failures stay blocking without masquerading as PDK/PWL damage."""
import copy
import unittest
from unittest import mock

from isf_tool.core.preflight import preflight
from isf_tool.core.waveform_exchange import inspect_waveform_exchange
from tests import test_portable_setup as fixtures
from tests.test_transfer_mapping_frequency_cache import ENGINE


class MappingFailureCausalityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PortableSetupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def report(self):
        result = self.fixture.reports()
        result.update(ok=False, issues=[{
            'severity': 'error', 'code': 'ISF-E1202',
            'detail': '所选相位输出的原始结果路径无法读取',
            'field': 'testbenches.vco.mapping_overrides.transfer_phase_output',
        }], pair={'pdk': {'name': 'test_pdk', 'root': str(self.fixture.root)}}, bindings={})
        result['roles']['vco']['mapping_issues'].append({
            'key': 'transfer_phase_output', 'severity': 'error',
            'message': result['issues'][0]['detail'],
        })
        for role in ('vco', 'flicker'):
            result['roles'][role]['selected_history'].update(name='test_history', complete_runs=1)
        return result

    def test_mapping_block_is_explicit_and_is_not_missing_pwl(self):
        exchange = inspect_waveform_exchange(self.report())
        self.assertFalse(exchange['ok'])
        self.assertEqual(['electrical_mapping'], exchange['blocked_by'])
        self.assertIn('受上游名称对应阻塞', exchange['summary'])
        self.assertIn('相位输出', exchange['summary'])
        self.assertIn('并非已确认缺少 PWL', exchange['summary'])

    def test_an_independent_missing_history_is_not_downgraded(self):
        report = self.report()
        report['roles']['vco']['selected_history'] = None
        exchange = inspect_waveform_exchange(report)
        self.assertFalse(exchange['ok'])
        self.assertFalse(exchange['blocked_by'])
        self.assertIn('VCO history', exchange['summary'])

    def test_preflight_keeps_root_failure_and_marks_only_dependents_blocked(self):
        config = dict(self.fixture.config, source_mode='maestro_pair', run_kind='replay',
                      execution={'dry_run': False, 'allow_execute': False},
                      testbenches={'vco': {'maestro_dir': str(self.fixture.vco.parent), 'binding_fingerprint': 'vco'},
                                   'replica': {'maestro_dir': str(self.fixture.replica.parent), 'binding_fingerprint': 'flicker'}})
        with mock.patch('isf_tool.core.preflight.inspect_maestro_pair', return_value=self.report()), \
             mock.patch('subprocess.Popen', side_effect=AssertionError('No simulation or active probes')):
            result = preflight(config, workspace=str(self.fixture.root))
        checks = {item['id']: item for item in result['checks']}
        self.assertFalse(result['ok'])
        self.assertEqual('fail', checks['maestro_vco']['status'])
        self.assertEqual('testbenches.vco.mapping_overrides.transfer_phase_output', checks['maestro_vco']['field'])
        for key in ('maestro_pair_contract', 'engine_config', 'waveform_exchange'):
            self.assertEqual('warn', checks[key]['status'], checks[key])
            self.assertIn('受上游', checks[key]['detail'])
            self.assertNotIn('modification hint', checks[key]['detail'])
        self.assertEqual('fail', checks['execution_authority']['status'])

    def test_private_preparation_trusts_declared_source_without_branch_requalification(self):
        cfg = self.fixture.build()['engine']
        cfg['vco_mapping']['transfer_contract'] = {
            'mode': 'existing_source', 'source': 'USER_CURRENT',
            'source_terminals': ['OTHER', 'COMMON'], 'direction_factor': 1,
            'effective_output': ['OLD_POS', 'OLD_NEG'], 'physical_validation': 'user_declared',
        }
        source = self.fixture.vco
        original = source.read_text()
        source.write_text(original + '\nUSER_CURRENT (OTHER COMMON) isource dc=0 type=dc\n')
        target = self.fixture.root / 'prepared/input.scs'
        ENGINE.prepare_main_netlist(cfg, source, target)
        self.assertIn('USER_CURRENT (OTHER COMMON) isource dc=0 type=dc', target.read_text())
        self.assertIn('hbxf (VOUT_P VOUT_N)', target.read_text())
        self.assertEqual(original + '\nUSER_CURRENT (OTHER COMMON) isource dc=0 type=dc\n', source.read_text())

    def test_source_collision_still_cannot_overwrite_user_current(self):
        cfg = self.fixture.build()['engine']
        cfg['vco_mapping']['transfer_contract'] = {
            'mode': 'existing_source', 'source': cfg['physical_profile']['main']['injection']['trace'],
            'direction_factor': 1,
        }
        with self.assertRaisesRegex(RuntimeError, 'may not overwrite'):
            ENGINE.prepare_main_netlist(cfg, self.fixture.vco, self.fixture.root / 'collision/input.scs')

    def test_lost_read_contract_never_silently_uses_private_source(self):
        for selected in ('transfer_output', 'transfer_phase_output'):
            for contract in (None, {}, {'mode': 'private_injection'}):
                with self.subTest(selected=selected, contract=contract):
                    cfg = self.fixture.build()['engine']
                    cfg['vco_mapping'] = {selected: 'selected_series', 'transfer_contract': contract}
                    with self.assertRaisesRegex(RuntimeError, 'no private-source fallback'):
                        ENGINE.prepare_main_netlist(cfg, self.fixture.vco, self.fixture.root / 'missing/input.scs')
                    with mock.patch.object(ENGINE, 'parse_complex_wave', side_effect=AssertionError('must not read another source')):
                        with self.assertRaisesRegex(RuntimeError, 'no private-source fallback'):
                            ENGINE.parse_hbxf(cfg, self.fixture.root, 'PRIVATE', 0)
        self.assertEqual({}, ENGINE._transfer_contract_for_reading({}))


if __name__ == '__main__':
    unittest.main()
