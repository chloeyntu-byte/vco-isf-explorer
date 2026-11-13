"""Native per-test global enable flags and repeated active.state variables."""
import unittest
import xml.etree.ElementTree as ET

from isf_tool.core.errors import ValidationError
from isf_tool.core.maestro_pair import _attach_global_variables, _global_variables
from isf_tool.core.signal_mapping import get_signal_catalog


class VariableDependencyScopeTests(unittest.TestCase):
    def root(self, attributes='enabled="0" omitted="0"', grouped=False):
        group = ('<variablesets><variableset>0<vars><var>gate</var></vars>'
                 '</variableset></variablesets>') if grouped else ''
        return ET.fromstring('''<setupdb><active><vars><var>gate<value>1</value>
            <dependentTests><dependentTest %s>DC</dependentTest></dependentTests>
            </var></vars>%s<tests><test>DC<vars><var>gate<value>0</value></var>
            </vars></test></tests></active></setupdb>''' % (attributes, group))

    def test_disabled_global_retains_local_zero_and_original_global_provenance(self):
        tests = [{'name': 'DC', 'variables': [{'name': 'gate', 'expression': '0'},
                                           {'name': 'gate', 'expression': '0'}]}]
        globals_ = _attach_global_variables(tests, self.root())
        self.assertFalse(globals_[0]['dependent_tests'][0]['enabled'])
        variable = tests[0]['variables'][0]
        self.assertEqual('0', variable['expression'])
        self.assertEqual('test', variable['scope'])
        self.assertEqual('1', variable['global_expression'])
        self.assertEqual(1, len(tests[0]['test_variables']))
        catalog = get_signal_catalog(tests[0], {})
        self.assertEqual('0', catalog[0]['expression'])
        self.assertEqual('1', catalog[0]['details']['global_expression'])

    def test_disabled_or_omitted_group_cannot_claim_effective_global_scan(self):
        for attributes in ('enabled="0" omitted="0"', 'enabled="1" omitted="1"'):
            with self.subTest(attributes=attributes):
                tests = [{'name': 'DC', 'variables': [{'name': 'gate', 'expression': '0'}]}]
                _attach_global_variables(tests, self.root(attributes, grouped=True))
                value = tests[0]['variables'][0]
                self.assertEqual('0', value['expression'])
                self.assertNotEqual('global', value['scope'])
                self.assertNotIn('scope_evidence', value)
                self.assertNotIn('test_cached_expression', value)

    def test_native_enabled_only_dependency_is_enabled_and_not_omitted(self):
        for attributes in ('enabled="1"', 'omitted="0"'):
            with self.subTest(attributes=attributes):
                dependency = _global_variables(self.root(attributes))[0]['dependent_tests'][0]
                self.assertTrue(dependency['enabled'])
                self.assertFalse(dependency['omitted'])

    def test_conflicting_repeated_local_values_fail_instead_of_last_wins(self):
        tests = [{'name': 'DC', 'variables': [{'name': 'gate', 'expression': '0'},
                                           {'name': 'gate', 'expression': '1'}]}]
        with self.assertRaisesRegex(ValidationError, 'DC.*gate.*conflicting'):
            _attach_global_variables(tests, self.root())


if __name__ == '__main__':
    unittest.main()
