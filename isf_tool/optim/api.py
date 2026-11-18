"""Public JSON contract. Diagnostics never substitute for measured HBnoise."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from ..core.command_template import wrap_command

VERSION = '1.0.0'
SCHEMA = 'vco.isf-optim-evaluation.v1'


def read(value):
    result = copy.deepcopy(value) if isinstance(value, dict) else json.loads(Path(value).read_text(encoding='utf-8'))
    if not isinstance(result, dict):
        raise ValueError('OPTIM_INPUT_OBJECT: expected a JSON object')
    return result


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)+'\n', encoding='utf-8')


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def resolve(value):
    """Paths are read-only; a sidecar supplies otherwise ambiguous bindings."""
    if isinstance(value, dict):
        result = copy.deepcopy(value)
    else:
        path = Path(value).expanduser().resolve()
        if path.is_dir():
            if (path/'optim-ref.json').is_file():
                result = read(path/'optim-ref.json')
            elif (path/'manifest.json').is_file() and (path/'project.json').is_file():
                result = dict(kind='design_lab', generated_dir=str(path))
            elif (path/'result.json').is_file():
                result = dict(kind='artifact', result_path=str(path/'result.json'))
            elif (path/'hb.fd.pss_hb').is_file():
                result = dict(kind='hb_psf', psf_dir=str(path))
            else:
                raise ValueError('Directory needs optim-ref.json or an unambiguous generated/result/HB PSF artifact')
        elif path.is_file():
            raw = read(path)
            if raw.get('schema') == 'vco.project.v1':
                result = dict(kind='design_lab', project_path=str(path))
            elif raw.get('schema') == 'vco.isf-design-ref.v1' or (
                    'schema' not in raw and raw.get('kind') in {'artifact', 'hb_psf', 'design_lab', 'native_spec', 'maestro'}):
                result = raw
            elif raw.get('schema') == 'isf.testbench.v1':
                result = dict(kind='native_spec', spec_path=str(path))
            else:
                result = dict(kind='artifact', result_path=str(path))
        else:
            raise ValueError('design_ref does not exist: '+str(path))
    result.setdefault('kind', 'hb_psf' if 'hb_waveform' in result or 'psf_dir' in result else 'artifact' if 'result_path' in result or 'result' in result else 'design_lab')
    result.setdefault('schema','vco.isf-design-ref.v1')
    from .schema import validate
    return validate(result,'DesignRef')


def _raw(ref):
    target = ref.get('result', ref.get('result_path'))
    if target is None:
        raise ValueError('Reference needs a canonical result or measured result_path')
    raw = read(target)
    for _ in range(4):
        if 'points' in raw or 'workpoint' in raw or raw.get('status') == 'measured':
            return raw
        if not isinstance(raw.get('result'), dict):
            return raw
        raw = raw['result']
    return raw


def _offsets(offsets_hz):
    offsets = [number(x) for x in offsets_hz]
    if not offsets or any(x is None or x <= 0 for x in offsets) or len(offsets) != len(set(offsets)):
        raise ValueError('offsets_hz must contain distinct, finite positive frequencies')
    return offsets


def _harmonic_order(row):
    """Nonnegative integer harmonic order of one row, or None if malformed."""
    if not isinstance(row, dict):
        return None
    k = row.get('k')
    if isinstance(k, bool):
        return None
    if isinstance(k, int):
        return k if k >= 0 else None
    if isinstance(k, str) and k.isascii() and k.isdigit():
        return int(k)
    return None


def _harmonic_order_issues(orders, maximum):
    """Require the full declared 0..N spectrum, never infer N from survivors."""
    if type(maximum) is not int or not 1 <= maximum <= 12:
        return ['ISF_HARMONIC_RANGE_INVALID']
    issues = []
    if len(orders) != len(set(orders)):
        issues.append('ISF_HARMONIC_ORDER_DUPLICATE')
    if set(orders) != {str(k) for k in range(maximum + 1)}:
        issues.append('ISF_HARMONIC_ORDERS_INCOMPLETE')
    return issues


def _flicker_issues(metrics):
    """Required finite diagnostics, including coherent harmonic projections."""
    flicker = metrics.get('flicker') or {}
    issues = ['ISF_FLICKER_'+key.upper()+'_UNAVAILABLE' for key in ('rho_dyn', 'c0_eff')
              if number(flicker.get(key)) is None]
    harmonics = flicker.get('c_k') or {}
    issues.extend(_harmonic_order_issues(list(harmonics),
                  (metrics.get('source') or {}).get('max_harmonic', 3)))
    if not harmonics or any(number(v) is None for v in harmonics.values()):
        issues.append('ISF_HARMONIC_Q_UNAVAILABLE')
    return issues


def gate(metrics):
    """Require affirmative quality and complete evidence before shaping."""
    if not isinstance(metrics, dict):
        raise ValueError('OPTIM_INPUT_OBJECT: expected a metrics object')
    if metrics.get('schema') == SCHEMA:
        decisions = [gate(m) for m in metrics.get('metrics', [])]
        allowed = bool(decisions) and all(d['isf_shaping_allowed'] for d in decisions)
        return dict(isf_shaping_allowed=allowed, reason='; '.join(d['reason'] for d in decisions) or 'ISF_CLOSURE_UNAVAILABLE: no metrics')
    closure = metrics.get('closure') or {}
    quality = metrics.get('quality') or {}
    gap = number(closure.get('gap_db'))
    error = number(closure.get('pair_error_db'))
    per_device = (metrics.get('source') or {}).get('device_instance')
    device_closure = closure.get('device') or {}
    reasons = []
    if quality.get('numerical_quality_pass') is not True:
        reasons.append('ISF_NUMERICAL_QUALITY_UNQUALIFIED: numerical quality must explicitly pass')
    if quality.get('pm_magnitude_projection_valid') is not True:
        reasons.append('ISF_PM_PROJECTION_UNQUALIFIED: PM projection must be explicitly valid')
    if quality.get('thermal_available') is not True:
        reasons.append('ISF_THERMAL_UNAVAILABLE: qualified thermal evidence required')
    reasons.extend(_flicker_issues(metrics))
    if quality.get('flicker_available') is not True:
        reasons.extend(quality.get('flicker_issues') or ['ISF_FLICKER_UNAVAILABLE'])
    if any(number(closure.get(k)) is None for k in ('spectre_total_dbc_hz', 'modeled_total_dbc_hz')):
        reasons.append('ISF_HBNOISE_REFERENCE_UNAVAILABLE: finite measured and modeled totals required')
    if gap is None:
        reasons.append('ISF_CLOSURE_UNAVAILABLE: obtain L2 and attribute sources')
    elif gap < -.5:
        reasons.append('ISF_OVERMODELED: modeled noise exceeds total by >0.5 dB')
    elif gap > 3:
        reasons.append('ISF_GAP_EXCEEDED: G > 3 dB; attribute unmodeled sources')
    if per_device:
        for mechanism in ('flicker', 'channel_thermal'):
            error_db = number((device_closure.get(mechanism) or {}).get('error_db'))
            if error_db is None or abs(error_db) > 2:
                reasons.append('ISF_DEVICE_CLOSURE_UNQUALIFIED: {} {} requires |error| <= 2 dB'
                               .format(per_device, mechanism))
    elif error is None or abs(error) >= .5:
        reasons.append('ISF_PAIR_CLOSURE_UNQUALIFIED: pair_error unavailable or |pair_error| >= 0.5 dB')
    return dict(isf_shaping_allowed=not reasons,
                reason='; '.join(dict.fromkeys(reasons)) if reasons else (
                    'Device closure qualified and G <= 3 dB' if per_device else
                    'Pair closure qualified and G <= 3 dB; HBnoise still decides adoption'),
                gap_db=gap, pair_error_db=error,
                dominant_unmodeled_source=closure.get('dominant_unmodeled_source'))


def _l2(raw, ref, offsets):
    from ..metrics import compute_metrics, select_point
    from ..core.result_presentation import enrich_summary
    from ..core.complementary import _sum_dbc

    rows = []
    core = raw.get('complementary_core')
    devices = [d['instance'] for d in core['devices']] if core is not None else [None]
    if core is not None and (len(devices) != 2 or len(set(devices)) != 2):
        raise ValueError('L2 complementary evidence requires both selected devices')
    for offset, device in ((offset, device) for offset in offsets for device in devices):
        _, data, summary = select_point(raw, point=ref.get('point'), offset_hz=offset, device=device)
        harmonics = data.get('harmonics') or []
        # Reject malformed rows before metrics can stringify/collapse their keys.
        # Canonical bundles store k as decimal strings ("0", "1", ...), other
        # writers as int; both are normalised, every other type is rejected.
        orders = [_harmonic_order(h) for h in harmonics]
        if any(order is None for order in orders):
            raise ValueError('ISF_HARMONIC_ORDER_INVALID: k must be a nonnegative integer')
        recorded_order = (summary.get('thermal_noise') or {}).get('requested_max_harmonic')
        maximum = ref.get('max_harmonic', recorded_order if recorded_order is not None else 3)
        order_issues = _harmonic_order_issues([str(order) for order in orders], maximum)
        m = compute_metrics(raw, point=ref.get('point'), offset_hz=offset, device=device)
        m['source']['max_harmonic'] = maximum
        s = enrich_summary(summary)
        p, q = number(s.get('predicted_pair_dBc_Hz')), number(s.get('spectre_pair_dBc_Hz'))
        m['quality'] = {k:s.get(k) for k in ('numerical_quality_pass','pm_magnitude_projection_valid','hbac_secondary_validation_pass')}
        m['quality']['thermal_available'] = (s.get('thermal_noise') or {}).get('status') == 'available'
        issues = _flicker_issues(m) + order_issues
        if not harmonics or any(number(h.get(k)) is None for h in harmonics
                                for k in ('Q_real_V_rtHz', 'Q_imag_V_rtHz')):
            issues.append('ISF_HARMONIC_Q_UNAVAILABLE')
        if device:
            device_rows = m['closure']['device']
            if number(device_rows['flicker']['error_db']) is None:
                issues.append('ISF_DEVICE_FLICKER_REFERENCE_UNAVAILABLE')
            m['quality']['thermal_available'] &= number(device_rows['channel_thermal']['error_db']) is not None
        else:
            if p is None or q is None:
                issues.append('ISF_PAIR_REFERENCE_UNAVAILABLE')
            m['closure']['pair_error_db'] = p-q if p is not None and q is not None else None
            m['closure']['pair_error_scope'] = 'gm_pair_flicker; thermal validation is separate'
        m['quality'].update(flicker_available=not issues, flicker_issues=list(dict.fromkeys(issues)))
        rows.append(m)
    if core is not None:
        for index in range(0, len(rows), len(devices)):
            group = rows[index:index + len(devices)]
            if len({m['source']['core_operating_point'] for m in group}) != 1:
                raise ValueError('Complementary metrics must use the same physical operating point')
            total = _sum_dbc([number(m['closure']['modeled_total_dbc_hz']) for m in group])
            for m in group:
                reference = number(m['closure']['spectre_total_dbc_hz'])
                m['closure'].update(modeled_total_dbc_hz=total,
                                    gap_db=reference-total if reference is not None and total is not None else None)
    return rows


def _l1(raw, ref, offsets):
    if raw.get('status') == 'measured':
        m = raw['metrics']
        return [dict(level='L1', source=dict(offset_hz=f, provenance='Spectre HBnoise PM-SSB', result_path=ref.get('result_path')),
                     pn_dbc_hz=_required_pn(m, f), f0_hz=m.get('frequency_hz'), closure={},
                     warnings=raw.get('warnings', [])) for f in offsets]
    rows = _l2(raw, ref, offsets)
    if raw.get('complementary_core'):
        rows = rows[::2]
    return [dict(level='L1', source=m['source'], pn_dbc_hz=_required_total(m),
                 f0_hz=m['f0_hz'], sources=m['sources'], closure=m['closure'],
                 quality=m['quality'], warnings=m['warnings']) for m in rows]


def _required_pn(metrics, offset):
    pn = number(metrics.get('pn_'+str(int(offset))+'_dbc_hz')) if offset.is_integer() else None
    if pn is None:
        raise ValueError('Exact HBnoise offset unavailable: '+str(offset))
    return pn


def _required_total(metrics):
    pn = number(metrics['closure'].get('spectre_total_dbc_hz'))
    if pn is None:
        raise ValueError('Whole-VCO Spectre HBnoise PM-SSB reference unavailable')
    return pn


def _l0(ref, calibration):
    from ..metrics import proxy_metrics, compute_metrics
    from .waveform import read_hb, lookup_alpha
    if calibration is None:
        raise ValueError('L0 requires a family-specific L2 calibration')
    calibration = read(calibration)
    expected = calibration.get('identity')
    if expected is not None and ref.get('identity') != expected:
        raise ValueError('Calibration identity differs or is absent; recalibrate for this family/geometry')
    wave = ref.get('hb_waveform')
    if wave is None:
        wave = read_hb(ref['psf_dir'], ref.get('nodes', {}))
    alpha = ref.get('alpha_dc')
    if alpha is None:
        alpha = lookup_alpha(wave, ref.get('device_lut', calibration.get('device_lut')))
    m = proxy_metrics(wave, alpha_dc=alpha, calibration=calibration)
    # Waveform-only diagnostics retain the exact voltage convention used by L0.
    rows = [dict(wt_rad=t, Vdiff_V=v, **({'VD_V':wave['vd_v'][i]} if 'vd_v' in wave else {}))
            for i, (t, v) in enumerate(zip(wave['theta_rad'], wave['vdiff_v']))]
    diag = compute_metrics(dict(workpoint=rows, summary=[dict(f0_Hz=wave['f0_hz'])]))
    m.update(waveform=diag['waveform'], single_ended=diag.get('single_ended', {}),
             closure={}, warnings=['Approximate ranking only; no measured phase noise at L0'],
             applicability='Supplied DC LUT and transferred delta/A; validate ranking against L1')
    return m


def evaluate(design_ref, *, level='L0', calibration=None, offsets_hz=(1e5,), device=None):
    """Evaluate existing evidence or execute an explicitly workspace-bound reference.

    External Design Lab and native engines are loaded only inside the owned worker.
    Existing artifacts cost zero *new* solver calls; acquisition costs are separate.
    """
    start = time.monotonic()
    if level not in ('L0', 'L1', 'L2'):
        raise ValueError('level must be L0, L1 or L2')
    offsets = _offsets(offsets_hz)
    ref = resolve(design_ref)
    if device is not None and (level != 'L2' or ref['kind'] != 'artifact'):
        raise ValueError('Device selection requires a saved L2 result')
    if level=='L0':
        if len(offsets)!=1 or calibration is None:
            raise ValueError('L0 requires one exact offset and a family-specific L2 calibration')
        calibration=read(calibration)
        from .schema import validate
        validate(calibration, 'Calibration')
        if number(calibration.get('source',{}).get('offset_hz'))!=offsets[0]:
            raise ValueError('L0 calibration offset must match the requested offset')
        if calibration.get('identity') is not None and ref.get('identity')!=calibration['identity']:
            raise ValueError('Calibration identity differs or is absent; recalibrate for this family/geometry')
    cost = dict(spectre_calls=0, elapsed_s=0., acquisition_spectre_calls=None, reused=True,
                accounting='new Spectre process invocations; artifact acquisition excluded')
    if ref['kind'] in ('design_lab', 'native_spec', 'maestro') and not (ref.get('psf_dir') and level in ('L0','L1')):
        if ref['kind']=='design_lab' and level not in ('L0','L1','L2'):
            raise ValueError('Invalid live level')
        if 'offset_hz' in ref and _offsets([ref['offset_hz']]) != offsets:
            raise ValueError('OPTIM_OFFSET_CONFLICT: offset_hz differs from offsets_hz')
        if 'offsets_hz' in ref and _offsets(ref['offsets_hz']) != offsets:
            raise ValueError('OPTIM_OFFSET_CONFLICT: reference offsets_hz differs from requested offsets_hz')
        if not ref.get('workspace'):
            raise ValueError('Live evaluation requires an explicit owned workspace in design_ref')
        work = Path(ref['workspace']).resolve()/('optim-'+uuid.uuid4().hex)
        work.mkdir(parents=True)
        request = dict(ref=ref, level=level, offsets_hz=offsets, workspace=str(work))
        write(work/'request.json', request)
        cmd = [sys.executable, '-B', '-m', 'isf_tool.optim.worker', str(work/'request.json')]
        if level != 'L2' or ref['kind'] == 'maestro' or ref.get('backend')=='private_netlist':
            cmd = wrap_command(cmd, 'sim:slots')
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
                   PYTHONPATH=str(Path(__file__).resolve().parents[2])+os.pathsep+os.environ.get('PYTHONPATH', ''))
        with (work/'worker.log').open('w') as log:
            completed = subprocess.run(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        if not (work/'response.json').is_file():
            raise RuntimeError('Evaluator worker failed; inspect '+str(work/'worker.log'))
        response = read(work/'response.json')
        if completed.returncode or response.get('status') != 'completed':
            raise RuntimeError(response.get('error', 'Evaluator failed')+'; evidence: '+str(work))
        ref.update(response['reference'])
        ref['kind'] = 'hb_psf' if level == 'L0' else 'artifact'
        cost.update(response['cost'], reused=False, acquisition_spectre_calls=response['cost']['spectre_calls'])
        cost['receipt_dir'] = str(work)
    if level == 'L0':
        if len(offsets) != 1:
            raise ValueError('L0 calibration is offset-specific; request one exact offset')
        cal = read(calibration) if calibration is not None else None
        if cal and number(cal.get('source', {}).get('offset_hz')) != offsets[0]:
            raise ValueError('L0 calibration offset must match the requested offset')
        metrics = [_l0(ref, cal)]
        metrics[0]['source'] = dict(point=ref.get('point'), psf_dir=ref.get('psf_dir'),
                                     offset_hz=offsets[0], calibration_source=(cal or {}).get('source'))
    else:
        if level=='L1' and ref.get('psf_dir') and not ref.get('result_path'):
            from .waveform import read_pn
            metrics = read_pn(ref['psf_dir'], offsets, ref.get('nodes'))
        else:
            raw = _raw(ref)
            if device is None:
                metrics = _l1(raw, ref, offsets) if level == 'L1' else _l2(raw, ref, offsets)
            else:
                from ..metrics import select_point
                _, _, summary = select_point(raw, point=ref.get('point'), offset_hz=offsets[0], device=device)
                if not raw.get('complementary_core'):
                    raise ValueError('Device selection requires the saved two-device analysis')
                physical_ref = dict(ref, point=summary['core_operating_point_id'])
                metrics = [row for row in _l2(raw, physical_ref, offsets)
                           if row['source'].get('device_instance') == device]
                if len(metrics) != len(offsets):
                    raise ValueError('Selected device is missing at a requested offset')
        if level == 'L2':
            for metric in metrics:
                issues = list(metric['quality']['flicker_issues'])
                if not metric['quality']['thermal_available']:
                    issues.append('ISF_THERMAL_UNAVAILABLE')
                if any(number(metric['closure'].get(k)) is None
                       for k in ('spectre_total_dbc_hz', 'modeled_total_dbc_hz')):
                    issues.append('ISF_HBNOISE_REFERENCE_UNAVAILABLE')
                if issues:
                    raise ValueError('L2 requires qualified flicker + thermal; partial evidence is not L2: '
                                     + ', '.join(issues) + '; evidence: '+str(ref.get('result_path', 'inline')))
    cost['elapsed_s'] = time.monotonic()-start
    result = dict(schema=SCHEMA, api_version=VERSION, status='completed', level=level, offsets_hz=offsets,
                  metrics=metrics, cost=cost, reference=ref, signoff=False)
    result['gate'] = gate(result)
    json.dumps(result, allow_nan=False)
    from .schema import validate
    validate(result)
    if not cost['reused']:
        write(Path(cost['receipt_dir'])/'evaluation.json',result)
    return result


def calibrate(design_ref):
    """Use the selected single-offset L2 workpoint, including empirical DC LUT."""
    from ..metrics import calibrate as make_calibration, select_point
    ref = resolve(design_ref)
    offset = _offsets([ref['offset_hz']])[0] if 'offset_hz' in ref else None
    if 'offsets_hz' in ref:
        offsets = _offsets(ref['offsets_hz'])
        if len(offsets) != 1:
            raise ValueError('Calibration requires one exact offset')
        if offset is not None and offset != offsets[0]:
            raise ValueError('OPTIM_OFFSET_CONFLICT: offset_hz differs from offsets_hz')
        offset = offsets[0]
    if ref['kind'] in ('design_lab', 'native_spec', 'maestro'):
        offset = offset if offset is not None else 1e5
        evaluation = evaluate(ref, level='L2', offsets_hz=[offset])
        ref = evaluation['reference']
    raw = _raw(ref)
    c = make_calibration(raw, point=ref.get('point'), offset_hz=offset)
    _, data, _ = select_point(raw, point=ref.get('point'), offset_hz=offset)
    c.update(api_version=VERSION, identity=ref.get('identity'),
             applicability='Family/geometry-specific; LUT interpolation and dynamic transfer require L1 spot checks')
    cols = ('VG_V', 'VD_V', 'VS_V', 'VB_V', 'm_DC_A_rtHz')
    if data.get('workpoint') and all(all(number(row.get(k)) is not None for k in cols) for row in data['workpoint']):
        c['device_lut'] = dict(schema='vco.isf-dc-lut.v1', source=c['source'],
            samples=[[r['VG_V']-r['VS_V'], r['VD_V']-r['VS_V'], r['VB_V']-r['VS_V'], r['m_DC_A_rtHz']]
                     for r in data['workpoint'][:-1]], max_distance=.25,
            method='normalized inverse-distance 4-neighbor interpolation; no axis extrapolation')
    from .schema import validate
    return validate(c, 'Calibration')


def rank_knobs(metrics, recipe):
    """Mechanism-to-recipe mapping; unknown benefit remains null, never invented."""
    from ..metrics import knob_recommendations
    if metrics.get('schema') == SCHEMA:
        if len(metrics['metrics']) != 1:
            raise ValueError('Select one offset before ranking knobs')
        metrics = metrics['metrics'][0]
    recipe = read(recipe)
    recipe = recipe.get('design_recipe', recipe)
    decision = gate(metrics)
    if not decision['isf_shaping_allowed']:
        return [dict(param='source_attribution', kind='attribution', direction='inspect', potential_gain_db=None,
                     basis=decision['reason'], evidence=metrics.get('closure', {}), requires_l1=True)]
    recommendations = knob_recommendations(metrics)
    two = recipe.get('two_f', {})
    two_knobs = {'aux_per_code':['registers.aux.value'], 'ratio':two.get('knobs', []),
                 'cmr_z11':list(two.get('vars', {}))}.get(two.get('step'), [])
    mapping = {'2f_resonance':two_knobs, '2f_prebias / gate_RC / core_width':two_knobs+['core_width_scale'],
               'coherent_projection':two_knobs, 'CM_impedance / tail_CB':two_knobs+(['tail_CB'] if recipe.get('tail_CB') else []),
               '3f_tuning':[k for k in two_knobs if k in ('k_scale','aux_cap_scale')],
               'class_C_bias / vdc':[recipe.get('power_knob', {}).get('param')],
               'tail_2f':two_knobs, 'tail_attribution':['source_attribution']}
    potentials = [p for p in metrics.get('potential_db', []) if number(p.get('value')) is not None]
    out = {}
    for rec in recommendations:
        for param in mapping.get(rec['knob'], []):
            if not param or param in ('core_length', 'core_l', 'l'):
                continue
            potential = max((p['value'] for p in potentials), default=None) if rec['knob']=='class_C_bias / vdc' else None
            item = out.setdefault(param, dict(param=param, kind=rec['kind'], direction='bounded_probe',
                potential_gain_db=potential, basis=[], evidence=[], requires_l1=True, requires_loopgain=True))
            item['basis'].append(rec['advice']); item['evidence'].append(rec['evidence'])
    return sorted(out.values(), key=lambda x: (x['potential_gain_db'] is None, -(x['potential_gain_db'] or 0), x['param']))
