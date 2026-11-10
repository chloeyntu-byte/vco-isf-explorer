import copy
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from isf_tool.core.errors import DiscoveryError, ValidationError
from isf_tool.core.maestro_context import SCHEMA, load_maestro_context, refresh_maestro_context
from isf_tool.core.maestro_pair import _history_binding, _history_records, inspect_maestro, inspect_maestro_pair


class MaestroContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'context.txt'

    def protocol(self, history='Interactive.1', library='LIB', extra=''):
        fields = [library, 'VCO with spaces', 'maestro', str(self.root),
                  str(self.root / 'library/VCO/maestro'), history, str(self.root/'bin/ocean')]
        encoded = lambda v: 'nil' if v is None else json.dumps(v)
        return (SCHEMA+'\nC\t'+'\t'.join(encoded(v) for v in fields)+'\n'
                'R\t"/cadence/lib"\nE\t"PATH"\t"/cadence/bin:/usr/bin"\n'
                'E\t"CDS_LIC_FILE"\t"27000@local"\n'
                'H\t"Interactive.1"\t"simulation/Interactive.1"\t"catalog/Interactive.1.rdb"\n'+extra)

    def load(self, text=None, consume=False):
        self.path.write_text(text or self.protocol())
        return load_maestro_context(self.path, consume=consume)

    def test_native_context_paths_and_null_history_without_guessing(self):
        context = self.load(self.protocol(history=None))
        self.assertIsNone(context['selected_history'])
        self.assertEqual(context['histories'][0]['rdb_file'], str(self.root/'catalog/Interactive.1.rdb'))
        self.assertEqual(context['histories'][0]['results_dir'], str(self.root/'simulation/Interactive.1'))
        self.assertEqual('VCO with spaces', context['cell'])
        self.assertTrue(self.path.exists())

    def test_consume_only_successful_context_and_keep_ocean_alias(self):
        (self.root/'bin').mkdir()
        (self.root/'bin/ocean').symlink_to('/usr/bin/true')
        context = self.load(consume=True)
        self.assertEqual(str(self.root/'bin/ocean'), context['ocean'])
        self.assertFalse(self.path.exists())
        self.path.write_text(self.protocol()+'X\t"ignored"\n')
        with self.assertRaises(ValidationError):
            load_maestro_context(self.path)
        self.assertTrue(self.path.exists())

    def test_reject_code_duplicate_records_unapproved_environment_and_limits(self):
        bad = [self.protocol().replace('"LIB"', '(system "touch /tmp/not-allowed")'),
               self.protocol(extra='E\t"LD_PRELOAD"\t"/anything"\n'),
               self.protocol(extra='E\t"PATH"\t"/replacement"\n'),
               self.protocol(extra='H\t"Interactive.1"\tnil\tnil\n'),
               self.protocol(library='../different'),
               self.protocol(library='X'*16385),
               self.protocol().replace('"LIB"', '"L\\nIB"'),
               'x'*(1024*1024+1)]
        for text in bad:
            with self.subTest(text=text[:80]):
                self.path.write_text(text)
                with self.assertRaises(ValidationError):
                    load_maestro_context(self.path)
                self.assertTrue(self.path.exists())

    def fake_refresh(self, context, changed_library=None):
        def run(args, **kwargs):
            script = Path(args[-1]).read_text()
            self.assertIn('?mode "r"', script)
            self.assertNotIn('axlGetWindowSession', script)
            self.assertNotIn('maeSave', script)
            self.assertEqual(str(self.root), kwargs['cwd'])
            self.assertEqual('/cadence/lib', kwargs['env']['LD_LIBRARY_PATH'])
            self.assertEqual('/cadence/bin:/usr/bin', kwargs['env']['PATH'])
            output = json.loads(re.search(r'port = outfile\(("(?:[^"\\]|\\.)*")\)', script).group(1))
            Path(output).write_text(self.protocol(history=context['selected_history'], library=changed_library or context['library'],
                extra='H\t"Interactive.2"\t"simulation/Interactive.2"\t"catalog/Interactive.2.rdb"\n'))
            return mock.Mock(returncode=0)
        return run

    def test_refresh_same_setup_preserves_missing_selection_and_python_environment(self):
        context = self.load(self.protocol(history='Deleted_history'))
        before, env = copy.deepcopy(context), dict(os.environ)
        with mock.patch('isf_tool.core.maestro_context.subprocess.run', side_effect=self.fake_refresh(context)):
            updated = refresh_maestro_context(context)
        self.assertEqual('Deleted_history', updated['selected_history'])
        self.assertEqual(2, len(updated['histories']))
        self.assertEqual(context['launch_id'], updated['launch_id'])
        self.assertEqual(before, context)
        self.assertEqual(env, dict(os.environ))

    def test_refresh_rejects_changed_library_and_never_scans_fallback(self):
        context = self.load()
        with mock.patch('isf_tool.core.maestro_context.subprocess.run', side_effect=self.fake_refresh(context, 'OTHER')):
            with self.assertRaisesRegex(DiscoveryError, 'identity changed'):
                refresh_maestro_context(context)
        with mock.patch('isf_tool.core.maestro_context.subprocess.run', return_value=mock.Mock(returncode=1)):
            with self.assertRaisesRegex(DiscoveryError, 'refresh failed'):
                refresh_maestro_context(context)


class NativeHistoryCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.maestro = self.root/'view'
        self.maestro.mkdir()

    def history(self, path, complete=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = self.root/'raw'/path.stem
        with sqlite3.connect(str(path)) as con:
            con.executescript('''CREATE TABLE psfTable(condition TEXT, rawDir TEXT, resultsDir TEXT);
                CREATE TABLE point(pointID INTEGER, designPointNumber INTEGER, cornerID INTEGER);
                CREATE TABLE test(testID INTEGER, name TEXT);
                CREATE TABLE corner(cornerID INTEGER, name TEXT);
                CREATE TABLE testStatus(pointID INTEGER, testID INTEGER, statusCode INTEGER);
                INSERT INTO point VALUES(1,1,1); INSERT INTO test VALUES(1,'VCO');
                INSERT INTO corner VALUES(1,'Fhigh'); INSERT INTO testStatus VALUES(1,1,3);''')
            con.execute('INSERT INTO psfTable VALUES(?,?,?)', ('one', str(raw), str(raw)))
        case=raw/'1/VCO'; psf=case/'psf'; netlist=case/'netlist'
        psf.mkdir(parents=True);netlist.mkdir()
        (psf/'.simDone').touch()
        (psf/'spectre.out').write_text('spectre completes with %s errors, 0 warnings.\n' % (0 if complete else 1))
        (netlist/'input.scs').write_text('simulator lang=spectre\nparameters VDD=0.6\n')
        return path

    def test_exact_external_rdb_still_checks_completion_and_hashes(self):
        rdb=self.history(self.root/'external/Interactive.9.rdb')
        histories, selected = _history_records(str(self.maestro),'VCO',requested_history=str(rdb))
        self.assertTrue(selected['fully_complete'])
        before = _history_binding(selected)[0]
        netlist = Path(selected['source_netlists'][0]['path'])
        netlist.write_text(netlist.read_text()+'// changed\n')
        self.assertNotEqual(before,_history_binding(selected)[0])
        (netlist.parent.parent/'psf/spectre.out').write_text('spectre completes with 1 errors.\n')
        histories, selected = _history_records(str(self.maestro),'VCO',requested_history=str(rdb))
        self.assertIsNone(selected)
        self.assertFalse(histories[0]['fully_complete'])

    def test_explicit_old_history_not_lost_to_sixteen_entry_scan_limit(self):
        directory=self.maestro/'results/maestro'
        target=self.history(directory/'Interactive.0.rdb')
        os.utime(target,(1,1))
        for index in range(1,18):
            path=directory/('Interactive.%d.rdb'%index)
            path.write_text('unreadable newer history')
            os.utime(path,(index+1,index+1))
        histories, selected=_history_records(str(self.maestro),'VCO',requested_history='Interactive.0')
        self.assertEqual(str(target),selected['path'])
        self.assertEqual(17,len(histories))

    def test_catalog_not_default_scan_and_explicit_blank_or_missing_never_fallback(self):
        rdb=self.history(self.root/'external/Pinned.rdb')
        self.history(self.maestro/'results/maestro/Unrelated.rdb')
        catalog=[{'name':'Native_name','rdb_file':str(rdb),'results_dir':'not-completion-evidence'}]
        histories, selected=_history_records(str(self.maestro),'VCO',history_catalog=catalog,explicit_history_selection=True)
        self.assertEqual(['Native_name'],[h['name'] for h in histories]);self.assertIsNone(selected)
        histories, selected=_history_records(str(self.maestro),'VCO',history_catalog=catalog,requested_history='Native_name')
        self.assertEqual(str(rdb),selected['path'])
        histories, selected=_history_records(str(self.maestro),'VCO',history_catalog=catalog,requested_history='Missing')
        self.assertIsNone(selected)
        histories, selected=_history_records(str(self.maestro),'VCO',history_catalog=catalog,requested_history=str(self.root/'missing.rdb'))
        self.assertIsNone(selected)
        self.assertTrue(histories[0]['error'])
        foreign=self.maestro/'results/maestro/Unrelated.rdb'
        histories, selected=_history_records(str(self.maestro),'VCO',history_catalog=catalog,requested_history=str(foreign))
        self.assertIsNone(selected)
        self.assertIn('not registered',histories[0]['error'])

    def test_pair_forwards_catalog_and_selection_policy_to_both_role_inspections(self):
        payload={'testbenches':{role:{'maestro_dir':str(self.maestro/role),'history_catalog':[],
                  'explicit_history_selection':True} for role in ('vco','replica')}}
        with mock.patch('isf_tool.core.maestro_pair.inspect_maestro',return_value={}) as inspect:
            inspect_maestro_pair(payload)
        self.assertEqual(2,inspect.call_count)
        for call in inspect.call_args_list:
            self.assertEqual([],call.args[0]['history_catalog'])
            self.assertTrue(call.args[0]['explicit_history_selection'])

    def noise_setup(self):
        tests = ['Noise_A', 'Noise_B']
        (self.maestro/'maestro.sdb').write_text('<setupdb version="6"><active><tests>'+''.join(
            '<test enabled="1">%s<tooloptions><option>lib<value>LIB</value></option>'
            '<option>cell<value>Noise</value></option><option>view<value>schematic</value></option>'
            '</tooloptions><vars><var>VGS<value>0.6</value></var><var>VDS<value>0.6</value></var>'
            '</vars></test>' % name for name in tests)+'</tests></active></setupdb>')
        (self.maestro/'active.state').write_text('<statedb version="5">'+''.join(
            '<Test Name="%s"><component Name="analyses"><analyses Name="analysis">'
            '<analysis Name="noise"><field Name="enable">(t)</field><field Name="start">1k</field>'
            '<field Name="stop">1M</field></analysis></analyses></component></Test>' % name
            for name in tests)+'</statedb>')
        rdb=self.history(self.maestro/'results/maestro/Selected.rdb')
        with sqlite3.connect(str(rdb)) as con:
            con.execute("UPDATE test SET name='Noise_B'")
        old=self.root/'raw/Selected/1/VCO'
        old.rename(old.with_name('Noise_B'))
        return rdb

    def test_explicit_complete_history_uniquely_disambiguates_actual_noise_test(self):
        rdb=self.noise_setup()
        report=inspect_maestro({'path':str(self.maestro),'role':'flicker','history':str(rdb)})
        self.assertEqual('Noise_B',report['selected_test']['name'])
        self.assertEqual('explicit_history_unique_test',report['selection'])
        self.assertEqual(str(rdb),report['selected_history']['path'])
        self.assertTrue(report['history_fingerprint'])

    def test_no_history_invalid_explicit_test_and_incomplete_history_do_not_disambiguate(self):
        rdb=self.noise_setup()
        base={'path':str(self.maestro),'role':'flicker','explicit_history_selection':True}
        self.assertIsNone(inspect_maestro(base)['selected_test'])
        self.assertIsNone(inspect_maestro(dict(base,history=str(rdb),test='Missing'))['selected_test'])
        (self.root/'raw/Selected/1/Noise_B/psf/spectre.out').write_text('spectre completes with 1 errors.\n')
        self.assertIsNone(inspect_maestro(dict(base,history=str(rdb)))['selected_test'])

    def test_two_complete_role_tests_in_selected_history_remain_ambiguous(self):
        rdb=self.noise_setup()
        with sqlite3.connect(str(rdb)) as con:
            con.execute("INSERT INTO test VALUES(2,'Noise_A')")
            con.execute('INSERT INTO testStatus VALUES(1,2,3)')
        case=self.root/'raw/Selected/1/Noise_B'
        shutil.copytree(case,case.with_name('Noise_A'))
        report=inspect_maestro({'path':str(self.maestro),'role':'flicker','history':str(rdb)})
        self.assertIsNone(report['selected_test'])
        self.assertEqual('ambiguous',report['selection'])


if __name__ == '__main__':
    unittest.main()
