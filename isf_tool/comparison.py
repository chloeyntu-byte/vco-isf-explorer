"""Read-only before/after attribution for two matched, simulated samples."""
import math

from .core.result_presentation import enrich_summary, noise_contribution_rows
from .metrics import compute_metrics, select_point


def _scope(summary):
    identity = summary.get('flicker_pair_identity') or {}
    thermal = summary.get('thermal_noise') or {}
    return dict(device=summary.get('device_instance') or summary.get('target_noise_trace')
                       or identity.get('target_noise_trace'),
                pair_model=summary.get('pair_model'),
                flicker='device' if summary.get('device_instance') else
                        'pair' if summary.get('predicted_pair_dBc_Hz') is not None else 'single',
                pair=(identity.get('target_noise_trace'), identity.get('peer_noise_trace')),
                thermal=thermal.get('scope'),
                thermal_members=sorted((item['device'], item['mechanism']) for item in thermal.get('sources', [])))


def _error(prediction, reference):
    return prediction - reference if prediction is not None and reference is not None else None


def _evidence(summary):
    if summary.get('device_instance'):
        from .core.complementary import device_closure
        rows = device_closure(summary)
        return {name: rows[key]['error_db'] for name, key in
                (('flicker', 'flicker'), ('thermal', 'channel_thermal'))}
    pair = summary.get('predicted_pair_dBc_Hz') is not None
    flicker = _error(summary.get('predicted_pair_dBc_Hz' if pair else 'dynamic_dBc_Hz'),
                     summary.get('spectre_pair_dBc_Hz' if pair else 'spectre_fn_dBc_Hz'))
    thermal = next((row for row in noise_contribution_rows(summary, 'pair', 'thermal')
                    if row.get('reference_complete') is True), {})
    return dict(flicker=flicker, thermal=_error(thermal.get('calculated_dbc'), thermal.get('simulated_dbc')))


def compare(before, after, *, point=None, after_point=None, offset_hz=None, device=None):
    """Report after minus before at one offset and modeled source scope."""
    selected = [select_point(data, point=selector, offset_hz=offset_hz, device=device)
                for data, selector in ((before, point), (after, after_point or point))]
    summaries = [enrich_summary(item[2]) for item in selected]
    a, b = summaries
    match_fields = ('operating_point_id', 'corner', 'operating_parameters')
    explicit_different_point = after_point is not None and a['operating_point_id'] != b['operating_point_id']
    if (not a['operating_point_id'] or not b['operating_point_id']
            or a['offset_Hz'] is None or a['offset_Hz'] != b['offset_Hz']
            or (not explicit_different_point and any(a.get(k) != b.get(k) for k in match_fields))):
        raise ValueError('Compare samples at the same operating point and offset, or select both keys explicitly.')
    if _scope(a) != _scope(b):
        raise ValueError('Compare samples with matching modeled source scopes')
    metrics = [compute_metrics(data, point=sample[0], offset_hz=summaries[i]['offset_Hz'], device=device)
               for i, (data, sample) in enumerate(zip((before, after), selected))]
    changes = []

    def add(name, unit, first, second, *, angle=False):
        delta = _error(second, first)
        if angle and delta is not None:
            delta = (delta + 180) % 360 - 180
        changes.append(dict(metric=name, unit=unit, before=first, after=second, delta=delta))

    errors = [_evidence(summary) for summary in summaries]
    for kind in ('flicker', 'thermal'):
        add(f'Matched {kind} ISF − HBnoise', 'dB', errors[0][kind], errors[1][kind])
    add('Oscillation frequency', 'Hz', *(m['f0_hz'] for m in metrics))
    add('Modeled ISF phase noise', 'dBc/Hz', *(m['closure']['modeled_total_dbc_hz'] for m in metrics))
    add('Whole-VCO HBnoise phase noise', 'dBc/Hz',
        *(m['closure']['spectre_total_dbc_hz'] for m in metrics))
    for label, unit, section, key in (
            ('c0_eff', 's^-1/√Hz', 'flicker', 'c0_eff'), ('ρ_dyn', '1', 'flicker', 'rho_dyn'),
            ('ρ_DC', '1', 'flicker', 'rho_dc'), ('Differential ψ2', 'deg', 'waveform', 'psi2_deg'),
            ('Single-ended ψ2', 'deg', 'single_ended', 'psi2_deg'),
            ('Max rise/fall slope ratio', '1', 'waveform', 'max_slope_ratio'),
            ('Mean rise/fall slope ratio', '1', 'waveform', 'mean_slope_ratio'),
            ('Γ_rms', '1/C', 'gamma', 'rms')):
        add(label, unit, *(m[section].get(key) for m in metrics), angle=unit == 'deg')
    add('Differential peak swing', 'V', *(m['vpk_v'] for m in metrics))

    def groups(label, unit, values):
        for key in sorted(set(values[0]) | set(values[1])):
            add(f'{label} {key}', unit, *(row.get(key) for row in values), angle=unit == 'deg')

    for field, label, unit in (('c_k', 'Coherent c_k', '1'), ('delta_k_deg', 'Dynamic δ_k', 'deg')):
        groups(label, unit, [m['flicker'][field] for m in metrics])
    for field, label, unit in (('band_frac', 'Thermal |m| share', '1'),):
        groups(label, unit, [m['thermal'][field] for m in metrics])
    powers = []
    for summary in summaries:
        by_band = {}
        for source in (summary.get('thermal_noise') or {}).get('sources', []):
            for band in source['input_band_contributions']:
                by_band.setdefault(str(band['input_band']), []).append(band['psd_V2_Hz'])
        powers.append({key: math.fsum(values) for key, values in by_band.items()})
    groups('Thermal B_m', 'V²/Hz', powers)
    groups('HBnoise source share', '1', [{s['id']: s['share'] for s in m['sources']} for m in metrics])
    return dict(schema='isf.metrics-comparison.v1',
                explanation='Explanation of simulated results. Use matched closure errors to assess attribution and whole-VCO HBnoise to qualify phase noise.',
                before=metrics[0]['source'], after=metrics[1]['source'], scope=_scope(a), changes=changes)
