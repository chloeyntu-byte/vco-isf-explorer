"""P0 occurrence identity, MOS evidence, parameter and private-edit regressions."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from isf_tool.core.errors import ValidationError
from isf_tool.core.spectre_topology import SpectreTopology, canonical_name
from isf_tool.core.generated_flicker import generated_fixture
from isf_tool.core.private_netlist import plan_instance_instrumentation, materialize_patch, instrument_model
from isf_tool.core.component_selection import discover_components, selection_request, resolve_selection, apply_selection
from isf_tool.core.signal_mapping import discover_signal_mapping
from isf_tool.core.portable_setup import build_portable_setup
from scripts import dynamic_isf_closure_engine as engine


class TopologyP0Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = self.write('model.scs', 'model nch bsim4 type=n\nmodel pch psp type=p\nsubckt rf (a z c q)\nparameters l=1u w=2u\nXinner (a z c q) inner l=l w=w\nends rf\nsubckt inner (d g s b)\nparameters l=1u w=2u\nmain (di g si b) nch l=l w=w\nRD (d di) resistor r=2\nRS (s si) resistor r=2\nends inner\n')

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def source(self, body):
        return self.write('input.scs', 'simulator lang=spectre\ninclude "model.scs"\n' + body + '\nosc hb fundfreqs=[1G]\npn (op on) hbnoise values=[10k]\nsimulatorOptions options temp=27\n')

    def test_model_types_not_names_and_recursive_identity(self):
        source = self.source('Xgm (op on 0 0) nch l=1u w=2u\nI3 (on op vdd vdd) pch l=1u w=2u\nQ (op on 0 0) rf l=1u w=2u\nMfake (op on 0 0) unknown')
        tree = SpectreTopology(source)
        mos = {r['name']:r for r in tree.catalog()['devices'] if r.get('mos')}
        self.assertEqual(set(mos), {'Xgm','I3','Q'})
        self.assertEqual('Xinner.main', mos['Q']['mos']['intrinsic_path'])
        self.assertEqual('nch', mos['Q']['mos']['primitive'])
        self.assertEqual('psp', mos['I3']['mos']['primitive_type'])
        self.assertEqual(tree.resolve_instance('/Q/Xinner/main')['name'], 'Q.Xinner.main')
        with self.assertRaisesRegex(ValidationError, 'case sensitive'): tree.resolve_instance('xgm')

    def test_depths_parent_parameters_m_and_continuations(self):
        for depth in range(1,6):
            defs = []
            for level in range(depth):
                child = 'leaf (d g \\\n s b) rf w=(W*2) l=1u nf=4' if level == depth-1 else 'X (d g s b) level{} W=W'.format(level+1)
                defs.append('subckt level{} (d g s b)\nparameters W=1u\n{}\nends level{}\n'.format(level,child,level))
            source = self.source(''.join(defs) + 'TOP (op on 0 0) level0 W=3u m=2')
            name = 'TOP.'+'X.'*(depth-1)+'leaf'
            tree = SpectreTopology(source)
            row = tree.resolve_instance('/'+name.replace('.','/'))
            self.assertEqual(row['terminals'], ['op','on','0','0'])
            fixture, evidence, _ = generated_fixture(source,name,10000)
            self.assertAlmostEqual(evidence['effective_parameters']['w'],6e-6)
            self.assertEqual(evidence['effective_parameters']['m'],2)
            self.assertIn('nf=4',fixture)

    def test_unknown_parent_parameter_is_specific(self):
        source=self.source('subckt core (d g s b)\nX (d g s b) rf w=(missing*2) l=1u\nends core\nTOP (op on 0 0) core')
        with self.assertRaisesRegex(ValidationError, 'missing'):
            generated_fixture(source,'TOP.X',10000)

    def test_sections_lexical_scope_duplicates(self):
        self.write('corners.scs','section TT\nsubckt core (d g s b)\nX (d g s b) nch w=2u\nends core\nendsection TT\nsection FF\nsubckt core (d g s b)\nX (g d s b) nch w=8u\nends core\nendsection FF\n')
        source=self.source('include "corners.scs" section=TT\nTOP (op on 0 0) core')
        self.assertEqual(SpectreTopology(source).resolve_instance('TOP.X')['parameters']['w'],'2u')
        source=self.source('subckt core (d g s b)\nends core\nsubckt core (d g s b)\nends core')
        with self.assertRaisesRegex(ValidationError,'duplicate active definition'): SpectreTopology(source)

    def test_permuted_formals_are_proven_by_connectivity(self):
        self.model.write_text('model nch bsim4\nsubckt rf (g d s b)\nparameters w=2u l=1u\nmain (d g s b) nch w=w l=l\nends rf\n')
        source=self.source('Xgm (on op 0 0) rf w=2u l=1u')
        row=next(r for r in SpectreTopology(source).catalog()['devices'] if r['name']=='Xgm')
        self.assertEqual(row['terminals'],['op','on','0','0'])
        fixture, evidence, _=generated_fixture(source,'Xgm',10000)
        self.assertIn('M_ISF_DC (G D S B) rf',fixture)
        self.assertEqual(evidence['mos']['terminal_order'],[1,0,2,3])

    def test_formal_names_never_override_connectivity(self):
        self.model.write_text('model nch bsim4\nsubckt rf (d g s b)\nmain (g d s b) nch w=2u l=1u\nends rf\n')
        source=self.source('Xgm (on op 0 0) rf w=2u l=1u')
        _, evidence, _=generated_fixture(source,'Xgm',10000)
        self.assertEqual(evidence['mos']['terminal_order'],[1,0,2,3])

    def test_macros_unsupported_and_capacitor_is_not_wire_proof(self):
        for pins, body in [('d g s','main (d g s s) nch'),('d g s b x','main (d g s b) nch'),('d g s b','main (di g s b) nch\nC (d di) capacitor c=1p'),('d g s b','main (d g s b) nch\nother (d g s b) nch')]:
            self.model.write_text('model nch bsim4\nsubckt rf ('+pins+')\n'+body+'\nends rf\n')
            source=self.source('Xgm ('+' '.join(pins.split())+') rf')
            with self.assertRaisesRegex(ValidationError,'unsupported|unproven'): generated_fixture(source,'Xgm',10000)

    def test_private_text_geometry_is_effective(self):
        source=self.source('Xgm (op on 0 0) nch w=2u l=1u')
        _, evidence, _=generated_fixture(source,'Xgm',10000,text=source.read_text().replace('w=2u','w=55u'))
        self.assertAlmostEqual(evidence['effective_parameters']['w'],55e-6)

    def test_same_master_only_selected_occurrence_instrumented(self):
        self.write('core.scs','subckt core (d g s b)\nXgm (d g s b) rf w=2u l=1u\nends core\n')
        source=self.source('include "core.scs"\nA (op on 0 0) core\nB (on op 0 0) core')
        tree=SpectreTopology(source)
        before={p:Path(p).read_bytes() for p in tree.files}
        plan=plan_instance_instrumentation(tree,'A.Xgm',{'p':'op','n':'on'})
        output=self.root/'private/input.scs'
        manifest=materialize_patch(plan,tree,output)
        self.assertEqual(manifest['injection'],'A.I_ISF_INJECT')
        private=SpectreTopology(output).catalog()['devices']
        self.assertEqual([r['name'] for r in private if r['model']=='isource'],['A.I_ISF_INJECT'])
        self.assertEqual(next(r for r in private if r['name']=='B.Xgm')['local_terminals'],['d','g','s','b'])
        self.assertEqual(before,{p:Path(p).read_bytes() for p in tree.files})
        source.write_text(source.read_text()+'// changed\n')
        with self.assertRaisesRegex(ValidationError,'stale'): materialize_patch(plan,tree,output)
        with self.assertRaisesRegex(ValidationError,'stale'): materialize_patch(plan,SpectreTopology(source),output)

    def test_same_leaf_different_master_and_iterated(self):
        source=self.source('subckt ca (d g s b)\nXgm (d g s b) rf w=2u l=1u\nends ca\nsubckt cb (d g s b)\nXgm (d g s b) rf w=2u l=1u\nends cb\nA<0:2> (op on 0 0) ca\nB (on op 0 0) cb')
        tree=SpectreTopology(source)
        row=tree.resolve_instance('/A\\<1\\>/Xgm')
        self.assertEqual(row['name'],'A<1>.Xgm')
        plan=plan_instance_instrumentation(tree,row)
        materialize_patch(plan,tree,self.root/'private/input.scs')
        devices=SpectreTopology(self.root/'private/input.scs').catalog()['devices']
        self.assertEqual([r['name'] for r in devices if r['model']=='isource'],['A<1>.I_ISF_INJECT'])

    def test_native_primitive_and_nested_model_injection(self):
        source=self.source('Xgm (op on 0 0) nch w=2u l=1u\nQ (op on 0 0) rf w=2u l=1u')
        tree=SpectreTopology(source)
        for name in ('Xgm','Q'):
            text=instrument_model(tree,tree.resolve_instance(name),'ISF_DYNAMIC','ISF_NOISE')
            private=SpectreTopology(text='include "'+str(self.model)+'"\n'+text+'\nTEST (d g s b) ISF_DYNAMIC w=2u l=1u')
            self.assertTrue(any(r['model']=='isource' for r in private.occurrences(external=True)))
            self.assertIn('pacmag=1',text)

    def test_hierarchical_buffer_output_and_unparenthesized_analysis(self):
        source=self.source('Xgm (op on 0 0) rf w=2u l=1u\nB (buf 0 op 0) vcvs gain=1')
        test={'name':'vco','analyses':[],'variables':[],'outputs':[]}
        history={'source_netlists':[{'path':str(source)}]}
        r=discover_signal_mapping(test,'vco',history,{'target_instance':'Xgm','output_p':'buf','output_n':'0'})
        r.update(selected_history=history,selected_test=test)
        setup=build_portable_setup({'roles':{'vco':r},'pair':{'replica_source':'generated'}},{},{'executables':{'spectre':'/bin/true','psf':'/bin/true'}})
        self.assertTrue(setup['ok'],setup['errors'])
        output=self.root/'private/input.scs'
        manifest=engine.prepare_main_netlist(setup['engine'],source,output)
        import hashlib
        self.assertEqual(manifest['sha256'],hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(json.loads(output.with_suffix('.isf-patch.json').read_text())['sha256'],manifest['sha256'])
        self.assertIn('hbnoise (buf 0)',output.read_text())
        self.assertIn('hb hb fundfreqs=',output.read_text())

    def test_selector_schema_hash_staleness_and_forgery(self):
        source=self.source('Xgm (op on 0 0) rf w=2u l=1u')
        spec={'source':{'kind':'netlist','cwd':str(self.root),'files':[{'path':str(source)}]}}
        catalog=discover_components(spec)
        request=selection_request(spec,catalog=catalog)
        result=resolve_selection(request,{'gm.target':'/Xgm','output.p':'op','output.n':'0'})
        schema=json.loads((Path(__file__).resolve().parents[1]/'formats/isf-selection.schema.json').read_text())
        for name, value in [('ComponentCatalog',catalog),('SelectionRequest',request),('SelectionResult',result)]:
            check_schema(self, value, schema['definitions'][name], schema)
        applied=apply_selection(spec,result)
        self.assertEqual(applied['gm']['terminals']['D']['resolved_net'],'op')
        bad=copy.deepcopy(result);bad['gm']['terminals']['D']['resolved_net']='fake'
        with self.assertRaisesRegex(ValidationError,'modified'):apply_selection(spec,bad)
        source.write_text(source.read_text()+'// changed\n')
        with self.assertRaisesRegex(ValidationError,'stale'):apply_selection(spec,result)

    def test_nested_lexical_definitions_and_instance_clone(self):
        source=self.source('subckt outer (d g s b)\nsubckt core (d g s b)\nXgm (d g s b) rf w=2u l=1u\nends core\nX (d g s b) core\nends outer\nA (op on 0 0) outer\nB (on op 0 0) outer')
        tree=SpectreTopology(source)
        plan=plan_instance_instrumentation(tree,'A.X.Xgm')
        output=self.root/'private/input.scs'
        materialize_patch(plan,tree,output)
        sources=[r['name'] for r in SpectreTopology(output).catalog()['devices'] if r['model']=='isource']
        self.assertEqual(sources,['A.X.I_ISF_INJECT'])

    def test_same_local_definition_name_in_different_scopes(self):
        source=self.source('subckt a (d g s b)\nsubckt core (d g s b)\nM (d g s b) nch w=2u l=1u\nends core\nX (d g s b) core\nends a\nsubckt b (d g s b)\nsubckt core (d g s b)\nM (d g s b) nch w=8u l=1u\nends core\nX (d g s b) core\nends b\nA (op on 0 0) a\nB (on op 0 0) b')
        tree=SpectreTopology(source)
        self.assertEqual(tree.resolve_instance('A.X.M')['parameters']['w'],'2u')
        self.assertEqual(tree.resolve_instance('B.X.M')['parameters']['w'],'8u')

    def test_binned_direct_model_and_non_utf8_hash(self):
        import hashlib
        raw=b'model nch bsim4{\n1: type=n lmin=1n lmax=9u\n+ wmin=1n wmax=99u\n}\n// legacy comment \xff\n'
        self.model.write_bytes(raw)
        source=self.source('Xgm (op on 0 0) nch w=2u l=1u')
        tree=SpectreTopology(source)
        self.assertEqual(tree.file_hashes[str(self.model)],hashlib.sha256(raw).hexdigest())
        self.assertEqual(tree.mos_identity(tree.resolve_instance('Xgm'))['primitive_type'],'bsim4')
        fixture,evidence,_=generated_fixture(source,'Xgm',10000)
        self.assertIn('nch w=2u l=1u',fixture)
        self.assertEqual(evidence['dependency_hashes'][str(self.model)],hashlib.sha256(raw).hexdigest())

    def test_read_cache_does_not_hide_changed_model(self):
        source=self.source('Xgm (op on 0 0) rf w=2u l=1u')
        cache={}
        first=SpectreTopology(source,read_cache=cache)
        self.assertEqual(first.mos_identity(first.resolve_instance('Xgm'))['primitive'],'nch')
        self.model.write_text(self.model.read_text().replace('main (di g si b) nch','main (di g si b) pch'))
        second=SpectreTopology(source,read_cache=cache)
        self.assertEqual(second.mos_identity(second.resolve_instance('Xgm'))['primitive'],'pch')
        self.assertNotEqual(first.content_hash(),second.content_hash())

    def test_direct_declared_and_builtin_model_full_preparation(self):
        from isf_tool.core.physical_profile import inspect_engine_contract
        for model in ('nch','bsim4'):
            source=self.source('Xgm (op on 0 0) '+model+' w=2u l=1u')
            tree=SpectreTopology(source)
            h={'source_netlists':[{'path':str(source)}]}
            test={'name':'vco','analyses':[],'variables':[],'outputs':[]}
            mapped=discover_signal_mapping(test,'vco',h,{'target_instance':'Xgm'},topology=tree)
            mapped.update(selected_test=test,selected_history=h)
            setup=build_portable_setup({'roles':{'vco':mapped},'pair':{'replica_source':'generated'}},{},
                {'executables':{'spectre':'/bin/true','psf':'/bin/true'}},topology=tree)
            self.assertTrue(setup['ok'],setup['errors'])
            cfg=setup['engine']
            fixture,_,_=generated_fixture(source,'Xgm',10000,topology=tree)
            dc=self.root/(model+'/dc/input.scs');dc.parent.mkdir(parents=True,exist_ok=True);dc.write_text(fixture)
            cfg['flicker_template_netlist']=str(dc)
            cfg['_physical_profile']=cfg['physical_profile']
            wave={'f0':1e9,'period':1e-9,'time':[0,0.5e-9,1e-9],'op':[1,.8,1],'on':[.3,.5,.3],'0':[0,0,0]}
            dynamic=self.root/(model+'/replica/input.scs')
            engine.prepare_replica_netlist(cfg,wave,dynamic)
            private=SpectreTopology(dynamic)
            leaf=private.resolve_instance('M_ISF_REPLICA.m0')
            self.assertEqual(leaf['model'],model)
            self.assertEqual(cfg['physical_profile']['main']['traces']['target_noise'],'Xgm')
            checked=inspect_engine_contract(cfg,require_explicit=True,check_paths=True,
                source_from_maestro=True,maestro_source_netlists=[{'path':str(source)}],maestro_vco_mapping=cfg['vco_mapping'])
            self.assertTrue(checked['ok'],checked['errors'])

    def test_root_local_model_wrapper_retains_sibling_model_declarations(self):
        source=self.write('input.scs','model nch bsim4\nsubckt rf (d g s b)\nparameters w=2u l=1u\nm0 (d g s b) nch w=w l=l\nends rf\nXgm (op on 0 0) rf w=2u l=1u\nsimulatorOptions options temp=27\n')
        fixture,_,_=generated_fixture(source,'Xgm',10000)
        private=SpectreTopology(self.root/'dc/input.scs',text=fixture)
        row=private.resolve_instance('M_ISF_DC')
        self.assertEqual(private.mos_identity(row)['primitive'],'nch')

    def test_unsupported_conditionals_and_missing_include_fail(self):
        with self.assertRaisesRegex(ValidationError,'unsupported conditional'):
            SpectreTopology(text='if (x) {\nM (d g s b) bsim4\n}\n')
        source=self.source('include "missing.scs"')
        with self.assertRaises((OSError,ValidationError)):SpectreTopology(source)

    def test_effective_geometry_and_bias_pair_policy(self):
        from isf_tool.core.signal_mapping import pair_symmetry
        self.model.write_text('model nch bsim4\nsubckt rf (d g s b)\nparameters wr=2u lr=1u fingers=4\nmain (d g s b) nch w=wr l=lr m=fingers\nends rf\n')
        source=self.source('A (op on 0 0) rf wr=2u lr=1u fingers=4\nB (on op 0 0) rf wr=2u lr=1u fingers=4')
        rows={r['name']:r for r in SpectreTopology(source).catalog()['devices']}
        self.assertTrue(pair_symmetry(rows['A'],rows['B'])['proven'])
        self.assertEqual(rows['A']['effective_mos_parameters']['m'],4)
        rows['B']['effective_mos_parameters']['m']=5
        self.assertFalse(pair_symmetry(rows['A'],rows['B'])['proven'])

    def test_iterated_bus_actuals_and_scalar_private_rewrite(self):
        source=self.source('Mpair<0:3> (out<0:3> gate<3:0> 0 0) nch w=2u l=1u')
        tree=SpectreTopology(source)
        row=tree.resolve_instance('/Mpair\\<2\\>')
        self.assertEqual(row['terminals'],['out<2>','gate<1>','0','0'])
        plan=plan_instance_instrumentation(tree,row,{'p':'out<2>','n':'0'})
        output=self.root/'private/input.scs'
        materialize_patch(plan,tree,output)
        private=SpectreTopology(output)
        self.assertEqual(private.resolve_instance('Mpair<1>')['terminals'],['out<1>','gate<2>','0','0'])
        self.assertEqual(private.resolve_instance('Mpair<2>')['terminals'][0],'ISF_DRAIN')

    def test_thermal_intrinsic_missing_requires_zero_evidence(self):
        from isf_tool.core.thermal_ports import physical_noise_port
        terminal={'M':{'master':'bsim4','terminals':{'d':'d','g':'g','s':'s','b':'b'}}}
        for evidence in (None,{'M':{'rd':1,'rs':0}}):
            with self.assertRaisesRegex(ValueError,'intrinsic'):
                physical_noise_port('M','id',terminal,['d','s'],model_evidence=evidence)
        port=physical_noise_port('M','id',terminal,['d','s'],model_evidence={'M':{'rd':0,'rs':0}})
        self.assertEqual((port['positive'],port['negative']),('d','s'))


class TimebaseP0Tests(unittest.TestCase):
    def test_partial_misaligned_nonfinite_duplicate_nonclosed_fail(self):
        for times,values in [([.2,.4,.6],[1,2,3]),([0,0,1],[1,2,1]),([0,.5,1],[1,float('nan'),1]),([0,.5,1],[1,2,3])]:
            with patch.object(engine,'exact_hb_fundamental',return_value=1),patch.object(engine,'parse_real_wave',return_value=(np.array(times),np.array(values))):
                with self.assertRaises(ValueError):engine.sample_periodic_wave({},'/unused',['d'])
        with patch.object(engine,'exact_hb_fundamental',return_value=1),patch.object(engine,'parse_real_wave',side_effect=[(np.array([0,.5,1]),np.array([1,2,1])),(np.array([.25,.75,1.25]),np.array([1,2,1]))]):
            with self.assertRaisesRegex(ValueError,'origin'):engine.sample_periodic_wave({},'/unused',['d','s'])

    def test_common_shift_and_unequal_sampling_axes_are_preserved(self):
        with patch.object(engine,'exact_hb_fundamental',return_value=1),patch.object(engine,'parse_real_wave',side_effect=[(np.array([2,2.5,3]),np.array([1,2,1])),(np.array([2,2.25,2.75,3]),np.array([0,1,1,0]))]):
            wave=engine.sample_periodic_wave({},'/unused',['d','s','0'],samples=5)
            self.assertEqual(wave['timebase']['t_origin_s'],2)
            self.assertEqual(wave['d'].tolist(),[1,1.5,2,1.5,1])


def check_schema(test, value, rule, root):
    """Validate the subset used by the published selector schemas, offline."""
    if '$ref' in rule:
        return check_schema(test, value, root['definitions'][rule['$ref'].rsplit('/',1)[1]], root)
    types = rule.get('type', [])
    types = [types] if isinstance(types, str) else types
    if types:
        actual = ('null' if value is None else 'boolean' if isinstance(value,bool) else
                  'object' if isinstance(value,dict) else 'array' if isinstance(value,list) else
                  'string' if isinstance(value,str) else 'number')
        test.assertIn(actual, types)
    if 'const' in rule: test.assertEqual(value,rule['const'])
    if 'enum' in rule: test.assertIn(value,rule['enum'])
    if 'pattern' in rule: test.assertRegex(value,rule['pattern'])
    if isinstance(value,dict):
        test.assertTrue(set(rule.get('required',[])) <= set(value))
        test.assertGreaterEqual(len(value),rule.get('minProperties',0))
        props=rule.get('properties',{})
        for k,v in value.items():
            if rule.get('additionalProperties') is False: test.assertIn(k,props)
            check_schema(test,v,props.get(k,rule.get('additionalProperties',{})) or {},root)
    if isinstance(value,list):
        test.assertGreaterEqual(len(value),rule.get('minItems',0))
        if rule.get('uniqueItems'):test.assertEqual(len(value),len({json.dumps(v,sort_keys=True) for v in value}))
        for v in value:check_schema(test,v,rule.get('items',{}),root)
