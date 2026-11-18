"""Additive private thermal measurements. Never changes the flicker netlists.

Noise input: MOS TB's phase-resolved intrinsic source PSD, and VCO static
passive source PSD. Transfer: independent signed-output HBXF. Main HBNoise
output values are only references, never inputs to the prediction.
"""

from __future__ import annotations

import csv
import json
import math
import re
import subprocess
from pathlib import Path

from ..calculation import dBc_from_pm_asd
from ..thermal import calculate_white_thermal, fourier_amplitude_from_psd, project_am_pm
from .maestro_pair import _root_statements
from .noise_sources import contributor_records, noise_sample, parse_psf_ascii
from .physical_profile import main_instance_path
from .private_validation import write_analysis_copy
from .thermal_ports import parse_terminal_map, physical_noise_port, port_transfer


PM_EVIDENCE = "spectre18_analytic_white_AM_PM_PWM_20260920_v1"
INDUCTOR_EVIDENCE = "spectre18_known_R_and_skin_R_voltage_noise_20260920_v1"


def _read(engine, cfg, path):
    return parse_psf_ascii(engine.run_command(
        [cfg["psf"], "-f", "%.16e", "-i", str(path)], cadence_env=cfg.get("cadence_env")))


def _analysis(statement):
    match = re.match(r"\s*(\S+)\s+(?:\([^)]*\)\s+)?(\w+)\b", statement)
    return match.groups() if match else (None, None)


def _private_main(text, output, offset, maxk, absolute_frequencies):
    """Preserve HB and devices verbatim; replace only private analyses."""
    additions = []
    pair = output["negative"] + " " + output["positive"]
    for name, frequency in (("thermal_upper", offset), ("thermal_lower", -offset)):
        additions.append(("{} ({}) hbxf sweeptype=relative relharmvec=[1] values=[{:.16e}] "
                          "maxsideband={} freqaxis=out stimuli=nodes_and_terminals annotate=status\n")
                         .format(name, pair, frequency, maxk + 1))
    replacements, hb_count, save_count = [], 0, 0
    names = {name for _, _, statement in _root_statements(text) for name, _ in [_analysis(statement)]}
    if names & {"thermal_upper", "thermal_lower", "thermal_static", "thermal_nodes", "thermal_save"}:
        raise ValueError("private thermal analysis name collision")
    for start, end, statement in _root_statements(text):
        name, kind = _analysis(statement)
        if name in {"isf_fn_upper", "isf_fn_lower", "isf_fn_nodes"}:
            replacements.append((start, end, ""))
        elif kind == "hb":
            hb_count += 1
            replacements.append((start, end, statement.rstrip() + "\n" + "".join(additions)))
        elif kind == "options":
            # HBXF stimuli=nodes_and_terminals still obeys save=selected.
            # Only the thermal copy needs every internal noise-source port.
            updated, count = re.subn(r'(?<![\w.])save\s*=\s*(?:"[^"]*"|[A-Za-z_]\w*)',
                                     'save=all', statement)
            if count:
                save_count += count
                replacements.append((start, end, updated))
    if hb_count != 1:
        raise ValueError("thermal extraction requires one selected HB solution")
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    if not save_count:
        text += "\nthermal_save options save=all\n"
    frequencies = " ".join("%.16e" % f for f in absolute_frequencies)
    # Static source fields are intrinsic; the output port is not interpreted
    # as a standalone LC current. A separate analysis after HB does not alter it.
    text += ("\nthermal_static ({}) noise values=[{}] separatenoise=yes annotate=status\n"
             'thermal_nodes info what=nodes where=file file="../thermal_nodes.txt"\n').format(pair, frequencies)
    return text


def _private_dc(text, frequencies):
    replacements, count = [], 0
    for start, end, statement in _root_statements(text):
        name, kind = _analysis(statement)
        if kind == "noise":
            match = re.match(r"\s*\S+\s+(\([^)]*\))\s+noise\b", statement)
            if match is None:
                raise ValueError("unsupported MOS noise output port")
            replacements.append((start, end, "thermal_sources {} noise values=[{}] separatenoise=yes annotate=status\n".format(
                match[1], " ".join("%.16e" % f for f in frequencies))))
            count += 1
    if count != 1:
        raise ValueError("MOS thermal template must have one selected DC noise analysis")
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _source_rows(dataset, frequency):
    return {(r["device"], r["mechanism"]): r for r in contributor_records(dataset, frequency)
            if r["component"] == "src"}


