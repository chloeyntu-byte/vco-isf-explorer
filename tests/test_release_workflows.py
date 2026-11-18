"""Release boundaries exercised without native tools or proprietary evidence."""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import unittest

from isf_tool.comparison import compare
from isf_tool.core.testbench import write
from isf_tool.tk.app import IsfApplication
from isf_tool.tk.controller import IsfController
from isf_tool.tk.viewmodel import load_thermal_coefficients
from test_optimization_metrics import synthetic_points


ROOT = Path(__file__).resolve().parents[1]


class ReleaseWorkflowTests(unittest.TestCase):
    def test_english_source_except_legacy_input_values(self):
        offenders = []
        cjk = re.compile(r'[\u3400-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]')
        for area in ('isf_tool', 'cadence', 'scripts'):
            for path in (ROOT / area).rglob('*'):
                if path.suffix not in ('.py', '.il', '.js', '.html', '.css', '.json'):
                    continue
                text = path.read_text(encoding='utf-8')
                if path.relative_to(ROOT).as_posix() == 'isf_tool/core/ui_state.py':
                    start = text.index('LEGACY_HARMONIC_CURVES = ')
                    end = text.index('def harmonic_curve_key', start)
                    text = text[:start] + text[end:]
                offenders.extend(f'{path.relative_to(ROOT)}:{i}' for i, line in enumerate(text.splitlines(), 1)
                                 if cjk.search(line))
        self.assertEqual([], offenders)

    def test_optional_resource_wrapper_preserves_argv(self):
        from unittest import mock

        from isf_tool.core.command_template import ENVIRONMENT_VARIABLE, wrap_command
        from isf_tool.core.errors import ValidationError

        command = ['python', 'worker.py', 'path containing spaces']
        with mock.patch.dict(os.environ, {ENVIRONMENT_VARIABLE: ''}):
            self.assertEqual(command, wrap_command(command, 'lib:example', 'sim:slots'))
        with mock.patch.dict(os.environ, {
            ENVIRONMENT_VARIABLE: '"/path with spaces/guard" {resource} -- {command}'
        }):
            self.assertEqual(
                ['/path with spaces/guard', 'lib:example', '--',
                 '/path with spaces/guard', 'sim:slots', '--', *command],
                wrap_command(command, 'lib:example', 'sim:slots'),
            )
        with mock.patch.dict(os.environ, {ENVIRONMENT_VARIABLE: 'guard {resource}'}):
            with self.assertRaisesRegex(ValidationError, 'exactly one'):
                wrap_command(command, 'sim:slots')

    def test_compare_requires_matching_operating_point_and_scopes(self):
        first, second = (copy.deepcopy(synthetic_points()[i]['result']) for i in (0, 2))
        response = compare(first, second, point='band_high', offset_hz=1e6)
        changes = {row['metric']: row for row in response['changes']}
        self.assertAlmostEqual(changes['Matched flicker ISF − HBnoise']['delta'], 0)
        self.assertAlmostEqual(changes['Matched thermal ISF − HBnoise']['delta'], 0)
        self.assertLess(changes['c0_eff']['delta'], 0)
        self.assertIn('Thermal B_m -2', changes)
        second['summary'][0]['operating_parameters']['code'] = 2
        with self.assertRaisesRegex(ValueError, 'same operating point'):
            compare(first, second, point='band_high')
        second['summary'][0]['operating_parameters']['code'] = 1
        second['summary'][0]['thermal_noise'] = None
        with self.assertRaisesRegex(ValueError, 'matching modeled source scopes'):
            compare(first, second, point='band_high')

    def test_metrics_qualifies_sweep_and_optimizer_accepts_schema_less_ref(self):
        from isf_tool.optim import gate

        record = synthetic_points()[4]
        raw = {'summary': record['result']['summary'], 'points': {record['point']: record['result']}}
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / 'sweep.json'
            ref_path = Path(directory) / 'ref.json'
            write(result_path, raw)
            write(ref_path, {'kind': 'artifact', 'result_path': str(result_path), 'point': 'nominal'})

            def cli(*args):
                return json.loads(subprocess.check_output([sys.executable, '-B', 'run.py', *args],
                                                          cwd=ROOT, text=True))

            metrics = cli('metrics', str(result_path), '--point', 'nominal',
                          '--offset-hz', '1000000', '--qualify', '--json')
            self.assertTrue(gate(metrics)['isf_shaping_allowed'])
            for path in (result_path, ref_path):
                evaluation = cli('optim', 'evaluate', str(path), '--point', 'nominal',
                                 '--offset-hz', '1000000', '--level', 'L2')
                self.assertEqual(metrics['closure'], evaluation['metrics'][0]['closure'])
                self.assertTrue(evaluation['gate']['isf_shaping_allowed'])

    def test_evaluate_accepts_the_same_device_sample_as_metrics(self):
        from isf_tool import api
        from isf_tool.optim import evaluate

        sample = synthetic_points()[0]['result']
        devices = ('XCORE.M1', 'XCORE.M2')
        raw = {'complementary_core': {'devices': [{'instance': name} for name in devices]},
               'points': {}, 'summary': []}
        for device in devices:
            data = copy.deepcopy(sample)
            row = data['summary'][0]
            key = device + ' | ' + row['point']
            row.update(point=key, operating_point_id=device + ' | band_high',
                       core_operating_point_id='band_high', device_instance=device,
                       device_polarity='nmos', pair_model='single_device_only',
                       target_noise_trace=device, dynamic_pm_asd_V_rtHz=(2e-14)**.5,
                       spectre_total_pm_asd_V_rtHz=(6e-14)**.5)
            thermal = row['thermal_noise']
            thermal.update(scope='selected_device', pair_model='single_device_only', target_instance=device,
                           target_noise_trace=device, peer_noise_trace=None, predicted_psd_V2_Hz=5e-15,
                           sources=[source for source in thermal['sources'] if source['device'] == device])
            raw['summary'].append(row)
            raw['points'][key] = data
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'two-device.json'
            write(path, raw)
            qualified = api.metrics(result_path=path, point='band_high', offset_hz=1e6,
                                    device=devices[1], qualify=True)
            response = evaluate({'result_path': str(path), 'point': qualified['source']['point']},
                                level='L2', offsets_hz=[1e6], device=devices[1])
            self.assertEqual([devices[1]], [row['source']['device_instance'] for row in response['metrics']])
            self.assertEqual(qualified['closure'], response['metrics'][0]['closure'])
            self.assertTrue(response['gate']['isf_shaping_allowed'])
            command = [sys.executable, '-B', 'run.py', 'optim', 'evaluate', str(path),
                       '--level', 'L2', '--point', 'band_high', '--device', devices[1], '--offset-hz', '1000000']
            cli = json.loads(subprocess.check_output(command, cwd=ROOT, text=True))
            self.assertEqual(response['metrics'], cli['metrics'])

    def test_thermal_coefficients_resolve_exact_sweep_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for job, offset in (('low', 1e5), ('high', 1e6)):
                child = root / job
                path = child / 'jobs' / job / 'simulation' / 'band_high' / 'thermal' / 'source_coefficients.json'
                write(path, {'sources': [{'device': job, 'mechanism': 'id',
                                          'source_time_s': [0.0], 'source_psd_A2_Hz': [1e-15]}]})
            children = [{'job_id': job, 'offset_hz': offset, 'workspace': str(root / job)}
                        for job, offset in (('low', 1e5), ('high', 1e6))]
            result = {'summary': [{'point': 'band_high@1000000Hz', 'operating_point_id': 'band_high',
                                   'offset_Hz': 1e6, 'f0_Hz': 2e9, 'child_job_id': 'high'}],
                      'offset_sweep': {'children': children}}
            projection = load_thermal_coefficients(result, 'band_high@1000000Hz', root)
            self.assertEqual(['high/id'], [row['id'] for row in projection['sources']])
            self.assertEqual([], load_thermal_coefficients(result, 'band_high@100000Hz', root)['sources'])

    @unittest.skipUnless(os.environ.get('ISF_GUI_TEST') == '1', 'requires private Tk display')
    def test_finished_api_job_survives_desktop_close_and_reopen(self):
        def wait(root, predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                root.update()
                if predicate():
                    return
                time.sleep(.02)
            self.fail('Timed out waiting for the desktop job state')

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            job_id = 'completed-api-job'
            manifest = work / 'api-jobs' / job_id / 'manifest.json'
            job = {'job_id': job_id, 'state': 'running', 'manifest_path': str(manifest)}
            write(manifest, {'job': job, 'stage': 'isf.run', 'issues': []})
            controller = IsfController(work)
            root = tk.Tk()
            app = IsfApplication(root, controller)
            try:
                record = synthetic_points()[0]
                sample = record['result']
                raw = {'summary': sample['summary'], 'points': {record['point']: sample}, 'run_kind': 'replay'}
                job['state'] = 'completed'
                write(manifest, {'job': job, 'stage': 'completed', 'result': raw, 'issues': []})
                app.testbench_page.started(job)
                wait(root, lambda: app.current_result_job_id == job_id)
                app.results_view.show(2)
                state = controller.save_state(dict(name='finished API job', config={}, view=app._state_view()))
                app._request_close()
                wait(root, lambda: app.queue.closed)
            finally:
                if not app.queue.closed:
                    app.queue.close()
                    root.destroy()

            root = tk.Tk()
            app = IsfApplication(root, IsfController(work))
            try:
                wait(root, lambda: app.jobs_tree.exists(job_id))
                app._load_saved_state(state['id'])
                wait(root, lambda: app.current_result_job_id == job_id)
                self.assertEqual(record['point'], app.point_var.get())
                self.assertEqual(2, app.results_view.view_id)
                for command in ('status', 'results', 'metrics'):
                    output = subprocess.check_output([sys.executable, '-B', 'run.py', command,
                                                      '--workspace', directory, '--job', job_id, '--json'],
                                                     cwd=ROOT, text=True)
                    payload = json.loads(output)
                    actual = (payload.get('job_id') or payload.get('job', {}).get('job_id')
                              or payload.get('source', {}).get('isf_job'))
                    self.assertEqual(job_id, actual)
            finally:
                app.queue.close()
                root.destroy()
