"""Offline regression for RDB operating points and private materialization."""

import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from isf_tool.core.errors import DiscoveryError, ValidationError
from isf_tool.core.maestro_pair import (
    _history_records, _operating_environment, _root_statements,
    match_flicker_operating_source, operating_template_key,
    restore_operating_point_text,
)
from isf_tool.core.psf_extract import _read_parameter_tables, _parameters_for_run


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('operating_runner', ROOT / 'scripts/cadence_dual_tb_runner.py')
RUNNER = importlib.util.module_from_spec(spec)
spec.loader.exec_module(RUNNER)


def contract(params, kinds=None):
    return {'params': params, 'ambiguous_params': {}, 'parameter_records': [
        {'name': name, 'value': value, 'type': (kinds or {}).get(name, 'desVar'), 'testID': ''}
        for name, value in params.items()]}


def source(path, params=None, corner='', point=1, kinds=None):
    op = contract(params or {}, kinds)
    rendered, changes = restore_operating_point_text(path.read_text(), op)
    op.update({'point_id': point, 'corner': corner, 'restored_values': changes,
               'effective_netlist_sha256': hashlib.sha256(rendered.encode()).hexdigest(),
               'environment': _operating_environment(rendered, str(path))})
    return {'path': str(path), 'corner': corner, 'point_id': point,
            'design_point': 1, 'test': 'test', 'operating_point': op}


class OperatingRestoreTests(unittest.TestCase):
    def test_restores_only_unique_case_sensitive_root_parameter(self):
        text = 'parameters SW=0 sw=2\nsubckt local d g\nparameters SW=7\nends local\n'
        updated, changes = restore_operating_point_text(text, contract({'SW': 1}))
        self.assertEqual('parameters SW=1 sw=2\nsubckt local d g\nparameters SW=7\nends local\n', updated)
        self.assertEqual('0', changes[0]['original'])
        self.assertEqual('1', changes[0]['effective'])

    def test_nested_expression_and_quoted_embedded_name_are_not_truncated(self):
        text = 'parameters note="SW=wrong" SW=(1+(2)) X=3\n'
        updated, _ = restore_operating_point_text(text, contract({'SW': 1}))
        self.assertEqual('parameters note="SW=wrong" SW=1 X=3\n', updated)

    def test_continuations_keep_other_values_and_root_scope(self):
        text = 'parameters A=2 \\\n SW=(1+(2)) \\\n+ VDD=0.8\n'
        updated, _ = restore_operating_point_text(text, contract({'SW': 1, 'VDD': 0.9}))
        self.assertIn(' SW=1 ', updated)
        self.assertIn('+ VDD=0.9', updated)
        self.assertIn('A=2', updated)

    def test_missing_duplicate_unknown_kind_and_ambiguity_fail(self):
        for text, op in [
            ('parameters OTHER=0\n', contract({'SW': 1})),
            ('parameters SW=0\nparameters SW=2\n', contract({'SW': 1})),
            ('parameters SW=0\n', contract({'SW': 1}, {'SW': 'modelGroup'})),
            ('parameters SW=0\n', dict(contract({}), ambiguous_params={'SW': [0, 1]})),
        ]:
            with self.subTest(text=text, op=op), self.assertRaises(ValidationError):
                restore_operating_point_text(text, op)

    def test_unsafe_unresolved_and_nonfinite_values_fail(self):
        for value in ['VDD/2', '1\ninclude "evil"', float('nan'), float('inf'), '1e300G', True]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                restore_operating_point_text('parameters SW=0\n', contract({'SW': value}))

    def test_temperature_existing_or_safe_append_and_nonstandard_name(self):
        for text in ['opts options temp=27\n', 'opts options reltol=1e-3\nsaveOptions options save=allpub\n']:
            updated, changes = restore_operating_point_text(text, contract({'temp': 85}, {'temp': 'temp'}))
            self.assertIn('temp=85', updated)
            self.assertEqual('temperature', changes[0]['kind'])

    def test_temperature_cannot_be_appended_inside_a_comment(self):
        with self.assertRaises(ValidationError):
            restore_operating_point_text('opts options reltol=1e-3 // retained comment\n',
                                         contract({'temp': 85}, {'temp': 'temp'}))
        updated, _ = restore_operating_point_text('opts options temp=27 // comment\n',
                                                  contract({'temp': 85}, {'temp': 'temp'}))
        self.assertEqual('opts options temp=85 // comment\n', updated)

    def test_local_declaration_and_unclosed_expression_never_qualify(self):
        for text in ['subckt a x\nparameters SW=0\nends a\n', 'parameters SW=(1+(2)\n']:
            with self.assertRaises(ValidationError):
                restore_operating_point_text(text, contract({'SW': 1}))


class OperatingPairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'input.scs'
        self.path.write_text('parameters VGS=0.5 SW=0\nopts options temp=27 tnom=27\ninclude "model.scs" section=tt\n')

    def test_bias_only_points_deduplicate_but_other_parameters_do_not(self):
        first = source(self.path, {'VGS': .5, 'SW': 0})
        second = source(self.path, {'VGS': .8, 'SW': 0})
        third = source(self.path, {'VGS': .8, 'SW': 1})
        self.assertEqual(operating_template_key(first, ['VGS']), operating_template_key(second, ['VGS']))
        self.assertNotEqual(operating_template_key(first, ['VGS']), operating_template_key(third, ['VGS']))
        self.assertNotEqual(operating_template_key(first, ['VGS']), operating_template_key(dict(first, corner='hot'), ['VGS']))

    def test_equal_count_never_permits_index_pairing(self):
        vco = source(self.path, {'SW': 0}, 'unknown')
        choices = [source(self.path, {'SW': 0}, 'a'), source(self.path, {'SW': 0}, 'b')]
        with self.assertRaises(ValidationError):
            RUNNER._flicker_source_for_point(choices, vco, 0, 2)

    def test_corner_and_shared_nonbias_params_choose_unique_template(self):
        vco = source(self.path, {'SW': 1}, 'tt')
        choices = [source(self.path, {'SW': 0}, 'tt'), source(self.path, {'SW': 1}, 'tt')]
        self.assertIs(choices[1], match_flicker_operating_source(choices, vco))

    def test_generic_template_requires_equal_physical_settings(self):
        vco = source(self.path, {'SW': 0}, 'hot')
        same = source(self.path, {})
        self.assertIs(same, match_flicker_operating_source([same], vco))
        hot_path = self.path.with_name('hot.scs')
        hot_path.write_text(self.path.read_text().replace('temp=27', 'temp=125'))
        hot = source(hot_path, {'SW': 0}, 'hot')
        with self.assertRaises(ValidationError):
            match_flicker_operating_source([same], hot)

    def test_changed_model_section_is_not_hidden_by_generic_corner(self):
        vco = source(self.path, {'SW': 0}, 'tt')
        other_path = self.path.with_name('ff.scs')
        other_path.write_text(self.path.read_text().replace('section=tt', 'section=ff'))
        with self.assertRaises(ValidationError):
            match_flicker_operating_source([source(other_path)], vco)


