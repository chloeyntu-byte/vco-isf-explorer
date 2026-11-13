"""Read-only inspection and binding of the two Maestro testbenches.

The portable workflow has one primary input contract: an existing VCO Maestro
setup and an existing flicker-noise Maestro setup.  This module reads their
``maestro.sdb``/``active.state`` files, inspects local RDB metadata, infers the
small set of logical signals needed by the ISF flow, and reports anything that
must be changed or mapped by the user.  It never writes OA, Maestro state,
history, PSF, or PDK files.
"""

from __future__ import unicode_literals

import copy
import hashlib
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover
    from collections import Mapping

from .discovery import inspect_rdb
from .errors import DiscoveryError, ValidationError
from .signal_mapping import cross_tb_mapping, discover_signal_mapping
from .psf_extract import _read_parameter_tables, _parameters_for_run, _id_token


PAIR_SCHEMA_VERSION = 1
MAX_XML_BYTES = 64 * 1024 * 1024
MAX_HISTORIES = 16
MAX_NETLIST_TREE_FILES = 4096
MAX_NETLIST_TREE_BYTES = 256 * 1024 * 1024

_PERIODIC_ANALYSES = frozenset(("hb", "pss"))
_PERIODIC_NOISE_ANALYSES = frozenset(("hbnoise", "pnoise"))
_NOISE_ANALYSES = frozenset(("noise", "hbnoise", "pnoise"))
_DYNAMIC_SMALL_SIGNAL_ANALYSES = frozenset(("hbac", "pac"))
_TRANSFER_ANALYSES = frozenset(("hbxf", "pxf"))

_VARIABLE_ALIASES = {
    "vgs": ("vgs", "vgstos", "gate_source", "v_gate_source"),
    "vds": ("vds", "vdstos", "drain_source", "v_drain_source"),
    "vbs": ("vbs", "vbstos", "bulk_source", "v_bulk_source"),
}

_SPICE_SUFFIX = {
    "t": 1e12,
    "g": 1e9,
    "meg": 1e6,
    "k": 1e3,
    "m": 1e-3,
    "u": 1e-6,
    "n": 1e-9,
    "p": 1e-12,
    "f": 1e-15,
}


def _path(value):
    if value in (None, ""):
        return None
    try:
        value = os.fspath(value)
    except (AttributeError, TypeError):
        pass
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, str):
        raise ValidationError("Maestro path must be a path string")
    return os.path.realpath(os.path.abspath(os.path.expanduser(value)))


def _plain_text(value):
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return text


def _logical_name(value):
    return re.sub(r"[^a-z0-9]+", "", _plain_text(value).lower())


def _truthy_skill(value):
    text = _plain_text(value).lower().replace(" ", "")
    return text in ("t", "(t)", "true", "1", "yes")


def _first(values, default=None):
    for value in values:
        if value not in (None, "", "nil", '""'):
            return value
    return default


def _direct_field(element, name):
    if element is None:
        return None
    for child in list(element):
        if child.tag == "field" and child.get("Name") == name:
            return _plain_text(child.text)
    return None


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _netlist_tree_manifest(input_scs):
    """Hash one generated netlist directory without following symlinks.

    ``input.scs`` is not always self-contained: hierarchy maps, local include
    files and simulator control files may live beside it.  Binding only the
    top file leaves a time-of-check/time-of-use gap, so the selected Maestro
    history is sealed to the complete regular-file tree.
    """

    input_scs = _path(input_scs)
    root = os.path.dirname(input_scs)
    records = []
    total_bytes = 0
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories[:] = sorted(directories)
        files = sorted(files)
        for name in files:
            path = os.path.join(current, name)
            relative = os.path.relpath(path, root)
            if os.path.islink(path):
                records.append(
                    {
                        "path": relative,
                        "type": "symlink",
                        "target": os.readlink(path),
                    }
                )
                continue
            if not os.path.isfile(path):
                continue
            size = os.path.getsize(path)
            total_bytes += size
            if (
                len(records) >= MAX_NETLIST_TREE_FILES
                or total_bytes > MAX_NETLIST_TREE_BYTES
            ):
                raise DiscoveryError(
                    "generated Maestro netlist tree exceeds inspection limit",
                    code="ISF-E1205",
                    details={
                        "root": root,
                        "files": len(records),
                        "bytes": total_bytes,
                    },
                )
            records.append(
                {
                    "path": relative,
                    "type": "file",
                    "bytes": size,
                    "sha256": _sha256_file(path),
                }
            )
    encoded = json.dumps(
        records, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "root": os.path.realpath(root),
        "files": records,
        "file_count": sum(1 for item in records if item["type"] == "file"),
        "bytes": total_bytes,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _parse_xml(path, label):
    if not path or not os.path.isfile(path):
        raise DiscoveryError(
            "%s is absent: %s" % (label, path),
            code="ISF-E1201",
            details={"path": path, "label": label},
        )
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise DiscoveryError(
            "cannot stat %s: %s" % (label, exc),
            code="ISF-E1201",
            details={"path": path},
        )
    if size > MAX_XML_BYTES:
        raise DiscoveryError(
            "%s exceeds the read-only inspection limit" % label,
            code="ISF-E1201",
            details={"path": path, "bytes": size, "limit": MAX_XML_BYTES},
        )
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, IOError, OSError) as exc:
        raise DiscoveryError(
            "cannot parse %s: %s" % (label, exc),
            code="ISF-E1201",
            details={"path": path},
        )


def normalize_maestro_dir(value):
    """Resolve a cell directory, maestro directory, or maestro.sdb file."""

    path = _path(value)
    if path is None:
        return None
    candidates = []
    if os.path.isfile(path):
        if os.path.basename(path) == "maestro.sdb":
            candidates.append(os.path.dirname(path))
        elif path.lower().endswith(".rdb"):
            current = os.path.dirname(path)
            for _index in range(5):
                candidates.append(current)
                current = os.path.dirname(current)
    elif os.path.isdir(path):
        candidates.extend((path, os.path.join(path, "maestro")))
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, "maestro.sdb")):
            return os.path.realpath(candidate)
    raise DiscoveryError(
        "path is not a Maestro setup: %s" % path,
        code="ISF-E1201",
        details={
            "path": path,
            "expected": "maestro/maestro.sdb or maestro.sdb",
        },
    )


def _tool_options(test):
    output = {}
    if test is None:
        return output
    for option in test.findall("./tooloptions/option"):
        name = _plain_text(option.text)
        value = option.find("./value")
        if name:
            output[name] = _plain_text(value.text if value is not None else "")
    return output


def _sdb_test_records(root):
    active = root.find("./active")
    records = []
    if active is None:
        return records
    for test in active.findall("./tests/test"):
        name = _plain_text(test.text)
        options = _tool_options(test)
        variables = []
        for variable in test.findall("./vars/var"):
            variable_name = _plain_text(variable.text)
            value = variable.find("./value")
            variables.append(
                {
                    "name": variable_name,
                    "expression": _plain_text(value.text if value is not None else ""),
                    "scope": "test",
                    # Native Maestro retains blank test placeholders after
                    # moving variables to global; an explicit quoted "" is
                    # still a real local string override.
                    "has_local_value": bool(value is not None and value.text and value.text.strip()),
                }
            )
        outputs = []
        for output in test.findall("./outputs/output"):
            outputs.append(
                {
                    "name": _plain_text(output.text),
                    "expression": None,
                    "selected": True,
                }
            )
        enabled = test.get("enabled")
        records.append(
            {
                "name": name,
                "enabled": enabled is None or str(enabled) not in ("0", "false"),
                "design": {
                    "lib": options.get("lib"),
                    "cell": options.get("cell"),
                    "view": options.get("view"),
                    "simulator": options.get("sim"),
                },
                "variables": variables,
                "outputs": outputs,
            }
        )
    return records


def _struct_records(component, list_name, item_prefix):
    if component is None:
        return []
    parent = component.find(".//field[@Name='%s']" % list_name)
    if parent is None:
        return []
    output = []
    for item in list(parent):
        if item.tag != "field" or not str(item.get("Name", "")).startswith(item_prefix):
            continue
        record = {}
        for child in list(item):
            if child.tag == "field" and child.get("Name"):
                record[child.get("Name")] = _plain_text(child.text)
        output.append(record)
    return output


def _state_variables(test):
    component = test.find("./component[@Name='variables']")
    records = _struct_records(component, "saveComponent", "saveComponent_")
    return [
        {
            "name": record.get("name"),
            "expression": record.get("expression"),
            "scope": "test",
        }
        for record in records
        if record.get("name")
    ]


def _state_outputs(test):
    component = test.find("./component[@Name='outputs']")
    records = _struct_records(component, "outputList", "outputList_")
    output = []
    for record in records:
        name = record.get("name")
        signal = record.get("signal")
        expression = record.get("expression")
        if name or (signal and signal.lower() != "nil"):
            output.append(
                {
                    "name": name,
                    "signal": signal if signal and signal.lower() != "nil" else None,
                    "expression": (
                        expression
                        if expression and expression.lower() != "nil"
                        else None
                    ),
                    "selected": _truthy_skill(record.get("displayIt")),
                }
            )
    return output


