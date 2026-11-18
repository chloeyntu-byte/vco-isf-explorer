"""Owned out-of-process worker with separate native and ISF stages."""
from pathlib import Path
import os
import subprocess
import sys
from . import api
from .core.command_template import wrap_command
from .core.models import utc_now
from .core.testbench import issue, read, tbgen, verify_source, write


def _save(path,row,stage):
    row['stage']=stage;row['updated_at']=utc_now();write(path,row)


def _fail(path,row,exc):
    row['job']['state']='failed'
    row['job']['coverage']['failed']=max(1,row['job']['coverage']['expected']-row['job']['coverage']['completed'])
    diagnostic=issue(row['stage'],str(exc),getattr(exc,'code','TIMEOUT' if isinstance(exc,TimeoutError) else 'EXECUTION_FAILED'))
    if getattr(exc,'details',None):diagnostic['details']=exc.details
    row['issues'].append(diagnostic);_save(path,row,'failed')
    return 4


def execute(path):
    path=Path(path)
    with api._workspace_lock(path.parents[2]):
        row=read(path);row['job']['state']='running';_save(path,row,'tbgen.materialize')
    spec=row['spec'];ext=spec['extensions']
    try:
        api._prepared_source(spec)
        # TB Generator coordinates its own resources during native transactions.
        prepared=api.materialize(spec,spec_hash=row['job']['spec_hash'])
        row['materialized']=prepared
        if prepared['status']!='prepared':raise RuntimeError(str(prepared))
        _save(path,row,'tbgen.run')
        module=tbgen(ext.get('tbgen_root'))
        native=module.run(prepared['materialized'],wait=True)
        row['tbgen_run']=native;_save(path,row,'tbgen.results')
        refs=module.results(native);row['native_results']=refs
        if refs['status'] != 'completed' and not any(
                p.get('completion', {}).get('state') == 'completed' for p in refs['points']):
            raise RuntimeError(str(refs.get('issues')))
        row['job']['coverage']['expected']=len(refs['points'])
        import hashlib
        setup=Path(prepared['materialized']['setup_dir'])
        checked=[setup/name for name in ('maestro.sdb','active.state')] + [Path(native['rdb'])]
        row['native_handoff_files']=[dict(path=str(item),sha256=hashlib.sha256(item.read_bytes()).hexdigest()) for item in checked]
        _save(path,row,'isf.waiting_for_resources')
        native_spec = prepared['materialized'].get('spec', ext['tbgen_spec'])
        command = [sys.executable, '-B', '-m', 'isf_tool.api_worker', str(path), '--isf-only']
        command = wrap_command(command, 'lib:' + native_spec['maestro']['lib'], 'sim:slots')
        row['isf_stage_command']=command;write(path,row)
        rc=subprocess.call(command,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        final=read(path)
        if final['job']['state'] in ('completed','partial','failed'):return 0 if final['job']['state']=='completed' else 4
        raise RuntimeError('ISF stage did not publish a terminal receipt; returncode='+str(rc))
    except Exception as exc:return _fail(path,row,exc)


def execute_isf(path):
    """Run the ISF stage on the native handoff after resource coordination."""
    path=Path(path);row=read(path);spec=row['spec'];ext=spec['extensions'];native=row['tbgen_run'];refs=row['native_results']
    try:
        retry_id = row['isf_job_id'] if row.get('stage') == 'isf.retry' else None
        row['job']['state'] = 'running'
        _save(path, row, 'isf.run' if retry_id else 'isf.preflight')
        import hashlib
        for item in row.get('native_handoff_files',[]):
            if hashlib.sha256(Path(item['path']).read_bytes()).hexdigest()!=item['sha256']:
                from .core.errors import ValidationError
                raise ValidationError('Native handoff changed while waiting for ISF: '+item['path'],code='SOURCE_CHANGED')
        from .core.service import ToolService
        (path.parent/'isf-workspace').mkdir(parents=True,exist_ok=True)
        service=ToolService(path.parent/'isf-workspace')
        execution_tb=row['materialized']['materialized'].get('spec',ext['tbgen_spec'])
        m=execution_tb['maestro'];setup=Path(execution_tb['library_path'])/m['cell']/m['view']
        history=native.get('rdb') or refs['points'][0]['rdb']
        a=spec['analysis']
        config=dict(schema_version=2,source_mode='maestro_pair',run_kind='replay',
            testbenches=dict(vco=dict(maestro_dir=str(setup),test=m['test'],history=history,
                mapping_overrides=dict(target_instance=spec['gm']['target_instance'],output_p=spec['output']['positive'],output_n=spec['output']['negative'])),replica=dict(source='generated')),
            analysis=dict(offset=dict(value=a['offsets_hz'][0],unit='Hz'),offsets=dict(mode='list',values=' '.join(map(str,a['offsets_hz']))),max_harmonic=a['isf_max_harmonic'],phase_samples=a['phase_samples']),
            execution=dict(dry_run=False,allow_execute=True,adapter_mode='bundled',background=False,require_preflight=True))
        if spec['gm'].get('peer_instances'):
            if len(spec['gm']['peer_instances'])!=1:raise ValueError('Current generated replica accepts exactly one explicitly selected peer')
            config['testbenches']['vco']['mapping_overrides']['peer_instance']=spec['gm']['peer_instances'][0]
        config.update(ext.get('app_config',{}))
        complementary = spec['gm'].get('complementary')
        if complementary is not None:
            config['analysis']['complementary'] = complementary
        overrides=config.setdefault('portable_setup',{}).setdefault('overrides',{})
        overrides['engine.dc_noise_samples']=a['dc_noise_samples']
        overrides['engine.main_max_sideband']=max(a['noise_max_sideband'],a['isf_max_harmonic']+1)
        # Compatibility bridge for TB Generator 0.1, which does not yet return
        # its resolved runtime profile in a public receipt. Same script, no env dump.
        tbgen(ext.get('tbgen_root'))
        from vco_tbgen.native import DEFAULT_RUNTIME
        runtime=dict(DEFAULT_RUNTIME,**ext['tbgen_spec'].get('runtime',{}))
        config.setdefault('profiles',{}).setdefault('environment',{})['cadence_env']=runtime['environment_script']
        row['native_runtime_profile']=dict(environment_script=runtime['environment_script'],provider='vco_tbgen.native.DEFAULT_RUNTIME+spec.runtime')
        from .core.sweep_points import history_points
        config['testbenches']['vco']['allow_partial_history'] = refs['status'] != 'completed'
        if retry_id:
            jobid = retry_id
            service.resume_job(jobid, {'background': False})
        else:
            report, points = history_points(config)
            if not points:
                raise RuntimeError('No verified Maestro point identities for the new test')
            if row.get('planned_selection'):
                from vco_tbgen.effective import numeric
                import math

                def matches(point, condition):
                    if point['corner'] != condition['corner']:
                        return False
                    parameters = point['parameters']
                    if condition.get('models'):
                        from vco_tbgen.effective import reported_corner_models
                        if reported_corner_models(parameters.get('corModelSpec')) != condition['models']:
                            return False
                    try:
                        return all(math.isclose(numeric(parameters[name]), value,
                                                rel_tol=1e-9, abs_tol=1e-18)
                                   for name, value in condition.get('effective_values', condition['variables']).items())
                    except (KeyError, TypeError, ValueError):
                        return False

                selected = []
                for condition in row['planned_selection']:
                    candidates = [p['id'] for p in points if matches(p, condition)]
                    if len(candidates) != 1:
                        raise RuntimeError('Cannot uniquely associate native condition ' + condition['label'])
                    selected.extend(candidates)
                config['point_selection'] = selected
            role = config['testbenches']['vco']
            role.update(binding_fingerprint=report['binding_fingerprint'],
                        history_fingerprint=report['history_fingerprint'])
            row['isf_config'] = config
            _save(path, row, 'isf.run')
            jobid = service.start_run(dict(config=config, background=False, require_preflight=True))
        row['isf_job_id'] = jobid
        job = service.get_job(jobid)
        row['isf_job'] = job
        if not job['result_available']:
            raise RuntimeError('ISF: ' + str(job.get('error') or job.get('message')))
        from .core.maestro_outputs import attach_outputs
        row['result']=attach_outputs(service.get_result(jobid),refs['points'],spec['measurements']['outputs'])
        row['canonical_path']=str(path.parent/'isf-workspace'/'jobs'/jobid/'canonical')
        summaries = row['result'].get('summary', [])
        points = (row['result'].get('point_sweep') or {}).get('points', [])
        if not points:
            points = [{'id': s.get('core_operating_point_id') or s['operating_point_id'],
                       'status': 'completed'} for s in summaries]
        ids = {p['id'] for p in points}
        completed = {p['id'] for p in points if p['status'] == 'completed'}
        thermal_incomplete = set()
        if 'thermal' in spec['replica']['mechanisms']:
            thermal_incomplete = completed & {
                s.get('core_operating_point_id') or s['operating_point_id'] for s in summaries
                if (s.get('thermal_noise') or {}).get('status') != 'available'
            }
        row['job']['coverage'] = dict(expected=len(ids), completed=len(completed),
                                      failed=len(ids - completed))
        if thermal_incomplete:
            row['job']['coverage']['thermal_incomplete'] = len(thermal_incomplete)
        if ids != completed or thermal_incomplete or job['status'] != 'succeeded':
            row['job']['state'] = 'partial' if summaries else 'failed'
            row['issues'] = [issue('isf.run', job.get('message') if job['status'] != 'succeeded'
                                  else 'Requested thermal qualification is incomplete',
                                  'POINTS_INCOMPLETE')]
            _save(path, row, row['job']['state'])
            return 4
        row['issues'] = []
        row['job']['state'] = 'completed'
        _save(path, row, 'completed')
        return 0

    except Exception as exc:return _fail(path,row,exc)

if __name__=='__main__':raise SystemExit(execute_isf(sys.argv[1]) if '--isf-only' in sys.argv else execute(sys.argv[1]))
