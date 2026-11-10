"""Versioned physical contract shared by preflight and the Cadence engine.

The built-in profile is deliberately synthetic.  Real PDK/design topology is
supplied as an external profile so public source never needs foundry-derived
instance, model, node, or sizing data.
"""

from __future__ import unicode_literals

import copy
import hashlib
import io
import json
import math
import os
import re

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover
    from collections import Mapping

from .errors import ProfileError, ValidationError
from .limits import MAX_PHASE_CANDIDATE_HARMONICS
from .source_paths import VCO_NETLIST_RELATIVE, resolve_source_path


PHYSICAL_PROFILE_SCHEMA_VERSION = 1

LEGACY_PHYSICAL_PROFILE = {
    "schema_version": PHYSICAL_PROFILE_SCHEMA_VERSION,
    "profile_id": "synthetic-cross-coupled-vco-v1",
    "main": {
        "target_instance": "M1",
        "device_subckt": "generic_nmos",
        "terminals": {
            "drain": "VOUTP",
            "gate": "VOUTN",
            "source": "VCOMMON",
            "bulk": "VCOMMON",
        },
        "output": {"positive": "VOUTP", "negative": "VOUTN"},
        "drain_probe": {"node": "M1D", "source": "V_M1D"},
        "injection": {
            "source": "I_HBXF_M1",
            "positive": "VOUTP",
            "negative": "VCOMMON",
        },
        "traces": {
            "gate": "VOUTN",
            "drain": "VOUTP",
            "source": "VCOMMON",
            "bulk": "VCOMMON",
            "current": "V_M1D:p",
            "target_noise": "M1.device",
            "peer_noise": "M0.device",
        },
    },
    "replica": {
        "instance": "MREP",
        "original_subckt": "generic_nmos",
        "generated_subckt": "generic_nmos_dynprobe",
        "device_parameters": "l=1u w=10u m=1",
        "model_sections": ["typical"],
        "sense_source": "V_SENSE",
        "noise_trace": "MREP.device",
        "intrinsic": {
            "instance": "device",
            "primitive": "generic_nmos_primitive",
            "terminals": ["d", "g", "s", "b"],
            "injection_source": "I_INJECT",
        },
    },
    "contracts": {
        "current_direction": "drain_to_source",
        "carrier_amplitude": "differential_peak",
        "output_noise": "PM_SSB",
        "source_noise_quantity": "PSD_A2_per_Hz",
        "canonical_noise_quantity": "ASD_A_per_sqrt_Hz",
        "common_source_normalization": "Mdyn[k]=Dk*sqrt_Sxi",
        "time_origin": "shared_unshifted_VCO_period",
        "manual_waveform_shift": False,
    },
}

REQUIRED_ENGINE_KEYS = (
    "source_root",
    "points",
    "model_file",
    "spectre",
    "psf",
    "offset_hz",
    "max_harmonic",
    "hb_max_harmonic",
    "main_max_sideband",
)

REQUIRED_CONTRACTS = copy.deepcopy(LEGACY_PHYSICAL_PROFILE["contracts"])


def _real_path(value, base=None):
    if value in (None, ""):
        return None
    try:
        value = os.fspath(value)
    except (AttributeError, TypeError):
        return None
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, str):
        return None
    value = os.path.expanduser(value)
    if not os.path.isabs(value):
        value = os.path.join(base or os.getcwd(), value)
    return os.path.realpath(os.path.abspath(value))


def _read_json(path, label):
    try:
        with io.open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (IOError, OSError, ValueError) as exc:
        raise ProfileError(
            "cannot load %s: %s" % (label, exc),
            details={"path": path, "label": label},
        )
    if not isinstance(value, Mapping):
        raise ProfileError(
            "%s root must be an object" % label,
            details={"path": path, "label": label},
        )
    return copy.deepcopy(dict(value))


def load_engine_config(path):
    path = _real_path(path)
    if not path or not os.path.isfile(path):
        raise ProfileError(
            "Cadence engine config does not exist",
            details={"path": path},
        )
    return _read_json(path, "Cadence engine config")


def _require_mapping(value, label):
    if not isinstance(value, Mapping):
        raise ProfileError("%s must be an object" % label)
    return dict(value)


def _require_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ProfileError("%s must be a non-empty string" % label)
    return value.strip()


