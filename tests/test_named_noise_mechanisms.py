"""Flicker input selection follows native names/units, not BSIM3 positions."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('named_noise_engine_test',
    Path(__file__).resolve().parents[1] / 'scripts/dynamic_isf_closure_engine.py')
ENGINE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ENGINE)


def dump(fields):
    declarations = '\n'.join('"%s" FLOAT DOUBLE PROP("units" "%s")' % (name, units)
                             for name, units, value in fields)
    values = ' '.join(str(value) for name, units, value in fields)
    return ('HEADER\n"analysis type" "noise"\nTYPE\n'
            '"mos" STRUCT(\n' + declarations + '\n) PROP("master" "bsim4")\n'
            'TRACE\n"MREF" "mos"\nVALUE\n"freq" 10000\n"MREF" (' + values + ')\nEND\n')


class NamedNoiseMechanismTests(unittest.TestCase):
    def read(self, fields, mechanism='fn', domain='V'):
        with mock.patch.object(ENGINE, 'psf_trace', return_value=dump(fields)):
            return ENGINE.noise_output_psd_at({}, 'noise.noise', 'MREF', 10000, mechanism, expected_domain=domain)

    def test_bsim4_zero_fourth_field_does_not_erase_real_flicker(self):
        fields = [(name, 'V^2/Hz', 0) for name in ('rd', 'rs', 'rgbi', 'rbpd', 'rbps', 'rbpb')]
        fields += [('id', 'V^2/Hz', 2e-20), ('fn', 'V^2/Hz', 7e-18), ('total', 'V^2/Hz', 9e-18)]
        self.assertEqual(7e-18, self.read(fields))
        self.assertEqual(9e-18, self.read(fields, 'total'))
        self.assertEqual(7e-18, self.read(list(reversed(fields))))

    def test_separate_noise_uses_output_psd_not_intrinsic_source_or_power_gain(self):
        fields = [('fn.src', 'A^2/Hz', 3e-19), ('fn.xf2', '(V^2/Hz)/(A^2/Hz)', 4),
                  ('fn.out', 'V^2/Hz', 12e-19), ('total', 'V^2/Hz', 2e-18)]
        self.assertEqual(12e-19, self.read(fields))
        self.assertEqual(2e-18, self.read(fields, 'total'))

    def test_replica_current_output_requires_explicit_current_domain(self):
        fields = [('fn', 'A^2/Hz', 7e-18)]
        self.assertEqual(7e-18, self.read(fields, domain='A'))
        with self.assertRaisesRegex(ValueError, 'domain mismatch'):
            self.read(fields, domain='V')

    def test_missing_ambiguous_or_wrong_domain_mechanisms_fail(self):
        cases = [[('rbpd', 'V^2/Hz', 1)], [('fn.src', 'A^2/Hz', 1)],
                 [('fn', 'V^2/Hz', 1), ('fn.out', 'V^2/Hz', 1)], [('fn', 'A^2/Hz', 1)]]
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises((RuntimeError, ValueError)):
                self.read(fields)


if __name__ == '__main__':
    unittest.main()
