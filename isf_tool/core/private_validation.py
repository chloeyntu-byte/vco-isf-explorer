"""Fail-closed validation shared by private main, DC and replica writers."""
from __future__ import annotations

import math
import hashlib
import re
from pathlib import Path

from .errors import ValidationError
from .spectre_topology import SpectreTopology, Statement


def equivalent(a, b):
    if isinstance(a, (float, int)) and isinstance(b, (float, int)):
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-30)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equivalent(a[k], b[k]) for k in a)
    return a == b


def occurrence_rows(tree):
    rows = tree.occurrences(external=True)
    result = {r['name']: r for r in rows}
    if len(result) != len(rows):
        raise ValidationError('duplicate private occurrence identity')
    return result


def _parameter_dependencies(expressions, environment):
    """Follow unresolved expressions as well as numerically resolved defaults."""
    pending = list(re.findall(r'(?<![\w])[^\W\d]\w*', expressions))
    dependencies = {}
    while pending:
        key = pending.pop()
        if key in dependencies or key not in environment:
            continue
        value = environment[key]
        dependencies[key] = value
        if isinstance(value, str):
            pending.extend(re.findall(r'(?<![\w])[^\W\d]\w*', value))
    return dependencies


def assert_same_model_declaration(before, row, after, copied, *, check_instance_parameters=True):
    """Retain declarations and their occurrence-specific parameter dependencies."""
    def declaration(tree, occurrence):
        item = tree.statement_for(occurrence)
        model = tree.lookup(item.model, item.scope)
        if not isinstance(model, Statement):
            return None, {}
        # A root model sees root parameters. A lexical model sees the owning
        # scope at this occurrence, including evaluated defaults and overrides.
        environment = tree.root_environment()
        for name in occurrence['ancestors'] + [occurrence['name']]:
            ancestor = tree.resolve_instance(name)
            if tree.statement_for(ancestor).scope is model.scope:
                environment = ancestor['scope_parameters']
                break
        expressions = re.sub(r'(?<![\w])[^\W\d]\w*\s*=', '', model.text)
        dependencies = _parameter_dependencies(expressions, environment)
        return model.text, dependencies
    def unresolved_instance_dependencies(occurrence):
        expressions = ' '.join(value for key, value in occurrence['parameters'].items()
                               if not isinstance(occurrence['effective_parameters'].get(key), (float, int)))
        return _parameter_dependencies(expressions, occurrence['scope_parameters'])
    if check_instance_parameters and not equivalent(
            unresolved_instance_dependencies(row), unresolved_instance_dependencies(copied)):
        raise ValidationError('private edit changed instance parameter dependencies: ' + row['name'])
    original, original_dependencies = declaration(before, row)
    actual, actual_dependencies = declaration(after, copied)
    if original != actual:
        raise ValidationError('private edit changed model declaration: ' + row['name'])
    if not equivalent(original_dependencies, actual_dependencies):
        raise ValidationError('private edit changed model parameter dependencies: ' + row['name'])


def assert_same_occurrences(before, after, *, mutable=(), models=None, terminals=None, local_terminals=None, additions=()):
    """Require exactly the authorized occurrence edits, including untouched peers."""
    original, copied = occurrence_rows(before), occurrence_rows(after)
    if set(copied) != set(original) | set(additions) or set(original) & set(additions):
        raise ValidationError('private occurrence set changed outside target')
    for name, row in original.items():
        other = copied[name]
        expected_model = (models or {}).get(name, row['model'])
        expected_pins = terminals(row) if terminals else row['terminals']
        expected_local = local_terminals(row) if local_terminals else row['local_terminals']
        if (other['model'] != expected_model or other['terminals'] != expected_pins
                or other['local_terminals'] != expected_local
                or (name not in mutable and not equivalent(row['effective_parameters'], other['effective_parameters']))):
            raise ValidationError('private edit changed occurrence: ' + name)
        # Scoped source edits can replace a PWL file parameter with a DC bias.
        # Their model declaration and connectivity are still compared.
        assert_same_model_declaration(before, row, after, other,
                                      check_instance_parameters=name not in mutable)
        if name in (models or {}):
            # Its added drain voltage probe deliberately changes connectivity
            # proof at this ancestor; every descendant is compared below.
            continue
        try:
            identity = before.mos_identity(row)
        except ValidationError:
            continue
        copied_identity = after.mos_identity(other)
        if any(identity[k] != copied_identity[k] for k in ('primitive_type', 'terminal_order', 'inline_alias')) or not equivalent(
                before.effective_mos_parameters(row), after.effective_mos_parameters(other)):
            raise ValidationError('private edit changed MOS identity/geometry: ' + name)


