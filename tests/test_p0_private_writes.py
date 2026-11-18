"""Second-review counterexamples: file identity, relocation and effective geometry."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from isf_tool.core.component_selection import discover_components, selection_request, resolve_selection
from isf_tool.core.errors import ValidationError
from isf_tool.core.generated_flicker import generated_fixture
from isf_tool.core.private_netlist import materialize_patch, plan_instance_instrumentation, instrument_model
from isf_tool.core.private_validation import write_private_netlist
from isf_tool.core.spectre_topology import SpectreTopology
from scripts import dynamic_isf_closure_engine as engine
import test_p0_review_regressions as review


class PrivateWriteRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.helper = review.ReviewRegressions()
        self.helper.root = self.root

    def source(self, include, *, nested=False):
        (self.root/'inner.scs').write_text('m0 (d g s b) nch w=2*w l=l ad=3p\n')
        if nested:
            (self.root/'middle.scs').write_text('section active\ninclude "inner.scs"\nendsection active\nsection off\ninvalid should not enter replica\nendsection off\n')
            include = '"middle.scs" section=active'
        return self.helper.source('model nch bsim4\nsubckt rf (d g s b)\nparameters w=2u l=1u\ninclude ' + include + '\nends rf\nA (op on 0 0) rf w=2u l=1u\nB (on op 0 0) rf')

    def exercise(self, source):
        before = {p: p.read_bytes() for p in self.root.glob('*.scs')}
        tree, _, cfg = self.helper.setup_for(source, 'A')
        dc = self.root/'dc/input.scs'
        fixture, _, _ = generated_fixture(source, 'A', 10000, topology=tree, destination=dc)
        self.assertEqual(dc.read_text(), fixture)
        self.assertIn('include "' + str(self.root), fixture)
        self.assertEqual(SpectreTopology(dc).effective_mos_parameters(SpectreTopology(dc).resolve_instance('M_ISF_DC'))['ad'], 3e-12)
        cfg.update(flicker_template_netlist=str(dc), _physical_profile=cfg['physical_profile'])
        wave = {'f0':1e9, 'period':1e-9, 'time':[0,.5e-9,1e-9],
                'op':[1,.8,1], 'on':[.3,.5,.3], '0':[0,0,0]}
        dynamic = self.root/'dynamic/input.scs'
        engine.prepare_replica_netlist(cfg, wave, dynamic)
        private = SpectreTopology(dynamic)
        rows = private.occurrences(external=True)
        self.assertEqual([r['name'] for r in rows if r['local_name']=='I_ISF_NOISE'], ['M_ISF_REPLICA.I_ISF_NOISE'])
        self.assertAlmostEqual(private.effective_mos_parameters(private.resolve_instance('M_ISF_REPLICA'))['w'],4e-6)
        self.assertNotIn('invalid should', dynamic.read_text())
        main = self.root/'main/input.scs'
        engine.prepare_main_netlist(cfg, source, main)
        copied = SpectreTopology(main)
        self.assertEqual(copied.resolve_instance('B.m0')['terminals'],tree.resolve_instance('B.m0')['terminals'])
        sample = self.root/'sample/input.scs'
        engine.prepare_dc_noise_netlist(cfg, {'VGS_V':.8,'VDS_V':1,'VBS_V':0,'VS_V':.1},sample)
        self.assertAlmostEqual(SpectreTopology(sample).effective_mos_parameters(SpectreTopology(sample).resolve_instance('M_ISF_DC'))['w'],4e-6)
        for p, raw in before.items():
            self.assertEqual(p.read_bytes(), raw)

    def test_relative_internal_include_survives_dc_dynamic_main_and_dc_sample(self):
        self.exercise(self.source('"inner.scs"'))

    def test_absolute_internal_include_uses_primitive_file_offsets(self):
        self.exercise(self.source('"' + str(self.root/'inner.scs') + '"'))

    def test_transitive_internal_include_preserves_active_section(self):
        self.exercise(self.source('', nested=True))

    def test_selector_defaults_expressions_overrides_and_extra_dimensions(self):
        source = self.source('"inner.scs"')
        source.write_text(source.read_text().replace('A (op on 0 0) rf w=2u l=1u', 'A (op on 0 0) rf'))
        tree = SpectreTopology(source)
        spec = {'source':{'entry':str(source)}}
        request = selection_request(spec,catalog=discover_components(spec,provider=tree))
        gm = resolve_selection(request,{'gm.target':'A'})['gm']
        self.assertEqual(gm['parameters'],{'w':4e-6,'l':1e-6,'ad':3e-12,'m':1,'nf':1})
        self.assertEqual(gm['parameter_sources']['w']['kind'],'parent_expression')
        self.assertEqual(gm['parameter_sources']['l']['kind'],'default')
        self.assertEqual(gm['parameter_sources']['nf']['kind'],'default')
        source.write_text(source.read_text().replace('A (op on 0 0) rf','A (op on 0 0) rf l=3u'))
        tree = SpectreTopology(source)
        gm = resolve_selection(selection_request(spec,catalog=discover_components(spec,provider=tree)),{'gm.target':'A'})['gm']
        self.assertEqual(gm['parameter_sources']['l']['kind'],'instance')
        self.assertEqual(gm['parameters']['l'],3e-6)

    def test_parameter_sources_follow_derived_defaults_and_inherited_m(self):
        source = self.helper.source('model nch bsim4\nsubckt core (d g s b)\n'
            'parameters W=1u WIDTH=2*W\nM (d g s b) nch w=WIDTH l=1u\nends core\n'
            'A (op on 0 0) core W=3u m=2')
        tree = SpectreTopology(source)
        row = tree.resolve_instance('A.M')
        self.assertAlmostEqual(tree.effective_mos_parameters(row)['w'],6e-6)
        sources = tree.mos_parameter_sources(row)
        self.assertEqual(sources['w']['kind'],'parent_expression')
        self.assertEqual(sources['w']['dependencies']['WIDTH']['expression'],'2*W')
        self.assertEqual(sources['w']['dependencies']['WIDTH']['dependencies']['W']['kind'],'instance')
        self.assertEqual(sources['m']['factors'][0]['occurrence'],'A')
        self.assertEqual(sources['m']['kind'],'instance')

    def test_main_analysis_edits_use_the_included_file_identity(self):
        source = self.source('"inner.scs"')
        analyses = self.root/'analyses.scs'
        analyses.write_text('osc hb fundfreqs=[1G]\npn (op on) hbnoise values=[10k]\n')
        netdir = self.root/'source'
        netdir.mkdir()
        entry = netdir/'input.scs'
        entry.write_text(source.read_text().replace('include "inner.scs"', 'include "' + str(self.root/'inner.scs') + '"').replace(
            'osc hb fundfreqs=[1G]\npn (op on) hbnoise values=[10k]', 'include "' + str(analyses) + '"'))
        original = analyses.read_bytes()
        _, _, cfg = self.helper.setup_for(entry,'A')
        destination = self.root/'main/input.scs'
        engine.prepare_main_netlist(cfg,entry,destination)
        tree = SpectreTopology(destination)
        for name in ('hb','hbnoise','hbxf'):
            self.assertEqual(len([i for i in tree.items if i.kind=='analysis' and i.name==name]),1)
        self.assertEqual(analyses.read_bytes(),original)

    def test_materializer_rejects_untargeted_occurrence_mutation(self):
        source = self.source('"inner.scs"')
        tree = SpectreTopology(source)
        from isf_tool.core import private_netlist
        render = private_netlist._render
        with mock.patch.object(private_netlist,'_render',side_effect=lambda t,p:render(t,p).replace('B (on op 0 0) rf','B (on op 0 0) rf w=99u')):
            with self.assertRaisesRegex(ValidationError,'changed occurrence|geometry'):
                materialize_patch(plan_instance_instrumentation(tree,'A'),tree,self.root/'bad/main.scs')

    def test_dynamic_writer_rejects_extra_or_modified_model_occurrence(self):
        source = self.source('"inner.scs"')
        tree, _, cfg = self.helper.setup_for(source,'A')
        dc = self.root/'dc/input.scs'
        generated_fixture(source,'A',10000,topology=tree,destination=dc)
        cfg.update(flicker_template_netlist=str(dc),_physical_profile=cfg['physical_profile'])
        wave={'f0':1e9,'period':1e-9,'time':[0,.5e-9,1e-9],'op':[1,.8,1],'on':[.3,.5,.3],'0':[0,0,0]}
        injected=engine.injected_nmos_subckt
        for replacement in ('extra (d s) resistor r=1\nm0 (d g s b)', 'm0 (g d s b)'):
            with self.subTest(replacement=replacement),mock.patch.object(engine,'injected_nmos_subckt',side_effect=lambda c:injected(c).replace('m0 (d g s b)',replacement)):
                with self.assertRaises(ValidationError):
                    engine.prepare_replica_netlist(cfg,wave,self.root/'bad/input.scs')

    def test_final_writer_reparses_actual_bytes_and_rejects_corruption(self):
        original = Path.write_text
        def corrupt(path, text, **kwargs):
            return original(path,text.replace('ends rf','ends truncated'),**kwargs)
        with mock.patch.object(Path,'write_text',corrupt):
            with self.assertRaisesRegex(ValidationError,'boundary mismatch'):
                write_private_netlist(self.root/'corrupt.scs','subckt rf (d g s b)\nends rf\n')

    def test_missing_dependency_fails_before_fixture_success(self):
        source = self.source('"inner.scs"')
        tree = SpectreTopology(source)
        (self.root/'inner.scs').unlink()
        with self.assertRaises((ValidationError,FileNotFoundError)):
            generated_fixture(source,'A',10000,topology=tree,destination=self.root/'dc/input.scs')

    def test_analysis_only_thermal_copy_rejects_circuit_change(self):
        from isf_tool.core.private_validation import write_analysis_copy
        from isf_tool.core.thermal_execution import _private_main
        source = self.source('"inner.scs"')
        text = _private_main(source.read_text(), {'negative':'op','positive':'on'},1e4,3,[1e4])
        destination = self.root/'thermal/input.scs'
        write_analysis_copy(source,destination,text)
        self.assertEqual(SpectreTopology(destination).resolve_instance('B')['terminals'],['on','op','0','0'])
        with self.assertRaisesRegex(ValidationError,'changed occurrence'):
            write_analysis_copy(source,destination,text.replace('B (on op 0 0) rf','B (on op 0 0) rf w=99u'))

    def test_file_bound_edit_outside_definition_is_rejected(self):
        source = self.source('"inner.scs"')
        tree = SpectreTopology(source)
        with self.assertRaisesRegex(ValidationError,'does not belong'):
            tree.relocated_definition_text(tree.lookup('rf'),[(str(self.root/'unrelated.scs'),0,1,'bad')])

    def test_tied_drain_gate_survives_main_preparation_at_each_hierarchy(self):
        wrapper = ('subckt rf (g d b s)\nparameters w=2u l=1u\n'
            'subckt core (b s g d)\nm0 (d g s b) nch w=w l=l\nends core\n'
            'x (b s g d) core\nends rf\n')
        cases = [
            ('primitive', '', 'nch', 'A'),
            ('wrapper', 'subckt rf (d g s b)\nparameters w=2u l=1u\nm0 (d g s b) nch w=w l=l\nends rf\n', 'rf', 'A'),
            ('permuted', wrapper, 'rf', 'A'),
            ('hierarchical', wrapper, 'rf', 'TOP.A'),
            ('intrinsic', wrapper, 'rf', 'TOP.A.x.m0'),
        ]
        for label, definitions, model, target in cases:
            with self.subTest(case=label):
                devices = ('A (op op 0 0) ' + model + ' w=2u l=1u\n'
                    'B (on op 0 0) ' + model + ' w=2u l=1u\n')
                if target.startswith('TOP.'):
                    devices = 'subckt vco (op on)\n' + devices + 'ends vco\nTOP (op on) vco\n'
                source = self.helper.source('model nch bsim4\n' + definitions + devices)
                before, _, cfg = self.helper.setup_for(source, target)
                destination = self.root/label/'input.scs'
                engine.prepare_main_netlist(cfg, source, destination)
                after = SpectreTopology(destination)
                old, new = before.resolve_instance(target), after.resolve_instance(target)
                order = before.mos_identity(old)['terminal_order']
                self.assertNotEqual(new['terminals'][order[0]], old['terminals'][order[0]])
                for index in order[1:]:
                    self.assertEqual(new['terminals'][index], old['terminals'][index])
                intrinsic = before.mos_identity(old)['intrinsic_path']
                leaf = target + ('.' + intrinsic if intrinsic else '')
                self.assertEqual(after.resolve_instance(leaf)['terminals'][1:], before.resolve_instance(leaf)['terminals'][1:])
                peer = 'TOP.B' if target.startswith('TOP.') else 'B'
                self.assertEqual(after.resolve_instance(peer)['terminals'], before.resolve_instance(peer)['terminals'])

    def test_main_rejects_local_port_swap_hidden_by_tied_source_bulk(self):
        source = self.helper.source('model nch bsim4\nsubckt rf (d g s b)\n'
            'parameters w=2u l=1u\nm0 (d g s b) nch w=w l=l\nends rf\nA (op on 0 0) rf')
        tree = SpectreTopology(source)
        from isf_tool.core import private_netlist
        render = private_netlist._render
        with mock.patch.object(private_netlist, '_render', side_effect=lambda t,p:render(t,p).replace(
                'm0 (d g s b)', 'm0 (d g b s)')):
            with self.assertRaisesRegex(ValidationError, 'changed occurrence|identity'):
                materialize_patch(plan_instance_instrumentation(tree, 'A'), tree, self.root/'bad/main.scs')

    def test_main_rejects_shared_model_declaration_mutation(self):
        from isf_tool.core import private_netlist
        for declaration in ('model nch bsim4 u0=0.05', 'model nch bsim4 {\n1: type=n u0=0.05\n}'):
            with self.subTest(declaration=declaration):
                source = self.helper.source(declaration + '\nA (op on 0 0) nch w=2u l=1u\nB (on op 0 0) nch w=2u l=1u')
                tree = SpectreTopology(source)
                render = private_netlist._render
                with mock.patch.object(private_netlist, '_render', side_effect=lambda t,p:render(t,p).replace('u0=0.05', 'u0=0.99')):
                    with self.assertRaisesRegex(ValidationError, 'changed model declaration'):
                        materialize_patch(plan_instance_instrumentation(tree, 'A'), tree, self.root/'bad/main.scs')

    def test_analysis_copy_rejects_model_mutation_in_each_lexical_scope(self):
        from isf_tool.core.private_validation import write_analysis_copy
        for scope in ('root', 'wrapper'):
            with self.subTest(scope=scope):
                declaration = 'model nch bsim4 u0=0.05\n'
                body = 'A (op on 0 0) nch w=2u l=1u\nB (on op 0 0) nch w=2u l=1u\n'
                if scope == 'wrapper':
                    body = 'subckt rf (op on)\n' + declaration + body + 'ends rf\nX (op on) rf\n'
                else:
                    body = declaration + body
                source = self.helper.source(body)
                destination = self.root/scope/'analysis.scs'
                write_analysis_copy(source, destination, source.read_text().replace('fundfreqs=[1G]', 'fundfreqs=[2G]'))
                with self.assertRaisesRegex(ValidationError, 'changed model declaration'):
                    write_analysis_copy(source, destination, source.read_text().replace('u0=0.05', 'u0=0.99'))

    def test_fixture_rejects_changed_model_declaration(self):
        from isf_tool.core.private_validation import validate_mos_copy
        for wrapper in (False, True):
            with self.subTest(wrapper=wrapper):
                definitions = 'subckt rf (d g s b)\nparameters w=2u l=1u\nm0 (d g s b) nch w=w l=l\nends rf\n' if wrapper else ''
                source = self.helper.source('model nch bsim4 u0=0.05\n' + definitions +
                    'A (op on 0 0) ' + ('rf' if wrapper else 'nch') + ' w=2u l=1u')
                tree = SpectreTopology(source)
                fixture, _, _ = generated_fixture(source, 'A', 10000, topology=tree)
                changed = SpectreTopology(self.root/'bad/fixture.scs', text=fixture.replace('u0=0.05', 'u0=0.99'))
                with self.assertRaisesRegex(ValidationError, 'changed model declaration'):
                    validate_mos_copy(tree, tree.resolve_instance('A'), changed, 'M_ISF_DC')

    def test_dynamic_writer_rejects_changed_scoped_model_declaration(self):
        source = self.helper.source('subckt rf (d g s b)\nparameters w=2u l=1u\n'
            'model nch bsim4 u0=0.05\nm0 (d g s b) nch w=w l=l\nends rf\nA (op on 0 0) rf w=2u l=1u')
        tree, _, cfg = self.helper.setup_for(source, 'A')
        dc = self.root/'dc/input.scs'
        generated_fixture(source, 'A', 10000, topology=tree, destination=dc)
        cfg.update(flicker_template_netlist=str(dc), _physical_profile=cfg['physical_profile'])
        wave = {'f0':1e9, 'period':1e-9, 'time':[0,.5e-9,1e-9], 'op':[1,.8,1], 'on':[.3,.5,.3], '0':[0,0,0]}
        injected = engine.injected_nmos_subckt
        with mock.patch.object(engine, 'injected_nmos_subckt', side_effect=lambda c:injected(c).replace('u0=0.05', 'u0=0.99')):
            with self.assertRaisesRegex(ValidationError, 'changed model declaration'):
                engine.prepare_replica_netlist(cfg, wave, self.root/'bad/input.scs')


if __name__ == '__main__':
    unittest.main()
