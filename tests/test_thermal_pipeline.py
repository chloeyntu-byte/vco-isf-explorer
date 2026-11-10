import copy
import csv
import json
import math
from pathlib import Path
import tempfile
import unittest

from isf_tool.core.thermal_budget import thermal_evidence, calculated_noise_dbc, load_thermal_evidence
from isf_tool.core.thermal_ports import parse_terminal_map, physical_noise_port, port_transfer
from isf_tool.core.thermal_execution import (_private_main, _private_dc, run_optional_thermal,
    _instance_member, _symmetric_peer_instance)
from isf_tool.core.result_presentation import enrich_summary, harmonic_noise_series, phase_noise_series
from isf_tool.core.dual_tb import recompute_dual_tb, _read_csv
from isf_tool.core.limits import MAX_CANONICAL_CSV_FIELD_BYTES
from test_scalar_manual_reference import canonical_fixture
from test_total_noise_extraction import EXPORT


def thermal_fixture(carrier=1.0, offset=10000):
    return dict(schema_version=1, status="available", scope="gm_pair_and_lc_network",
                passive_scope="passive_only_lc_roots_v1",
                noise_quantity="PM_SSB", pm_validation="independent_analytic_fixture",
                pair_model="symmetric_independent", carrier_peak_V=carrier, offset_Hz=offset, f0_Hz=5e9,
                predicted_psd_V2_Hz=5e-12, sources=[dict(device="X1.R", mechanism="rn", psd_V2_Hz=5e-12,
                input_band_contributions=[dict(input_band=-1, psd_V2_Hz=2e-12), dict(input_band=1, psd_V2_Hz=3e-12)])])


