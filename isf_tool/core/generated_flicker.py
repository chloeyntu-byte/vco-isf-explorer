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
    from .spectre_topology import SpectreTopology
    tree = SpectreTopology(Path(parent) / '__isf_include__.scs', text=statement, includes=False)
    if len(tree.includes) != 1:
        raise ValidationError('literal include required: ' + statement)
    item = tree.includes[0]
    path = Path(item.include_path)
    if not path.is_file():
        raise ValidationError('Cannot read the include for the selected VCO operating point:' + str(path))
    return item.kind, path, item.parameters.get('section')


def active_subckt(path, section, name, read_cache=None):
    from .spectre_topology import SpectreTopology, Definition
    tree = SpectreTopology(path, section=section, read_cache=read_cache)
    definition = tree.lookup(name)
    active = "\n".join(i.text for i in tree.items if i.source == str(Path(path).resolve()))
    return active, tree.definition_text(definition) if isinstance(definition, Definition) else None


def model_definition(includes, model, read_cache=None):
    from .spectre_topology import SpectreTopology, Definition, MOS_TYPES
    text = "\n".join(kind + " " + json.dumps(str(path)) + (" section=" + section if section else "")
                     for kind, path, section in includes)
    tree = SpectreTopology(text=text, read_cache=read_cache)
    definition = tree.lookup(model)
    if definition is None and model.lower() not in MOS_TYPES:
        raise ValidationError("unproven MOS definition: " + model)
    pins = definition.ports if isinstance(definition, Definition) else ['d', 'g', 's', 'b']
    # Resolve through the same tree instead of an independent model regex.
    probe_text = "ISF_MODEL_PROBE (" + " ".join(pins) + ") " + model
    from .spectre_topology import statements
    item = statements(probe_text)[0]
    item.kind, item.name, item.model, item.pins, item.scope = 'instance', 'ISF_MODEL_PROBE', model, pins, tree.root
    tree.items.append(item)
    tree._items_by_span[(item.source,item.start)] = item
    tree.root.instances.append(item)
    row = tree.resolve_instance('ISF_MODEL_PROBE')
    identity = tree.mos_identity(row)
    owner = identity['definition_file']
    root = next((str(path) for _, path, _ in includes), owner)
    return dict(model_file=root, device_subckt_file=owner,
                device_subckt_section=identity['definition_section'], mos=identity), (tree.definition_text(definition) if isinstance(definition, Definition) else '')


def _ordered_mos_pins(outer_pins, body):
    # Compatibility helper: without model TYPE evidence no order is proven.
    from .spectre_topology import SpectreTopology
    text = 'subckt ISF_CHECK (' + ' '.join(outer_pins) + ')\n' + '\n'.join(body) + '\nends ISF_CHECK\nX (' + ' '.join(outer_pins) + ') ISF_CHECK\n'
    try:
        tree = SpectreTopology(text=text)
        return tree.mos_identity(tree.resolve_instance('X'))['terminal_order'] == [0, 1, 2, 3]
    except ValidationError:
        return False


