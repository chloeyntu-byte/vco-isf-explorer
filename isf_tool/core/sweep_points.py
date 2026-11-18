"""Maestro sweep identities and durable execution of independent points."""
import copy
import hashlib
import json
from pathlib import Path

from .errors import PreflightError, ValidationError
from .models import empty_result

POINT_STATE = '.isf-point-sweep.json'


def point_key(corner, parameters):
    """Use exact Maestro coordinates, independent of history or point order."""
    coords = json.dumps([corner, parameters], sort_keys=True, ensure_ascii=True,
                        separators=(',', ':'), allow_nan=False)
    return 'pt-' + hashlib.sha256(coords.encode()).hexdigest()[:24]


def label(corner, parameters):
    return ' · '.join([str(corner or 'nominal')] +
                      [f'{key}={value}' for key, value in sorted(parameters.items())])


def catalogue(sources):
    """Retain native IDs and all parameters. Display axes that vary within a corner."""
    by_corner = {}
    for source in sources:
        op = source['operating_point']
        by_corner.setdefault(op['corner'], []).append(op['params'])
    axes = set()
    for group in by_corner.values():
        for name in set().union(*(row.keys() for row in group)):
            if len({json.dumps(row.get(name), sort_keys=True) for row in group}) > 1:
                axes.add(name)
    result = []
    for source in sources:
        op = source['operating_point']
        parameters = {name: value for name, value in op['params'].items()
                      if name != 'corModelSpec'}
        values = {name: parameters[name] for name in sorted(axes) if name in parameters}
        result.append(dict(id=point_key(op['corner'], parameters),
                           label=label(op['corner'], values), corner=op['corner'],
                           variables=values, parameters=op['params'],
                           history=op['rdb'], point_id=op['point_id'], test=op['test'],
                           psf_dir=source.get('psf_dir'), native_error=source.get('error'),
                           test_id=op['test_id'], environment=op.get('environment', {})))
    keys = [item['id'] for item in result]
    if len(keys) != len(set(keys)):
        raise ValidationError('The Maestro history contains repeated corner/variable coordinates')
    return result


def select_points(points, requested):
    if requested is None:
        return points
    if (not isinstance(requested, list) or not requested
            or any(not isinstance(key, str) for key in requested)
            or len(set(requested)) != len(requested)):
        raise ValidationError('Select at least one unique Maestro point key')
    if set(requested) - {p['id'] for p in points}:
        raise ValidationError('Selected point keys do not belong to this Maestro history')
    return [p for p in points if p['id'] in requested]


def history_points(config):
    from .maestro_pair import inspect_maestro
    test = config.get('testbenches', {}).get('vco', {})
    mode = str(config.get('source_mode', config.get('mode', 'auto'))).lower()
    if (mode not in ('auto', 'maestro_pair', 'maestro-pair', 'pair')
            or str(config.get('run_kind', 'replay')).lower() == 'signoff'
            or not (test.get('maestro_dir') or test.get('path')) or test.get('point_key') or config.get('execution', {}).get('dry_run', True)):
        return None, []
    report = inspect_maestro(dict(test, role='vco', catalog_only=True))
    history = report.get('selected_history') or {}
    return report, history.get('points', [])


def child_config(config, report, point):
    child = copy.deepcopy(config)
    child.pop('point_selection', None)
    child.pop('point_batch', None)
    child['testbenches']['vco'].update(
        history=point['history'], point_key=point['id'],
        history_fingerprint=report['history_fingerprint'])
    return child


def combine_point_results(rows, results):
    output = empty_result()
    output.update(run_kind='replay', signoff=False, point_sweep={'points': copy.deepcopy(rows)})
    for identity, result in results.items():
        row = next(item for item in rows if item['id'] == identity)
        for key, tables in result['points'].items():
            if key in output['points']:
                raise ValidationError('Repeated result sample: ' + key)
            target = output['points'][key] = copy.deepcopy(tables)
            for sample in target['summary']:
                sample['point_identity'] = copy.deepcopy(row)
        output['summary'].extend(dict(sample, point_identity=copy.deepcopy(row)) for sample in result['summary'])
        if 'dual_tb' in result:
            output.setdefault('dual_tb', copy.deepcopy(result['dual_tb']))
        if result.get('complementary_core'):
            core = result['complementary_core']
            aggregate = output.setdefault('complementary_core',
                {k: copy.deepcopy(v) for k, v in core.items() if k != 'combined'})
            aggregate.setdefault('combined', []).extend(copy.deepcopy(core.get('combined', [])))
    return output


