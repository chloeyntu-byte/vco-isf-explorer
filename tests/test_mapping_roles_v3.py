"""Editable role choices must change checked physical bindings, not labels."""
import copy
import unittest
import xml.etree.ElementTree as ET

import test_portable_setup as fixtures
from isf_tool.core.maestro_pair import _attach_global_variables, _global_variables
from isf_tool.core.signal_mapping import discover_signal_mapping


class MappingRoleV3Tests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PortableSetupTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def role(self, name, overrides=None, mutate=None):
        report = self.fixture.reports()
        original = report['roles'][name]
        test = copy.deepcopy(original['selected_test'])
        if mutate:
            mutate(test)
        role = discover_signal_mapping(test, name, original['selected_history'], overrides)
        role.update(selected_test=test, selected_history=original['selected_history'])
        report['roles'][name] = role
        return role, report

    @staticmethod
    def fields(role):
        return {item['key']: item for item in role['mapping_fields']}

    def test_manual_net_alias_is_real_and_unknown_name_is_rejected(self):
        role, _ = self.role('vco', {'output_p': '/VOUT_P'})
        self.assertEqual('/VOUT_P', role['mapping']['output_p'])
        self.assertEqual('VOUT_P', role['mapping']['output_p_netlist'])
        self.assertFalse([issue for issue in role['mapping_issues'] if issue['severity'] == 'error'])
        role, _ = self.role('vco', {'output_p': '/missing'})
        self.assertEqual('invalid', self.fields(role)['output_p']['status'])

    def test_role_dropdowns_include_all_outputs_and_all_variables(self):
        def mutate(test):
            test['outputs'].append({'name': 'some_other_output', 'expression': 'average(1)'})
        role, _ = self.role('vco', mutate=mutate)
        for key in ('vgs_output', 'vds_output', 'vbs_output', 'transfer_output', 'transfer_phase_output'):
            field = self.fields(role)[key]
            self.assertIn('some_other_output', field['candidates'])
            self.assertTrue(field['allow_custom'])
            self.assertEqual('output', field['candidate_kind'])
        role, _ = self.role('flicker', mutate=lambda test: test['variables'].append({'name': 'arbitrary_global', 'scope': 'global', 'expression': '1'}))
        for key in ('vgs_variable', 'vds_variable', 'vgs_pwl_variable', 'vds_pwl_variable', 'vbs_pwl_variable'):
            self.assertIn('arbitrary_global', self.fields(role)[key]['candidates'])

    def test_old_output_declaration_is_not_proof_of_net_existence(self):
        role, _ = self.role('vco', {'output_p': '/removed_net'},
            lambda test: test['outputs'].append({'name': 'old_wave', 'expression': 'vtime(\'hb "/removed_net")'}))
        self.assertEqual('invalid', self.fields(role)['output_p']['status'])
        self.assertTrue(any('actual netlist does not contain' in issue['message'] for issue in role['mapping_issues']))

    def test_selecting_target_recomputes_default_waveforms(self):
        def mutate(test):
            test['outputs'].append({'name': 'partner_gate', 'expression': 'vtime(\'hb "GATE_B") - vtime(\'hb "COMMON")'})
            test['outputs'].append({'name': 'partner_drain', 'expression': 'vtime(\'hb "VOUT_N") - vtime(\'hb "COMMON")'})
        role, _ = self.role('vco', {'target_instance': '/PARTNER'}, mutate)
        self.assertEqual('PARTNER', role['mapping']['target_instance'])
        self.assertEqual('partner_gate', role['mapping']['vgs_output'])
        self.assertEqual('partner_drain', role['mapping']['vds_output'])
        self.assertFalse([issue for issue in role['mapping_issues'] if issue['severity'] == 'error'])

    def test_missing_vbs_output_is_visible_as_script_derived(self):
        role, _ = self.role('vco')
        field = self.fields(role)['vbs_output']
        self.assertTrue(field['active'])
        self.assertIsNone(field['value'])
        self.assertEqual('script_derived', field['maestro_presence'])
        self.assertEqual('V(COMMON)-V(COMMON)', field['effective_derivation'])

    def test_sense_selection_resolves_two_readout_chains_and_reaches_profile(self):
        self.fixture.replica.write_text(self.fixture.replica.read_text() +
            'SECOND_DUT (D2 G_INT VSS VSS) portable_mos l=1u w=4u\n'
            'SECOND_SENSE (D_EXT D2) vsource dc=0 type=dc\n'
            'SECOND_READOUT (i_out VSS) ccvs rm=1 probe=SECOND_SENSE\n')
        role, _ = self.role('flicker')
        self.assertIsNone(role['mapping']['dc_instance'])
        role, report = self.role('flicker', {'dc_sense_source': 'SECOND_SENSE'})
        self.assertEqual('SECOND_DUT', role['mapping']['dc_instance'])
        self.assertEqual('SECOND_READOUT', role['mapping']['dc_transducer'])
        setup = self.fixture.build(report)
        self.assertTrue(setup['ok'], setup['errors'])
        replica = setup['engine']['physical_profile']['replica']
        self.assertEqual('SECOND_SENSE', replica['dc_sense_source'])
        self.assertEqual('SECOND_DUT', replica['dc_instance'])
        self.assertEqual('SECOND_READOUT', replica['dc_transducer'])

    def test_wrong_bias_supply_cannot_be_selected_as_current_sense(self):
        role, report = self.role('flicker', {'dc_sense_source': 'G_SOURCE'})
        errors = [issue for issue in role['mapping_issues'] if issue['severity'] == 'error']
        self.assertTrue(any(issue['key'] == 'dc_sense_source' for issue in errors))
        self.assertFalse(self.fixture.build(report)['ok'])

    def test_bias_sources_follow_terminals_and_allow_manual_selection(self):
        role, _ = self.role('flicker', {'vgs_source': '/G_SOURCE'})
        self.assertEqual('G_SOURCE', role['mapping']['vgs_source'])
        self.assertEqual('D_SOURCE', role['mapping']['vds_source'])
        role, report = self.role('flicker', {'vgs_source': 'D_SOURCE'})
        self.assertEqual('invalid', self.fields(role)['vgs_source']['status'])
        self.assertFalse(self.fixture.build(report)['ok'])

    def test_unconnected_variable_does_not_claim_to_change_dc_bias(self):
        role, report = self.role('flicker', {'vgs_variable': 'other_bias'},
            lambda test: test['variables'].append({'name': 'other_bias', 'expression': '0.6', 'scope': 'global'}))
        self.assertEqual('invalid', self.fields(role)['vgs_variable']['status'])
        self.assertFalse(self.fixture.build(report)['ok'])

    def test_pwl_file_variable_detected_from_source_not_name(self):
        self.fixture.replica.write_text(self.fixture.replica.read_text().replace(
            'dc=G_BIAS type=dc', 'dc=G_BIAS type=pwl file=unusual_file_name'))
        role, _ = self.role('flicker', mutate=lambda test: test['variables'].append(
            {'name': 'unusual_file_name', 'expression': 'waveforms/fresh_gate.pwl', 'scope': 'global'}))
        self.assertEqual('unusual_file_name', role['mapping']['vgs_pwl_variable'])
        field = self.fields(role)['vgs_pwl_variable']
        self.assertEqual('global', field['scope'])
        self.assertEqual('global_variable', field['maestro_presence'])
        self.assertTrue(field['global_required'])

    def test_numeric_parameter_cannot_be_claimed_as_pwl_path(self):
        role, report = self.role('flicker', {'vgs_pwl_variable': 'G_BIAS'})
        self.assertEqual('invalid', self.fields(role)['vgs_pwl_variable']['status'])
        self.assertFalse(self.fixture.build(report)['ok'])

    def test_vs_common_mode_file_role_is_explicit_without_original_source(self):
        role, _ = self.role('flicker')
        field = self.fields(role)['vs_pwl_variable']
        self.assertIsNone(field['value'])
        self.assertEqual('script_derived', field['maestro_presence'])
        self.assertTrue(field['global_required'])
        self.assertNotIn('vs_source', role['mapping'])
        role, _ = self.role('flicker', mutate=lambda test: test['variables'].append(
            {'name': 'ISF_VS_PWL_FILE', 'expression': 'waveforms/source.pwl', 'scope': 'global'}))
        self.assertEqual('ISF_VS_PWL_FILE', role['mapping']['vs_pwl_variable'])
        self.assertEqual('global', self.fields(role)['vs_pwl_variable']['scope'])

    def test_vs_common_mode_accepts_manual_global_name_and_rejects_numeric(self):
        from isf_tool.core.waveform_exchange import channel_variables
        from isf_tool.core.maestro_pair import replaceable_bias_variables
        role, _ = self.role('flicker', {'vs_pwl_variable': 'custom_source_common_mode'},
            lambda test: test['variables'].append({'name': 'custom_source_common_mode', 'expression': 'source.pwl', 'scope': 'global'}))
        self.assertEqual('custom_source_common_mode', channel_variables(role['mapping'])['VS'])
        self.assertIn('custom_source_common_mode', replaceable_bias_variables(role['mapping']))
        role, report = self.role('flicker', {'vs_pwl_variable': 'G_BIAS'})
        self.assertEqual('invalid', self.fields(role)['vs_pwl_variable']['status'])
        self.assertFalse(self.fixture.build(report)['ok'])

    def test_old_execution_override_cannot_hide_user_sense_choice(self):
        report = self.fixture.reports()
        config = copy.deepcopy(self.fixture.config)
        config['portable_setup'] = {'overrides': {'replica.dc_sense_source': 'G_SOURCE'}}
        setup = self.fixture.build(report, config)
        self.assertFalse(setup['ok'])
        self.assertTrue(any('signal mapping' in item['message'] for item in setup['errors']))

    def _with_transfer(self, test):
        test['outputs'].extend([
            {'name': 'custom_amplitude', 'expression': 'mag(harmonic(getData("injectA" ?result "xf_custom") 0))'},
            {'name': 'custom_phase', 'expression': 'phase(harmonic(getData("injectA" ?result "xf_custom") 0))'},
            {'name': 'other_phase', 'expression': 'phase(harmonic(getData("injectB" ?result "xf_custom") 0))'},
        ])

    def _add_transfer_netlist(self):
        self.fixture.vco.write_text(self.fixture.vco.read_text() +
            'injectA (VOUT_P COMMON) isource dc=0\n'
            'injectB (COMMON VOUT_P) isource dc=0\n'
            'xf_custom (VOUT_P VOUT_N) hbxf start=1k stop=100k\n')

    def test_phase_reference_keeps_user_choice_without_combining_sources(self):
        self._add_transfer_netlist()
        role, _ = self.role('vco', mutate=self._with_transfer)
        self.assertEqual('custom_amplitude', role['mapping']['transfer_output'])
        self.assertEqual('custom_phase', role['mapping']['transfer_phase_output'])
        self.assertEqual('custom_phase', role['mapping']['transfer_contract']['phase_output_name'])
        role, report = self.role('vco', {'transfer_phase_output': 'other_phase'}, self._with_transfer)
        self.assertEqual('manual', self.fields(role)['transfer_phase_output']['status'])
        transfer = role['mapping']['transfer_contract']
        self.assertEqual('injectA', transfer['source'])
        self.assertEqual('injectB', transfer['phase_reference']['source'])
        self.assertEqual('reference_only_remeasured_complex', transfer['phase_reference']['consumption_mode'])
        self.assertTrue(self.fixture.build(report)['ok'])

    def test_non_transfer_output_is_listed_but_not_falsely_consumed(self):
        role, report = self.role('vco', {'transfer_output': 'HB_TIME_VGS'})
        self.assertEqual('invalid', self.fields(role)['transfer_output']['status'])
        self.assertEqual('invalid', role['mapping']['transfer_contract']['mode'])
        self.assertFalse(self.fixture.build(report)['ok'])


