"""Flicker port evidence and transfer-frame regressions."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from isf_tool.core.flicker_transfer import add_flicker_analyses, flicker_port, read_flicker_transfer


class FlickerTransferTests(unittest.TestCase):
    device = 'DUT.MN.m0'
    terminal_map = {device: {'master': 'bsim4', 'terminals': {'d': 'D', 's': '0'}}}
    int_d = device + ':int_d'
    int_s = device + ':int_s'

    def test_nodal_transfer_precedes_native_analyses_that_can_replace_hb_state(self):
        text = ('simulator lang=spectre\nopts options save=selected\n'
                'hb hb fundfreqs=[1G]\nnative_info info what=oppoint\n')
        result = add_flicker_analyses(text, {'positive': 'OP', 'negative': 'ON'}, 1e5, 7)
        names = [result.index(name) for name in ('hb hb ', 'isf_fn_upper ', 'isf_fn_lower ', 'native_info ')]
        self.assertEqual(names, sorted(names))
        self.assertIn('opts options save=all', result)

    def test_external_requires_consistent_absence_and_complete_external_port(self):
        port, evidence = flicker_port(self.device, self.terminal_map, [{'D'}, {'D'}], {'D'})
        self.assertEqual(port['positive'], 'D')
        self.assertEqual(evidence['mode'], 'external_ds_fallback')
        self.assertEqual(evidence['absent_intrinsic_traces'], [self.int_d, self.int_s])
        self.assertIn('External D/S', evidence['notice'])
        with self.assertRaisesRegex(ValueError, 'Incomplete intrinsic'):
            flicker_port(self.device, self.terminal_map, [{'D', self.int_d}, {'D'}], {'D'})
        with self.assertRaisesRegex(ValueError, 'present in HB'):
            flicker_port(self.device, self.terminal_map, [{'D'}, {'D'}], {self.int_s})
        with self.assertRaisesRegex(ValueError, 'incomplete D/S'):
            flicker_port(self.device, self.terminal_map, [{'D'}, {'OUT'}], {'D'})

    def test_projection_keeps_both_signed_sidebands_and_removes_am(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'isf_fn_nodes.txt').write_text(f'bsim4: {self.device}\n  d : D\n  s : 0\n')
            engine = SimpleNamespace(exact_hb_fundamental=lambda *_: 100.0)
            # Rotating by the carrier leaves 2 as AM and 3 as PM.
            values = {self.int_d: (2 + 3j) * 1j, self.int_s: 0j, 'D': 99 + 0j}
            with patch('isf_tool.core.flicker_transfer._read', return_value={}) as read, \
                    patch('isf_tool.core.flicker_transfer.noise_sample', return_value={'values': values}):
                transfer, evidence = read_flicker_transfer(
                    engine, {'offset_hz': 1.0}, root / 'psf', self.device, 1, 1j)
            self.assertEqual(transfer, {-1: 3j, 0: 3j, 1: 3j})
            self.assertEqual(evidence['mode'], 'intrinsic_ds')
            self.assertEqual(read.call_args_list[0].args[2].name, 'isf_fn_upper.-2.pxf_hbxf')
            self.assertEqual(read.call_args_list[1].args[2].name, 'isf_fn_lower.0.pxf_hbxf')
