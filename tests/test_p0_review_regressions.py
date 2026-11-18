"""End-to-end counterexamples for portable model and device binding."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from isf_tool.core import private_netlist
from isf_tool.core.private_validation import write_analysis_copy, validate_mos_copy

from isf_tool.core.component_selection import discover_components, selection_request, resolve_selection
from isf_tool.core.errors import ValidationError
from isf_tool.core.generated_flicker import generated_fixture
from isf_tool.core.portable_setup import build_portable_setup
from isf_tool.core.private_netlist import plan_instance_instrumentation, materialize_patch
from isf_tool.core.signal_mapping import discover_signal_mapping, pair_symmetry
from isf_tool.core.spectre_topology import SpectreTopology
from scripts import dynamic_isf_closure_engine as engine


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def source(self, body):
        path = self.root/'input.scs'
        path.write_text('simulator lang=spectre\n' + body + '\nosc hb fundfreqs=[1G]\npn (op on) hbnoise values=[10k]\nsimulatorOptions options temp=27\n')
        return path

    def setup_for(self, source, target):
        tree = SpectreTopology(source)
        history = {'source_netlists': [{'path': str(source)}]}
        test = {'name': 'vco', 'analyses': [], 'variables': [], 'outputs': []}
        mapping = discover_signal_mapping(test, 'vco', history, {'target_instance': target, 'output_p': 'op', 'output_n': 'on'}, topology=tree)
        mapping.update(selected_test=test, selected_history=history)
        setup = build_portable_setup({'roles': {'vco': mapping}, 'pair': {'replica_source': 'generated'}}, {}, {'executables': {'spectre': '/bin/true', 'psf': '/bin/true'}}, topology=tree)
        self.assertTrue(setup['ok'], setup['errors'])
        return tree, mapping, setup['engine']

    def test_dynamic_replica_with_two_sections_of_one_include(self):
        (self.root / 'parts.scs').write_text('section mos\nm0 (d g s b) nch w=w l=l\n'
            'endsection mos\nsection passive\nc0 (d g) capacitor c=1p\nendsection passive\n')
        source = self.source('model nch bsim4\nsubckt rf (d g s b)\nparameters w=2u l=1u\ninclude "parts.scs" section=mos\ninclude "parts.scs" section=passive\nends rf\nA (op on 0 0) rf w=2u l=1u\nB (on op 0 0) rf w=2u l=1u')
        self.assert_fixture_and_dynamic(source, 'A', 2e-6)

    def test_model_parameter_dependencies_checked_in_every_private_writer(self):
        source = self.source('parameters mobility=0.05 base=2\n'
            'model nch bsim4 u0=mobility*base\n'
            'A (op on 0 0) nch w=2u l=1u\nB (on op 0 0) nch w=2u l=1u')
        tree = SpectreTopology(source)
        mutate = lambda text: text.replace('mobility=0.05', 'mobility=0.99')
        with self.assertRaisesRegex(ValidationError, 'model parameter dependencies'):
            write_analysis_copy(source, self.root/'analysis.scs', mutate(source.read_text()))
        render = private_netlist._render
        with mock.patch.object(private_netlist, '_render', side_effect=lambda t, p: mutate(render(t, p))):
            with self.assertRaisesRegex(ValidationError, 'model parameter dependencies'):
                materialize_patch(plan_instance_instrumentation(tree, 'A'), tree, self.root/'main.scs')
        fixture, _, _ = generated_fixture(source, 'A', 10000, topology=tree)
        copied = self.root/'fixture.scs'
        copied.write_text(mutate(fixture))
        with self.assertRaisesRegex(ValidationError, 'model parameter dependencies'):
            validate_mos_copy(tree, tree.resolve_instance('A'), SpectreTopology(copied), 'M_ISF_DC')

    def test_model_dependencies_use_lexical_scope_and_derived_values(self):
        source = self.source('parameters base=0.05 mobility=2*base unused=7\n'
            'model nch bsim4 u0=mobility\n'
            'subckt core (d g s b)\nparameters mobility=99\n'
            'M (d g s b) nch w=2u l=1u\nends core\n'
            'A (op on 0 0) core\nB (on op 0 0) core')
        with self.assertRaisesRegex(ValidationError, 'model parameter dependencies'):
            write_analysis_copy(source, self.root/'bad.scs', source.read_text().replace('base=0.05', 'base=0.1'))
        write_analysis_copy(source, self.root/'ok.scs', source.read_text().replace('unused=7', 'unused=8'))

    def test_unresolved_model_and_instance_parameter_dependencies_are_transitive(self):
        for model, tail in (('model nch bsim4 u0=mobility', 'w=2u l=1u'),
                            ('model nch bsim4', 'w=mobility l=1u')):
            source = self.source('parameters base=0.05 mobility=base+missing\n' + model +
                '\nA (op on 0 0) nch ' + tail)
            with self.subTest(model=model), self.assertRaisesRegex(ValidationError, 'parameter dependencies'):
                write_analysis_copy(source, self.root/'bad.scs', source.read_text().replace('base=0.05', 'base=0.99'))

    def test_overrides_recompute_derived_defaults_and_pair_receipt(self):
        source = self.source('''model nch bsim4
subckt core (d g s b)
parameters W=1u WIDTH=2*W
M (d g s b) nch w=WIDTH l=1u
ends core
A (op on 0 0) core W=3u
B (on op 0 0) core W=4u''')
        tree, mapping, cfg = self.setup_for(source, 'A.M')
        rows = {r['name']: r for r in tree.catalog()['devices']}
        for name, width in [('A.M', 6e-6), ('B.M', 8e-6)]:
            self.assertAlmostEqual(tree.resolve_instance(name)['effective_parameters']['w'], width)
            self.assertAlmostEqual(rows[name]['effective_mos_parameters']['w'], width)
            fixture, evidence, _ = generated_fixture(source, name, 10000, topology=tree)
            parsed = SpectreTopology(self.root/'fixture.scs', text=fixture)
            self.assertAlmostEqual(parsed.effective_mos_parameters(parsed.resolve_instance('M_ISF_DC'))['w'], width)
            self.assertAlmostEqual(evidence['effective_parameters']['w'], width)
        self.assertFalse(mapping['mapping']['pair_policy']['proven'])
        receipt = engine.prepare_main_netlist(cfg, source, self.root/'private/input.scs')
        self.assertFalse(receipt['pair_policy']['proven'])

    def test_shadowed_forward_defaults_and_caller_overrides(self):
        source = self.source('''parameters W=99u WIDTH=90u
model nch bsim4
subckt core (d g s b)
parameters WIDTH=2*W W=1u
M (d g s b) nch w=WIDTH l=1u
ends core
A (op on 0 0) core W=3u
B (on op 0 0) core W=W''')
        tree = SpectreTopology(source)
        self.assertAlmostEqual(tree.resolve_instance('A.M')['effective_parameters']['w'], 6e-6)
        self.assertAlmostEqual(tree.resolve_instance('B.M')['effective_parameters']['w'], 198e-6)

    def test_unresolved_override_never_uses_default_or_parent(self):
        source = self.source('''parameters W=99u
model nch bsim4
subckt core (d g s b)
parameters W=1u WIDTH=2*W
M (d g s b) nch w=WIDTH l=1u
ends core
A (op on 0 0) core W=missing''')
        with self.assertRaisesRegex(ValidationError, 'W=missing'):
            generated_fixture(source, 'A.M', 10000)

    def assert_fixture_and_dynamic(self, source, target, expected_width):
        tree, _, cfg = self.setup_for(source, target)
        fixture, evidence, _ = generated_fixture(source, target, 10000, topology=tree)
        dc = self.root/'dc/input.scs'
        dc.parent.mkdir(parents=True, exist_ok=True)
        dc.write_text(fixture)
        dc_tree = SpectreTopology(dc)
        row = dc_tree.resolve_instance('M_ISF_DC')
        self.assertEqual(dc_tree.mos_identity(row)['primitive_type'], 'bsim4')
        self.assertAlmostEqual(dc_tree.effective_mos_parameters(row)['w'], expected_width)
        cfg['flicker_template_netlist'] = str(dc)
        cfg['_physical_profile'] = cfg['physical_profile']
        wave = {'f0': 1e9, 'period': 1e-9, 'time': [0, 0.5e-9, 1e-9],
                'op': [1, .8, 1], 'on': [.3, .5, .3], '0': [0, 0, 0]}
        dynamic = self.root/'replica/input.scs'
        engine.prepare_replica_netlist(cfg, wave, dynamic)
        dynamic_tree = SpectreTopology(dynamic)
        copied = dynamic_tree.resolve_instance('M_ISF_REPLICA')
        self.assertEqual(dynamic_tree.mos_identity(copied)['primitive_type'], 'bsim4')
        self.assertAlmostEqual(dynamic_tree.effective_mos_parameters(copied)['w'], expected_width)
        self.assertTrue(any(r['local_name'] == 'I_ISF_NOISE' for r in dynamic_tree.occurrences(external=True)))
        return evidence

    def test_wrapper_multiplicity_is_not_applied_twice_in_fixture(self):
        source = self.source('''model nch bsim4
subckt rf (d g s b)
parameters w=2u l=1u
m0 (d g s b) nch w=w l=l
ends rf
subckt core (d g s b)
X (d g s b) rf w=2u l=1u
ends core
A (op on 0 0) core m=2''')
        tree = SpectreTopology(source)
        self.assertEqual(tree.effective_mos_parameters(tree.resolve_instance('A.X'))['m'], 2)
        fixture, _, _ = generated_fixture(source, 'A.X', 10000, topology=tree)
        private = SpectreTopology(self.root/'dc.scs', text=fixture)
        self.assertEqual(private.effective_mos_parameters(private.resolve_instance('M_ISF_DC'))['m'], 2)

    def test_lexical_wrapper_full_fixture_and_replica(self):
        source = self.source('''model nch bsim4
subckt outer (d g s b)
subckt rf (d g s b)
parameters w=2u l=1u
m0 (d g s b) nch w=w l=l
ends rf
M (d g s b) rf w=2u l=1u
ends outer
A (op on 0 0) outer''')
        self.assert_fixture_and_dynamic(source, 'A.M', 2e-6)

    def test_lexical_wrapper_captures_sibling_models_and_overridden_parameters(self):
        source = self.source('''subckt outer (d g s b)
parameters W=1u WIDTH=2*W
model local bsim4
subckt sibling (d g s b)
parameters w=1u l=1u
m0 (d g s b) local w=w l=l
ends sibling
subckt rf (d g s b)
parameters l=1u
inner (d g s b) sibling w=WIDTH l=l
ends rf
M (d g s b) rf l=1u
ends outer
A (op on 0 0) outer W=3u''')
        self.assert_fixture_and_dynamic(source, 'A.M', 6e-6)

    def test_root_wrapper_keeps_reachable_sibling_model_only(self):
        source = self.source('''model nch bsim4
subckt sibling (d g s b)
parameters w=1u l=1u
m0 (d g s b) nch w=w l=l
ends sibling
subckt rf (d g s b)
parameters w=2u l=1u
inner (d g s b) sibling w=w l=l
ends rf
subckt unused (d g s b)
parameters HIDDEN=99
ends unused
A (op on 0 0) rf w=2u l=1u''')
        self.assert_fixture_and_dynamic(source, 'A', 2e-6)
        text = (self.root/'replica/input.scs').read_text()
        self.assertNotIn('HIDDEN=99', text)

    def test_lexical_wrapper_in_local_include(self):
        body = self.root/'core.scs'
        body.write_text('''model nch bsim4
subckt outer (d g s b)
subckt rf (d g s b)
parameters w=2u l=1u
m0 (d g s b) nch w=w l=l
ends rf
M (d g s b) rf w=2u l=1u
ends outer
''')
        source = self.source('include "core.scs"\nA (op on 0 0) outer')
        self.assert_fixture_and_dynamic(source, 'A.M', 2e-6)

    def test_lexical_root_shadowing_is_explicitly_rejected(self):
        source = self.source('''model nch bsim4
subckt rf (d g s b)
m0 (d g s b) nch w=9u l=1u
ends rf
subckt outer (d g s b)
subckt rf (d g s b)
m0 (d g s b) nch w=2u l=1u
ends rf
M (d g s b) rf
ends outer
A (op on 0 0) outer''')
        with self.assertRaisesRegex(ValidationError, 'shadows a root definition'):
            generated_fixture(source, 'A.M', 10000)

    def test_inline_alias_survives_selected_and_untouched_private_write(self):
        source = self.source('''model nch bsim4
inline subckt rf(d g s b)
parameters w=2u l=1u
rf (d g s b) nch w=w l=l
ends rf
subckt core(d g s b)
X (d g s b) rf w=2u l=1u
ends core
A (op on 0 0) core
B (on op 0 0) rf w=2u l=1u''')
        for selected in ('A.X', 'B'):
            with self.subTest(selected=selected):
                tree, _, cfg = self.setup_for(source, selected)
                self.assertEqual(cfg['physical_profile']['main']['traces']['target_noise'], selected)
                output = self.root/(selected+'/input.scs')
                engine.prepare_main_netlist(cfg, source, output)
                private = SpectreTopology(output)
                for path in ('A.X', 'B'):
                    self.assertTrue(tree.mos_identity(tree.resolve_instance(path))['inline_alias'])
                    self.assertTrue(private.mos_identity(private.resolve_instance(path))['inline_alias'])
                self.assertEqual(private.lookup('rf').header.text, tree.lookup('rf').header.text)
                # Dynamic replicas deliberately use an ordinary wrapper. Their
                # contributor path remains explicit, distinct from main B.
                dynamic = engine.injected_nmos_subckt(cfg)
                self.assertTrue(dynamic.lstrip().startswith('subckt rf_isf_dynamic'))
                self.assertEqual(cfg['physical_profile']['replica']['noise_trace'], 'M_ISF_REPLICA.rf')

    def test_multi_mos_core_with_geometry_parameters_remains_browsable(self):
        for included in (False, True):
            body = '''model nch bsim4
subckt core (d g s b)
parameters w=2u l=1u
Mleft (d g s b) nch w=w l=l
Mright (g d s b) nch w=w l=l
ends core
'''
            if included:
                model = self.root/'core.scs'
                model.write_text(body)
                body = 'include "core.scs"\n'
            source = self.source(body + 'DUT (op on 0 0) core w=3u l=1u')
            tree = SpectreTopology(source)
            spec = {'source': {'kind': 'netlist', 'cwd': str(self.root), 'files': [{'path': str(source)}]}}
            catalog = discover_components(spec, provider=tree)
            self.assertEqual({r['name'] for r in catalog['devices']}, {'DUT.Mleft', 'DUT.Mright'})
            request = selection_request(spec, catalog=catalog)
            selected = resolve_selection(request, {'gm.target': '/DUT/Mleft', 'output.p': 'op', 'output.n': 'on'})
            self.assertEqual(selected['gm']['target_instance'], 'DUT.Mleft')
            self.assertEqual(tree.resolve_instance('DUT.Mleft')['effective_parameters']['w'], 3e-6)
            self.assert_fixture_and_dynamic(source, 'DUT.Mleft', 3e-6)
            tree, _, cfg = self.setup_for(source, 'DUT.Mleft')
            out = self.root/'multi-private/input.scs'
            engine.prepare_main_netlist(cfg, source, out)
            self.assertEqual(SpectreTopology(out).resolve_instance('DUT.Mright')['local_terminals'], ['g', 'd', 's', 'b'])

    def test_same_spelled_models_in_distinct_scopes_are_not_a_pair(self):
        source = self.source('''subckt left (d g s b)
model local bsim4
M (d g s b) local w=2u l=1u
ends left
subckt right (d g s b)
model local psp
M (d g s b) local w=2u l=1u
ends right
A (op on 0 0) left
B (on op 0 0) right''')
        rows = {r['name']: r for r in SpectreTopology(source).catalog()['devices']}
        self.assertFalse(pair_symmetry(rows['A.M'], rows['B.M'])['proven'])


if __name__ == '__main__':
    unittest.main()