def validate_physical_profile(profile):
    profile = _require_mapping(profile, "physical_profile")
    if profile.get("schema_version") != PHYSICAL_PROFILE_SCHEMA_VERSION:
        raise ProfileError(
            "unsupported physical profile schema",
            details={
                "actual": profile.get("schema_version"),
                "supported": PHYSICAL_PROFILE_SCHEMA_VERSION,
            },
        )
    _require_text(profile.get("profile_id"), "physical_profile.profile_id")
    main = _require_mapping(profile.get("main"), "physical_profile.main")
    replica = _require_mapping(
        profile.get("replica"), "physical_profile.replica"
    )
    terminals = _require_mapping(
        main.get("terminals"), "physical_profile.main.terminals"
    )
    output = _require_mapping(main.get("output"), "physical_profile.main.output")
    probe = _require_mapping(
        main.get("drain_probe"), "physical_profile.main.drain_probe"
    )
    injection = _require_mapping(
        main.get("injection"), "physical_profile.main.injection"
    )
    traces = _require_mapping(
        main.get("traces"), "physical_profile.main.traces"
    )
    intrinsic = _require_mapping(
        replica.get("intrinsic"), "physical_profile.replica.intrinsic"
    )
    for label, value in (
        ("main.target_instance", main.get("target_instance")),
        ("main.device_subckt", main.get("device_subckt")),
        ("main.output.positive", output.get("positive")),
        ("main.output.negative", output.get("negative")),
        ("main.drain_probe.node", probe.get("node")),
        ("main.drain_probe.source", probe.get("source")),
        ("main.injection.source", injection.get("source")),
        ("main.injection.positive", injection.get("positive")),
        ("main.injection.negative", injection.get("negative")),
        ("replica.instance", replica.get("instance")),
        ("replica.original_subckt", replica.get("original_subckt")),
        ("replica.generated_subckt", replica.get("generated_subckt")),
        ("replica.device_parameters", replica.get("device_parameters")),
        ("replica.sense_source", replica.get("sense_source")),
        ("replica.noise_trace", replica.get("noise_trace")),
        ("replica.intrinsic.instance", intrinsic.get("instance")),
        ("replica.intrinsic.primitive", intrinsic.get("primitive")),
        ("replica.intrinsic.injection_source", intrinsic.get("injection_source")),
    ):
        _require_text(value, "physical_profile.%s" % label)
    for name in ("drain", "gate", "source", "bulk"):
        _require_text(
            terminals.get(name),
            "physical_profile.main.terminals.%s" % name,
        )
        _require_text(
            traces.get(name),
            "physical_profile.main.traces.%s" % name,
        )
    resolved_terminals = main.get("resolved_terminals")
    if resolved_terminals is not None:
        resolved_terminals = _require_mapping(
            resolved_terminals, "physical_profile.main.resolved_terminals"
        )
        if set(resolved_terminals) != {"drain", "gate", "source", "bulk"}:
            raise ProfileError("resolved_terminals must contain exactly drain, gate, source and bulk")
        _require_text(main.get("instance_path"), "physical_profile.main.instance_path")
        for name in ("drain", "gate", "source", "bulk"):
            _require_text(resolved_terminals[name], "physical_profile.main.resolved_terminals.%s" % name)
    required_traces = ("current", "target_noise")
    if profile.get("replica_source") != "generated":
        required_traces += ("peer_noise",)
    for name in required_traces:
        _require_text(
            traces.get(name), "physical_profile.main.traces.%s" % name
        )
    model_sections = replica.get("model_sections")
    if (
        not isinstance(model_sections, list)
        or any(not isinstance(item, str) or not item for item in model_sections)
    ):
        raise ProfileError(
            "physical_profile.replica.model_sections must be a string list"
        )
    if main.get("instance_path") not in (None, ""):
        _require_text(
            main.get("instance_path"),
            "physical_profile.main.instance_path",
        )
    if probe.get("trace") not in (None, ""):
        _require_text(
            probe.get("trace"),
            "physical_profile.main.drain_probe.trace",
        )
    if injection.get("trace") not in (None, ""):
        _require_text(
            injection.get("trace"),
            "physical_profile.main.injection.trace",
        )
    if traces.get("dc_noise") not in (None, ""):
        _require_text(
            traces.get("dc_noise"),
            "physical_profile.main.traces.dc_noise",
        )
    if replica.get("dc_instance") not in (None, ""):
        _require_text(
            replica.get("dc_instance"),
            "physical_profile.replica.dc_instance",
        )
    if replica.get("dc_sense_source") not in (None, ""):
        _require_text(
            replica.get("dc_sense_source"),
            "physical_profile.replica.dc_sense_source",
        )
        for name in (
            "dc_sense_drain_terminal",
            "dc_transducer",
            "dc_noise_output",
        ):
            _require_text(
                replica.get(name),
                "physical_profile.replica.%s" % name,
            )
        if replica.get("dc_sense_drain_terminal") != "negative":
            raise ProfileError(
                "physical_profile.replica.dc_sense_drain_terminal must be negative",
                details={
                    "reason": (
                        "the canonical drain-to-source current is positive only "
                        "when the zero-volt sense source negative terminal is "
                        "connected to the MOS drain"
                    )
                },
            )
    intrinsic_terminals = intrinsic.get("terminals")
    if (
        not isinstance(intrinsic_terminals, list)
        or len(intrinsic_terminals) != 4
        or any(not isinstance(item, str) or not item for item in intrinsic_terminals)
    ):
        raise ProfileError(
            "physical_profile.replica.intrinsic.terminals must contain four nodes"
        )
    contracts = _require_mapping(
        profile.get("contracts"), "physical_profile.contracts"
    )
    wrong = {}
    for key, expected in REQUIRED_CONTRACTS.items():
        if contracts.get(key) != expected:
            wrong[key] = {"expected": expected, "actual": contracts.get(key)}
    if wrong:
        raise ProfileError(
            "physical profile normalization contract is inconsistent",
            details={"contracts": wrong},
        )

    semantic = {}
    terminal_trace_mismatches = {}
    instance_path = str(main.get("instance_path") or "")
    parent_path = instance_path.rsplit(".", 1)[0] if "." in instance_path else ""
    for name in ("drain", "gate", "source", "bulk"):
        terminal = str(terminals[name]).lower()
        trace = str(traces[name]).lower()
        hierarchical_terminal = (
            "%s.%s" % (parent_path.lower(), terminal)
            if parent_path
            else None
        )
        allowed_traces = ((str(resolved_terminals[name]).lower(),)
                          if resolved_terminals is not None
                          else (terminal, hierarchical_terminal))
        if trace not in allowed_traces:
            terminal_trace_mismatches[name] = {
                "terminal": terminals[name],
                "trace": traces[name],
                "allowed_hierarchical_trace": hierarchical_terminal,
                "resolved_terminal": resolved_terminals.get(name) if resolved_terminals is not None else None,
            }
    if terminal_trace_mismatches:
        semantic["terminal_traces"] = terminal_trace_mismatches
    if str(output["positive"]).lower() == str(output["negative"]).lower():
        semantic["output"] = "positive and negative outputs must differ"
    resolved_drain = resolved_terminals["drain"] if resolved_terminals is not None else terminals["drain"]
    if str(resolved_drain).lower() not in (
        str(output["positive"]).lower(),
        str(output["negative"]).lower(),
    ):
        semantic["drain_output"] = {
            "drain": resolved_drain,
            "output": output,
        }
    if (
        str(injection["positive"]).lower()
        != str(terminals["drain"]).lower()
        or str(injection["negative"]).lower()
        != str(terminals["source"]).lower()
    ):
        semantic["injection_direction"] = {
            "expected_positive": terminals["drain"],
            "expected_negative": terminals["source"],
            "actual_positive": injection["positive"],
            "actual_negative": injection["negative"],
        }
    if str(probe["node"]).lower() in set(
        str(value).lower() for value in terminals.values()
    ):
        semantic["drain_probe_node"] = (
            "probe node must be a new node distinct from device terminals"
        )
    expected_current_trace = probe.get("trace", "%s:p" % probe["source"])
    if str(traces["current"]).lower() != expected_current_trace.lower():
        semantic["drain_probe_trace"] = {
            "expected": expected_current_trace,
            "actual": traces["current"],
        }
    target_prefix = "%s." % main.get(
        "instance_path", main["target_instance"]
    )
    inline_alias = intrinsic.get("inline_alias", False)
    if not isinstance(inline_alias, bool):
        raise ProfileError("physical_profile.replica.intrinsic.inline_alias must be boolean")
    target_trace_valid = (str(traces["target_noise"]).lower() == target_prefix[:-1].lower()
                          if inline_alias else str(traces["target_noise"]).lower().startswith(target_prefix.lower()))
    if not target_trace_valid:
        semantic["target_noise_trace"] = {
            "expected_prefix": target_prefix,
            "actual": traces["target_noise"],
        }
    if injection.get("trace"):
        expected_injection_trace = (
            "%s.%s" % (parent_path, injection["source"])
            if parent_path
            else injection["source"]
        )
        if str(injection["trace"]).lower() != expected_injection_trace.lower():
            semantic["injection_trace"] = {
                "expected": expected_injection_trace,
                "actual": injection["trace"],
            }
    replica_prefix = "%s." % replica["instance"]
    if not str(replica["noise_trace"]).lower().startswith(
        replica_prefix.lower()
    ):
        semantic["replica_noise_trace"] = {
            "expected_prefix": replica_prefix,
            "actual": replica["noise_trace"],
        }
    if traces.get("dc_noise"):
        dc_prefix = "%s." % replica.get("dc_instance", "M1")
        dc_trace_valid = (str(traces["dc_noise"]).lower() == dc_prefix[:-1].lower()
                          if inline_alias else str(traces["dc_noise"]).lower().startswith(dc_prefix.lower()))
        if not dc_trace_valid:
            semantic["dc_noise_trace"] = {
                "expected_prefix": dc_prefix,
                "actual": traces["dc_noise"],
            }
    if str(replica["original_subckt"]).lower() != str(
        main["device_subckt"]
    ).lower():
        semantic["replica_subckt"] = {
            "main": main["device_subckt"],
            "replica": replica["original_subckt"],
        }
    if len(set(str(item).lower() for item in intrinsic_terminals)) != 4:
        semantic["intrinsic_terminals"] = "intrinsic terminals must be unique"
    if str(intrinsic["injection_source"]).lower() == str(
        replica["sense_source"]
    ).lower():
        semantic["replica_sources"] = (
            "deterministic injection and drain sense sources must differ"
        )
    if semantic:
        raise ProfileError(
            "physical profile topology is internally inconsistent",
            details={"topology": semantic},
        )
    return copy.deepcopy(profile)


