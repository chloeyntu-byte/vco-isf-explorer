import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from isf_tool.api import _filter_results
from isf_tool.core.errors import PreflightError, ValidationError
from isf_tool.core.maestro_pair import _verify_model_includes
from isf_tool.core.result_presentation import operating_points, sweep_summary
from isf_tool.core.service import ToolService
from isf_tool.core.sweep_points import catalogue, point_key, run_points, select_points
from isf_tool.example_data import result
from isf_tool.metrics import select_point
from isf_tool.tk.sweep_view import core_data, sweep_traces


def sources():
    return [dict(operating_point=dict(corner=corner, params={'BIAS': value, 'VDD': 1.2},
                                     rdb='/fixture/history.rdb', point_id=index, test='ISF', test_id=5))
            for index, (corner, value) in enumerate((('TT', '1m'), ('TT', '2m'), ('hot', '1m')), 1)]


class SweepIdentityTests(unittest.TestCase):
    def test_coordinates_preserve_native_values_and_are_independent_of_order(self):
        rows = catalogue(sources())
        reverse = catalogue(list(reversed(sources())))
        self.assertEqual({item['id'] for item in rows}, {item['id'] for item in reverse})
        self.assertEqual(rows[0]['id'], point_key('TT', {'VDD': 1.2, 'BIAS': '1m'}))
        self.assertEqual(rows[0]['variables'], {'BIAS': '1m'})
        self.assertEqual((rows[0]['history'], rows[0]['point_id']), ('/fixture/history.rdb', 1))
        self.assertEqual(select_points(rows, [rows[1]['id']]), [rows[1]])
        with self.assertRaises(ValidationError):
            select_points(rows, ['a different history'])
        with self.assertRaises(ValidationError):
            catalogue([sources()[0], sources()[0]])

    def test_ic618_model_group_must_match_exact_point_netlist_includes(self):
        selected = 'File=All#Global#/fixture/lib.scs Section="slow";'
        self.assertEqual(_verify_model_includes(selected, {'model_sections': {'/fixture/lib.scs': ['slow']}}),
                         {'/fixture/lib.scs': ['slow']})
        with self.assertRaisesRegex(ValidationError, 'do not match'):
            _verify_model_includes(selected, {'model_sections': {'/fixture/lib.scs': ['fast']}})

    def test_partial_failure_publishes_completed_point_and_retries_only_failure(self):
        points = catalogue(sources())[:2]
        calls = []
        completed = {}

        class Child:
            def __init__(self, workspace, **kwargs):
                self.key = workspace.name

            def start_run(self, payload):
                source = payload['config']['testbenches']['vco']
                assert source['point_key'] == self.key
                assert source['history_fingerprint'] == 'frozen'
                calls.append(self.key)
                if self.key == points[1]['id'] and calls.count(self.key) == 1:
                    raise RuntimeError('Spectre failed for this point')
                summary = dict(point=self.key, operating_point_id=self.key, offset_Hz=10000)
                completed[self.key] = dict(points={self.key: dict(summary=[summary])}, summary=[summary])
                return self.key

            def get_result(self, child_id):
                return completed[child_id]

            def get_job(self, child_id):
                return {'status': 'succeeded'}

        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            service = ToolService(path)
            service._jobs['parent'] = {}
            service._persist_job = lambda _: None
            cfg = dict(testbenches={'vco': {}},
                       point_batch=dict(points=points, history_fingerprint='frozen'))
            payload = dict(run_root=root)
            with patch('isf_tool.core.service.ToolService', Child):
                with self.assertRaisesRegex(PreflightError, 'Retry failed points'):
                    run_points(service, 'parent', payload, cfg, lambda *args: None)
                checkpoint = json.loads((path / '.isf-point-sweep.json').read_text())
                self.assertEqual([p['status'] for p in checkpoint['points']], ['completed', 'failed'])
                partial = service._jobs['parent']['result']
                self.assertEqual(list(partial['points']), [points[0]['id']])
                final = run_points(service, 'parent', dict(payload, resume=True), cfg, lambda *args: None)
            self.assertEqual(calls, [points[0]['id'], points[1]['id'], points[1]['id']])
            self.assertEqual(len(final['points']), 2)

    def test_filtered_asymmetric_result_keeps_only_selected_closure(self):
        combined = [dict(operating_point_id=point, offset_hz=offset)
                    for point in ('A', 'B') for offset in (10000, 20000)]
        samples = [dict(point=f"MN1:{c['operating_point_id']}@{c['offset_hz']}",
                        core_operating_point_id=c['operating_point_id'], offset_Hz=c['offset_hz'])
                   for c in combined]
        raw = dict(summary=samples, points={sample['point']: {} for sample in samples},
                   complementary_core=dict(combined=combined))
        filtered = _filter_results(raw, ['B'], [20000])
        self.assertEqual(filtered['summary'], [samples[-1]])
        self.assertEqual(filtered['complementary_core']['combined'], [combined[-1]])
        self.assertEqual(len(raw['complementary_core']['combined']), 4)

    def test_synthetic_sweep_carries_exact_offsets_and_complete_core_data(self):
        raw = result(sweep=True)
        points = operating_points(raw)
        self.assertEqual(len(points), 6)
        rows = sweep_summary(raw, points, 10000.0)
        self.assertEqual(len({r['f0'] for r in rows}), 6)
        self.assertTrue(all(r['isf'] is not None and r['hbnoise'] is not None for r in rows))
        self.assertEqual(len(sweep_traces(rows, 'VBIAS')), 4)
        for row in rows:
            key, _, summary = select_point(raw, point=row['id'], offset_hz=10000)
            self.assertEqual(summary['operating_parameters'], row['variables'])
            self.assertEqual(list(_filter_results(raw, [row['id']], [10000])['points']), [key])
        captions = {name for name, _, _ in core_data(raw, rows[0])}
        self.assertIn('Γrms / c0_eff / ρ_dyn', captions)
        self.assertIn('Flicker pair error', captions)
        self.assertEqual(len(operating_points(result(single=True))), 1)
