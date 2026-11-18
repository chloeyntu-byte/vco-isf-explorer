"""Plan and combine two explicitly selected, independently evaluated MOS sources."""
import copy
import math
from pathlib import Path

from ..calculation import dBc_from_pm_asd
from .component_selection import core_mode, core_targets, validate_core_polarities, validate_core_targets
from .errors import JobFailedError, PreflightError, ValidationError
from .models import POINT_TABLES, empty_result
from .result_presentation import enrich_summary
from .spectre_topology import SpectreTopology
from .testbench import write


def device_configs(config):
    from .service import _RUN_CONFIG_KEYS

    template = {key: copy.deepcopy(value) for key, value in config.items() if key in _RUN_CONFIG_KEYS}
    complementary = template['analysis'].pop('complementary')
    if (template.get('source_mode') not in ('maestro_pair', 'maestro-pair', 'pair')
            or template['testbenches']['replica'].get('source') != 'generated'
            or template.get('config_path') or template['execution'].get('adapter_mode') != 'bundled'
            or template.get('run_kind') == 'signoff'):
        raise ValidationError('Complementary core requires generated replicas with automatic bundled Maestro replay settings')
    vco = template['testbenches']['vco']
    mapping = vco.get('mapping_overrides', {})
    targets = core_targets(mapping.get('target_instance'), mapping.get('peer_instance'), complementary)
    # Target-specific voltage or transfer fields must be rediscovered for each binding.
    output = {key: mapping[key] for key in ('output_p', 'output_n') if mapping.get(key)}
    for target, peer in targets:
        child = copy.deepcopy(template)
        overrides = dict(output, target_instance=target, core_device=True)
        if peer:
            overrides['peer_instance'] = peer
        child['testbenches']['vco']['mapping_overrides'] = overrides
        yield (target, peer), child


def prepare_devices(service, config):
    from .maestro_pair import inspect_maestro_pair
    from .preflight import preflight
    from .offset_sweep import is_sweep, scalar_config
    from .offsets import expand_offsets

    planned, reports = [], []
    mode = core_mode(config['analysis']['complementary'])
    pairs = list(device_configs(config))
    targets = [target for target, _ in pairs]
    polarities = None
    fingerprints = None
    for index, ((target, peer), child) in enumerate(pairs):
        offset = expand_offsets(child['analysis'])[0]
        binding = inspect_maestro_pair(dict(testbenches=child['testbenches'], offset_hz=offset))
        role = binding.get('roles', {}).get('vco', {})
        if not binding['ok'] or role.get('mapping', {}).get('target_instance') != target:
            raise PreflightError('Cannot bind selected complementary device ' + target,
                                 details={'device_instance': target, 'issues': binding.get('issues', [])})
        bound = {key: role.get(key) for key in ('binding_fingerprint', 'history_fingerprint')}
        if fingerprints is not None and fingerprints != bound:
            raise PreflightError('Complementary devices must use the same source setup and completed history')
        fingerprints = bound
        child['testbenches']['vco'].update(fingerprints)
        # A sweep's scalar children still perform their normal per-offset checks.
        checked = scalar_config(child, offset) if is_sweep(child) else child
        report = preflight(checked, workspace=service.workspace, **service._environment_options)
        reports.append(report)
        if not report['ok']:
            raise PreflightError('Complementary device preflight failed for ' + target,
                                 details={'device_instance': target, 'failed_checks': [
                                     item for item in report['checks'] if item['status'] == 'fail']})
        role = report['detected']['maestro_pair']['roles']['vco']
        histories = (role.get('selected_history') or {}).get('source_netlists') or []
        if not histories:
            raise PreflightError('Complementary core requires a completed source history')
        for entry in histories:
            observed = validate_core_targets(SpectreTopology(entry['path']), targets, mode=mode)
            if polarities is not None and observed != polarities:
                raise ValidationError('Complementary model polarity differs across operating points')
            polarities = observed
        planned.append((dict(instance=target, peer_instance=peer, polarity=polarities[index]), child))
    return planned, reports