def _read_text(path):
    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as stream:
            return stream.read()
    except (IOError, OSError):
        return ""


def main_instance_path(profile):
    """Return the PSF hierarchy path, defaulting to the netlist-local name."""

    main = profile["main"]
    return main.get("instance_path", main["target_instance"])


def drain_probe_trace(profile):
    """Return the saved PSF current trace for the inserted drain probe."""

    probe = profile["main"]["drain_probe"]
    return probe.get("trace", "%s:p" % probe["source"])


def main_injection_trace(profile):
    """Return the HBXF source trace, including hierarchy when injected locally."""

    injection = profile["main"]["injection"]
    return injection.get("trace", injection["source"])


def dc_noise_trace(profile):
    """Return the standalone DC-sample contributor trace.

    The default is inferred from the replica contributor suffix, so a new PDK
    normally does not need another UI or Profile field.
    """

    traces = profile["main"]["traces"]
    if traces.get("dc_noise"):
        return traces["dc_noise"]
    replica = profile["replica"]
    suffix = str(replica["noise_trace"]).split(".", 1)
    if len(suffix) != 2 or not suffix[1]:
        raise ProfileError(
            "cannot infer standalone DC-noise trace from replica.noise_trace"
        )
    return "%s.%s" % (replica.get("dc_instance", "M1"), suffix[1])


def _include_paths(path):
    """Resolve literal Spectre include targets without changing PDK files."""

    text = _read_text(path)
    parent = os.path.dirname(path)
    output = []
    for match in re.finditer(
        r'(?im)^\s*include\s+["\']([^"\']+)["\']', text
    ):
        value = os.path.expandvars(os.path.expanduser(match.group(1)))
        if not os.path.isabs(value):
            value = os.path.join(parent, value)
        value = os.path.realpath(os.path.abspath(value))
        if value not in output:
            output.append(value)
    return output


def _contains_subckt(path, name):
    return bool(
        re.search(
            r"(?im)^\s*(?:inline\s+)?subckt\s+{}\b".format(re.escape(name)),
            _read_text(path),
        )
    )


def resolve_device_subckt_file(model_file, profile, configured=None):
    """Find the file that owns the replica MOS subckt.

    Many PDKs keep corner sections in one ``.lib`` and include the device
    subcircuits from another file.  A manual ``device_subckt_file`` remains an
    escape hatch, but the normal path follows literal include directives
    automatically (bounded to keep preflight lightweight).
    """

    if configured not in (None, ""):
        return _real_path(configured)
    model_file = _real_path(model_file)
    if not model_file or not os.path.isfile(model_file):
        return None
    target = profile["replica"]["original_subckt"]
    pending = [(model_file, 0)]
    visited = set()
    while pending and len(visited) < 128:
        path, depth = pending.pop(0)
        if path in visited or not os.path.isfile(path):
            continue
        visited.add(path)
        if _contains_subckt(path, target):
            return path
        if depth >= 5:
            continue
        pending.extend(
            (child, depth + 1)
            for child in _include_paths(path)
            if child not in visited
        )
    return None