def _state_analyses(test):
    component = test.find("./component[@Name='analyses']")
    output = []
    if component is None:
        return output
    for analysis in component.findall(".//analyses[@Name='analysis']/analysis"):
        fields = {}
        for field in analysis.findall(".//field"):
            name = field.get("Name")
            value = _plain_text(field.text)
            if name and value not in ("", "nil", "()", "(nil)"):
                fields[name] = value
        enable_values = [
            _plain_text(field.text)
            for field in analysis.findall(".//field[@Name='enable']")
        ]
        output.append(
            {
                "name": str(analysis.get("Name", "")).lower(),
                "enabled": any(_truthy_skill(value) for value in enable_values),
                "fields": fields,
            }
        )
    return output


def _state_models(test):
    component = test.find("./component[@Name='modelSetup']")
    if component is None:
        return []
    field = component.find(".//field[@Name='modelFiles']")
    text = field.text if field is not None else ""
    records = []
    for path, section in re.findall(r'\(\s*"([^"]+)"\s+"([^"]*)"\s*\)', text or ""):
        records.append(
            {
                "path": os.path.realpath(os.path.expanduser(path)),
                "section": section or None,
                "exists": os.path.isfile(os.path.expanduser(path)),
            }
        )
    return records


def _state_design(test):
    component = test.find("./component[@Name='adeInfo']")
    field = component.find(".//field[@Name='designInfo']") if component is not None else None
    values = re.findall(r'"([^"]*)"', field.text or "") if field is not None else []
    return {
        "lib": values[0] if len(values) > 0 else None,
        "cell": values[1] if len(values) > 1 else None,
        "view": values[2] if len(values) > 2 else None,
        "simulator": values[3] if len(values) > 3 else None,
    }


def _state_test_records(root):
    records = []
    if root is None:
        return records
    for test in root.findall("./Test"):
        records.append(
            {
                "name": test.get("Name"),
                "design": _state_design(test),
                "variables": _state_variables(test),
                "outputs": _state_outputs(test),
                "analyses": _state_analyses(test),
                "models": _state_models(test),
            }
        )
    return records


def _merge_named_records(primary, overlay):
    output = []
    by_name = {}
    for record in primary + overlay:
        name = record.get("name")
        key = str(name or "")
        if key not in by_name:
            by_name[key] = copy.deepcopy(record)
            output.append(by_name[key])
            continue
        target = by_name[key]
        for field, value in record.items():
            if value not in (None, "", [], {}):
                target[field] = copy.deepcopy(value)
    return output


def _global_variables(sdb_root):
    """Read active setup globals only, never corner/history parameter values."""
    active = sdb_root.find('./active')
    if active is None:
        return []
    parametric_groups = _variable_groups(active)
    records = []
    for path in ('./vars/var', './globalvars/var', './globalVariables/var'):
        for variable in active.findall(path):
            name = _plain_text(variable.text) or variable.get('name')
            value = variable.find('./value')
            if name:
                records.append({'name': name, 'expression': _plain_text(value.text if value is not None else ''),
                                'scope': 'global', 'source': 'maestro.sdb active global',
                                'parametric_groups': [group['name'] for group in parametric_groups if name in group['variables']],
                                'dependent_tests': [{'name': _plain_text(item.text),
                                                     'enabled': _truthy_skill(item.get('enabled', '1')),
                                                     'omitted': str(item.get('omitted', '0')).lower() not in ('0', 'false', 'nil', 'no')}
                                                    for item in variable.findall('./dependentTests/dependentTest')]})
    return _merge_named_records([], records)


def _variable_groups(parent):
    groups = []
    if parent is None:
        return groups
    for item in parent.findall('./variablesets/variableset'):
        if str(item.get('enabled', '1')).lower() in ('0', 'false'):
            continue
        groups.append({'name': _plain_text(item.text),
                       'variables': [_plain_text(var.text) for var in item.findall('./vars/var') if _plain_text(var.text)]})
    return groups


def _unique_test_variables(records, test_name):
    """Native active.state can repeat one variable; conflicting values fail."""
    result, by_name = [], {}
    for record in records:
        name = record['name']
        if name in by_name:
            previous = by_name[name]
            if _plain_text(previous.get('expression')) != _plain_text(record.get('expression')):
                raise ValidationError('Maestro test %s variable %s has conflicting duplicate values: %r / %r; cannot silently select one value'
                                      % (test_name, name, previous.get('expression'), record.get('expression')))
            continue
        by_name[name] = record
        result.append(record)
    return result


def _attach_global_variables(tests, sdb_root):
    globals_ = _global_variables(sdb_root)
    original_globals = {item['name']: item for item in globals_}
    sdb_tests = {record['name']: record for record in _sdb_test_records(sdb_root)}
    local_groups = {_plain_text(item.text): _variable_groups(item) for item in sdb_root.findall('./active/tests/test')}
    for test in tests:
        local_names = {item['name'] for item in sdb_tests.get(test['name'], {}).get('variables', []) if item.get('has_local_value', True)}
        by_name = {item['name']: copy.deepcopy(item) for item in globals_}
        local_records = []
        local_group_variables = {name for group in local_groups.get(test['name'], []) for name in group['variables']}
        for variable in _unique_test_variables(test.get('variables', []), test['name']):
            name = variable['name']
            if name in by_name and name not in local_names:
                # active.state can repeat effective global values inside a Test;
                # only explicit test declarations shadow active setup globals.
                continue
            record = copy.deepcopy(variable)
            record['scope'] = 'test'
            if name in by_name:
                global_record = by_name[name]
                applies = any(item['name'] == test['name'] and item.get('enabled', True) and not item['omitted']
                              for item in global_record.get('dependent_tests', []))
                grouped = bool(global_record.get('parametric_groups'))
                if grouped and applies and name not in local_group_variables:
                    # Native Maestro recreates non-empty scalar test values
                    # after a global parametric run. The global active group
                    # plus explicit non-omitted dependency establishes scope;
                    # never infer a cache from equality/minimum of the values.
                    global_record['scope_evidence'] = 'active_global_parametric_group_and_test_dependency'
                    global_record['test_cached_expression'] = record.get('expression')
                    global_record['test_value_role'] = 'maestro_sweep_cache_or_local_default'
                    global_record['scope_note'] = 'Global joint parameter group applies to this test effective; separately saved test scalars may be native sweep cache or local defaults, execution uses selected history of RDB restore per-point values. '
                    record.update(scope='test_cache_or_default', global_expression=global_record.get('expression'),
                                  scope_evidence=global_record['scope_evidence'], scope_note=global_record['scope_note'])
                    local_records.append(record)
                    continue
                if grouped:
                    record.update(scope='global_or_test', global_expression=global_record.get('expression'),
                                  parametric_groups=global_record.get('parametric_groups'),
                                  scope_note='Both global parameter group and test Value, lacks proof that the group applies to this test complete markers; the scalar alone cannot establish a local override or sweep cache. ')
                    by_name[name] = record
                    local_records.append(copy.deepcopy(record))
                    continue
                record['shadows_global'] = True
                record['global_expression'] = original_globals[name].get('expression')
            by_name[name] = record
            local_records.append(copy.deepcopy(record))
        test['variables'] = list(by_name.values())
        test['global_variables'] = copy.deepcopy(globals_)
        test['global_parametric_groups'] = _variable_groups(sdb_root.find('./active'))
        test['test_variables'] = local_records
    return globals_


def _analysis_map(test):
    # Disabled analyses are retained in the raw inspection report for UI
    # diagnosis, but they must never satisfy an execution capability.
    return dict(
        (item["name"], item)
        for item in test.get("analyses", [])
        if item.get("enabled")
    )


def _enabled_analysis_names(test):
    return sorted(
        item["name"] for item in test.get("analyses", []) if item.get("enabled")
    )


def _named_values(test, field):
    return [item.get("name") for item in test.get(field, []) if item.get("name")]


def _match_alias(names, aliases):
    exact = dict((_logical_name(name), name) for name in names if name)
    for alias in aliases:
        value = exact.get(_logical_name(alias))
        if value:
            return value
    for name in names:
        logical = _logical_name(name)
        if any(_logical_name(alias) in logical for alias in aliases):
            return name
    return None


def _parse_spice_number(value):
    text = _plain_text(value).strip("()[] ")
    match = re.match(
        r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z]+)?$",
        text,
    )
    if not match:
        return None
    number = float(match.group(1))
    raw_suffix = match.group(2) or ""
    # ADE frequency fields conventionally render MHz as ``M``.  Preserve the
    # case here even though generic SPICE numeric syntax often treats ``m`` as
    # milli.
    suffix = "meg" if raw_suffix == "M" else raw_suffix.lower()
    if suffix:
        if suffix not in _SPICE_SUFFIX:
            return None
        number *= _SPICE_SUFFIX[suffix]
    return number


def _analysis_covers(analysis, offset_hz):
    if not analysis or not analysis.get("enabled"):
        return False
    fields = analysis.get("fields", {})
    values = fields.get("values") or fields.get("values6")
    if values:
        numbers = [float(item) for item in re.findall(r"[+-]?[0-9.]+(?:[eE][+-]?\d+)?", values)]
        if any(abs(number - offset_hz) <= max(1e-9 * offset_hz, 1e-6) for number in numbers):
            return True
    start = _parse_spice_number(fields.get("start"))
    stop = _parse_spice_number(fields.get("stop"))
    if start is None or stop is None:
        return True
    return min(start, stop) <= offset_hz <= max(start, stop)


