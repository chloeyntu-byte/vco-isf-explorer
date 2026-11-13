"""Portable per-operating-point VCO -> replica waveform handoff.

No simulator or Calculator code is evaluated during binding. Both the static
probe and the live writer use the same finite, periodic table contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from .errors import ValidationError

CHANNELS = (("VGS", "gate"), ("VDS", "drain"), ("VBS", "bulk"), ("VS", "source"))
PATH_POLICY = "each operating point/independent offsets: this run VCO HB → current run directory replica/netlist/waveforms; uses only netlist-relative paths, does not reuse old TB absolute PWL Path. private global ISF_F0/ISF_T0 always uses this run’s HB fundamental/period. "


def channel_variables(mapping=None):
    mapping = mapping or {}
    values = {semantic: str(mapping.get(semantic.lower() + "_pwl_variable") or "ISF_" + semantic + "_PWL_FILE")
              for semantic, _ in CHANNELS}
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) for value in values.values()):
        raise ValueError("Waveform file global variable names may contain only letters, digits and underscores, cannot start with a digit")
    if len(set(values.values())) != len(values):
        raise ValueError("VGS/VDS/VBS/VS cannot share the same waveform file variable")
    if {"ISF_F0", "ISF_T0"} & set(values.values()):
        raise ValueError("ISF_F0/ISF_T0 reserved for this run’s fundamental frequency and period, cannot be used as waveform file variables")
    return values


def waveform_tables(wave, traces):
    """Canonical differential voltages plus source common mode, in one timebase."""
    period, f0 = float(wave["period"]), float(wave["f0"])
    times = [float(value) for value in wave["time"]]
    if not math.isfinite(period) or period <= 0 or not math.isfinite(f0) or f0 <= 0:
        raise ValueError("HB period/fundamental frequency is invalid")
    if abs(period * f0 - 1) > 1e-6:
        raise ValueError("HB period and fundamental frequency disagree")
    if len(times) < 3 or any(not math.isfinite(t) for t in times):
        raise ValueError("Periodic waveform requires at least three finite time samples")
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("Periodic waveform time axis must strictly increase")
    if abs(times[0]) > period * 1e-9 or abs(times[-1] - period) > period * 1e-6:
        raise ValueError("Waveforms must cover the same complete period, from 0 to T0")
    values = {}
    for _, terminal in CHANNELS:
        trace = traces.get(terminal)
        if trace not in wave:
            raise ValueError("VCO Missing %s terminal waveform: %s" % (terminal, trace))
        data = [float(value) for value in wave[trace]]
        if len(data) != len(times) or any(not math.isfinite(v) for v in data):
            raise ValueError("VCO terminal waveform lengths differ or include non-finite values: %s" % terminal)
        if abs(data[-1] - data[0]) > max(1e-12, max(abs(v) for v in data) * 1e-9):
            raise ValueError("VCO terminal waveform endpoints do not close: %s" % terminal)
        values[terminal] = data
    result = {}
    for semantic, terminal in CHANNELS:
        data = values[terminal] if semantic == "VS" else [a - b for a, b in zip(values[terminal], values["source"])]
        result[semantic] = "".join("%.17e %.17e\n" % pair for pair in zip(times, data))
    return result


def export_waveforms(wave, traces, netlist_dir, mapping=None, provenance=None):
    """Only write newly generated task-local waveform assets; never touch a TB."""
    netlist_dir = Path(netlist_dir)
    variables = channel_variables(mapping)
    tables = waveform_tables(wave, traces)
    folder = netlist_dir / "waveforms"
    if folder.is_symlink() or (folder / "manifest.json").is_symlink():
        raise ValueError("Waveform directory and manifest cannot be symbolic links")
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "f0_hz": float(wave["f0"]), "period_s": float(wave["period"]),
                "samples": len(wave["time"]), "provenance": provenance or {}, "channels": []}
    for semantic, terminal in CHANNELS:
        relative = "waveforms/" + semantic.lower() + ".pwl"
        content = tables[semantic].encode("ascii")
        path = netlist_dir / relative
        if path.is_symlink() or folder.is_symlink():
            raise ValueError("Waveform output directory cannot contain symbolic links")
        path.write_bytes(content)
        manifest["channels"].append({"semantic": semantic, "variable": variables[semantic],
            "relative_file": relative, "sha256": hashlib.sha256(content).hexdigest(),
            "positive_trace": traces[terminal], "negative_trace": "0" if semantic == "VS" else traces["source"]})
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return manifest


def validate_waveform_assets(netlist_dir):
    """Missing/changed files fail before simulation or cache reuse, even after moving."""
    root = Path(netlist_dir).resolve()
    path = root / "waveforms/manifest.json"
    if not path.exists():
        # A generated netlist advertising the contract must never lose it.
        netlist = root / "input.scs"
        if netlist.is_file() and "// ISF waveform-exchange-v1" in netlist.read_text(encoding="utf-8"):
            raise ValueError("Waveform exchange manifest is missing; regenerate the current operating point")
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    channels = manifest.get("channels", [])
    if manifest.get("schema_version") != 1 or {row.get("semantic") for row in channels} != {row[0] for row in CHANNELS} or len(channels) != 4:
        raise ValueError("Waveform exchange manifest is incomplete")
    for row in channels:
        relative = Path(row["relative_file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Waveforms must use paths relative to the current netlist")
        asset = root / relative
        if asset.is_symlink() or root not in asset.resolve().parents or not asset.is_file():
            raise ValueError("Current operating-point waveform file missing or outside allowed path: " + str(relative))
        if hashlib.sha256(asset.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Current operating-point waveform checksum mismatch: " + str(relative))
    return manifest


def set_root_parameters(text, replacements):
    """Replace/add root parameters, keeping subckt defaults and other variables intact."""
    from .maestro_pair import _assignment_spans, _root_statements
    edits, found = [], set()
    for start, _, statement in _root_statements(text):
        if not re.match(r"(?i)^\s*parameters\b", statement):
            continue
        for name, begin, end in _assignment_spans(statement):
            if name in replacements:
                if name in found:
                    raise ValueError("Duplicate global parameter declaration: " + name)
                found.add(name)
                edits.append((start + begin, start + end, str(replacements[name])))
    for begin, end, replacement in sorted(edits, reverse=True):
        text = text[:begin] + replacement + text[end:]
    missing = {key: value for key, value in replacements.items() if key not in found}
    if missing:
        text += "\nparameters " + " ".join(key + "=" + str(value) for key, value in missing.items()) + "\n"
    return text


def prepare_dc_bias_sources(text, mapping):
    """Shared static/live preparation of mapped root sources for the Mdc baseline."""
    from .maestro_pair import _root_statements, _assignment_spans
    statements = list(_root_statements(text))
    root_parameters = [name for _, _, statement in statements
        if re.match(r"(?i)^\s*parameters\b", statement)
        for name, _, _ in _assignment_spans(statement)]
    source_edits = []
    for semantic in ("vgs", "vds", "vbs"):
        source = mapping.get(semantic + "_source")
        variable = mapping.get(semantic + "_variable")
        if variable and root_parameters.count(variable) != 1:
            raise ValueError("Bias variable must have one definition at the root of the selected history netlist: " + variable)
        if not source or not variable:
            continue
        matches = []
        for begin, end, statement in statements:
            match = re.match(r"^\s*" + re.escape(source) + r"\s*\(([^)]*)\)\s+vsource\b", statement)
            if match:
                matches.append((begin, end, "%s (%s) vsource dc=%s type=dc\n" % (source, match.group(1), variable)))
        if len(matches) != 1:
            raise ValueError("Bias source must be unique and at the selected noise-netlist root; internal hierarchical sources are not changed automatically: " + source)
        source_edits.extend(matches)
    if len({begin for begin, _, _ in source_edits}) != len(source_edits):
        raise ValueError("Multiple bias roles cannot override the same voltage source")
    for begin, end, replacement in sorted(source_edits, reverse=True):
        text = text[:begin] + replacement + text[end:]
    return text


def inspect_waveform_exchange(pair_report, engine=None):
    """Static executable preparation contract, explicitly not a completed simulation."""
    roles = pair_report.get("roles", {})
    vco, replica = roles.get("vco") or {}, roles.get("flicker") or {}
    generated = pair_report.get("pair", {}).get("replica_source") == "generated"
    vm = vco.get("mapping", {})
    if generated:
        from .generated_flicker import DC_MAPPING
        rm = DC_MAPPING
    else:
        rm = replica.get("mapping", {})
    traces = (engine or {}).get("physical_profile", {}).get("main", {}).get("traces", {})
    if not traces:
        traces = {terminal: vm.get(terminal) for _, terminal in CHANNELS}
    checks, rows = [], []
    def check(name, ok, detail):
        checks.append({"id": name, "status": "pass" if ok else "fail", "detail": detail})
    check("terminals", all(traces.get(terminal) for _, terminal in CHANNELS), "validate against the selected target device D/G/S/B; missing named output export terminal differences when unavailable, an unknown source terminal cannot be assumed grounded. ")
    check("source_history", bool((vco.get("selected_history") or {}).get("source_netlists")), "requires a selected VCO history readable netlist; save four-terminal waveforms in the private copy at runtime. ")
    if generated:
        check("replica_generated", bool((engine or {}).get("device_subckt_file")),
              "from selected VCO operating point inherits target MOS instance parameters, active model and temperature, generate a private noise netlist. ")
    else:
        check("replica_history", bool((replica.get("selected_history") or {}).get("source_netlists")), "requires selected noise TB history readable netlist, to preserve device parameters and models. ")
    mapping_errors = [item.get("message", "Invalid electrical signal mapping") for report in (vco, replica)
        for item in report.get("mapping_issues", []) if item.get("severity") == "error"]
    check("electrical_mapping", not mapping_errors, "; ".join(mapping_errors) if mapping_errors else "selected nodes, Named output, bias sources and target device are electrically consistent. ")
    for role, report in (("vco", vco), ("flicker", replica)):
        unique_paths = {record.get("path") for record in (report.get("selected_history") or {}).get("source_netlists", [])}
        try:
            for path in unique_paths:
                if not path or not Path(path).is_file() or Path(path).stat().st_size > 32 * 1024 * 1024:
                    raise ValueError("Netlist is unreadable or exceeds the safe read limit: %s" % path)
                text = Path(path).read_text(encoding="utf-8", errors="replace")
                if role == "flicker":
                    prepare_dc_bias_sources(text, rm)
                    analysis = rm.get("noise_analysis") or "noise"
                    if not re.search(r"(?m)^\s*" + re.escape(analysis) + r"\s*\([^)]*\)\s+noise\b", text):
                        raise ValueError("Noise baseline analysis does not exist in the netlist: " + analysis)
                else:
                    if vm.get("periodic_analysis_type", "hb") != "hb":
                        raise ValueError("The waveform executor currently requires HB; does not treat shooting PSS results as HB")
                    for key, kind in (("periodic_analysis", "hb"), ("noise_analysis", "hbnoise")):
                        name = vm.get(key) or kind
                        if not re.search(r"(?m)^\s*" + re.escape(name) + r"\s+(?:\([^\n)]*\)\s+)?" + kind + r"\b", text):
                            raise ValueError("Private waveform preparation requires a selected %s Analysis %s; missing in current netlist or type mismatch" % (kind, name))
            if unique_paths:
                check(role + "_netlist_prepare", True, "Read the selected operating-point netlist and applied the same bias-source preparation checks; does not access old PWL files or modify the original TB. ")
        except (OSError, ValueError, TypeError, ValidationError) as exc:
            check(role + "_netlist_prepare", False, str(exc))
    variables = {}
    try:
        variables = channel_variables(rm)
        fixture_traces = {terminal: terminal for _, terminal in CHANNELS}
        fixture = {"f0": 1.0, "period": 1.0, "time": [0, .5, 1],
                   "gate": [1, 2, 1], "drain": [2, 3, 2], "source": [.2, .3, .2], "bulk": [.1, .2, .1]}
        tables = waveform_tables(fixture, fixture_traces)
        check("script_contract", len(tables) == 4, "Self-checked the same waveform generation code: time axis, differential, level, file global variables and relative paths; no new execution yet for HB Simulation. ")
    except (ValueError, KeyError, TypeError) as exc:
        check("script_contract", False, str(exc))
    cross = {row.get("semantic"): row for row in pair_report.get("cross_tb_mapping", [])}
    for semantic, terminal in CHANNELS:
        item = cross.get(semantic, {})
        named = vm.get(semantic.lower() + "_output")
        negative = "0" if semantic == "VS" else traces.get("source")
        rows.append({"semantic": semantic, "source_kind": item.get("vco_source", "named_waveform" if named else "pin_derived"),
            "source_output": named, "source_expression": "V(%s)-V(%s)" % (traces.get(terminal), negative),
            "source_nodes": [traces.get(terminal), negative],
            "destination_variable": rm.get(semantic.lower() + "_variable"), "destination_source": "private VS (source common-mode level)" if semantic == "VS" else rm.get(semantic.lower() + "_source"),
            "pwl_variable": variables.get(semantic), "relative_file": "waveforms/" + semantic.lower() + ".pwl",
            "scope": "Maestro global / current private netlist global; legacy history added by the script when undefined", "status": "pass" if traces.get(terminal) and traces.get("source") else "fail",
            "detail": "periodic simulation reads the current differential PWL; also sample using the same waveform DC bias calculation Mdc baseline. source common-mode level is preserved through a separate VS waveform preserves. "})
    ok = all(item["status"] == "pass" for item in checks)
    failures = [item for item in checks if item["status"] == "fail"]
    blocked = bool(mapping_errors) and all(item["id"] == "electrical_mapping" for item in failures)
    summary = "Static waveform exchange check passed; actual waveforms will be generated after the VCO simulation and verified file by file. "
    if not ok:
        summary = ("Blocked by upstream name mapping: " + mapping_errors[0] + "; not confirmed missing PWL File. "
                   if blocked else "Waveform exchange check failed: " + failures[0]["detail"])
    return {"schema_version": 1, "status": "pass" if ok else "fail", "ok": ok, "checked": "static_prepare",
        "summary": summary, "blocked_by": ["electrical_mapping"] if blocked else [],
        "root_causes": mapping_errors,
        "checks": checks, "rows": rows, "path_policy": PATH_POLICY, "dc_baseline_required": True}