def _instance_member(device, instance):
    """Include an inline primitive itself and descendants, never name prefixes."""
    return device == instance or device.startswith(instance + ".")


def _symmetric_peer_instance(target_instance, target_trace, peer_trace):
    """Resolve both ordinary subcircuit and inline primitive trace ownership."""
    if not _instance_member(target_trace, target_instance):
        raise ValueError("target noise trace is outside the selected gm instance")
    suffix = target_trace[len(target_instance):]
    if suffix:
        if not peer_trace.endswith(suffix):
            raise ValueError("cannot identify symmetric peer primitive hierarchy")
        peer = peer_trace[:-len(suffix)]
    else:
        peer = peer_trace
    if not peer or _instance_member(peer, target_instance) or _instance_member(target_instance, peer):
        raise ValueError("symmetric peer must be a distinct independent instance")
    return peer


def lc_passive_roots(terminal_map):
    """Find independently passive LC branches at any hierarchy depth.

    Use the elaborated descendants at each boundary. An active sibling cannot
    disqualify a passive LC branch, while a bank's bare resistor has no LC
    ownership evidence and remains excluded.
    """
    groups = {}
    for device, record in terminal_map.items():
        parts = device.split('.')
        for depth in range(1, len(parts) + 1):
            groups.setdefault('.'.join(parts[:depth]), set()).add(str(record.get('master', '')).lower())
    allowed = {'resistor', 'capacitor', 'inductor', 'mutual_inductor'}
    candidates = sorted(root for root, primitives in groups.items()
                        if primitives <= allowed and primitives & {'capacitor', 'inductor'})
    roots = []
    for candidate in candidates:
        if not any(_instance_member(candidate, root) for root in roots):
            roots.append(candidate)
    return roots


def _mos_white_sources(low, high, highest, dc_instance, target_instance):
    """Use the same mechanism, unit and white-spectrum checks for both fixtures."""
    selected = {}
    for key, row in low.items():
        device, mechanism = key
        if not _instance_member(device, dc_instance) or row['noise_family'] not in ('channel_thermal', 'resistive_thermal'):
            continue
        if any(key not in sample or sample[key]['units'] != 'A^2/Hz' for sample in (low, high, highest)):
            raise ValueError('missing MOS source PSD units/frequency evidence: ' + str(key))
        if any(not math.isclose(row['value'], sample[key]['value'], rel_tol=1e-6, abs_tol=1e-40)
               for sample in (high, highest)):
            raise ValueError('MOS source is not white over required bands: ' + str(key))
        selected[(target_instance + device[len(dc_instance):], mechanism)] = row['value']
    return selected


class ThermalCapabilityError(ValueError):
    """An explicit N/A with the simulator identity that needs validation."""


def _require_spectre18(version):
    if not str(version).startswith('18.1.'):
        raise ThermalCapabilityError('N/A: thermal source units and PM projection are validated for Spectre 18.1.*; '
                                     'Spectre ' + (str(version) or 'unknown') + ' requires independent reference validation')


def _carrier(values, output):
    return (0j if output['negative'] == '0' else values[output['negative']]) - (
            0j if output['positive'] == '0' else values[output['positive']])


