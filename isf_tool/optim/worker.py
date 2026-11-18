"""Owned synchronous execution worker. Resource locks stay with native owners."""
from __future__ import annotations
import hashlib
import importlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
from .api import read, write


def module(root, name):
    if not root:
        raise ValueError('design_lab_root or VCO_DESIGN_LAB_ROOT is required for live Design Lab evaluation')
    root = Path(root).resolve()
    sys.path.insert(0, str(root))
    try:
        result = importlib.import_module(name)
        if not Path(result.__file__).resolve().is_relative_to(root):
            raise ValueError('External module identity collision: '+name)
        return result
    finally:
        sys.path.remove(str(root))


def count_calls(work):
    # Count unique fresh execution directories, never analyses or HB harmonics.
    locations = set()
    for p in work.rglob('receipt.json'):
        row = read(p)
        if row.get('argv') and row.get('start_epoch') is not None:
            locations.add(str(p.parent.resolve()))
    for p in work.rglob('solver-process.json'):
        row=read(p)
        if row.get('pid') is not None and row.get('schema')=='vco.solver.process.v1':
            locations.add(str(p.parent.resolve()))
    for p in work.rglob('run_environment.json'):
        row = read(p)
        if row.get('simulation_completed') and row.get('execution_mode')=='fresh_simulation':
            locations.add(str(p.parent.resolve()))
    # Native ADE runs are not launched by Python. Their Spectre log is evidence.
    for p in work.rglob('spectre.out'):
        text=p.read_text(encoding='utf-8',errors='replace')
        match=re.search(r'^Current working directory:\s*(.+)$',text,re.M)
        directory=Path(match.group(1).strip()) if match else (p.parent.parent if p.parent.name=='psf' else p.parent)
        if directory.name=='netlist': directory=directory.parent
        locations.add(str(directory.resolve()))
    write(work/'spectre-call-directories.json', sorted(locations))
    return len(locations)


def generated_run(request, work):
    ref, level = request['ref'], request['level']
    root = ref.get('design_lab_root') or os.environ.get('VCO_DESIGN_LAB_ROOT')
    gen = module(root, 'vco_studio.generator')
    source = Path(ref['project_path']) if ref.get('project_path') else Path(ref['generated_dir'])/'project.json'
    project = read(source)
    if level == 'L2':
        generated = work/'generated'
        gen.generate(project, generated)
        if ref.get('backend')=='private_netlist':
            from .private import run
            return run(generated,ref,work,request['offsets_hz'],module(root,'vco_studio.site'))
        integration = module(root, 'vco_studio.integrations')
        # Use this checkout only in this disposable worker; no site/config edits.
        integration.TOOL_ROOTS['isf'] = Path(__file__).resolve().parents[2]
        if ref.get('tbgen_root'):
            integration.TOOL_ROOTS['tbgen'] = Path(ref['tbgen_root'])
        response = integration.prepare_isf(generated, work/'native', gm=ref.get('gm'))
        from .. import api
        overrides = dict(analysis=dict(offsets_hz=request['offsets_hz']))
        overrides.update(ref.get('overrides', {}))
        if ref.get('complementary') is not None:
            overrides.setdefault('gm', {}).update(target_instance=ref.get('gm'),
                                                   peer_instances=[ref['peer']] if ref.get('peer') else [],
                                                   complementary=ref['complementary'])
        prepared = api.complete(response['prepare']['spec'], overrides=overrides)
        if prepared['status'] != 'ready':
            write(work/'needs-input.json', prepared)
            raise ValueError('Native preparation needs explicit bindings: '+str(prepared['issues']))
        return native_run(dict(ref=dict(spec_path=prepared['spec_path']), offsets_hz=request['offsets_hz']), work)
    project.setdefault('simulation', {})['noise'] = level == 'L1'
    generated = work/'generated'
    gen.generate(project, generated)
    nodes = ref.get('nodes', {})
    if level=='L0' and nodes:
        # Only modify newly generated private input; preserve its original hash.
        tb = generated/'tb.scs'; manifest = generated/'manifest.json'
        stamp = time.strftime('%Y%m%d-%H%M%S')
        shutil.copy2(tb, str(tb)+'.bak-'+stamp)
        shutil.copy2(manifest, str(manifest)+'.bak-'+stamp)
        saves = sorted(set(v for v in nodes.values() if isinstance(v,str) and v!='0'))
        tb.write_text(tb.read_text()+'\nsave '+' '.join(saves)+'\n')
        row = read(manifest); row['files']['tb.scs'] = hashlib.sha256(tb.read_bytes()).hexdigest(); write(manifest, row)
    sim = module(root, 'vco_studio.simulator')
    result = sim.run(generated, work/'simulation', timeout_s=ref.get('timeout_s',180))
    if result['status'] != 'measured':
        raise RuntimeError(result.get('error', 'HB failed'))
    return dict(psf_dir=str(work/'simulation'/'psf'), result_path=str(work/'simulation'/'result.json'))