def _normalized_net(value):
    value = str(value or "").strip()
    while value.startswith("/"):
        value = value[1:]
    return value


def _netlist_semantic_errors(
    path, profile, require_private_supplements=True, vco_mapping=None
):
    text = _read_text(path)
    main = profile["main"]
    terminals = main["terminals"]
    instance = main["target_instance"]
    subckt = main["device_subckt"]
    injection = main["injection"]["source"]
    terminal_pattern = r"\s+".join(
        re.escape(terminals[name])
        for name in ("drain", "gate", "source", "bulk")
    )
    checks = [
        (
            "target_instance",
            r"(?im)^\s*{}\s*\(\s*{}\s*\)\s+{}\b".format(
                re.escape(instance),
                terminal_pattern,
                re.escape(subckt),
            ),
            "target instance/terminal/subckt signature is absent",
        ),
        ("hb", r"(?im)^\s*{}\s*(?:\(|\b)".format(re.escape(str((vco_mapping or {}).get("periodic_analysis") or "hb"))), "HB analysis is absent"),
        (
            "hbnoise",
            r"(?im)^\s*{}\s*(?:\(|\b)".format(re.escape(str((vco_mapping or {}).get("noise_analysis") or "hbnoise"))),
            "HBNOISE analysis is absent",
        ),
    ]
    if require_private_supplements:
        checks.extend(
            [
                (
                    "injection_source",
                    r"(?im)^\s*{}\s*\(\s*[^\s()]+\s+[^\s()]+\s*\)\s+isource\b".format(
                        re.escape(injection)
                    ),
                    "configured HBXF injection source is absent",
                ),
                (
                    "hbxf",
                    r"(?im)^\s*hbxf\s*(?:\(|\b)",
                    "HBXF analysis is absent",
                ),
            ]
        )
    errors = [
        {"field": field, "message": message, "path": path}
        for field, pattern, message in checks
        if not re.search(pattern, text)
    ]
    if main.get("resolved_terminals") is not None:
        # A profile's alias claim alone is not proof. Re-expand the exact bound
        # generated netlist, using the same bounded local-include reader as
        # discovery; external PDK/install files are never traversed here.
        from .signal_mapping import inspect_history_topology
        topology = inspect_history_topology({"source_netlists": [{"path": path}]})
        target_path = str(main.get("instance_path") or "")
        matches = [record for record in topology.get("devices", [])
                   if str(record.get("name", "")).lower() == target_path.lower()]
        if len(matches) != 1 or matches[0].get("ambiguous"):
            errors.append({"field": "physical_profile.main.resolved_terminals",
                           "message": "resolved terminal evidence requires one exact expanded target instance",
                           "instance_path": target_path, "matches": len(matches), "path": path})
        else:
            record = matches[0]
            expected_local = [str(terminals[pin]).lower() for pin in ("drain", "gate", "source", "bulk")]
            actual_local = [str(node).lower() for node in record.get("local_terminals", [])]
            expected_resolved = [str(main["resolved_terminals"][pin]).lower() for pin in ("drain", "gate", "source", "bulk")]
            actual_resolved = [str(node).lower() for node in record.get("terminals", [])]
            if (record.get("local_name", "").lower() != str(instance).lower()
                    or record.get("model", "").lower() != str(subckt).lower()
                    or expected_local != actual_local
                    or expected_resolved != actual_resolved):
                errors.append({"field": "physical_profile.main.resolved_terminals",
                               "message": "resolved terminal evidence disagrees with the bound target's local pins or expanded nets",
                               "instance_path": target_path,
                               "expected_local": expected_local, "actual_local": actual_local,
                               "expected_resolved": expected_resolved, "actual_resolved": actual_resolved,
                               "path": path})
    target_pattern = checks[0][1]
    target_count = len(re.findall(target_pattern, text))
    if target_count > 1:
        errors.append(
            {
                "field": "target_instance",
                "message": "target instance/terminal/subckt signature is ambiguous",
                "path": path,
                "count": target_count,
            }
        )
    mapping = dict(vco_mapping or {})
    mapped_p = _normalized_net(mapping.get("output_p"))
    mapped_n = _normalized_net(mapping.get("output_n"))
    if mapped_p or mapped_n:
        if not mapped_p or not mapped_n or mapped_p.lower() == mapped_n.lower():
            errors.append(
                {
                    "field": "testbenches.vco.mapping.output",
                    "message": "bound VCO differential output mapping is incomplete",
                    "output_p": mapping.get("output_p"),
                    "output_n": mapping.get("output_n"),
                    "path": path,
                }
            )
        else:
            for label, node in (("output_p", mapped_p), ("output_n", mapped_n)):
                if not re.search(
                    r"(?i)(?<![A-Za-z0-9_]){}(?![A-Za-z0-9_])".format(
                        re.escape(node)
                    ),
                    text,
                ):
                    errors.append(
                        {
                            "field": "testbenches.vco.mapping.%s" % label,
                            "message": "mapped VCO output node is absent from generated netlist",
                            "node": node,
                            "path": path,
                        }
                    )
            terminal_nodes = set(
                _normalized_net(value).lower()
                for value in terminals.values()
            )
            resolved_drain = (main.get("resolved_terminals") or terminals)["drain"]
            if _normalized_net(mapping.get("drain") or resolved_drain).lower() not in (
                mapped_p.lower(),
                mapped_n.lower(),
            ):
                errors.append(
                    {
                        "field": "physical_profile.main.terminals.drain",
                        "message": "target drain is not on either bound differential output",
                        "drain": terminals["drain"],
                        "output_p": mapped_p,
                        "output_n": mapped_n,
                        "path": path,
                    }
                )
    return errors


