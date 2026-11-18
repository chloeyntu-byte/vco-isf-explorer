"""Read verified named Maestro outputs without changing ISF calculations."""
import copy
import math


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def attach_outputs(result, points, definitions):
    result = copy.deepcopy(result)
    metadata = {d['name']:d for d in definitions}
    for summary in result.get('summary', []):
        candidates = []
        for point in points:
            if point.get('corner') != summary.get('corner'):continue
            variables=point.get('variables', {})
            matches=True
            for key,value in summary.get('operating_parameters', {}).items():
                actual=variables.get(key)
                from .spectre_topology import evaluate
                try:actual_number=evaluate(str(actual),{})
                except ValueError:actual_number=actual
                try:value_number=evaluate(str(value),{})
                except ValueError:value_number=value
                if finite(actual_number) and finite(value_number):
                    if not math.isclose(actual_number,value_number,rel_tol=1e-8,abs_tol=1e-18):matches=False
                elif actual!=value:matches=False
            if matches:candidates.append(point)
        if len(candidates)!=1:
            summary['maestro_output_status']='unknown: no unique corner/variable match'
            continue
        point=candidates[0];values={}
        for name, record in point.get('outputs', {}).items():
            if name not in metadata or not finite(record.get('value')):continue
            qualification=point.get('output_qualification',{}).get(name,{})
            if qualification.get('status')!='verified':continue
            values[name]=dict(value=record['value'],units=metadata[name].get('units'),source='Maestro RDB',
                              point_id=point.get('point_id'),corner=point.get('corner'),test=point.get('test'),history=point.get('history'))
        summary['maestro_outputs']=values
        summary['maestro_output_status']='verified' if values else 'unavailable'
        metrics=output_metrics(summary)
        if metrics.get('pn_dbc_hz') is not None and finite(summary.get('spectre_total_dBc_Hz')):
            summary['maestro_pn_reference_delta_db']=metrics['pn_dbc_hz']-summary['spectre_total_dBc_Hz']
    return result


def output_metrics(summary):
    """Exact selected offset; absent/nonfinite data never become zero."""
    values=summary.get('maestro_outputs',{})
    def value(key):
        v=values.get(key,{}).get('value')
        return v if finite(v) else None
    offset=summary.get('offset_Hz')
    pn=None
    if finite(offset):
        token=format(offset,'.15g').replace('.','p').replace('+','')
        pn=value('ISF_HBNOISE_PN_'+token+'Hz_dBc_Hz')
    return dict(frequency_hz=value('ISF_HB_F0_Hz'),pn_dbc_hz=pn,delta_db=summary.get('maestro_pn_reference_delta_db'))