class OperatingRdbTests(unittest.TestCase):
    def make_rdb(self, path):
        connection = sqlite3.connect(str(path))
        connection.executescript('''
            CREATE TABLE parameter(parameterID INTEGER, name TEXT, defaultValue TEXT, testID TEXT, type TEXT);
            CREATE TABLE parameterValue(pointID INTEGER, parameterID INTEGER, value NUMERIC);
            CREATE TABLE localParameterValue(pointID INTEGER, parameterID INTEGER, testID INTEGER, value NUMERIC);
            INSERT INTO parameter VALUES(1, 'SW', '0', '', 'desVar');
            INSERT INTO parameterValue VALUES(1, 1, 0);
        ''')
        connection.commit()
        return connection

    def test_local_test_override_has_priority_without_affecting_other_tests(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'history.rdb'
            with self.make_rdb(path) as connection:
                connection.execute('INSERT INTO localParameterValue VALUES(1,1,4,1)')
            _, values = _read_parameter_tables(str(path))
            self.assertEqual({'SW': 1}, _parameters_for_run(values['1'], 4)[0])
            self.assertEqual({'SW': 0}, _parameters_for_run(values['1'], 5)[0])

    def test_model_group_records_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'history.rdb'
            with self.make_rdb(path) as connection:
                connection.execute('CREATE TABLE model(modelGroup TEXT, file TEXT)')
                connection.execute("INSERT INTO model VALUES('ff', 'model.scs')")
            with self.assertRaises(DiscoveryError):
                _read_parameter_tables(str(path))

    def test_history_counts_only_selected_test_and_retains_effective_parameters(self):
        with tempfile.TemporaryDirectory() as root:
            maestro = Path(root) / 'maestro'
            history = maestro / 'results/maestro'
            history.mkdir(parents=True)
            rdb = history / 'Interactive.0.rdb'
            self.make_rdb(rdb).close()
            netlist = Path(root) / 'run/netlist'
            netlist.mkdir(parents=True)
            (netlist / 'input.scs').write_text('parameters SW=7\n')
            psf = netlist.parent / 'psf'
            psf.mkdir()
            fake = {'tests': [{'name': 'selected'}, {'name': 'other'}], 'rawDir': [],
                    'completion': {'checked': 2, 'complete': 1}, 'runs': [
                        {'test': 'selected', 'testID': 4, 'pointID': 1, 'corner': 'tt', 'complete': True, 'psfDir': str(psf)},
                        {'test': 'other', 'testID': 5, 'pointID': 1, 'complete': False}]}
            with mock.patch('isf_tool.core.maestro_pair.inspect_rdb', return_value=fake):
                records, selected = _history_records(str(maestro), 'selected')
            self.assertTrue(selected['fully_complete'], records)
            self.assertEqual(1, selected['checked_runs'])
            self.assertEqual({'SW': 0}, selected['source_netlists'][0]['operating_point']['params'])
            self.assertEqual('parameters SW=7\n', (netlist / 'input.scs').read_text())


class OperatingMaterializationTests(unittest.TestCase):
    def test_shared_template_restores_two_corners_and_hashes_each_private_copy(self):
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            model = root / 'model.scs'
            model.write_text('// model\n')
            vpath = root / 'vco/input.scs'
            vpath.parent.mkdir()
            vpath.write_text('parameters SW=0\ninclude "%s" section=tt\nopts options temp=27\n' % model)
            fpath = root / 'current/input.scs'
            fpath.parent.mkdir()
            fpath.write_text('parameters VGS=0.5 VDS=1\ninclude "%s" section=tt\nopts options temp=27\n' % model)
            original = vpath.read_bytes()
            sources = [source(vpath, {'SW': 0}, 'Fhigh', 1), source(vpath, {'SW': 1}, 'Flow', 2)]
            current = [source(fpath, {'VGS': .5, 'VDS': 1}, '', 1), source(fpath, {'VGS': .8, 'VDS': 1}, '', 2)]
            report = {'ok': True, 'bindings': {}, 'roles': {
                role: {'selected_test': {'models': [{'path': str(model)}]},
                       'selected_history': {'path': 'test.rdb', 'fully_complete': True, 'source_netlists': rows},
                       'mapping': mapping}
                for role, rows, mapping in [('vco', sources, {}), ('flicker', current, {'vgs_variable': 'VGS', 'vds_variable': 'VDS'})]}}
            engine = root / 'engine.json'
            engine.write_text(json.dumps({'model_file': str(model), 'spectre': '/bin/true', 'psf': '/bin/true', 'points': []}))
            run = root / 'run'
            app = {'analysis': {'offset': {'hz': 10000}}, 'testbenches': {}}
            RUNNER.write_json(run / RUNNER.APP_CONFIG_NAME, app)
            class FakeEngine:
                @staticmethod
                def load_config(path):
                    return json.loads(Path(path).read_text())
            with mock.patch.object(RUNNER, 'inspect_maestro_pair', return_value=report):
                result = RUNNER.materialize_maestro_engine(FakeEngine, engine, run, app)
            config = json.loads(result.read_text())
            self.assertEqual(2, len(config['points']))
            for point, expected in zip(config['points'], (0, 1)):
                path = Path(config['source_root']) / point['source_dir'] / 'vco/netlist/input.scs'
                self.assertIn('SW=%d' % expected, path.read_text())
                self.assertEqual(expected, point['operating_point']['params']['SW'])
            manifest = json.loads((run / RUNNER.PAIR_SOURCE_MANIFEST_NAME).read_text())
            self.assertEqual(1, len(manifest['roles']['flicker']))
            self.assertEqual(2, len(manifest['roles']['flicker'][0]['represented_points']))
            high, low = manifest['sources']
            self.assertEqual(high['source_sha256'], low['source_sha256'])
            self.assertNotEqual(high['private_copy_sha256'], low['private_copy_sha256'])
            self.assertEqual(low['private_copy_sha256'], low['operating_point']['effective_netlist_sha256'])
            self.assertEqual(original, vpath.read_bytes())


if __name__ == '__main__':
    unittest.main()
