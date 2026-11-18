import math
import unittest

from isf_tool.core.complementary import combine_devices, device_closure
from isf_tool.core.component_selection import (
    core_targets, resolve_selection, selection_request, validate_core_targets,
)
from isf_tool.core.errors import ValidationError
from isf_tool.core.spectre_topology import SpectreTopology
from isf_tool.optim.api import gate


NETLIST = """
model nch bsim4 type=n
model pch bsim4 { 1: type=p }
MN (OP ON 0 0) nch w=4u l=180n
MN2 (ON OP 0 0) nch w=4u l=180n
MP (ON OP VDD VDD) pch w=4u l=180n
"""


class ComplementaryCoreTests(unittest.TestCase):
    def test_selection_requires_explicit_distinct_and_opposite_polarity(self):
        tree = SpectreTopology(text=NETLIST)
        spec = {'gm': {'complementary': {'target_instance': None}}}
        request = selection_request(spec, provider=tree)
        result = resolve_selection(request, {'gm.target': 'MP', 'gm.complementary': '/MN'})
        self.assertEqual(result['gm']['polarity'], 'pmos')
        self.assertEqual(result['complementary_gm']['polarity'], 'nmos')
        self.assertEqual(result['complementary_gm']['terminals']['D']['resolved_net'], 'OP')
        self.assertNotIn('gm.complementary', selection_request({}, provider=tree)['roles'])
        with self.assertRaisesRegex(ValidationError, 'one NMOS and one PMOS'):
            resolve_selection(request, {'gm.target': 'MN', 'gm.complementary': 'MN2'})
        with self.assertRaises(ValidationError):
            resolve_selection(request, {'gm.complementary': 'MP'})
        with self.assertRaises(ValidationError):
            core_targets('MN', None, {'target_instance': '/MN'})
        with self.assertRaises(ValidationError):
            validate_core_targets(tree, [('MN', 'MP'), ('MP', None)])

    def test_asymmetric_pair_requires_two_same_type_occurrences_without_peers(self):
        tree = SpectreTopology(text=NETLIST)
        selection = {'mode': 'asymmetric_pair', 'target_instance': 'MN2'}
        request = selection_request({'gm': {'complementary': selection}}, provider=tree)
        binding = resolve_selection(request, {'gm.target': 'MN', 'gm.complementary': 'MN2'})
        self.assertEqual(binding['complementary_gm']['polarity'], 'nmos')
        targets = core_targets('MN', None, selection)
        self.assertEqual(validate_core_targets(tree, targets, mode='asymmetric_pair'), ['nmos', 'nmos'])
        with self.assertRaisesRegex(ValidationError, 'two NMOS or two PMOS'):
            resolve_selection(request, {'gm.target': 'MP', 'gm.complementary': 'MN2'})
        with self.assertRaisesRegex(ValidationError, 'without symmetric peer'):
            core_targets('MN', 'MN2', selection)

    def test_view_04_merges_comparison_with_its_exact_primitive(self):
        from isf_tool.tk.viewmodel import build_result_view, contribution_projection

        primary = self.sample('DUT.MN.m0', 1e-5, 1e-12, 2e-12)
        second = self.sample('DUT.MN2.m0', 3e-5, 4e-12, 4e-12)
        result = combine_devices([
            (dict(instance='DUT.MN', polarity='nmos'), primary),
            (dict(instance='DUT.MN2', polarity='nmos'), second),
        ], mode='asymmetric_pair')
        self.assertEqual(result['complementary_core']['mode'], 'asymmetric_pair')
        view = build_result_view(result, point='DUT.MN | P', noise_kind='both')
        rows = contribution_projection(view)['rows']
        primary_flicker = [row for row in rows if row['mechanism'] == 'fn'
                           and row['device'] in ('DUT.MN', 'DUT.MN.m0')]
        self.assertEqual(len(primary_flicker), 1)
        self.assertAlmostEqual(primary_flicker[0]['psd_V2_Hz'], 1e-10)
        self.assertTrue(any(row['device'] == 'DUT.MN2' and row['mechanism'] == 'fn' for row in rows))

    @staticmethod
    def sample(name, amplitude, predicted_id, reference_id):
        thermal = dict(schema_version=1, status='available', scope='selected_device',
                       pair_model='single_device_only', noise_quantity='PM_SSB', pm_validation='proof',
                       target_instance=name, target_noise_trace=name, f0_Hz=1e9, offset_Hz=1e5,
                       carrier_peak_V=1., predicted_psd_V2_Hz=predicted_id,
                       sources=[dict(device=name, mechanism='id', psd_V2_Hz=predicted_id,
                                     input_band_contributions=[dict(input_band=0, psd_V2_Hz=predicted_id)])])
        row = dict(point='P', carrier_peak_V=1., f0_Hz=1e9, offset_Hz=1e5,
                   dynamic_pm_asd_V_rtHz=amplitude, spectre_total_pm_asd_V_rtHz=1e-3,
                   spectre_noise_reference=dict(schema_version=1, scope='whole_vco'),
                   target_noise_trace=name, thermal_noise=thermal,
                   spectre_noise_contributions=[
                       dict(device=name, mechanism='id', psd_V2_Hz=reference_id),
                       dict(device=name, mechanism='fn', psd_V2_Hz=amplitude**2),
                       dict(device='switch', mechanism='fn', psd_V2_Hz=1e-7),
                       dict(device='switch', mechanism='id', psd_V2_Hz=1e-7)])
        return dict(summary=[row], points={'P': dict(summary=[row], harmonics=[], workpoint=[], phase_candidates=[])})

    def test_power_sum_and_same_source_closure(self):
        nmos = self.sample('MN', 1e-5, 1e-12, 2e-12)
        pmos = self.sample('MP', 3e-5, 4e-12, 4e-12)
        result = combine_devices([
            (dict(instance='MN', polarity='nmos'), nmos),
            (dict(instance='MP', polarity='pmos'), pmos),
        ])
        self.assertEqual(list(result['points']), ['MN | P', 'MP | P'])
        combined = result['complementary_core']['combined'][0]
        self.assertAlmostEqual(combined['closure']['flicker']['isf_dbc_hz'],
                               result['summary'][0]['dynamic_dBc_Hz'] + 10 * math.log10(10))
        self.assertAlmostEqual(result['summary'][0]['device_closure']['channel_thermal']['error_db'],
                               -10 * math.log10(2))
        nmos['summary'][0]['spectre_noise_contributions'].append(dict(device='MN', mechanism='id', psd_V2_Hz=2e-12))
        self.assertIsNone(device_closure(nmos['summary'][0])['channel_thermal']['error_db'])

    def test_optimization_gate_requires_each_mechanism_within_two_db(self):
        metrics = dict(
            source=dict(device_instance='MN', max_harmonic=1),
            quality=dict(numerical_quality_pass=True, pm_magnitude_projection_valid=True,
                         thermal_available=True, flicker_available=True),
            closure=dict(gap_db=1, spectre_total_dbc_hz=-90, modeled_total_dbc_hz=-91,
                         device=dict(flicker=dict(error_db=-2), channel_thermal=dict(error_db=2))),
            flicker=dict(rho_dyn=.2, c0_eff=.3, c_k={'0': .7, '1': .3}),
        )
        self.assertTrue(gate(metrics)['isf_shaping_allowed'])
        metrics['closure']['device']['channel_thermal']['error_db'] = 2.01
        decision = gate(metrics)
        self.assertFalse(decision['isf_shaping_allowed'])
        self.assertIn('MN channel_thermal', decision['reason'])