def _positive(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if value > 0 and math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _source_power(rows, trace, mechanism):
    powers = [_positive(row.get('psd_V2_Hz')) for row in rows
              if row.get('device') == trace and row.get('mechanism') == mechanism]
    return powers[0] if trace and len(powers) == 1 else None


def _dbc(power, carrier):
    return dBc_from_pm_asd(math.sqrt(power), carrier) if power and carrier else None


def _comparison(predicted, measured, carrier):
    p, m = _dbc(predicted, carrier), _dbc(measured, carrier)
    return dict(isf_dbc_hz=p, hbnoise_dbc_hz=m, error_db=p-m if p is not None and m is not None else None)


def device_closure(summary):
    """Only the exact intrinsic trace and the matching fn/id source family."""
    row = enrich_summary(summary)
    thermal = row.get('thermal_noise') or {}
    trace = row.get('target_noise_trace') or thermal.get('target_noise_trace')
    carrier = _positive(row.get('carrier_peak_V'))
    amplitude = _positive(row.get('dynamic_pm_asd_V_rtHz'))
    references = row.get('spectre_noise_contributions') or []
    return dict(
        flicker=_comparison(amplitude**2 if amplitude else None, _source_power(references, trace, 'fn'), carrier),
        channel_thermal=_comparison(_source_power(thermal.get('sources', []), trace, 'id'),
                                    _source_power(references, trace, 'id'), carrier),
    )


def _sum_dbc(values):
    if any(value is None for value in values):
        return None
    peak = max(values)
    return peak + 10 * math.log10(math.fsum(10 ** ((value - peak)/10) for value in values))


def combine_devices(entries, *, mode='complementary'):
    """Keep device tables intact and publish independent sums by point and offset."""
    if len(entries) != 2:
        raise ValidationError('Two-device analysis requires two device results')
    if mode == 'asymmetric_pair':
        validate_core_polarities([device['polarity'] for device, _ in entries], mode)
        if any(device.get('peer_instance') for device, _ in entries):
            raise ValidationError('Asymmetric pair results require two independent device runs')
    result = empty_result()
    summary_sets = []
    for device, source in entries:
        if source.get('run_kind') == 'plan':
            continue
        summaries = {}
        for key, tables in source['points'].items():
            rows = tables['summary']
            if len(rows) != 1:
                raise ValidationError('Complementary analysis requires one summary per device sample')
            row = dict(enrich_summary(rows[0]), **{
                name: rows[0].get(name) or (source.get('dual_tb') or {}).get('metadata', {}).get(name)
                for name in ('target_noise_trace', 'replica_source')
            })
            instance = device['instance']
            thermal = row.get('thermal_noise') or {}
            if mode == 'asymmetric_pair' and (
                    row.get('pair_model') == 'symmetric_independent' or thermal.get('peer_noise_trace')):
                raise ValidationError('Asymmetric pair results cannot include a symmetric peer estimate')
            trace = row.get('target_noise_trace') or thermal.get('target_noise_trace')
            if trace and not (trace == instance or trace.startswith(instance + '.')):
                raise ValidationError('Result noise trace does not belong to selected device ' + instance)
            row.update(target_noise_trace=trace, replica_source='generated')
            physical = row['operating_point_id']
            identity = (physical, row['offset_Hz'])
            new_key = device['instance'] + ' | ' + str(key)
            if identity in summaries or identity[1] is None:
                raise ValidationError('Complementary analysis has a duplicate or unbound device sample')
            tags = dict(point=new_key, operating_point_id=device['instance'] + ' | ' + physical,
                        core_operating_point_id=physical, device_instance=device['instance'],
                        device_polarity=device['polarity'])
            row.update(tags, device_closure=device_closure(row), requested_peer_instance=device.get('peer_instance'))
            root = device.get('workspace')
            job = device.get('job_id')
            if root and job and Path(physical).name == physical and thermal.get('status') == 'available':
                job_root = Path(root) / 'jobs' / job
                if source.get('offset_sweep'):
                    match = [entry for entry in source['offset_sweep']['children'] if entry['offset_hz'] == identity[1]]
                    if len(match) == 1:
                        job_root = Path(match[0]['workspace']) / 'jobs' / match[0]['job_id']
                thermal['source_coefficients_path'] = str(job_root / 'simulation' / physical / 'thermal' / 'source_coefficients.json')
            summaries[identity] = row
            result['points'][new_key] = {name: [dict(item, **tags) for item in tables[name]] for name in POINT_TABLES}
            result['points'][new_key]['summary'] = [row]
            result['summary'].append(row)
        summary_sets.append(summaries)
    combined = []
    if summary_sets:
        if len(summary_sets) != 2 or not summary_sets[0] or set(summary_sets[0]) != set(summary_sets[1]):
            raise ValidationError('Complementary analyses must cover the same operating points and offsets')
        for physical, offset in sorted(summary_sets[0]):
            samples = [rows[(physical, offset)] for rows in summary_sets]
            for field in ('corner', 'operating_parameters'):
                if samples[0].get(field) != samples[1].get(field):
                    raise ValidationError('Device analyses disagree on shared operating evidence: ' + field)
            for field in ('f0_Hz', 'carrier_peak_V', 'spectre_total_dBc_Hz'):
                left, right = [row.get(field) for row in samples]
                dbc = field == 'spectre_total_dBc_Hz'
                if left is None or right is None or not math.isclose(
                        float(left), float(right), rel_tol=0 if dbc else 1e-4, abs_tol=0.05 if dbc else 0):
                    raise ValidationError('Device analyses disagree on shared operating evidence: ' + field)
            closure = {}
            for mechanism in ('flicker', 'channel_thermal'):
                preds = [row['device_closure'][mechanism]['isf_dbc_hz'] for row in samples]
                refs = [row['device_closure'][mechanism]['hbnoise_dbc_hz'] for row in samples]
                p, m = _sum_dbc(preds), _sum_dbc(refs)
                closure[mechanism] = dict(isf_dbc_hz=p, hbnoise_dbc_hz=m,
                                          error_db=p-m if p is not None and m is not None else None)
            flicker = [row['predicted_pair_dBc_Hz'] if row.get('flicker_pair_identity')
                       else row['device_closure']['flicker']['isf_dbc_hz'] for row in samples]
            thermal = [row.get('thermal_dBc_Hz') for row in samples]
            modeled = _sum_dbc(flicker + thermal)
            combined.append(dict(operating_point_id=physical, offset_hz=offset, closure=closure,
                                 modeled_flicker_dbc_hz=_sum_dbc(flicker),
                                 modeled_thermal_dbc_hz=_sum_dbc(thermal),
                                 modeled_total_dbc_hz=modeled,
                                 spectre_total_dbc_hz=samples[0]['spectre_total_dBc_Hz']))
    planned = not summary_sets
    result.update(complementary_core=dict(schema='isf.complementary-core.v1', mode=mode,
                                         devices=[device for device, _ in entries], combined=combined),
                  run_kind='plan' if planned else 'replay', signoff=False)
    if planned:
        result.update(dry_run=True, device_plans=[source for _, source in entries])
    return result


def run_complementary(service, payload, config, emit):
    from .service import ToolService

    planned, _ = prepare_devices(service, config)
    entries = []
    for index, (device, child_config) in enumerate(planned, 1):
        target = device['instance']
        emit('core_device', 'running', 5 + 45 * (index - 1), 'Analyzing ' + target)
        workspace = Path(payload['run_root']) / ('device-%d' % index)
        workspace.mkdir(parents=True, exist_ok=True)
        child = ToolService(str(workspace), command_runner=service._runner, **service._environment_options)
        child_id = child.start_run(dict(config=child_config, background=False, require_preflight=True))
        status = child.get_job(child_id)
        if status['status'] != 'succeeded':
            raise JobFailedError('Complementary analysis failed for ' + target, details=status)
        entries.append((dict(device, job_id=child_id, workspace=child.workspace), child.get_result(child_id)))
    result = combine_devices(entries, mode=core_mode(config['analysis']['complementary']))
    if payload.get('bundle_path'):
        write(Path(payload['bundle_path']) / 'result.json', result)
    return result
