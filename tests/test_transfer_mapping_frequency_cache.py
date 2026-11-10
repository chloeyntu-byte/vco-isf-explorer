"""The selected transfer branch and requested frequency must be truly consumed."""

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from isf_tool.core.signal_mapping import discover_signal_mapping, transfer_output_reference
from isf_tool.core.portable_setup import build_portable_setup
from tests import test_portable_setup as setup_fixtures

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('transfer_frequency_engine', ROOT / 'scripts/dynamic_isf_closure_engine.py')
ENGINE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ENGINE)


class TransferMappingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = setup_fixtures.PortableSetupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.vco.write_text(self.fixture.vco.read_text() +
            'I_OLD (COMMON VOUT_P) isource type=sine\n'
            'I_WRONG (COMMON GATE_A) isource type=sine\n'
            'hbxf (VOUT_P COMMON) hbxf values=[10k] annotate=status\n')
        self.pair = self.fixture.reports()
        test = self.pair['roles']['vco']['selected_test']
        test['outputs'].extend([
            {'name': 'mag_pxf0', 'expression': 'abs(harmonic(getData("/I_OLD" ?result "hbxf") 0))'},
            {'name': 'wrong_branch', 'expression': 'abs(harmonic(getData("/I_WRONG" ?result "hbxf") 0))'},
            {'name': 'wrong_sideband', 'expression': 'abs(harmonic(getData("/I_OLD" ?result "hbxf") 1))'},
            {'name': 'unused_metric', 'expression': 'average(VT("GATE_A"))'},
        ])

    def map(self, overrides=None):
        old = self.pair['roles']['vco']
        value = discover_signal_mapping(old['selected_test'], 'vco', old['selected_history'], overrides=overrides)
        value.update(selected_test=old['selected_test'], selected_history=old['selected_history'])
        self.pair['roles']['vco'] = value
        return value

    def test_auto_anchor_proves_reverse_branch_and_declares_remeasurement(self):
        result = self.map()
        transfer = result['mapping']['transfer_contract']
        self.assertEqual('mag_pxf0', result['mapping']['transfer_output'])
        self.assertEqual('I_OLD', transfer['source'])
        self.assertEqual(-1, transfer['direction_factor'])
        self.assertEqual(['VOUT_P', 'COMMON'], transfer['original_output'])
        self.assertEqual(['VOUT_P', 'VOUT_N'], transfer['effective_output'])
        self.assertEqual('source_binding_remeasured', transfer['consumption_mode'])

    def test_declared_branch_and_sideband_are_not_rejected(self):
        for name in ('wrong_branch', 'wrong_sideband'):
            with self.subTest(name=name):
                result = self.map({'transfer_output': name})
                self.assertEqual('existing_source', result['mapping']['transfer_contract']['mode'])
                self.assertEqual(name, result['mapping']['transfer_contract']['output_name'])
                self.assertFalse(any(issue['key'] == 'transfer_output' and issue['severity'] == 'error' for issue in result['mapping_issues']))
                self.assertTrue(build_portable_setup(self.pair, self.fixture.config, self.fixture.environment)['ok'])

    def test_unreadable_or_unknown_output_never_falls_back(self):
        for name in ('unused_metric', 'does_not_exist'):
            with self.subTest(name=name):
                result = self.map({'transfer_output': name})
                self.assertEqual('invalid', result['mapping']['transfer_contract']['mode'])
                self.assertTrue(any(issue['key'] == 'transfer_output' and issue['severity'] == 'error' for issue in result['mapping_issues']))
                setup = build_portable_setup(self.pair, self.fixture.config, self.fixture.environment)
                self.assertFalse(setup['ok'])

    def test_mapping_fields_expose_use_and_keep_full_catalog(self):
        result = self.map()
        fields = {field['key']: field for field in result['mapping_fields']}
        self.assertTrue(fields['transfer_output']['active'])
        self.assertEqual('signal', fields['transfer_output']['group'])
        self.assertEqual('source_binding_remeasured', fields['transfer_output']['consumption_mode'])
        self.assertFalse(fields['noise_output_at_offset']['active'])
        self.assertFalse(fields['current_trace']['active'])
        self.assertIn('unused_metric', [item['name'] for item in result['signal_catalog']])

    def test_explicit_invalid_reference_is_not_hidden_by_used_only_filter(self):
        result = self.map({'noise_output_at_offset': 'deleted_output'})
        field = next(item for item in result['mapping_fields'] if item['key'] == 'noise_output_at_offset')
        self.assertTrue(field['active'])
        self.assertEqual('invalid', field['status'])

    def test_private_preparation_keeps_original_source_and_remeasures_output(self):
        self.map()
        setup = build_portable_setup(self.pair, self.fixture.config, self.fixture.environment)
        self.assertTrue(setup['ok'], setup['errors'])
        cfg = setup['engine']
        original = self.fixture.vco.read_text()
        target = self.fixture.root / 'prepared/input.scs'
        ENGINE.prepare_main_netlist(cfg, self.fixture.vco, target)
        self.assertIn('I_OLD (COMMON VOUT_P) isource type=sine', target.read_text())
        self.assertIn('hbxf (VOUT_P VOUT_N)', target.read_text())
        self.assertEqual(original, self.fixture.vco.read_text())
        self.assertNotEqual('I_OLD', cfg['physical_profile']['main']['injection']['source'])
        self.assertEqual('I_OLD', cfg['transfer_source'])

    def test_parser_does_not_evaluate_composed_or_arbitrary_calculator_code(self):
        self.assertIsNotNone(transfer_output_reference('abs(harmonic(getData("/I0" ?result "custom") 0))'))
        for expression in ('abs(harmonic(getData("/I0" ?result "hbxf") 0))*2', 'shell("evil")', 'mag_pxf0'):
            self.assertIsNone(transfer_output_reference(expression))