def _output_at_offset(test, offset_hz):
    candidates = []
    for output in test.get("outputs", []):
        expression = str(output.get("expression") or "")
        name = str(output.get("name") or "")
        if "noise" not in expression.lower() and "flicker" not in name.lower():
            continue
        numbers = [
            float(item)
            for item in re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", expression)
        ]
        if any(abs(number - offset_hz) <= max(1e-9 * offset_hz, 1e-6) for number in numbers):
            candidates.append(name or expression)
    return candidates[0] if candidates else None


def _role_score(test, role):
    analyses = set(_enabled_analysis_names(test))
    name_text = " ".join(
        str(value or "")
        for value in (
            test.get("name"),
            test.get("design", {}).get("lib"),
            test.get("design", {}).get("cell"),
        )
    ).lower()
    variables = _named_values(test, "variables")
    if role == "vco":
        score = 0
        score += 8 if analyses & _PERIODIC_ANALYSES else 0
        score += 7 if analyses & _PERIODIC_NOISE_ANALYSES else 0
        score += 3 if any(token in name_text for token in ("vco", "osc", "tank")) else 0
        score -= 4 if "flicker" in name_text else 0
        return score
    score = 0
    score += 6 if analyses & _NOISE_ANALYSES else 0
    score += 4 if _match_alias(variables, _VARIABLE_ALIASES["vgs"]) else 0
    score += 4 if _match_alias(variables, _VARIABLE_ALIASES["vds"]) else 0
    score += 3 if any(token in name_text for token in ("flicker", "noise", "replica")) else 0
    score -= 4 if "vco" in name_text and not (analyses & {"noise"}) else 0
    return score


def _select_test(tests, role, requested):
    if requested:
        matches = [test for test in tests if test.get("name") == requested]
        return (matches[0], "explicit") if len(matches) == 1 else (None, "missing")
    enabled = [test for test in tests if test.get("enabled", True)] or list(tests)
    ranked = sorted(
        ((-_role_score(test, role), str(test.get("name") or ""), test) for test in enabled),
        key=lambda item: (item[0], item[1]),
    )
    if not ranked:
        return None, "missing"
    best_score = -ranked[0][0]
    if best_score <= 0:
        return None, "ambiguous"
    if len(ranked) > 1 and -ranked[1][0] == best_score:
        return None, "ambiguous"
    return ranked[0][2], "auto"


def _model_root(path):
    path = _path(path)
    if not path:
        return None
    parts = path.split(os.sep)
    lowered = [item.lower() for item in parts]
    for marker in ("models", "model"):
        if marker in lowered:
            index = lowered.index(marker)
            return os.sep.join(parts[:index]) or os.sep
    return os.path.dirname(path)


def _test_binding(test):
    analyses = []
    for item in test.get("analyses", []):
        if not item.get("enabled"):
            continue
        fields = dict(
            (str(key), value)
            for key, value in item.get("fields", {}).items()
            if str(key).lower() != "enable"
        )
        analyses.append(
            {
                "name": item.get("name"),
                # Fundamental, sweep, sideband and output settings are part of
                # the structural analysis contract.  Ordinary design-variable
                # values remain excluded below so bias/size sweeps do not
                # require a rebind.
                "fields": fields,
            }
        )
    value = {
        "name": test.get("name"),
        "enabled": bool(test.get("enabled", True)),
        "design": test.get("design", {}),
        "analyses": sorted(
            analyses, key=lambda item: json.dumps(item, sort_keys=True)
        ),
        "variables": sorted(_named_values(test, "variables")),
        "variable_scopes": sorted((item.get('name'), item.get('scope', 'unknown'), bool(item.get('shadows_global')))
                                  for item in test.get('variables', []) if item.get('name')),
        "outputs": sorted(
            [
                {
                "name": item.get("name"),
                    "signal": item.get("signal"),
                    "expression": item.get("expression"),
                    "selected": bool(item.get("selected", True)),
                }
                for item in test.get("outputs", [])
                if item.get("name") or item.get("signal")
            ],
            key=lambda item: json.dumps(item, sort_keys=True),
        ),
        "models": sorted(
            [
                {
                    "path": os.path.realpath(item.get("path") or ""),
                    "basename": os.path.basename(item.get("path") or ""),
                    "section": item.get("section"),
                    "size": (
                        os.path.getsize(item.get("path"))
                        if item.get("path") and os.path.isfile(item.get("path"))
                        else None
                    ),
                    "mtime_ns": (
                        getattr(
                            os.stat(item.get("path")),
                            "st_mtime_ns",
                            int(
                                os.stat(item.get("path")).st_mtime
                                * 1000000000
                            ),
                        )
                        if item.get("path") and os.path.isfile(item.get("path"))
                        else None
                    ),
                }
                for item in test.get("models", [])
            ],
            key=lambda item: json.dumps(item, sort_keys=True),
        ),
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), value


def _source_design_header(path):
    """Read only the generated root header, never a child schematic comment."""
    values = {key: [] for key in ("library", "cell", "view")}
    with open(path, "r", encoding="utf-8", errors="replace") as stream:
        header = stream.read(65536)
    for line in header.splitlines():
        if line.strip() and not line.lstrip().startswith("//"):
            break
        match = re.fullmatch(r"\s*//\s*Design (library|cell|view) name:\s*(\S.*?)\s*", line)
        if match:
            values[match.group(1)].append(match.group(2))
    return {key: entries[0] if len(entries) == 1 else None for key, entries in values.items()}


def _history_binding(record):
    """Hash-bind the exact Maestro history selected during Preflight."""

    if not isinstance(record, Mapping):
        return None, None
    rdb_path = _path(record.get("path"))
    if record.get('rdb_sha256') and _sha256_file(rdb_path) != record['rdb_sha256']:
        raise DiscoveryError('Maestro RDB changed while binding operating-point parameters')
    sources = []
    for item in record.get("source_netlists", []):
        path = _path(item.get("path")) if isinstance(item, Mapping) else None
        if not path or not os.path.isfile(path):
            continue
        sources.append(
            {
                "path": path,
                "sha256": _sha256_file(path),
                "netlist_tree": _netlist_tree_manifest(path),
                "point_id": item.get("point_id"),
                "design_point": item.get("design_point"),
                "corner": item.get("corner"),
                "test": item.get("test"),
                "operating_point": item.get("operating_point"),
            }
        )
    sources.sort(key=lambda item: json.dumps(item, sort_keys=True))
    value = {
        "name": record.get("name"),
        "path": rdb_path,
        "rdb_sha256": (
            _sha256_file(rdb_path) if rdb_path and os.path.isfile(rdb_path) else None
        ),
        "source_netlists": sources,
    }
    if record.get("raw_dir_resolutions"):
        value["raw_dir_resolutions"] = copy.deepcopy(record["raw_dir_resolutions"])
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    public_value = copy.deepcopy(value)
    for source in public_value["source_netlists"]:
        # Display-only provenance must not make a filesystem touch invalidate
        # the content identity of an otherwise identical frozen history.
        source["source_design"] = _source_design_header(source["path"])
        source["source_mtime"] = os.path.getmtime(source["path"])
        tree = source.get("netlist_tree") or {}
        source["netlist_tree"] = {
            key: tree.get(key)
            for key in ("root", "file_count", "bytes", "sha256")
        }
    return hashlib.sha256(encoded).hexdigest(), public_value


def _operating_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _operating_scalar(value):
    """Accept an evaluated scalar, never an ADE expression or netlist text."""
    token = str(value).strip()
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)", token)
    if not match or match.group(2).lower() not in ("", "t", "g", "meg", "k", "m", "u", "n", "p", "f", "a"):
        raise ValidationError("Operating parameter must be an evaluated number, expressions are not executed implicitly: %r" % value)
    factor = dict(_SPICE_SUFFIX, a=1e-18).get(match.group(2).lower(), 1.0)
    if not math.isfinite(float(match.group(1)) * factor):
        raise ValidationError("Operating parameter must be finite: %r" % value)
    return token


def _operating_numeric_key(value):
    token = _operating_scalar(value)
    match = re.fullmatch(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)', token)
    factor = dict(_SPICE_SUFFIX, a=1e-18).get(match.group(2).lower(), 1.0)
    return format(float(match.group(1)) * factor, '.17g')


def _operating_string(value):
    """An already evaluated, double-quoted literal; no ADE expression evaluation."""
    if not isinstance(value, str) or not value.strip().startswith('"'):
        raise ValidationError('String operating parameter must be an evaluated double-quoted string')
    try:
        decoded = json.loads(value.strip())
    except (ValueError, TypeError) as exc:
        raise ValidationError('String operating parameter is not a complete double-quoted literal, expressions cannot be executed') from exc
    if not isinstance(decoded, str) or any(ord(char) < 32 or ord(char) == 127 for char in decoded):
        raise ValidationError('String operating parameters cannot contain control characters or newlines')
    return decoded


def replaceable_bias_variables(mapping):
    """Only explicitly mapped DC/PWL roles are regenerated from the VCO period.

    Other strings (including arbitrary model/corner selector names) keep their
    identity. A naming convention alone never makes a parameter replaceable.
    """
    values = {mapping.get(semantic + suffix)
                   for semantic in ('vgs', 'vds', 'vbs')
                   for suffix in ('_variable', '_pwl_variable')
                   if mapping.get(semantic + suffix)}
    if mapping.get('vs_pwl_variable'):
        values.add(mapping['vs_pwl_variable'])
    return sorted(values)


