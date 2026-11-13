"""Derive a private four-terminal noise fixture from a frozen VCO point."""

import json
import re
from pathlib import Path

from .errors import ValidationError


DC_MAPPING = {
    "dc_instance": "M_ISF_DC",
    "dc_sense_source": "V_ISF_DC_SENSE",
    "dc_transducer": "H_ISF_DC_READOUT",
    "output_p": "ISF_DC_NOISE_OUT",
    "output_n": "0",
    "noise_analysis": "noise",
    "vgs_variable": "ISF_DC_VGS",
    "vds_variable": "ISF_DC_VDS",
    "vbs_variable": "ISF_DC_VBS",
    "vs_variable": "ISF_DC_VS",
    "vgs_source": "V_ISF_DC_G",
    "vds_source": "V_ISF_DC_D",
    "vbs_source": "V_ISF_DC_B",
}
FLICKER_SCOPE = "selected_device_with_symmetric_power_estimate"
_MAX_NETLIST = 32 * 1024 * 1024
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[a-zA-Z]*")
_INCLUDE = re.compile(
    r'(?i)^(include|ahdl_include)\s+"([^"\r\n]+)"'
    r'(?:\s+section\s*=\s*([^\s/]+))?\s*(?://.*)?$'
)
_SECTIONS = re.compile(r"(?im)^[ \t]*(endsection|section)\b[ \t]*(\S*)[^\n]*")
_SUBCKT = re.compile(r"(?im)^[ \t]*(?:inline[ \t]+)?subckt[ \t]+([^\s(]+)\b")


def replica_source(testbenches):
    if not isinstance(testbenches, dict):
        return "maestro"
    spec = testbenches.get("replica", {})
    if not isinstance(spec, dict) or "source" not in spec:
        spec = testbenches.get("flicker", {})
    source = spec.get("source") if isinstance(spec, dict) else None
    if source in (None, "", "maestro"):
        return "maestro"
    if source == "generated":
        return source
    raise ValidationError("Flicker Testbench source must be generated or maestro")


def _read(path):
    if not path.is_file() or path.stat().st_size > _MAX_NETLIST:
        raise ValidationError("Netlist or model is unreadable or exceeds the automatic detection size limit: " + str(path))
    return path.read_text(encoding="utf-8", errors="replace")


def _include(statement, parent):
    match = _INCLUDE.fullmatch(statement.strip())
    if not match:
        raise ValidationError("Generated replica requires literal paths and explicit models section: " + statement[:160])
    kind, path, section = match.groups()
    path = (parent / Path(path).expanduser()).resolve()
    if not path.is_file():
        raise ValidationError("Selected VCO operating point include unreadable: " + str(path))
    return kind, path, section


def _section_index(text, path):
    spans = list(_SECTIONS.finditer(text))
    if not spans:
        return {None: [(0, len(text))]}
    ranges, start, name = {None: []}, 0, None
    for marker in spans:
        kind, token = marker.groups()
        if kind.lower() == "section":
            if name is not None:
                raise ValidationError("Model section not closed: " + str(path))
            ranges[None].append((start, marker.start()))
            start, name = marker.end(), token
        else:
            if name is None or token and token != name:
                raise ValidationError("Model section boundary mismatch: " + str(path))
            ranges.setdefault(name, []).append((start, marker.start()))
            start, name = marker.end(), None
    if name is not None:
        raise ValidationError("Model section not closed: " + str(path))
    ranges[None].append((start, len(text)))
    return ranges


def active_subckt(path, section, name, read_cache=None):
    """Read exactly the definition that the selected include section activates."""
    path = Path(path)
    if read_cache is None:
        read_cache = {}
    if path not in read_cache:
        read_cache[path] = _read(path)
    index_key = (path, "sections")
    text = read_cache[path]
    if index_key not in read_cache:
        read_cache[index_key] = _section_index(text, path)
    spans = read_cache[index_key].get(section, [])
    if section is not None and len(spans) != 1:
        raise ValidationError("Cannot uniquely locate model section: {} in {}".format(section, path))
    active = "\n".join(text[start:end] for start, end in spans)
    block_pattern = r"(?ims)^[ \t]*(?:inline[ \t]+)?subckt[ \t]+" + re.escape(name)
    block_pattern += r"(?=\s|\().*?^[ \t]*ends(?:[ \t]+" + re.escape(name) + r")?[ \t]*(?:$|\n)"
    blocks = re.findall(block_pattern, active)
    names = [match.group(1) for match in _SUBCKT.finditer(active)]
    if names.count(name) > 1 or (name in names and len(blocks) != 1):
        raise ValidationError("active model section of MOS definition is duplicated or not closed: " + name)
    return active, blocks[0] if blocks else None


