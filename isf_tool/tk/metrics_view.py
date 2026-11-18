"""Display projection for optimization diagnostics; no solver or file access."""
from .fieldwork_tk import SERIES
from .viewmodel import engineering

LABELS = {
    'f0_hz':'Oscillation frequency', 'vpk_v':'Vdiff peak',
    'rise_max_slope_v_rad':'Max rise slope', 'fall_max_slope_v_rad':'Max fall slope',
    'rise_mean_slope_v_rad':'Mean rise slope (10–90%)', 'fall_mean_slope_v_rad':'Mean fall slope (90–10%)',
    'rise_max_slope_v_s':'Max rise slope per second', 'fall_max_slope_v_s':'Max fall slope per second',
    'rise_mean_slope_v_s':'Mean rise slope per second', 'fall_mean_slope_v_s':'Mean fall slope per second',
    'max_slope_ratio':'Max rise/fall slope ratio', 'mean_slope_ratio':'Mean rise/fall slope ratio',
    'rise_time_s':'Rise time (10–90%)', 'fall_time_s':'Fall time (90–10%)', 'duty_cycle':'Duty cycle',
    'positive_peak':'Γ positive peak', 'negative_peak':'Γ negative peak',
    'positive_fwhm_cycle':'Γ positive-lobe FWHM', 'negative_fwhm_cycle':'Γ negative-lobe FWHM',
    'positive_area':'Γ positive area', 'negative_area':'Γ negative area', 'area_ratio':'Γ positive/negative area ratio',
    'rms':'Γrms', 'gamma_crest':'Γ crest factor', 'rho_dyn':'ρ_dyn', 'rho_dc':'ρ_DC',
    'c0_eff':'c0_eff', 'c0_dc':'c0_DC', 'area_asym':'A_asym (dynamic)', 'area_asym_dc':'A_asym (DC)',
    'area_positive_dyn':'Γ×m_dyn positive area', 'area_negative_dyn':'Γ×m_dyn negative area',
    'area_positive_dc':'Γ×m_DC positive area', 'area_negative_dc':'Γ×m_DC negative area',
    'gap_db':'closure gap G', 'spectre_total_dbc_hz':'Spectre total PN', 'modeled_total_dbc_hz':'Modeled total PN',
    'dominant_unmodeled_source':'Dominant unmodeled source'
}
STATUS = {'measured':'Computed from data', 'approximate':'Approximate', 'N/A':'N/A · Data missing'}