def run_points(service, job_id, payload, config, emit):
    from .offset_sweep import OffsetSweepStopped
    from .service import ToolService

    root = Path(payload['run_root'])
    path = root / POINT_STATE
    batch = config['point_batch']
    if path.exists() and not payload.get('resume'):
        raise ValidationError('Run folder is already bound to a point sweep')
    checkpoint = {'job_id': job_id, 'history_fingerprint': batch['history_fingerprint'],
                  'points': [dict(point, status='pending') for point in batch['points']]}
    if payload.get('resume'):
        previous = json.loads(path.read_text())
        if (previous['job_id'] != job_id or previous['history_fingerprint'] != batch['history_fingerprint']
                or [p['id'] for p in previous['points']] != [p['id'] for p in batch['points']]):
            raise ValidationError('Point checkpoint does not match this run')
        checkpoint = previous
    rows = checkpoint['points']
    results = {}

    def publish():
        service._atomic_json(str(path), checkpoint)
        result = combine_point_results(rows, results)
        with service._lock:
            service._jobs[job_id]['result'] = result
        service._persist_job(job_id)
        return result

    for row in rows:
        if row['status'] == 'completed':
            child = ToolService(root / 'points' / row['id'], command_runner=service._runner)
            results[row['id']] = child.get_result(row['job_id'])
    publish()
    for index, row in enumerate(rows):
        if row['id'] in results:
            continue
        with service._lock:
            stopped = service._jobs[job_id].get('cancel_requested', False)
        if stopped:
            raise OffsetSweepStopped('Stopped at the point boundary. Resume runs remaining points.')
        row.update(status='running', error=None)
        row.pop('error_details', None)
        publish()
        emit('point_sweep', 'running', 95 * index / len(rows), row['label'])
        try:
            if row.get('native_error'):
                raise ValidationError(row['native_error'])
            workspace = root / 'points' / row['id']
            workspace.mkdir(parents=True, exist_ok=True)
            child = ToolService(workspace, command_runner=service._runner)
            cfg = child_config(config, batch, row)
            child_id = child.start_run(dict(config=cfg, background=False, require_preflight=True))
            state = child.get_job(child_id)
            row['job_id'] = child_id
            if state['status'] != 'succeeded':
                error = state.get('error') or {}
                raise PreflightError(error.get('message') or state['message'],
                                     details=error.get('details'))
            result = child.get_result(child_id)
            if not result['summary'] or any(
                    sample.get('operating_point_id') != row['id']
                    and sample.get('core_operating_point_id') != row['id']
                    for sample in result['summary']):
                raise ValidationError('Child result identity differs from the requested point')
            rows_by_sample = {sample['point']: sample for sample in result['summary']}
            evidence = {}
            for sample_key, sample in rows_by_sample.items():
                child_workspace = root / 'points' / row['id']
                child_job_id = child_id
                offset = sample.get('offset_Hz')
                if result.get('offset_sweep'):
                    entry = next((item for item in result['offset_sweep']['children']
                                  if item.get('job_id') == sample.get('child_job_id')
                                  and item.get('offset_hz') == offset), None)
                    if not entry:
                        continue
                    child_workspace = Path(entry['workspace'])
                    child_job_id = entry['job_id']
                evidence[sample_key] = str(child_workspace / 'jobs' / child_job_id)
            row['sample_evidence'] = evidence
            results[row['id']] = result
            row['status'] = 'completed'
        except Exception as exc:  # each point has its own preflight, run and artifacts
            row.update(status='failed', error=str(exc))
            if getattr(exc, 'details', None):
                row['error_details'] = exc.details
        publish()
    failed = [row['label'] for row in rows if row['status'] == 'failed']
    if failed:
        raise PreflightError('Retry failed points: ' + ', '.join(failed))
    return combine_point_results(rows, results)
