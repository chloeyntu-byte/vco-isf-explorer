"""Shared thermal acquisition contracts for generated and manual MOS fixtures."""
import copy
import csv
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from isf_tool.core import thermal_execution as thermal
from isf_tool.core.thermal_budget import thermal_evidence
from scripts import cadence_dual_tb_runner as runner
from scripts import dynamic_isf_closure_engine as engine


class GeneratedThermalTests(unittest.TestCase):
    def source(self, device='M_ISF_DC.m0', family='channel_thermal', units='A^2/Hz', value=1e-18):
        return {(device, 'id'): dict(noise_family=family, units=units, value=value)}

    def test_filter_uses_mechanisms_and_exact_occurrence_boundary(self):
        rows = self.source()
        rows.update(self.source('M_ISF_DC2.m0'))
        rows.update(self.source('M_ISF_DC.rgate', family='resistive_thermal'))
        rows.update(self.source('M_ISF_DC.flicker', family='flicker'))
        rows.update(self.source('M_ISF_DC.shot', family='shot'))
        selected = thermal._mos_white_sources(rows, rows, rows, 'M_ISF_DC', 'TOP.gm')
        self.assertEqual({('TOP.gm.m0', 'id'), ('TOP.gm.rgate', 'id')}, set(selected))

    def test_white_spotcheck_rejects_frequency_dependence_missing_sources_and_units(self):
        low = self.source()
        for bad in ({}, self.source(value=1.001e-18), self.source(units='V^2/Hz')):
            for position in (0, 1):
                samples = [low, low]
                samples[position] = bad
                with self.subTest(bad=bad, position=position), self.assertRaises(ValueError):
                    thermal._mos_white_sources(low, *samples, 'M_ISF_DC', 'M1')

    def setup_case(self, root, fixture):
        text = 'model nch bsim4\nM (a b 0 0) nch w=1u l=1u\nhb hb fundfreqs=[1G]\nnoise (a 0) noise values=[1k]\n'
        for relative in ('main_vco', 'dc_flicker/sample_01', 'dc_flicker/sample_02'):
            netlist = root / relative / 'netlist'
            netlist.mkdir(parents=True)
            (netlist / 'input.scs').write_text(text)
        table = root / 'dc_flicker/iflicker_samples.csv'
        with table.open('w') as handle:
            writer = csv.DictWriter(handle, ['time_s'])
            writer.writeheader()
            writer.writerows([{'time_s': value} for value in (0, .5e-9, 1e-9)])
        nodes = ('bsim3v3: M1.m0 of nch\n d : drain\n g : gate\n s : source\n b : 0\n'
                 'resistor: M1.rgate\n 1 : gate\n 2 : gi\n'
                 'inductor: TOP.L\n 1 : drain\n 2 : 0\n'
                 'capacitor: TOP.C.cap\n 1 : drain\n 2 : 0\n'
                 'bsim3v3: TOP.active\n d : drain\n g : gate\n s : source\n b : 0\n'
                 'resistor: TOP.C.r\n 1 : drain\n 2 : 0\n')
        def simulate(cfg, target, **kwargs):
            if target.parent.name == 'main':
                (target.parent / 'thermal_nodes.txt').write_text(nodes)
        def copy_tree(source, destination):
            destination.mkdir(parents=True)
            (destination / 'input.scs').write_text((source / 'input.scs').read_text())
        fake = SimpleNamespace(
            physical_profile=lambda cfg: {'main': {'instance_path':'M1', 'target_instance':'M1',
                'traces':{'target_noise':'M1.m0', 'peer_noise':'M0.m0'}},
                'replica':{'dc_instance':fixture}},
            main_output=lambda cfg: {'negative':'drain', 'positive':'0'},
            exact_hb_fundamental=lambda cfg, path:1e9,
            _copy_regular_tree=copy_tree, run_spectre=mock.Mock(side_effect=simulate),
            aggregate_spectre_quality=lambda paths:{'pass':True},
            _strict_json_dumps=lambda value, **kwargs:json.dumps(value, **kwargs))
        return fake

    def dataset(self, eng, cfg, path):
        values = {'drain':2j, 'gate':1j, 'source':0j, 'gi':.5j,
                  'M1.m0:int_d':1j, 'M1.m0:int_s':0j, 'TOP.L:1':1j}
        if self.missing_node:
            values.pop('M1.m0:int_d')
        return {'header':{'version':self.version, 'harms':4}, 'values':values, 'path':str(path)}

    def rows(self, dataset, frequency):
        if 'thermal_sources.noise' in dataset['path']:
            rows = self.source(self.fixture+'.m0')
            rows[(self.fixture+'.rgate', 'rn')] = dict(noise_family='resistive_thermal', units='A^2/Hz', value=1e-19)
            return rows
        return {(device, 'rn'):dict(noise_family='resistive_thermal', units='A^2/Hz', value=1e-19)
                for device in ('TOP.L','TOP.C.r')}

    def run_case(self, root, fixture, **overrides):
        self.fixture = fixture
        self.missing_node = overrides.get('missing_node', False)
        self.version = overrides.get('version', "18.1.0.001")
        fake = self.setup_case(root, fixture)
        cfg = dict(replica_source='generated' if fixture=='M_ISF_DC' else 'maestro',
                   offset_hz=1000, max_harmonic=1, pair_policy={'proven':overrides.get('proven', True)})
        with mock.patch.object(thermal, '_read', side_effect=self.dataset), \
             mock.patch.object(thermal, '_source_rows', side_effect=self.rows), \
             mock.patch.object(thermal, 'noise_sample', side_effect=lambda data, *args, **kw: data):
            result = thermal.run_optional_thermal(fake, cfg, {}, root)
        return result, fake

    def test_full_generated_and_manual_acquisition_are_identical_with_single_ended_carrier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated, _ = self.run_case(root/'generated', 'M_ISF_DC')
            manual, _ = self.run_case(root/'manual', 'M_REF')
            self.assertEqual('available', generated['status'], generated)
            self.assertEqual(manual, generated)
            self.assertEqual(2, generated['carrier_peak_V'])
            self.assertEqual(6, len(generated['sources']))
            self.assertEqual(['TOP.C','TOP.L'], generated['passive_roots'])
            self.assertEqual('available', thermal_evidence({'thermal_noise':generated})['status'])

    def test_missing_internal_node_yields_na(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = self.run_case(Path(directory), 'M_ISF_DC', missing_node=True)
            self.assertEqual('unavailable', result['status'])
            self.assertIn('missing intrinsic node', result['reason'])

    def test_unproven_pair_never_doubles_thermal(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = self.run_case(Path(directory), 'M_ISF_DC', proven=False)
            self.assertEqual('unavailable', result['status'])
            self.assertIn('symmetry', result['reason'])

    def test_non18_spectre_is_explicit_na_before_thermal_simulations(self):
        for version in ('23.1.0', '', '18.10.0'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                result, fake = self.run_case(Path(directory), 'M_ISF_DC', version=version)
                self.assertEqual('unavailable', result['status'])
                self.assertIn('N/A:', result['reason'])
                self.assertIn(version or 'unknown', result['reason'])
                self.assertEqual('spectre18_thermal_reference', result['capability'])
                fake.run_spectre.assert_not_called()

    def test_runner_runs_generated_thermal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            netlist = root/'point/replica/netlist/input.scs'
            netlist.parent.mkdir(parents=True)
            netlist.write_text('')
            fake = mock.Mock()
            fake.point_name.return_value = 'point'
            with mock.patch.object(thermal, 'run_optional_thermal', return_value={'status':'available'}) as run:
                runner.run_replica(fake, {'replica_source':'generated'}, [{}], root)
                run.assert_called_once()


class MeasuredPairTests(unittest.TestCase):
    def test_sampling_uses_precise_fd_period_when_header_is_rounded(self):
        import numpy as np
        rounded_f0 = 2e9
        f0 = rounded_f0 + 123.25
        period = 1 / f0
        with mock.patch.object(engine, 'exact_hb_fundamental', return_value=f0), \
             mock.patch.object(engine, 'fundamental', return_value=rounded_f0), \
             mock.patch.object(engine, 'parse_real_wave', return_value=(np.array([0, period/2, period]), np.array([1, -1, 1]))):
            wave = engine.sample_periodic_wave({}, Path('/unused'), ['OUTP'], samples=65)
        self.assertEqual(period, wave['period'])
        self.assertEqual(period, wave['time'][-1])

    def test_cross_coupled_pair_still_requires_measured_bias(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.scs'
            source.write_text('model nch bsim4\nA (op on 0 0) nch w=2u l=1u\nB (on op 0 0) nch w=2u l=1u\n')
            receipt = root / 'main_vco/netlist/input.isf-patch.json'
            receipt.parent.mkdir(parents=True)
            manifest = {'source_netlist': str(source), 'pair_policy': {'proven': True}}
            cfg = {'vco_mapping': {'peer_instance': 'B'}}
            phase = np.linspace(0, 2 * np.pi, 1025)
            for common_mode in (0, .02):
                with self.subTest(common_mode=common_mode):
                    receipt.write_text(json.dumps(manifest))
                    wave = {'op': np.sin(phase) + common_mode * np.sin(phase),
                            'on': -np.sin(phase) + common_mode * np.sin(phase),
                            '0': np.zeros_like(phase)}
                    with mock.patch.object(engine, 'physical_profile', return_value={'main': {'target_instance': 'A'}}), \
                         mock.patch.object(engine, 'sample_periodic_wave', return_value=wave):
                        policy = engine.prove_measured_pair_symmetry(cfg, root)
                    self.assertEqual(common_mode == 0, policy['proven'])
                    self.assertEqual(policy, json.loads(receipt.read_text())['pair_policy'])

    def test_bias_proof_accepts_half_period_and_rejects_bias_mismatch(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source.scs'
            source.write_text('model nch bsim4\nA (op g1 0 0) nch w=2u l=1u\nB (on g2 0 0) nch w=2u l=1u\n')
            receipt = root/'main_vco/netlist/input.isf-patch.json'
            receipt.parent.mkdir(parents=True)
            manifest = {'source_hashes':{str(source):'unused'}, 'pair_policy':{'proven':False}}
            cfg = {'vco_mapping':{'peer_instance':'B'}}
            phase = np.linspace(0, 2*np.pi, 1025)
            wave = {'op':np.sin(phase), 'on':-np.sin(phase), 'g1':-np.sin(phase),
                    'g2':np.sin(phase), '0':np.zeros(1025)}
            for mismatch in (False, True):
                receipt.write_text(json.dumps(manifest))
                data = copy.deepcopy(wave)
                if mismatch: data['g2'] += .02
                with mock.patch.object(engine, 'physical_profile', return_value={'main':{'instance_path':'A', 'target_instance':'A'}}), \
                     mock.patch.object(engine, 'sample_periodic_wave', return_value=data):
                    policy = engine.prove_measured_pair_symmetry(cfg, root)
                self.assertEqual(not mismatch, policy['proven'])
                self.assertIn('bias_evidence', policy)