class ExactFrequencyTests(unittest.TestCase):
    def test_requested_coordinate_not_first_row_is_consumed(self):
        self.assertEqual(2j, ENGINE.exact_frequency_value([(1e3, 1j), (1e4, 2j)], 1e4))
        for rows, target in [([(1e3, 1j)], 1e4), ([(1e4, 1j), (1e4, 2j)], 1e4), ([], 1e4)]:
            with self.assertRaises(RuntimeError):
                ENGINE.exact_frequency_value(rows, target)

    def test_noise_missing_requested_frequency_is_not_nearest_neighbor(self):
        with mock.patch.object(ENGINE, 'parse_struct', return_value={1e3: [0, 0, 0, 1e-20]}):
            with self.assertRaises(RuntimeError):
                ENGINE.struct_value_at({}, 'noise.noise', 'flicker', 1e4, 3)

    def test_hbxf_uses_exact_fd_fundamental_full_complex_and_reverse_factor(self):
        precise = 5890732107.400799
        cfg = {'offset_hz': 1e4, 'vco_mapping': {'transfer_contract': {'mode': 'existing_source', 'source': 'I_OLD', 'direction_factor': -1}}}
        observed = []
        def parse(_cfg, path, trace):
            observed.append(trace)
            return [(precise + 1e3, 99j), (precise + 1e4, 2 + 3j)]
        with mock.patch.object(ENGINE, 'exact_hb_fundamental', return_value=precise), mock.patch.object(ENGINE, 'parse_complex_wave', side_effect=parse), mock.patch.object(Path, 'exists', return_value=True):
            result = ENGINE.parse_hbxf(cfg, Path('/raw'), 'PRIVATE', 1)
        self.assertEqual({-1: -2-3j, 0: -2-3j, 1: -2-3j}, result)
        self.assertEqual(['I_OLD'] * 3, observed)

    def test_fd_axis_corrects_rounded_header_but_rejects_harmonic_index_axis(self):
        exact = 5890732107.400799
        with mock.patch.object(ENGINE, 'fundamental', return_value=5890730000), mock.patch.object(ENGINE, 'parse_complex_wave', return_value=[(0, 0j), (exact, 1j), (2 * exact, .1j)]):
            self.assertEqual(exact, ENGINE.exact_hb_fundamental({}, '/raw', 'OUT'))
        with mock.patch.object(ENGINE, 'fundamental', return_value=5890730000), mock.patch.object(ENGINE, 'parse_complex_wave', return_value=[(0, 0j), (1, 1j), (2, .1j)]):
            with self.assertRaises(RuntimeError):
                ENGINE.exact_hb_fundamental({}, '/raw', 'OUT')

    def test_sideband_frequency_tolerance_does_not_hide_wrong_small_offset(self):
        f0 = 5890732107.400799
        with self.assertRaises(RuntimeError):
            ENGINE.exact_frequency_value([(f0 + 1e3, 1j)], f0 + 1e4, 1e4)
        self.assertEqual(1j, ENGINE.exact_frequency_value([(f0 + 1e4 + 1e-6, 1j)], f0 + 1e4, 1e4))

    def test_hbac_reads_each_output_sideband_coordinate(self):
        f0, offset = 5890730000.0, 10000.0
        def parse(cfg, path, trace):
            k = int(path.name.split('.')[1])
            return [(k * f0 + 1000.0, 99j), (k * f0 + offset, complex(k, 1))]
        with mock.patch.object(ENGINE, 'exact_hb_fundamental', return_value=f0), mock.patch.object(ENGINE, 'parse_complex_wave', side_effect=parse):
            self.assertEqual({0: 1j, 1: 1+1j, 2: 2+1j}, ENGINE.parse_hbac_current({'offset_hz': offset}, '/raw', 'SENSE:p', 2))