def generated_fixture(source, target_instance, offset_hz, *, text=None, read_cache=None, topology=None, destination=None):
    """Derive a fixture from the exact effective tree and proven MOS binding."""
    from .maestro_pair import _assignment_spans, _root_statements
    from .signal_mapping import _lines
    from .spectre_topology import SpectreTopology, Definition, evaluate

    source = Path(source).resolve()
    text = _read(source) if text is None else text
    tree = topology or SpectreTopology(source, text=text, read_cache=read_cache)
    target = tree.resolve_instance(target_instance)
    identity = tree.mos_identity(target)
    extra_globals = (tree.globals - {'0'}) & set(identity.get('occupied_names', []))
    if extra_globals:
        raise ValidationError('unsupported model global bias nodes: ' + ', '.join(sorted(extra_globals)))
    model = target['model']
    params = dict(target['effective_parameters'])
    unresolved = [key + '=' + str(value) for key, value in params.items()
                  if not isinstance(value, (float, int))]
    inherited = target.get('inherited_m', 1)
    if not isinstance(inherited, (float, int)):
        unresolved.append(str(inherited))
    if unresolved:
        context = [key + '=' + str(value) for key, value in target.get('scope_parameters', {}).items()
                   if not isinstance(value, (float, int))]
        raise ValidationError('unresolved effective parameters for {}: {}{}'.format(
            target_instance, ', '.join(unresolved),
            '; enclosing parameters: ' + ', '.join(context) if context else ''))
    if inherited != 1:
        params['m'] = params.get('m', 1) * inherited
    parts = []
    for key, value in params.items():
        original = target['parameters'].get(key, '')
        literal = bool(_NUMBER.fullmatch(original)) and (key != 'm' or inherited == 1)
        parts.append(key + '=' + (original if literal else format(value, '.16g')))
    tail = ' '.join(parts)
    if not tail:
        # A primitive can legitimately rely on its model defaults.
        tail = ''

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

    definition = tree.lookup(model, tree.statement_for(target).scope)
    model_block = tree.exported_model_text(target)
    owner = identity['definition_file']
    # Keep the first active include entry as the model root (legacy field).
    # Full include closure/hashes are retained independently.
    model_root = str(includes[0][1]) if includes else owner
    evidence = dict(model_file=model_root, device_subckt_file=owner,
                    device_subckt_section=identity['definition_section'],
                    target_instance=target['name'], model=model, device_parameters=tail,
                    mos=identity, effective_parameters=params,
                    dependency_hashes=tree.file_hashes)
    local_definitions = []
    if definition is not None and not isinstance(definition, Definition) and definition.scope is not tree.root:
        raise ValidationError('unsupported scoped primitive model {}: select its four-terminal model wrapper to retain lexical model parameters'.format(model))
    if owner == str(source):
        # Local model definitions can reference sibling models/subckts. Keep
        # their declaration closure, without copying any top-level stimulus.
        local_definitions.extend(tree.files[d.source][d.start:d.end] for d in tree.root.models.values() if d.source == str(source))
        local_definitions.extend(tree.relocated_definition_text(d) for d in tree.root.definitions.values() if d.header.source == str(source))
    if isinstance(definition, Definition) and definition.parent is not tree.root:
        root_definition = tree.lookup(model)
        if root_definition is not None:
            raise ValidationError('unsupported lexical model promotion: {} shadows a root definition; select an enclosing four-terminal wrapper'.format(model))
        local_definitions.append(model_block)
    order = identity['terminal_order']
    connection = [None] * 4
    for role, index in zip(('D', 'G', 'S', 'B'), order):
        connection[index] = role
    mapping = DC_MAPPING
    fixture = [
        "// ISF generated single-MOS noise fixture",
        "simulator lang=spectre", "global 0",
        *parameters,
        "parameters " + " ".join(mapping[name + "_variable"] + "=0" for name in ("vgs", "vds", "vbs", "vs")),
        *include_lines,
        *local_definitions,
        "{} (G S) vsource dc={} type=dc".format(mapping["vgs_source"], mapping["vgs_variable"]),
        "{} (DEXT S) vsource dc={} type=dc".format(mapping["vds_source"], mapping["vds_variable"]),
        "{} (B S) vsource dc={} type=dc".format(mapping["vbs_source"], mapping["vbs_variable"]),
        "V_ISF_DC_S (S 0) vsource dc={} type=dc".format(mapping["vs_variable"]),
        "{} (DEXT D) vsource dc=0 type=dc".format(mapping["dc_sense_source"]),
        "{} ({}) {} {}".format(mapping["dc_instance"], " ".join(connection), model, tail),
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
    fixture_text = "\n".join(fixture)
    # A fixture is successful only if its root MOS resolves with the same
    # effective geometry. Catch lost lexical declarations before setup_ok.
    private = SpectreTopology(destination or Path('/__isf_private_fixture__/input.scs'), text=fixture_text, read_cache=read_cache)
    copied = private.resolve_instance(DC_MAPPING['dc_instance'])
    copied_identity = private.mos_identity(copied)
    expected = tree.effective_mos_parameters(target, identity)
    actual = private.effective_mos_parameters(copied, copied_identity)
    for key, value in expected.items():
        if value is not None and not isinstance(value, (float, int)):
            intrinsic_path = target['name'] + ('.' + identity['intrinsic_path'] if identity['intrinsic_path'] else '')
            intrinsic = tree.resolve_instance(intrinsic_path)
            context = ', '.join(k + '=' + str(v) for k, v in intrinsic.get('scope_parameters', {}).items()
                                if not isinstance(v, (float, int)))
            raise ValidationError('unresolved effective {}={} for {}{}'.format(
                key, value, target['name'], '; enclosing parameters: ' + context if context else ''))
        if value != actual.get(key):
            if isinstance(value, (float, int)) and isinstance(actual.get(key), (float, int)) and abs(value - actual[key]) <= max(abs(value), abs(actual[key]), 1e-30) * 1e-12:
                continue
            raise ValidationError('fixture lost effective {} for {}: {} -> {}'.format(key, target['name'], value, actual.get(key)))
    from .private_validation import validate_mos_copy, write_private_netlist
    validate_mos_copy(tree, target, private, DC_MAPPING['dc_instance'])
    if destination is not None:
        write_private_netlist(destination, fixture_text, sources=tree.files, candidate=private,
                              validate=lambda copied: validate_mos_copy(tree, target, copied, DC_MAPPING['dc_instance']))
    return fixture_text, evidence, model_block