def _root_statements(text):
    """Yield source spans of root-level Spectre statements, including continuations."""
    lines = text.splitlines(keepends=True)
    positions = []
    cursor = 0
    for line in lines:
        positions.append(cursor)
        cursor += len(line)
    index, depth = 0, 0
    while index < len(lines):
        start = index
        while index + 1 < len(lines) and (lines[index].rstrip().endswith('\\') or lines[index + 1].lstrip().startswith('+')):
            index += 1
        statement = ''.join(lines[start:index + 1])
        head = statement.strip()
        if re.match(r"(?i)^(?:inline\s+)?subckt\b", head):
            depth += 1
        elif re.match(r"(?i)^ends\b", head):
            depth -= 1
            if depth < 0:
                raise ValidationError("netlist subckt/ends scope is incomplete")
        elif depth == 0 and head and not head.startswith(('//', '*')):
            yield positions[start], positions[index] + len(lines[index]), statement
        index += 1
    if depth:
        raise ValidationError("netlist subckt/ends scope is incomplete")


def _assignment_spans(statement):
    """Find complete scalar/expression values without truncating nested syntax."""
    assignment = re.compile(r'(?<![\w.])([A-Za-z_]\w*)\s*=\s*')
    cursor = 0
    while cursor < len(statement):
        match = assignment.search(statement, cursor)
        comment = statement.find('//', cursor)
        if not match or (comment >= 0 and comment < match.start()):
            return
        start = match.end()
        index, quote, stack = start, None, []
        while index < len(statement):
            char = statement[index]
            if quote:
                if char == '\\':
                    index += 2
                    continue
                if char == quote:
                    quote = None
            elif char in ('"', "'"):
                quote = char
            elif char in '([{':
                stack.append(char)
            elif char in ')]}':
                if not stack or '([{'.index(stack.pop()) != ')]}'.index(char):
                    raise ValidationError('Unbalanced parentheses in raw operating-parameter expression')
            elif not stack and statement.startswith('//', index):
                break
            elif not stack and (char.isspace() or char == '\\'):
                after = index
                while after < len(statement) and (statement[after].isspace() or statement[after] == '\\'):
                    after += 1
                if after < len(statement) and statement[after] == '+':
                    after += 1
                    while after < len(statement) and statement[after].isspace():
                        after += 1
                if after == len(statement) or assignment.match(statement, after) or statement.startswith('//', after):
                    break
            index += 1
        if quote or stack:
            raise ValidationError('Raw operating-parameter expression is not fully closed')
        end = index
        while end > start and statement[end - 1].isspace():
            end -= 1
        if end == start:
            raise ValidationError('Raw operating-parameter value is empty')
        yield match.group(1), start, end
        cursor = max(index, end)


def restore_operating_point_text(text, operating_point):
    """Pure preview used by preflight and the private-copy writer alike.

    Explicit RDB numbers and quoted string literals are restored. Strings also
    require an already quoted root declaration. No ADE expression is evaluated.
    Unknown parameter types, missing/duplicate declarations and model overrides
    fail closed.
    """
    operating_point = operating_point or {}
    if operating_point.get('ambiguous_params'):
        raise ValidationError("Ambiguous operating parameter: %s" % ', '.join(sorted(operating_point['ambiguous_params'])))
    params = operating_point.get('params', {})
    records = operating_point.get('parameter_records', [])
    changes = []
    for name, value in sorted(params.items()):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', str(name)):
            raise ValidationError("Unsupported operating-parameter name: %s" % name)
        string_value = isinstance(value, str) and value.strip().startswith('"')
        replacement = json.dumps(_operating_string(value), ensure_ascii=False) if string_value else _operating_scalar(value)
        relevant = [r for r in records if str(r.get('name')) == name]
        priorities = [(2 if r.get('value_source') == 'localParameterValue' else
                       1 if r.get('testID') not in (None, '') else 0, r) for r in relevant]
        effective = [r for priority, r in priorities if priority == max((p for p, _ in priorities), default=0)]
        kinds = {str(r.get('type') or 'desVar').lower() for r in effective} or {'desvar'}
        temperature = name.lower() in ('temp', 'temperature', 'tnom') and kinds <= {'temp', 'temperature', 'simulatoroption', 'option'}
        if string_value and temperature:
            raise ValidationError('Temperature operating parameter cannot be a string: ' + name)
        if not temperature and not kinds <= {'desvar', 'designvariable', 'variable', ''}:
            raise ValidationError("Cannot restore operating parameter yet %s type %s; generate separate operating-point netlists" % (name, sorted(kinds)))
        if any(r.get('instance') not in (None, '') for r in effective):
            raise ValidationError("Cannot restore instance-level operating parameter yet %s; generate separate operating-point netlists" % name)
        target_name = 'temp' if temperature and name.lower() == 'temperature' else name
        candidates = []
        option_statements = []
        for start, end, statement in _root_statements(text):
            if temperature:
                wanted = re.match(r'(?i)^\s*(?!saveOptions\b)\S+\s+options\b', statement)
                if wanted:
                    option_statements.append((start, end, statement))
            else:
                wanted = re.match(r'(?i)^\s*parameters\b', statement)
            if wanted:
                candidates.extend((start, end, statement, value_start, value_end)
                                  for field, value_start, value_end in _assignment_spans(statement)
                                  if field == target_name)
        if not candidates and temperature and len(option_statements) == 1:
            start, end, statement = option_statements[0]
            if '//' in statement:
                raise ValidationError('temperature settings in options contains comments without a separate temperature field; specify explicitly temp/tnom declaration')
            updated = statement.rstrip() + ' ' + target_name + '=' + replacement + '\n'
            original = None
        elif len(candidates) == 1:
            start, end, statement, value_start, value_end = candidates[0]
            original = statement[value_start:value_end]
            if string_value:
                # Prove the original netlist parameter is a string too; a
                # quoted RDB value cannot change a numeric parameter's type.
                _operating_string(original)
            elif original.strip().startswith('"'):
                raise ValidationError('Cannot change a declared string operating parameter to a number: ' + name)
            updated = statement[:value_start] + replacement + statement[value_end:]
        else:
            raise ValidationError("Operating parameters %s must have exactly one declaration at the netlist root, found %d items" % (name, len(candidates)))
        text = text[:start] + updated + text[end:]
        changes.append({'name': name, 'netlist_name': target_name, 'original': original,
                        'effective': replacement, 'kind': 'temperature' if temperature else ('design_variable_string' if string_value else 'design_variable'),
                        'source_records': effective})
    return text, changes


def _operating_point_for_run(rdb, run, parameter_values, netlist):
    params, records, ambiguous = _parameters_for_run(parameter_values.get(_id_token(run.get('pointID')), []), run.get('testID'))
    contract = {'schema_version': 1, 'rdb': os.path.realpath(rdb),
                'point_id': run.get('pointID'), 'test_id': run.get('testID'),
                'test': run.get('test'), 'corner': run.get('corner'),
                'params': params, 'parameter_records': records, 'ambiguous_params': ambiguous}
    with open(netlist, encoding='utf-8') as stream:
        preview, changes = restore_operating_point_text(stream.read(), contract)
    contract['restored_values'] = changes
    contract['environment'] = _operating_environment(preview, netlist)
    contract['effective_netlist_sha256'] = hashlib.sha256(preview.encode('utf-8')).hexdigest()
    contract['sha256'] = _operating_hash(contract)
    return contract


def _operating_environment(text, netlist):
    settings, includes = {}, {}
    for _, _, statement in _root_statements(text):
        if re.match(r'(?i)^\s*(?!saveOptions\b)\S+\s+options\b', statement):
            for name, start, end in _assignment_spans(statement):
                if name.lower() in ('temp', 'tnom', 'scale', 'scalem'):
                    if name.lower() in settings:
                        raise ValidationError('Operating point has duplicate simulation temperature/dimension options: ' + name)
                    settings[name.lower()] = _operating_numeric_key(statement[start:end].strip())
        include = re.match(r'^\s*include\s+"([^"]+)"(?:\s+section\s*=\s*(\S+))?', statement)
        if include:
            path = include.group(1)
            if not os.path.isabs(path):
                path = os.path.join(os.path.dirname(netlist), path)
            includes.setdefault(os.path.realpath(path), []).append(include.group(2) or '')
    return {'settings': settings, 'model_sections': {key: sorted(set(value)) for key, value in includes.items()}}


def operating_source_environment(source):
    contract = source.get('operating_point') or {}
    if contract.get('environment') is not None:
        return contract['environment']
    with open(source['path'], encoding='utf-8') as stream:
        text, _ = restore_operating_point_text(stream.read(), contract)
    return _operating_environment(text, source['path'])


def operating_nonbias_parameters(source, bias_variables=()):
    contract = source.get('operating_point') or {}
    result = {}
    for key, value in contract.get('params', {}).items():
        if key in set(bias_variables):
            continue
        if isinstance(value, str) and value.strip().startswith('"'):
            decoded = _operating_string(value)
            # Preserve type: numeric 1 and string "1" are different corners.
            result[key] = {'string': decoded}
        else:
            result[key] = _operating_numeric_key(value)
    return result


