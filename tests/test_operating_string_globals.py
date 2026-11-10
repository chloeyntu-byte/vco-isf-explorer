"""Quoted Maestro globals retain safe, exact operating-point identity."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from isf_tool.core.errors import ValidationError
from isf_tool.core.maestro_pair import (
    restore_operating_point_text, operating_nonbias_parameters,
    operating_template_key, match_flicker_operating_source,
    replaceable_bias_variables, _operating_environment,
)
from isf_tool.core.psf_extract import _read_parameter_tables, _parameters_for_run


def contract(params):
    return {'params': params, 'parameter_records': [{'name': name, 'type': 'desVar'} for name in params]}


def bound_source(path, params, corner='', point_id=1):
    op = contract(params)
    text, changes = restore_operating_point_text(path.read_text(), op)
    op.update(restored_values=changes, environment=_operating_environment(text, str(path)),
              effective_netlist_sha256=hashlib.sha256(text.encode()).hexdigest())
    return {'path': str(path), 'corner': corner, 'point_id': point_id, 'test': 'noise', 'operating_point': op}


class OperatingStringGlobalTests(unittest.TestCase):
    def test_quoted_file_global_restores_only_existing_root_string(self):
        text = 'parameters custom_gate_file="old/gate.pwl" gain=2\nsubckt x a b\nparameters custom_gate_file="inner.pwl"\nends x\n'
        result, changes = restore_operating_point_text(text, contract({'custom_gate_file': '"new machine/gate.pwl"'}))
        self.assertIn('custom_gate_file="new machine/gate.pwl" gain=2', result)
        self.assertIn('custom_gate_file="inner.pwl"', result)
        self.assertEqual('design_variable_string', changes[0]['kind'])

    def test_native_quoted_paths_survive_continued_global_declarations(self):
        text = 'parameters gateFile="/old/g.pwl" \\\n drainFile="/old/d.pwl" \\\n bias=0.6\n'
        result, _ = restore_operating_point_text(text, contract({'gateFile': '"/new/g.pwl"', 'drainFile': '"/new/d.pwl"'}))
        self.assertIn('gateFile="/new/g.pwl"', result)
        self.assertIn('drainFile="/new/d.pwl"', result)
        self.assertIn('bias=0.6', result)

    def test_string_does_not_change_numeric_or_undefined_parameter_type(self):
        for text in ('parameters gateFile=1\n', 'parameters other=1\n', 'subckt x a\nparameters gateFile="a"\nends x\n', 'parameters gateFile="a"\nparameters gateFile="b"\n'):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                restore_operating_point_text(text, contract({'gateFile': '"new.pwl"'}))
        with self.assertRaises(ValidationError):
            restore_operating_point_text('parameters gateFile="1"\n', contract({'gateFile': 1}))

    def test_ade_expressions_unquoted_paths_and_injection_are_not_evaluated(self):
        values = ['strcat("root" "file")', '/tmp/file.pwl', '"a" include "evil"', '"a"\nparameters injected=1',
                  '"a\\ninclude evil"', '"a\\u0000b"', '"a\\tb"', '"unterminated']
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                restore_operating_point_text('parameters fileName="before.pwl"\n', contract({'fileName': value}))

    def test_escaped_quotes_remain_inside_one_literal(self):
        value = json.dumps('path with "quotes"/$literal.pwl')
        result, _ = restore_operating_point_text('parameters fileName="before"\n', contract({'fileName': value}))
        self.assertEqual('parameters fileName=' + value + '\n', result)

    def test_string_temperature_is_invalid(self):
        op = contract({'temp': '"27"'})
        op['parameter_records'][0]['type'] = 'temperature'
        with self.assertRaises(ValidationError):
            restore_operating_point_text('simulatorOptions options temp=27\n', op)

    def test_only_explicit_mapped_file_variables_are_excluded(self):
        mapping = {'vgs_variable': 'gateBias', 'vds_variable': 'drainBias', 'vgs_pwl_variable': 'any_name', 'other': 'corner_file'}
        self.assertEqual(['any_name', 'drainBias', 'gateBias'], replaceable_bias_variables(mapping))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.scs'
            path.write_text('parameters any_name="a.pwl" corner_file="slow" bias=1\nopts options temp=27\n')
            first = bound_source(path, {'any_name': '"a.pwl"', 'corner_file': '"slow"'})
            second = bound_source(path, {'any_name': '"b.pwl"', 'corner_file': '"slow"'}, point_id=2)
            other = bound_source(path, {'any_name': '"b.pwl"', 'corner_file': '"fast"'}, point_id=3)
            excluded = replaceable_bias_variables(mapping)
            self.assertEqual(operating_template_key(first, excluded), operating_template_key(second, excluded))
            self.assertNotEqual(operating_template_key(first, excluded), operating_template_key(other, excluded))
            self.assertNotEqual(operating_template_key(first, excluded), operating_template_key(dict(first, corner='hot'), excluded))
            self.assertEqual({'corner_file': {'string': 'slow'}}, operating_nonbias_parameters(first, excluded))

    def test_equal_frequency_or_offset_does_not_mix_string_corners(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.scs'
            path.write_text('parameters MODE="slow" OFFSET=10k arbitraryPath="g.pwl"\nopts options temp=27\n')
            choices = [bound_source(path, {'MODE': '"slow"', 'OFFSET': 10000, 'arbitraryPath': '"one.pwl"'}),
                       bound_source(path, {'MODE': '"fast"', 'OFFSET': 10000, 'arbitraryPath': '"two.pwl"'})]
            vco = bound_source(path, {'MODE': '"fast"', 'OFFSET': 10000})
            self.assertIs(choices[1], match_flicker_operating_source(choices, vco, ['arbitraryPath']))
            vco_100k = bound_source(path, {'MODE': '"fast"', 'OFFSET': 100000})
            with self.assertRaises(ValidationError):
                match_flicker_operating_source(choices, vco_100k, ['arbitraryPath'])

    def test_numeric_and_string_corner_values_are_different(self):
        string = {'operating_point': contract({'MODE': '"1"'})}
        number = {'operating_point': contract({'MODE': 1})}
        self.assertNotEqual(operating_nonbias_parameters(string), operating_nonbias_parameters(number))

    def test_real_sqlite_rdb_string_value_retains_test_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            rdb = Path(directory) / 'history.rdb'
            with sqlite3.connect(str(rdb)) as connection:
                connection.executescript('CREATE TABLE parameter(parameterID INTEGER, name TEXT, testID TEXT, type TEXT);'
                    'CREATE TABLE parameterValue(pointID INTEGER, parameterID INTEGER, value NUMERIC);'
                    'CREATE TABLE localParameterValue(pointID INTEGER, parameterID INTEGER, testID INTEGER, value NUMERIC);')
                connection.execute('INSERT INTO parameter VALUES(1,?,?,?)', ('some_file', '', 'desVar'))
                connection.execute('INSERT INTO parameterValue VALUES(1,1,?)', ('"global.pwl"',))
                connection.execute('INSERT INTO localParameterValue VALUES(1,1,4,?)', ('"local.pwl"',))
            _, values = _read_parameter_tables(str(rdb))
            params, records, ambiguous = _parameters_for_run(values['1'], 4)
            self.assertFalse(ambiguous)
            text, _ = restore_operating_point_text('parameters some_file="old.pwl"\n', {'params': params, 'parameter_records': records})
            self.assertEqual('parameters some_file="local.pwl"\n', text)
            self.assertEqual('"global.pwl"', _parameters_for_run(values['1'], 5)[0]['some_file'])


if __name__ == '__main__':
    unittest.main()