def validate_main_copy(before, after, plan):
    target = before.resolve_instance(plan['target'])
    identity = before.mos_identity(target)
    prefix = target['name'].rsplit('.', 1)[0] + '.' if '.' in target['name'] else ''
    drain, source = [target['terminals'][i] for i in (identity['terminal_order'][0], identity['terminal_order'][2])]
    node = prefix + plan['names']['node']
    def local_terminals(row):
        pins = list(row['local_terminals'])
        if row['name'] == target['name']:
            pins[identity['terminal_order'][0]] = plan['names']['node']
        return pins
    original = occurrence_rows(before)
    expected_pins = {}
    # Topology expansion visits each parent before its descendants.
    for name, row in original.items():
        pins = list(row['terminals'])
        if name == target['name']:
            # Only the drain port moves, even when another port shares its net.
            pins[identity['terminal_order'][0]] = node
        elif name.startswith(target['name'] + '.'):
            parent = original[row['ancestors'][-1]]
            definition = before.lookup(parent['model'], before.statement_for(parent).scope)
            bindings = dict(zip(definition.ports, expected_pins[parent['name']]))
            pins = [bindings.get(pin, pin if pin in before.globals else parent['name'] + '.' + pin)
                    for pin in row['local_terminals']]
        expected_pins[name] = pins
    added = [prefix + plan['names'][k] for k in ('probe', 'injection')]
    assert_same_occurrences(before, after, models={c['occurrence']: c['private'] for c in plan['clones']},
                            terminals=lambda row: expected_pins[row['name']],
                            local_terminals=local_terminals, additions=added)
    rows = occurrence_rows(after)
    for name, model, pins in ((added[0], 'vsource', [drain, node]), (added[1], 'isource', [drain, source])):
        if rows[name]['model'] != model or rows[name]['terminals'] != pins:
            raise ValidationError('private probe/injection binding changed: ' + name)