def _template_resource_identity(text, netlist, regenerated_strings):
    """Keep directory identity whenever input-resource independence is unproven.

    Native batch points have separate directories. Absolute input literals are
    independent of that directory; relative/expressive inputs are not. Only
    known *output* literals and the native PSF format string are exempted.
    """
    literal = r'"(?:\\.|[^"\\])*"'
    allowed, paths = set(), set()
    uncertain = False
    for start, _, statement in _root_statements(text):
        options = re.match(r'(?i)^\s*\S+\s+options\b', statement)
        dc = re.match(r'(?i)^\s*\S+\s+dc\b', statement)
        for name, value_start, value_end in _assignment_spans(statement):
            if (options and name.lower() in ('psfversion', 'sensfile')) or (dc and name.lower() == 'write'):
                token = statement[value_start:value_end]
                if re.fullmatch(literal, token):
                    _operating_string(token)
                    allowed.add(start + value_start)
    # Preserve character offsets while excluding full-line comments.
    body = re.sub(r'(?m)^\s*(?://|\*)[^\n]*', lambda match: ' ' * len(match.group()), text)
    for directive in re.finditer(r'(?im)^\s*(?:include|ahdl_include|vec_include)\s+', body):
        token = re.match(literal, body[directive.end():])
        if token is None:
            uncertain = True
    for quoted in re.finditer(literal, body):
        if quoted.start() in allowed:
            continue
        try:
            value = _operating_string(quoted.group())
        except ValidationError:
            uncertain = True
            continue
        if not value:
            continue
        if os.path.isabs(value) and not any(char in value for char in ('$','~')):
            paths.add(os.path.realpath(value))
        else:
            uncertain = True
    for assignment in re.finditer(r'(?<![\w.])([A-Za-z_]\w*)\s*=\s*', body):
        name = assignment.group(1).lower()
        if not ('file' in name or 'path' in name or name.startswith('read')):
            continue
        if assignment.end() in allowed:
            continue
        token = re.match(literal + r'|[^\s]+', body[assignment.end():])
        value = token.group() if token else ''
        if value.startswith('"'):
            continue  # The quoted-literal check above retains its identity.
        if name == 'file' and value in regenerated_strings:
            continue  # This mapped PWL file is regenerated from the VCO period.
        uncertain = True
    unquoted = re.sub(literal, '', body)
    if re.search(r"['\"]|\S+/\S+", unquoted):
        # Unknown quoted syntax or unquoted path/expression: do not guess.
        uncertain = True
    return {'absolute_resources': sorted(paths),
            'context_directory': os.path.realpath(os.path.dirname(netlist)) if uncertain else None}


def _template_content_identity(source, bias_variables):
    with open(source['path'], encoding='utf-8') as stream:
        text = stream.read()
    replacements, found, regenerated_strings = [], set(), set()
    for start, _, statement in _root_statements(text):
        if not re.match(r'(?i)^\s*parameters\b', statement):
            continue
        for name, value_start, value_end in _assignment_spans(statement):
            if name not in bias_variables:
                continue
            if name in found:
                raise ValidationError('Replaceable bias variable is declared twice at the netlist root: ' + name)
            found.add(name)
            token = statement[value_start:value_end]
            string = token.strip().startswith('"')
            if string:
                _operating_string(token)
                regenerated_strings.add(name)
            replacements.append((start + value_start, start + value_end, '""' if string else '0'))
    for start, end, placeholder in sorted(replacements, reverse=True):
        text = text[:start] + placeholder + text[end:]
    return {'normalized_netlist_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'resource_identity': _template_resource_identity(text, source['path'], regenerated_strings)}


def operating_template_key(source, bias_variables=()):
    # Exact path/hash remain in history binding and materialization provenance.
    # Equivalence additionally supports native per-point batch netlists, but
    # never ignores devices, analyses, saves, non-bias values or resource scope.
    bias_variables = set(bias_variables)
    value = {'template': _template_content_identity(source, bias_variables),
             'corner': source.get('corner') or '', 'test': source.get('test'),
             'nonbias_parameters': operating_nonbias_parameters(source, bias_variables),
             'environment': operating_source_environment(source)}
    return _operating_hash(value)


def match_flicker_operating_source(sources, vco_source, bias_variables=()):
    """Match only proven corner/parameter/environment contracts, never indices."""
    corner = str(vco_source.get('corner') or '')
    params = operating_nonbias_parameters(vco_source)
    environment = operating_source_environment(vco_source)
    def compatible(source):
        other = operating_nonbias_parameters(source, bias_variables)
        if any(str(params[key]) != str(other[key]) and params[key] != other[key]
               for key in set(params) & set(other)):
            return False
        target = operating_source_environment(source)
        if environment['settings'] != target['settings']:
            return False
        for path in set(environment['model_sections']) & set(target['model_sections']):
            # The current TB may include extra passive/device families, but
            # cannot substitute a different section for a VCO-used family.
            if not set(environment['model_sections'][path]) <= set(target['model_sections'][path]):
                return False
        return True
    exact = [item for item in sources if str(item.get('corner') or '') == corner and compatible(item)]
    generic = [item for item in sources if not str(item.get('corner') or '') and compatible(item)]
    candidates = exact if exact else generic
    if len(candidates) != 1:
        raise ValidationError('flicker Maestro operating points cannot be matched by corner, effective non-bias parameters, temperature and model section uniquely paired; point numbers or list order are not used to guess')
    return candidates[0]


def _history_records(
    maestro_dir, selected_test, include_histories=True, requested_history=None,
    history_catalog=None, explicit_history_selection=False,
):
    if not include_histories:
        return [], None
    root = os.path.join(maestro_dir, "results", "maestro")
    catalog_names = {}
    catalog_paths = {}
    if history_catalog is not None:
        if not isinstance(history_catalog, list) or len(history_catalog) > 512:
            raise ValidationError("history_catalog must be a list of at most 512 records")
        paths = []
        for item in history_catalog:
            if not isinstance(item, Mapping):
                raise ValidationError("history_catalog entries must be objects")
            value = item.get("rdb_file", item.get("path"))
            if not value:
                continue
            if not isinstance(value, str) or not os.path.isabs(value):
                raise ValidationError("history_catalog RDB paths must be absolute")
            path = _path(value)
            if not path.lower().endswith(".rdb"):
                raise ValidationError("history_catalog path must identify an RDB file")
            name = item.get("name") or os.path.basename(path)[:-4]
            if not isinstance(name, str) or not name or len(name) > 16384:
                raise ValidationError("history_catalog name must be a nonempty string")
            if path in catalog_names and catalog_names[path] != name:
                raise ValidationError("history_catalog has conflicting names for one RDB")
            if name in catalog_paths and catalog_paths[name] != path:
                raise ValidationError("history_catalog has conflicting RDB paths for one name")
            catalog_names[path] = name
            catalog_paths[name] = path
            if path not in paths:
                paths.append(path)
    else:
        paths = [
            os.path.join(root, name)
            for name in (os.listdir(root) if os.path.isdir(root) else [])
            if name.lower().endswith(".rdb") and os.path.isfile(os.path.join(root, name))
        ]
        paths.sort(key=lambda path: (os.path.getmtime(path), path), reverse=True)
        paths = paths[:MAX_HISTORIES]
    # A selected native RDB may live outside the default directory or be older
    # than the bounded discovery list.  Its contents still undergo every check.
    unregistered_requested_path = None
    if requested_history:
        requested_text = str(requested_history)
        explicit_paths = [p for p in paths if catalog_names.get(p) == requested_text]
        if os.path.isabs(requested_text):
            explicit_paths = [_path(requested_text)]
            if history_catalog is not None and explicit_paths[0] not in paths:
                unregistered_requested_path = explicit_paths[0]
        elif not explicit_paths and history_catalog is None and os.path.basename(requested_text) == requested_text:
            filename = requested_text if requested_text.lower().endswith(".rdb") else requested_text + ".rdb"
            explicit_paths = [_path(os.path.join(root, filename))]
        for path in reversed(explicit_paths):
            if not path.lower().endswith(".rdb"):
                raise ValidationError("selected history must identify an RDB file")
            paths = [path] + [p for p in paths if p != path]
    records = []
    selected = None
    for path in paths:
        record = {
            "name": catalog_names.get(path, os.path.basename(path)[:-4]),
            "path": os.path.realpath(path),
            "mtime": os.path.getmtime(path) if os.path.isfile(path) else None,
            "tests": [],
            "raw_dirs": [],
            "complete_runs": 0,
            "checked_runs": 0,
            "source_netlists": [],
            "fully_complete": False,
            "stale_raw_dirs": [],
            "error": None,
        }
        try:
            if path == unregistered_requested_path:
                raise DiscoveryError("Selected RDB is not registered in this Maestro context; refresh this setup or choose a manual source")
            record['rdb_sha256'] = _sha256_file(path)
            inspected = inspect_rdb({"path": path, "verify_completion": True})
            _, parameter_values = _read_parameter_tables(path)
            record["tests"] = [row.get("name") for row in inspected.get("tests", [])]
            record["declared_raw_dirs"] = list(inspected.get("rawDir", []))
            record["raw_dirs"] = list(inspected.get("effectiveRawDir") or inspected.get("rawDir", []))
            record["raw_dir_resolutions"] = copy.deepcopy(inspected.get("rawDirResolutions", []))
            completion = inspected.get("completion", {})
            selected_runs = [run for run in inspected.get('runs', [])
                             if not selected_test or run.get('test') == selected_test]
            record["complete_runs"] = sum(bool(run.get('complete')) for run in selected_runs)
            record["checked_runs"] = len(selected_runs)
            record["stale_raw_dirs"] = [
                item for item in record["raw_dirs"] if not os.path.isdir(str(item))
            ]
            for run in selected_runs:
                if not run.get("complete"):
                    continue
                psf_dir = run.get("psfDir")
                if not psf_dir:
                    continue
                netlist = os.path.join(os.path.dirname(psf_dir), "netlist", "input.scs")
                if os.path.isfile(netlist):
                    record["source_netlists"].append(
                        {
                            "path": os.path.realpath(netlist),
                            "point_id": run.get("pointID"),
                            "design_point": run.get("designPointNumber"),
                            "corner": run.get("corner"),
                            "test": run.get("test"),
                            "psf_dir": os.path.realpath(psf_dir),
                            "operating_point": _operating_point_for_run(path, run, parameter_values, netlist),
                        }
                    )
            record["fully_complete"] = bool(
                record["checked_runs"]
                and record["complete_runs"] == record["checked_runs"]
                and len(record["source_netlists"]) == record["complete_runs"]
            )
            if _sha256_file(path) != record['rdb_sha256']:
                raise DiscoveryError('Maestro RDB changed during operating-point inspection')
        except Exception as exc:  # one damaged history must not hide the setup
            record["error"] = str(exc)
            record["fully_complete"] = False
        records.append(record)
        test_matches = not selected_test or selected_test in record["tests"]
        history_matches = True
        if requested_history:
            requested_text = str(requested_history)
            requested_path = _path(requested_text)
            history_matches = bool(
                record["name"] == requested_text
                or (requested_path and record["path"] == requested_path)
            )
        if (
            selected is None
            and (bool(requested_history) or not explicit_history_selection)
            and test_matches
            and history_matches
            and record["fully_complete"]
        ):
            selected = copy.deepcopy(record)
    return records, selected


def _issue(code, severity, title, detail, field=None, action=None):
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "detail": detail,
        "field": field,
        "action": action,
    }


