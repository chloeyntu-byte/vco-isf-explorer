"""Shared, hash-bound gm/output selector for Tk and future source adapters.

SelectionRequest uses the public JSON schema: ``roles`` and ``candidates``;
no undocumented ``role/current/required`` top-level fields are emitted.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .errors import ValidationError
from .spectre_topology import SpectreTopology, canonical_name

ROLES = ('gm.target', 'output.p', 'output.n')
COMPLEMENTARY_ROLE = 'gm.complementary'


def core_mode(selection):
    if not isinstance(selection, dict):
        raise ValidationError('Two-device analysis requires a second device object')
    mode = selection.get('mode', 'complementary')
    if mode not in ('complementary', 'asymmetric_pair'):
        raise ValidationError('Unknown two-device analysis mode: ' + str(mode))
    return mode


def core_targets(primary, primary_peer, complementary):
    mode = core_mode(complementary)
    devices = [(primary, primary_peer),
               (complementary.get('target_instance'), complementary.get('peer_instance'))]
    if mode == 'asymmetric_pair' and any(peer is not None for _, peer in devices):
        raise ValidationError('Asymmetric pair analysis accepts two explicit devices without symmetric peer estimates')
    paths = []
    for target, peer in devices:
        if not isinstance(target, str) or not target.strip():
            raise ValidationError('Select both devices explicitly for the complementary core')
        for value in (target, peer):
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValidationError('Device and peer selections must be nonempty occurrence paths')
            paths.append(canonical_name(value.strip()))
    if any(a == b or a.startswith(b + '.') or b.startswith(a + '.')
           for i, a in enumerate(paths) for b in paths[:i]):
        raise ValidationError('Complementary devices and their peers must be distinct independent occurrences')
    return [(canonical_name(target.strip()), canonical_name(peer.strip()) if peer else None)
            for target, peer in devices]


def validate_core_polarities(polarities, mode):
    if mode == 'asymmetric_pair':
        if len(polarities) != 2 or len(set(polarities)) != 1 or polarities[0] not in ('nmos', 'pmos'):
            raise ValidationError('Asymmetric pair analysis requires two NMOS or two PMOS devices')
    elif set(polarities) != {'nmos', 'pmos'}:
        raise ValidationError('The complementary selections must resolve to one NMOS and one PMOS')


def validate_core_targets(tree, targets, *, mode='complementary'):
    polarities = []
    for target, peer in targets:
        polarity = tree.mos_identity(tree.resolve_instance(target)).get('polarity')
        polarities.append(polarity)
        if peer and tree.mos_identity(tree.resolve_instance(peer)).get('polarity') != polarity:
            raise ValidationError('A symmetric peer must have the same polarity as its selected device')
    validate_core_polarities(polarities, mode)
    return polarities


def spec_hash(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _tree(spec, provider=None):
    if isinstance(provider, SpectreTopology): return provider
    if callable(provider): return provider(spec)
    if provider is not None: return SpectreTopology(provider)
    source = spec.get('source', {})
    files = source.get('files', [])
    path = source.get('entry') or next((f.get('path') for f in files if f.get('path')), None)
    if not path: raise ValidationError('component discovery requires a bound netlist source/provider')
    return SpectreTopology(Path(source.get('cwd', '.')) / path)


def _device(row):
    identity = row['mos']
    order = identity['terminal_order']
    return dict(id='device:' + row['name'], name=row['name'], display_name=row['name'],
                source=row['source'], evidence=identity['evidence'],
                details=dict(row, terminals={role: dict(formal=identity['formal_pins'][order[index]],
                    local_net=row['local_terminals'][index], resolved_net=row['terminals'][index])
                    for index, role in enumerate('DGSB')}))


def discover_components(spec, *, provider=None):
    tree = _tree(spec, provider)
    catalog = tree.catalog()
    return catalog_from_topology(spec, catalog, source=tree.source, dependencies=tree.file_hashes)


def catalog_from_topology(spec, catalog, *, source='', dependencies=None):
    devices = [_device(row) for row in catalog['devices'] if row.get('mos') and not row.get('ambiguous')]
    nets = [dict(id='net:' + net, name=net, display_name=net, source=source,
                 evidence=['resolved netlist connectivity']) for net in catalog['nets']]
    result = dict(schema='isf.component-catalog.v1', spec_hash=spec_hash(spec),
                  nets=nets, ports=[], devices=devices)
    result['catalog_hash'] = spec_hash(dict(catalog=result, dependencies=dependencies or {}))
    return result


def selection_request(spec, *, roles=None, provider=None, catalog=None):
    selection = spec.get('gm', {}).get('complementary')
    available = ROLES + (COMPLEMENTARY_ROLE,) if selection is not None else ROLES
    roles = available if roles is None else roles
    if not roles or any(r not in available for r in roles): raise ValidationError('unknown/empty selector role')
    catalog = catalog or discover_components(spec, provider=provider)
    if catalog['spec_hash'] != spec_hash(spec): raise ValidationError('stale component catalog: spec changed')
    candidates = []
    for kind, rows in (('device', catalog['devices']), ('net', catalog['nets'])):
        for row in rows:
            candidates.append(dict(id=row['id'], kind=kind, name=row['name'], evidence=row['evidence'],
                                   details=copy.deepcopy(row.get('details', {}))))
    request = dict(schema='isf.selection-request.v1', spec_hash=catalog['spec_hash'],
                   catalog_hash=catalog['catalog_hash'], roles=list(roles), candidates=candidates)
    if selection is not None and core_mode(selection) == 'asymmetric_pair':
        request['core_mode'] = 'asymmetric_pair'
    return request


def resolve_selection(request, choices):
    if request.get('schema') != 'isf.selection-request.v1': raise ValidationError('invalid selection request schema')
    if not choices or any(role not in request['roles'] for role in choices): raise ValidationError('invalid selector choices')
    result = dict(schema='isf.selection.v1', spec_hash=request['spec_hash'],
                  catalog_hash=request['catalog_hash'], choices={})
    for role, value in choices.items():
        kind = 'device' if role in ('gm.target', COMPLEMENTARY_ROLE) else 'net'
        matches = [c for c in request['candidates'] if c['kind'] == kind and
                   (c['id'] == value or canonical_name(c['name']) == canonical_name(value))]
        if len(matches) != 1: raise ValidationError('selection does not resolve uniquely: ' + role + '=' + str(value))
        row = matches[0]
        result['choices'][role] = row['id']
        if kind == 'device':
            d = row['details']
            key = 'gm' if role == 'gm.target' else 'complementary_gm'
            result[key] = dict(target_instance=row['name'], peer_instances=[],
                terminals=copy.deepcopy(d['terminals']), local_name=d['local_name'],
                owner_definition=d['scope'], definition_file=d['source'], model=d['model'],
                parameters=copy.deepcopy(d['effective_mos_parameters']),
                parameter_sources=copy.deepcopy(d['mos_parameter_sources']), terminal_evidence=row['evidence'],
                noise_scope='selected_device', symmetric_pair_confirmed=False)
    if 'complementary_gm' in result:
        primary = result.get('gm')
        if not primary:
            raise ValidationError('Select both devices explicitly for the complementary core')
        rows = {c['id']: c for c in request['candidates']}
        polarities = [rows[result['choices'][role]]['details']['mos'].get('polarity')
                      for role in ('gm.target', COMPLEMENTARY_ROLE)]
        mode = core_mode({'mode': request.get('core_mode', 'complementary')})
        validate_core_polarities(polarities, mode)
        core_targets(primary['target_instance'], None, result['complementary_gm'])
        primary['polarity'], result['complementary_gm']['polarity'] = polarities
    if result['choices'].get('output.p') == result['choices'].get('output.n') and 'output.p' in result['choices']:
        raise ValidationError('output p/n must differ')
    return result


def apply_selection(spec, selection, *, provider=None):
    if selection.get('schema') != 'isf.selection.v1' or spec_hash(spec) != selection.get('spec_hash'):
        raise ValidationError('stale selection: spec changed')
    catalog = discover_components(spec, provider=provider)
    if selection.get('catalog_hash') != catalog['catalog_hash']:
        raise ValidationError('stale selection: netlist/model dependencies changed')
    # Re-resolve ids and terminals; clients cannot forge a gm binding.
    validated = resolve_selection(selection_request(spec, catalog=catalog), selection['choices'])
    if any(selection.get(key) != validated.get(key) for key in ('gm', 'complementary_gm')):
        raise ValidationError('selection terminal binding was modified')
    result = copy.deepcopy(spec)
    if 'gm' in validated:
        result['gm'] = validated['gm']
        if 'complementary_gm' in validated:
            result['gm']['complementary'] = validated['complementary_gm']
            prior = spec['gm']['complementary']
            if 'mode' in prior:
                result['gm']['complementary']['mode'] = core_mode(prior)
        elif spec.get('gm', {}).get('complementary') is not None:
            result['gm']['complementary'] = copy.deepcopy(spec['gm']['complementary'])
    for role, candidate in validated['choices'].items():
        if role.startswith('output.'):
            result.setdefault('output', {})[{'output.p':'positive','output.n':'negative'}[role]] = candidate[len('net:'):]
    output = result.get('output', {})
    if output.get('positive') and output.get('positive') == output.get('negative'):
        raise ValidationError('output p/n must differ')
    return result
