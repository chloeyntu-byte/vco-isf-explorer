"""Three source adapters and one frozen TB Generator completion contract.

This module never edits OA or Maestro. All such writes belong to vco_tbgen.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import sys
from .component_selection import (COMPLEMENTARY_ROLE, core_targets, discover_components,
                                  selection_request, resolve_selection, validate_core_targets, core_mode)
from .spectre_topology import SpectreTopology
from .errors import ValidationError

CONTRACT_VERSION = '1.0.0'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def read(value):
    return copy.deepcopy(value) if isinstance(value, dict) else json.loads(Path(value).read_text(encoding='utf-8'))


def write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def tbgen(root=None):
    root = root or os.environ.get('VCO_TBGEN_ROOT')
    if root:
        root = Path(root).resolve()
        sys.path.insert(0, str(root))
    prior = sys.dont_write_bytecode; sys.dont_write_bytecode = True
    try:
        module = importlib.import_module('vco_tbgen')
        if root and not Path(module.__file__).resolve().is_relative_to(root):
            raise ValidationError('TB Generator module identity collision')
        return module
    finally:
        sys.dont_write_bytecode = prior
        if root: sys.path.remove(str(root))


def issue(field, message, code='INPUT_REQUIRED'):
    return dict(code=code, severity='error', field=field, message=message)


def operation(op, field, reason, **values):
    return dict(op=op, field=field, reason=reason, **values)


def _lcv(value):
    return dict(library=value.get('lib', value.get('library')), cell=value['cell'], view=value.get('view', 'schematic'))


def _file(path, role='other'):
    path = Path(path).resolve()
    return dict(path=str(path), role=role, sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def verify_source(spec):
    for row in spec['source']['files'] + spec['pdk']['includes']:
        if row.get('sha256') and _file(row['path'])['sha256'] != row['sha256']:
            raise ValidationError('SOURCE_CHANGED: ' + row['path'], code='SOURCE_CHANGED')


def _merge(base, overlay):
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict): _merge(base[key], value)
        else: base[key] = copy.deepcopy(value)
    return base


def _pointer_set(spec, pointer, value):
    keys = pointer.strip('/').split('/'); parent = spec
    for key in keys[:-1]: parent = parent.setdefault(key.replace('~1', '/').replace('~0', '~'), {})
    parent[keys[-1].replace('~1', '/').replace('~0', '~')] = copy.deepcopy(value)


def _override(spec, values):
    for key, value in (values or {}).items():
        if key.startswith('/'): _pointer_set(spec, key, value)
        elif isinstance(value, dict) and isinstance(spec.get(key), dict): _merge(spec[key], value)
        else: spec[key] = copy.deepcopy(value)


def draft(kind, tb, *, source=None, workspace=None, overrides=None, tbgen_root=None, topology_file=None):
    tb = copy.deepcopy(tb); work = str(Path(workspace or tb['workspace']).resolve())
    tb['workspace']=work
    src = dict(kind=kind, cwd=work, files=[], cds_lib=tb['cds_lib'], dirty_state='saved')
    src.update(source or {})
    if Path(tb['cds_lib']).is_file(): src['files'].append(_file(tb['cds_lib'], 'config'))
    for name, sha in tb.get('source', {}).get('hashes', {}).items():
        path = Path(tb['source']['generated_dir']) / name
        row = _file(path, 'other')
        if row['sha256'] != sha: raise ValidationError('SOURCE_CHANGED: ' + str(path))
        src['files'].append(row)
    if topology_file:
        src['files'].insert(0, _file(topology_file, 'entry'))
    if kind == 'maestro':
        setup = Path(tb['library_path']) / tb['maestro']['cell'] / tb['maestro']['view']
        src.update(maestro_dir=str(setup), test=tb['maestro']['source_test'], basis='saved_setup')
        src['files'] += [_file(setup / name, 'setup') for name in ('maestro.sdb', 'active.state')]
    elif kind == 'design_lab':
        generated = tb.get('source', {}).get('generated_dir', src.get('generated_dir', ''))
        src.update(project=src.get('project', str(Path(generated) / 'project.json')), generated_dir=generated,
                   graph_hash=tb.get('source', {}).get('manifest', {}).get('graph_hash') or digest(tb.get('source', {})), basis='generated')
    else: src.update(lcv=_lcv(tb['dut']), basis='cell')
    ports = [dict(name=p['name'], direction=p.get('direction', 'inputOutput'), members=[p['name']],
                  role='differential' if p['role'].startswith('output_') else p['role'] if p['role'] not in ('unused', 'external') else 'other') for p in tb['ports']]
    output = {r: next((p['net'] for p in tb['ports'] if p['role'] == 'output_' + r[0]), None) for r in ('positive', 'negative')}
    output.update(expression='V(positive)-V(negative)', top_level=True)
    spec = dict(schema='isf.testbench.v1', phase='draft', spec_id=digest(dict(source=src, tb=tb))[:24], source=src,
        dut=dict(instance_path='DUT', lcv=_lcv(tb['dut']), ports=ports, bindings={p['name']:p['net'] for p in tb['ports']}),
        output=output, gm=dict(target_instance=None, peer_instances=[], terminals={}, noise_scope='selected_device'),
        supplies=[dict(id=s['name'], positive=s['positive'], negative=s['negative'], voltage=s['value'], variable=str(s['value'])) for s in tb.get('supplies', [])],
        controls=[dict(port=c['positive'], kind='bias_current' if c.get('kind')=='current' else 'bias_voltage', value=c['value'], reference=c['negative']) for c in tb.get('controls', [])],
        registers=[dict(id=r['name'], width=r['width'], members_lsb_first=r['members_lsb_first'], code_variable=r['code_variable'], code=tb.get('variables',{}).get(r['code_variable']), supply_id=r['vdd'], implementation='decimal_to_binary_va', rise_s=1e-11, fall_s=1e-11, polarity='active_low' if r.get('invert') else 'active_high') for r in tb.get('registers', [])],
        conditions=dict(temperature_c=tb.get('simulator_options',{}).get('temp'), variables=[dict(name=k, scope='test', value=v, enabled=True, omitted=False) for k,v in tb.get('variables',{}).items()], sweeps=[], corners=[], points=[], selection=dict(mode='all_enabled', point_ids=[])),
        analysis=dict(existing=[], frequency_guess_hz=None, hb_harmonics=15, tstab_s=None, offsets_hz=[10000.], isf_max_harmonic=3, noise_max_sideband=10, phase_samples=512, dc_noise_samples=24, initial_conditions=tb.get('initial_conditions',{}), effective=[]),
        measurements=dict(probe=dict(mode='auto_zero_volt', direction='drain_to_source'), transfer=dict(mode='private_injection', direction_factor=1), outputs=[], save=[]),
        pdk=dict(binding=None, includes=[dict(_file(m['path'], 'model'), section=m.get('section')) for m in tb.get('models',[])], section_by_corner={}),
        replica=dict(source='generated', mechanisms=['flicker', 'thermal']), execution=dict(backend='maestro', materialization='source_new_test' if kind=='maestro' else 'private_maestro_test', workspace=work, tools_profile=None),
        field_evidence={}, extensions=dict(contract_version=CONTRACT_VERSION, tbgen_spec=tb, tbgen_root=tbgen_root, topology_file=topology_file), completion_plan=[])
    if kind != 'maestro':
        spec['conditions']['sweeps'] = [
            dict(id=row['variable'], semantics='cartesian', axes=[row['variable']],
                 native_record=copy.deepcopy(row)) for row in tb.get('sweeps', [])]
        spec['conditions']['corners'] = [
            dict(id=row['name'], enabled=row.get('enabled', True),
                 native_record=copy.deepcopy(row)) for row in tb.get('corners', [])]
    from .spectre_topology import evaluate
    parse_spectre_number = lambda value: evaluate(value, {})
    for a in tb['analyses']:
        spec['analysis']['existing'].append(dict(name=a['kind'], type=a['kind'], enabled=a.get('enabled', True), options=a['parameters']))
        if a['kind']=='hb' and a.get('enabled',True):
            options=a['parameters']; f=options.get('fundfreqs', [options.get('HBFreq1')])
            if f and f[0] is not None:
                try:spec['analysis']['frequency_guess_hz']=parse_spectre_number(str(f[0]))
                except ValueError:pass
            h=options.get('maxharms', [options.get('HBHarm1', 12)])
            try:spec['analysis']['hb_harmonics']=int(float(h[0]))
            except (ValueError,TypeError,IndexError):pass
    if spec['analysis']['frequency_guess_hz'] is None:
        spec['analysis']['frequency_guess_hz'] = tb.get('source', {}).get('frequency_guess_hz')
    _override(spec, overrides)
    return complete_spec(spec)


def complete_spec(value, *, overrides=None, confirmations=()):
    spec = read(value)
    if spec.get('schema') != 'isf.testbench.v1': raise ValidationError('unsupported ISF spec version')
    verify_source(spec)
    _override(spec, overrides)
    spec['phase']='draft'; issues=[]; plan=[]
    a=spec['analysis']; ext=spec['extensions']; tb=copy.deepcopy(ext['tbgen_spec'])
    def ask(field, reason):
        issues.append(issue(field, reason)); plan.append(operation('ask', field, reason))
    p,n=spec['output']['positive'],spec['output']['negative']
    if not p or not n or p==n: ask('/output', 'Select two distinct differential output nets')
    if not spec['gm'].get('target_instance'): ask('/gm/target_instance', 'Select one gm occurrence from the shared hierarchy selector')
    topology=ext.get('topology_file')
    complementary = spec['gm'].get('complementary')
    if complementary is not None:
        if not isinstance(complementary, dict):
            raise ValidationError('Complementary core requires a second device object')
        if not complementary.get('target_instance'):
            ask('/gm/complementary/target_instance', 'Select the second complementary device')
        if not topology:
            ask('/gm/complementary', 'A source netlist is required to prove both device bindings')
    if topology and spec['gm'].get('target_instance') and p and n and (
            complementary is None or complementary.get('target_instance')):
        tree = SpectreTopology(topology)
        request=selection_request(spec, provider=tree)
        choices = {'gm.target':spec['gm']['target_instance'], 'output.p':p, 'output.n':n}
        if complementary is not None:
            peers = spec['gm'].get('peer_instances', [])
            if len(peers) > 1:
                raise ValidationError('Select at most one symmetric peer per complementary device')
            targets = core_targets(choices['gm.target'], peers[0] if peers else None, complementary)
            validate_core_targets(tree, targets, mode=core_mode(complementary))
            choices[COMPLEMENTARY_ROLE] = complementary['target_instance']
        selected=resolve_selection(request, choices)
        if complementary is not None:
            binding = selected['complementary_gm']
            binding.pop('parameter_sources', None)
            binding.pop('peer_instances')
            if 'mode' in complementary:
                binding['mode'] = core_mode(complementary)
            binding['peer_instance'] = targets[1][1]
            spec['gm']['complementary'] = binding
        # The selector owns D/G/S/B proof. Its extra provenance stays in extensions.
        gm=selected['gm']; ext['gm_parameter_sources']=gm.pop('parameter_sources', {})
        peers=spec['gm'].get('peer_instances',[])
        spec['gm'].update(gm)
        spec['gm']['peer_instances']=peers
    if spec['replica']['source']!='generated': ask('/replica/source', 'This native integration requires the existing generated replica path')
    if type(a['dc_noise_samples']) is not int or a['dc_noise_samples']<8 or a['dc_noise_samples']<=2*a['isf_max_harmonic']: ask('/analysis/dc_noise_samples', 'DC noise sampling requires at least 8 samples and more than twice N')
    if a['frequency_guess_hz'] is None or not math.isfinite(float(a['frequency_guess_hz'])) or a['frequency_guess_hz']<=0:
        ask('/analysis/frequency_guess_hz', 'fguess requires a known oscillation result or explicit user input')
    if not a['offsets_hz'] or any(isinstance(f,bool) or not isinstance(f,(float,int)) or not math.isfinite(f) or f<=0 for f in a['offsets_hz']): ask('/analysis/offsets_hz', 'Offsets must be finite positive Hz')
    if not isinstance(a['isf_max_harmonic'],int) or not 1<=a['isf_max_harmonic']<=12: ask('/analysis/isf_max_harmonic', 'N must be an integer from 1 to 12 (current ISF core limit)')
    if spec['source']['kind']=='maestro':
        inherited=ext.get('inherited_conditions',{})
        if 'temperature_c' in inherited and spec['conditions']['temperature_c']!=inherited['temperature_c']:
            ask('/conditions/temperature_c','B inherits source temperature; changing it requires an explicit native condition override')
        for field in ('variables','sweeps','corners'):
            if spec['conditions'].get(field):ask('/conditions/'+field,'B inherits native source conditions; condition overrides are unsupported')
        if a['initial_conditions']:
            ask('/analysis/initial_conditions','B inherits source IC; an IC override cannot be represented by the current copy transaction')
    else:
        # Register code is a shared test variable, not a second independent value.
        for register in spec['registers']:
            for variable in spec['conditions']['variables']:
                if variable['name']==register['code_variable']:variable['value']=register['code']
    if issues: return publish(spec, plan, issues)
    module=tbgen(ext.get('tbgen_root'))
    try:
        isf = module.isf
    except AttributeError as error:
        raise ValidationError('This ISF Explorer requires the modular VCO TB Generator', code='UNSUPPORTED_CAPABILITY') from error
    occupied={r['name'] for category in ('supplies','controls','probes','registers') for r in tb.get(category,[])}
    occupied.update(row['net'] for row in tb['ports'])
    if topology:
        occurrences = SpectreTopology(topology).occurrences()
        occupied.update(row['name'] for row in occurrences)
        occupied.update(net for row in occurrences for net in row.get('nodes', []))
    try:
        injection = isf.injection_for_device(spec['gm'], occupied=occupied)
    except ValueError as error:
        field = '/gm/terminals' if 'proven' in str(error) else '/measurements/transfer'
        ask(field, str(error))
        return publish(spec, plan, issues)
    ext['hbxf_transfer_evidence']=dict(probe=copy.deepcopy(injection),
        equivalence='Zero-DC D-to-S injection with an inline iprobe, plus the selected device drain current')
    plan.append(operation('add','/measurements/transfer','HBXF current injection across selected gm D/S; raw complex V/A family',value=ext['hbxf_transfer_evidence']))
    if spec['source']['kind']=='maestro' and not spec['supplies'] and topology:
        from .bundle_sources import measurement_supplies
        spec['supplies']=measurement_supplies(topology)
        ext['supply_measurement_evidence']=dict(provider='source topology DC voltage sources',path=topology,supplies=copy.deepcopy(spec['supplies']))
    bundle=isf.analysis_bundle(
        [dict(kind=r['type'],enabled=r['enabled'],parameters=r['options']) for r in a['existing']],
        frequency=a['frequency_guess_hz'],positive=p,negative=n,offsets_hz=a['offsets_hz'],
        isf_harmonic=a['isf_max_harmonic'],hb_harmonics=a['hb_harmonics'],sidebands=a['noise_max_sideband'],
        conditions=dict(spec['conditions'],**({'inherited':ext['inherited_conditions']} if ext.get('inherited_conditions') else {})),initial_conditions=a['initial_conditions'],solver_options=a.get('solver_options'))
    ext['analysis_bundle']=bundle
    for row in bundle['pending']:ask('/analysis/'+row['field'],row['reason'])
    if issues:return publish(spec,plan,issues)
    for row in bundle['changes']:
        plan.append(operation('add','/analysis_bundle/'+row['field'],row['reason'],before=row['before'],value=row['after']))
    analyses=bundle['analyses']
    a['noise_max_sideband']=bundle['shared']['sidebands']
    solver=bundle['shared']['solver']
    if solver.get('tstab') and solver.get('oscmethod')!='twotier':
        from .spectre_topology import evaluate
        try:a['tstab_s']=evaluate(str(solver['tstab']),{})
        except ValueError:a['tstab_s']=None
    else:a['tstab_s']=None
    # Keep requested UI values stable; derived minima are visible in the bundle.
    ext['native_offsets_hz']=bundle['shared']['native_offsets_hz']
    if tb['mode']=='create_new':
        tb['sweeps'] = [copy.deepcopy(row['native_record']) for row in spec['conditions']['sweeps']]
        tb['corners'] = [dict(copy.deepcopy(row['native_record']), enabled=row['enabled'])
                         for row in spec['conditions']['corners']]
        # Canonical ISF decisions are authoritative; TB Generator only receives
        # its own fields. ISF gm/replica/numerical settings never leak into it.
        tb['ports']=[dict(name=row['name'],net=spec['dut']['bindings'][row['name']],direction=row.get('direction','inputOutput'),
                         role=('output_p' if spec['dut']['bindings'][row['name']]==p else 'output_n' if spec['dut']['bindings'][row['name']]==n else 'unknown') if row['role']=='differential' else row['role'] if row['role']!='other' else 'unused') for row in spec['dut']['ports']]
        tb['supplies']=[dict(name=row['id'],positive=row['positive'],negative=row['negative'],value=row['voltage']) for row in spec['supplies']]
        old_controls={row['positive']:row['name'] for row in tb.get('controls',[])}
        tb['controls']=[dict(name=old_controls.get(row['port'],'ISF_CTRL_'+str(index)),positive=row['port'],negative=row['reference'],value=row['value'],kind='current' if row['kind']=='bias_current' else 'voltage') for index,row in enumerate(spec['controls']) if row['kind']!='external']
        tb['models']=[dict(path=row['path'],**({'section':row['section']} if row.get('section') else {})) for row in spec['pdk']['includes']]
        tb['variables']={row['name']:row['value'] for row in spec['conditions']['variables'] if row['enabled'] and not row['omitted'] and row['scope']=='test'}
        supplies={row['id']:row['positive'] for row in spec['supplies']}
        original_registers={row['name']:row for row in tb.get('registers',[])}
        tb['registers']=[]
        for row in spec['registers']:
            prior=original_registers.get(row['id'],{})
            tb['registers'].append(dict(name=row['id'],width=row['width'],members_lsb_first=row['members_lsb_first'],code_variable=row['code_variable'],vdd=supplies.get(row['supply_id'],row['supply_id']),vss=prior.get('vss','0'),implementation='veriloga',invert=int(row.get('polarity')=='active_low')))
            tb['variables'][row['code_variable']]=row['code']
        tb['initial_conditions']=copy.deepcopy(a['initial_conditions'])
        temperature=spec['conditions']['temperature_c']
        if temperature is not None:
            tb.setdefault('simulator_options',{})['temp']=str(temperature)
            plan.append(operation('add','/conditions/temperature_c','Apply shared temperature to the generated test',value=temperature))
    sources = [dict(name=row['id'],positive=row['positive'],negative=row['negative']) for row in spec['supplies']]
    tb = isf.install_isf(tb, bundle, injection=injection, device=spec['gm'], supplies=sources)
    if complementary is not None:
        second = spec['gm']['complementary']
        saves_by_name = {row['name']: row for row in tb['saves']}
        required = tb['required']['saves']
        extra = [(term['resolved_net'], 'net') for term in second['terminals'].values()]
        extra.append((second['target_instance'] + '/D', 'terminal'))
        for node, kind in extra:
            name = '/' + node.replace('.', '/').lstrip('/')
            if name in ('/0', '/gnd!'):
                continue
            saves_by_name.setdefault(name, dict(name=name, kind=kind))
            if name not in required:
                required.append(name)
        tb['saves'] = list(saves_by_name.values())
    saves = tb['saves']
    spec['measurements']['outputs']=[dict(name=o['name'],kind='raw' if '_RAW_' in o['name'] else 'scalar',analysis='hbxf' if 'HBXF' in o['name'] else 'hbnoise' if 'HBNOISE' in o['name'] else 'hb',source=o['expression'],units=o['unit']) for o in tb['outputs'] if o['name'].startswith('ISF_')]
    plan.append(operation('add','/analysis_bundle','HB / HBNOISE / HBXF shared settings and generated outputs',value=dict(shared=bundle['shared'],analyses=analyses,outputs=spec['measurements']['outputs'])))
    spec['measurements']['save']=[r['name'] for r in saves]
    a['effective']=[dict(name=r['kind'],type=r['kind'],enabled=True,options=r['parameters']) for r in analyses]
    if spec['source']['kind']=='maestro':
        summary=dict(source_test=tb['maestro']['source_test'],new_test=tb['maestro']['test'],
            inherited=copy.deepcopy(ext.get('source_snapshot',{})),analyses=copy.deepcopy(analyses),
            saves=copy.deepcopy(saves),outputs=copy.deepcopy(tb.get('outputs',[])),required=copy.deepcopy(tb['required']))
        ext['new_test_review']=summary
        plan.append(operation('keep','/extensions/new_test_review','Review all inherited source settings and every new-test override',value=summary))
    tbplan=tbgen(ext.get('tbgen_root')).plan(tb)
    for row in tbplan['operations']:
        plan.append(operation(row['op'],'/extensions/tbgen_spec/'+row['field'],row['reason']))
        if row['op']=='ask': issues.append(issue(row['field'],row['reason']))
    ext.update(tbgen_spec=tb,tbgen_plan=tbplan)
    if spec['source']['kind']=='maestro':
        field='/execution/materialization'
        confirmed=any(c.get('field')==field and c.get('value_hash')==digest('source_new_test') for c in confirmations)
        # Consent is tied to the full source/spec proposal, never persisted as an execution default.
        if confirmed: ext['source_write_consent']=digest(dict(files=spec['source']['files'],test=tb['maestro'],analyses=analyses,saves=saves,outputs=tb['outputs']))
        expected=digest(dict(files=spec['source']['files'],test=tb['maestro'],analyses=analyses,saves=saves,outputs=tb['outputs']))
        if ext.get('source_write_consent')!=expected: ask(field,'Confirm adding the ISF test to this original Maestro; backup and original-test verification are mandatory')
        plan.append(operation('keep','/conditions','TB Generator native sourceTest copy preserves variables, sweeps, corners, models and original tests'))
    return publish(spec,plan,issues)


def publish(spec,plan,issues):
    spec['completion_plan']=plan
    spec['phase']='draft' if issues else 'completed'
    from .contracts import validate
    validate(spec)
    h=digest(spec); path=Path(spec['execution']['workspace'])/'preparations'/spec['spec_id']/(h+'.json')
    write(path,spec)
    return dict(schema='isf.prepare-response.v1',contract_version=CONTRACT_VERSION,status='needs_input' if issues else 'ready',spec=spec,spec_path=str(path),spec_hash=h,plan=plan,issues=issues)