def model_definition(includes, model, read_cache=None):
    """Walk only the literal include branches activated by the bound VCO point."""
    from .signal_mapping import _lines

    if read_cache is None:
        read_cache = {}
    queue = [(path, section, path, 0) for kind, path, section in includes if kind == "include"]
    visited, matches = set(), {}
    for path, section, root, depth in queue:
        identity = (path, section)
        if identity in visited:
            continue
        if len(visited) >= 160 or depth >= 12:
            raise ValidationError("Selected VCO Model include chain exceeds safe parsing scope")
        visited.add(identity)
        active, block = active_subckt(path, section, model, read_cache)
        if block:
            matches[identity] = (root, block)
        for match in re.finditer(r"(?im)^[ \t]*include\b[^\n]*", active):
            end = match.end()
            while active[match.start():end].rstrip().endswith("\\") and end < len(active):
                line_end = active.find("\n", end + 1)
                end = len(active) if line_end < 0 else line_end
            line = _lines(active[match.start():end])[0]
            _, child, child_section = _include(line, path.parent)
            queue.append((child, child_section, root, depth + 1))
    if len(matches) != 1:
        raise ValidationError("Cannot uniquely confirm the four terminals in the selected operating point’s active model MOS definition: " + model)
    (owner, section), (root, block) = next(iter(matches.items()))
    header = _lines(block)[0]
    pins = re.search(r"\(([^)]*)\)", header)
    outer_pins = pins.group(1).lower().split() if pins else []
    if not _ordered_mos_pins(outer_pins, _lines(block)[1:]):
        raise ValidationError("Cannot prove this PDK MOS external terminal order is D G S B: " + model)
    return {
        "model_file": str(root),
        "device_subckt_file": str(owner),
        "device_subckt_section": section,
    }, block


def _ordered_mos_pins(outer_pins, body):
    if outer_pins == ["d", "g", "s", "b"]:
        return True
    if len(outer_pins) != 4 or len(set(outer_pins)) != 4:
        return False
    edges, intrinsic = {}, []
    for line in body:
        match = re.match(r"^\S+\s*\(([^)]*)\)\s+(\S+)(.*)", line.lower())
        if not match:
            continue
        pin_text, model, tail = match.groups()
        pins = pin_text.split()
        if len(pins) == 2 and model in ("resistor", "inductor"):
            a, b = pins
            edges.setdefault(a, set()).add(b)
            edges.setdefault(b, set()).add(a)
        elif len(pins) == 4 and re.search(r"\bl\s*=", tail):
            intrinsic.append(pins)
    if len(intrinsic) != 1:
        return False
    for outer, inner in zip(outer_pins, intrinsic[0]):
        pending, connected = [inner], set()
        while pending:
            node = pending.pop()
            if node not in connected:
                connected.add(node)
                pending.extend(edges.get(node, ()))
        if connected.intersection(outer_pins) != {outer}:
            return False
    return True