def validate_mos_copy(before, target, after, copied_path, *, injection=None):
    """Compare the full selected model subtree, permitting only its PAC source."""
    copied = after.resolve_instance(copied_path)
    original_identity, copied_identity = before.mos_identity(target), after.mos_identity(copied)
    if any(original_identity[k] != copied_identity[k] for k in ('primitive_type', 'terminal_order')) or not equivalent(
            before.effective_mos_parameters(target), after.effective_mos_parameters(copied)):
        raise ValidationError('private MOS copy changed identity/effective geometry: ' + target['name'])
    if [copied['terminals'][i] for i in copied_identity['terminal_order']] != ['D', 'G', 'S', 'B']:
        raise ValidationError('private MOS copy changed external D/G/S/B binding')
    def subtree(tree, row):
        return {r['name'][len(row['name']):]: r for r in occurrence_rows(tree).values()
                if r['name'] == row['name'] or r['name'].startswith(row['name'] + '.')}
    original, actual = subtree(before, target), subtree(after, copied)
    # A bare primitive is deliberately wrapped to hold its one injection source.
    primitive_wrapper = original_identity['kind'] == 'primitive' and injection
    if primitive_wrapper:
        original = {'.m0': target}
        actual.pop('')
    injection_path = None
    if injection:
        intrinsic = copied_identity['intrinsic_path']
        parent = intrinsic.rsplit('.', 1)[0] + '.' if '.' in intrinsic else ''
        injection_path = '.' + parent + injection
        if injection_path in original or injection_path not in actual:
            raise ValidationError('private injection collision/missing source')
        injected = actual.pop(injection_path)
        leaf = after.resolve_instance(copied_path + '.' + intrinsic)
        if injected['model'].lower() != 'isource' or injected['terminals'] != [leaf['terminals'][i] for i in (0, 2)] or not equivalent(
                injected['parameters'], dict(dc='0', type='dc', pacmag='1', pacphase='0')):
            raise ValidationError('private injection escaped primitive D/S')
    if set(original) != set(actual):
        raise ValidationError('private model copy changed occurrence set')
    def symbolic_connections(tree, root, rows):
        # Compare formal connectivity with four independent terminals. The VCO
        # may short S/B to ground; that must not hide a bad copied pin binding.
        pins = {'': ['@' + str(i) for i in range(4)]}
        for relative in sorted(rows, key=lambda key: key.count('.')):
            if not relative:
                continue
            parent = relative.rsplit('.', 1)[0]
            parent_row = root if not parent else rows[parent]
            definition = tree.lookup(parent_row['model'], tree.statement_for(parent_row).scope)
            bindings = dict(zip(definition.ports, pins[parent]))
            pins[relative] = [bindings.get(pin, pin if pin in tree.globals else parent + '.' + pin)
                              for pin in rows[relative]['local_terminals']]
        return pins
    expected_pins = {'.m0': ['@' + str(i) for i in range(4)]} if primitive_wrapper else symbolic_connections(before, target, original)
    actual_pins = symbolic_connections(after, copied, dict(actual, **{'': copied}))
    intrinsic = original_identity['intrinsic_path']
    cloned = {''} | {'.' + '.'.join(intrinsic.split('.')[:i]) for i in range(1, len(intrinsic.split('.')))} if injection else set()
    for relative, row in original.items():
        other = actual[relative]
        if actual_pins[relative] != expected_pins[relative]:
            raise ValidationError('private model copy changed connectivity: ' + relative)
        if relative not in cloned and row['model'] != other['model']:
            raise ValidationError('private model copy changed unselected master: ' + relative)
        assert_same_model_declaration(before, row, after, other)
        try:
            before.mos_identity(row)
        except ValidationError:
            if not equivalent(row['effective_parameters'], other['effective_parameters']):
                raise ValidationError('private model copy changed passive parameters: ' + relative)
        else:
            if not equivalent(before.effective_mos_parameters(row), after.effective_mos_parameters(other)):
                raise ValidationError('private model copy changed MOS parameters: ' + relative)


def write_private_netlist(destination, text, *, validate=None, sources=(), candidate=None):
    """Parse at the destination, then re-read the actual file before success."""
    destination = Path(destination)
    if str(destination.resolve()) in sources:
        raise ValidationError('private destination may not overwrite a source file')
    if candidate is None:
        candidate = SpectreTopology(destination, text=text)
    elif candidate.source != str(destination.resolve()) or candidate.file_hashes[candidate.source] != hashlib.sha256(text.encode('utf-8')).hexdigest():
        raise ValidationError('private validation candidate does not match destination bytes')
    if validate:
        validate(candidate)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding='utf-8')
    written = SpectreTopology(destination)
    if written.file_hashes != candidate.file_hashes:
        raise ValidationError('private netlist/dependency changed during write')
    if validate:
        validate(written)
    return written


def write_analysis_copy(source, destination, text):
    """Analysis-only private copies must preserve every circuit occurrence."""
    original = SpectreTopology(source)
    candidate = SpectreTopology(source, text=text)
    text = candidate.relocated_text(candidate.source, 0, len(text))
    return write_private_netlist(destination, text, sources=original.files,
        validate=lambda copied: assert_same_occurrences(original, copied))