class DcSampleCacheTests(unittest.TestCase):
    def setUp(self):
        self.fixture = setup_fixtures.PortableSetupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.cfg = self.fixture.build()['engine']
        self.cfg.update(source_root=str(self.fixture.root), flicker_template_netlist=str(self.fixture.replica), dc_noise_samples=8)
        self.point = {'source_dir': 'source', 'corner': 'tt', 'k': 1}
        (self.fixture.root / 'source/vco/netlist').mkdir(parents=True)
        (self.fixture.root / 'source/vco/netlist/input.scs').write_text(self.fixture.vco.read_text())
        self.wave = {'time': np.linspace(0, 1e-9, 9), 'period': 1e-9, 'f0': 1e9}
        traces = self.cfg['physical_profile']['main']['traces']
        for key, value in [('gate', .6), ('drain', .8), ('source', 0), ('bulk', 0)]:
            self.wave[traces[key]] = np.full(9, value)
        self.case = self.fixture.root / 'case'

    def fake_spectre(self, cfg, netlist_dir, force=False):
        base = Path(netlist_dir).parent
        psf = base / 'psf'
        psf.mkdir(exist_ok=True)
        (psf / '.simDone').write_text('complete')
        (psf / 'spectre.out').write_text('spectre completes with 0 errors')
        (psf / 'noise.noise').write_text('synthetic noise artifact')
        (base / 'run_environment.json').write_text(json.dumps({'cache_identity': ENGINE._simulation_cache_identity(cfg, netlist_dir)}))
        return 'simulated'

    def test_valid_sample_cache_reuses_without_execution_and_changed_offset_fails(self):
        with mock.patch.object(ENGINE, 'run_spectre', side_effect=self.fake_spectre) as run:
            self.assertEqual('generated', ENGINE.ensure_dc_flicker_samples(self.cfg, self.point, self.case, self.wave))
            self.assertEqual(8, run.call_count)
        with mock.patch.object(ENGINE, 'run_spectre') as run:
            self.assertEqual('cached', ENGINE.ensure_dc_flicker_samples(self.cfg, self.point, self.case, self.wave))
            run.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'DC sample cache'):
            ENGINE.ensure_dc_flicker_samples(dict(self.cfg, offset_hz=1e5), self.point, self.case, self.wave)

    def test_wave_template_and_output_changes_do_not_reuse(self):
        with mock.patch.object(ENGINE, 'run_spectre', side_effect=self.fake_spectre):
            ENGINE.ensure_dc_flicker_samples(self.cfg, self.point, self.case, self.wave)
        altered = copy.deepcopy(self.wave)
        trace = self.cfg['physical_profile']['main']['traces']['gate']
        altered[trace][1] += .1
        with self.assertRaisesRegex(RuntimeError, 'DC sample cache'):
            ENGINE.ensure_dc_flicker_samples(self.cfg, self.point, self.case, altered)
        noise = self.case / 'dc_flicker/sample_01/psf/noise.noise'
        noise.write_text('changed')
        with self.assertRaisesRegex(RuntimeError, 'DC sample cache'):
            ENGINE.ensure_dc_flicker_samples(self.cfg, self.point, self.case, self.wave)

    def test_legacy_marker_alone_cannot_claim_a_cache_hit(self):
        root = self.case / 'dc_flicker'
        root.mkdir(parents=True)
        (root / '.samplesDone').write_text('old unversioned done')
        with self.assertRaisesRegex(RuntimeError, 'unversioned'):
            ENGINE.ensure_dc_flicker_samples(self.cfg, self.point, self.case, self.wave)

    def test_input_identity_covers_model_template_mapping_sampling_and_corner(self):
        original = ENGINE._dc_samples_identity(self.cfg, self.point, self.wave, 8)
        for cfg, point, count in [(dict(self.cfg, offset_hz=1e5), self.point, 8),
                                  (dict(self.cfg, flicker_mapping={'vgs_variable': 'CHANGED'}), self.point, 8),
                                  (self.cfg, dict(self.point, corner='ff'), 8), (self.cfg, self.point, 16)]:
            self.assertNotEqual(original, ENGINE._dc_samples_identity(cfg, point, self.wave, count))
        self.fixture.replica.write_text(self.fixture.replica.read_text() + '// template changed\n')
        self.assertNotEqual(original, ENGINE._dc_samples_identity(self.cfg, self.point, self.wave, 8))
        self.fixture.model.write_text(self.fixture.model.read_text() + '// model changed\n')
        self.assertNotEqual(original, ENGINE._dc_samples_identity(self.cfg, self.point, self.wave, 8))


if __name__ == '__main__':
    unittest.main()