class ThermalBudgetTests(unittest.TestCase):
    def test_old_missing_unavailable_and_json_null_are_not_zero(self):
        for row in ({}, {"thermal_noise_json": "null"}, {"thermal_noise": dict(schema_version=1, status="unavailable", reason="missing") } ):
            self.assertIsNone(calculated_noise_dbc(row))

    def test_power_total_uses_old_ssb_normalization_exactly_once(self):
        row = dict(carrier_peak_V=1, predicted_pair_pm_asd_V_rtHz=2e-6, thermal_noise=thermal_fixture())
        before = copy.deepcopy(row)
        self.assertAlmostEqual(calculated_noise_dbc(row), 10 * math.log10(2 * 9e-12))
        self.assertAlmostEqual(calculated_noise_dbc(row, noise_kind="thermal"), -110)
        self.assertEqual(row, before)

    def test_reject_tampered_sum_duplicate_band_offset_and_carrier(self):
        mutations = [lambda b: b.update(predicted_psd_V2_Hz=9e-12),
                     lambda b: b['sources'][0]['input_band_contributions'][0].update(psd_V2_Hz=1e-12),
                     lambda b: b['sources'][0]['input_band_contributions'][0].update(input_band=1),
                     lambda b: b.update(offset_Hz=1000), lambda b: b.update(carrier_peak_V=2)]
        for mutate in mutations:
            budget = thermal_fixture()
            mutate(budget)
            with self.assertRaises(ValueError):
                thermal_evidence(dict(thermal_noise=budget, carrier_peak_V=1, offset_Hz=10000))

    def test_malformed_source_shapes_fail_and_numeric_psd_strings_normalize(self):
        for bad in (None, 1, 'not a source', []):
            budget = thermal_fixture()
            budget['sources'] = [bad]
            with self.assertRaises(ValueError):
                thermal_evidence(dict(thermal_noise=budget))
            budget = thermal_fixture()
            budget['sources'][0]['input_band_contributions'] = [bad]
            with self.assertRaises(ValueError):
                thermal_evidence(dict(thermal_noise=budget))
        budget = thermal_fixture()
        budget['sources'][0]['psd_V2_Hz'] = '5e-12'
        budget['sources'][0]['input_band_contributions'][0]['psd_V2_Hz'] = '2e-12'
        parsed = thermal_evidence(dict(thermal_noise=budget))
        self.assertEqual(parsed['sources'][0]['psd_V2_Hz'], 5e-12)
        self.assertEqual(parsed['sources'][0]['input_band_contributions'][0]['psd_V2_Hz'], 2e-12)

    def test_thermal_frequency_identity_is_required_and_checked(self):
        for f0 in (None, 0, -1, float('nan'), float('inf'), True, 1e4):
            budget = thermal_fixture()
            budget['f0_Hz'] = f0
            with self.assertRaises(ValueError):
                thermal_evidence(dict(thermal_noise=budget))
        with self.assertRaisesRegex(ValueError, 'frequency mismatch'):
            thermal_evidence(dict(thermal_noise=thermal_fixture(), f0_Hz=6e9))
        self.assertEqual(5e9, thermal_evidence(dict(thermal_noise=thermal_fixture(), f0_Hz=5e9))['f0_Hz'])
        budget = thermal_fixture()
        budget['f0_Hz'] = 7180319021.050036
        self.assertEqual(budget['f0_Hz'], thermal_evidence(dict(thermal_noise=budget, f0_Hz=7180320000))['f0_Hz'])
        with self.assertRaisesRegex(ValueError, 'frequency mismatch'):
            thermal_evidence(dict(thermal_noise=budget, f0_Hz=7180330000))

    def test_disabled_thermal_cannot_read_stale_budget_even_in_analyze_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'thermal' / 'budget.json'
            path.parent.mkdir()
            path.write_text(json.dumps(thermal_fixture()))
            cfg = dict(thermal_enabled=False)
            self.assertEqual('unavailable', load_thermal_evidence(cfg, directory, {})['status'])
            self.assertEqual('available', json.loads(path.read_text())['status'])
            run_optional_thermal(None, cfg, None, directory)
            self.assertEqual('unavailable', json.loads(path.read_text())['status'])

    def test_csv_evidence_over_legacy_limit_roundtrips_with_bounded_limit(self):
        # Compact real budgets still commonly exceed csv's legacy 128KiB default.
        budget = thermal_fixture()
        budget['audit_note'] = 'x' * 140000
        payload = json.dumps(budget)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'evidence.csv'
            with path.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=['thermal_noise_json'])
                writer.writeheader()
                writer.writerow({'thermal_noise_json': payload})
            for reader in (EXPORT.rows, lambda p: _read_csv(str(p), {'thermal_noise_json'}, 'utf-8')):
                csv.field_size_limit(131072)
                rows = reader(path)
                self.assertEqual(thermal_evidence(rows[0])['predicted_psd_V2_Hz'], 5e-12)
                self.assertEqual(csv.field_size_limit(), MAX_CANONICAL_CSV_FIELD_BYTES)

    def test_analyze_only_does_not_relabel_old_budget_with_new_harmonic_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "thermal" / "budget.json"
            path.parent.mkdir()
            budget = thermal_fixture()
            budget.update(input_max_harmonic=3, requested_max_harmonic=3, transfer_max_harmonic=3)
            path.write_text(json.dumps(budget))
            self.assertEqual("available", load_thermal_evidence(dict(max_harmonic=3), directory, {})["status"])
            rejected = load_thermal_evidence(dict(max_harmonic=1), directory, {})
            self.assertEqual("unavailable", rejected["status"])
            self.assertIn("rerun", rejected["reason"])
            self.assertEqual(json.loads(path.read_text()), budget)

    def test_canonical_roundtrip_keeps_flicker_unchanged(self):
        canonical = canonical_fixture()
        baseline = recompute_dual_tb(canonical)
        row = baseline['summary'][0]
        budget = thermal_fixture(row['carrier_peak_V'], row['offset_Hz'])
        fields = EXPORT.noise_evidence_csv_fields(dict(thermal_noise=budget))
        for vrow in canonical['vco_results']:
            vrow.update(fields)
        result = recompute_dual_tb(canonical)
        for key in ('dynamic_pm_asd_V_rtHz', 'spectre_fn_pm_asd_V_rtHz', 'predicted_pair_pm_asd_V_rtHz'):
            self.assertEqual(result['summary'][0][key], baseline['summary'][0][key])
        actual = result['summary'][0]['thermal_noise']
        self.assertEqual(actual['predicted_psd_V2_Hz'], 5e-12)
        self.assertEqual(thermal_evidence(dict(thermal_noise_json=json.dumps(actual))), actual)

    def test_bands_sum_in_power_and_missing_offset_is_gap(self):
        rows = [dict(point='a', operating_point_id='corner', offset_Hz=1e4, carrier_peak_V=1,
                     predicted_pair_pm_asd_V_rtHz=2e-6, thermal_noise=thermal_fixture()),
                dict(point='b', operating_point_id='corner', offset_Hz=1e5, carrier_peak_V=1,
                     predicted_pair_pm_asd_V_rtHz=2e-6)]
        data = dict(summary=rows, points={})
        curves = harmonic_noise_series(data, scope='pair', noise_kind='thermal')
        self.assertAlmostEqual(curves[0]['points'][0][1], -110)
        self.assertIsNone(curves[0]['points'][1][1])
        self.assertEqual(curves[0]['k'], 1)
        self.assertTrue(all(c['points'][1][1] is None for c in phase_noise_series(data, scope='pair', noise_kind='both') if c['id']!='flicker'))