def projection(metrics):
    m=metrics;curves=m['curves'];info=m['metric_info'];rows=[]
    for path,definition in info.items():
        value=m
        for part in path.split('.'):
            value=value[int(part)] if isinstance(value,list) else value.get(part)
        field=path.rsplit('.',1)[-1]
        label=LABELS.get(field,path)
        if path.startswith('single_ended.'):label='Single-ended VD · '+field
        if path.startswith('sources.'):label='Source share · '+m['sources'][int(path.split('.')[1])]['id']
        rows.append(dict(key='opt:'+path,label=label,value=value,unit=definition['unit'],
                         status=STATUS[definition['status']],definition=definition['definition'],
                         source=definition['source'],reason=definition['reason']))
    for i,p in enumerate(m['potential_db']):
        rows.append(dict(key='opt:potential_db.%d'%i,label='Conditional benefit · '+p['id'],value=p['value'],unit='dB',
                         status=STATUS[p['status']],definition=p['definition'],source=p['source'],reason=p['reason']))
    keys=[r['key'] for r in rows]
    phase=[x/(2*3.141592653589793) for x in curves.get('theta_rad',[])]
    def matching(*prefix):return [k for k in keys if any(k.startswith('opt:'+p) for p in prefix)]
    def series(name,field,primary,highlight,*,color=SERIES[0],side='left',style=None,dash=False):
        values=curves.get(field,[])
        return dict(id=field,name=name,points=list(zip(phase,values)),keys=[primary]*len(values),
                    highlight_keys=highlight,color=color,side=side,style=style,dash=dash)
    wavekeys=matching('waveform.','vpk','single_ended.')
    gammakeys=matching('gamma.','waveform.gamma_crest')
    dynkeys=matching('flicker.area_','flicker.rho_dyn','flicker.c0_eff')
    dckeys=matching('flicker.rho_dc','flicker.c0_dc','flicker.area_asym_dc')
    derivative=series('dVdiff/dθ (V/rad)','dv_dtheta','opt:waveform.max_slope_ratio',wavekeys,side='right',color=SERIES[1])
    panels=[dict(title='Vdiff and slope · Rise/fall ratio '+engineering(m['waveform']['max_slope_ratio']),
                 ylabel='Vdiff (V)',right_label='dV/dθ (V/rad)',series=[series('Vdiff (V)','vdiff_v','opt:vpk_v',wavekeys),derivative]),
            dict(title='Γ · positive area '+engineering(m['gamma']['positive_area'])+' / negative area '+engineering(m['gamma']['negative_area'])+' rad/C',
                 ylabel='Γ (C^-1)',series=[]),
            dict(title='Effective ISF · c0='+engineering(m['flicker']['c0_eff'])+' s^-1/√Hz · ρ_dyn='+('%.4f'%m['flicker']['rho_dyn'] if m['flicker']['rho_dyn'] is not None else 'N/A')+' / ρ_DC='+('%.4f'%m['flicker']['rho_dc'] if m['flicker']['rho_dc'] is not None else 'N/A'),
                 ylabel='Γm (s^-1/√Hz)',series=[])]
    gamma=curves.get('gamma',[])
    for i,(field,values,prefix,hkeys) in enumerate((('gamma',gamma,'gamma',gammakeys),('effective_dyn',curves.get('effective_dyn',[]),'flicker',dynkeys)),1):
        for sign,color in ((1,SERIES[0]),(-1,SERIES[1])):
            name='Positive' if sign>0 else 'Negative'
            path=('gamma.'+('positive_area' if sign>0 else 'negative_area')) if field=='gamma' else ('flicker.area_'+('positive' if sign>0 else 'negative')+'_dyn')
            s=dict(id=field+str(sign),name=name+' area '+engineering(m['gamma' if field=='gamma' else 'flicker'][path.split('.')[-1]]),
                   points=[(p,max(y,0) if sign>0 else min(y,0)) for p,y in zip(phase,values)],
                   color=color,style='fill',keys=['opt:'+path]*len(values),highlight_keys=hkeys)
            panels[i]['series'].append(s)
    panels[2]['series'].append(series('DC reference','effective_dc','opt:flicker.rho_dc',dckeys,color=SERIES[2],dash=True))
    panels[2]['mean_lines']=[dict(value=m['flicker']['c0_eff'],key='opt:flicker.c0_eff',label='c0_dyn')]
    for index,panel in enumerate(panels):
        panel.update(group='phase',xlabel='θ / 2π (cycles)',xlimits=(0,1),zero=True,
                     layout=(0,index/3,1,(index+1)/3),show_xlabel=index==2,regions=[],markers=[])
    def intervals(panel,field,label,hkeys):
        interval=curves.get(field,[])
        if not interval:return
        start,end=[p/(2*3.141592653589793) for p in interval]
        while start<0:start+=1;end+=1
        while start>=1:start-=1;end-=1
        for a,b in ((start,min(1,end)),(0,end-1)) if end>1 else ((start,end),):
            panels[panel]['regions'].append(dict(start=a,end=b,label=label,keys=hkeys))
    for edge in ('rise','fall'):
        label='10–90%' if edge=='rise' else '90–10%'
        intervals(0,edge+'_interval_rad',label,matching('waveform.'+edge,'waveform.max_slope_ratio','waveform.mean_slope_ratio'))
        ph=m['waveform'][edge+'_max_phase_rad'];value=m['waveform'][edge+'_max_slope_v_rad']
        if ph is not None and value is not None:
            panels[0]['markers'].append(dict(x=ph/(2*3.141592653589793),y=value if edge=='rise' else -value,side='right',label='↑ max' if edge=='rise' else '↓ max',key='opt:waveform.'+edge+'_max_slope_v_rad'))
    for sign in ('positive','negative'):
        intervals(1,sign+'_fwhm_rad','FWHM '+('+' if sign=='positive' else '−'),matching('gamma.'+sign+'_fwhm_cycle'))
        ph=m['gamma'][sign+'_peak_phase_rad'];value=m['gamma'][sign+'_peak']
        if ph is not None and value is not None:
            panels[1]['markers'].append(dict(x=ph/(2*3.141592653589793),y=value if sign=='positive' else -value,label='Γ+' if sign=='positive' else 'Γ−',key='opt:gamma.'+sign+'_peak'))
    note=' · '.join(p['advice'] for p in m['recommendations']) or 'Closure check passed. Use these metrics to explain simulated results and HBnoise to verify phase noise.'
    reasons=' · '.join(dict.fromkeys(m['warnings']))
    return dict(panels=panels,rows=rows,specs=[('label','Optimization metrics'),('value','Value'),('unit','Units'),('status','Evidence'),('definition','Definition'),('source','Source'),('reason','Reason for approximate / N/A')],
                note=note,reason=reasons)