def run_thermal_point(engine, cfg, point, case_dir, *, force=False):
    """Produce versioned budget or raise; caller keeps flicker evidence intact."""
    case_dir = Path(case_dir)
    root = case_dir / "thermal"
    profile = engine.physical_profile(cfg)
    output = engine.main_output(cfg)
    main_psf = case_dir / "main_vco" / "psf"
    offset = float(cfg["offset_hz"])
    primary = _read(engine, cfg, main_psf / "hb.fd.pss_hb")
    _require_spectre18(primary['header'].get('version', ''))
    f0 = engine.exact_hb_fundamental(cfg, main_psf)
    if not 0 < offset < f0:
        raise ValueError("thermal PM projection requires offset below the carrier")
    # The requested calculation order, not the simulator's wider reference
    # sideband setting, bounds both transfer and independent input bands.
    maxk = int(cfg["max_harmonic"])
    bands = range(-maxk, maxk + 1)
    frequencies = sorted({abs(k * f0 + offset) for k in bands})
    if min(frequencies) <= 0:
        raise ValueError("thermal source band hits DC; choose a nonzero non-harmonic offset")
    source = case_dir / "main_vco" / "netlist"
    target = root / "main" / "netlist"
    engine._copy_regular_tree(source, target)
    write_analysis_copy(source / "input.scs", target / "input.scs",
                        _private_main((source / "input.scs").read_text(), output, offset, maxk, frequencies))
    engine.run_spectre(cfg, target, force=force)
    psf = target.parent / "psf"
    hb = _read(engine, cfg, psf / "hb.fd.pss_hb")
    # Spectre clamps XF sidebands to the saved HB harmonic support. Use the
    # common signed domain of upper/lower measurements, never fabricate edges.
    maxk = min(maxk, int(hb["header"]["harms"]) - 1)
    if maxk < int(cfg["max_harmonic"]):
        raise ValueError("saved VCO HB harmonics do not support requested thermal order")
    bands = range(-maxk, maxk + 1)
    # Matching fresh HB time origin is essential for the phase-dependent PSD.
    values = noise_sample(hb, f0, relative_tol=1e-10)["values"]
    carrier = _carrier(values, output)
    primary_values = noise_sample(primary, f0, relative_tol=1e-10)["values"]
    primary_carrier = _carrier(primary_values, output)
    if abs(carrier - primary_carrier) > max(abs(primary_carrier) * 1e-4, 1e-12):
        raise ValueError("thermal and flicker private HB have different carrier/time origin")
    terminal_map = parse_terminal_map((target.parent / "thermal_nodes.txt").read_text())
    selected_only = profile['main'].get('thermal_scope') == 'selected_device'
    passive_roots = [] if selected_only else lc_passive_roots(terminal_map)
    upper, lower = {}, {}
    for k in bands:
        u = _read(engine, cfg, psf / ("thermal_upper.%d.pxf_hbxf" % (k - 1)))
        l = _read(engine, cfg, psf / ("thermal_lower.%d.pxf_hbxf" % (-k - 1)))
        upper[k] = noise_sample(u, f0 + offset, relative_tol=1e-10)["values"]
        lower[k] = noise_sample(l, f0 - offset, relative_tol=1e-10)["values"]
    static = _read(engine, cfg, psf / "thermal_static.noise")
    simulator_version = str(static["header"].get("version", ""))
    _require_spectre18(simulator_version)
    static_rows = _source_rows(static, offset)
    target_instance = main_instance_path(profile)
    target_trace = profile["main"]["traces"]["target_noise"]
    receipt = case_dir / 'main_vco/netlist/input.isf-patch.json'
    policy = json.loads(receipt.read_text()).get('pair_policy', {}) if receipt.is_file() else (
        cfg.get('pair_policy') or (cfg.get('vco_mapping') or {}).get('pair_policy') or {})
    if not selected_only and not policy.get('proven'):
        raise ValueError('thermal pair estimate requires proven master/geometry/bias symmetry; single-device flicker remains available')
    peer_trace = profile["main"]["traces"].get("peer_noise") if policy.get('proven') else None
    peer_instance = _symmetric_peer_instance(target_instance, target_trace, peer_trace) if peer_trace else None
    dc_instance = profile["replica"].get("dc_instance", "M1")
    dc_root = case_dir / "dc_flicker"
    table = dc_root / "iflicker_samples.csv"
    if not table.is_file():
        raise ValueError("thermal requires fresh, local waveform-driven MOS DC samples")
    with table.open() as handle:
        samples = list(csv.DictReader(handle))
    times = [float(row["time_s"]) for row in samples]
    period = times[-1] - times[0]
    periodic = []
    quality_paths = [("thermal_main", psf / "spectre.out")]
    for index in range(1, len(samples)):
        source = dc_root / ("sample_%02d" % index) / "netlist"
        target_dc = root / ("sample_%02d" % index) / "netlist"
        engine._copy_regular_tree(source, target_dc)
        highest_input = (maxk + 32) * f0 + offset
        write_analysis_copy(source / "input.scs", target_dc / "input.scs",
                            _private_dc((source / "input.scs").read_text(), [offset, f0, highest_input]))
        engine.run_spectre(cfg, target_dc, force=force)
        dataset = _read(engine, cfg, target_dc.parent / "psf" / "thermal_sources.noise")
        low, high = _source_rows(dataset, offset), _source_rows(dataset, f0)
        highest = _source_rows(dataset, highest_input)
        selected = _mos_white_sources(low, high, highest, dc_instance, target_instance)
        periodic.append(selected)
        quality_paths.append(("thermal_mos", target_dc.parent / "psf" / "spectre.out"))
    if not periodic or not periodic[0]:
        raise ValueError("no supported thermal PSD in bound MOS TB")
    if any(set(row) != set(periodic[0]) for row in periodic):
        raise ValueError("MOS source list changes between phase samples")
    periodic.append(dict(periodic[0]))
    sources, source_coefficients = [], []

    def transfer(device, mechanism):
        port = physical_noise_port(device, mechanism, terminal_map, upper[0])
        hp = {k: port_transfer(upper[k], port) for k in bands}
        hn = {k: port_transfer(lower[k], port).conjugate() for k in bands}
        projection = project_am_pm(hp, hn, carrier, validated=True, validation_evidence=PM_EVIDENCE)
        return port, projection

    for (device, mechanism) in sorted(periodic[0]):
        port, projection = transfer(device, mechanism)
        # Dense interpolation resolves products in RMS/Fourier integration;
        # source resolution itself remains explicitly the measured phase grid.
        import numpy as np
        dense = np.linspace(times[0], times[-1], 2049)
        density = np.interp(dense, times, [row[(device, mechanism)] for row in periodic])
        amplitude = fourier_amplitude_from_psd(dense.tolist(), density.tolist(), period=period, max_harmonic=32)
        result = calculate_white_thermal(projection["pm_transfer"], amplitude, carrier_peak=abs(carrier),
            max_input_harmonic=maxk,
            source_id=device + "." + mechanism, transfer_quantity="PM_SSB", pm_contract=projection["contract"])
        record = {"device": device, "mechanism": mechanism, "psd_V2_Hz": result["total_psd_V2_Hz"],
                  "phase_noise_dBc_Hz": result["phase_noise_dBc_Hz"],
                  "input_band_contributions": [{"input_band": part["input_band"], "psd_V2_Hz": part["psd_V2_Hz"]}
                                               for part in result["input_band_contributions"]],
                  "port": port, "source_basis": "MOS_TB_phase_resolved_white_PSD",
                  "phase_sample_count": len(samples) - 1, "method": result["method"]}
        record["amplitude_max_harmonic"] = 32
        record["input_band_truncation"] = result["input_band_truncation"]
        record["white_spectrum_spotcheck_Hz"] = [offset, f0, highest_input]
        source_coefficients.append({"device": device, "mechanism": mechanism,
            "source_time_s": times, "source_psd_A2_Hz": [row[(device, mechanism)] for row in periodic],
            "transfer_PM": [{"k": k, "real": v.real, "imag": v.imag} for k, v in projection["pm_transfer"].items()],
            "amplitude_coefficients": [{"k": k, "real": v.real, "imag": v.imag} for k, v in amplitude.items()]})
        sources.append(record)
        # Explicit user-selected symmetric independent pair model. This is
        # not a claim that the second transistor was separately simulated.
        if peer_instance:
            sources.append(dict(record, device=peer_instance + device[len(target_instance):],
                                source_basis="symmetric_independent_peer", modeled_from=device))
    for (device, mechanism), row in sorted(static_rows.items()):
        if any(_instance_member(device, instance) for instance in (target_instance, peer_instance) if instance):
            continue
        primitive = terminal_map.get(device, {}).get("master")
        if not any(_instance_member(device, branch) for branch in passive_roots):
            continue
        if primitive not in ("resistor", "inductor") or mechanism != "rn":
            continue
        port, projection = transfer(device, mechanism)
        parts = []
        for k in bands:
            frequency = abs(k * f0 + offset)
            source_row = _source_rows(static, frequency)[(device, mechanism)]
            if source_row["units"] != "A^2/Hz":
                raise ValueError("unvalidated primitive source unit declaration")
            parts.append({"input_band": k, "source_frequency_Hz": frequency,
                          "source_psd": source_row["value"],
                          "psd_V2_Hz": abs(projection["pm_transfer"][k]) ** 2 * source_row["value"]})
        power = math.fsum(part["psd_V2_Hz"] for part in parts)
        sources.append({"device": device, "mechanism": mechanism, "psd_V2_Hz": power,
                        "phase_noise_dBc_Hz": dBc_from_pm_asd(math.sqrt(power), abs(carrier)),
                        "input_band_contributions": parts, "port": port,
                        "source_basis": "VCO_static_intrinsic_PSD_at_actual_input_bands",
                        "unit_evidence": INDUCTOR_EVIDENCE if primitive == "inductor" else "PSF_A2_per_Hz"})
    included_sources = {(s["device"], s["mechanism"]) for s in sources}
    excluded_sources = [{"device": device, "mechanism": mechanism,
                         "primitive": terminal_map.get(device, {}).get("master", row.get("primitive")),
                         "noise_family": row["noise_family"],
                         "reason": "outside gm pair and independently passive LC branches"}
                        for (device, mechanism), row in sorted(static_rows.items())
                        if (device, mechanism) not in included_sources and row["noise_family"] != "flicker"]
    power = math.fsum(source["psd_V2_Hz"] for source in sources)
    quality = engine.aggregate_spectre_quality(quality_paths)
    budget = {"schema_version": 1, "status": "available", "scope": "selected_device" if selected_only else "gm_pair_and_lc_network",
              "passive_scope": "passive_lc_branches_v2", "passive_roots": passive_roots,
              "noise_quantity": "PM_SSB", "method": "independent_source_and_input_band_power_sum_v1",
              "predicted_psd_V2_Hz": power, "phase_noise_dBc_Hz": dBc_from_pm_asd(math.sqrt(power), abs(carrier)),
              "carrier_peak_V": abs(carrier), "offset_Hz": offset, "f0_Hz": f0,
              "transfer_max_harmonic": maxk, "input_max_harmonic": maxk,
              "requested_max_harmonic": int(cfg["max_harmonic"]),
              "sources": sources, "excluded_sources": excluded_sources,
              "pair_model": "symmetric_independent" if peer_instance else "single_device_only", "pair_policy": policy, "pm_validation": PM_EVIDENCE,
              "target_noise_trace": target_trace, "peer_noise_trace": peer_trace,
              "numerical_quality": quality, "simulator_version": simulator_version,
              "limitations": ["MOS thermal uses phase-resolved quasi-static white source PSD; colored/correlated mechanisms are not inferred.",
                              "The budget includes the selected MOS and any symmetry-qualified peer." if selected_only else
                              "MOS pair is symmetric by user model; other active devices and shot noise are outside the predicted budget.",
                              "LC coverage includes resistor/inductor sources owned by independently passive LC branches at any depth; other active sources and unknown primitives are excluded."]}
    if selected_only:
        budget.update(target_instance=target_instance, peer_instance=peer_instance)
    (root / "source_coefficients.json").write_text(engine._strict_json_dumps(source_coefficients, indent=2) + "\n")
    (root / "budget.json").write_text(engine._strict_json_dumps(budget, indent=2, sort_keys=True) + "\n")
    return budget