def _mapping_for_role(test, role, offset_hz):
    analyses = _analysis_map(test)
    if role == "vco":
        periodic = _first(
            (analyses.get(name) for name in ("hb", "pss")), {}
        )
        noise = _first(
            (analyses.get(name) for name in ("hbnoise", "pnoise")), {}
        )
        periodic_fields = periodic.get("fields", {}) if periodic else {}
        noise_fields = noise.get("fields", {}) if noise else {}
        return {
            "periodic_analysis": periodic.get("name") if periodic else None,
            "noise_analysis": noise.get("name") if noise else None,
            "output_p": _first((periodic_fields.get("p"), noise_fields.get("p"))),
            "output_n": _first((periodic_fields.get("n"), noise_fields.get("n"))),
            "noise_covers_offset": _analysis_covers(noise, offset_hz),
            "has_transfer_analysis": bool(set(analyses) & _TRANSFER_ANALYSES),
        }
    variable_names = _named_values(test, "variables")
    noise = _first((analyses.get("noise"), analyses.get("hbnoise"), analyses.get("pnoise")), {})
    return {
        "noise_analysis": noise.get("name") if noise else None,
        "vgs_variable": _match_alias(variable_names, _VARIABLE_ALIASES["vgs"]),
        "vds_variable": _match_alias(variable_names, _VARIABLE_ALIASES["vds"]),
        "vbs_variable": _match_alias(variable_names, _VARIABLE_ALIASES["vbs"]),
        "noise_covers_offset": _analysis_covers(noise, offset_hz),
        "noise_output_at_offset": _output_at_offset(test, offset_hz),
        "has_dynamic_small_signal": bool(set(analyses) & _DYNAMIC_SMALL_SIGNAL_ANALYSES),
        "has_periodic_noise": bool(set(analyses) & _PERIODIC_NOISE_ANALYSES),
    }


def inspect_maestro(payload, role=None, include_histories=True):
    """Inspect one Maestro setup and select one test for ``vco``/``flicker``."""

    if isinstance(payload, (str, bytes)):
        options = {"path": payload}
    elif isinstance(payload, Mapping):
        options = dict(payload)
    else:
        raise ValidationError("Maestro inspection payload must be an object or path")
    role = str(role or options.get("role") or "").lower()
    if role == "replica":
        role = "flicker"
    if role not in ("vco", "flicker"):
        raise ValidationError("Maestro role must be vco or flicker")
    maestro_dir = normalize_maestro_dir(options.get("path", options.get("maestro_dir")))
    sdb_path = os.path.join(maestro_dir, "maestro.sdb")
    state_path = os.path.join(maestro_dir, "active.state")
    sdb_root = _parse_xml(sdb_path, "maestro.sdb")
    state_root = _parse_xml(state_path, "active.state") if os.path.isfile(state_path) else None
    tests = _merge_named_records(
        _sdb_test_records(sdb_root), _state_test_records(state_root)
    )
    global_variables = _attach_global_variables(tests, sdb_root)
    requested = options.get("test")
    selected, selection = _select_test(tests, role, requested)
    histories, selected_history = _history_records(
        maestro_dir,
        selected.get("name") if selected else requested,
        include_histories=bool(options.get("include_histories", include_histories)),
        requested_history=options.get("history", options.get("selected_history")),
        history_catalog=options.get("history_catalog"),
        explicit_history_selection=bool(options.get("explicit_history_selection", False)),
    )
    # An explicitly chosen complete history can disambiguate equally plausible
    # setup tests.  Use actual completed per-test netlists, not every test name
    # registered in the RDB, and never infer a history to make this work.
    if (selected is None and not requested and selection == "ambiguous"
            and options.get("history", options.get("selected_history"))
            and selected_history is not None):
        completed_tests = {item.get("test") for item in selected_history.get("source_netlists", [])}
        candidates = [test for test in tests
                      if test.get("name") in completed_tests and _role_score(test, role) > 0]
        if len(candidates) == 1:
            selected, selection = candidates[0], "explicit_history_unique_test"
            histories, selected_history = _history_records(
                maestro_dir, selected.get("name"),
                include_histories=bool(options.get("include_histories", include_histories)),
                requested_history=options.get("history", options.get("selected_history")),
                history_catalog=options.get("history_catalog"),
                explicit_history_selection=bool(options.get("explicit_history_selection", False)),
            )
    history_fingerprint = None
    history_manifest = None
    if selected_history is not None:
        history_fingerprint, history_manifest = _history_binding(selected_history)
        selected_history["history_fingerprint"] = history_fingerprint
        selected_history["history_manifest"] = history_manifest
    fingerprint = None
    binding = None
    mapping = {}
    discovered = {
        "mapping_fields": [], "signal_catalog": [], "topology": {},
        "mapping_issues": [], "mapping_overrides": {},
    }
    if selected is not None:
        fingerprint, binding = _test_binding(selected)
        mapping = _mapping_for_role(
            selected, role, float(options.get("offset_hz", 10000.0))
        )
        discovered = discover_signal_mapping(
            selected, role, selected_history,
            overrides=options.get("mapping_overrides"),
            offset_hz=float(options.get("offset_hz", 10000.0)),
        )
        mapping.update(discovered["mapping"])
        mapping["noise_covers_offset"] = _analysis_covers(
            {"enabled": bool(mapping.get("noise_analysis")),
             "fields": mapping.get("noise_analysis_fields", {})},
            float(options.get("offset_hz", 10000.0)),
        )
    return {
        "schema_version": PAIR_SCHEMA_VERSION,
        "role": role,
        "maestro_dir": maestro_dir,
        "maestro_sdb": sdb_path,
        "active_state": state_path if os.path.isfile(state_path) else None,
        "sdb_version": sdb_root.get("version"),
        "state_version": state_root.get("version") if state_root is not None else None,
        "ic_version": state_root.get("ICVersion") if state_root is not None else None,
        "tests": tests,
        "global_variables": global_variables,
        "test_names": [test.get("name") for test in tests],
        "selected_test": copy.deepcopy(selected),
        "selection": selection,
        "role_score": _role_score(selected, role) if selected else 0,
        "mapping": mapping,
        "mapping_fields": discovered["mapping_fields"],
        "signal_catalog": discovered["signal_catalog"],
        "topology": discovered["topology"],
        "mapping_issues": discovered["mapping_issues"],
        "mapping_overrides": discovered["mapping_overrides"],
        "binding_fingerprint": fingerprint,
        "binding_manifest": binding,
        "files": {
            "maestro_sdb_sha256": _sha256_file(sdb_path),
            "active_state_sha256": _sha256_file(state_path) if os.path.isfile(state_path) else None,
        },
        "histories": histories,
        "selected_history": selected_history,
        "history_fingerprint": history_fingerprint,
        "history_manifest": history_manifest,
        "read_only": True,
    }


