"""Session-bound TB Generator handoff. Only generated SKILL touches live ADE.

The GUI transaction never opens/closes the caller's session. After native
readback, the batch pipeline runs a history-free private execution copy.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from .command_template import wrap_command
from .errors import ValidationError
from .testbench import digest, read, tbgen, verify_source, write


def prepare(spec_path, *, spec_hash, context, save_policy='reject'):
    spec=read(spec_path)
    if digest(spec)!=spec_hash:raise ValidationError('SPEC_CHANGED',code='SOURCE_CHANGED')
    if spec['phase']!='completed' or spec['source']['kind']!='maestro' or not spec['extensions'].get('source_write_consent'):
        raise ValidationError('Complete and confirm the Maestro proposal first')
    verify_source(spec)
    module=tbgen(spec['extensions'].get('tbgen_root'))
    from vco_tbgen.session import disk_revision
    native=copy.deepcopy(context.get('session_add',context))
    bridge_input=Path(native.pop('bridge_dir'))
    if bridge_input.is_symlink():raise ValidationError('Session bridge cannot be a symlink')
    bridge=bridge_input.resolve()
    native.update(schema='vco-tbgen.session-context.v1',save_policy=save_policy)
    setup=Path(spec['source']['maestro_dir'])
    native.setdefault('revision',disk_revision(setup))
    plan=module.plan_session_add(spec['extensions']['tbgen_spec'],native)
    materialized=module.materialize_session_add(plan,native)
    bridge.mkdir(mode=0o700,parents=True,exist_ok=True)
    if bridge.is_symlink() or bridge.stat().st_uid!=os.getuid() or bridge.stat().st_mode & 0o077:
        raise ValidationError('Session bridge must be private and owned by the GUI user')
    if (bridge/'pending.json').exists():raise ValidationError('Session bridge already has a transaction; reopen from its Maestro window')
    write(bridge/'review.json',spec['extensions']['new_test_review'])
    pending=dict(schema='isf.session-add.v1',spec=spec,spec_hash=spec_hash,context=native,
        materialized=materialized,bridge_dir=str(bridge),review_path=str(bridge/'review.json'))
    script=Path(materialized['skill_path'])
    pending['skill_sha256']=hashlib.sha256(script.read_bytes()).hexdigest()
    # All strings are encoded SKILL literals, never shell fragments.
    q=lambda v:json.dumps(str(v),ensure_ascii=True)
    pending['dispatch']='isfMaestroApplySessionAdd('+ ' '.join(q(v) for v in [bridge,native['session'],native['generation'],script,materialized['receipt_path'],bridge/'review.json']) +')\n'
    write(bridge/'pending.json',pending)
    return dict(schema='isf.session-add-handle.v1',status='prepared',bridge_dir=str(bridge),review_path=str(bridge/'review.json'))


def submit(handle):
    bridge=Path(handle['bridge_dir']);pending=read(bridge/'pending.json')
    if (bridge/'guard.json').exists():raise ValidationError('Session transaction was already submitted')
    module_root=Path(__file__).resolve().parents[2]
    command = [sys.executable, '-B', '-m', 'isf_tool.core.session_add', str(bridge)]
    command = wrap_command(command, 'lib:' + pending['context']['lcv']['lib'], 'sim:slots')
    write(bridge/'guard.json',dict(command=command))
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONPATH=str(module_root)+os.pathsep+os.environ.get('PYTHONPATH',''))
    with (bridge/'guard.log').open('w') as log:
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,env=env,start_new_session=True)
    return dict(handle,status='awaiting_session_confirmation',guard_pid=process.pid)


def dispatch(bridge):
    """Publish the prepared session request from the guardian."""
    bridge=Path(bridge);p=read(bridge/'pending.json')
    script=Path(p['materialized']['skill_path'])
    if hashlib.sha256(script.read_bytes()).hexdigest()!=p['skill_sha256']:raise ValidationError('Session transaction script changed')
    verify_source(p['spec'])
    path=bridge/'request.il';temporary=bridge/'request.il.tmp'
    temporary.write_text(p['dispatch']);temporary.replace(path)


def status(handle):
    bridge=Path(handle['bridge_dir']);p=read(bridge/'pending.json');terminal=bridge/'native-status.txt'
    if not terminal.exists():
        if (bridge/'guard-failure.json').exists():return dict(handle,status='failed',error=read(bridge/'guard-failure.json'))
        return dict(handle,status='awaiting_session_confirmation')
    state=terminal.read_text().strip()
    if state!='executed':return dict(handle,status=state)
    receipt=read(p['materialized']['receipt_path'])
    if receipt.get('schema')!='vco-tbgen.session-receipt.v1' or receipt.get('context')!=p['context'] or receipt.get('new_test')!=p['spec']['extensions']['tbgen_spec']['maestro']['test']:
        raise ValidationError('Session receipt identity mismatch')
    if receipt.get('status')=='added' and (receipt.get('original_tests_verified') is not True or receipt.get('session_closed') is not False):
        raise ValidationError('Session preservation was not verified')
    return dict(handle,status=receipt['status'],receipt=receipt)


def accept(handle):
    """Freeze a verified session result for the unchanged batch run/results API."""
    state=status(handle)
    if state['status']!='added':raise ValidationError('Session add did not succeed: '+state['status'])
    bridge=Path(handle['bridge_dir']);p=read(bridge/'pending.json');spec=p['spec']
    from .. import api
    cached=api._materialization_path(spec)
    if cached.exists():return api._prepared_source(spec)
    if shutil.disk_usage('/').free<1610612736:raise RuntimeError('DISK_LOW')
    source=Path(spec['source']['maestro_dir'])
    tbgen(spec['extensions'].get('tbgen_root'))
    from vco_tbgen.session import logical_snapshot
    verified=Path(p['materialized']['receipt_path']).parent/'verify-snapshot.json'
    if logical_snapshot(source)!=read(verified):
        raise ValidationError('Session setup changed after transaction readback',code='SOURCE_CHANGED')
    tb=copy.deepcopy(spec['extensions']['tbgen_spec'])
    runroot=Path(spec['execution']['workspace'])/'session-execution'/uuid.uuid4().hex
    lib='ISF_SESSION_'+uuid.uuid4().hex[:12];library=runroot/'oa'/lib
    target=library/tb['maestro']['cell']/tb['maestro']['view']
    # Exclude inherited history before any private batch session can open it.
    def ignore(folder,names):return [n for n in names if n=='results' or n.endswith(('.cdslck','.oa-lock')) or '.cdslck.' in n or n.startswith('.oa-lock')]
    before={n:hashlib.sha256((source/n).read_bytes()).hexdigest() for n in ('maestro.sdb','active.state')}
    shutil.copytree(source,target,ignore=ignore)
    for n,h in before.items():
        if hashlib.sha256((source/n).read_bytes()).hexdigest()!=h or hashlib.sha256((target/n).read_bytes()).hexdigest()!=h:
            raise ValidationError('Session setup changed during private copy',code='SOURCE_CHANGED')
    sdb=target/'maestro.sdb';shutil.copy2(sdb,str(sdb)+'.bak-'+time.strftime('%Y%m%d-%H%M%S'))
    sdb.write_text(re.sub(r'\s*<history>.*?</history>','',sdb.read_text(),flags=re.S))
    cds=runroot/'execution-cds.lib'
    cds.write_text('INCLUDE '+json.dumps(tb['cds_lib'])+'\nDEFINE '+lib+' '+json.dumps(str(library))+'\n')
    tb.update(workspace=str(runroot),cds_lib=str(cds),library_path=str(library));tb['maestro']['lib']=lib
    module=tbgen(spec['extensions'].get('tbgen_root'))
    materialized=dict(schema='vco-tbgen.materialized.v1',status='prepared',spec=tb,spec_hash=digest(tb),
        plan=module.plan(tb),setup_dir=str(target),workspace=str(runroot),session_receipt=state['receipt'],
        original_setup=str(source),original_setup_hashes=before)
    write(runroot/'materialized.json',materialized)
    response=dict(schema='isf.materialized.v1',contract_version='1.0.0',status='prepared',materialized=materialized)
    paths=[Path(row['path']) for row in spec['source']['files']+spec['pdk']['includes']]+[target/n for n in before]+[cds]
    checked=[dict(path=str(f),sha256=hashlib.sha256(f.read_bytes()).hexdigest()) for f in sorted(set(paths))]
    write(cached,dict(schema='isf.materialization-cache.v1',response=response,checked_files=checked))
    return response


def guard(bridge):
    bridge=Path(bridge)
    try:
        dispatch(bridge)
        deadline=time.monotonic()+1800
        while not (bridge/'native-status.txt').exists():
            if time.monotonic()>deadline:
                # The native callback checks this cancellation marker before load.
                (bridge/'cancelled').write_text('guardian timeout\n')
                raise TimeoutError('Maestro session did not acknowledge the request within 1800 seconds')
            time.sleep(.2)
    except Exception as exc:
        write(bridge/'guard-failure.json',dict(error=str(exc)))
        raise

if __name__=='__main__':guard(sys.argv[1])
