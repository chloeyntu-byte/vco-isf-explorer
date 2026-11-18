"""Optimization diagnostics, never phase-noise signoff or solver input.

All cycle moments use trapezoids on the supplied *closed* 2π grid. Scalars
remain numbers (report §2.4); metric_info[path] carries their units/provenance.
No files are read, no input objects mutated, no solver is invoked.
"""
from __future__ import annotations

import math
import numpy as np

SCHEMA = 'vco.isf-metrics.v1'
TAU = 2 * math.pi


def _number(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _ratio(a, b):
    return a / b if a is not None and b is not None and b != 0 else None


def _trap(y, x):
    return np.trapezoid(y, x) if hasattr(np, 'trapezoid') else np.trapz(y, x)


def _mean(y, x):
    return float(_trap(y, x) / TAU)


def _rms(y, x):
    return math.sqrt(max(0., _mean(y * y, x)))


def _column(rows, name):
    vals = [_number(r.get(name)) for r in rows]
    return np.asarray(vals) if vals and all(v is not None for v in vals) else None


def _grid(rows):
    x = _column(rows, 'wt_rad')
    if x is None or len(x) < 5 or np.any(np.diff(x) <= 0) or not math.isclose(x[-1]-x[0], TAU, abs_tol=1e-5):
        return None
    return x


def _differential_voltage(arrays, theta):
    """One voltage/provenance convention for metrics and L0 calibration."""
    v, vd, vdn = (arrays.get(name) for name in ('Vdiff_V', 'VD_V', 'VDN_V'))
    if v is not None:
        return v, 'measured', 'workpoint.Vdiff_V', ''
    if vd is not None and vdn is not None:
        return vd-vdn, 'measured', 'workpoint.VD_V - VDN_V', ''
    if vd is not None:
        v = vd-np.interp(((theta-theta[0]+math.pi)%TAU)+theta[0],theta,vd)
        return (v, 'approximate', 'VD(θ) - VD(θ+π)',
                'Opposite drain absent; assumes symmetric pair, cancels even harmonics')
    return None, 'N/A', 'workpoint.Vdiff_V / VD_V / VDN_V', 'Drain voltage data missing'


def derivative(y, theta):
    """Periodic spectral derivative on uniform grids, local gradient otherwise.

    No filtering: staircase HB samples can amplify derivative error. The closing
    sample is retained in integrals, excluded only from the periodic FFT.
    """
    if np.allclose(np.diff(theta), np.diff(theta)[0], rtol=1e-5, atol=1e-10):
        n = len(y)-1
        k = np.fft.fftfreq(n, 1/n)
        dy = np.fft.ifft(1j*k*np.fft.fft(y[:-1])).real
        return np.r_[dy, dy[0]]
    return np.gradient(y, theta, edge_order=2)


def _fourier(y, x, k):
    return complex(_trap(y * np.exp(-1j*k*x), x) / TAU)


def _areas(y, x):
    pos = float(_trap(np.maximum(y, 0), x))
    neg = float(-_trap(np.minimum(y, 0), x))
    return pos, neg


def _peak_interval(y, x, sign):
    """Half-height connected lobe containing the signed global peak.

    Crossings are linearly interpolated. Return unwrapped radians; the UI splits
    seam crossings. Other local lobes do not artificially broaden the peak.
    """
    z = sign*y[:-1]
    peak = int(np.argmax(z))
    if z[peak] <= 0:
        return None, None
    xx = np.r_[x[:-1]-TAU, x[:-1], x[:-1]+TAU]
    zz = np.tile(z, 3)
    p = peak+len(z); level = z[peak]/2
    a = b = p
    while a > p-len(z) and zz[a] >= level:
        a -= 1
    while b < p+len(z) and zz[b] >= level:
        b += 1
    if a == p-len(z) or b == p+len(z):
        return None, None
    left = xx[a]+(level-zz[a])/(zz[a+1]-zz[a])*(xx[a+1]-xx[a])
    right = xx[b-1]+(level-zz[b-1])/(zz[b]-zz[b-1])*(xx[b]-xx[b-1])
    return [float(left), float(right)], float(x[peak])


def _edge(y, x, rising):
    n = len(y)-1
    start = int(np.argmin(y[:-1]) if rising else np.argmax(y[:-1]))
    stop = int(np.argmax(y[:-1]) if rising else np.argmin(y[:-1]))
    count = (stop-start) % n
    if not count:
        return None
    indices = (start+np.arange(count+1)) % n
    xx = x[indices].copy()
    for i in range(1,len(xx)):
        if xx[i] <= xx[i-1]:xx[i:] += TAU
    yy = y[indices]
    lo, hi = float(np.min(y)), float(np.max(y))
    if hi-lo <= 1e-14:return None
    z = (yy-lo)/(hi-lo) if rising else (hi-yy)/(hi-lo)
    crossings = []
    for level in (.1,.9):
        candidates = [float(xx[i]+(level-z[i])/(z[i+1]-z[i])*(xx[i+1]-xx[i]))
                      for i in range(len(z)-1) if z[i] <= level < z[i+1]]
        if not candidates:return None
        crossings.append(candidates[0])
    return crossings if crossings[1] > crossings[0] else None


def select_point(result, *, point=None, offset_hz=None, device=None):
    """Select exactly one sample; ambiguity is an error, never 'latest'."""
    raw = result
    for _ in range(3):
        if 'points' in raw or 'workpoint' in raw:break
        nested = raw.get('result') or raw.get('data')
        if not isinstance(nested, dict):break
        raw = nested
    points = raw.get('points')
    if points is None:
        if 'workpoint' not in raw:raise ValueError('result contains no ISF workpoint')
        points = {str((raw.get('summary') or [{}])[0].get('point','single')):raw}
    matches = []
    for key, data in points.items():
        summary = next(iter(data.get('summary') or []), None)
        if summary is None:summary = next((s for s in raw.get('summary',[]) if s.get('point')==key),{})
        identity = summary.get('operating_point_id', key.split('@')[0])
        if device is not None and summary.get('device_instance') != device:
            continue
        if point is not None and point not in (key, identity, summary.get('corner'), summary.get('core_operating_point_id')):continue
        off = _number(summary.get('offset_Hz',summary.get('offset_hz')))
        if offset_hz is not None and (off is None or not math.isclose(off,float(offset_hz),rel_tol=1e-9)):continue
        matches.append((key, data, summary))
    if len(matches)!=1:
        raise ValueError('Select one point/offset (matched %d samples)' % len(matches))
    return matches[0]


def compute_metrics(result, *, point=None, offset_hz=None, device=None):
    """Compute v1 diagnostics for one result-owned operating point/offset."""
    key, data, raw_summary = select_point(result, point=point, offset_hz=offset_hz, device=device)
    from .core.result_presentation import enrich_summary, noise_contribution_rows
    summary = enrich_summary(raw_summary)
    out = dict(schema=SCHEMA, version=1, level='L2',
               source=dict(isf_job=result.get('job_id',result.get('job',{}).get('job_id')), point=key,
                           operating_point=summary.get('operating_point_id'), code=summary.get('operating_parameters',{}).get('code'),
                           offset_hz=_number(summary.get('offset_Hz',summary.get('offset_hz')))),
               waveform={}, gamma={}, flicker=dict(c_k={},delta_k_deg={},A_k={}),
               thermal=dict(band_frac={},overlap={}), closure={}, sources=[], potential_db=[],
               warnings=[], recommendations=[], metric_info={}, curves={})
    if summary.get('device_instance'):
        from .core.complementary import device_closure
        out['source'].update(device_instance=summary['device_instance'], device_polarity=summary['device_polarity'],
                             core_operating_point=summary['core_operating_point_id'])
        out['closure']['device'] = device_closure(summary)
    info = out['metric_info']
    def put(path, value, unit, definition, source, status='measured', reason=''):
        target=out
        parts=path.split('.')
        for part in parts[:-1]:target=target.setdefault(part,{})
        value = _number(value) if value is not None and not isinstance(value,str) else value
        target[parts[-1]]=value
        info[path]=dict(unit=unit,definition=definition,source=source,status=status if value is not None else 'N/A',
                        reason=reason if value is not None else reason or 'Required data missing or zero denominator')
    f0 = _number(summary.get('f0_Hz'))
    put('f0_hz',f0,'Hz','Oscillation frequency','summary.f0_Hz')
    rows = data.get('workpoint',[]); x = _grid(rows)
    valid = x is not None
    if not valid:out['warnings'].append('Incomplete/nonmonotonic phase grid: cycle diagnostics unavailable')
    arrays = {name:_column(rows,name) if valid else None for name in ('VD_V','Vdiff_V','VDN_V','isf_i','m_DC_A_rtHz','m_dyn_A_rtHz','ID_main_A')}
    g, dc, dyn = (arrays[n] for n in ('isf_i','m_DC_A_rtHz','m_dyn_A_rtHz'))
    if g is None and valid:g = _column(rows,'isf')
    vd = arrays['VD_V']
    v,wstatus,wsource,wreason = _differential_voltage(arrays,x)
    if wstatus == 'approximate':out['warnings'].append(wreason)
    dv = derivative(v,x) if v is not None else None
    if valid:out['curves']['theta_rad']=x.tolist()
    for name,y in (('vdiff_v',v),('dv_dtheta',dv),('gamma',g),('m_dc',dc),('m_dyn',dyn)):
        out['curves'][name]=y.tolist() if y is not None else []
    put('vpk_v',(np.max(v)-np.min(v))/2 if v is not None else None,'V','Half peak-to-peak Vdiff',wsource,wstatus,wreason)
    for label,rising in (('rise',True),('fall',False)):
        edge=_edge(v,x,rising) if v is not None else None
        peak_index=int(np.argmax(dv) if rising else np.argmin(dv)) if dv is not None else None
        maximum=float(abs(dv[peak_index])) if dv is not None else None
        duration=edge[1]-edge[0] if edge else None
        avg=.8*float(np.ptp(v))/duration if duration else None
        if edge:out['curves'][label+'_interval_rad']=edge
        put('waveform.'+label+'_max_slope_v_rad',maximum,'V/rad','Maximum '+label+' slope magnitude; periodic derivative',wsource,wstatus,wreason)
        put('waveform.'+label+'_mean_slope_v_rad',avg,'V/rad','0.8 Vpp / 10–90% phase duration on global min/max edge',wsource,wstatus,wreason)
        put('waveform.'+label+'_max_phase_rad',float(x[peak_index]) if peak_index is not None else None,'rad','Phase of maximum slope magnitude',wsource,wstatus,wreason)
        for mode,val in (('max',maximum),('mean',avg)):
            put('waveform.'+label+'_'+mode+'_slope_v_s',val*TAU*f0 if val is not None and f0 else None,'V/s','dV/dt = 2π f0 dV/dθ',wsource,wstatus,wreason)
        put('waveform.'+label+'_time_s',duration/(TAU*f0) if duration and f0 else None,'s','10–90% rising / 90–10% falling duration',wsource,wstatus,wreason)
    for mode in ('max','mean'):
        put('waveform.'+mode+'_slope_ratio',_ratio(out['waveform']['rise_'+mode+'_slope_v_rad'],out['waveform']['fall_'+mode+'_slope_v_rad']),'1','Rising/falling slope magnitude ratio',wsource,wstatus,wreason)
    put('waveform.duty_cycle',_mean((v>(np.min(v)+np.max(v))/2).astype(float),x) if v is not None else None,'cycle','Trapezoid mean of Vdiff > midpoint(Vmin,Vmax)',wsource,wstatus,wreason)
    for prefix,y,source,status,reason in (('waveform',v,wsource,wstatus,wreason),('single_ended',vd,'workpoint.VD_V','measured','Report §1.1 uses single-ended VD, not Vdiff')):
        coeffs = {k:_fourier(y,x,k) for k in (1,2,3)} if y is not None else {}
        for k in (2,3):
            ratio=_ratio(abs(coeffs[k]),abs(coeffs[1])) if coeffs else None
            phase=math.degrees(np.angle(coeffs[k])-k*np.angle(coeffs[1])) if ratio is not None and ratio>1e-8 else None
            phase=(phase+180)%360-180 if phase is not None else None
            put(prefix+'.psi%d_deg'%k,phase,'deg','arg(Vk) − k arg(V1), wrapped to [−180,180); undefined for vanishing harmonic',source,status,reason)
            put(prefix+'.v%d_over_v1'%k,ratio,'1','|Vk| / |V1|',source,status,reason)
    if g is not None:
        grms=_rms(g,x);ap,an=_areas(g,x)
    else:grms=ap=an=None
    put('gamma.rms',grms,'C^-1','sqrt(⟨ΓI²⟩), carrier in-phase ISF','workpoint.isf_i')
    put('waveform.gamma_crest',_ratio(float(np.max(np.abs(g))),grms) if g is not None else None,'1','max|ΓI| / Γrms','workpoint.isf_i')
    for label,sign,area in (('positive',1,ap),('negative',-1,an)):
        interval,phase=_peak_interval(g,x,sign) if g is not None else (None,None)
        if interval:out['curves'][label+'_fwhm_rad']=interval
        put('gamma.'+label+'_peak',max(0.,float(np.max(sign*g))) if g is not None else None,'C^-1','Signed lobe peak magnitude','workpoint.isf_i')
        put('gamma.'+label+'_peak_phase_rad',phase,'rad','Phase of signed lobe maximum','workpoint.isf_i')
        put('gamma.'+label+'_fwhm_cycle',(interval[1]-interval[0])/TAU if interval else None,'cycle','Connected half-height lobe containing signed global peak; linear crossings','workpoint.isf_i')
        put('gamma.'+label+'_area',area,'rad/C','∫ max(±ΓI,0) dθ over complete period; magnitude','workpoint.isf_i')
    put('gamma.area_ratio',_ratio(ap,an),'1','Positive / negative Γ area magnitudes','workpoint.isf_i')
    for suffix,m in (('dyn',dyn),('dc',dc)):
        product=g*m if g is not None and m is not None else None
        pos,neg=_areas(product,x) if product is not None else (None,None)
        c0=_mean(product,x) if product is not None else None
        out['curves']['effective_'+suffix]=product.tolist() if product is not None else []
        for name,val,unit,definition in (
                ('area_positive_'+suffix,pos,'rad s^-1/√Hz','∫ max(ΓI m,0) dθ'),
                ('area_negative_'+suffix,neg,'rad s^-1/√Hz','−∫ min(ΓI m,0) dθ; positive magnitude'),
                ('area_ratio_'+suffix,_ratio(pos,neg),'1','Positive / negative effective ISF area magnitudes'),
                ('c0_eff' if suffix=='dyn' else 'c0_dc',c0,'s^-1/√Hz','⟨ΓI m⟩ = signed area / 2π'),
                ('area_asym' if suffix=='dyn' else 'area_asym_dc',_ratio(pos-neg,pos+neg) if pos is not None else None,'1','(positive−negative)/(positive+negative) effective areas'),
                ('rho_'+suffix,_ratio(c0,grms*_rms(m,x)) if m is not None and grms is not None else None,'1','⟨ΓI m⟩ / (Γrms mrms)')):
            put('flicker.'+name,val,unit,definition,'workpoint.isf_i × m_'+suffix)
    harmonics=data.get('harmonics',[])
    if not harmonics:harmonics=[{'k':k} for k in range(4)]
    qs=[complex(h['Q_real_V_rtHz'],h['Q_imag_V_rtHz']) if _number(h.get('Q_real_V_rtHz')) is not None and _number(h.get('Q_imag_V_rtHz')) is not None else None for h in harmonics]
    total=sum(qs) if all(q is not None for q in qs) else None
    for h,q in zip(harmonics,qs):
        k=str(h['k'])
        put('flicker.c_k.'+k,(q*total.conjugate()).real/abs(total)**2 if total is not None and abs(total)>0 else None,'1','Re(Qk ΣQ*)/|ΣQ|²; signed coherent projection, not power share','harmonics.Q')
        put('flicker.delta_k_deg.'+k,h.get('delta_k_deg'),'deg','arg(Mdyn,k / Mdc,k)','harmonics.delta_k_deg')
        put('flicker.A_k.'+k,h.get('A_k'),'1','|Mdyn,k / Mdc,k|','harmonics.A_k')
    thermal=summary.get('thermal_noise') or {}
    trusted=thermal.get('status')=='available'
    bands={}
    for src in thermal.get('sources',[]) if trusted else []:
        for band in src.get('input_band_contributions',[]):
            n=str(abs(int(band['input_band'])))
            bands[n]=bands.get(n,0)+max(0.,band['psd_V2_Hz'])
    for n,power in sorted(bands.items()):
        put('thermal.band_frac.'+n,_ratio(power,sum(bands.values())),'1','Σsources Σ|m|=n Bm / Σall retained thermal Bm (excludes flicker)','summary.thermal_noise.sources.input_band_contributions')
    if not bands:
        put('thermal.band_frac.unavailable',None,'1','Thermal input-band power fractions','summary.thermal_noise',reason='No validated thermal budget')
    # These are target-device shape proxies, never asserted as exact O_j.
    gm2=_mean(g*g,x) if g is not None else None
    for name,shape,why in (('target_mdyn2',dyn*dyn if dyn is not None else None,'Flicker m_dyn² used as thermal PSD shape; not measured thermal α²'),
                           ('target_abs_id',np.abs(arrays['ID_main_A']) if arrays['ID_main_A'] is not None else None,'|ID_main| used as thermal PSD shape; assumes PSD ∝ |ID|')):
        ov=_ratio(_mean(g*g*shape,x),gm2*_mean(shape,x)) if g is not None and shape is not None else None
        put('thermal.overlap.'+name,ov,'1','⟨Γ²α²⟩/(⟨Γ²⟩⟨α²⟩)','workpoint',status='approximate',reason=why)
    for src in thermal.get('sources',[]) if trusted else []:
        sid=src['device'].replace('.', '/')+':'+src['mechanism']
        put('thermal.overlap.'+sid,None,'1','Source-specific O_j requires Γ_j and phase-resolved source PSD','summary.thermal_noise.sources',reason='result.json lacks source-specific Γ_j and phase-resolved PSD; stationary source shape alone does not determine Γ_j')
    # Membership follows the components actually counted in closure. A pair
    # prediction covers both explicit fn members even when no per-source PSD
    # was exported. Neither a matching name nor a Spectre reference qualifies
    # an unvalidated thermal budget.
    pair_db = _number(summary.get('predicted_pair_dBc_Hz'))
    single_db = _number(summary.get('dynamic_dBc_Hz'))
    flicker_db = pair_db if pair_db is not None else single_db
    thermal_db = _number(summary.get('thermal_dBc_Hz')) if trusted else None
    modeled_sources = {(s['device'],s['mechanism']) for s in thermal.get('sources',[])} if thermal_db is not None else set()
    identity = summary.get('flicker_pair_identity') or {}
    if pair_db is not None and identity:
        modeled_sources.update((identity[k],'fn') for k in ('target_noise_trace','peer_noise_trace'))
    elif flicker_db is not None:
        target = (summary.get('target_noise_trace') or identity.get('target_noise_trace')
                  or (raw_summary.get('flicker_pair_identity') or {}).get('target_noise_trace')
                  or (thermal.get('target_noise_trace') if trusted else None))
        if target:modeled_sources.add((target,'fn'))
    for r in noise_contribution_rows(raw_summary,noise_kind='both'):
        if r.get('kind')!='source':continue
        share=_ratio(r.get('fraction_pct'),100)
        out['sources'].append(dict(id=r['device']+':'+r['mechanism'],device=r['device'],mechanism=r['mechanism'],
                                   share=share,modeled=(r['device'],r['mechanism']) in modeled_sources))
    out['sources'].sort(key=lambda s:-(s['share'] or 0))
    for i,src in enumerate(out['sources']):
        info['sources.%d.share'%i]=dict(unit='1',definition='Source PM PSD / Spectre total PM PSD; not normalized to partial source list',source='summary.spectre_noise_contributions / spectre_total_pm_asd²',status='measured' if src['share'] is not None else 'N/A',reason='' if src['share'] is not None else 'Total/source PM PSD missing')
    total_db=_number(summary.get('spectre_total_dBc_Hz'))
    modeled=_number(summary.get('predicted_total_dBc_Hz'))
    # Preserve every available modeled component, including a valid single
    # device prediction when a pair is unavailable. Never add single + pair.
    components = [v for v in (flicker_db,thermal_db) if v is not None]
    if modeled is None and components:
        reference = max(components)
        modeled = reference+10*math.log10(sum(10**((v-reference)/10) for v in components))
    put('closure.spectre_total_dbc_hz',total_db,'dBc/Hz','Spectre total VCO PM-SSB phase noise','summary.spectre_total_dBc_Hz')
    put('closure.modeled_total_dbc_hz',modeled,'dBc/Hz','Available modeled flicker (pair, else target single device) + validated thermal in linear power; absent components excluded','enrich_summary.predicted_total_dBc_Hz / predicted_pair_dBc_Hz / dynamic_dBc_Hz / thermal_dBc_Hz')
    gap=total_db-modeled if total_db is not None and modeled is not None else None
    put('closure.gap_db',gap,'dB','Spectre total − actually modeled total','summary')
    unmodeled=next((s for s in out['sources'] if not s['modeled'] and s['share'] is not None),None)
    put('closure.dominant_unmodeled_source',unmodeled['id'] if unmodeled else None,'source','Largest unmodeled Spectre source with explicit modeled-source identity','summary.spectre_noise_contributions')
    # A conditional benefit estimate for the *identified* target channel source.
    target=raw_summary.get('target_noise_trace') or thermal.get('target_noise_trace')
    overlap=out['thermal']['overlap'].get('target_mdyn2')
    src=next((s for s in out['sources'] if s['device']==target and s['mechanism']=='id' and s['modeled']),None)
    if src and overlap and src['share'] is not None:
        gain=potential_benefit(src['share'],overlap,.5)
        out['potential_db'].append(dict(id=src['id'],value=gain,unit='dB',status='approximate',
            definition='−10 log10[1−s_j(1−O_target/O_j)]',source='Spectre share and target_mdyn2 proxy',
            reason='Conditional O_target=0.5; flicker modulation used as thermal shape; not a guaranteed PN gain',overlap_target=.5))
    peer=thermal.get('peer_noise_trace')
    peer_src=next((s for s in out['sources'] if s['device']==peer and s['mechanism']=='id' and s['modeled']),None)
    if src and peer_src and thermal.get('pair_model')=='symmetric_independent' and overlap and src['share'] is not None and peer_src['share'] is not None:
        gain=potential_benefit(src['share']+peer_src['share'],overlap,.5)
        out['potential_db'].append(dict(id='symmetric_gm_pair:id',value=gain,unit='dB',status='approximate',definition='−10 log10[1−(s_target+s_peer)(1−0.5/O_proxy)]',source='Spectre target+peer shares; target_mdyn2 proxy',reason='Symmetric independent pair assumed; same O proxy for both devices; conditional target 0.5, not a guaranteed gain',overlap_target=.5))
    if not out['potential_db']:
        out['potential_db'].append(dict(id='unavailable',value=None,unit='dB',status='N/A',definition='−10 log10[1−s_j(1−O_target/O_j)]',source='Spectre and overlap',reason='No identified modeled target channel source with share and overlap'))
    out['closure']['gate']='attribute_sources' if gap is None or gap>3 else 'shape_diagnostics'
    out['recommendations']=knob_recommendations(out)
    if gap is None or gap>3:out['warnings'].append('unmodeled_dominant_source' if gap is not None else 'closure_unavailable')
    return out


def potential_benefit(share, overlap, target):
    """Conditional report §1.2 gain, dB; no clamping of invalid shares."""
    if not 0 <= share <= 1 or overlap<=0 or target<0:return None
    factor=1-share*(1-target/overlap)
    return -10*math.log10(factor) if factor>0 else None


def knob_recommendations(metrics):
    """Explain evidence under the existing closure gate."""
    closure = metrics['closure']
    flicker = metrics['flicker']
    cards = []

    def add(key, advice, evidence, kind='attribution'):
        # Keep the recommendation envelope for clients of the v1 metrics API.
        cards.append(dict(knob=key, advice=advice, evidence=evidence, kind=kind))

    gap = closure.get('gap_db')
    if gap is None or gap > 3:
        add('source_attribution', 'Source attribution required. Inspect modeled and unmodeled HBnoise sources.',
            f"G = {gap:.3f} dB" if gap is not None else 'Closure gap unavailable', 'gate')
        dominant = closure.get('dominant_unmodeled_source')
        if dominant:
            add('inspect_source', 'Identify the circuit role of the largest unmodeled HBnoise source.', dominant)
        return cards

    rho, dc = flicker.get('rho_dyn'), flicker.get('rho_dc')
    if rho is not None and dc is not None and abs(dc) < .05 and abs(rho) > .1:
        add('dynamic_modulation', 'Dynamic modulation changes the ISF overlap relative to the DC reference.',
            f"ρ_DC={dc:.4g}, ρ_dyn={rho:.4g}, δ1={flicker['delta_k_deg'].get('1')}°")
    visible_orders = [f'k={k}: {value:.3g}' for k, value in flicker['c_k'].items()
                      if value is not None and abs(value) > .1]
    if visible_orders:
        add('coherent_projection', 'These orders contribute to the coherent flicker projection.',
            ', '.join(visible_orders))
    visible_bands = [f'|m|={n}: {value:.1%}' for n, value in metrics['thermal']['band_frac'].items()
                     if value is not None and value > .15]
    if visible_bands:
        add('thermal_bands', 'These input bands carry substantial modeled thermal power.',
            ', '.join(visible_bands))
    return cards


def calibrate(result, *, point=None, offset_hz=None):
    """δ_k/A_k calibration tied to one measured sample, not universal physics."""
    key,data,summary=select_point(result,point=point,offset_hz=offset_hz)
    rows=data.get('workpoint',[]);x=_grid(rows)
    if x is None:raise ValueError('Calibration needs a complete closed phase grid')
    arrays = {name:_column(rows,name) for name in ('Vdiff_V','VD_V','VDN_V')}
    v,status,source,reason = _differential_voltage(arrays,x)
    g=_column(rows,'isf_i')
    if g is None:g=_column(rows,'isf')
    f0=_number(summary.get('f0_Hz'))
    scale=sign=None
    if v is not None and g is not None and f0 and np.ptp(v)>0:
        gp=-derivative(v,x)*(TAU*f0)**2/(np.ptp(v)/2)**2
        sign=1 if _mean(gp*g,x)>=0 else -1
        scale=_ratio(_rms(g,x),_rms(gp,x))
    return dict(schema='vco.isf-calibration.v1',source=dict(point=key,offset_hz=summary.get('offset_Hz')),
                delta_k_deg={str(h['k']):_number(h.get('delta_k_deg')) for h in data.get('harmonics',[])},
                A_k={str(h['k']):_number(h.get('A_k')) for h in data.get('harmonics',[])},
                gamma_scale=scale,gamma_sign=sign,waveform=dict(status=status,source=source,reason=reason),limits='Family/geometry specific; L0 ranks only. Recalibrate for a changed design family.')


def proxy_metrics(hb_waveform, *, alpha_dc, calibration):
    """L0: HB derivative + transferred δ/A correction, no measured Γ required.

    hb_waveform = {theta_rad: closed grid, vdiff_v: voltage, f0_hz: frequency}.
    alpha_dc is a closed-grid quasi-static modulation in A/√Hz. Fourier
    corrections change the specified orders only; missing corrections remain 1.
    """
    x=np.asarray(hb_waveform['theta_rad'],dtype=float)
    v=np.asarray(hb_waveform['vdiff_v'],dtype=float)
    dc=np.asarray(alpha_dc,dtype=float)
    if x.ndim!=1 or v.shape!=x.shape or dc.shape!=x.shape or len(x)<5 or not all(np.all(np.isfinite(a)) for a in (x,v,dc)):
        raise ValueError('Proxy arrays must be finite, one-dimensional and equal length')
    if np.any(np.diff(x)<=0) or not math.isclose(x[-1]-x[0],TAU,abs_tol=1e-5):raise ValueError('Proxy needs a complete closed 2π grid')
    f0=float(hb_waveform['f0_hz']);vpk=float(np.ptp(v))/2
    scale=calibration.get('gamma_scale');sign=calibration.get('gamma_sign')
    if f0<=0 or vpk<=0 or scale is None or sign not in (-1,1):raise ValueError('Proxy requires f0>0, Vpk>0 and calibrated Γ scale/sign')
    gamma=-derivative(v,x)*(TAU*f0)**2/vpk**2*scale*sign
    alpha=dc.copy()
    for kstr,deg in calibration.get('delta_k_deg',{}).items():
        k=int(kstr);amp=calibration.get('A_k',{}).get(kstr)
        if deg is None or amp is None or k==0:continue
        if k >= (len(x)-1)/2:raise ValueError('Calibration harmonic exceeds HB grid Nyquist limit')
        correction=amp*np.exp(1j*math.radians(deg))
        coeff=_fourier(dc,x,k)
        alpha += 2*np.real(coeff*(correction-1)*np.exp(1j*k*x))
    # Mean modulation calibration (k=0 has no phase).
    amp0=calibration.get('A_k',{}).get('0')
    if amp0 is not None:alpha += _mean(dc,x)*(amp0-1)
    c0=_mean(gamma*alpha,x);rho=_ratio(c0,_rms(gamma,x)*_rms(alpha,x))
    return dict(schema=SCHEMA,version=1,level='L0',source=calibration.get('source'),
                gamma_hat=gamma.tolist(),alpha_hat=alpha.tolist(),theta_rad=x.tolist(),rho_hat=rho,c0_hat=c0,
                metric_info=dict(rho_hat=dict(unit='1',definition='⟨Γ̂ α̂⟩/(Γ̂rms α̂rms)',status='approximate',source='HB derivative + transferred calibration',reason=calibration.get('limits','L0 ranking only')),
                                 c0_hat=dict(unit='s^-1/√Hz',definition='⟨Γ̂ α̂⟩; calibrated amplitude',status='approximate',source='HB derivative + transferred calibration',reason='Not a phase-noise signoff value')))
