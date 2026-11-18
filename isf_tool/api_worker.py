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
        if refs['status']!='completed':raise RuntimeError(str(refs.get('issues')))
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
        row['isf_config']=config;_save(path,row,'isf.preflight')
        if complementary is None:
            report=service.inspect_maestro_pair(config);row['pair_report']=report
            if not report.get('ok'):raise RuntimeError('Maestro/replica handoff: '+str(report.get('issues')))
            role=report['roles']['vco']
            config['testbenches']['vco'].update(binding_fingerprint=role['binding_fingerprint'],history_fingerprint=role['history_fingerprint'])
        row['isf_config']=config;row['isf_preflight']=service.preflight(dict(config=config));_save(path,row,'isf.run')
        # Documented transitional native-history -> owned private rerun. All
        # exchange/replica/thermal/extraction remain the existing implementation.
        jobid=service.start_run(dict(config=config,background=False,require_preflight=True))
        row['isf_job_id']=jobid;job=service.get_job(jobid);row['isf_job']=job
        if job['status']!='succeeded':raise RuntimeError('ISF: '+str(job.get('error') or job.get('message')))
        from .core.maestro_outputs import attach_outputs
        row['result']=attach_outputs(service.get_result(jobid),refs['points'],spec['measurements']['outputs'])
        row['canonical_path']=str(path.parent/'isf-workspace'/'jobs'/jobid/'canonical')
        summaries=row['result'].get('summary',[])
        missing_thermal='thermal' in spec['replica']['mechanisms'] and (not summaries or any((item.get('thermal_noise') or {}).get('status')!='available' for item in summaries))
        if missing_thermal:
            row['job']['state']='partial';row['job']['coverage']['failed']=len(refs['points'])
            row['issues'].append(issue('/replica/mechanisms','Flicker result is available but requested thermal qualification is incomplete','MECHANISM_UNAVAILABLE'))
            _save(path,row,'partial');return 4
        row['job']['coverage']['completed']=len(refs['points']);row['job']['state']='completed';_save(path,row,'completed')
        return 0
    except Exception as exc:return _fail(path,row,exc)

if __name__=='__main__':raise SystemExit(execute_isf(sys.argv[1]) if '--isf-only' in sys.argv else execute(sys.argv[1]))
