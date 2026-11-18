"""Pre-merge review regressions using frozen evidence; never run Spectre."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from isf_tool.metrics import select_point
from isf_tool.optim import calibrate, evaluate, gate
from isf_tool.optim.adapters import design_lab_evaluator
from isf_tool.optim.schema import validate
from test_optimization_metrics import synthetic_points


class OptimReviewRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sample = next(p for p in synthetic_points() if p['name'] == 'A')

    def reference(self):
        return copy.deepcopy(dict(result=self.sample['result'], point=self.sample['point']))

    def evaluation(self, ref, level='L2'):
        _, _, summary = select_point(ref['result'], point=ref['point'])
        return evaluate(ref, level=level, offsets_hz=[summary['offset_Hz']])

    def test_baseline_still_qualifies(self):
        result = self.evaluation(self.reference())
        self.assertTrue(result['gate']['isf_shaping_allowed'])
        validate(result)

    def test_quality_and_pm_flags_must_explicitly_pass(self):
        for field, code in [('numerical_quality_pass', 'ISF_NUMERICAL_QUALITY_UNQUALIFIED'),
                            ('pm_magnitude_projection_valid', 'ISF_PM_PROJECTION_UNQUALIFIED')]:
            for value in ('missing', None, False):
                with self.subTest(field=field, value=value):
                    ref = self.reference()
                    _, _, summary = select_point(ref['result'], point=ref['point'])
                    if value == 'missing':
                        summary.pop(field, None)
                    else:
                        summary[field] = value
                    result = self.evaluation(ref)
                    self.assertFalse(result['gate']['isf_shaping_allowed'])
                    self.assertIn(code, result['gate']['reason'])

    def test_l2_rejects_missing_isf_columns_and_l1_remains_closed(self):
        ref = self.reference()
        _, data, _ = select_point(ref['result'], point=ref['point'])
        for row in data['workpoint']:
            for key in ('isf_i', 'isf', 'm_dyn_A_rtHz'):
                row.pop(key, None)
        with self.assertRaisesRegex(ValueError, 'ISF_FLICKER_RHO_DYN_UNAVAILABLE'):
            self.evaluation(ref)
        result = self.evaluation(ref, 'L1')
        self.assertFalse(result['gate']['isf_shaping_allowed'])
        self.assertIn('ISF_FLICKER_C0_EFF_UNAVAILABLE', result['gate']['reason'])

    def test_l2_rejects_missing_or_nonfinite_qk(self):
        for value in (None, float('inf'), float('nan')):
            with self.subTest(value=value):
                ref = self.reference()
                _, data, _ = select_point(ref['result'], point=ref['point'])
                data['harmonics'][0]['Q_real_V_rtHz'] = value
                with self.assertRaisesRegex(ValueError, 'ISF_HARMONIC_Q_UNAVAILABLE'):
                    self.evaluation(ref)

    def test_harmonic_orders_must_be_complete_and_unique(self):
        cases = {
            'drop_k0': ('ISF_HARMONIC_ORDERS_INCOMPLETE', lambda hs: [h for h in hs if h['k'] != 0]),
            'drop_k1': ('ISF_HARMONIC_ORDERS_INCOMPLETE', lambda hs: [h for h in hs if h['k'] != 1]),
            'drop_k3': ('ISF_HARMONIC_ORDERS_INCOMPLETE', lambda hs: [h for h in hs if h['k'] != 3]),
            'keep_only_k1': ('ISF_HARMONIC_ORDERS_INCOMPLETE', lambda hs: [h for h in hs if h['k'] == 1]),
            'duplicate_k1': ('ISF_HARMONIC_ORDER_DUPLICATE', lambda hs: hs + [copy.deepcopy(hs[1])]),
            'replace_k3_with_k1': ('ISF_HARMONIC_ORDER_DUPLICATE', lambda hs: hs[:3] + [copy.deepcopy(hs[1])]),
            'extra_k4': ('ISF_HARMONIC_ORDERS_INCOMPLETE', lambda hs: hs + [dict(hs[-1], k=4)]),
        }
        for case, (code, mutate) in cases.items():
            with self.subTest(case=case):
                ref = self.reference()
                ref['max_harmonic'] = 3
                _, data, _ = select_point(ref['result'], point=ref['point'])
                data['harmonics'] = mutate(data['harmonics'])
                with self.assertRaisesRegex(ValueError, code):
                    self.evaluation(ref)
                diagnostic = self.evaluation(ref, 'L1')
                self.assertFalse(diagnostic['metrics'][0]['quality']['flicker_available'])
                self.assertFalse(diagnostic['gate']['isf_shaping_allowed'])
                self.assertIn(code, diagnostic['gate']['reason'])

    def test_default_range_does_not_shrink_with_surviving_rows(self):
        ref = self.reference()
        _, data, _ = select_point(ref['result'], point=ref['point'])
        data['harmonics'] = data['harmonics'][:2]
        with self.assertRaisesRegex(ValueError, 'ISF_HARMONIC_ORDERS_INCOMPLETE'):
            self.evaluation(ref)

    def test_declared_harmonic_range_is_retained_and_rechecked_by_gate(self):
        ref = self.reference()
        ref['max_harmonic'] = 4
        _, data, _ = select_point(ref['result'], point=ref['point'])
        with self.assertRaisesRegex(ValueError, 'ISF_HARMONIC_ORDERS_INCOMPLETE'):
            self.evaluation(ref)
        data['harmonics'].append(dict(data['harmonics'][-1], k=4, Q_real_V_rtHz=0., Q_imag_V_rtHz=0.))
        result = self.evaluation(ref)
        self.assertTrue(result['gate']['isf_shaping_allowed'])
        metric = result['metrics'][0]
        self.assertEqual(metric['source']['max_harmonic'], 4)
        self.assertTrue(gate(json.loads(json.dumps(metric)))['isf_shaping_allowed'])
        for order in ('0', '1', '4'):
            with self.subTest(order=order):
                damaged = copy.deepcopy(metric)
                damaged['flicker']['c_k'].pop(order)
                self.assertTrue(damaged['quality']['flicker_available'])
                decision = gate(damaged)
                self.assertFalse(decision['isf_shaping_allowed'])
                self.assertIn('ISF_HARMONIC_ORDERS_INCOMPLETE', decision['reason'])

    def test_invalid_harmonic_range_and_row_orders_are_structured_errors(self):
        baseline = self.evaluation(self.reference())['metrics'][0]
        for maximum in (None, True, 0, -1, 3.5, '3', 13):
            with self.subTest(maximum=maximum):
                ref = self.reference()
                ref['max_harmonic'] = maximum
                with self.assertRaisesRegex(ValueError, 'OPTIM_CONTRACT /max_harmonic'):
                    self.evaluation(ref)
                metric = copy.deepcopy(baseline)
                metric['source']['max_harmonic'] = maximum
                self.assertIn('ISF_HARMONIC_RANGE_INVALID', gate(metric)['reason'])
        for order in (None, True, -1, 1.5, 1.0, '-1', '1.0', 'x', ''):
            with self.subTest(order=order):
                ref = self.reference()
                _, data, _ = select_point(ref['result'], point=ref['point'])
                data['harmonics'][1]['k'] = order
                with self.assertRaisesRegex(ValueError, 'ISF_HARMONIC_ORDER_INVALID'):
                    self.evaluation(ref)

    def test_canonical_string_orders_are_accepted(self):
        # Canonical bundles written by dual_tb store k as decimal strings.
        baseline = self.evaluation(self.reference())
        for kind in (str,):
            with self.subTest(kind=kind.__name__):
                ref = self.reference()
                _, data, _ = select_point(ref['result'], point=ref['point'])
                for row in data['harmonics']:
                    row['k'] = kind(int(row['k']))
                result = self.evaluation(ref)
                self.assertEqual(baseline['metrics'][0]['flicker']['rho_dyn'],
                                 result['metrics'][0]['flicker']['rho_dyn'])

    def test_l2_rejects_missing_thermal(self):
        ref = self.reference()
        _, _, summary = select_point(ref['result'], point=ref['point'])
        summary.pop('thermal_noise')
        with self.assertRaisesRegex(ValueError, 'ISF_THERMAL_UNAVAILABLE'):
            self.evaluation(ref)

    def test_l2_requires_pair_and_whole_vco_hbnoise(self):
        for keys, code in [
            (('spectre_pair_pm_asd_V_rtHz', 'spectre_pair_fn_pm_asd_V_rtHz', 'spectre_pair_dBc_Hz'),
             'ISF_PAIR_REFERENCE_UNAVAILABLE'),
            (('spectre_noise_reference',), 'ISF_HBNOISE_REFERENCE_UNAVAILABLE')]:
            with self.subTest(keys=keys):
                ref = self.reference()
                _, _, summary = select_point(ref['result'], point=ref['point'])
                for key in keys:
                    summary.pop(key, None)
                with self.assertRaisesRegex(ValueError, code):
                    self.evaluation(ref)

    def test_gate_rechecks_finite_data_even_with_quality_flags(self):
        baseline = self.evaluation(self.reference())['metrics'][0]
        for section, field, code in [('flicker', 'rho_dyn', 'ISF_FLICKER_RHO_DYN_UNAVAILABLE'),
                                     ('flicker', 'c0_eff', 'ISF_FLICKER_C0_EFF_UNAVAILABLE'),
                                     ('closure', 'spectre_total_dbc_hz', 'ISF_HBNOISE_REFERENCE_UNAVAILABLE')]:
            for value in (None, float('inf'), float('nan')):
                with self.subTest(field=field, value=value):
                    metric = copy.deepcopy(baseline)
                    metric[section][field] = value
                    decision = gate(metric)
                    self.assertFalse(decision['isf_shaping_allowed'])
                    self.assertIn(code, decision['reason'])
        metric = copy.deepcopy(baseline)
        metric['flicker']['c_k']['1'] = None
        self.assertIn('ISF_HARMONIC_Q_UNAVAILABLE', gate(metric)['reason'])
        metric = copy.deepcopy(baseline)
        metric['quality'].pop('flicker_available')
        self.assertFalse(gate(metric)['isf_shaping_allowed'])

    def test_adapter_na_objectives_return_contract(self):
        checks = lambda p, d: dict(status='completed', feasible=True, total_violation=0)
        for level, field in [('L0', 'rho_hat'), ('L1', 'pn_dbc_hz'), ('L2', 'rho_dyn')]:
            for value in (None, float('inf'), float('nan'), True, 'N/A'):
                with self.subTest(level=level, value=value):
                    metric = {'flicker': {field: value}} if level == 'L2' else {field: value}
                    result = dict(metrics=[metric])
                    with mock.patch('isf_tool.optim.adapters.isf_evaluate', return_value=result):
                        out = design_lab_evaluator(lambda p, d: {}, level=level, constraints=checks)({}, '.')
                    self.assertEqual(out['status'], 'not_evaluated')
                    self.assertFalse(out['feasible'])
                    self.assertGreater(out['total_violation'], 0)
                    self.assertIsNone(out['merit'])
                    self.assertEqual(out['objectives'], [])
                    self.assertEqual(out['evidence']['isf'], result)

    def test_live_calibration_acquires_and_selects_same_offset(self):
        for fields in ({'offset_hz': 1e6}, {'offsets_hz': [1e6]},
                       {'offset_hz': 1e6, 'offsets_hz': [1e6]}):
            with self.subTest(fields=fields):
                acquired = self.reference()
                _, _, summary = select_point(acquired['result'], point=acquired['point'])
                summary['offset_Hz'] = 1e6
                ref = dict(kind='native_spec', spec_path='unused.json', workspace='/unused', **fields)
                with mock.patch('isf_tool.optim.api.evaluate', return_value=dict(reference=acquired)) as acquire:
                    result = calibrate(ref)
                self.assertEqual(acquire.call_args.kwargs['offsets_hz'], [1e6])
                self.assertEqual(result['source']['offset_hz'], 1e6)

    def test_calibration_offset_conflicts_fail_before_acquisition(self):
        for fields in ({'offset_hz': 1e6, 'offsets_hz': [1e5]}, {'offsets_hz': [1e5, 1e6]},
                       {'offset_hz': -1}, {'offsets_hz': []}):
            with self.subTest(fields=fields), mock.patch('isf_tool.optim.api.evaluate') as acquire:
                with self.assertRaises(ValueError):
                    calibrate(dict(kind='native_spec', spec_path='unused.json', workspace='/unused', **fields))
                acquire.assert_not_called()

    def test_evaluate_offset_conflict_creates_no_workspace_or_process(self):
        with tempfile.TemporaryDirectory() as directory:
            for fields in ({'offset_hz': 1e6}, {'offsets_hz': [1e6]}):
                with self.subTest(fields=fields), mock.patch('isf_tool.optim.api.subprocess.run') as runner:
                    with self.assertRaisesRegex(ValueError, 'OPTIM_OFFSET_CONFLICT'):
                        evaluate(dict(kind='native_spec', spec_path='unused.json', workspace=directory, **fields), level='L2')
                    runner.assert_not_called()
                    self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cli_nonobject_inputs_are_json_errors(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'input.json'
            for value in ([], None, 'text', 42, True):
                path.write_text(json.dumps(value), encoding='utf-8')
                for action in ('evaluate', 'calibrate', 'gate', 'rank-knobs'):
                    with self.subTest(value=value, action=action):
                        args = [sys.executable, '-B', 'run.py', 'optim', action, str(path)]
                        if action == 'rank-knobs':
                            args.append(str(path))
                        proc = subprocess.run(args, cwd=root, capture_output=True, text=True)
                        self.assertEqual(proc.returncode, 2, proc.stderr)
                        error = json.loads(proc.stdout)
                        self.assertEqual(error['schema'], 'isf.error.v1')
                        self.assertEqual(error['status'], 'failed')
                        self.assertIn('expected a JSON object', error['message'])
