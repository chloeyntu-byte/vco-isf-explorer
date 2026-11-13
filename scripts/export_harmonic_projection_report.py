"""Read-only CSV/Markdown of stored Qk at all measured offsets (no solver)."""
import argparse
import csv
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from isf_tool.core.result_presentation import enrich_result,harmonic_projection_fractions,fit_noise_slope


def main():
    p=argparse.ArgumentParser(__doc__);p.add_argument('out',type=Path);p.add_argument('--result',type=Path);a=p.parse_args()
    from isf_tool.example_data import result as example_result
    source=enrich_result(json.loads(a.result.read_text()) if a.result else example_result());a.out.mkdir(parents=True,exist_ok=True)
    summaries=source['summary'];records=[];text=['# Qk projection report','',
        'Default input: synthetic ideal-LC example. Single-device equivalents; pair adds 3.0103 dB.',
        'k 是 Γ_k·M_k 配对阶，全部属于 c0_eff，不是输入频带。占比为复数投影，正负相加为 100%。',
        '斜率按最低两个十倍频程的有效原始点做 OLS；此表为导出诊断，UI 不绘制逐 k 扫频曲线。','']
    for identity in sorted({s['operating_point_id'] for s in summaries}):
        rows=sorted([s for s in summaries if s['operating_point_id']==identity],key=lambda s:s['offset_Hz'])
        projected=[{r['k']:r for r in harmonic_projection_fractions(source['points'][s['point']]['harmonics'])} for s in rows]
        orders=sorted({k for data in projected for k in data})
        text+=['## '+identity,'','单阶等效相噪 (dBc/Hz)：','',
               '| k | '+' | '.join('{:g} Hz'.format(s['offset_Hz']) for s in rows)+' | slope (dB/dec) |',
               '| --- | '+' | '.join(['---']*(len(rows)+1))+' |']
        for k in orders:
            values=[data.get(k,{}).get('harmonic_equivalent_dBc_Hz') for data in projected]
            slope=fit_noise_slope([(s['offset_Hz'],v) for s,v in zip(rows,values)])
            text+=['| '+str(k)+' | '+' | '.join('{:.6f}'.format(v) if v is not None else 'N/A' for v in values)+' | '+('{:.6f}'.format(slope) if slope is not None else 'N/A')+' |']
            for summary,data,value in zip(rows,projected,values):
                harmonic=data.get(k,{})
                records.append(dict(operating_point=identity,k=k,offset_Hz=summary['offset_Hz'],single_dBc_Hz=value,
                    pair_dBc_Hz=harmonic.get('pair_harmonic_equivalent_dBc_Hz'),slope_dB_dec=slope,
                    projection_fraction=harmonic.get('projection_fraction'),cancellation=harmonic.get('cancellation')))
        text+=['','投影占比 (%)：','', '| k | '+' | '.join('{:g} Hz'.format(s['offset_Hz']) for s in rows)+' |',
               '| --- | '+' | '.join(['---']*len(rows))+' |']
        for k in orders:
            values=[d.get(k,{}).get('projection_fraction') for d in projected]
            text+=['| '+str(k)+' | '+' | '.join('{:.9f}'.format(v*100) if v is not None else 'N/A' for v in values)+' |']
        text+=['| Σ | '+' | '.join('{:.9f}'.format(sum(r['projection_fraction'] for r in d.values())*100) for d in projected)+' |','']
    (a.out/'projection-tables.md').write_text('\n'.join(text)+'\n')
    with (a.out/'projection-harmonics.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(records[0]));writer.writeheader();writer.writerows(records)
    print(len(records),'rows; projection max sum error',max(abs(sum(r['projection_fraction'] for r in records if r['operating_point']==s['operating_point_id'] and r['offset_Hz']==s['offset_Hz'])-1) for s in summaries))

if __name__=='__main__':main()
