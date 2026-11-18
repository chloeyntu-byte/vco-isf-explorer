"""Versioned JSON API for native preparation and the existing ISF engine.

Native writes/solver runs are exclusively delegated to VCO TB Generator. The
second stage uses ToolService's Maestro + generated replica path unchanged.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from .core.errors import ValidationError
from .core.testbench import CONTRACT_VERSION, complete_spec, digest, draft, read, tbgen, verify_source, write


def capabilities(*, tbgen_root=None):
    try:
        module=tbgen(tbgen_root)
        available=hasattr(module, 'isf'); version=module.__version__
    except (ImportError, ValidationError):
        available=False; version=None
    return dict(schema='isf.capabilities.v1',contract_version=CONTRACT_VERSION,
                entries=['design_lab','maestro','oa_cell'],tbgen=dict(available=available,version=version),
                mechanisms=['flicker','thermal'],backend='maestro',replica='generated',
                gui_add_test=dict(available=available and all(callable(getattr(module,n,None)) for n in ('plan_session_add','materialize_session_add')),reason='Requires the context-bound Virtuoso plugin; native confirmation and preservation receipt are mandatory'),
                compatibility=dict(existing_launch=True,web=True,diagnose=True))


def browse_libraries(cds_lib, *, workspace, tbgen_root=None):
    tbgen(tbgen_root)
    from vco_tbgen.catalog import browse_libraries as native_browse
    catalog = native_browse(cds_lib, workspace=Path(workspace).resolve()/'tbgen-readers')
    return dict(schema='isf.catalog.v1',contract_version=CONTRACT_VERSION,catalog=catalog)


def describe_cell(lcv, *, cds_lib, workspace, tbgen_root=None):
    module = tbgen(tbgen_root)
    from vco_tbgen.catalog import describe_cell as native_describe
    descriptor = native_describe(_native_lcv(lcv), cds_lib, workspace=Path(workspace).resolve()/'tbgen-readers')
    return dict(schema='isf.cell.v1',contract_version=CONTRACT_VERSION,descriptor=descriptor,classification=module.classify_ports(descriptor))


def _native_lcv(value):
    return dict(lib=value.get('lib',value.get('library')),cell=value['cell'],view=value.get('view','schematic'))


def _tb_base(request,workspace,mode):
    m=request['maestro']; work=Path(workspace).resolve()
    return dict(schema='vco-tbgen.spec.v1',mode=mode,workspace=str(work),cds_lib=str(Path(request['cds_lib']).resolve()),
                library_path=str(Path(request.get('library_path',work/'oa'/m['lib'])).resolve()),
                dut=_native_lcv(request['dut']),ports=[],maestro=copy.deepcopy(m),analyses=[])


def prepare_from_design_lab(generated_dir, *, workspace, dut=None, cds_lib=None, library_path=None, maestro=None,
                            tbgen_root=None, design_lab_root=None, overrides=None, confirmations=(), tbgen_spec=None):
    """A: consume a Design Lab generated directory and its exported DUT identity."""
    if isinstance(generated_dir,dict) and tbgen_spec is None:
        request=generated_dir
        if 'tbgen_spec' in request: tbgen_spec=request['tbgen_spec']
        else:
            return prepare_from_design_lab(request['generated_dir'],workspace=workspace,
                dut=request['dut'],cds_lib=request['cds_lib'],library_path=request['library_path'],maestro=request['maestro'],
                tbgen_root=tbgen_root or request.get('tbgen_root'),design_lab_root=design_lab_root,overrides=overrides or request.get('overrides'),confirmations=confirmations)
    module=tbgen(tbgen_root)
    tb=copy.deepcopy(tbgen_spec) if tbgen_spec else module.from_design_lab(generated_dir,dut=_native_lcv(dut),workspace=workspace,
            cds_lib=cds_lib,library_path=library_path,maestro=maestro,design_lab_root=design_lab_root,completeness='L-b')
    generated=tb.get('source',{}).get('generated_dir', str(generated_dir))
    topology=Path(generated)/'tb.scs'
    response=draft('design_lab',tb,workspace=workspace,overrides=overrides,tbgen_root=tbgen_root,topology_file=str(topology) if topology.is_file() else None)
    return complete(response['spec'],confirmations=confirmations) if confirmations else response


def prepare_from_cell(cell, *, cds_lib=None, workspace, port_bindings=None, overrides=None, confirmations=(), tbgen_root=None):
    """C: explicit bindings; unclassified/unpowered ports remain blocking asks."""
    request=copy.deepcopy(cell)
    if cds_lib: request['cds_lib']=cds_lib
    if 'tbgen_spec' in request: tb=copy.deepcopy(request['tbgen_spec'])
    else:
        request.setdefault('dut',cell.get('lcv',cell))
        tb=_tb_base(request,workspace,'create_new')
        tb.update(copy.deepcopy(port_bindings or request.get('port_bindings') or {}))
        for key in ('ports','supplies','controls','registers','variables','models','analyses','saves','outputs','probes','initial_conditions','simulator_options','corners','sweeps','runtime'):
            if key in request:tb[key]=copy.deepcopy(request[key])
        if not tb['ports']:
            desc=describe_cell(tb['dut'],cds_lib=tb['cds_lib'],workspace=workspace,tbgen_root=tbgen_root)['descriptor']
            tb['ports']=[dict(name=p['name'],net=p['name'],role='unknown',direction=p['direction']) for p in desc['ports']]
    response=draft('oa_cell',tb,workspace=workspace,overrides=overrides or request.get('overrides'),tbgen_root=tbgen_root,topology_file=request.get('topology_file'))
    return complete(response['spec'],confirmations=confirmations) if confirmations else response


def prepare_from_maestro(maestro, *, workspace, test=None, history=None, overrides=None, confirmations=(), tbgen_root=None, **kwargs):
    """B: augment the actual specified Maestro, never silently copy the setup."""
    request=read(maestro) if not isinstance(maestro,dict) else copy.deepcopy(maestro)
    if 'tbgen_spec' in request: tb=request['tbgen_spec']
    else:
        tb=_tb_base(request,workspace,'add_test_to_existing')
        if test:tb['maestro']['source_test']=test
    tb['mode']='add_test_to_existing'
    from .core.maestro_pair import inspect_maestro
    path=Path(tb['library_path'])/tb['maestro']['cell']/tb['maestro']['view']
    inspected=inspect_maestro(dict(path=str(path),test=tb['maestro']['source_test'],history=history,role='vco',include_histories=bool(history)))
    selected=inspected['selected_test']
    if not selected:raise ValidationError('Select an existing source test')
    module=tbgen(tbgen_root)
    from vco_tbgen.effective import literal
    # Read all saved analysis fields without executing SKILL expressions.
    analyses=[]
    for a in selected.get('analyses',[]):
        kind=a.get('type',a.get('name'))
        if kind not in ('hb','hbnoise','hbxf'):continue
        if not a.get('enabled',False):
            analyses.append(dict(kind=kind,enabled=False,parameters={}))
            continue
        options={}
        for k,v in a.get('native_fields', a.get('fields',{})).items():
            if k=='enable':continue
            try:options[k]=literal(v)
            except ValueError:options[k]=v
        analyses.append(dict(kind=kind,parameters=options))
    # The source's enabled HB is authoritative even when a saved ISF draft
    # already contains incremental hbnoise/hbxf settings.
    tb['analyses']=analyses
    topology=request.get('topology_file')
    if not topology and inspected.get('selected_history'):
        files=inspected['selected_history'].get('source_netlists',[])
        if files:topology=files[0]['path']
    response=draft('maestro',tb,workspace=workspace,overrides=overrides or request.get('overrides'),tbgen_root=tbgen_root,topology_file=topology)
    response['spec']['extensions']['source_snapshot']=dict(global_variables=inspected.get('global_variables',[]),selected_test=selected,
        saved_setup_xml={name:(path/name).read_text() for name in ('maestro.sdb','active.state')})
    # Conditions are inherited once by the copied test and shared by the set.
    # Keep serialized global/test/corner scopes, without guessing effective values.
    import xml.etree.ElementTree as ET
    state=ET.parse(path/'active.state').getroot()
    native_test=next((t for t in state.findall('./Test') if t.get('Name')==tb['maestro']['source_test']),None)
    if native_test is not None:
        temps=native_test.findall('./component[@Name="simulatorOptions"]//field[@Name="temp"]')
        if temps:
            temperature = literal(temps[-1].text or 'nil')
            from vco_tbgen.effective import numeric
            if temperature is not None:
                try:
                    numeric(temperature)
                except ValueError:
                    response['spec']['extensions']['source_temperature_expression'] = temperature
                else:
                    response['spec']['conditions']['temperature_c'] = temperature
    response['spec']['extensions']['inherited_conditions']=dict(global_variables=inspected.get('global_variables',[]),variables=selected.get('variables',[]),temperature_c=response['spec']['conditions']['temperature_c'],source_setup=str(path))
    return complete(response['spec'],overrides=overrides or request.get('overrides'),confirmations=confirmations)


def complete(spec, *, overrides=None, confirmations=()):
    return complete_spec(spec,overrides=overrides,confirmations=confirmations)


def _materialization_path(spec):
    return Path(spec['execution']['workspace'])/'materializations'/(digest(spec)+'.json')


def _prepared_source(spec):
    receipt=_materialization_path(spec)
    if not receipt.exists():
        verify_source(spec);return None
    cached=read(receipt)
    for row in cached['checked_files']:
        path=Path(row['path'])
        import hashlib
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
            raise ValidationError('Prepared native source changed: '+str(path),code='SOURCE_CHANGED')
    return cached['response']


def materialize(spec_path, *, spec_hash, workspace=None):
    import hashlib
    spec=read(spec_path)
    if digest(spec)!=spec_hash: raise ValidationError('SPEC_CHANGED',code='SOURCE_CHANGED')
    if spec['phase']!='completed':raise ValidationError('Complete all requested inputs first')
    cached=_prepared_source(spec)
    if cached:return cached
    ext=spec['extensions']
    if spec['source']['kind']=='maestro' and not ext.get('source_write_consent'):raise ValidationError('Original Maestro write requires explicit confirmation')
    result=tbgen(ext.get('tbgen_root')).materialize(ext['tbgen_spec'],ext['tbgen_plan'])
    response=dict(schema='isf.materialized.v1',contract_version=CONTRACT_VERSION,status=result['status'],materialized=result)
    if result['status']=='prepared':
        paths=[row['path'] for row in spec['source']['files']+spec['pdk']['includes']]
        if result.get('setup_dir'):paths += [str(Path(result['setup_dir'])/name) for name in ('maestro.sdb','active.state')]
        checked=[dict(path=p,sha256=hashlib.sha256(Path(p).read_bytes()).hexdigest()) for p in sorted(set(paths))]
        write(_materialization_path(spec),dict(schema='isf.materialization-cache.v1',response=response,checked_files=checked))
    return response


@contextlib.contextmanager
def _workspace_lock(work):
    work.mkdir(parents=True,exist_ok=True)
    with (work/'.isf-api.lock').open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        yield


def planned_points(spec):
    """Preview the native condition grid before running the testbench."""
    from .core.sweep_points import label, point_key
    spec = read(spec)
    ext = spec['extensions']
    tbgen(ext.get('tbgen_root'))
    from vco_tbgen.effective import effective_spec, expected_conditions
    tb = copy.deepcopy(ext['tbgen_spec'])
    if spec['source']['kind'] == 'maestro':
        tb['maestro']['test'] = tb['maestro']['source_test']
        tb = effective_spec(tb, spec['source']['maestro_dir'])
    conditions = expected_conditions(tb)
    rows = []
    dimensions = {name for name in set().union(*(c['variables'].keys() for c in conditions))
                  if len({c['variables'].get(name) for c in conditions}) > 1}
    for c in conditions:
        variables = {k: v for k, v in c['variables'].items() if k in dimensions}
        rows.append(dict(c, id=point_key(c['corner'], c['variables']),
                         label=label(c['corner'], variables), variables=variables,
                         effective_values=c['variables']))
    return rows


def run(spec_path, *, spec_hash, workspace, request_id, run_kind='replay', wait=False, point_ids=()):
    spec=read(spec_path); work=Path(workspace).resolve()
    if digest(spec)!=spec_hash:raise ValidationError('SPEC_CHANGED',code='SOURCE_CHANGED')
    if spec['phase']!='completed':raise ValidationError('spec is not completed')
    if run_kind!='replay':raise ValidationError('Native integration currently supports engineering replay only',code='UNSUPPORTED_CAPABILITY')
    if not isinstance(request_id,str) or not request_id:raise ValidationError('request_id required')
    point_ids = list(point_ids)
    planned = planned_points(spec) if point_ids else []
    if point_ids and (len(set(point_ids)) != len(point_ids) or set(point_ids) - {r['id'] for r in planned}):
        raise ValidationError('Point selection differs from the prepared native condition grid')
    key=digest(request_id); manifest=work/'api-jobs'/key/'manifest.json'
    with _workspace_lock(work):
        if manifest.exists():
            old=read(manifest)
            if old['job']['spec_hash'] != spec_hash or old.get('point_ids', []) != point_ids:
                raise ValidationError('request_id conflicts with another spec or point selection', code='SOURCE_CHANGED')
            return _run_handle(old['job'])
        _prepared_source(spec)
        active=[]
        for p in (work/'api-jobs').glob('*/manifest.json'):
            row=read(p)
            if row['job']['state'] in ('queued','running'):active.append(row['job']['job_id'])
        if active:raise ValidationError('Workspace has an active job: '+','.join(active))
        job=dict(schema='isf.run.v1',contract_version=CONTRACT_VERSION,job_id=key,state='queued',spec_hash=spec_hash,manifest_path=str(manifest),coverage=dict(expected=0,completed=0,failed=0))
        from .core.models import utc_now
        write(manifest,dict(created_at=utc_now(),schema='isf.run-manifest.v1',contract_version=CONTRACT_VERSION,request_id=request_id,job=job,spec=spec,stage='queued',issues=[], point_ids=point_ids,
                            planned_selection=[r for r in planned if r['id'] in point_ids]))
        command=[sys.executable,'-B','-m','isf_tool.api_worker',str(manifest)]
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONPATH=str(Path(__file__).resolve().parents[1])+os.pathsep+os.environ.get('PYTHONPATH',''))
        with (manifest.parent/'worker.log').open('w') as log:
            proc=subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        payload=read(manifest); payload['worker_pid']=proc.pid;payload['worker_command']=command
        # Worker also takes this lock before publishing, preventing lost stage updates.
        write(manifest,payload)
    if wait:proc.wait()
    return status(key,workspace=workspace)


def retry_points(job_id, *, workspace):
    """Retry failed ISF children against the same verified native history."""
    from .core.api_jobs import manifest, manifest_path
    work = Path(workspace).resolve()
    with _workspace_lock(work):
        row = manifest(work, job_id)
        if (not row or row['job']['state'] not in ('failed', 'partial')
                or not (row.get('isf_job') or {}).get('resume_available')):
            raise ValidationError('This run has no retryable ISF points')
        path = manifest_path(work, job_id)
        if any(read(p)['job']['state'] in ('queued', 'running')
               for p in (work / 'api-jobs').glob('*/manifest.json')):
            raise ValidationError('Workspace has an active testbench job')
        row['job']['state'] = 'queued'
        row['stage'] = 'isf.retry'
        write(path, row)
        try:
            with (path.parent / 'worker.log').open('a') as log:
                env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
                           PYTHONPATH=str(Path(__file__).resolve().parents[1]) + os.pathsep + os.environ.get('PYTHONPATH', ''))
                subprocess.Popen(row['isf_stage_command'], stdin=subprocess.DEVNULL, env=env,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError:
            row['job']['state'] = 'partial' if row.get('result') else 'failed'
            row['stage'] = row['job']['state']
            write(path, row)
            raise
    return dict(ok=True, job_id=job_id, status='queued')


def _run_handle(job):
    # Older saved jobs predate the explicit version field. Reading them must
    # return the current envelope without rewriting historical evidence.
    return dict(job,contract_version=CONTRACT_VERSION)


def _desktop_run_handle(job, workspace):
    state = {'succeeded': 'completed', 'interrupted': 'stopped'}.get(job['status'], job['status'])
    return dict(schema='isf.desktop-run.v1', contract_version=CONTRACT_VERSION, job_id=job['id'],
                state=state, progress=job['progress'], result_available=job['result_available'],
                manifest_path=str(Path(workspace).resolve() / 'jobs' / job['id'] / 'job.json'))


def status(job_id, *, workspace):
    from .core import api_jobs
    from .core.service import ToolService
    row = api_jobs.manifest(workspace, job_id)
    if row is not None:
        return _run_handle(row['job'])
    return _desktop_run_handle(ToolService(workspace).get_job(job_id), workspace)


def _filter_results(result, point_ids, offsets_hz):
    if not result or not (point_ids or offsets_hz):
        return result
    filtered = copy.deepcopy(result)
    offsets = {float(hz) for hz in offsets_hz}
    ids = set(point_ids)

    def matches(row):
        return ((not ids or ids.intersection((row.get('point'), row.get('operating_point_id'),
                                               row.get('core_operating_point_id'))))
                and (not offsets or row.get('offset_Hz') in offsets))

    filtered['summary'] = [r for r in filtered['summary'] if matches(r)]
    keys = {r['point'] for r in filtered['summary']}
    filtered['points'] = {k: v for k, v in filtered['points'].items() if k in keys}
    core = filtered.get('complementary_core')
    if core:
        selected = {(row.get('core_operating_point_id') or row['operating_point_id'], row['offset_Hz'])
                    for row in filtered['summary']}
        core['combined'] = [row for row in core['combined']
                            if (row['operating_point_id'], row['offset_hz']) in selected]
    batch = filtered.get('point_sweep')
    if batch and ids:
        batch['points'] = [row for row in batch['points'] if row['id'] in ids or
                           any(r.get('operating_point_id') == row['id'] or
                               r.get('core_operating_point_id') == row['id'] for r in filtered['summary'])]
    return filtered


def results(job_id, *, workspace, point_ids=(), offsets_hz=()):
    from .core import api_jobs
    from .core.service import ToolService
    row = api_jobs.manifest(workspace, job_id)
    if row is None:
        service = ToolService(workspace)
        job = service.get_job(job_id)
        result = service.get_result(job_id) if job['result_available'] else None
        directory = Path(workspace).resolve() / 'jobs' / job_id / 'canonical'
        error = job.get('error')
        issues = [] if not error else [dict(code=error.get('code') or 'EXECUTION_FAILED',
                                           severity='error', field='', message=error.get('message') or 'Job failed')]
        return dict(schema='isf.results.v1', contract_version=CONTRACT_VERSION,
                    job=_desktop_run_handle(job, workspace), canonical_path=str(directory) if directory.is_dir() else None,
                    result=_filter_results(result, point_ids, offsets_hz), issues=issues)
    result = row.get('result')
    if result and row.get('native_results') and row.get('spec', {}).get('measurements'):
        from .core.maestro_outputs import attach_outputs
        result = attach_outputs(result, row['native_results']['points'], row['spec']['measurements']['outputs'])
    return dict(schema='isf.results.v1', contract_version=CONTRACT_VERSION, job=_run_handle(row['job']),
                canonical_path=row.get('canonical_path'), result=_filter_results(result, point_ids, offsets_hz), issues=row['issues'])


def open_ui(spec_path=None, *, workspace, auto_run=False):
    import time
    if auto_run:raise ValidationError('Open UI does not grant execution consent')
    work=Path(workspace).resolve();work.mkdir(parents=True,exist_ok=True)
    session_id=uuid.uuid4().hex;ready=work/('ui-ready-'+session_id+'.json')
    command=[sys.executable,'-B','-m','isf_tool.tk','--workspace',str(work),'--ready-file',str(ready)]
    if spec_path:command+=['--spec',str(Path(spec_path).resolve())]
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONPATH=str(Path(__file__).resolve().parents[1])+os.pathsep+os.environ.get('PYTHONPATH',''))
    with (work/'ui.log').open('a') as log:proc=subprocess.Popen(command,env=env,stdout=log,stderr=subprocess.STDOUT)
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        if ready.is_file():return dict(schema='isf.open-ui.v1',contract_version=CONTRACT_VERSION,status='opened',session_id=session_id,workspace=str(work),pid=proc.pid)
        if proc.poll() is not None:break
        time.sleep(.05)
    return dict(schema='isf.open-ui.v1', contract_version=CONTRACT_VERSION, status='failed', session_id=None, workspace=str(work), pid=proc.pid if proc.poll() is None else None)


def prepare_session_add(spec_path, *, spec_hash, context, save_policy='reject'):
    from .core.session_add import prepare
    return prepare(spec_path,spec_hash=spec_hash,context=context,save_policy=save_policy)

def submit_session_add(handle):
    from .core.session_add import submit
    return submit(handle)

def session_add_status(handle):
    from .core.session_add import status
    return status(handle)

def accept_session_add(handle):
    from .core.session_add import accept
    return accept(handle)


def metrics(job_id=None, *, result_path=None, point=None, offset_hz=None, device=None, workspace=None, qualify=False):
    """Read-only metrics for a result path, API job, or legacy job in workspace.

    Multiple samples require an explicit point/offset. No run or source change
    is triggered. Job resolution is bounded to the supplied workspace.
    """
    from .metrics import compute_metrics
    target = result_path if result_path is not None else job_id
    if target is None:
        raise ValueError('metrics requires job_id or result_path')
    path = Path(target).expanduser()
    resolved_job = False
    manifest_path = None
    if result_path is not None and not path.is_file():
        raise FileNotFoundError(f'Result file not found: {path}')
    if not path.is_file():
        if workspace is None:
            raise ValueError('A job ID requires workspace; alternatively pass a result.json path')
        if Path(str(target)).name != str(target) or str(target) in ('.', '..'):
            raise ValueError('Invalid job ID')
        resolved_job = True
        work = Path(workspace).expanduser().resolve()
        manifest = work / 'api-jobs' / str(target) / 'manifest.json'
        if manifest.is_file():
            manifest_path = str(manifest)
            payload = read(manifest)
            # Prefer the saved result snapshot to a moved canonical directory.
            raw = payload.get('result')
            if not isinstance(raw, dict):
                canonical = payload.get('canonical_path')
                if not canonical:
                    raise ValueError('Job has no saved ISF result')
                path = Path(canonical)
                if path.is_dir():path = path / 'result.json'
                raw = read(path)
        else:
            path = work / 'jobs' / str(target) / 'result.json'
            raw = read(path)
    else:
        raw = read(path)
    if qualify:
        from .metrics import select_point
        from .optim.api import _l2
        key, _, summary = select_point(raw, point=point, offset_hz=offset_hz, device=device)
        offset = summary.get('offset_Hz', summary.get('offset_hz'))
        if offset is None:
            raise ValueError('Qualification requires an exact offset')
        # For a two-device result, qualification needs both devices at the
        # same physical point before selecting the requested metrics entry.
        physical = summary.get('core_operating_point_id') or key
        qualified = _l2(raw, {'point': physical}, [float(offset)])
        response = next(row for row in qualified if row['source']['point'] == key)
    else:
        response = compute_metrics(raw, point=point, offset_hz=offset_hz, device=device)
    if resolved_job:response['source']['isf_job'] = str(target)
    response['source']['manifest_path'] = manifest_path
    response['source']['result_path'] = str(path.resolve()) if path.is_file() else None
    return response
