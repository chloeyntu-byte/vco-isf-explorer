"""Named transfer families describe user-declared sources, not a topology gate."""

import copy
import unittest

import test_portable_setup as fixtures
from isf_tool.core.signal_mapping import discover_signal_mapping, transfer_output_reference
from isf_tool.core.transfer_family import output_family_suffix


def output(name, sideband=0, source='I_SOURCE', phase=False, analysis='hbxf', inner=True):
    data = 'getData("/%s" ?result "%s")' % (source, analysis)
    function = 'phaseDegUnwrapped' if phase else 'abs'
    expression = ('harmonic(%s(%s) %s)' % (function, data, sideband) if inner
                  else '%s(harmonic(%s %s))' % (function, data, sideband))
    return {'name': name, 'expression': expression}


class TransferExpressionTests(unittest.TestCase):
    def test_both_function_nestings_have_identical_read_locations(self):
        for function in ('abs', 'mag', 'phase', 'phaseDeg', 'phaseDegUnwrapped'):
            for sideband in (-7, 0, 6):
                with self.subTest(function=function, sideband=sideband):
                    data = 'getData("/scope/INJ" ?result "custom_xf")'
                    outer = transfer_output_reference('%s(harmonic(%s %s))' % (function, data, sideband))
                    inner = transfer_output_reference('harmonic(%s(%s) %s)' % (function, data, sideband))
                    self.assertEqual(outer, inner)
                    self.assertEqual('scope.INJ', inner['source'])
                    self.assertEqual(sideband, inner['sideband'])

    def test_raw_complex_is_also_a_valid_read_location(self):
        reference = transfer_output_reference('harmonic(getData("/INJ" ?result "hbxf") -2)')
        self.assertEqual('complex', reference['view'])

    def test_mixed_expressions_are_not_executed_or_guessed(self):
        for expression in ('shell("echo bad")', 'mag_pxf0',
                           '2*abs(harmonic(getData("I0" ?result "hbxf") 0))',
                           'abs(harmonic(getData("I0" ?result "hbxf") 0) + harmonic(getData("I1" ?result "hbxf") 0))'):
            self.assertIsNone(transfer_output_reference(expression))

    def test_arbitrary_base_and_signed_suffix_styles(self):
        for name, expected in {
            'harm_mag_m1': ('harm_mag', -1), 'harm_mag_0': ('harm_mag', 0),
            'harm_mag_12': ('harm_mag', 12), 'mag_pxfm1': ('mag_pxf', -1),
            'mag_pxf0': ('mag_pxf', 0), 'renamed_set_-3': ('renamed_set', -3),
            'custom_ph+2': ('custom_ph', 2),
        }.items():
            self.assertEqual(expected, output_family_suffix(name))
        self.assertIsNone(output_family_suffix('unindexed_output'))


class TransferFamilyMappingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PortableSetupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.vco.write_text(self.fixture.vco.read_text() +
            'I_SOURCE (COMMON VOUT_P) isource type=sine\n'
            'I_OTHER (COMMON GATE_A) vsource dc=0\n'
            'hbxf (VOUT_P COMMON) hbxf start=1k stop=100k\n')

    def role(self, outputs=(), overrides=None, role='vco', mutate=None):
        report = self.fixture.reports()
        old = report['roles'][role]
        test = copy.deepcopy(old['selected_test'])
        test['outputs'].extend(outputs)
        if mutate:
            mutate(test)
        result = discover_signal_mapping(test, role, old['selected_history'], overrides)
        result.update(selected_test=test, selected_history=old['selected_history'])
        report['roles'][role] = result
        return result, report

    @staticmethod
    def fields(result):
        return {field['key']: field for field in result['mapping_fields']}

    @staticmethod
    def family(base, phase=False, underscores=True, bands=(-1, 0, 1, 2)):
        return [output(base + ('_' if underscores else '') + ('m%d' % -band if band < 0 else str(band)),
                       band, phase=phase, inner=phase) for band in bands]

    def test_existing_real_style_sixteen_outputs_default_to_two_families(self):
        outputs = self.family('mag_pxf', underscores=False, bands=range(-1, 7))
        outputs += self.family('ph_pxf', phase=True, underscores=False, bands=range(-1, 7))
        result, report = self.role(outputs)
        self.assertEqual('mag_pxf', result['mapping']['transfer_output'])
        self.assertEqual('ph_pxf', result['mapping']['transfer_phase_output'])
        contract = result['mapping']['transfer_contract']
        self.assertEqual('mag_pxf0', contract['output_name'])
        self.assertEqual('I_SOURCE', contract['source'])
        self.assertEqual(-1, contract['direction_factor'])
        self.assertEqual('topology_reverse', contract['direction_basis'])
        self.assertEqual(list(range(-1, 7)), contract['family']['sidebands'])
        self.assertEqual(8, len(contract['family']['members']))
        self.assertTrue(self.fixture.build(report)['ok'])

    def test_arbitrary_underscore_families_and_custom_phase_work(self):
        result, _ = self.role(self.family('harm_mag') + self.family('harm_ph', phase=True))
        fields = self.fields(result)
        self.assertEqual('harm_mag', fields['transfer_output']['value'])
        self.assertEqual('harm_ph', fields['transfer_phase_output']['value'])
        self.assertIn('harm_mag_0', fields['transfer_output']['candidates'])
        self.assertEqual('transfer_family', fields['transfer_output']['selected_details']['kind'])
        self.assertEqual('harm_mag_0', fields['transfer_output']['candidate_details']['harm_mag']['representative_output'])

    def test_renaming_base_does_not_change_raw_transfer_direction(self):
        first, _ = self.role(self.family('one'))
        second, _ = self.role(self.family('totally_new_name'))
        for key in ('source', 'analysis', 'direction_factor', 'effective_output'):
            self.assertEqual(first['mapping']['transfer_contract'][key], second['mapping']['transfer_contract'][key])

    def test_legacy_exact_member_remains_selectable(self):
        result, _ = self.role(self.family('mag_pxf', underscores=False), {'transfer_output': 'mag_pxf0'})
        self.assertEqual('mag_pxf0', result['mapping']['transfer_output'])
        self.assertNotIn('family', result['mapping']['transfer_contract'])

    def test_nonzero_single_member_and_family_without_zero_are_allowed(self):
        single, _ = self.role([output('only_transfer', sideband=3)], {'transfer_output': 'only_transfer'})
        self.assertEqual(3, single['mapping']['transfer_contract']['sideband'])
        result, _ = self.role(self.family('no_zero', bands=(-2, 3)))
        details = self.fields(result)['transfer_output']['selected_details']
        self.assertEqual('no_zero_m2', details['representative_output'])
        self.assertTrue(any('没有边带 0' in note for note in details['notes']))
        self.assertFalse(details['errors'])

    def test_suffix_mismatch_and_duplicate_sidebands_are_descriptive(self):
        result, _ = self.role([output('custom_0', 0), output('custom_1', 0)], {'transfer_output': 'custom'})
        details = self.fields(result)['transfer_output']['selected_details']
        self.assertEqual('manual', self.fields(result)['transfer_output']['status'])
        self.assertFalse(details['errors'])
        self.assertTrue(any('名称后缀' in note for note in details['notes']))
        self.assertTrue(any('对应多个名称' in note for note in details['notes']))

    def test_mixed_family_sources_are_disclosed_not_a_physical_gate(self):
        result, _ = self.role([output('mixed_0'), output('mixed_1', 1, source='I_OTHER')], {'transfer_output': 'mixed'})
        contract = result['mapping']['transfer_contract']
        self.assertEqual('I_SOURCE', contract['source'])
        self.assertFalse(contract['family']['errors'])
        self.assertTrue(any('注入电流源有多个' in note for note in contract['family']['notes']))

    def test_different_phase_source_is_reference_not_complex_recombination(self):
        result, _ = self.role([output('amplitude'), output('user_phase', 2, source='I_OTHER', phase=True)],
                              {'transfer_output': 'amplitude', 'transfer_phase_output': 'user_phase'})
        contract = result['mapping']['transfer_contract']
        self.assertEqual('I_SOURCE', contract['source'])
        self.assertEqual('I_OTHER', contract['phase_reference']['source'])
        self.assertFalse(contract['phase_reference']['used_as_transfer_source'])
        self.assertFalse([item for item in result['mapping_issues'] if item['severity'] == 'error'])

    def test_unrecognized_topology_and_analysis_do_not_override_user_declaration(self):
        result, _ = self.role([output('user_declared', source='CUSTOM_READ_PATH', analysis='custom_xf')],
                              {'transfer_output': 'user_declared'})
        contract = result['mapping']['transfer_contract']
        self.assertEqual('CUSTOM_READ_PATH', contract['source'])
        self.assertEqual('custom_xf', contract['analysis'])
        self.assertEqual(1, contract['direction_factor'])
        self.assertEqual('user_declared', contract['direction_basis'])
        self.assertFalse([item for item in result['mapping_issues'] if item['severity'] == 'error'])

    def test_non_current_source_model_does_not_reject_explicit_selection(self):
        result, _ = self.role([output('declared', source='I_OTHER')], {'transfer_output': 'declared'})
        self.assertEqual('existing_source', result['mapping']['transfer_contract']['mode'])
        self.assertEqual('user_declared', result['mapping']['transfer_contract']['direction_basis'])

    def test_unreadable_selected_expression_still_reports_missing_read_location(self):
        result, _ = self.role([{'name': 'mixed_expression', 'expression': 'abs(foo + bar)'}], {'transfer_output': 'mixed_expression'})
        self.assertEqual('invalid', result['mapping']['transfer_contract']['mode'])
        self.assertTrue(any(item['key'] == 'transfer_output' and item['severity'] == 'error' for item in result['mapping_issues']))

    def test_stale_reference_only_names_warn_without_blocking_actual_data(self):
        result, report = self.role(overrides={'noise_output_at_offset': 'deleted_noise_label', 'current_trace': 'deleted_current_label'})
        fields = self.fields(result)
        for key in ('noise_output_at_offset', 'current_trace'):
            issue = next(item for item in result['mapping_issues'] if item['key'] == key)
            self.assertEqual('warning', issue['severity'])
            self.assertIn('不影响原始数据计算', issue['message'])
            self.assertEqual('invalid', fields[key]['status'])
            self.assertTrue(fields[key]['active'])
        self.assertTrue(self.fixture.build(report)['ok'])

    def test_missing_actual_binding_is_not_relaxed_with_reference_only_names(self):
        result, _ = self.role(overrides={'vgs_output': 'deleted_actual_output'})
        self.assertTrue(any(item['key'] == 'vgs_output' and item['severity'] == 'error' for item in result['mapping_issues']))

    def test_global_already_exists_does_not_request_repeat_migration(self):
        def mutate(test):
            variable = next(item for item in test['variables'] if item['name'] == 'G_BIAS')
            variable.update(scope='test', shadows_global=True, global_expression='0.7', expression='0.6')
        result, _ = self.role(role='flicker', mutate=mutate)
        field = self.fields(result)['vgs_variable']
        issue = next(item for item in result['mapping_issues'] if item['key'] == 'vgs_variable')
        self.assertEqual('info', issue['severity'])
        self.assertIn('不需要重复迁移', issue['message'])
        self.assertEqual('test', field['scope'])
        self.assertEqual('0.7', field['selected_details']['global_expression'])
        self.assertEqual('0.6', field['selected_details']['expression'])

    def test_true_test_only_variable_still_suggests_global_migration(self):
        def mutate(test):
            next(item for item in test['variables'] if item['name'] == 'G_BIAS')['scope'] = 'test'
        result, _ = self.role(role='flicker', mutate=mutate)
        issue = next(item for item in result['mapping_issues'] if item['key'] == 'vgs_variable')
        self.assertEqual('warning', issue['severity'])
        self.assertIn('移到 Maestro global', issue['message'])
