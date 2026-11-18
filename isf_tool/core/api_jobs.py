"""Resolve native preparation jobs through their durable workspace manifests."""
from pathlib import Path

from .errors import JobNotFoundError
from .testbench import read


def manifest_path(workspace, job_id):
    if not isinstance(job_id, str) or not job_id or Path(job_id).name != job_id or job_id in ('.', '..'):
        raise JobNotFoundError(f'Invalid job ID: {job_id}')
    return Path(workspace).resolve() / 'api-jobs' / job_id / 'manifest.json'


def manifest(workspace, job_id):
    path = manifest_path(workspace, job_id)
    if not path.is_file():
        return None
    if path.resolve() != path:
        raise JobNotFoundError(f'Job manifest redirects outside its expected path: {job_id}')
    row = read(path)
    if row.get('job', {}).get('job_id') != job_id:
        raise JobNotFoundError(f'Job manifest identity mismatch: {job_id}')
    return row


def desktop_job(row):
    job = row['job']
    state = job['state']
    available = isinstance(row.get('result'), dict)
    status = 'succeeded' if state == 'completed' or (state == 'partial' and available) else state
    stage = row.get('stage') or state
    terminal = state in ('completed', 'partial', 'failed')
    progress = 100 if terminal else {
        'tbgen.materialize': 10, 'tbgen.run': 25, 'tbgen.results': 45,
        'isf.waiting_for_resources': 55, 'isf.preflight': 65, 'isf.run': 75,
    }.get(stage, 0)
    message = f'{stage} · {state}' if stage != state else state
    events = [dict(stage=stage, status=status, message=message,
                   progress=progress, timestamp=row.get('updated_at'))]
    return dict(id=job['job_id'], status=status, api_state=state, message=message,
                progress=progress, result_available=available, resume_available=False,
                created_at=row.get('created_at'), updated_at=row.get('updated_at'),
                is_offset_sweep=False, events=events, stages=events,
                error=next(iter(row.get('issues', [])), None))


def desktop_jobs(workspace):
    for path in (Path(workspace) / 'api-jobs').glob('*/manifest.json'):
        try:
            row = manifest(workspace, path.parent.name)
            if row is not None:
                yield desktop_job(row)
        except (OSError, ValueError, KeyError, TypeError, JobNotFoundError):
            continue