def generated_fixture(source, target_instance, offset_hz, *, text=None, read_cache=None):
    """Prepare the exact DC template and model identity without writing files."""
    from .maestro_pair import _assignment_spans, _root_statements
    from .signal_mapping import _lines, inspect_history_topology

    source = Path(source).resolve()
    text = _read(source) if text is None else text
    topology = inspect_history_topology({"source_netlists": [{"path": str(source)}]})
    devices = [item for item in topology["devices"] if item["name"] == target_instance]
    if len(devices) != 1 or len(devices[0]["terminals"]) != 4:
        raise ValidationError("Selected VCO No unique four-terminal device in the operating point MOS instance: " + str(target_instance))
    target = devices[0]
    model, tail = target["model"], target["parameter_text"]
    if not tail or not list(_assignment_spans(tail)):
        raise ValidationError("Selected MOS no verifiable instance parameters provided: " + target_instance)
    if target["scope"]:
        values = [tail[begin:end] for _, begin, end in _assignment_spans(tail)]
        if any(not _NUMBER.fullmatch(value) for value in values):
            raise ValidationError("hierarchy MOS contains unevaluated parameter expressions, use existing Maestro noise TB: " + target_instance)
        ancestor_names = [".".join(target_instance.split(".")[:index])
                          for index in range(1, target_instance.count(".") + 1)]
        parents = [item for item in topology["devices"] if item["name"] in ancestor_names]
        if len(parents) != len(ancestor_names) or any(
            any(name.lower() == "m" and value not in ("1", "1.0")
                for name, value in parent["parameters"].items())
            for parent in parents
        ) or any(
            parameter["scope"] in {parent["model"] for parent in parents}
            and parameter["name"].lower() == "m" for parameter in topology["parameters"]
        ):
            raise ValidationError("Cannot preserve hierarchy MOS inherited instance multiplier parameters: " + target_instance)

    statements = list(_root_statements(text))
    parameters, options, includes, include_lines = [], [], [], []
    parameter_names = set()
    for _, _, statement in statements:
        if re.match(r"(?i)^\s*parameters\b", statement):
            parameters.append(statement.strip())
            for name, _, _ in _assignment_spans(statement):
                if name.lower() in parameter_names:
                    raise ValidationError("VCO duplicate root parameter: " + name)
                parameter_names.add(name.lower())
        elif re.match(r"(?i)^\s*(?:include|ahdl_include)\b", statement):
            kind, path, section = _include(_lines(statement)[0], source.parent)
            includes.append((kind, path, section))
            include_lines.append(kind + " " + json.dumps(str(path), ensure_ascii=False)
                                 + (" section=" + section if section else ""))
        elif re.match(r"(?i)^\s*\S+\s+options\b", statement):
            if re.match(r"(?i)^\s*saveOptions\s+", statement):
                save_keys = {"save", "pwr", "subcktoppoint", "currents", "nestlvl", "useprobes"}
                if {name.lower() for name, _, _ in _assignment_spans(statement)} <= save_keys:
                    continue
            options.append(statement.strip())
    if len(options) != 1:
        raise ValidationError("Selected VCO Operating point lacks a unique root-level simulator options, cannot inherit temperature and model options")
    reserved = {key.lower() for key in DC_MAPPING.values() if key.startswith("ISF_DC_")}
    reserved.update("isf_{}_pwl_file".format(name) for name in ("vgs", "vds", "vbs", "vs"))
    reserved.update(("isf_f0", "isf_t0"))
    if parameter_names & reserved:
        raise ValidationError("VCO parameter conflicts with a reserved generated-replica name: " + ", ".join(sorted(parameter_names & reserved)))

    evidence, model_block = model_definition(includes, model, read_cache=read_cache)
    evidence.update({"target_instance": target_instance, "model": model, "device_parameters": tail})
    mapping = DC_MAPPING
    fixture = [
        "// ISF generated single-MOS noise fixture",
        "simulator lang=spectre", "global 0",
        *parameters,
        "parameters " + " ".join(mapping[name + "_variable"] + "=0" for name in ("vgs", "vds", "vbs", "vs")),
        *include_lines,
        "{} (G S) vsource dc={} type=dc".format(mapping["vgs_source"], mapping["vgs_variable"]),
        "{} (DEXT S) vsource dc={} type=dc".format(mapping["vds_source"], mapping["vds_variable"]),
        "{} (B S) vsource dc={} type=dc".format(mapping["vbs_source"], mapping["vbs_variable"]),
        "V_ISF_DC_S (S 0) vsource dc={} type=dc".format(mapping["vs_variable"]),
        "{} (DEXT D) vsource dc=0 type=dc".format(mapping["dc_sense_source"]),
        "{} (D G S B) {} {}".format(mapping["dc_instance"], model, tail),
        "{} ({} 0) ccvs rm=1 type=ccvs probe={}".format(
            mapping["dc_transducer"], mapping["output_p"], mapping["dc_sense_source"]),
        *options,
        "dcOp dc maxiters=150 maxsteps=10000 annotate=status",
        "noise ({} 0) noise sweeptype=absolute values=[{:.16e}] annotate=status".format(
            mapping["output_p"], float(offset_hz)),
        "modelParameter info what=models where=rawfile",
        "element info what=inst where=rawfile",
        "designParamVals info what=parameters where=rawfile",
        "saveOptions options save=allpub subcktoppoint=yes",
        "",
    ]
    return "\n".join(fixture), evidence, model_block
