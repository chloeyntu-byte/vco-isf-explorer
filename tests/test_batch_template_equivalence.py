"""Native independent-point histories may share one proven bias template."""

import tempfile
import unittest
from pathlib import Path

from isf_tool.core.errors import ValidationError
from isf_tool.core.maestro_pair import operating_template_key


class BatchTemplateEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.text = ('simulator lang=spectre\nparameters VGS=0.5 VDS=1.2 SW=0\n'
                     'include "/models/device.scs" section=tt\n'
                     'M1 (d g 0 0) nmos w=8u l=400n\n'
                     'VG (g 0) vsource type=dc dc=VGS\n'
                     'VD (d 0) vsource type=dc dc=VDS\n'
                     'opts options temp=27 tnom=27 psfversion="1.1.0" sensfile="../psf/sens.output"\n'
                     'dcOp dc write="spectre.dc"\nnoise (d 0) noise start=1k stop=1G\n'
                     'save d g\n')

    def source(self, name, text=None, **kwargs):
        path = self.root / str(name) / 'input.scs'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.text if text is None else text)
        return dict({'path': str(path), 'corner': 'tt', 'test': 'noise_tb',
                     'operating_point': {'params': {}}}, **kwargs)

    def key(self, source, bias=('VGS', 'VDS')):
        return operating_template_key(source, bias)

    def test_25_independent_bias_netlists_have_one_template(self):
        sources = [self.source(i, self.text.replace('VGS=0.5 VDS=1.2',
                   'VGS=%.8g VDS=%.8g' % (0.1 + i / 100, 1.0 + i / 100))) for i in range(25)]
        self.assertEqual(25, len({item['path'] for item in sources}))
        self.assertEqual(1, len({self.key(item) for item in sources}))

    def test_device_analysis_save_model_temperature_and_nonbias_differences_remain(self):
        baseline = self.key(self.source('baseline'))
        for i, (before, after) in enumerate((('w=8u', 'w=9u'), ('start=1k', 'start=10k'),
                ('save d g', 'save d'), ('section=tt', 'section=ff'), ('temp=27', 'temp=125'),
                ('SW=0', 'SW=1'), ('nmos w=', 'pmos w='))):
            with self.subTest(change=(before, after)):
                self.assertNotEqual(baseline, self.key(self.source(i, self.text.replace(before, after))))

    def test_corner_test_and_rdb_nonbias_identity_remain(self):
        original = self.source('original')
        baseline = self.key(original)
        for changed in (dict(original, corner='ss'), dict(original, test='other'),
                        dict(original, operating_point={'params': {'SW': 1}})):
            self.assertNotEqual(baseline, self.key(changed))

    def test_relative_include_in_separate_directories_never_merges(self):
        text = self.text.replace('"/models/device.scs"', '"model.scs"')
        self.assertNotEqual(self.key(self.source(1, text)), self.key(self.source(2, text)))

    def test_relative_input_file_and_unknown_resource_expression_keep_directory(self):
        for extra in ('I1 (d 0) isource file="waveforms/current.pwl"\n',
                      'I1 (d 0) isource file=otherFile\n',
                      'parameters unknown="relative-resource"\n',
                      'include model.scs\n', 'ahdl_include strcat(libRoot "/model.va")\n'):
            with self.subTest(extra=extra):
                self.assertNotEqual(self.key(self.source(1, self.text + extra)),
                                    self.key(self.source(2, self.text + extra)))

    def test_absolute_input_file_is_context_independent_but_path_identity_remains(self):
        text = self.text + 'I1 (d 0) isource file="/waveforms/current.pwl"\n'
        self.assertEqual(self.key(self.source(1, text)), self.key(self.source(2, text)))
        self.assertNotEqual(self.key(self.source(1, text)),
                            self.key(self.source(2, text.replace('/waveforms/current.pwl', '/other/current.pwl'))))

    def test_mapped_string_file_is_regenerated_not_original_directory_bound(self):
        first = self.source(1, self.text + 'parameters customFile="old/one.pwl"\n'
                            'VG2 (g 0) vsource type=pwl file=customFile\n')
        second = self.source(2, self.text + 'parameters customFile="elsewhere/two.pwl"\n'
                             'VG2 (g 0) vsource type=pwl file=customFile\n')
        self.assertEqual(self.key(first, ('VGS', 'VDS', 'customFile')),
                         self.key(second, ('VGS', 'VDS', 'customFile')))
        self.assertNotEqual(self.key(first), self.key(second))

    def test_nested_parameter_with_same_name_is_not_masked(self):
        first = self.source(1, self.text + 'subckt local p n\nparameters VGS=1\nends local\n')
        second = self.source(2, self.text + 'subckt local p n\nparameters VGS=2\nends local\n')
        self.assertNotEqual(self.key(first), self.key(second))

    def test_duplicate_root_bias_declaration_fails(self):
        with self.assertRaises(ValidationError):
            self.key(self.source(1, self.text + 'parameters VGS=0.7\n'))


if __name__ == '__main__':
    unittest.main()