def _flicker_netlist_semantic_errors(path, profile, mapping):
    """Check that the selected flicker history can be patched, not recreated."""

    from .maestro_pair import _root_statements
    # The main MOS/sense/readout patcher operates on root statements. Local
    # names reused inside any subcircuit are not duplicate root devices.
    text = "\n".join(statement for _, _, statement in _root_statements(_read_text(path)))
    replica = profile["replica"]
    instance = replica.get("dc_instance", "M1")
    subckt = replica["original_subckt"]
    errors = []
    device_pattern = r"(?im)^\s*{}\s*\(\s*([^\n)]*)\)\s+{}\b".format(
        re.escape(instance), re.escape(subckt)
    )
    device_matches = re.findall(device_pattern, text)
    device_count = len(device_matches)
    device_nodes = (
        re.findall(r"[^\s]+", device_matches[0])
        if device_count == 1
        else []
    )
    device_drain = device_nodes[0] if len(device_nodes) >= 4 else None
    if device_count != 1:
        errors.append(
            {
                "field": "testbenches.replica.history.netlist",
                "message": "target flicker device/subckt signature is not unique",
                "instance": instance,
                "subckt": subckt,
                "count": device_count,
                "path": path,
            }
        )
    dc_sense_source = replica.get("dc_sense_source")
    if dc_sense_source:
        sense_pattern = r"(?im)^\s*{}\s*\(\s*([^\n)]*)\)\s+(vsource|isource)\b([^\n]*)".format(
            re.escape(str(dc_sense_source))
        )
        sense_matches = re.findall(sense_pattern, text)
        sense_count = len(sense_matches)
        if sense_count != 1:
            errors.append(
                {
                    "field": "physical_profile.replica.dc_sense_source",
                    "message": "flicker drain-current sense source is not unique",
                    "source": dc_sense_source,
                    "count": sense_count,
                    "path": path,
                }
            )
        else:
            sense_nodes = re.findall(r"[^\s]+", sense_matches[0][0])
            sense_kind = sense_matches[0][1].lower()
            sense_parameters = sense_matches[0][2]
            if len(sense_nodes) != 2:
                errors.append(
                    {
                        "field": "physical_profile.replica.dc_sense_source",
                        "message": "flicker drain-current sense source must have exactly two terminals",
                        "source": dc_sense_source,
                        "nodes": sense_nodes,
                        "path": path,
                    }
                )
            elif device_drain and sense_nodes[1].lower() != device_drain.lower():
                errors.append(
                    {
                        "field": "physical_profile.replica.dc_sense_drain_terminal",
                        "message": "sense-source negative terminal is not connected to the target MOS drain",
                        "source": dc_sense_source,
                        "expected_drain": device_drain,
                        "positive": sense_nodes[0],
                        "negative": sense_nodes[1],
                        "path": path,
                    }
                )
            if sense_kind != "vsource" or not re.search(
                r"(?i)(?<![A-Za-z0-9_])dc\s*=\s*0(?:\.0*)?(?![A-Za-z0-9_.])",
                sense_parameters,
            ):
                errors.append(
                    {
                        "field": "physical_profile.replica.dc_sense_source",
                        "message": "flicker drain-current probe must be a zero-volt vsource",
                        "source": dc_sense_source,
                        "kind": sense_kind,
                        "parameters": sense_parameters.strip(),
                        "path": path,
                    }
                )

        transducer = replica.get("dc_transducer")
        noise_output = _normalized_net(replica.get("dc_noise_output"))
        transducer_pattern = r"(?im)^\s*{}\s*\(\s*([^\n)]*)\)\s+ccvs\b([^\n]*)".format(
            re.escape(str(transducer))
        )
        transducer_matches = re.findall(transducer_pattern, text)
        if len(transducer_matches) != 1:
            errors.append(
                {
                    "field": "physical_profile.replica.dc_transducer",
                    "message": "flicker current-to-voltage transducer is not unique",
                    "transducer": transducer,
                    "count": len(transducer_matches),
                    "path": path,
                }
            )
        else:
            transducer_nodes = re.findall(r"[^\s]+", transducer_matches[0][0])
            transducer_parameters = transducer_matches[0][1]
            probe_match = re.search(
                r"(?i)(?<![A-Za-z0-9_])probe\s*=\s*([^\s]+)",
                transducer_parameters,
            )
            probe_name = probe_match.group(1) if probe_match else None
            if (
                len(transducer_nodes) != 2
                or not noise_output
                or transducer_nodes[0].lower() != noise_output.lower()
                or transducer_nodes[1] != str(replica.get("dc_noise_reference", "0"))
                or not probe_name
                or probe_name.lower() != str(dc_sense_source).lower()
                or not re.search(
                    r"(?i)(?<![A-Za-z0-9_])rm\s*=\s*1(?:\.0*)?(?![A-Za-z0-9_.])",
                    transducer_parameters,
                )
            ):
                errors.append(
                    {
                        "field": "physical_profile.replica.dc_transducer",
                        "message": "CCVS must map the audited sense source to the audited noise output with rm=1",
                        "transducer": transducer,
                        "nodes": transducer_nodes,
                        "probe": probe_name,
                        "expected_probe": dc_sense_source,
                        "expected_output": noise_output,
                        "parameters": transducer_parameters.strip(),
                        "path": path,
                    }
                )
    mapping = dict(mapping or {})
    for logical in ("vgs_variable", "vds_variable"):
        variable = mapping.get(logical)
        if not variable or not re.search(
            r"(?i)(?<![A-Za-z0-9_]){}\s*=".format(re.escape(str(variable))),
            text,
        ):
            errors.append(
                {
                    "field": "testbenches.replica.mapping.%s" % logical,
                    "message": "mapped flicker variable is absent from generated netlist",
                    "variable": variable,
                    "path": path,
                }
            )
    vbs_variable = mapping.get("vbs_variable")
    if vbs_variable and not re.search(
        r"(?i)(?<![A-Za-z0-9_]){}\s*=".format(re.escape(str(vbs_variable))),
        text,
    ):
        errors.append(
            {
                "field": "testbenches.replica.mapping.vbs_variable",
                "message": "mapped VBS variable is absent from generated netlist",
                "variable": vbs_variable,
                "path": path,
            }
        )
    analysis = mapping.get("noise_analysis") or "noise"
    analysis_pattern = r"(?im)^\s*{}\s*\(\s*([^\n)]*)\)\s+noise\b".format(
        re.escape(str(analysis))
    )
    analysis_matches = re.findall(analysis_pattern, text)
    if not analysis_matches:
        errors.append(
            {
                "field": "testbenches.replica.mapping.noise_analysis",
                "message": "mapped noise analysis is absent from generated netlist",
                "analysis": analysis,
                "path": path,
            }
        )
    elif dc_sense_source:
        analysis_nodes = re.findall(r"[^\s]+", analysis_matches[0])
        noise_output = _normalized_net(replica.get("dc_noise_output"))
        if (
            len(analysis_nodes) != 2
            or analysis_nodes[0].lower() != noise_output.lower()
            or analysis_nodes[1] != str(replica.get("dc_noise_reference", "0"))
        ):
            errors.append(
                {
                    "field": "testbenches.replica.mapping.noise_output",
                    "message": "mapped noise analysis does not measure the audited CCVS output",
                    "analysis": analysis,
                    "nodes": analysis_nodes,
                    "expected_output": noise_output,
                    "path": path,
                }
            )
    includes = _include_paths(path)
    if not includes:
        errors.append(
            {
                "field": "testbenches.replica.history.netlist",
                "message": "flicker generated netlist has no literal model include",
                "path": path,
            }
        )
    for include_path in includes:
        if not os.path.isfile(include_path):
            errors.append(
                {
                    "field": "testbenches.replica.history.netlist",
                    "message": "flicker generated netlist include is absent",
                    "path": include_path,
                    "source": path,
                }
            )
    return errors


