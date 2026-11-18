"""Reviewable occurrence-local instrumentation; no source files are modified."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .errors import ValidationError
from .spectre_topology import SpectreTopology, Definition, canonical_name


def plan_instance_instrumentation(tree, device_binding, output_binding=None, *, noise_port=None, names=None):
    row = tree.resolve_instance(device_binding if isinstance(device_binding, str) else device_binding['name'])
    identity = tree.mos_identity(row)
    names = dict(names or {})
    defaults = dict(probe='V_ISF_DRAIN', node='ISF_DRAIN', injection='I_ISF_INJECT')
    defaults.update(names)
    names = defaults
    statements = [tree.statement_for(tree.resolve_instance(p)) for p in row['ancestors']] + [tree.statement_for(row)]
    scope = statements[-1].scope
    occupied = {i.name for i in scope.instances} | {n for i in scope.instances for n in i.pins}
    if len(set(names.values())) != len(names) or set(names.values()) & occupied:
        raise ValidationError('private instrumentation name collision in ' + row['name'])
    outputs = output_binding or {}
    if outputs:
        p, n = canonical_name(outputs.get('p')), canonical_name(outputs.get('n'))
        if not p or not n or p == n:
            raise ValidationError('output p/n must be two distinct nets')
        if not {p, n} <= set(tree.catalog()['nets']):
            raise ValidationError('selected output net not in topology')
    clones = []
    for index, item in enumerate(statements[:-1]):
        definition = tree.lookup(item.model, item.scope)
        if not isinstance(definition, Definition): raise ValidationError('ancestor is not a subckt: ' + item.name)
        digest = hashlib.sha256((row['name'] + ':' + str(index)).encode()).hexdigest()[:10]
        clone = definition.name + '__isf_' + digest
        if tree.lookup(clone, definition.parent): raise ValidationError('private master collision: ' + clone)
        clones.append(dict(original=definition.name, private=clone,
                           occurrence=row['ancestors'][index], span=definition.header.span()))
    return dict(schema='isf.patch-plan.v1', source=tree.source, tree_hash=tree.content_hash(),
                target=row['name'], span=row['span'], mos=identity, names=names,
                output=outputs, noise_port=noise_port or 'external_drain_to_source',
                clones=clones, source_hashes=dict(tree.file_hashes))


def _instance(item, *, name=None, model=None, pins=None, tail=None):
    return '{} ({}) {}{}'.format(name or item.name, ' '.join(item.pins if pins is None else pins),
                                  model or item.model, (' ' + (item.tail.strip() if tail is None else tail)) if (item.tail.strip() if tail is None else tail) else '')


def _render(tree, plan):
    target = tree.resolve_instance(plan['target'])
    selected_rows = [tree.resolve_instance(n) for n in target['ancestors']] + [target]
    selected = [tree.statement_for(r) for r in selected_rows]
    clone_defs = [tree.lookup(i.model, i.scope) for i in selected[:-1]]
    clone_names = [c['private'] for c in plan['clones']]
    names = plan['names']
    order = plan['mos']['terminal_order']

    def render_scope(scope, active_index=None, renamed=None):
        lines = []
        if scope is not tree.root:
            # Keep inline and the original header grammar/attributes. Only a
            # cloned master's declaration name changes; untouched definitions
            # retain their simulator alias and contributor-path semantics.
            header = re.sub(r'(?i)^((?:inline\s+)?subckt\s+)[^\s(]+',
                            lambda m: m.group(1) + (renamed or scope.name), scope.header.text)
            lines.append(header)
        for item in scope.body:
            if not tree.is_local(item.source) and scope is tree.root:
                continue
            if item.kind == 'ends': continue
            if item.kind == 'subckt':
                definition = scope.definitions[item.name]
                lines.append(render_scope(definition))
                continue
            if item.kind == 'include':
                # Included body statements are already in this lexical scope.
                # Emit them once, including files outside the circuit directory.
                if scope is not tree.root or tree.is_local(item.include_path): continue
                lines.append('include ' + json.dumps(item.include_path) + (' section=' + item.parameters['section'] if item.parameters.get('section') else ''))
                continue
            if item.kind == 'ahdl_include':
                lines.append('ahdl_include ' + json.dumps(item.include_path))
                continue
            chosen = active_index is not None and item is selected[active_index]
            if chosen and active_index == len(selected) - 1:
                # Split an iterated statement only at the chosen occurrence.
                for local in tree._names(item.name):
                    if local != selected_rows[-1]['local_name']:
                        lines.append(_instance(item, name=local, pins=tree.instance_pins(item,local)))
                pins = tree.instance_pins(item,selected_rows[-1]['local_name'])
                d, s = pins[order[0]], pins[order[2]]
                pins[order[0]] = names['node']
                lines.extend(['{} ({} {}) vsource dc=0 type=dc'.format(names['probe'], d, names['node']),
                              '{} ({} {}) isource dc=0 type=dc'.format(names['injection'], d, s),
                              _instance(item, name=selected_rows[-1]['local_name'], pins=pins)])
            elif chosen:
                for local in tree._names(item.name):
                    lines.append(_instance(item, name=local, pins=tree.instance_pins(item,local), model=clone_names[active_index] if local == selected_rows[active_index]['local_name'] else item.model))
            else:
                lines.append(item.text)
        # Definitions are cloned in their original lexical owner. For global
        # definitions this happens at root; nested ones travel with the clone.
        for index, definition in enumerate(clone_defs):
            if definition.parent is scope:
                lines.append(render_scope(definition, index + 1, clone_names[index]))
        if scope is not tree.root: lines.append('ends ' + (renamed or scope.name))
        return '\n'.join(lines)
    return render_scope(tree.root, 0) + '\n'


def materialize_patch(plan, source_tree, destination):
    tree = source_tree if isinstance(source_tree, SpectreTopology) else SpectreTopology(source_tree)
    if tree.content_hash() != plan['tree_hash']:
        raise ValidationError('stale patch plan: source dependency changed')
    for source, digest in plan['source_hashes'].items():
        if source == '<text>' or source == tree.source and tree.source_override: continue
        if not Path(source).is_file() or hashlib.sha256(Path(source).read_bytes()).hexdigest() != digest:
            raise ValidationError('stale patch plan: source dependency changed: ' + source)
    destination = Path(destination)
    if destination.resolve() in [Path(p).resolve() for p in tree.files]:
        raise ValidationError('private destination may not overwrite a source file')
    text = _render(tree, plan)
    # Validate before writing. A clone may affect only the chosen occurrence.
    private = SpectreTopology(destination, text=text)
    devices = private.catalog()['devices']
    added = [r['name'] for r in devices if r['model'].lower() == 'isource' and r['local_name'] == plan['names']['injection']]
    prefix = plan['target'].rsplit('.', 1)[0] + '.' if '.' in plan['target'] else ''
    before = {r['name'] for r in tree.catalog()['devices'] if r['model'].lower() == 'isource' and r['local_name'] == plan['names']['injection']}
    added = [name for name in added if name not in before]
    if added != [prefix + plan['names']['injection']]:
        raise ValidationError('instrumentation escaped selected occurrence: ' + repr(added))
    from .private_validation import write_private_netlist, validate_main_copy
    write_private_netlist(destination, text, sources=tree.files, candidate=private,
                          validate=lambda copied: validate_main_copy(tree, copied, plan))
    return dict(schema='isf.patch-manifest.v1', destination=str(destination.resolve()),
                sha256=hashlib.sha256(text.encode()).hexdigest(), target=plan['target'],
                clones=plan['clones'], source_hashes=plan['source_hashes'],
                instance_map={plan['target']:plan['target']}, injection=added[0])


def instrument_model(tree, row, generated_name, injection_name):
    """Clone the unique MOS model chain and inject at the true primitive D/S."""
    from .spectre_topology import Statement
    identity = tree.mos_identity(row)
    item = tree.statement_for(row)
    definition = tree.lookup(item.model, item.scope)
    if not isinstance(definition, Definition):
        params = ' '.join(k + '=' + str(v) for k,v in row['parameters'].items())
        # Parameters belong to the new wrapper, so the primitive uses the same
        # evaluated geometry supplied by the fixture instance.
        declarations = 'parameters ' + params if params else ''
        passthrough = ' '.join(k + '=' + k for k in row['parameters'])
        model_line = ''  # Original model declarations are retained by the fixture template.
        return '\n'.join([model_line, 'subckt {} (d g s b)'.format(generated_name), declarations,
            '{} (d s) isource dc=0 type=dc pacmag=1 pacphase=0'.format(injection_name),
            'm0 (d g s b) {} {}'.format(item.model, passthrough), 'ends ' + generated_name])
    path = identity['intrinsic_path'].split('.')
    blocks = []
    def clone(block, depth, name):
        selected = next((i for i in block.instances if i.name == path[depth]), None)
        if selected is None: raise ValidationError('model primitive path changed')
        child = tree.lookup(selected.model, selected.scope)
        # Build from the definition's exact text/spans, retaining lexical model
        # declarations, parameters and passive/access networks.
        edits = [(block.header.source, block.header.start, block.header.end,
                  'subckt {} ({}){}\n'.format(name, ' '.join(block.ports),
                    ' parameters ' + ' '.join(k + '=' + v for k,v in block.header.parameters.items()) if block.header.parameters else '')),
                 (block.end.source, block.end.start, block.end.end, 'ends ' + name + '\n')]
        if depth == 0:
            context = tree.model_context(row)
            if context:
                position = block.end.start
                edits.append((block.end.source, position, position, context + '\n'))
        begin, end = selected.start, selected.end
        if isinstance(child, Definition):
            private = generated_name + '__model_' + str(depth + 1)
            nested = clone(child, depth + 1, private)
            if child.parent is not tree.root:
                # Lexical children and siblings stay below the copied context;
                # lifting them to root would lose enclosing models/parameters.
                edits.append((block.end.source, block.end.start, block.end.start, nested + '\n'))
            else: blocks.append(nested)
            replacement = _instance(selected, model=private) + '\n'
        else:
            if depth != len(path) - 1: raise ValidationError('model path stops before primitive')
            replacement = '{} ({} {}) isource dc=0 type=dc pacmag=1 pacphase=0\n{}\n'.format(injection_name, selected.pins[0], selected.pins[2], selected.text)
        edits.append((selected.source,begin,end,replacement))
        return tree.relocated_definition_text(block, edits)
    top = clone(definition, 0, generated_name)
    return '\n'.join(blocks + [top])
