"""Acquire and project the selected MOS flicker source's physical transfer."""

import re

from .noise_sources import noise_sample
from .thermal_execution import _read
from .thermal_ports import parse_terminal_map, physical_noise_port, port_transfer
from ..thermal import project_am_pm


def add_flicker_analyses(text, output, offset, order):
    """Save complete nodal HBXF for both output sidebands on the same HB orbit."""
    from .maestro_pair import _root_statements
    from .thermal_execution import _analysis

    statements = [(start, end, statement, *_analysis(statement))
                  for start, end, statement in _root_statements(text)]
    names = {'isf_fn_upper', 'isf_fn_lower', 'isf_fn_nodes', 'isf_fn_save'}
    if any(name in names for _, _, _, name, _ in statements):
        raise ValueError('Private flicker analysis name collision')
    hb_statements = [(start, end, statement) for start, end, statement, _, kind in statements
                     if kind == 'hb']
    if len(hb_statements) != 1:
        raise ValueError('Flicker transfer requires one selected HB analysis')
    pair = output['negative'] + ' ' + output['positive']
    additions = []
    for name, frequency in (('isf_fn_upper', offset), ('isf_fn_lower', -offset)):
        additions.append(
            f'{name} ({pair}) hbxf sweeptype=relative relharmvec=[1] values=[{frequency:.16e}] '
            f'maxsideband={order + 1} freqaxis=out stimuli=nodes_and_terminals annotate=status\n')
    # Other native analyses can invalidate the HB state. Keep both XF calls
    # next to their periodic solution.
    start, end, hb = hb_statements[0]
    edits = [(start, end, hb.rstrip() + '\n' + ''.join(additions))]
    for start, end, statement, _, kind in statements:
        if kind == 'options':
            updated, count = re.subn(r'(?<![\w.])save\s*=\s*(?:"[^"]*"|[A-Za-z_]\w*)',
                                     'save=all', statement)
            if count:
                edits.append((start, end, updated))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    if len(edits) == 1:
        text += '\nisf_fn_save options save=all\n'
    return text + 'isf_fn_nodes info what=nodes where=file file="../isf_fn_nodes.txt"\n'


def read_flicker_transfer(engine, cfg, psf, device, order, carrier):
    """Read complete signed bands and record the resolved source port."""
    f0 = engine.exact_hb_fundamental(cfg, psf)
    offset = float(cfg['offset_hz'])
    if not 0 < offset < f0:
        raise ValueError('Flicker PM projection requires an offset below the carrier')
    terminal_map = parse_terminal_map((psf.parent / 'isf_fn_nodes.txt').read_text())
    upper, lower = {}, {}
    bands = range(-order, order + 1)
    for k in bands:
        u = _read(engine, cfg, psf / f'isf_fn_upper.{k - 1}.pxf_hbxf')
        l = _read(engine, cfg, psf / f'isf_fn_lower.{-k - 1}.pxf_hbxf')
        upper[k] = noise_sample(u, f0 + offset, relative_tol=1e-10)['values']
        lower[k] = noise_sample(l, f0 - offset, relative_tol=1e-10)['values']
    hb = _read(engine, cfg, psf / 'hb.fd.pss_hb')
    hb_traces = noise_sample(hb, f0, relative_tol=1e-10)['values']
    port, evidence = flicker_port(device, terminal_map, [*upper.values(), *lower.values()], hb_traces)
    hp = {k: port_transfer(upper[k], port) for k in bands}
    hn = {k: port_transfer(lower[k], port).conjugate() for k in bands}
    projected = project_am_pm(hp, hn, carrier)
    evidence.update(device=device, port=port, projection=projected['contract']['convention'])
    # Keep the exported transfer in the carrier frame used by the ISF plots.
    rotation = carrier / abs(carrier)
    return {k: h * rotation for k, h in projected['pm_transfer'].items()}, evidence


def flicker_port(device, terminal_map, sidebands, hb_traces):
    row = terminal_map.get(device)
    if not row or row['master'].lower() not in ('bsim3v3', 'bsim4'):
        raise ValueError('Flicker transfer requires selected BSIM terminal evidence: ' + device)
    available = set.intersection(*(set(values) for values in sidebands))
    present = set.union(*(set(values) for values in sidebands))
    intrinsic = {device + ':int_d', device + ':int_s'}
    missing = intrinsic - present
    if (intrinsic & present) - available:
        raise ValueError('Incomplete intrinsic flicker sideband evidence for ' + device)
    if missing.intersection(hb_traces):
        raise ValueError('An intrinsic node is present in HB but missing from the flicker transfer')
    if not missing:
        return physical_noise_port(device, 'id', terminal_map, available), {'mode': 'intrinsic_ds'}
    terminals = row['terminals']
    positive, negative = terminals['d'], terminals['s']
    if {positive, negative} - available - {'0'}:
        raise ValueError('External flicker fallback has incomplete D/S transfer evidence')
    return (
        dict(kind='parallel_current', positive=positive, negative=negative, transfer_units='V/A',
             source_units='A^2/Hz', master=row['master'].lower()),
        dict(mode='external_ds_fallback', absent_intrinsic_traces=sorted(missing),
             notice='External D/S used because the listed intrinsic nodes are absent from saved HB and both complete nodal HBXF families.'),
    )