def _model_semantic_errors(path, profile, device_subckt_path=None, device_subckt_section=None):
    text = _read_text(path)
    replica = profile["replica"]
    intrinsic = replica["intrinsic"]
    original = replica["original_subckt"]
    errors = []
    for section in replica["model_sections"]:
        if not re.search(
            r"(?im)^\s*section\s+{}\b".format(re.escape(section)), text
        ):
            errors.append(
                {
                    "field": "replica.model_sections",
                    "message": "model section is absent",
                    "section": section,
                    "path": path,
                }
            )
    subckt_path = device_subckt_path or path
    if profile.get("replica_source") == "generated":
        from .generated_flicker import active_subckt
        try:
            _, block = active_subckt(subckt_path, device_subckt_section, original)
        except (OSError, ValueError, ValidationError) as exc:
            errors.append({"field": "replica.original_subckt", "message": str(exc), "path": subckt_path})
            return errors
    else:
        match = re.search(
            r"(?ims)^\s*(?:inline\s+)?subckt\s+{}\b.*?^\s*ends\s+{}\s*$".format(
                re.escape(original), re.escape(original)
            ),
            _read_text(subckt_path),
        )
        block = match.group(0) if match else None
    if block is None:
        errors.append(
            {
                "field": "replica.original_subckt",
                "message": "device subckt is absent from the resolved model include chain",
                "subckt": original,
                "path": subckt_path,
            }
        )
        return errors
    actual_inline_alias = (bool(re.match(r"(?i)^\s*inline\s+subckt\b", block))
                           and intrinsic["instance"].lower() == original.lower())
    if bool(intrinsic.get("inline_alias", False)) != actual_inline_alias:
        errors.append({"field": "replica.intrinsic.inline_alias",
                       "message": "inline trace alias does not match the native model declaration",
                       "path": subckt_path})
    terminal_pattern = r"\s+".join(
        re.escape(item) for item in intrinsic["terminals"]
    )
    if not re.search(
        r"(?im)^\s*{}\s*\(\s*{}\s*\)\s+{}\b".format(
            re.escape(intrinsic["instance"]),
            terminal_pattern,
            re.escape(intrinsic["primitive"]),
        ),
        block,
    ):
        errors.append(
            {
                "field": "replica.intrinsic",
                "message": "intrinsic source location is absent from device subckt",
                "instance": intrinsic["instance"],
                "primitive": intrinsic["primitive"],
                "path": subckt_path,
            }
        )
    return errors


