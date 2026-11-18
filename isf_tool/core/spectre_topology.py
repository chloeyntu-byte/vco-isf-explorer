"""Bounded Spectre syntax, lexical definitions and occurrence identities.

Identifiers are case sensitive (Spectre); keywords and primitive types are not.
No simulator expressions are executed. Source spans always refer to original
files, including continuations. Unsupported elaboration fails with its location.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ValidationError

ANALYSES = frozenset('hb pss hbnoise pnoise noise dc tran stb hbxf pxf hbac pac ac sp'.split())
SOURCES = frozenset('vsource isource iprobe ccvs vcvs vccs cccs'.split())
MOS_TYPES = frozenset('bsim3 bsim3v3 bsim4 bsim6 bsimsoi bsimcmg psp psp102 psp103 mos1 mos2 mos3 mos9 mos11'.split())
MAX_BYTES = 32 * 1024 * 1024


def canonical_name(value):
    value = str(value or '').strip().replace('\\<', '<').replace('\\>', '>')
    value = value.lstrip('/').replace('/', '.')
    return '0' if value == 'gnd!' else value


def assignments(text):
    result = {}
    # Find assignments only outside quotes/brackets; values may contain spaces.
    starts, depth, quote, escaped = [], 0, None, False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == '\\':
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in '\"\'':
            quote = char
        elif char in '([{':
            depth += 1
        elif char in ')]}':
            depth -= 1
        elif char == '=' and depth == 0:
            end = index
            while end and text[end - 1].isspace(): end -= 1
            begin = end
            while begin and not text[begin - 1].isspace() and text[begin - 1] != '=': begin -= 1
            if begin < end:
                starts.append((begin, index + 1, text[begin:end]))
    for n, (start, begin, name) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(text)
        result[name] = text[begin:end].strip()
    return result


@dataclass
class Statement:
    source: str
    start: int
    end: int
    line: int
    end_line: int
    text: str
    scope: object = None
    kind: str = 'other'
    name: str = ''
    model: str = ''
    pins: list = field(default_factory=list)
    parameters: dict = field(default_factory=dict)
    tail: str = ''
    section: str = None
    include_path: str = None

    def span(self):
        return dict(file=self.source, start=self.start, end=self.end,
                    line=self.line, end_line=self.end_line)


def statements(text, source='<text>'):
    """Both trailing-backslash and leading-plus continuation, with spans."""
    result, pending, offset, model_depth = [], None, 0, 0
    for number, raw in enumerate(text.splitlines(True), 1):
        # // inside a quoted pathname is data.
        clean = re.sub(r'("[^"\n]*"|\x27[^\x27\n]*\x27)|//.*',
                       lambda m: m.group(1) or '', raw).strip()
        if not clean or clean.startswith('*'):
            if pending:
                pending.end = offset + len(raw)
            offset += len(raw)
            continue
        plus = clean.startswith('+')
        if plus or model_depth > 0 or pending and pending.text.endswith('\\'):
            if pending is None:
                raise ValidationError('orphan continuation at {}:{}'.format(source, number))
            pending.text = pending.text.rstrip('\\').rstrip() + ' ' + (clean[1:].strip() if plus else clean)
            pending.end, pending.end_line = offset + len(raw), number
            if model_depth > 0: model_depth += clean.count('{') - clean.count('}')
        else:
            pending = Statement(str(source), offset, offset + len(raw), number, number, clean)
            result.append(pending)
            model_depth = clean.count('{') - clean.count('}') if clean.split()[0].lower() == 'model' else 0
        offset += len(raw)
    if model_depth > 0:
        raise ValidationError('unclosed model block in ' + source)
    if pending and pending.text.endswith('\\'):
        raise ValidationError('unterminated continuation at {}:{}'.format(source, pending.line))
    return result


def logical_lines(text):
    return [item.text for item in statements(text)]


_FILE_SYNTAX_CACHE = OrderedDict()
_FILE_SYNTAX_LOCK = threading.RLock()


def _file_syntax(source):
    # Read and hash bytes on every bind: a file can change twice within one
    # filesystem timestamp tick. Stat-only caches can silently reuse old ASTs.
    raw = Path(source).read_bytes()
    if len(raw) > MAX_BYTES: raise ValidationError('model exceeds parser bound: ' + source)
    digest = hashlib.sha256(raw).hexdigest()
    key = (source,digest)
    with _FILE_SYNTAX_LOCK:
        if key in _FILE_SYNTAX_CACHE:
            result = _FILE_SYNTAX_CACHE.pop(key)
            _FILE_SYNTAX_CACHE[key] = result
            return result
    text = raw.decode('utf-8',errors='replace')
    result = text, statements(text,source), digest
    with _FILE_SYNTAX_LOCK:
        _FILE_SYNTAX_CACHE[key] = result
        while len(_FILE_SYNTAX_CACHE) > 48: _FILE_SYNTAX_CACHE.popitem(last=False)
    return result


def _active(items, section, source):
    regions, active, seen, result = [], None, {}, []
    for item in items:
        tokens = item.text.split()
        key = tokens[0].lower()
        if key == 'section':
            if active is not None or len(tokens) != 2:
                raise ValidationError('invalid section at {}:{}'.format(source, item.line))
            active = tokens[1]
            seen[active] = seen.get(active, 0) + 1
        elif key == 'endsection':
            if active is None or len(tokens) > 1 and tokens[1] != active:
                raise ValidationError('section boundary mismatch at {}:{}'.format(source, item.line))
            active = None
        elif key not in ('library', 'endlibrary') and (active is None or active == section):
            item.section = active
            result.append(item)
    if active is not None:
        raise ValidationError('unclosed section in ' + source)
    if section is not None and seen.get(section) != 1:
        raise ValidationError('cannot uniquely locate section {} in {}'.format(section, source))
    return result


@dataclass
class Definition:
    name: str
    header: Statement
    parent: object
    ports: list = field(default_factory=list)
    parameters: dict = field(default_factory=dict)
    instances: list = field(default_factory=list)
    definitions: dict = field(default_factory=dict)
    models: dict = field(default_factory=dict)
    body: list = field(default_factory=list)
    end: Statement = None
    inline: bool = False


def _suffix_number(match):
    scale = {'t': 1e12, 'g': 1e9, 'meg': 1e6, 'k': 1e3, 'm': 1e-3,
             'u': 1e-6, 'n': 1e-9, 'p': 1e-12, 'f': 1e-15, 'a': 1e-18}
    return str(float(match.group(1)) * scale[match.group(2).lower()])


def evaluate(expression, environment):
    expression = str(expression).strip().strip("'{}")
    expression = re.sub(r'(?<![\w.])((?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(meg|[tgkmunpfa])\b', _suffix_number, expression, flags=re.I)
    try:
        if len(expression) > 16384: raise ValueError('expression exceeds evaluator bound')
        tree = ast.parse(expression.replace('^', '**'), mode='eval')
        def visit(node):
            if isinstance(node, ast.Expression): return visit(node.body)
            if isinstance(node, ast.Constant) and type(node.value) in (float, int): return float(node.value)
            if isinstance(node, ast.Name):
                if node.id not in environment: raise ValueError('missing parameter ' + node.id)
                value = environment[node.id]
                if not isinstance(value, (int, float)): raise ValueError('unresolved parameter {}={}'.format(node.id, value))
                return value
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                return visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
            if isinstance(node, ast.BinOp):
                a, b = visit(node.left), visit(node.right)
                if isinstance(node.op, ast.Add): return a + b
                if isinstance(node.op, ast.Sub): return a - b
                if isinstance(node.op, ast.Mult): return a * b
                if isinstance(node.op, ast.Div): return a / b
                if isinstance(node.op, ast.Pow) and abs(b) < 100: return a ** b
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
                functions = dict(sqrt=math.sqrt, abs=abs, min=min, max=max, pow=math.pow, pwr=math.pow, floor=math.floor, ceil=math.ceil, log=math.log, exp=math.exp)
                if node.func.id in functions: return functions[node.func.id](*[visit(n) for n in node.args])
            raise ValueError('unsupported expression ' + expression)
        value = float(visit(tree))
        if not math.isfinite(value): raise ValueError('nonfinite expression ' + expression)
        return value
    except (SyntaxError, TypeError, ZeroDivisionError, OverflowError) as exc:
        raise ValueError('cannot evaluate {}: {}'.format(expression, exc))


def effective_values(raw, environment):
    result, pending = {}, dict(raw)
    for _ in range(len(raw) + 1):
        progress = False
        for name, expr in list(pending.items()):
            try:
                result[name] = evaluate(expr, dict(environment, **result))
            except ValueError:
                continue
            del pending[name]
            progress = True
        if not progress: break
    return dict(result, **pending)


class SpectreTopology:
    def __init__(self, source=None, *, text=None, section=None, includes=True, read_cache=None):
        self.source_override = text is not None
        self.source = str(Path(source).resolve()) if source and str(source) != '<text>' else '<text>'
        self.root = Definition('', None, None)
        self.files, self.file_hashes, self.items, self.includes = {}, {}, [], []
        self.globals = {'0'}
        self._loaded, self._stack = set(), set()
        self._read_cache = read_cache if read_cache is not None else {}
        self._follow = includes
        self._load(self.source, section, self.root, text)
        self._occurrences = {}
        self._catalog = None
        self._root_env = None
        self._items_by_span = {(i.source,i.start):i for i in self.items if i.kind == "instance"}
        self._mos_cache = {}

    def _read(self, source):
        path = Path(source)
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValidationError('netlist/model unreadable or too large: ' + source)
        if path not in self._read_cache:
            self._read_cache[path] = path.read_text(encoding='utf-8', errors='replace')
        return self._read_cache[path]

    def _load(self, source, section, owner, text=None):
        identity = (source, section, id(owner))
        if identity in self._stack:
            raise ValidationError('recursive include: ' + source)
        if identity in self._loaded: return
        if len(self._loaded) >= 160 or len(self._stack) >= 16:
            raise ValidationError('include chain exceeds bounded parser: ' + source)
        self._loaded.add(identity)
        self._stack.add(identity)
        if text is None:
            stat = Path(source).stat()
            if stat.st_size > MAX_BYTES: raise ValidationError('model exceeds parser bound: ' + source)
            text, syntax, raw_hash = _file_syntax(source)
        else:
            syntax = statements(text, source)
            raw_hash = hashlib.sha256(text.encode()).hexdigest()
        self.files[source] = text
        self.file_hashes[source] = raw_hash
        cache_key = (source, section, raw_hash, 'syntax')
        if cache_key not in self._read_cache or source == self.source:
            self._read_cache[cache_key] = _active(syntax, section, source)
        active = [copy.copy(i) for i in self._read_cache[cache_key]]
        current, nesting = owner, []
        for item in active:
            item.scope = current
            line = item.text
            tokens = line.split()
            first = tokens[0].lower()
            if first in ('if', 'else', 'elseif', 'alter', 'altergroup') or first.startswith('if('):
                raise ValidationError('unsupported conditional/elaboration at {}:{}'.format(source,item.line))
            self.items.append(item)
            current.body.append(item)
            header = re.match(r'(?i)^(inline\s+)?subckt\s+([^\s(]+)\s*(.*)$', line)
            if header:
                inline, name, rest = header.groups()
                params = assignments(rest)
                ports_text = rest.split('parameters', 1)[0]
                if '(' in rest:
                    ports_text = rest[rest.index('(') + 1:rest.index(')')]
                else:
                    ports_text = re.split(r'\b[^\s=]+\s*=', ports_text, maxsplit=1)[0]
                ports = [n for p in ports_text.split() if p != 'parameters' for n in self._names(p)]
                if name in current.definitions or name in current.models:
                    raise ValidationError('duplicate active definition {} at {}:{}'.format(name, source, item.line))
                item.kind, item.name, item.parameters = 'subckt', name, params
                definition = Definition(name, item, current, ports, dict(params), inline=bool(inline))
                current.definitions[name] = definition
                nesting.append(current)
                current = definition
                continue
            if first == 'ends':
                if not nesting or len(tokens) > 1 and tokens[1] != current.name:
                    raise ValidationError('subckt boundary mismatch at {}:{}'.format(source, item.line))
                item.kind = 'ends'
                current.end = item
                current = nesting.pop()
                continue
            if first in ('include', 'ahdl_include'):
                match = re.fullmatch(r'''(?i)(include|ahdl_include)\s+["']([^"']+)["'](?:\s+section\s*=\s*([^\s]+))?''', line)
                if not match:
                    raise ValidationError('unsupported include (literal pathname required) at {}:{}: {}'.format(source, item.line, line))
                kind, path, child_section = match.groups()
                path = os.path.expanduser(os.path.expandvars(path))
                if '$' in path:
                    raise ValidationError('missing include environment variable: ' + path)
                item.kind, item.name = kind.lower(), path
                item.include_path = str((Path(source).parent / path).resolve())
                item.parameters = {'section': child_section} if child_section else {}
                self.includes.append(item)
                if kind.lower() == 'include' and self._follow:
                    self._load(item.include_path, child_section, current)
                continue
            if first == 'global':
                self.globals.update(n for p in tokens[1:] for n in self._names(p))
                item.kind = 'global'
            elif first == 'parameters':
                item.kind, item.parameters = 'parameters', assignments(line[len(tokens[0]):])
                for key, value in item.parameters.items():
                    if key in current.parameters:
                        raise ValidationError('duplicate parameter {} at {}:{}'.format(key, source, item.line))
                    current.parameters[key] = value
            elif first == 'model':
                if len(tokens) < 3: raise ValidationError('invalid model at ' + source)
                item.kind, item.name, item.model = 'model', tokens[1], tokens[2].lower().rstrip('{')
                item.parameters = {}  # Model bin equations are opaque; only primitive TYPE is needed.
                if item.name in current.models or item.name in current.definitions:
                    raise ValidationError('duplicate active model {} at {}:{}'.format(item.name, source, item.line))
                current.models[item.name] = item
            elif first == 'save':
                item.kind, item.pins = 'save', tokens[1:]
            else:
                match = re.match(r'^(\S+)\s*\(([^)]*)\)\s*(\S+)(.*)$', line)
                if match:
                    item.name, nodes, item.model, item.tail = match.groups()
                    item.pins = [canonical_name(n) for n in nodes.split()]
                    item.parameters = assignments(item.tail)
                    item.kind = 'analysis' if item.model.lower() in ANALYSES else 'instance'
                elif len(tokens) > 1 and tokens[1].lower() in ANALYSES | {'options', 'info'}:
                    item.name, item.model = tokens[:2]
                    item.tail = line.split(tokens[1], 1)[1].strip()
                    item.parameters = assignments(item.tail)
                    item.kind = 'analysis' if item.model.lower() in ANALYSES else item.model.lower()
                if item.kind == 'instance': current.instances.append(item)
        if nesting:
            raise ValidationError('unclosed subckt in ' + source)
        self._stack.remove(identity)

    def lookup(self, name, scope=None):
        scope = scope or self.root
        while scope is not None:
            if name in scope.definitions: return scope.definitions[name]
            if name in scope.models: return scope.models[name]
            scope = scope.parent
        return None

    def primitive_type(self, item):
        found = self.lookup(item.model, item.scope)
        if isinstance(found, Definition): return None
        return found.model if isinstance(found, Statement) else item.model.lower() if item.model.lower() in MOS_TYPES else None

    def mos_polarity(self, item):
        """Read polarity from the primitive or all explicitly typed model bins."""
        primitive = self.primitive_type(item)
        aliases = {'n': 'nmos', 'nmos': 'nmos', 'p': 'pmos', 'pmos': 'pmos'}
        if primitive in ('nmos', 'pmos'):
            return primitive
        model = self.lookup(item.model, item.scope)
        if isinstance(model, Statement):
            values = {aliases.get(value.lower()) for value in
                      re.findall(r'(?i)\btype\s*=\s*["\']?([a-z]+)\b', model.text)}
            if len(values) == 1:
                return values.pop()
        return None

    def is_local(self, source):
        return self.source == '<text>' or os.path.commonpath([str(Path(self.source).parent), source]) == str(Path(self.source).parent)

    def _names(self, name):
        name = canonical_name(name)
        match = re.fullmatch(r'(.*)<(-?\d+):(-?\d+)>', name)
        if not match: return [canonical_name(name)]
        stem, begin, end = match.groups()
        begin, end = int(begin), int(end)
        if abs(end - begin) > 10000: raise ValidationError('iterated instance exceeds parser bound: ' + name)
        step = 1 if end >= begin else -1
        return ['{}<{}>'.format(stem, n) for n in range(begin, end + step, step)]

    def instance_pins(self, item, local_name=None):
        names = self._names(item.name)
        if len(names) == 1:
            return [n for p in item.pins for n in self._names(p)]
        index = names.index(local_name)
        pins = []
        for pin in item.pins:
            members = self._names(pin)
            if len(members) not in (1,len(names)):
                raise ValidationError('unsupported iterated bus width at ' + item.name)
            pins.append(members[0] if len(members) == 1 else members[index])
        return pins

    def root_environment(self):
        if self._root_env is None: self._root_env = effective_values(self.root.parameters,{})
        return self._root_env

    def instance_environment(self, definition, parameters, parent):
        """Bind overrides in the caller before evaluating callee defaults.

        Default expressions remain live until the overrides are bound. Mask
        callee names in the parent so a forward dependency cannot accidentally
        consume a shadowed caller value. Unresolved overrides stay unresolved.
        """
        defaults = {k: v for k, v in definition.parameters.items() if k not in parameters}
        base = {k: v for k, v in parent.items() if k not in definition.parameters}
        base.update(parameters)
        return dict(base, **effective_values(defaults, base))

    def occurrences(self, *, external=False):
        if external in self._occurrences: return copy.deepcopy(self._occurrences[external])
        result = []
        def expand(scope, prefix='', port_map=None, env=None, ancestors=(), stack=(), inherited_m=1):
            env = self.root_environment() if env is None else env
            port_map = port_map or {}
            for item in scope.instances:
                for local_name in self._names(item.name):
                    name = prefix + local_name
                    local_pins = self.instance_pins(item,local_name)
                    nodes = [port_map.get(p, p if p in self.globals or not prefix else prefix + p) for p in local_pins]
                    params = effective_values(item.parameters, env)
                    definition = self.lookup(item.model, item.scope)
                    model_wrapper = isinstance(definition, Definition) and (not self.is_local(definition.header.source) or bool({'l', 'w'} & (set(item.parameters) | set(definition.parameters))))
                    row = dict(name=name, local_name=local_name, model=item.model, terminals=nodes,
                               local_terminals=local_pins, parameters=dict(item.parameters),
                               effective_parameters=params, parameter_text=item.tail.strip(),
                               scope=scope.name, hierarchy_path=prefix.rstrip('.'), definition=item.text,
                               source=item.source, span=item.span(), ancestors=list(ancestors),
                               inherited_m=inherited_m, model_wrapper=model_wrapper, scope_parameters=dict(env))
                    if model_wrapper:
                        try:
                            self.mos_identity(row)
                        except ValidationError:
                            # Geometry parameter names do not make a circuit a
                            # single-MOS macro. Continue through multi-MOS cores.
                            model_wrapper = False
                            row['model_wrapper'] = False
                    result.append(row)
                    if len(result) > 10000: raise ValidationError('expanded instance count exceeds parser bound')
                    if isinstance(definition, Definition) and (external or not model_wrapper):
                        if id(definition) in stack or len(stack) >= 32:
                            raise ValidationError('recursive/deep subckt at ' + name)
                        if len(nodes) != len(definition.ports):
                            raise ValidationError('port count mismatch at ' + name)
                        child_env = self.instance_environment(definition, params, env)
                        mult = params.get('m', 1)
                        if not isinstance(mult, (float, int)):
                            mult = 'unresolved inherited m at ' + name
                        elif isinstance(inherited_m, (float, int)):
                            mult *= inherited_m
                        else: mult = inherited_m
                        expand(definition, name + '.', dict(zip(definition.ports, nodes)), child_env,
                               ancestors + (name,), stack + (id(definition),), mult)
        expand(self.root)
        self._occurrences[external] = result
        return copy.deepcopy(result)

    def statement_for(self, row):
        return self._items_by_span[(row['source'],row['span']['start'])]

    def resolve_instance(self, path):
        path = canonical_name(path)
        parts = path.split('.')
        scope, prefix, bindings = self.root, '', {}
        env, inherited, ancestors = self.root_environment(), 1, []
        for index, part in enumerate(parts):
            matches = [(i,n) for i in scope.instances for n in self._names(i.name) if n == part]
            if len(matches) != 1:
                raise ValidationError('instance path does not resolve uniquely (case sensitive): ' + path)
            item, local = matches[0]
            local_pins = self.instance_pins(item,local)
            nodes = [bindings.get(p,p if p in self.globals or not prefix else prefix+p) for p in local_pins]
            params = effective_values(item.parameters, env)
            definition = self.lookup(item.model,item.scope)
            row = dict(name=prefix+local,local_name=local,model=item.model,terminals=nodes,
                       local_terminals=local_pins,parameters=dict(item.parameters),
                       effective_parameters=params,parameter_text=item.tail.strip(),scope=scope.name,
                       hierarchy_path=prefix.rstrip('.'),definition=item.text,source=item.source,
                       span=item.span(),ancestors=list(ancestors),inherited_m=inherited,scope_parameters=dict(env))
            if index == len(parts)-1: return row
            if not isinstance(definition,Definition) or len(nodes) != len(definition.ports):
                raise ValidationError('cannot descend instance/model ports: '+row['name'])
            child_env = self.instance_environment(definition, params, env)
            multiplier=params.get('m',1)
            inherited = inherited*multiplier if isinstance(inherited,(float,int)) and isinstance(multiplier,(float,int)) else 'unresolved inherited m at '+row['name']
            ancestors.append(row['name'])
            env, bindings, scope, prefix = child_env, dict(zip(definition.ports,nodes)), definition, row['name']+'.'
        raise ValidationError('empty instance path')

    def effective_mos_parameters(self, row, identity=None):
        identity = identity or self.mos_identity(row)
        path = row['name'] + ('.'+identity['intrinsic_path'] if identity['intrinsic_path'] else '')
        intrinsic = self.resolve_instance(path)
        parameters = dict(intrinsic['effective_parameters'])
        for key in ('w','l','nf'):
            if key not in parameters and key in row['effective_parameters']:
                parameters[key] = row['effective_parameters'][key]
        if isinstance(intrinsic['inherited_m'],(float,int)):
            parameters['m'] = parameters.get('m',1) * intrinsic['inherited_m']
        else: parameters['m'] = intrinsic['inherited_m']
        parameters.setdefault('nf',row['effective_parameters'].get('nf',1))
        return dict(parameters, **{key:parameters.get(key) for key in ('w','l','m','nf')})

    def mos_parameter_sources(self, row, identity=None):
        """Trace geometry to caller assignments or callee defaults, without guessing values."""
        identity = identity or self.mos_identity(row)
        path = row['name'] + ('.' + identity['intrinsic_path'] if identity['intrinsic_path'] else '')
        intrinsic = self.resolve_instance(path)
        env = {key: dict(kind='default', expression=value, occurrence='<root>')
               for key, value in self.root.parameters.items()}
        sources, multipliers = {}, []
        for name in intrinsic['ancestors'] + [path]:
            current = self.resolve_instance(name)
            item = self.statement_for(current)
            def origin(expr, *, default=False):
                refs = [key for key in dict.fromkeys(re.findall(r'(?<![\w])([^\W\d]\w*)', expr)) if key in env]
                kind = env[expr]['kind'] if expr in env else 'parent_expression' if refs else 'default' if default else 'instance'
                return dict(kind=kind,
                            expression=expr, occurrence=name,
                            dependencies={key: env[key] for key in refs})
            sources = {key: origin(value) for key, value in item.parameters.items()}
            if 'm' in sources:
                multipliers.append(sources['m'])
            definition = self.lookup(item.model, item.scope)
            if isinstance(definition, Definition):
                defaults = {key: origin(value, default=True) for key, value in definition.parameters.items()
                            if key not in item.parameters}
                # Retain derived default expressions and their callee dependencies.
                bound = dict(defaults, **sources)
                for key, record in defaults.items():
                    refs = [other for other in dict.fromkeys(re.findall(
                        r'(?<![\w])([^\W\d]\w*)', record['expression'])) if other != key and other in bound]
                    if refs:
                        record['kind'] = 'parent_expression'
                        record['dependencies'] = dict(record['dependencies'], **{
                            other: dict(kind=bound[other]['kind'], expression=bound[other]['expression'],
                                        occurrence=bound[other]['occurrence']) for other in refs})
                env = dict({k: v for k, v in env.items() if k not in definition.parameters}, **bound)
        result = {}
        for key in self.effective_mos_parameters(row, identity):
            result[key] = sources.get(key, dict(kind='default', expression='1' if key in ('m', 'nf') else '', occurrence=path))
        if multipliers:
            result['m'] = dict(result['m'], factors=multipliers,
                               kind='parent_expression' if len(multipliers) > 1 else multipliers[0]['kind'])
        return result

    def mos_identity(self, row):
        item = self.statement_for(row)
        definition = self.lookup(item.model, item.scope)
        key = (id(definition), item.model)
        if key in self._mos_cache: return copy.deepcopy(self._mos_cache[key])
        if len(row['local_terminals']) != 4:
            raise ValidationError('unsupported MOS macro {}: {} terminals; four independently driven D/G/S/B required'.format(row['name'], len(row['local_terminals'])))
        if not isinstance(definition, Definition):
            primitive = self.primitive_type(item)
            if primitive not in MOS_TYPES:
                raise ValidationError('unproven MOS model type {} at {}:{}'.format(item.model, item.source, item.line))
            identity = dict(kind='primitive', model=item.model, primitive=item.model, primitive_type=primitive,
                            polarity=self.mos_polarity(item), model_chain=[item.model], intrinsic_path='', intrinsic_terminals=['d','g','s','b'],
                            terminal_order=[0, 1, 2, 3], formal_pins=['d', 'g', 's', 'b'], inline_alias=False,
                            definition_file=definition.source if definition else item.source,
                            definition_section=definition.section if definition else None,
                            evidence=['MOS primitive type ' + primitive], occupied_names=[])
        else:
            edges, leaves, chain, occupied = {}, [], [], set()
            def walk(block, prefix='', bindings=None, stack=()):
                if id(block) in stack: raise ValidationError('recursive MOS model ' + block.name)
                chain.append(block.name)
                bindings = bindings or {p: p for p in block.ports}
                def net(p): return bindings.get(p, p if p in self.globals else prefix + p)
                for child in block.instances:
                    if len(self._names(child.name)) != 1:
                        raise ValidationError('unsupported MOS macro with iterated internal primitives: ' + child.name)
                    nodes = [net(p) for p in self.instance_pins(child)]
                    occupied.update(nodes + [prefix + child.name])
                    found = self.lookup(child.model, child.scope)
                    if isinstance(found, Definition):
                        if len(nodes) != len(found.ports): raise ValidationError('unsupported model port count: ' + child.name)
                        walk(found, prefix + child.name + '.', dict(zip(found.ports, nodes)), stack + (id(block),))
                    elif self.primitive_type(child) in MOS_TYPES:
                        if len(nodes) != 4: raise ValidationError('unsupported MOS primitive terminal count: ' + child.name)
                        leaves.append((prefix + child.name, child, nodes))
                    elif child.model.lower() in ('resistor', 'inductor', 'iprobe') and len(nodes) == 2:
                        a, b = nodes
                        edges.setdefault(a, set()).add(b)
                        edges.setdefault(b, set()).add(a)
            walk(definition)
            if len(definition.ports) != 4 or len(set(definition.ports)) != 4 or len(leaves) != 1:
                raise ValidationError('unsupported MOS macro {}: need four ports and unique MOS primitive, found {}'.format(item.model, len(leaves)))
            intrinsic_path, leaf, inner = leaves[0]
            order, proof = [], []
            for role, node in zip('DGSB', inner):
                pending, reached = [node], set()
                while pending:
                    n = pending.pop()
                    if n not in reached:
                        reached.add(n)
                        pending.extend(edges.get(n, ()))
                candidates = reached.intersection(definition.ports)
                if len(candidates) != 1:
                    raise ValidationError('unproven {} connectivity in {}: {}'.format(role, item.model, sorted(candidates)))
                port = next(iter(candidates))
                order.append(definition.ports.index(port))
                proof.append('{}: {} -> {} through wires/R/L'.format(role, node, port))
            if len(set(order)) != 4:
                raise ValidationError('unproven independent D/G/S/B connectivity in ' + item.model)
            identity = dict(kind='subckt', model=item.model, primitive=leaf.model,
                            primitive_type=self.primitive_type(leaf), polarity=self.mos_polarity(leaf), model_chain=chain + [leaf.model],
                            intrinsic_path=intrinsic_path, intrinsic_terminals=list(leaf.pins),
                            terminal_order=order, formal_pins=list(definition.ports),
                            inline_alias=definition.inline and intrinsic_path == definition.name,
                            definition_file=definition.header.source, definition_section=definition.header.section,
                            evidence=proof, occupied_names=sorted(occupied))
        identity['definition_identity'] = (definition.header.span() if isinstance(definition, Definition) else definition.span() if definition else {'builtin': item.model.lower()})
        self._mos_cache[key] = identity
        return copy.deepcopy(identity)

    def catalog(self):
        if self._catalog is not None: return copy.deepcopy(self._catalog)
        result = dict(devices=[], sources=[], analyses=[], parameters=[], nets=[], saves=[], includes=[], subcircuits=[], warnings=[], files=[self.source])
        for row in self.occurrences():
            try:
                identity = self.mos_identity(row)
                row['mos'] = identity
                row['effective_mos_parameters'] = self.effective_mos_parameters(row,identity)
                row['mos_parameter_sources'] = self.mos_parameter_sources(row,identity)
                order = identity['terminal_order']
                row['connection_terminals'] = list(row['terminals'])
                row['connection_local_terminals'] = list(row['local_terminals'])
                row['terminals'] = [row['terminals'][n] for n in order]
                row['local_terminals'] = [row['local_terminals'][n] for n in order]
            except ValidationError as exc:
                row['mos_error'] = str(exc)
            result['devices'].append(row)
            if row['model'].lower() in SOURCES: result['sources'].append(row)
        for item in self.items:
            if item.kind == 'analysis' and item.scope is self.root:
                result['analyses'].append(dict(name=item.name, model=item.model, analysis_type=item.model.lower(), terminals=item.pins, parameters=item.parameters, definition=item.text, source=item.source, span=item.span()))
            elif item.kind == 'parameters':
                for name, expr in item.parameters.items():
                    result['parameters'].append(dict(name=name, expression=expr, scope=item.scope.name, source=item.source))
            elif item.kind == 'save':
                result['saves'].extend(dict(name=p, scope=item.scope.name, source=item.source) for p in item.pins)
            elif item.kind in ('include', 'ahdl_include'):
                result['includes'].append(dict(name=item.include_path, expression=item.text, source=item.source))
            elif item.kind == 'subckt':
                result['subcircuits'].append(dict(name=item.name, ports=self.lookup(item.name, item.scope).ports, source=item.source))
        result['nets'] = sorted(self.globals | {p for d in result['devices'] + result['analyses'] for p in d['terminals']})
        self._catalog = result
        return copy.deepcopy(result)

    def root_definition_closure(self, model):
        """Root declaration owners reachable from a selected model.

        Follow instance masters through lexical lookup. Unused definitions and
        their local includes must not enter a dynamic replica template.
        """
        needed, visited = set(), set()
        def visit(definition):
            if not isinstance(definition, Definition) or id(definition) in visited:
                return
            visited.add(id(definition))
            owner = definition
            while owner.parent is not self.root:
                owner = owner.parent
            needed.add(id(owner))
            for item in definition.instances:
                visit(self.lookup(item.model, item.scope))
        visit(self.lookup(model) if model else None)
        return [d for d in self.root.definitions.values() if id(d) in needed]

    def model_context(self, row):
        """Declarations needed when a lexical MOS wrapper is copied to root.

        Keep visible sibling declarations inside the copied wrapper (nearest
        scope wins) and freeze referenced enclosing parameters at this exact
        occurrence. No enclosing circuit instances or stimuli are copied.
        """
        item = self.statement_for(row)
        definition = self.lookup(item.model, item.scope)
        if not isinstance(definition, Definition) or definition.parent is self.root:
            return ''
        visible = set(definition.definitions) | set(definition.models) | {definition.name}
        lines = []
        owner = definition.parent
        while owner is not self.root:
            for name, value in list(owner.definitions.items()) + list(owner.models.items()):
                if name in visible:
                    continue
                visible.add(name)
                lines.append(self.relocated_definition_text(value) if isinstance(value, Definition)
                             else self.files[value.source][value.start:value.end])
            visible.add(owner.name)
            owner = owner.parent
        used = self.definition_text(definition) + '\n' + '\n'.join(lines)
        captures = {}
        for key, value in row.get('scope_parameters', {}).items():
            if key not in definition.parameters and re.search(r'(?<![\w])' + re.escape(key) + r'(?![\w])', used):
                if not isinstance(value, (float, int)):
                    raise ValidationError('unresolved lexical parameter {}={} for {}'.format(key, value, row['name']))
                captures[key] = value
        if captures:
            lines.insert(0, 'parameters ' + ' '.join(k + '=' + format(v, '.16g') for k, v in captures.items()))
        return '\n'.join(lines)

    def exported_model_text(self, row):
        definition = self.lookup(row['model'], self.statement_for(row).scope)
        if not isinstance(definition, Definition):
            return ''
        context = self.model_context(row)
        edits = [(definition.end.source, definition.end.start, definition.end.start, context + '\n')] if context else ()
        return self.relocated_definition_text(definition, edits)

    def relocated_text(self, source, start, end, edits=()):
        """Apply file-bound edits to a copied range, expanding edited includes only.

        Other includes remain read-only absolute references. An edit can never
        consume offsets from another source or escape the requested range.
        """
        used = set()
        def edited(source, visited=()):
            if source in visited:
                return False  # The parser already validated active section recursion.
            return any(file == source for file, _, _, _ in edits) or any(
                edited(i.include_path, visited + (source,)) for i in self.includes
                if i.source == source and i.kind == 'include')
        def render(source, start, end, section=None, expanded=False):
            text = self.files[source][start:end]
            changes = []
            # Includes of different sections share byte offsets. Apply an edit
            # only in the expansion whose active statements own its span.
            syntax = _active(statements(self.files[source], source), section, source) if expanded else None
            for index, (file, begin, finish, replacement) in enumerate(edits):
                if file == source and start <= begin <= finish <= end:
                    if syntax is not None and not any(
                            item.start <= begin < item.end and finish <= item.end for item in syntax):
                        continue
                    changes.append((begin-start, finish-start, replacement))
                    used.add(index)
            includes = {(i.start, i.end): i for i in self.includes if i.source == source
                        and start <= i.start and i.end <= end}
            if syntax is not None:
                active_spans = {(i.start, i.end) for i in syntax}
                includes = {span: item for span, item in includes.items() if span in active_spans}
            for include in includes.values():
                if include.kind == 'include' and edited(include.include_path):
                    child = self.files[include.include_path]
                    replacement = render(include.include_path, 0, len(child), include.parameters.get('section'), expanded=True)
                else:
                    replacement = include.kind + ' ' + json.dumps(include.include_path) + (
                        ' section=' + include.parameters['section'] if include.parameters.get('section') else '') + '\n'
                changes.append((include.start-start, include.end-start, replacement))
            if expanded:
                # Expand only active statements, omitting library/section markers.
                active_spans = {(i.start, i.end) for i in syntax}
                changes.extend((i.start-start, i.end-start, '') for i in statements(self.files[source], source)
                               if start <= i.start and i.end <= end and (i.start, i.end) not in active_spans)
            boundary = len(text) + 1
            for begin, finish, replacement in sorted(changes, key=lambda e: (e[0], e[1]), reverse=True):
                if finish > boundary:
                    raise ValidationError('overlapping private edits in ' + source)
                text = text[:begin] + replacement + text[finish:]
                boundary = begin
            return text
        result = render(source, start, end)
        if len(used) != len(edits):
            raise ValidationError('private edit does not belong to copied file tree')
        return result

    def relocated_definition_text(self, definition, edits=()):
        return self.relocated_text(definition.header.source, definition.header.start, definition.end.end, edits)

    def definition_text(self, definition):
        return self.files[definition.header.source][definition.header.start:definition.end.end]

    def content_hash(self):
        return hashlib.sha256(json.dumps(self.file_hashes, sort_keys=True).encode()).hexdigest()


def load_topology(source, **kwargs):
    return SpectreTopology(source, **kwargs)