def native_run(request, work):
    from .. import api
    from ..core.testbench import digest
    ref = request['ref']
    spec = read(ref['spec_path'])
    if spec['source']['kind']=='maestro':
        raise ValueError('Use kind=maestro replay for an existing Maestro; native_spec must not augment a source setup')
    # Relocate all newly materialized TB and solver outputs to this evaluation.
    spec['execution']['workspace'] = str(work/'native-prepared')
    tb = spec['extensions']['tbgen_spec']
    tb['workspace'] = spec['execution']['workspace']
    tb['maestro']['lib'] = 'ISF_OPT_'+work.name.removeprefix('optim-')[:12]
    tb['library_path'] = str(work/'native-prepared'/'oa'/tb['maestro']['lib'])
    prepared = api.complete(spec, overrides=dict(analysis=dict(offsets_hz=request['offsets_hz'])))
    if prepared['status'] != 'ready':
        raise ValueError('Native spec is incomplete: '+str(prepared['issues']))
    job = api.run(prepared['spec_path'], spec_hash=prepared['spec_hash'], workspace=str(work/'jobs'),
                  request_id='optim-'+digest(str(work)), wait=True)
    result = api.results(job['job_id'], workspace=str(work/'jobs'))
    if result['job']['state'] != 'completed':
        write(work/'native-failure.json',result)
        raise RuntimeError('Native L2 did not complete: '+str(result.get('issues')))
    write(work/'result.json', result['result'])
    return dict(result_path=str(work/'result.json'))


def maestro_run(request, work):
    from ..core.service import ToolService
    ref = request['ref']
    if not ref.get('test') or not ref.get('history'):
        raise ValueError('Maestro evaluation needs explicit test and completed history')
    config = read(ref.get('config', {}))
    config.update(source_mode='maestro_pair', run_kind='replay', testbenches=dict(
        vco=dict(maestro_dir=ref['maestro_dir'], test=ref['test'], history=ref['history'],
                 mapping_overrides=ref.get('mapping_overrides', {})), replica=dict(source='generated')))
    config['analysis'] = dict(offsets=dict(mode='list',values=' '.join(map(str,request['offsets_hz']))),
                              offset=dict(value=request['offsets_hz'][0],unit='Hz'),max_harmonic=ref.get('max_harmonic',3))
    config['execution'] = dict(dry_run=False,allow_execute=True,adapter_mode='bundled',background=False,require_preflight=True)
    service = ToolService(work/'service')
    if ref.get('complementary') is not None:
        config['analysis']['complementary'] = ref['complementary']
        mapping = config['testbenches']['vco']['mapping_overrides']
        if ref.get('gm'):
            mapping['target_instance'] = ref['gm']
        if ref.get('peer'):
            mapping['peer_instance'] = ref['peer']
    else:
        report = service.inspect_maestro_pair(config)
        if not report.get('ok'):
            raise ValueError('Maestro bindings invalid: '+str(report.get('issues')))
        role = report['roles']['vco']
        config['testbenches']['vco'].update(binding_fingerprint=role['binding_fingerprint'],history_fingerprint=role['history_fingerprint'])
    job = service.start_run(dict(config=config,background=False,require_preflight=True))
    state = service.get_job(job)
    if state['status']!='succeeded':
        raise RuntimeError('Maestro ISF failed: '+str(state.get('error')))
    write(work/'result.json',service.get_result(job))
    return dict(result_path=str(work/'result.json'))


def main(path):
    request = read(path); work = Path(request['workspace']); start = time.monotonic()
    try:
        subprocess.run(['df','-h','/'],check=True)
        if shutil.disk_usage('/').free < 1610612736:
            raise RuntimeError('DISK_LOW: below 1.5 GiB')
        ref,level = request['ref'],request['level']
        if ref['kind']=='design_lab':
            reference = generated_run(request, work)
        elif ref['kind']=='native_spec' and level=='L2':
            reference = native_run(request, work)
        elif ref['kind']=='maestro' and level=='L2':
            reference = maestro_run(request, work)
        else:
            raise ValueError('Live Maestro/native_spec supports L2; use exported HB/PN evidence for L0/L1')
        response = dict(status='completed',reference=reference)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        response = dict(status='failed',error=str(exc))
    response['cost'] = dict(spectre_calls=count_calls(work), worker_elapsed_s=time.monotonic()-start)
    write(work/'response.json',response)
    return 0 if response['status']=='completed' else 2


if __name__=='__main__':
    raise SystemExit(main(sys.argv[1]))