class ThermalPortTests(unittest.TestCase):
    MAP = '''resistor: X7.R17 of model
          1 : gate
          2 : X7.gi
inductor: tank
          1 : vdd
          2 : output
bsim3v3: X7.core of model
          d : drain
          g : gate
          s : source
          b : bulk
'''

    def test_named_ports_not_one_drain_kernel_for_every_source(self):
        nodes = parse_terminal_map(self.MAP)
        values = {'gate': 8j, 'X7.gi': 3j, 'tank:1': 2j, 'drain': 5j,
                  'source': 1j, 'X7.core:int_d': 4j, 'X7.core:int_s': 2j}
        resistor = physical_noise_port('X7.R17', 'rn', nodes, values)
        self.assertEqual(port_transfer(values, resistor), 5j)
        channel = physical_noise_port('X7.core', 'id', nodes, values)
        self.assertEqual(port_transfer(values, channel), 2j)
        access = physical_noise_port('X7.core', 'rd', nodes, values)
        self.assertEqual(port_transfer(values, access), 1j)
        series = physical_noise_port('tank', 'rn', nodes, values)
        self.assertEqual(series['source_units'], 'V^2/Hz')
        self.assertEqual(port_transfer(values, series), 2j)

    def test_unknown_sources_are_not_silently_classified_thermal(self):
        with self.assertRaises(ValueError):
            physical_noise_port('X7.core', 'ig', parse_terminal_map(self.MAP), [])

    def test_bsim4_intrinsic_and_access_ports_keep_distinct_transfer_functions(self):
        nodes = parse_terminal_map(self.MAP.replace('bsim3v3', 'bsim4'))
        values = {'drain': 8+3j, 'source': 2-1j,
                  'X7.core:int_d': 5+2j, 'X7.core:int_s': 3-2j}
        expected = {'id': 2+4j, 'rd': 3+1j, 'rs': -1+1j}
        for mechanism, transfer in expected.items():
            with self.subTest(mechanism=mechanism):
                port = physical_noise_port('X7.core', mechanism, nodes, values)
                self.assertEqual('A^2/Hz', port['source_units'])
                self.assertEqual(transfer, port_transfer(values, port))
        for mechanism in ('rgbi', 'rbpd', 'igs', 'igd', 'igb', 'corl'):
            with self.subTest(mechanism=mechanism), self.assertRaises(ValueError):
                physical_noise_port('X7.core', mechanism, nodes, values)

    def test_bsim4_eliminated_access_nodes_do_not_invent_a_nonzero_branch(self):
        nodes = parse_terminal_map(self.MAP.replace('bsim3v3', 'bsim4'))
        values = {'drain': 5j, 'source': 2j}
        self.assertEqual(3j, port_transfer(values, physical_noise_port('X7.core', 'id', nodes, values)))
        for mechanism in ('rd', 'rs'):
            self.assertEqual(0j, port_transfer(values, physical_noise_port('X7.core', mechanism, nodes, values)))

    def test_inline_and_nested_gm_sources_preserve_instance_boundaries(self):
        self.assertEqual('MP', _symmetric_peer_instance('MT', 'MT', 'MP'))
        self.assertEqual('Xpair.MP', _symmetric_peer_instance('Xpair.MT', 'Xpair.MT.core', 'Xpair.MP.core'))
        selected = [name for name in ('MR', 'MR.core', 'MR.rgate', 'MR2', 'MR2.core', 'array.MR')
                    if _instance_member(name, 'MR')]
        self.assertEqual(['MR', 'MR.core', 'MR.rgate'], selected)
        for target, trace, peer in (('MT', 'MT2.core', 'MP.core'), ('MT', 'MT.core', 'MP.other'),
                                    ('MT', 'MT', 'MT'), ('MT', 'MT', 'MT.child'),
                                    ('X.MT', 'X.MT', 'X')):
            with self.subTest(target=target, trace=trace, peer=peer), self.assertRaises(ValueError):
                _symmetric_peer_instance(target, trace, peer)

    def test_private_extension_preserves_all_original_hb_noise_and_device_statements(self):
        text = 'R1 (a b) resistor r=2\nnoise (a b) noise values=[1e4]\nhb (a b) hb maxharms=[10]\nhbnoise (a b) hbnoise values=[1e4]\n'
        result = _private_main(text, dict(negative='a', positive='b'), 1e4, 9, [1e4, 1e9])
        for line in text.splitlines():
            self.assertIn(line, result)
        self.assertIn('values=[-1.0000000000000000e+04]', result)
        self.assertIn('stimuli=nodes_and_terminals', result)
        dc = _private_dc('noise (n 0) noise start=1k stop=100M\n', [1e4, 1e9])
        self.assertIn('separatenoise=yes', dc)

    def test_thermal_all_nodes_save_is_private_root_scoped_and_preserves_other_options(self):
        text = ('subckt child (a b)\nlocal options save=selected pwr=none\nends child\n'
                'saveOptions options save=selected pwr=all\n'
                'hb (a b) hb maxharms=[10]\n')
        output = _private_main(text, dict(negative='a', positive='b'), 1e4, 9, [1e4, 1e9])
        self.assertIn('saveOptions options save=all pwr=all', output)
        self.assertIn('local options save=selected pwr=none', output)
        self.assertIn('saveOptions options save=selected pwr=all', text)
        without = _private_main('hb (a b) hb maxharms=[10]\n', dict(negative='a', positive='b'), 1e4, 9, [1e4])
        self.assertIn('thermal_save options save=all', without)


if __name__ == '__main__':
    unittest.main()