class MaestroGlobalScopeTests(unittest.TestCase):
    def _grouped_scope(self, omitted='0', local_group=False):
        local = '<variablesets><variableset>local<vars><var>bias</var></vars></variableset></variablesets>' if local_group else ''
        root = ET.fromstring('''<setupdb><active><vars><var>bias<value>0.2 0.8</value>
            <dependentTests><dependentTest omitted="%s">T</dependentTest></dependentTests></var></vars>
            <variablesets><variableset>0<vars><var>bias</var></vars></variableset></variablesets>
            <tests><test>T<vars><var>bias<value>0.2</value></var></vars>%s</test></tests></active></setupdb>''' % (omitted, local))
        tests = [{'name': 'T', 'variables': [{'name': 'bias', 'expression': '0.2'}]}]
        _attach_global_variables(tests, root)
        return tests[0]

    def test_native_active_global_group_preserves_test_cache_without_shadow(self):
        test = self._grouped_scope()
        variable = test['variables'][0]
        self.assertEqual('global', variable['scope'])
        self.assertEqual('0.2 0.8', variable['expression'])
        self.assertEqual('0.2', variable['test_cached_expression'])
        self.assertNotIn('shadows_global', variable)
        self.assertEqual('active_global_parametric_group_and_test_dependency', variable['scope_evidence'])
        self.assertEqual('test_cache_or_default', test['test_variables'][0]['scope'])
        self.assertEqual([{'name': '0', 'variables': ['bias']}], test['global_parametric_groups'])

    def test_matching_test_scalar_does_not_prove_global_group_applies(self):
        for options in ({'omitted': '1'}, {'local_group': True}):
            with self.subTest(options=options):
                variable = self._grouped_scope(**options)['variables'][0]
                self.assertEqual('global_or_test', variable['scope'])
                self.assertNotIn('shadows_global', variable)
                self.assertNotIn('test_cached_expression', variable)

    def test_native_blank_test_placeholder_inherits_global(self):
        root = ET.fromstring('''<setupdb><active><vars><var>bias<value>0.6</value></var><var>path<value>"global.pwl"</value></var></vars>
            <tests><test>T<vars><var>bias<value /></var><var>path<value>""</value></var></vars></test></tests></active></setupdb>''')
        tests = [{'name': 'T', 'variables': [{'name': 'bias', 'expression': '0.6'}, {'name': 'path', 'expression': ''}]}]
        _attach_global_variables(tests, root)
        records = {item['name']: item for item in tests[0]['variables']}
        self.assertEqual('global', records['bias']['scope'])
        self.assertEqual('test', records['path']['scope'])
        self.assertTrue(records['path']['shadows_global'])

    def test_globals_are_distinct_from_corners_and_history(self):
        root = ET.fromstring('''<setupdb><active><vars><var>fresh_path<value>"waveforms/g.pwl"</value></var></vars>
            <corners><corner><vars><var>corner_only<value>1</value></var></vars></corner></corners>
            <tests><test>T<vars><var>local_bias<value>0.6</value></var></vars></test></tests></active>
            <histories><history><vars><var>history_only<value>1</value></var></vars></history></histories></setupdb>''')
        globals_ = _global_variables(root)
        self.assertEqual(['fresh_path'], [item['name'] for item in globals_])
        tests = [{'name': 'T', 'variables': [{'name': 'fresh_path', 'expression': 'inherited'}, {'name': 'local_bias', 'expression': '0.6'}]}]
        _attach_global_variables(tests, root)
        records = {item['name']: item for item in tests[0]['variables']}
        self.assertEqual('global', records['fresh_path']['scope'])
        self.assertEqual('waveforms/g.pwl', records['fresh_path']['expression'])
        self.assertEqual('test', records['local_bias']['scope'])

    def test_explicit_test_override_is_exposed_as_shadowing_global(self):
        root = ET.fromstring('''<setupdb><active><vars><var>bias<value>0.6</value></var></vars>
            <tests><test>T<vars><var>bias<value>0.7</value></var></vars></test></tests></active></setupdb>''')
        tests = [{'name': 'T', 'variables': [{'name': 'bias', 'expression': '0.7'}]}]
        _attach_global_variables(tests, root)
        variable = tests[0]['variables'][0]
        self.assertEqual('test', variable['scope'])
        self.assertTrue(variable['shadows_global'])
        self.assertEqual('0.6', variable['global_expression'])


if __name__ == '__main__':
    unittest.main()