def linked_panels(metrics, panels, key):
    """Keep the metric table; show a contextual chart for non-phase metrics."""
    path=key.removeprefix('opt:')
    rows=[];unit='1';title=None;selected=path
    f=metrics['flicker'];t=metrics['thermal']
    for prefix,label,values,units in (
            ('flicker.c_k.','Coherent projection c_k',f['c_k'],'1'),
            ('flicker.delta_k_deg.','Dynamic lag δ_k',f['delta_k_deg'],'deg'),
            ('flicker.A_k.','Dynamic amplitude ratio A_k',f['A_k'],'1'),
            ('thermal.band_frac.','thermal input band |m| share',t['band_frac'],'1')):
        if path.startswith(prefix):
            title=label;unit=units
            rows=[(float(k),v,'opt:'+prefix+k) for k,v in values.items() if k.replace('-','').isdigit()]
            break
    if path.startswith('closure.'):
        title='Closure · Spectre total / modeled total';unit='dBc/Hz'
        rows=[(i,metrics['closure'][k],'opt:closure.'+k) for i,k in enumerate(('spectre_total_dbc_hz','modeled_total_dbc_hz'))]
    if path.startswith('sources.') or path.startswith('potential_db.'):
        title='Spectre source shares (top 8 + selected source)';sources=metrics['sources']
        indices=list(range(min(8,len(sources))))
        if path.startswith('sources.'):
            i=int(path.split('.')[1])
            if i not in indices:indices.append(i)
        rows=[(i,sources[i]['share'],'opt:sources.%d.share'%i) for i in indices]
    if path.startswith('thermal.overlap.'):
        title='thermal overlap · proxy and N/A'
        rows=[(i,t['overlap'].get(k),'opt:thermal.overlap.'+k) for i,k in enumerate(('target_mdyn2','target_abs_id'))]
    if title is None:return panels
    context=dict(panels[0],title=title,ylabel=unit,right_label=None,group='metric-context',xlabel='Order / band / index',show_xlabel=True,
                 xlimits=None,zero=unit!='dBc/Hz',regions=[],markers=[],mean_lines=[],series=[dict(id='metric-context',name=title,unit=unit,style='bar',
                 points=[(x,y) for x,y,k in rows],keys=[k for x,y,k in rows],highlight_keys=[key])],
                 reason='See the selected row for values and provenance. Missing values appear as N/A.' if not any(y is not None for x,y,k in rows) else '')
    return [context]+panels[1:]