def resolve_physical_profile(engine_config, config_path=None, allow_legacy=True):
    engine_config = _require_mapping(engine_config, "engine config")
    config_dir = (
        os.path.dirname(_real_path(config_path))
        if config_path not in (None, "")
        else os.getcwd()
    )
    explicit = True
    source = "embedded"
    profile = engine_config.get("physical_profile")
    profile_path = engine_config.get("physical_profile_path")
    if profile is None and profile_path not in (None, ""):
        resolved_path = _real_path(profile_path, config_dir)
        profile = _read_json(resolved_path, "physical profile")
        source = resolved_path
    elif profile is None:
        if not allow_legacy:
            raise ProfileError(
                "live execution requires an explicit physical_profile",
                details={"field": "physical_profile"},
            )
        profile = copy.deepcopy(LEGACY_PHYSICAL_PROFILE)
        explicit = False
        source = "legacy-built-in"
    profile = validate_physical_profile(profile)
    encoded = json.dumps(
        profile,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "profile": profile,
        "explicit": explicit,
        "source": source,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def inspect_engine_contract(
    engine_config,
    config_path=None,
    app_config=None,
    pdk=None,
    require_explicit=False,
    check_paths=False,
    source_from_maestro=False,
    maestro_source_netlists=None,
    maestro_vco_mapping=None,
    maestro_flicker_netlists=None,
    maestro_flicker_mapping=None,
):
    """Return a non-throwing physical-contract report for preflight."""

    errors = []
    warnings = []
    engine_config = dict(engine_config or {})
    required_keys = [
        key
        for key in REQUIRED_ENGINE_KEYS
        if not (source_from_maestro and key in ("source_root", "points"))
    ]
    missing = [key for key in required_keys if engine_config.get(key) in (None, "")]
    if missing:
        errors.append(
            {"field": "config_path", "message": "missing engine keys", "keys": missing}
        )
    try:
        harmonic_count = int(engine_config.get("max_harmonic"))
        if harmonic_count > MAX_PHASE_CANDIDATE_HARMONICS:
            errors.append(
                {
                    "field": "max_harmonic",
                    "message": (
                        "exhaustive phase-candidate search is limited to "
                        "%d harmonics" % MAX_PHASE_CANDIDATE_HARMONICS
                    ),
                    "actual": harmonic_count,
                    "maximum": MAX_PHASE_CANDIDATE_HARMONICS,
                }
            )
    except (TypeError, ValueError):
        pass
    profile_info = None
    try:
        profile_info = resolve_physical_profile(
            engine_config,
            config_path=config_path,
            allow_legacy=not require_explicit,
        )
    except ProfileError as exc:
        errors.append(
            {
                "field": "physical_profile",
                "message": exc.message,
                "details": exc.details,
            }
        )
    if profile_info is not None:
        profile = profile_info["profile"]
        main = profile["main"]
        replica = profile["replica"]
        expected_aliases = {
            "target_device": main["target_instance"],
            "main_injection_source": main["injection"]["source"],
            "target_noise_trace": main["traces"]["target_noise"],
            "replica_noise_trace": replica["noise_trace"],
            "source_sense": replica["sense_source"],
        }
        mismatches = {}
        for key, expected in expected_aliases.items():
            actual = engine_config.get(key)
            if actual not in (None, "", expected):
                mismatches[key] = {"expected": expected, "actual": actual}
        if mismatches:
            errors.append(
                {
                    "field": "physical_profile",
                    "message": "engine aliases disagree with physical profile",
                    "values": mismatches,
                }
            )
        if not profile_info["explicit"]:
            warnings.append(
                {
                    "field": "physical_profile",
                    "message": "using legacy built-in topology; replay only",
                }
            )

    app_config = dict(app_config or {})
    analysis = app_config.get("analysis", {})
    if isinstance(analysis, Mapping):
        offset = analysis.get("offset", {})
        expected_offset = (
            offset.get("hz") if isinstance(offset, Mapping) else None
        )
        try:
            actual_offset = float(engine_config.get("offset_hz"))
            expected_offset = float(expected_offset)
            if not math.isclose(
                actual_offset, expected_offset, rel_tol=1.0e-12, abs_tol=1.0e-9
            ):
                errors.append(
                    {
                        "field": "analysis.offset",
                        "message": "UI and engine offsets differ",
                        "expected": expected_offset,
                        "actual": actual_offset,
                    }
                )
        except (TypeError, ValueError):
            pass
        try:
            actual_harmonic = int(engine_config.get("max_harmonic"))
            expected_harmonic = int(analysis.get("max_harmonic"))
            if actual_harmonic != expected_harmonic:
                errors.append(
                    {
                        "field": "analysis.max_harmonic",
                        "message": "UI and engine harmonic limits differ",
                        "expected": expected_harmonic,
                        "actual": actual_harmonic,
                    }
                )
        except (TypeError, ValueError):
            pass

    base = os.path.dirname(_real_path(config_path)) if config_path else os.getcwd()
    path_fields = (
        "source_root",
        "model_file",
        "device_subckt_file",
        "cadence_env",
        "spectre",
        "psf",
    )
    resolved_paths = dict(
        (key, _real_path(engine_config.get(key), base)) for key in path_fields
    )
    if profile_info is not None:
        resolved_paths["device_subckt_file"] = resolve_device_subckt_file(
            resolved_paths.get("model_file"),
            profile_info["profile"],
            configured=resolved_paths.get("device_subckt_file"),
        )
    if check_paths:
        checked_path_keys = (
            ("model_file", "spectre", "psf")
            if source_from_maestro
            else ("source_root", "model_file", "spectre", "psf")
        )
        for key in checked_path_keys:
            path = resolved_paths.get(key)
            valid = (
                os.path.isdir(path)
                if key == "source_root"
                else os.path.isfile(path)
            )
            if not path or not valid:
                errors.append(
                    {
                        "field": key,
                        "message": "configured path does not exist",
                        "path": path,
                    }
                )
        cadence_env = resolved_paths.get("cadence_env")
        if engine_config.get("cadence_env") not in (None, "") and (
            not cadence_env or not os.path.isfile(cadence_env)
        ):
            errors.append(
                {
                    "field": "cadence_env",
                    "message": "configured Cadence child environment file does not exist",
                    "path": cadence_env,
                }
            )
        device_subckt_file = resolved_paths.get("device_subckt_file")
        if not device_subckt_file or not os.path.isfile(device_subckt_file):
            errors.append(
                {
                    "field": "device_subckt_file",
                    "message": (
                        "target device subckt was not found in model_file or "
                        "its literal include chain; set device_subckt_file only "
                        "when automatic discovery cannot resolve the PDK"
                    ),
                    "path": device_subckt_file,
                }
            )
        points = engine_config.get("points")
        if source_from_maestro:
            warnings.append(
                {
                    "field": "testbenches",
                    "message": "VCO points and flicker templates are materialized from the two bound Maestro histories",
                }
            )
        elif not isinstance(points, list) or not points:
            errors.append(
                {
                    "field": "points",
                    "message": "points must be a non-empty list",
                }
            )
        else:
            source_root = resolved_paths.get("source_root")
            seen = set()
            for index, point in enumerate(points):
                if not isinstance(point, Mapping) or not point.get("source_dir"):
                    errors.append(
                        {
                            "field": "points[%d]" % index,
                            "message": "point requires source_dir",
                        }
                    )
                    continue
                source_dir = str(point["source_dir"])
                try:
                    point_root = resolve_source_path(
                        source_root, source_dir
                    )
                    netlist = resolve_source_path(
                        source_root,
                        source_dir,
                        VCO_NETLIST_RELATIVE,
                    )
                except ValueError as exc:
                    errors.append(
                        {
                            "field": "points[%d].source_dir" % index,
                            "message": str(exc),
                            "value": source_dir,
                            "source_root": source_root,
                        }
                    )
                    continue
                if point_root in seen:
                    errors.append(
                        {
                            "field": "points[%d].source_dir" % index,
                            "message": "duplicate source_dir",
                            "value": source_dir,
                        }
                    )
                seen.add(point_root)
                if not netlist or not os.path.isfile(netlist):
                    errors.append(
                        {
                            "field": "points[%d].source_dir" % index,
                            "message": "source VCO netlist does not exist",
                            "path": netlist,
                        }
                    )
                elif profile_info is not None:
                    errors.extend(
                        _netlist_semantic_errors(
                            netlist, profile_info["profile"]
                        )
                    )
        model_file = resolved_paths.get("model_file")
        if (
            profile_info is not None
            and model_file
            and os.path.isfile(model_file)
        ):
            errors.extend(
                _model_semantic_errors(
                    model_file,
                    profile_info["profile"],
                    resolved_paths.get("device_subckt_file"),
                    engine_config.get("device_subckt_section"),
                )
            )

    # A Maestro-pair run intentionally adds HBXF and the deterministic source
    # only to a private copy.  Still verify the bound generated netlist against
    # the selected device/terminal profile before declaring preflight success.
    # This catches same-PDK topology changes without requiring source_root.
    if source_from_maestro and profile_info is not None:
        profile = profile_info["profile"]
        for field, selected, effective in (
            ("testbenches.vco.mapping.target_instance", (maestro_vco_mapping or {}).get("target_instance"), main_instance_path(profile)),
            ("testbenches.replica.mapping.dc_instance", (maestro_flicker_mapping or {}).get("dc_instance"), profile["replica"].get("dc_instance", "M1")),
        ):
            if selected and _normalized_net(selected).replace("/", ".") != str(effective):
                errors.append({"field": field, "message": "映射表所选器件与执行设置不一致；请修正选择或清空专家文件以自动生成。", "selected": selected, "effective": effective})
        peer = (maestro_vco_mapping or {}).get("peer_instance")
        peer_trace = profile["main"]["traces"].get("peer_noise") or ""
        peer_trace_valid = (peer_trace == str(peer) if profile["replica"]["intrinsic"].get("inline_alias")
                            else peer_trace.startswith(str(peer) + "."))
        if peer and not peer_trace_valid:
            errors.append({"field": "testbenches.vco.mapping.peer_instance", "message": "配对器件与实际噪声贡献路径不一致。", "selected": peer})
        seen_maestro_sources = set()
        for source in maestro_source_netlists or []:
            path = _real_path(source.get("path") if isinstance(source, Mapping) else source)
            if not path or path in seen_maestro_sources:
                continue
            seen_maestro_sources.add(path)
            if not os.path.isfile(path):
                if check_paths:
                    errors.append(
                        {
                            "field": "testbenches.vco.history.netlist",
                            "message": "bound Maestro generated netlist is absent",
                            "path": path,
                        }
                    )
                continue
            errors.extend(
                _netlist_semantic_errors(
                    path,
                    profile_info["profile"],
                    require_private_supplements=False,
                    vco_mapping=maestro_vco_mapping,
                )
            )
        seen_flicker_sources = set()
        for source in maestro_flicker_netlists or []:
            path = _real_path(
                source.get("path") if isinstance(source, Mapping) else source
            )
            if not path or path in seen_flicker_sources:
                continue
            seen_flicker_sources.add(path)
            if not os.path.isfile(path):
                if check_paths:
                    errors.append(
                        {
                            "field": "testbenches.replica.history.netlist",
                            "message": "bound flicker generated netlist is absent",
                            "path": path,
                        }
                    )
                continue
            errors.extend(
                _flicker_netlist_semantic_errors(
                    path,
                    profile_info["profile"],
                    maestro_flicker_mapping,
                )
            )

    pdk = dict(pdk or {})
    pdk_root = _real_path(pdk.get("root"))
    model_file = resolved_paths.get("model_file")
    if pdk_root and model_file:
        for field, candidate in (
            ("model_file", model_file),
            ("device_subckt_file", resolved_paths.get("device_subckt_file")),
        ):
            if not candidate:
                continue
            try:
                inside = os.path.commonpath((pdk_root, candidate)) == pdk_root
            except (ValueError, OSError):
                inside = False
            if not inside:
                errors.append(
                    {
                        "field": "profiles.pdk.root",
                        "message": "engine %s is outside the selected PDK root" % field,
                        "pdk_root": pdk_root,
                        field: candidate,
                    }
                )

    project_root = _real_path(app_config.get("project_root"))
    source_root = resolved_paths.get("source_root")
    if project_root and source_root and not source_from_maestro:
        try:
            aligned = os.path.commonpath((project_root, source_root)) == project_root
        except (ValueError, OSError):
            aligned = False
        if not aligned:
            errors.append(
                {
                    "field": "project_root",
                    "message": "engine source_root is outside the selected project root",
                    "project_root": project_root,
                    "source_root": source_root,
                }
            )

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "profile": profile_info,
        "resolved_paths": resolved_paths,
        "source_from_maestro": bool(source_from_maestro),
    }
