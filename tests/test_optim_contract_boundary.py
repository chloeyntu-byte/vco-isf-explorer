"""Reject incompatible calibration contracts before any solver acquisition."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from isf_tool.optim import evaluate
from isf_tool.optim.schema import validate
from test_optim_api import reference


class OptimContractBoundaryTests(unittest.TestCase):
    def test_wrong_version_or_missing_contract_is_rejected(self):
        ref, calibration = reference()
        for field, value in [('api_version', '99.0.0'), ('schema', 'unknown')]:
            bad = copy.deepcopy(calibration)
            bad[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'OPTIM_CONTRACT'):
                evaluate(ref, calibration=bad, offsets_hz=[1e6])
        bad = copy.deepcopy(calibration)
        bad.pop('api_version')
        with self.assertRaisesRegex(ValueError, 'OPTIM_CONTRACT'):
            evaluate(ref, calibration=bad, offsets_hz=[1e6])

    def test_boolean_sign_is_not_a_json_number(self):
        ref, calibration = reference()
        calibration['gamma_sign'] = True
        with self.assertRaisesRegex(ValueError, 'OPTIM_CONTRACT'):
            evaluate(ref, calibration=calibration, offsets_hz=[1e6])

    def test_invalid_live_calibration_is_rejected_before_acquisition(self):
        _, calibration = reference()
        calibration['api_version'] = '99.0.0'
        with tempfile.TemporaryDirectory() as directory:
            ref = dict(kind='design_lab', project_path='unused.json', workspace=directory)
            with mock.patch('isf_tool.optim.api.subprocess.run') as runner:
                with self.assertRaisesRegex(ValueError, 'OPTIM_CONTRACT'):
                    evaluate(ref, calibration=calibration, offsets_hz=[1e6])
                runner.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_json_const_keeps_boolean_distinct_from_zero(self):
        ref, calibration = reference()
        result = evaluate(ref, calibration=calibration, offsets_hz=[1e6])
        result['signoff'] = 0
        with self.assertRaisesRegex(ValueError, 'invalid version/value'):
            validate(result)

    def test_cli_rejects_unsupported_calibration_version(self):
        ref, calibration = reference()
        calibration['api_version'] = '99.0.0'
        with tempfile.TemporaryDirectory() as directory:
            refpath = Path(directory)/'ref.json'
            calpath = Path(directory)/'calibration.json'
            ref.update(schema='vco.isf-design-ref.v1', kind='hb_psf')
            refpath.write_text(json.dumps(ref))
            calpath.write_text(json.dumps(calibration))
            process = subprocess.run([sys.executable, '-B', 'run.py', 'optim', 'evaluate',
                str(refpath), '--calibration', str(calpath), '--offset-hz', '1000000'],
                capture_output=True, text=True)
            self.assertEqual(process.returncode, 2, process.stdout[:300])
            error = json.loads(process.stdout)
            self.assertEqual(error['status'], 'failed')
            self.assertIn('OPTIM_CONTRACT', error['message'])
