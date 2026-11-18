"""Small HB ASCII reader and explicitly bounded empirical DC-noise LUT."""
from pathlib import Path
import math
import re
import numpy as np


def read_ascii(path, axis='freq'):
    _, separator, body = Path(path).read_text(encoding='utf-8').partition('\nVALUE\n')
    if not separator:
        raise ValueError('Expected qualified ASCII PSF VALUE section: '+str(path))
    rows = []
    row = None
    for line in body.splitlines():
        match = re.fullmatch(r'"([^"\n]+)"\s+(.*)', line)
        if not match:
            continue
        key, value = match.groups()
        if key == axis:
            row = {axis:float(value)}
            rows.append(row)
        elif row is not None:
            try:
                row[key] = complex(*map(float, value.strip('()').split())) if value.startswith('(') else float(value)
            except ValueError:
                pass
    if not rows:
        raise ValueError('No ASCII HB data: '+str(path))
    return rows


def read_hb(psf_dir, nodes=None, samples=513):
    nodes = dict(nodes or {})
    path = Path(psf_dir)/'hb.fd.pss_hb'
    if not path.is_file():
        raise ValueError('Binary PSF requires a caller-exported hb_waveform or ASCII HB; no implicit reader guessing')
    rows = read_ascii(path)
    f0 = min(r['freq'] for r in rows if r['freq'] > 0)
    theta = np.linspace(0, 2*math.pi, samples)
    def voltage(node):
        if node == '0':
            return np.zeros(samples)
        if any(node not in r for r in rows):
            raise ValueError('HB node was not saved: '+str(node))
        # Spectre HB ASCII uses peak phasors, as does the Design Lab PN reader.
        return sum(float(complex(r[node]).real)*np.ones(samples) if r['freq']==0 else
                   np.real(complex(r[node])*np.exp(1j*round(r['freq']/f0)*theta)) for r in rows)
    vd = voltage(nodes.get('output_p', 'OUTP'))
    vn = voltage(nodes.get('output_n', 'OUTN'))
    out = dict(theta_rad=theta.tolist(), vdiff_v=(vd-vn).tolist(), vd_v=vd.tolist(), f0_hz=f0)
    if all(k in nodes for k in ('gate','drain','source','bulk')):
        vs = voltage(nodes['source'])
        out['vgs_v'] = (voltage(nodes['gate'])-vs).tolist()
        out['vds_v'] = (voltage(nodes['drain'])-vs).tolist()
        out['vbs_v'] = (voltage(nodes['bulk'])-vs).tolist()
    return out


def lookup_alpha(wave, lut):
    if not lut or lut.get('schema') != 'vco.isf-dc-lut.v1':
        raise ValueError('L0 needs alpha_dc or an explicit device_lut; voltage alone is insufficient')
    data = np.asarray(lut['samples'], dtype=float)
    if data.ndim != 2 or data.shape[1] != 4 or len(data)<4 or not np.isfinite(data).all():
        raise ValueError('DC LUT samples must be finite [VGS,VDS,VBS,alpha_dc] rows')
    # The engine's harmonic-reconstructed DC modulation can be signed.
    # Preserve that convention; it is not a raw nonnegative source PSD.
    query = np.column_stack([wave[k] for k in ('vgs_v','vds_v','vbs_v')])
    if query.ndim != 2 or query.shape[1] != 3 or not len(query) or not np.isfinite(query).all():
        raise ValueError('HB bias coordinates must be finite nonempty arrays')
    low, high = data[:,:3].min(axis=0), data[:,:3].max(axis=0)
    scale = np.maximum(high-low, 1e-3)
    tolerance = float(lut.get('voltage_tolerance_v', 1e-3))
    max_distance = float(lut.get('max_distance', .25))
    if not math.isfinite(tolerance) or tolerance < 0 or not math.isfinite(max_distance) or max_distance <= 0:
        raise ValueError('DC LUT domain controls must be finite with tolerance >= 0 and max_distance > 0')
    if np.any(query < low-tolerance) or np.any(query > high+tolerance):
        raise ValueError('HB bias is outside DC LUT bounds; acquire a wider LUT or recalibrate')
    result = []
    for sample in query:
        distances = np.linalg.norm((data[:,:3]-sample)/scale, axis=1)
        nearest = np.argsort(distances)[:4]
        if distances[nearest[0]] > max_distance:
            raise ValueError('HB bias too far from sampled LUT trajectory; recalibrate')
        if distances[nearest[0]] < 1e-10:
            result.append(float(data[nearest[0],3]))
        else:
            weights = 1/np.maximum(distances[nearest], 1e-12)**2
            result.append(float(np.dot(weights, data[nearest,3])/weights.sum()))
    return result


def read_pn(psf_dir, offsets, nodes=None):
    """Exact-frequency PM-SSB HBnoise; no interpolation or proxy substitution."""
    nodes = nodes or {}
    fd = read_ascii(Path(psf_dir)/'hb.fd.pss_hb')
    fundamental = min((r for r in fd if r['freq']>0), key=lambda r:r['freq'])
    amplitude = abs(fundamental[nodes.get('output_p','OUTP')]-fundamental[nodes.get('output_n','OUTN')])
    if amplitude<=0:
        raise ValueError('HB carrier amplitude must be positive')
    noise = read_ascii(Path(psf_dir)/'hbnoise.pm.pnoise_hbnoise', 'relative frequency')
    result = []
    for offset in offsets:
        matches = [r for r in noise if math.isclose(r['relative frequency'],offset,rel_tol=1e-9)]
        if len(matches)!=1 or not isinstance(matches[0].get('out'),(float,int)) or matches[0]['out']<=0:
            raise ValueError('Exact PM HBnoise sample unavailable: '+str(offset))
        result.append(dict(level='L1',source=dict(offset_hz=offset,provenance='Spectre HBnoise PM-SSB',psf_dir=str(psf_dir)),
                           pn_dbc_hz=20*math.log10(math.sqrt(2)*matches[0]['out']/amplitude),
                           f0_hz=fundamental['freq'],closure={},warnings=[]))
    return result
