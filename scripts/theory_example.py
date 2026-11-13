"""Reproduce the synthetic identities in ISF_THEORY.md (no simulator)."""
from pathlib import Path
import sys
import math
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from isf_tool.example_data import OFFSETS_HZ, CORNER_HZ
from isf_tool.core.result_presentation import harmonic_projection_fractions, fit_noise_slope


def table():
    q = (2., -.5, .25)
    fractions = harmonic_projection_fractions([dict(k=k,Q_real_V_rtHz=v,Q_imag_V_rtHz=0.) for k,v in enumerate(q)])
    assert math.isclose(sum(r['projection_fraction'] for r in fractions),1.)
    lines = ['| k | projection (%) | slope (dB/dec) |','|---|---|---|']
    for row in fractions:
        powers = [(f, 10*math.log10(q[row['k']]**2*(1e3/f)**3)) for f in OFFSETS_HZ]
        lines.append('| %d | %.6f | %.6f |' % (row['k'],100*row['projection_fraction'],fit_noise_slope(powers)))
    lines += ['', 'dc − dynamic = %.6f dB (dynamic modulation = 2 × DC).' % (20*math.log10(.5)), '',
              '| offset (Hz) | n=0 power | n=1 power | n=2 power | sum power |', '|---|---|---|---|---|']
    for f in OFFSETS_HZ:
        # A/f^3 + B/f^2 transitions at the fictional example source corner.
        white = 1e-10*(1e4/f)**2
        bands = [white*(1+CORNER_HZ/f),white/2,white/3]
        total = sum(bands)
        assert math.isclose(sum(b/total for b in bands),1.)
        lines.append('| %g | %.6e | %.6e | %.6e | %.6e |' % (f,*bands,total))
    lines += ['', 'n=0 asymptotic slopes: −30 dB/dec below the corner, −20 dB/dec above it.',
              'These are analytic synthetic identities, not validation of any physical circuit.']
    return '\n'.join(lines)


if __name__ == '__main__':
    print(table())