def discover_maestro_setups(payload):
    """Find Maestro setup roots without opening OA or following symlinks."""

    if isinstance(payload, (str, bytes)):
        options = {"roots": [payload]}
    elif isinstance(payload, Mapping):
        options = dict(payload)
    else:
        raise ValidationError("Maestro discovery payload must be an object or path")
    roots = options.get("roots", options.get("root", []))
    if isinstance(roots, (str, bytes)):
        roots = [roots]
    if not isinstance(roots, (list, tuple)):
        raise ValidationError("Maestro discovery roots must be a path or list")
    max_depth = max(0, min(int(options.get("max_depth", 6)), 20))
    max_results = max(1, min(int(options.get("max_results", 100)), 1000))
    findings = []
    warnings = []
    for raw_root in roots:
        root = _path(raw_root)
        if not root or not os.path.isdir(root):
            warnings.append("root is not a readable directory: %s" % root)
            continue
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            relative = os.path.relpath(current, root)
            depth = 0 if relative == "." else len(relative.split(os.sep))
            directories[:] = sorted(
                name
                for name in directories
                if not name.startswith(".") and name not in ("results", "simulation")
            )
            if depth >= max_depth:
                directories[:] = []
            if "maestro.sdb" not in files:
                continue
            try:
                base = inspect_maestro(
                    {
                        "path": current,
                        "role": "vco",
                        "include_histories": False,
                    }
                )
                flicker = inspect_maestro(
                    {
                        "path": current,
                        "role": "flicker",
                        "include_histories": False,
                    }
                )
                findings.append(
                    {
                        "path": os.path.realpath(current),
                        "depth": depth,
                        "vco_score": base.get("role_score", 0),
                        "flicker_score": flicker.get("role_score", 0),
                        "tests": base.get("test_names", []),
                        "designs": [
                            test.get("design", {}) for test in base.get("tests", [])
                        ],
                    }
                )
            except Exception as exc:
                warnings.append("%s: %s" % (current, exc))
            if len(findings) >= max_results:
                break
        if len(findings) >= max_results:
            break
    findings.sort(key=lambda item: item["path"])
    return {
        "roots": [_path(root) for root in roots],
        "findings": findings,
        "warnings": warnings,
        "truncated": len(findings) >= max_results,
        "read_only": True,
    }


def _auto_pair_from_findings(findings):
    def choose(role):
        key = "%s_score" % role
        ranked = sorted(findings, key=lambda item: (-int(item.get(key, 0)), item["path"]))
        if not ranked or int(ranked[0].get(key, 0)) <= 0:
            return None
        if len(ranked) > 1 and ranked[1].get(key) == ranked[0].get(key):
            return None
        return ranked[0]["path"]

    vco = choose("vco")
    flicker = choose("flicker")
    if vco and flicker and vco == flicker:
        return None, None
    return vco, flicker


