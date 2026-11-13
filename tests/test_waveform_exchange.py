"""The waveform handoff is portable, differential, isolated, and cache sealed."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from isf_tool.core.waveform_exchange import (
    channel_variables, export_waveforms, inspect_waveform_exchange,
    set_root_parameters, validate_waveform_assets, waveform_tables,
)
from tests import test_portable_setup as fixtures
from tests.test_transfer_mapping_frequency_cache import ENGINE


class WaveformExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.traces = {key: key for key in ('gate', 'drain', 'source', 'bulk')}
        self.wave = {'f0': 1e9, 'period': 1e-9, 'time': [0, 5e-10, 1e-9],
                     'gate': [.8, 1.2, .8], 'drain': [1.5, 1.9, 1.5],
                     'source': [.2, .3, .2], 'bulk': [.1, .15, .1]}

    def test_differential_plus_common_mode_reconstructs_every_terminal(self):
        tables = waveform_tables(self.wave, self.traces)
        values = {key: [float(line.split()[1]) for line in text.splitlines()] for key, text in tables.items()}
        for semantic, terminal in [('VGS', 'gate'), ('VDS', 'drain'), ('VBS', 'bulk')]:
            for expected, delta, common in zip(self.wave[terminal], values[semantic], values['VS']):
                self.assertAlmostEqual(expected, delta + common, places=14)

    def test_relocation_needs_no_original_directory_and_keeps_custom_globals(self):
        original = self.root / 'original/netlist'
        manifest = export_waveforms(self.wave, self.traces, original, {'vgs_pwl_variable': 'arbitraryGateFile'})
        self.assertEqual('arbitraryGateFile', manifest['channels'][0]['variable'])
        moved = self.root / 'moved/netlist'
        shutil.move(str(original), str(moved))
        self.assertFalse(original.exists())
        self.assertEqual(manifest, validate_waveform_assets(moved))
        self.assertTrue(all(not Path(row['relative_file']).is_absolute() for row in manifest['channels']))

    def test_tampering_missing_manifest_and_path_traversal_fail(self):
        root = self.root / 'netlist'
        export_waveforms(self.wave, self.traces, root)
        (root / 'input.scs').write_text('// ISF waveform-exchange-v1\n')
        (root / 'waveforms/vgs.pwl').write_text('0 100\n')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            validate_waveform_assets(root)
        manifest = export_waveforms(self.wave, self.traces, root)
        manifest['channels'][0]['relative_file'] = '../escape'
        path = root / 'waveforms/manifest.json'
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'relative to the current netlist'):
            validate_waveform_assets(root)
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'manifest is missing'):
            validate_waveform_assets(root)

    def test_invalid_waveforms_and_variable_collisions_are_rejected(self):
        for key, value in [('time', [0, 0, 1e-9]), ('gate', [0, float('nan'), 0]),
                           ('drain', [1, 2, 3]), ('period', 0), ('f0', 1)]:
            wave = dict(self.wave, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                waveform_tables(wave, self.traces)
        for mapping in [{'vgs_pwl_variable': 'bad/name'}, {'vgs_pwl_variable': 'ISF_T0'}, {'vgs_pwl_variable': 'same', 'vds_pwl_variable': 'same'}]:
            with self.assertRaises(ValueError):
                channel_variables(mapping)

    def test_root_string_override_preserves_nested_defaults(self):
        text = 'parameters PATH="/legacy/data/file" VGS=.8\nsubckt dut (d g s b)\nparameters PATH="nested"\nends dut\n'
        updated = set_root_parameters(text, {'PATH': '"waveforms/vgs.pwl"', 'NEW': '"waveforms/vbs.pwl"'})
        self.assertIn('parameters PATH="nested"', updated)
        self.assertIn('PATH="waveforms/vgs.pwl" VGS=.8', updated)
        self.assertNotIn('/legacy/data', updated)
        with self.assertRaises(ValueError):
            set_root_parameters('parameters F="a"\nparameters F="b"\n', {'F': '"c"'})

    def test_static_inspection_is_explicit_and_missing_history_blocks(self):
        fixture = fixtures.PortableSetupTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        report = fixture.reports()
        exchange = inspect_waveform_exchange(report, fixture.build(report)['engine'])
        self.assertTrue(exchange['ok'], exchange)
        self.assertEqual('static_prepare', exchange['checked'])
        self.assertTrue(exchange['dc_baseline_required'])
        self.assertIn('after the VCO simulation', exchange['summary'])
        report['roles']['vco']['selected_history'] = {}
        self.assertFalse(inspect_waveform_exchange(report)['ok'])

    def test_private_periodic_and_dc_paths_do_not_consume_old_absolute_pwl(self):
        fixture = fixtures.PortableSetupTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.replica.write_text(fixture.replica.read_text().replace('type=dc', 'type=pwl file="/nonexistent/old-host.pwl"', 2))
        cfg = fixture.build()['engine']
        cfg['flicker_template_netlist'] = str(fixture.replica)
        wave = dict(self.wave)
        for terminal, trace in cfg['physical_profile']['main']['traces'].items():
            if terminal in self.traces:
                wave[trace] = self.wave[terminal]
        periodic = fixture.root / 'private/replica/netlist/input.scs'
        ENGINE.prepare_replica_netlist(cfg, wave, periodic)
        text = periodic.read_text()
        self.assertIn('file=ISF_VGS_PWL_FILE', text)
        self.assertIn('VG (G S)', text)
        self.assertNotIn('/nonexistent/old-host.pwl', text)
        self.assertTrue(validate_waveform_assets(periodic.parent))
        identity = ENGINE._simulation_cache_identity(cfg, periodic.parent)
        self.assertIsNotNone(identity['waveform_exchange'])
        dc = fixture.root / 'private/dc/netlist/input.scs'
        ENGINE.prepare_dc_noise_netlist(cfg, {'VGS_V': .8, 'VDS_V': 1.2, 'VBS_V': 0}, dc)
        self.assertNotIn('/nonexistent/old-host.pwl', dc.read_text())
        self.assertIn('G_SOURCE (G_INT VSS) vsource dc=G_BIAS type=dc', dc.read_text())

    def test_unselected_histories_return_readable_checks_not_exception(self):
        exchange = inspect_waveform_exchange({'roles': {'vco': {'selected_history': None}, 'flicker': {'selected_history': None}}})
        self.assertFalse(exchange['ok'])
        self.assertIn('source_history', [item['id'] for item in exchange['checks'] if item['status'] == 'fail'])

    def test_actual_unpreparable_source_and_invalid_electrical_mapping_fail_static(self):
        fixture = fixtures.PortableSetupTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        report = fixture.reports()
        report['roles']['flicker']['mapping']['vgs_source'] = 'X.NESTED_SOURCE'
        exchange = inspect_waveform_exchange(report)
        self.assertFalse(exchange['ok'])
        self.assertTrue(any(item['id'] == 'flicker_netlist_prepare' and item['status'] == 'fail' for item in exchange['checks']))
        report = fixture.reports()
        report['roles']['vco']['mapping_issues'].append({'severity': 'error', 'message': 'wrong device'})
        self.assertFalse(inspect_waveform_exchange(report)['ok'])

    def test_manifest_symlink_cannot_overwrite_external_file(self):
        outside = self.root / 'outside.json'
        outside.write_text('preserve')
        folder = self.root / 'netlist/waveforms'
        folder.mkdir(parents=True)
        (folder / 'manifest.json').symlink_to(outside)
        with self.assertRaises(ValueError):
            export_waveforms(self.wave, self.traces, folder.parent)
        self.assertEqual('preserve', outside.read_text())

    def test_custom_analysis_names_keep_existing_engine_support(self):
        fixture = fixtures.PortableSetupTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        report = fixture.reports()
        report['roles']['vco']['mapping'].update(periodic_analysis='periodic_run', noise_analysis='pn_measure')
        fixture.vco.write_text(fixture.vco.read_text().replace('hb (', 'periodic_run (').replace('hbnoise (', 'pn_measure ('))
        exchange = inspect_waveform_exchange(report)
        self.assertTrue(exchange['ok'], exchange)