def run_optional_thermal(engine, cfg, point, case_dir, *, force=False):
    """A missing thermal capability must not corrupt the proven flicker path."""
    if cfg.get("thermal_enabled", True) is False:
        budget = {"schema_version": 1, "status": "unavailable", "reason": "thermal disabled in expert configuration"}
        root = Path(case_dir) / "thermal"
        root.mkdir(parents=True, exist_ok=True)
        (root / "budget.json").write_text(json.dumps(budget, indent=2) + "\n")
        return budget
    try:
        return run_thermal_point(engine, cfg, point, case_dir, force=force)
    except (OSError, ValueError, RuntimeError, KeyError, IndexError, TypeError, subprocess.SubprocessError) as exc:
        root = Path(case_dir) / "thermal"
        root.mkdir(parents=True, exist_ok=True)
        budget = {"schema_version": 1, "status": "unavailable", "reason": type(exc).__name__ + ": " + str(exc),
                  "code": "ISF-W3101", "offset_Hz": float(cfg["offset_hz"])}
        if isinstance(exc, ThermalCapabilityError):
            budget['capability'] = 'spectre18_thermal_reference'
        (root / "budget.json").write_text(json.dumps(budget, indent=2) + "\n")
        print("thermal unavailable: " + str(exc), flush=True)
        return budget