def inspect_maestro_pair(payload):
    """Inspect, map, and change-guard one VCO/flicker Maestro pair."""

    if not isinstance(payload, Mapping):
        raise ValidationError("Maestro pair payload must be an object")
    options = copy.deepcopy(dict(payload))
    testbenches = options.get("testbenches", {})
    if not isinstance(testbenches, Mapping):
        raise ValidationError("testbenches must be an object")
    vco_spec = testbenches.get("vco", {})
    flicker_spec = testbenches.get("flicker", testbenches.get("replica", {}))
    if not isinstance(vco_spec, Mapping):
        vco_spec = {}
    if not isinstance(flicker_spec, Mapping):
        flicker_spec = {}
    from .generated_flicker import replica_source
    source_mode = replica_source(dict(testbenches))
    generated = source_mode == "generated"
    roots = options.get("roots", options.get("search_roots", []))
    if isinstance(roots, (str, bytes)):
        roots = [roots]
    candidates = {"roots": [], "findings": [], "warnings": [], "truncated": False}
    vco_path = vco_spec.get("maestro_dir", vco_spec.get("path"))
    flicker_path = None if generated else flicker_spec.get("maestro_dir", flicker_spec.get("path"))
    if (not vco_path or not generated and not flicker_path) and roots:
        candidates = discover_maestro_setups(
            {
                "roots": roots,
                "max_depth": options.get("max_depth", 6),
                "max_results": options.get("max_results", 100),
            }
        )
        auto_vco, auto_flicker = _auto_pair_from_findings(candidates["findings"])
        vco_path = vco_path or auto_vco
        if not generated:
            flicker_path = flicker_path or auto_flicker

    offset_hz = float(options.get("offset_hz", 10000.0))
    issues = []
    roles = {"vco": None, "flicker": None}
    for role, path, spec in (
        ("vco", vco_path, vco_spec),
        ("flicker", flicker_path, flicker_spec),
    ):
        if generated and role == "flicker":
            continue
        field = "testbenches.%s.maestro_dir" % ("vco" if role == "vco" else "replica")
        if not path:
            issues.append(
                _issue(
                    "ISF-E1201",
                    "error",
                    "%s Maestro Not selected" % ("VCO" if role == "vco" else "Flicker"),
                    "Select existing Maestro directory or containing maestro.sdb of cell Directory. ",
                    field=field,
                    action="manually select the corresponding item on Setup Maestro; the search root is auxiliary only. ",
                )
            )
            continue
        try:
            report = inspect_maestro(
                {
                    "path": path,
                    "role": role,
                    "test": spec.get("test"),
                    "history": spec.get("history", spec.get("selected_history")),
                    "history_catalog": spec.get("history_catalog"),
                    "explicit_history_selection": spec.get("explicit_history_selection", False),
                    "mapping_overrides": spec.get("mapping_overrides"),
                    "offset_hz": offset_hz,
                    "include_histories": options.get("include_histories", True),
                }
            )
            roles[role] = report
        except Exception as exc:
            code = getattr(exc, "code", "ISF-E1201")
            issues.append(
                _issue(
                    code,
                    "error",
                    "%s Maestro Cannot read" % ("VCO" if role == "vco" else "Flicker"),
                    str(exc),
                    field=field,
                    action="confirm the selection is a maestro directory and ensure the current user has read permission. ",
                )
            )
            continue
        for mapping_issue in report.get("mapping_issues", []):
            issues.append(
                _issue(
                    "ISF-E1202" if mapping_issue["severity"] == "error" else "ISF-W1201",
                    mapping_issue["severity"],
                    "%s name mappings need review" % ("VCO" if role == "vco" else "Flicker"),
                    mapping_issue["message"],
                    field="testbenches.%s.mapping_overrides.%s" % (
                        "vco" if role == "vco" else "replica", mapping_issue["key"]),
                    action="expand automatic detection and manual overrides in Advanced settings; leave blank to restore automatic settings, detect again after names change. ",
                )
            )
        if report.get("selected_test") is None:
            issues.append(
                _issue(
                    "ISF-E1202",
                    "error",
                    "%s test Cannot uniquely determine" % ("VCO" if role == "vco" else "Flicker"),
                    "available test: %s" % ", ".join(report.get("test_names", [])),
                    field="testbenches.%s.test" % ("vco" if role == "vco" else "replica"),
                    action="only select manually test; other variables and outputs remain automatically detected. ",
                )
            )
            continue
        expected = spec.get("binding_fingerprint")
        actual = report.get("binding_fingerprint")
        if expected and actual and expected != actual:
            issues.append(
                _issue(
                    "ISF-E1204",
                    "error",
                    "%s Maestro contract changed" % ("VCO" if role == "vco" else "Flicker"),
                    "Variable, Output, analysis, design or model section differs from the previous binding. ",
                    field="testbenches.%s.binding_fingerprint" % ("vco" if role == "vco" else "replica"),
                    action="after reviewing the changes, on Setup explicitly click“Rebind current settings”. ",
                )
            )
        expected_history = spec.get("history_fingerprint")
        actual_history = report.get("history_fingerprint")
        if expected_history and expected_history != actual_history:
            issues.append(
                _issue(
                    "ISF-E1204" if actual_history else "ISF-E1205",
                    "error",
                    "%s Maestro history changed or unavailable"
                    % ("VCO" if role == "vco" else "Flicker"),
                    "Preflight bound RDB/generated netlist differs from current disk contents. ",
                    field="testbenches.%s.history_fingerprint"
                    % ("vco" if role == "vco" else "replica"),
                    action="Detect again; if this is the intended new history, explicitly rebind and start a new job. ",
                )
            )

    vco = roles.get("vco")
    flicker = roles.get("flicker")
    if vco and vco.get("selected_test"):
        mapping = vco.get("mapping", {})
        missing = [
            label
            for label, value in (
                ("HB/PSS", mapping.get("periodic_analysis")),
                ("HBNOISE/PNOISE", mapping.get("noise_analysis")),
                ("output p", mapping.get("output_p")),
                ("output n", mapping.get("output_n")),
                ("core MOS", mapping.get("target_instance") if generated else True),
            )
            if not value
        ]
        if missing:
            issues.append(
                _issue(
                    "ISF-E1202",
                    "error",
                    "VCO Maestro Missing primary workflow capability",
                    "Missing: %s" % ", ".join(missing),
                    field="testbenches.vco",
                    action="in VCO Maestro add periodic analysis in, periodic noise and differential output nodes, then detect again. ",
                )
            )
        elif (
            str(mapping.get("periodic_analysis_type", mapping.get("periodic_analysis", ""))).lower() != "hb"
            or str(mapping.get("noise_analysis_type", mapping.get("noise_analysis", ""))).lower() != "hbnoise"
        ):
            issues.append(
                _issue(
                    "ISF-E1202",
                    "error",
                    "Current bundled runner supports only HB/HBNOISE",
                    "Detected periodic analysis %s, periodic noise %s; PSS/PNOISE can be detected, but execution is unsupported in this version. "
                    % (
                        mapping.get("periodic_analysis") or "none",
                        mapping.get("noise_analysis") or "none",
                    ),
                    field="testbenches.vco.analysis",
                    action="in the existing VCO Maestro enable in HB/HBNOISE, or connect a validated custom implementation in Advanced settings PSS adapter. ",
                )
            )
        if mapping.get("noise_analysis") and not mapping.get("noise_covers_offset"):
            issues.append(
                _issue(
                    "ISF-W1202",
                    "warning",
                    "VCO noise sweep does not cover 10 kHz",
                    "Current %s range excludes %.6g Hz; private runner overrides with the exact frequency. " % (mapping.get("noise_analysis"), offset_hz),
                    field="testbenches.vco.analysis",
                    action="to compare directly Maestro history, add 10 kHz add HBNOISE/PNOISE. ",
                )
            )
        if not mapping.get("has_transfer_analysis"):
            issues.append(
                _issue(
                    "ISF-W1202",
                    "warning",
                    "VCO Maestro not saved HBXF/PXF",
                    "signed transimpedance will be added in the private netlist copy with the same output and time reference. ",
                    action="No changes required to OA; when needed for Maestro history add only when reading directly HBXF/PXF. ",
                )
            )
        if not vco.get("selected_history"):
            stale = any(item.get("stale_raw_dirs") for item in vco.get("histories", []))
            issues.append(
                _issue(
                    "ISF-W1203" if stale else "ISF-W1201",
                    "warning",
                    "VCO executable history unavailable",
                    (
                        "RDB rawDir points to an old machine or local files are missing for point/test PSF and netlist. "
                        if stale
                        else "No history with both completed PSF and generated netlists history. "
                    ),
                    field="testbenches.vco.history",
                    action="on this machine use the selected Maestro run once again, then refresh detection; do not edit manually RDB rawDir. ",
                )
            )

    if flicker and flicker.get("selected_test"):
        mapping = flicker.get("mapping", {})
        missing = [
            label
            for label, value in (
                ("noise", mapping.get("noise_analysis")),
                ("VGS variable", mapping.get("vgs_variable")),
                ("VDS variable", mapping.get("vds_variable")),
            )
            if not value
        ]
        if missing:
            issues.append(
                _issue(
                    "ISF-E1202",
                    "error",
                    "Flicker Maestro Missing primary workflow capability",
                    "Missing: %s" % ", ".join(missing),
                    field="testbenches.replica",
                    action="retain original TB framework, add only VGS/VDS variables and drain-current noise Output. ",
                )
            )
        elif str(mapping.get("noise_analysis_type", mapping.get("noise_analysis", ""))).lower() != "noise":
            issues.append(
                _issue(
                    "ISF-E1202",
                    "error",
                    "Flicker template must retain an independent noise analysis",
                    "Detected %s; this version reads from this noise TB Read 10 kHz absolute magnitude, then generate the dynamic replica in a private copy replica. "
                    % (mapping.get("noise_analysis") or "none"),
                    field="testbenches.replica.analysis",
                    action="in the original flicker Maestro retain a standard noise analysis; no third one needed for TB. ",
                )
            )
        if mapping.get("noise_analysis") and not mapping.get("noise_covers_offset"):
            issues.append(
                _issue(
                    "ISF-E1202",
                    "error",
                    "Flicker noise sweep does not cover 10 kHz",
                    "Current noise range excludes %.6g Hz. " % offset_hz,
                    field="testbenches.replica.analysis",
                    action="set 10 kHz add to existing noise sweep; no new application or extra TB. ",
                )
            )
        if not mapping.get("noise_output_at_offset"):
            issues.append(
                _issue(
                    "ISF-W1201",
                    "warning",
                    "No explicit match for 10 kHz flicker output",
                    "noise analysis can cover this frequency, but output name/expression is not explicitly bound to 10 kHz. ",
                    field="testbenches.replica.mapping.noise_output",
                    action="select an existing output in Advanced mappings, or let the extractor read directly raw noise frequency point. ",
                )
            )
        if not mapping.get("vbs_variable"):
            issues.append(
                _issue(
                    "ISF-W1201",
                    "warning",
                    "Flicker TB not discovered VBS Variable",
                    "by default uses source/bulk assumed equipotential; if VCO in VBS(t) add a mapping if nonzero. ",
                    field="testbenches.replica.mapping.vbs_variable",
                )
            )
        if not mapping.get("has_dynamic_small_signal"):
            issues.append(
                _issue(
                    "ISF-W1202",
                    "info",
                    "HBAC/PAC not configured",
                    "the primary method still uses HBNOISE AM/PM Candidates; 10 kHz deterministic injection is only a secondary validation. ",
                    action="only when secondary phase validation is needed, from private replica added to netlist HBAC/PAC. ",
                )
            )
        if not flicker.get("selected_history"):
            stale = any(
                item.get("stale_raw_dirs") for item in flicker.get("histories", [])
            )
            issues.append(
                _issue(
                    "ISF-W1203" if stale else "ISF-W1201",
                    "warning",
                    "Flicker executable history unavailable",
                    (
                        "RDB rawDir points to an old machine or local files are missing for generated netlist/PSF. "
                        if stale
                        else "No history with both generated netlists and completed results flicker history. "
                    ),
                    field="testbenches.replica.history",
                    action="run the selected setup on this machine flicker Maestro; Live use its netlist as DC-noise/replica template. ",
                )
            )

    pair = {
        "replica_source": source_mode,
        "same_setup": False,
        "model_roots": {"vco": [], "flicker": []},
        "model_overlap": None,
        "pdk": None,
    }
    if vco and flicker and vco.get("selected_test") and flicker.get("selected_test"):
        pair["same_setup"] = vco.get("maestro_dir") == flicker.get("maestro_dir")
        if pair["same_setup"]:
            issues.append(
                _issue(
                    "ISF-E1203",
                    "error",
                    "Both roles are bound to the same Maestro",
                    "VCO and flicker-noise must come from two independent TB. ",
                    field="testbenches",
                )
            )
        for role, report in (("vco", vco), ("flicker", flicker)):
            roots_for_role = sorted(
                set(
                    _model_root(item.get("path"))
                    for item in report["selected_test"].get("models", [])
                    if item.get("path")
                )
            )
            pair["model_roots"][role] = [item for item in roots_for_role if item]
        vco_roots = set(pair["model_roots"]["vco"])
        flicker_roots = set(pair["model_roots"]["flicker"])
        pair["model_overlap"] = bool(vco_roots & flicker_roots) if vco_roots and flicker_roots else None
        if pair["model_overlap"] is False:
            issues.append(
                _issue(
                    "ISF-E1203",
                    "error",
                    "two Maestro of PDK/model inconsistent",
                    "VCO=%s; Flicker=%s" % (sorted(vco_roots), sorted(flicker_roots)),
                    field="testbenches",
                    action="select again from the same PDK under flicker-noise Maestro. ",
                )
            )
        common = sorted(vco_roots & flicker_roots)
        if common:
            pair["pdk"] = {
                "root": common[0],
                "name": os.path.basename(common[0]),
                "source": "maestro-pair-models",
            }

    if generated and vco and vco.get("selected_test"):
        model_roots = sorted({
            root for item in vco["selected_test"].get("models", [])
            for root in [_model_root(item.get("path"))] if root
        })
        pair["model_roots"]["vco"] = model_roots
        if len(model_roots) == 1:
            pair["pdk"] = {"root": model_roots[0], "name": os.path.basename(model_roots[0]),
                           "source": "maestro-vco-models"}

    if vco and flicker:
        vhistory, fhistory = vco.get('selected_history'), flicker.get('selected_history')
        if vhistory and fhistory:
            try:
                mapping = flicker.get('mapping') or {}
                bias = replaceable_bias_variables(mapping)
                templates = {}
                for source in fhistory.get('source_netlists', []):
                    templates.setdefault(operating_template_key(source, bias), source)
                pair['operating_points'] = []
                for source in vhistory.get('source_netlists', []):
                    matched = match_flicker_operating_source(list(templates.values()), source, bias)
                    pair['operating_points'].append({'vco': source.get('operating_point'),
                        'flicker_template_key': operating_template_key(matched, bias),
                        'flicker_nonbias_parameters': operating_nonbias_parameters(matched, bias)})
            except (ValidationError, OSError, ValueError) as exc:
                issues.append(_issue('ISF-E1203', 'error', 'two TB effective operating points cannot be safely matched', str(exc), field='testbenches'))

    errors = sum(1 for issue in issues if issue["severity"] == "error")
    warnings = sum(1 for issue in issues if issue["severity"] == "warning")
    infos = sum(1 for issue in issues if issue["severity"] == "info")
    bindings = {}
    for role, report in roles.items():
        if report and report.get("selected_test"):
            bindings[role] = {
                "maestro_dir": report.get("maestro_dir"),
                "test": report["selected_test"].get("name"),
                "binding_fingerprint": report.get("binding_fingerprint"),
                "mapping": copy.deepcopy(report.get("mapping", {})),
                "mapping_overrides": copy.deepcopy(report.get("mapping_overrides", {})),
                "history": (
                    report.get("selected_history", {}).get("path")
                    if report.get("selected_history")
                    else None
                ),
                "history_fingerprint": report.get("history_fingerprint"),
                "history_manifest": copy.deepcopy(report.get("history_manifest")),
            }
    return {
        "schema_version": PAIR_SCHEMA_VERSION,
        "ok": errors == 0,
        "status": "fail" if errors else ("warn" if warnings else "pass"),
        "offset_hz": offset_hz,
        "summary": {"error": errors, "warning": warnings, "info": infos},
        "roles": roles,
        "cross_tb_mapping": cross_tb_mapping(vco, flicker),
        "pair": pair,
        "bindings": bindings,
        "issues": issues,
        "candidates": candidates,
        "inspected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "read_only": True,
    }


__all__ = [
    "PAIR_SCHEMA_VERSION",
    "discover_maestro_setups",
    "inspect_maestro",
    "inspect_maestro_pair",
    "normalize_maestro_dir",
]
