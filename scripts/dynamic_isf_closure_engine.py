#!/usr/bin/env python3
"""Dynamic device-noise/ISF closure for the VCO cross-coupled gm MOS.

The script never edits OA, PDK, Maestro state, or historical PSF data.  It
copies one already generated VCO netlist per design point into a private run
root, adds an ideal drain-current probe, runs fresh HB/HBNOISE/HBXF, builds a
periodically driven replica MOS, and evaluates a physical-unit coherent sum.

HBNOISE AM/PM are second-order quantities.  They therefore produce two phase
candidates per non-DC harmonic, not a unique random-noise phasor.  Candidate
selection here is an explicit engineering heuristic based only on the DC-noise
Fourier phase, common-delay smoothness, and cross-point continuity.  The final
VCO PNOISE result is deliberately excluded from candidate selection.
"""

import argparse
import csv
import hashlib
import itertools
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np


HERE = Path(__file__).resolve().parent
TOOL_ROOT = HERE.parent
BOOTSTRAP_ROOTS = [TOOL_ROOT]
if TOOL_ROOT.name == "_runtime" and TOOL_ROOT.parent.name == "isf_tool":
    BOOTSTRAP_ROOTS.append(TOOL_ROOT.parent.parent)
for bootstrap_root in reversed(BOOTSTRAP_ROOTS):
    if str(bootstrap_root) not in sys.path:
        sys.path.insert(0, str(bootstrap_root))

from isf_tool.core.physical_profile import (
    dc_noise_trace,
    drain_probe_trace,
    main_injection_trace,
    main_instance_path,
    resolve_device_subckt_file,
    resolve_physical_profile,
)
from isf_tool.core.limits import MAX_PHASE_CANDIDATE_HARMONICS
from isf_tool.core.waveform_exchange import (
    export_waveforms, set_root_parameters, validate_waveform_assets, prepare_dc_bias_sources,
)
from isf_tool.core.signal_mapping import inspect_history_topology, canonical_net, _voltage_equivalence
from isf_tool.core.maestro_pair import (
    _assignment_spans,
    _root_statements,
    restore_operating_point_text,
)
from isf_tool.core.source_paths import (
    VCO_NETLIST_RELATIVE,
    resolve_source_path,
)
from isf_tool.resources import config_path
from isf_tool.core.noise_sources import parse_psf_ascii, noise_quantity


DEFAULT_CONFIG = config_path("engine_pair_example_flat.json")

ENGINE_CONFIG_PATH_FIELDS = (
    "source_root",
    "model_file",
    "device_subckt_file",
    "spectre",
    "psf",
    "physical_profile_path",
    "flicker_template_netlist",
    "cadence_env",
    "ld_preload",
    "license_file",
)
_PATH_LIST_FIELDS = frozenset(("ld_preload", "license_file"))


def _strict_json_payload(value):
    """Replace non-finite numeric sentinels with interoperable JSON nulls."""

    if isinstance(value, np.generic):
        return _strict_json_payload(value.item())
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {
            key: _strict_json_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_strict_json_payload(item) for item in value]
    return value


def _strict_json_dumps(value, **options):
    return json.dumps(_strict_json_payload(value), allow_nan=False, **options)


def _resolve_config_path_token(value, config_dir, field):
    if not value:
        return value
    if (
        field == "license_file"
        and "@" in value
        and "/" not in value
        and "\\" not in value
    ):
        return value
    expanded = os.path.expanduser(value)
    if os.path.isabs(expanded):
        return value if expanded == value else expanded
    return os.path.realpath(os.path.join(str(config_dir), expanded))


def _resolve_config_path_value(value, config_dir, field):
    if not isinstance(value, str) or not value:
        return value
    if field in _PATH_LIST_FIELDS and os.pathsep in value:
        return os.pathsep.join(
            _resolve_config_path_token(item, config_dir, field)
            for item in value.split(os.pathsep)
        )
    return _resolve_config_path_token(value, config_dir, field)


def resolve_engine_config_paths(config, config_path):
    """Anchor relative engine paths to the JSON file, never to process cwd."""
    config_path = Path(config_path).expanduser().resolve()
    resolved = dict(config)
    for field in ENGINE_CONFIG_PATH_FIELDS:
        if field in resolved:
            resolved[field] = _resolve_config_path_value(
                resolved[field], config_path.parent, field
            )
    return resolved


def load_config(path):
    config_path = Path(path).expanduser().resolve()
    with config_path.open() as handle:
        config = json.load(handle)
    config = resolve_engine_config_paths(config, config_path)
    _enforce_phase_candidate_limit(config.get("max_harmonic"))
    profile_info = resolve_physical_profile(
        config, config_path=str(config_path), allow_legacy=True
    )
    config["_physical_profile"] = profile_info["profile"]
    config["_physical_profile_sha256"] = profile_info["sha256"]
    config["_physical_profile_explicit"] = profile_info["explicit"]
    config["_device_subckt_file"] = resolve_device_subckt_file(
        config.get("model_file"),
        profile_info["profile"],
        configured=config.get("device_subckt_file"),
    )
    config["_engine_config_path"] = str(config_path)
    config["_engine_config_sha256"] = _sha256_file(config_path)
    return config


def physical_profile(cfg):
    profile = cfg.get("_physical_profile")
    if profile is not None:
        return profile
    return resolve_physical_profile(cfg, allow_legacy=True)["profile"]


def _netlist_node(value):
    value = str(value or "").strip()
    while value.startswith("/"):
        value = value[1:]
    return value


def main_output(cfg):
    """Return the bound Maestro differential output in engine convention.

    Cadence stores oscillator ``p``/``n`` in the selected analysis.  The
    legacy engine names its fields ``negative``/``positive`` because the
    carrier is evaluated as ``negative-positive``.  Map them explicitly so
    the resulting carrier is always Maestro ``p-n``.  The physical-profile
    values remain the replay fallback only.
    """

    mapping = cfg.get("vco_mapping")
    if isinstance(mapping, dict):
        p_node = _netlist_node(mapping.get("output_p"))
        n_node = _netlist_node(mapping.get("output_n"))
        if p_node and n_node:
            if p_node.lower() == n_node.lower():
                raise RuntimeError("bound Maestro differential outputs are identical")
            return {
                "negative": p_node,
                "positive": n_node,
                "source": "bound_maestro_p_minus_n",
            }
    output = dict(physical_profile(cfg)["main"]["output"])
    output["source"] = "physical_profile_fallback"
    return output


def flicker_template_path(cfg, point=None):
    """Return the exact private copy selected from the flicker Maestro history."""

    value = None
    if isinstance(point, dict):
        value = point.get("flicker_template_netlist")
    value = value or cfg.get("flicker_template_netlist")
    if not value:
        return None
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError("bound flicker Maestro template is absent: {}".format(path))
    return path


def _copy_regular_tree(source, target):
    """Copy one already-private Maestro tree and reject symlink traversal."""

    source = Path(source).expanduser().resolve()
    target = Path(target)
    if target.exists():
        shutil.rmtree(str(target))
    target.mkdir(parents=True, exist_ok=True)
    for current, directories, files in os.walk(str(source), topdown=True, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(source)
        target_current = target / relative
        target_current.mkdir(parents=True, exist_ok=True)
        for name in sorted(directories):
            if (current_path / name).is_symlink():
                raise RuntimeError(
                    "flicker Maestro template contains a directory symlink"
                )
        directories[:] = sorted(directories)
        for name in sorted(files):
            path = current_path / name
            if path.is_symlink():
                raise RuntimeError("flicker Maestro template contains a file symlink")
            if path.is_file():
                shutil.copy2(str(path), str(target_current / name))


def _continued_statements(text, first_line_pattern):
    lines = text.splitlines()
    output = []
    index = 0
    matcher = re.compile(first_line_pattern, flags=re.IGNORECASE)
    while index < len(lines):
        if not matcher.search(lines[index]):
            index += 1
            continue
        block = [lines[index]]
        while (
            block[-1].rstrip().endswith("\\")
            and index + 1 < len(lines)
        ):
            index += 1
            block.append(lines[index])
        while index + 1 < len(lines) and lines[index + 1].lstrip().startswith("+"):
            index += 1
            block.append(lines[index])
        output.append("\n".join(block))
        index += 1
    return output


def _template_include_lines(template_path):
    text = template_path.read_text(encoding="utf-8", errors="ignore")
    lines = [
        statement.rstrip("\r\n")
        for _, _, statement in _root_statements(text)
        if re.match(r"^\s*(?:include|ahdl_include)\s+", statement, flags=re.IGNORECASE)
    ]
    output = []
    for line in lines:
        match = re.search(r'^\s*(?:include|ahdl_include)\s+"([^"]+)"', line, flags=re.IGNORECASE)
        if not match:
            raise RuntimeError("flicker Maestro include path is not a literal string")
        include_path = Path(os.path.expanduser(match.group(1)))
        if not include_path.is_absolute():
            include_path = (template_path.parent / include_path).resolve()
        if not include_path.is_file():
            raise RuntimeError(
                "flicker Maestro model include is absent: {}".format(include_path)
            )
        output.append(
            line[: match.start(1)] + str(include_path) + line[match.end(1) :]
        )
    if not output:
        raise RuntimeError("flicker Maestro template has no model include")
    return output


def _template_device_tail(cfg, template_path):
    profile = physical_profile(cfg)
    replica = profile["replica"]
    instance = replica.get("dc_instance", "M1")
    original = replica["original_subckt"]
    text = template_path.read_text(encoding="utf-8", errors="ignore")
    pattern = re.compile(
        r"^\s*{}\s+\(\s*[^)]*\)\s+{}\b([\s\S]*)$".format(
            re.escape(instance), re.escape(original)
        ),
        flags=re.IGNORECASE,
    )
    # Cadence emits both backslash and '+' continuations. Keep the complete
    # root statement so a continued multiplicity/size is never discarded or
    # a dangling backslash joined to the following simulatorOptions line.
    matches = [match for _, _, statement in _root_statements(text)
               for match in [pattern.match(statement)] if match is not None]
    if len(matches) != 1:
        raise RuntimeError(
            "flicker Maestro target device {} / {} is not unique".format(
                instance, original
            )
        )
    return matches[0].group(1).strip()


def _template_parameter_statements(template_path):
    text = template_path.read_text(encoding="utf-8", errors="ignore")
    return [
        statement.rstrip("\r\n")
        for _, _, statement in _root_statements(text)
        if re.match(r"^\s*parameters\b", statement, flags=re.IGNORECASE)
    ]


def _template_simulator_options(template_path):
    text = template_path.read_text(encoding="utf-8", errors="ignore")
    options = []
    save_only_keys = {"save", "pwr", "subcktoppoint", "currents", "nestlvl", "useprobes"}
    for _, _, statement in _root_statements(text):
        match = re.match(r"^\s*(\S+)\s+options\b", statement, flags=re.IGNORECASE)
        if not match:
            continue
        if match.group(1).lower() == "saveoptions":
            keys = {name.lower() for name, _, _ in _assignment_spans(statement)}
            if not keys <= save_only_keys:
                raise RuntimeError(
                    "flicker Maestro saveOptions contains non-save simulator settings; "
                    "cannot safely replace it in the private replica"
                )
            continue
        options.append(statement.rstrip("\r\n"))
    if len(options) > 1:
        raise RuntimeError("flicker Maestro template has multiple simulator options blocks")
    return options[0] if options else None


_FILE_DIGEST_CACHE = {}


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _enforce_phase_candidate_limit(value):
    try:
        count = int(value)
    except (TypeError, ValueError):
        return
    if count > MAX_PHASE_CANDIDATE_HARMONICS:
        raise RuntimeError(
            "ISF-E1001: max_harmonic={} exceeds the exhaustive phase-candidate "
            "limit {}; reduce max_harmonic before running Spectre".format(
                count, MAX_PHASE_CANDIDATE_HARMONICS
            )
        )


def _safe_name_part(value, fallback, maximum=36):
    token = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value)).strip("_")
    return (token or fallback)[:maximum]


def _legacy_point_name(point):
    return "k_{:.1f}_{}".format(
        float(point["k"]), point["corner"]
    ).replace(".", "p")


def point_name(point):
    k_value = float(point["k"])
    if not math.isfinite(k_value):
        raise RuntimeError("point k must be finite")
    logical_identity = {"k": k_value}
    logical_identity.update(
        {
            key: point.get(key)
            for key in (
                "corner",
                "source_dir",
                "maestro_point_id",
                "maestro_design_point",
            )
            if key in point
        }
    )
    encoded = json.dumps(
        logical_identity,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    identity = hashlib.sha256(encoded).hexdigest()[:10]
    k_token = _safe_name_part(
        format(k_value, ".12g").replace(".", "p"), "0"
    )
    corner = _safe_name_part(point.get("corner"), "corner")
    return "k_{}_{}__{}".format(k_token, corner, identity)


def _file_cache_identity(value, hash_contents):
    if value in (None, ""):
        return None
    path = Path(str(value)).expanduser().resolve()
    identity = {"path": str(path)}
    if not path.is_file():
        identity["missing"] = True
        return identity
    stat = path.stat()
    identity.update(
        {
            "bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
        }
    )
    if hash_contents:
        cache_key = (
            str(path),
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )
        if cache_key not in _FILE_DIGEST_CACHE:
            _FILE_DIGEST_CACHE[cache_key] = _sha256_file(path)
        identity["sha256"] = _FILE_DIGEST_CACHE[cache_key]
    return identity


def _simulation_cache_identity(cfg, netlist_dir):
    netlist_dir = Path(netlist_dir)
    config_value = {
        str(key): value
        for key, value in cfg.items()
        if key != "_analysis_case_dir"
    }
    config_sha256 = hashlib.sha256(
        json.dumps(
            config_value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    dependencies = {}
    for field in (
        "model_file",
        "device_subckt_file",
        "_device_subckt_file",
        "physical_profile_path",
        "cadence_env",
    ):
        if cfg.get(field) not in (None, ""):
            dependencies[field] = _file_cache_identity(cfg[field], True)
    for field in ("spectre", "psf"):
        if cfg.get(field) not in (None, ""):
            dependencies[field] = _file_cache_identity(cfg[field], False)
    preload_values = str(cfg.get("ld_preload") or "").split(os.pathsep)
    for index, value in enumerate(preload_values):
        if value:
            dependencies["ld_preload[%d]" % index] = _file_cache_identity(
                value, False
            )
    identity = {
        "schema_version": 1,
        "netlist_sha256": _sha256_file(netlist_dir / "input.scs"),
        "config_sha256": config_sha256,
        "dependencies": dependencies,
    }
    identity["waveform_exchange"] = validate_waveform_assets(netlist_dir)
    identity["key_sha256"] = hashlib.sha256(
        json.dumps(
            identity, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return identity


def _validated_cached_run(contract_path, expected_identity):
    try:
        with Path(contract_path).open(encoding="utf-8") as handle:
            contract = json.load(handle)
    except (OSError, ValueError):
        raise RuntimeError(
            "ISF-E5001: existing Spectre cache has no readable versioned "
            "identity; preserve it or rerun explicitly with --force"
        )
    cached_identity = contract.get("cache_identity")
    if not isinstance(cached_identity, dict):
        raise RuntimeError(
            "ISF-E5001: legacy Spectre cache is unversioned and will not be "
            "reused; preserve it or rerun explicitly with --force"
        )
    if cached_identity != expected_identity:
        raise RuntimeError(
            "ISF-E5001: Spectre cache identity does not match the current "
            "netlist/config/tool inputs; preserve it or rerun explicitly "
            "with --force"
        )


def source_case_path(cfg, point):
    """Resolve one design point without permitting source_root escape."""
    resolve_source_path(
        cfg["source_root"],
        point["source_dir"],
        VCO_NETLIST_RELATIVE,
    )
    return Path(
        resolve_source_path(
            cfg["source_root"], point["source_dir"]
        )
    )


def source_input_path(cfg, point, relative_path):
    return Path(
        resolve_source_path(
            cfg["source_root"], point["source_dir"], relative_path
        )
    )


def run_command(args, cwd=None, cadence_env=None):
    if cadence_env:
        command = ". {} >/dev/null 2>&1; exec {}".format(
            shlex.quote(str(cadence_env)),
            " ".join(shlex.quote(str(item)) for item in args),
        )
        child_env = os.environ.copy()
        child_env["LC_ALL"] = "C"
        child_env["LANG"] = "C"
        return subprocess.check_output(
            ["/bin/bash", "-c", command],
            cwd=str(cwd) if cwd else None,
            env=child_env,
        ).decode("utf-8", "ignore")
    return subprocess.check_output(args, cwd=str(cwd) if cwd else None).decode(
        "utf-8", "ignore"
    )


def psf_trace(cfg, path, trace):
    return run_command(
        [cfg["psf"], "-s", "-t", trace, "-f", "%.16e", "-i", str(path)],
        cadence_env=cfg.get("cadence_env"),
    )


def parse_real_wave(cfg, path, trace):
    text = psf_trace(cfg, path, trace)
    x = []
    y = []
    current = None
    qtrace = '"{}"'.format(trace)
    for line in text.splitlines():
        stripped = line.strip()
        parts = stripped.split()
        if (
            (len(parts) == 2 and parts[0] in ('"time"', '"freq"', '"harmonic"'))
            or stripped.startswith('"relative frequency" ')
        ):
            try:
                current = float(stripped.rsplit(None, 1)[1].strip('"'))
            except ValueError:
                current = None
        elif current is not None and len(parts) == 2 and parts[0] == qtrace:
            try:
                x.append(current)
                y.append(float(parts[1].strip('"')))
            except ValueError:
                pass
    if not y:
        raise RuntimeError("No real trace {} in {}".format(trace, path))
    return np.asarray(x), np.asarray(y)


def parse_complex_wave(cfg, path, trace):
    text = psf_trace(cfg, path, trace)
    rows = []
    current = None
    qtrace = '"{}"'.format(trace)
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in ('"freq"', '"harmonic"'):
            try:
                current = float(parts[1].strip('"'))
            except ValueError:
                current = None
        elif current is not None and line.strip().startswith(qtrace):
            match = re.search(r"\(([-+0-9.eE]+)\s+([-+0-9.eE]+)\)", line)
            if match:
                rows.append((current, complex(float(match.group(1)), float(match.group(2)))))
    if not rows:
        raise RuntimeError("No complex trace {} in {}".format(trace, path))
    return rows


def nearest_complex(rows, target):
    return min(rows, key=lambda row: abs(row[0] - target))[1]


def exact_frequency_value(rows, target, offset_hz=None, label='PSF'):
    """Read a measured coordinate; never substitute an unrelated nearest point."""
    target = float(target)
    tolerance = max(1e-6, abs(target) * 1e-12,
                    abs(float(offset_hz if offset_hz is not None else target)) * 1e-8)
    matches = [(float(frequency), value) for frequency, value in rows
               if math.isfinite(float(frequency)) and abs(float(frequency) - target) <= tolerance]
    if len(matches) != 1:
        available = sorted(float(frequency) for frequency, _ in rows if math.isfinite(float(frequency)))
        raise RuntimeError('{}: requested frequency {:.16g} Hz has {} exact matches (tolerance {:.3g} Hz; available {})'.format(
            label, target, len(matches), tolerance, available[:8]))
    return matches[0][1]


def exact_hb_fundamental(cfg, psf_dir, trace=None):
    """HB fd coordinates retain precision that the PSF header may round away."""
    trace = trace or main_output(cfg)['negative']
    rows = parse_complex_wave(cfg, Path(psf_dir) / 'hb.fd.pss_hb', trace)
    positive = sorted(set(float(frequency) for frequency, _ in rows
                          if math.isfinite(float(frequency)) and float(frequency) > 0.0))
    if not positive:
        raise RuntimeError('No positive HB frequency coordinate in {}'.format(psf_dir))
    rounded = fundamental(cfg, psf_dir)
    candidates = [value for value in positive if abs(value - rounded) <= max(1e-6, abs(rounded) * 1e-5)]
    if len(candidates) != 1:
        raise RuntimeError('No unique absolute fundamental coordinate near the HB header in {}'.format(psf_dir))
    return candidates[0]


def fundamental(cfg, psf_dir):
    text = run_command(
        [cfg["psf"], "-i", str(Path(psf_dir) / "hb.fd.pss_hb")],
        cadence_env=cfg.get("cadence_env"),
    )
    match = re.search(r'^"fundamental"\s+([-+0-9.eE]+)', text, re.MULTILINE)
    if not match:
        raise RuntimeError("No HB fundamental in {}".format(psf_dir))
    return float(match.group(1))


def parse_struct(cfg, path, trace):
    """Return x -> numeric vector for a PSF contributor struct."""
    text = psf_trace(cfg, path, trace)
    out = {}
    current = None
    collecting = False
    values = []
    qtrace = '"{}"'.format(trace)
    for raw in text.splitlines():
        line = raw.strip()
        parts = line.split()
        if len(parts) == 2 and parts[0] in ('"freq"', '"relative'):
            # The relative-frequency label has three tokens and is handled below.
            pass
        if line.startswith('"freq" ') or line.startswith('"relative frequency" '):
            try:
                current = float(line.rsplit(None, 1)[1].strip('"'))
            except ValueError:
                current = None
        elif current is not None and line == qtrace + " (":
            collecting = True
            values = []
        elif collecting and line == ")":
            out[current] = values
            collecting = False
        elif collecting:
            try:
                values.append(float(line))
            except ValueError:
                pass
    if not out:
        raise RuntimeError("No struct trace {} in {}".format(trace, path))
    return out


def struct_value_at(cfg, path, trace, frequency, field_index):
    rows = parse_struct(cfg, path, trace)
    values = exact_frequency_value(list(rows.items()), frequency, label=str(path) + ':' + trace)
    if field_index >= len(values):
        raise RuntimeError("Field {} absent in {} ({})".format(field_index, trace, values))
    return values[field_index]


def noise_output_psd_at(cfg, path, trace, frequency, mechanism, *, expected_domain):
    """Read one named output mechanism, independent of compact-model layout.

    Ordinary/HB noise stores ``fn``; separatenoise stores ``fn.out`` beside
    distinct source PSD and gain fields. Neither a positional index nor a
    source field is an output-noise substitute.
    """
    dataset = parse_psf_ascii(psf_trace(cfg, path, trace))
    type_name = dataset["traces"].get(trace)
    descriptor = dataset["types"].get(type_name, {})
    fields = {item["name"] for item in descriptor.get("fields", [])}
    names = [name for name in (mechanism, mechanism + ".out") if name in fields]
    if len(names) != 1:
        raise RuntimeError("Expected one named {} output in {}: {}".format(mechanism, trace, names))
    return noise_quantity(dataset, frequency, trace, names[0], expected_domain=expected_domain)["psd"]


def wrap_phase(value):
    return (value + math.pi) % (2.0 * math.pi) - math.pi


def phase_deg(value):
    return math.degrees(math.atan2(value.imag, value.real))


def fourier_coefficients(wt, values, max_harmonic):
    """Fourier coefficients for x(t)=M0+2Re sum(Mk exp(j*k*wt))."""
    # NumPy 2.x removed the long-deprecated ``trapz`` alias.  Keep this
    # extractor portable across older IC618 hosts and current Python 3.11.
    integrate = getattr(np, "trapezoid", None)
    if integrate is None:
        integrate = np.trapz
    wt = np.asarray(wt, dtype=float)
    values = np.asarray(values, dtype=float)
    order = np.argsort(wt)
    wt = wt[order]
    values = values[order]
    if wt[0] > 1e-12:
        wt = np.r_[0.0, wt]
        values = np.r_[values[-1], values]
    if wt[-1] < 2.0 * math.pi - 1e-12:
        wt = np.r_[wt, 2.0 * math.pi]
        values = np.r_[values, values[0]]
    else:
        values[-1] = values[0]
    return {
        k: integrate(values * np.exp(-1j * k * wt), wt) / (2.0 * math.pi)
        for k in range(max_harmonic + 1)
    }


def read_dc_flicker(cfg, point, max_harmonic):
    noise_trace = dc_noise_trace(physical_profile(cfg))
    case_dir = cfg.get("_analysis_case_dir")
    local_root = Path(case_dir) / "dc_flicker" if case_dir else None
    local_csv = local_root / "iflicker_samples.csv" if local_root else None
    use_local = bool(local_csv and local_csv.is_file())
    sample_csv = (
        local_csv
        if use_local
        else source_input_path(cfg, point, "iflicker_samples.csv")
    )
    rows = []
    with sample_csv.open() as handle:
        for row in csv.DictReader(handle):
            rows.append(row)
    wt_all = np.array([float(row["wt_rad"]) for row in rows])
    flicker = []
    used_wt = []
    for index in range(1, len(rows) + 1):
        relative_noise = os.path.join(
            "sample_{:02d}".format(index),
            "psf",
            "noise.noise",
        )
        noise_path = (
            local_root / relative_noise
            if use_local
            else source_input_path(
                cfg,
                point,
                os.path.join("iflicker", relative_noise),
            )
        )
        if not noise_path.exists():
            # Historical sweeps store N independent points and represent 2*pi
            # by repeating point 1 in the CSV.  Close that endpoint explicitly.
            if index == len(rows) and abs(wt_all[index - 1] - 2.0 * math.pi) < 1e-9:
                flicker.append(flicker[0])
                used_wt.append(wt_all[index - 1])
                continue
            raise FileNotFoundError(noise_path)
        psd = noise_output_psd_at(
            cfg, noise_path, noise_trace, cfg["offset_hz"], "fn", expected_domain="V"
        )
        flicker.append(math.sqrt(max(psd, 0.0)))
        used_wt.append(wt_all[index - 1])
    wt = np.asarray(used_wt)
    return wt, np.asarray(flicker), fourier_coefficients(wt, flicker, max_harmonic)


def sample_periodic_wave(cfg, psf_dir, traces, samples=65):
    f0 = fundamental(cfg, psf_dir)
    period = 1.0 / f0
    result = {"f0": f0, "period": period}
    target = np.linspace(0.0, period, samples)
    for trace in traces:
        if trace == "0":
            result[trace] = np.zeros_like(target)
            continue
        times, values = parse_real_wave(cfg, Path(psf_dir) / "hb.td.pss_hb.tran", trace)
        times = times - times[0]
        mask = times <= period * (1.0 + 1e-6)
        times = times[mask]
        values = values[mask]
        if times[-1] < period:
            times = np.r_[times, period]
            values = np.r_[values, values[0]]
        sampled = np.interp(target, times, values)
        sampled[-1] = sampled[0]
        result[trace] = sampled
    result["time"] = target
    return result


def carrier_phasor(cfg, psf_dir, frequency):
    output = main_output(cfg)
    path = Path(psf_dir) / "hb.fd.pss_hb"
    positive, negative = (
        0j if node == "0" else nearest_complex(parse_complex_wave(cfg, path, node), frequency)
        for node in (output["negative"], output["positive"])
    )
    return positive - negative


def source_wave(name, p, n, times, values, period):
    tokens = []
    for tval, value in zip(times, values):
        tokens.extend(["{:.16e}".format(tval), "{:.16e}".format(value)])
    lines = ["{} ({} {}) vsource type=pwl wave=[".format(name, p, n)]
    for index in range(0, len(tokens), 8):
        lines.append("+ " + " ".join(tokens[index : index + 8]))
    lines.append(
        "+ ] pwlperiod={:.16e} pwlperiodstart=0 twidth={:.16e}".format(
            period, period / 10000.0
        )
    )
    return "\n".join(lines)


def injected_nmos_subckt(cfg):
    """Copy the configured MOS subckt and add a unit intrinsic PAC source."""
    replica = physical_profile(cfg)["replica"]
    original = replica["original_subckt"]
    generated = replica["generated_subckt"]
    intrinsic = replica["intrinsic"]
    intrinsic_instance = intrinsic["instance"]
    primitive = intrinsic["primitive"]
    terminals = intrinsic["terminals"]
    injection_source = intrinsic["injection_source"]
    model_path = cfg.get("_device_subckt_file") or resolve_device_subckt_file(
        cfg.get("model_file"),
        physical_profile(cfg),
        configured=cfg.get("device_subckt_file"),
    )
    if not model_path:
        raise RuntimeError(
            "Could not locate {} in read-only PDK include chain".format(original)
        )
    if cfg.get("replica_source") == "generated":
        from isf_tool.core.generated_flicker import active_subckt
        _, block = active_subckt(model_path, cfg.get("device_subckt_section"), original)
    else:
        text = Path(model_path).read_text(encoding="utf-8", errors="ignore")
        match = re.search(
            r"^\s*(?:inline\s+)?subckt\s+{}\s*\([^\n]+\)[\s\S]*?^\s*ends\s+{}\s*$".format(
                re.escape(original), re.escape(original)
            ),
            text,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        block = match.group(0) if match else None
    if not block:
        raise RuntimeError(
            "Could not extract {} from read-only PDK".format(original)
        )
    block = re.sub(
        r"^\s*(?:inline\s+)?subckt\s+{}\b".format(re.escape(original)),
        "subckt {}".format(generated),
        block,
        count=1,
        flags=re.IGNORECASE,
    )
    block = re.sub(
        r"^\s*ends\s+{}\s*$".format(re.escape(original)),
        "ends {}".format(generated),
        block,
        count=1,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    terminal_pattern = r"\s+".join(re.escape(item) for item in terminals)
    block, count = re.subn(
        r"^(\s*{}\s+\(\s*{}\s*\)\s+{}\b)".format(
            re.escape(intrinsic_instance),
            terminal_pattern,
            re.escape(primitive),
        ),
        "{} ({} {}) isource dc=0 type=dc pacmag=1 pacphase=0\n\\1".format(
            injection_source, terminals[0], terminals[2]
        ),
        block,
        count=1,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if count != 1:
        raise RuntimeError("Could not place intrinsic deterministic injection")
    return block


def _transfer_contract_for_reading(cfg):
    """Do not silently replace a selected output when its read location is lost."""
    mapping = cfg.get('vco_mapping') or {}
    contract = mapping.get('transfer_contract') or {}
    selected = mapping.get('transfer_output') or mapping.get('transfer_phase_output')
    if contract.get('mode') == 'invalid' or (selected and contract.get('mode') != 'existing_source'):
        raise RuntimeError('Selected transfer output has no usable raw read location; no private-source fallback is permitted')
    return contract


def prepare_main_netlist(cfg, source_path, target_path):
    profile = physical_profile(cfg)
    main = profile["main"]
    terminals = main["terminals"]
    output = main_output(cfg)
    probe = main["drain_probe"]
    injection = main["injection"]
    instance = main["target_instance"]
    subckt = main["device_subckt"]
    offset = "{:.16g}".format(float(cfg["offset_hz"]))
    text = Path(source_path).read_text(encoding="utf-8")
    # Preserve user-selected analysis instances at the input boundary. The
    # private copy uses the canonical result names consumed by the extractor.
    mapping = cfg.get("vco_mapping", {})
    transfer = _transfer_contract_for_reading(cfg)
    if transfer.get('mode') == 'existing_source':
        if canonical_net(main_injection_trace(profile)) == canonical_net(transfer.get('source')):
            raise RuntimeError('The private injection may not overwrite the selected existing current source')
        # The user owns the physical meaning of the selected current-transfer
        # outputs. Do not requalify its MOS branch/model or the old measurement
        # terminals here. The private HBXF below uses the chosen output pair;
        # extraction still requires the requested raw source/bands to exist.
    for key, canonical in (("periodic_analysis", "hb"), ("noise_analysis", "hbnoise"), ("transfer_analysis", "hbxf")):
        selected = mapping.get(key) or canonical
        if selected != canonical:
            if re.search(r"(?m)^\s*" + re.escape(canonical) + r"\s*\(", text):
                raise RuntimeError("private analysis name collision: " + canonical)
            text, renamed = re.subn(
                r"(?m)^([ \t]*)" + re.escape(selected) + r"(?=[ \t]*\([^\n]*\)[ \t]+" + canonical + r"\b)",
                lambda match: match.group(1) + canonical, text,
            )
            if renamed != 1:
                raise RuntimeError("selected analysis is not unique: " + selected)
    target_pattern = (
        r"^([ \t]*){}\s+\(\s*{}\s+{}\s+{}\s+{}\s*\)\s+({}\b[^\n]*)$"
    ).format(
        re.escape(instance),
        re.escape(terminals["drain"]),
        re.escape(terminals["gate"]),
        re.escape(terminals["source"]),
        re.escape(terminals["bulk"]),
        re.escape(subckt),
    )
    target_matches = list(
        re.finditer(
            target_pattern,
            text,
            flags=re.MULTILINE | re.IGNORECASE,
        )
    )
    if len(target_matches) != 1:
        raise RuntimeError(
            "Target {} terminal/subckt signature occurs {} times; refine the physical profile".format(
                instance, len(target_matches)
            )
        )
    injection_pattern = (
        r"^([ \t]*){}\s+\(\s*[^\s()]+\s+[^\s()]+\s*\)\s+"
        r"(isource\b[^\n]*)$"
    ).format(re.escape(injection["source"]))
    has_existing_injection = bool(
        re.search(injection_pattern, text, flags=re.MULTILINE | re.IGNORECASE)
    )

    def insert_probe(match):
        indent = match.group(1)
        device_tail = match.group(2)
        local_injection = ""
        if not has_existing_injection:
            local_injection = (
                "{indent}{source_name} ({positive} {negative}) "
                "isource dc=0 type=dc\n"
            ).format(
                indent=indent,
                source_name=injection["source"],
                positive=injection["positive"],
                negative=injection["negative"],
            )
        return (
            "{indent}{probe} ({drain} {node}) vsource dc=0 type=dc\n"
            "{local_injection}"
            "{indent}{instance} ({node} {gate} {source} {bulk}) {tail}"
        ).format(
            indent=indent,
            probe=probe["source"],
            drain=terminals["drain"],
            node=probe["node"],
            instance=instance,
            gate=terminals["gate"],
            source=terminals["source"],
            bulk=terminals["bulk"],
            tail=device_tail,
            local_injection=local_injection,
        )

    text, count = re.subn(
        target_pattern,
        insert_probe,
        text,
        count=1,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if count != 1:
        raise RuntimeError(
            "Could not insert {} drain probe using physical profile".format(
                instance
            )
        )
    text, count = re.subn(
        injection_pattern,
        "\\1{} ({} {}) \\2".format(
            injection["source"],
            injection["positive"],
            injection["negative"],
        ),
        text,
        count=1,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if count == 0:
        if has_existing_injection:
            raise RuntimeError(
                "Could not align {} with drain-to-source direction".format(
                    injection["source"]
                )
            )
        # The source was inserted next to the target MOS, preserving the same
        # subcircuit scope and therefore the actual drain/source branch.
    elif count != 1:
        raise RuntimeError(
            "Could not align {} with drain-to-source direction".format(
                injection["source"]
            )
        )
    text, count = re.subn(
        r"^\s*hbnoise\s+\([^\n]*\)\s+hbnoise[\s\S]*?annotate=status\s*$",
        "hbnoise ({} {}) hbnoise sweeptype=relative\n"
        "+ relharmvec=[1] values=[{}] maxsideband={}\n"
        "+ noiseout=[usb lsb am pm] saveallsidebands=yes separatenoise=yes annotate=status".format(
            output["negative"],
            output["positive"],
            offset,
            cfg["main_max_sideband"],
        ),
        text,
        count=1,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if count != 1:
        raise RuntimeError("Could not patch main HBNOISE")
    text, count = re.subn(
        r"^\s*hbxf\s+\([^\n]*\)\s+hbxf[\s\S]*?annotate=status\s*$",
        "hbxf ({} {}) hbxf sweeptype=relative relharmvec=[1]\n"
        "+ values=[{}] maxsideband={} freqaxis=out annotate=status".format(
            output["negative"],
            output["positive"],
            offset,
            cfg["main_max_sideband"],
        ),
        text,
        count=1,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if count == 0:
        hbxf_statement = (
            "hbxf ({} {}) hbxf sweeptype=relative relharmvec=[1]\n"
            "+ values=[{}] maxsideband={} freqaxis=out annotate=status"
        ).format(
            output["negative"],
            output["positive"],
            offset,
            cfg["main_max_sideband"],
        )
        # Periodic small-signal analyses must remain in the HB analysis chain.
        # Appending HBXF after info/save statements makes Spectre 18.1 report
        # SPCRTRF-15225 even though HB completed.  Insert it immediately after
        # the patched HBNOISE statement in the private copy.
        text, inserted = re.subn(
            r"(^\s*hbnoise\s+\([^\n]*\)\s+hbnoise[\s\S]*?annotate=status\s*$)",
            r"\1\n" + hbxf_statement,
            text,
            count=1,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        if inserted != 1:
            raise RuntimeError("Could not place private HBXF after HBNOISE")
    elif count != 1:
        raise RuntimeError("Could not patch main HBXF")
    traces = main["traces"]
    text += "\nsave {} {} {} {} {} {} {}\n".format(
        output["positive"],
        output["negative"],
        traces["gate"],
        traces["source"],
        traces["bulk"],
        drain_probe_trace(profile),
        traces["drain"],
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(text, encoding="utf-8")


def prepare_replica_netlist(cfg, wave, target_path, point=None):
    profile = physical_profile(cfg)
    main_traces = profile["main"]["traces"]
    replica = profile["replica"]
    f0 = wave["f0"]
    period = wave["period"]
    offset = cfg["offset_hz"]
    maxh = cfg["hb_max_harmonic"]
    maxsb = cfg["main_max_sideband"]
    template_path = flicker_template_path(cfg, point)
    template_parameters = (
        _template_parameter_statements(template_path) if template_path else []
    )
    template_options = (
        _template_simulator_options(template_path) if template_path else None
    )
    device_parameters = (
        _template_device_tail(cfg, template_path)
        if template_path
        else replica["device_parameters"]
    )
    target_path = Path(target_path)
    exchange = export_waveforms(
        wave, main_traces, target_path.parent, cfg.get("flicker_mapping"),
        provenance={"point": point or {}, "source": "fresh main_vco HB in the same operating-point directory"},
    )
    by_semantic = {row["semantic"]: row for row in exchange["channels"]}
    parameter_text = set_root_parameters("\n".join(template_parameters), {
        row["variable"]: json.dumps(row["relative_file"]) for row in exchange["channels"]
    })
    parameter_text = set_root_parameters(parameter_text, {"ISF_F0": "%.16e" % f0, "ISF_T0": "%.16e" % period})
    def exchanged_source(name, positive, negative, semantic):
        return ("{} ({} {}) vsource type=pwl file={} pwlperiod=ISF_T0 "
                "pwlperiodstart=0 twidth={:.16e}").format(
                    name, positive, negative, by_semantic[semantic]["variable"], period / 10000.0)
    parts = [
        "// Private replica generated by dynamic_isf_closure.py",
        "// ISF waveform-exchange-v1",
        "// Device/model/options source template: {}".format(
            template_path or "legacy physical profile"
        ),
        "simulator lang=spectre",
        "global 0",
    ]
    parts.append(parameter_text)
    if template_path:
        parts.extend(_template_include_lines(template_path))
    elif replica["model_sections"]:
        for section in replica["model_sections"]:
            parts.append('include "{}" section={}'.format(cfg["model_file"], section))
    else:
        parts.append('include "{}"'.format(cfg["model_file"]))
    parts.append(injected_nmos_subckt(cfg))
    parts.extend(
        [
            exchanged_source("VG", "G", "S", "VGS"),
            exchanged_source("VD", "DEXT", "S", "VDS"),
            exchanged_source("VS", "S", "0", "VS"),
            exchanged_source("VB", "B", "S", "VBS"),
            "{} (DEXT D) vsource dc=0 type=dc".format(replica["sense_source"]),
            "{} (D G S B) {} {}".format(
                replica["instance"],
                replica["generated_subckt"],
                device_parameters,
            ),
            template_options
            or "simulatorOptions options psfversion=\"1.1.0\" reltol=1e-3 vabstol=1e-6 "
            "iabstol=1e-12 temp=27 tnom=27 gmin=1e-12 maxnotes=1000 maxwarns=1000",
            "hb hb fundfreqs=[ISF_F0] maxharms=[{}] tstab={} errpreset=conservative "
            "oversample=[1] annotate=status".format(maxh, "{:.16e}".format(2.0 * period)),
            "hbac hbac sweeptype=absolute values=[{}] maxsideband={} "
            "freqaxis=out annotate=status".format(offset, maxh),
            "hn0 hbnoise oprobe={} sweeptype=relative relharmvec=[0] values=[{}] "
            "maxsideband={} noiseout=[usb lsb] saveallsidebands=yes separatenoise=yes "
            "annotate=status".format(replica["sense_source"], offset, maxsb),
        ]
    )
    for k in range(1, cfg["max_harmonic"] + 1):
        parts.append(
            "hn{0} hbnoise oprobe={3} sweeptype=relative relharmvec=[{0}] "
            "values=[{1}] maxsideband={2} noiseout=[usb lsb am pm] "
            "saveallsidebands=yes separatenoise=yes annotate=status".format(
                k, offset, maxsb, replica["sense_source"]
            )
        )
    parts.extend(
        [
            "modelParameter info what=models where=rawfile",
            "element info what=inst where=rawfile",
            "outputParameter info what=output where=rawfile",
            "designParamVals info what=parameters where=rawfile",
            "save G D DEXT S B {}:p".format(replica["sense_source"]),
            "saveOptions options save=all pwr=all subcktoppoint=yes",
            "",
        ]
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("\n".join(parts), encoding="utf-8")


def model_include_lines(cfg, point=None):
    template_path = flicker_template_path(cfg, point)
    if template_path:
        return _template_include_lines(template_path)
    replica = physical_profile(cfg)["replica"]
    if replica["model_sections"]:
        return [
            'include "{}" section={}'.format(cfg["model_file"], section)
            for section in replica["model_sections"]
        ]
    return ['include "{}"'.format(cfg["model_file"])]


def _replace_template_parameter(text, name, value):
    # A DC sample overrides the selected root design variable only.  Names in
    # subcircuit defaults, device parameters, comments or a differently cased
    # variable must remain untouched; the shared parser rejects ambiguity.
    updated, _changes = restore_operating_point_text(
        text,
        {
            "params": {str(name): "{:.16e}".format(float(value))},
            "parameter_records": [{"name": str(name), "type": "desVar"}],
        },
    )
    return updated


def prepare_dc_noise_netlist(cfg, biases, target_path, point=None):
    """Patch the selected flicker Maestro TB for one four-terminal sample."""

    template_path = flicker_template_path(cfg, point)
    if template_path:
        target_path = Path(target_path)
        _copy_regular_tree(template_path.parent, target_path.parent)
        text = target_path.read_text(encoding="utf-8", errors="ignore")
        mapping = cfg.get("flicker_mapping", {})
        bias_roles = (
            ("vgs_variable", "VGS_V"),
            ("vds_variable", "VDS_V"),
            ("vbs_variable", "VBS_V"),
        )
        generated = cfg.get("replica_source") == "generated"
        if generated:
            bias_roles += (("vs_variable", "VS_V"),)
        for logical, bias_key in bias_roles:
            variable = mapping.get(logical) if isinstance(mapping, dict) else None
            if variable:
                text = _replace_template_parameter(text, variable, biases[bias_key])
            elif generated or logical in ("vgs_variable", "vds_variable"):
                raise RuntimeError(
                    "flicker Maestro mapping lacks {}".format(logical)
                )
        text = prepare_dc_bias_sources(text, mapping)
        analysis_name = (
            mapping.get("noise_analysis") if isinstance(mapping, dict) else None
        ) or "noise"
        noise_pattern = (
            r"^([ \t]*{}[ \t]*\([^\n)]*\)[ \t]+noise\b)"
            r"[\s\S]*?annotate=status[ \t]*(?=\n|$)"
        ).format(re.escape(str(analysis_name)))
        replacement = (
            r"\1 sweeptype=absolute values=[{:.16e}] annotate=status"
        ).format(float(cfg["offset_hz"]))
        text, count = re.subn(
            noise_pattern,
            replacement,
            text,
            count=1,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        if count != 1:
            raise RuntimeError(
                "could not patch mapped flicker noise analysis {}".format(
                    analysis_name
                )
            )
        if analysis_name != "noise":
            if re.search(r"(?m)^\s*noise\s*\(", text):
                raise RuntimeError("private analysis name collision: noise")
            text = re.sub(
                r"(?m)^([ \t]*)" + re.escape(str(analysis_name)) + r"(?=[ \t]*\([^\n]*\)[ \t]+noise\b)",
                lambda match: match.group(1) + "noise", text, count=1,
            )
        provenance = (
            "// ISF private DC sample; derived from {}\n"
            "// template={}\n"
        ).format("the selected VCO MOS" if generated else "bound flicker Maestro history", template_path)
        target_path.write_text(provenance + text, encoding="utf-8")
        return

    # Legacy/replay compatibility only.  Pair-mode Live always takes the
    # template branch above.

    profile = physical_profile(cfg)
    replica = profile["replica"]
    dc_instance = replica.get("dc_instance", "M1")
    parts = [
        "// Private four-terminal DC flicker sample generated by the ISF tool",
        "simulator lang=spectre",
        "global 0",
    ]
    parts.extend(model_include_lines(cfg, point))
    parts.extend(
        [
            "VG (G 0) vsource dc={:.16e} type=dc".format(biases["VG_V"]),
            "VD (DEXT 0) vsource dc={:.16e} type=dc".format(biases["VD_V"]),
            "VS (S 0) vsource dc={:.16e} type=dc".format(biases["VS_V"]),
            "VB (B 0) vsource dc={:.16e} type=dc".format(biases["VB_V"]),
            "IDC_SENSE (DEXT D) vsource dc=0 type=dc",
            "{} (D G S B) {} {}".format(
                dc_instance,
                replica["original_subckt"],
                replica["device_parameters"],
            ),
            "HNOISE (noise_out 0) ccvs rm=1 type=ccvs probe=IDC_SENSE",
            "simulatorOptions options psfversion=\"1.1.0\" reltol=1e-3 "
            "vabstol=1e-6 iabstol=1e-12 temp=27 tnom=27 gmin=1e-12 "
            "maxnotes=1000 maxwarns=1000",
            "dcOp dc maxiters=150 maxsteps=10000 annotate=status",
            "noise (noise_out 0) noise sweeptype=absolute values=[{:.16e}] "
            "annotate=status".format(float(cfg["offset_hz"])),
            "modelParameter info what=models where=rawfile",
            "element info what=inst where=rawfile",
            "outputParameter info what=output where=rawfile",
            "designParamVals info what=parameters where=rawfile",
            "saveOptions options save=allpub subcktoppoint=yes",
            "",
        ]
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("\n".join(parts), encoding="utf-8")


def _dc_samples_identity(cfg, point, wave, sample_count):
    template = flicker_template_path(cfg, point)
    profile = physical_profile(cfg)
    traces = profile['main']['traces']
    waveform = {'f0': float(wave['f0']), 'period': float(wave['period']),
                'time': np.asarray(wave['time'], dtype=float).tolist()}
    for key in ('gate', 'drain', 'source', 'bulk'):
        waveform[key] = np.asarray(wave[traces[key]], dtype=float).tolist()
    config_value = {key: value for key, value in cfg.items() if key != '_analysis_case_dir'}
    template_files = []
    if template:
        for path in sorted(template.parent.rglob('*')):
            if path.is_symlink():
                raise RuntimeError('DC noise template contains a symlink: ' + str(path))
            if path.is_file():
                template_files.append((str(path.relative_to(template.parent)), _sha256_file(path)))
    value = {'schema_version': 1, 'point': point, 'sample_count': sample_count,
             'config': config_value, 'template_files': template_files, 'waveform': waveform,
             'dependencies': {key: _file_cache_identity(cfg[key], True)
                              for key in ('model_file', 'device_subckt_file', '_device_subckt_file', 'cadence_env')
                              if cfg.get(key)}}
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, default=str).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _dc_sample_artifacts(root, sample_count):
    records = []
    for index in range(1, sample_count + 1):
        sample = root / 'sample_{:02d}'.format(index)
        for relative in ('netlist/input.scs', 'run_environment.json', 'psf/noise.noise', 'psf/spectre.out'):
            path = sample / relative
            records.append({'path': str(path.relative_to(root)), 'sha256': _sha256_file(path)})
        if not (sample / 'psf/.simDone').is_file():
            raise RuntimeError('DC noise sample completion is missing: ' + str(sample))
    return records


def ensure_dc_flicker_samples(cfg, point, case_dir, wave, force=False):
    """Reuse audited legacy samples, otherwise generate them under run_root."""

    try:
        legacy_csv = source_input_path(cfg, point, "iflicker_samples.csv")
    except ValueError:
        raise
    if legacy_csv.is_file():
        return "legacy-source"

    sample_count = max(int(cfg.get("dc_noise_samples", 24)), 8)
    if sample_count < 2 * int(cfg["max_harmonic"]) + 1:
        raise RuntimeError(
            "dc_noise_samples must exceed twice max_harmonic"
        )
    root = Path(case_dir) / "dc_flicker"
    marker = root / ".samplesDone"
    identity = _dc_samples_identity(cfg, point, wave, sample_count)
    if marker.is_file() and not force:
        try:
            cached = json.loads(marker.read_text(encoding='utf-8'))
            if cached.get('schema_version') != 1 or cached.get('input_sha256') != identity:
                raise ValueError('input identity mismatch')
            if cached.get('samples_csv_sha256') != _sha256_file(root / 'iflicker_samples.csv'):
                raise ValueError('sample table changed')
            if cached.get('artifacts') != _dc_sample_artifacts(root, sample_count):
                raise ValueError('sample artifacts changed')
            for index in range(1, sample_count + 1):
                sample = root / 'sample_{:02d}'.format(index)
                _validated_cached_run(sample / 'run_environment.json', _simulation_cache_identity(cfg, sample / 'netlist'))
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            raise RuntimeError('ISF-E5001: DC sample cache is unversioned or differs from current configuration/template/waveforms; preserve it or rerun with --force: ' + str(exc)) from exc
        return "cached"
    if force and root.exists():
        shutil.rmtree(str(root))
    root.mkdir(parents=True, exist_ok=True)
    profile = physical_profile(cfg)
    traces = profile["main"]["traces"]
    mapping = cfg.get("flicker_mapping", {})
    template_path = flicker_template_path(cfg, point)
    if (
        template_path
        and isinstance(mapping, dict)
        and not mapping.get("vbs_variable")
    ):
        vbs = np.asarray(wave[traces["bulk"]]) - np.asarray(
            wave[traces["source"]]
        )
        tolerance = float(cfg.get("vbs_tolerance_v", 1.0e-6))
        if float(np.max(np.abs(vbs))) > tolerance:
            raise RuntimeError(
                "flicker Maestro has no VBS mapping but VCO |VBS(t)| exceeds {:.3g} V".format(
                    tolerance
                )
            )
    wt_values = np.linspace(0.0, 2.0 * math.pi, sample_count + 1)
    time_values = wt_values / (2.0 * math.pi) * wave["period"]
    rows = []
    for index, (wt, time_s) in enumerate(zip(wt_values, time_values), 1):
        values = {}
        for label, trace in (
            ("VG_V", traces["gate"]),
            ("VD_V", traces["drain"]),
            ("VS_V", traces["source"]),
            ("VB_V", traces["bulk"]),
        ):
            values[label] = float(
                np.interp(time_s, wave["time"], wave[trace])
            )
        row = {
            "sample": index,
            "time_s": time_s,
            "wt_rad": wt,
            **values,
        }
        row["VGS_V"] = row["VG_V"] - row["VS_V"]
        row["VDS_V"] = row["VD_V"] - row["VS_V"]
        row["VBS_V"] = row["VB_V"] - row["VS_V"]
        rows.append(row)
        if index > sample_count:
            continue
        netlist = root / "sample_{:02d}".format(index) / "netlist" / "input.scs"
        prepare_dc_noise_netlist(cfg, row, netlist, point=point)
        run_spectre(cfg, netlist.parent, force=force)
    with (root / "iflicker_samples.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    marker.write_text(json.dumps({'schema_version': 1, 'input_sha256': identity,
        'sample_count': sample_count, 'samples_csv_sha256': _sha256_file(root / 'iflicker_samples.csv'),
        'artifacts': _dc_sample_artifacts(root, sample_count)}, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    return "generated"


NUMERICAL_QUALITY_PATTERNS = (
    (
        "HB_STEADY_STATE_NOT_DETECTED",
        r"steady-state has not been detected",
    ),
    (
        "MODEL_RESULTS_DECLARED_INCORRECT",
        r"results computed by Spectre\(R\) are now incorrect",
    ),
    (
        "ANALYSIS_FAILED_TO_CONVERGE",
        r"(?:analysis|simulation).*failed to converge",
    ),
    (
        "MODEL_MISSING_JUNCTION_FORWARD_BIAS",
        r"\bMissing\s+bulk-(?:drain|source)\s+diode\s+would\s+be\s+forward\s+biased\b",
    ),
)


def spectre_log_quality(log_path):
    """Classify warnings that make numerical signoff unsafe.

    A smoke run may still be inspected when Spectre exits with zero errors, but
    these explicit warnings are carried into the report and block formal
    export.  Benign deprecation/LTE warnings remain visible in spectre.out and
    are not promoted here.
    """

    log_path = Path(log_path)
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    issues = [
        name
        for name, pattern in NUMERICAL_QUALITY_PATTERNS
        if re.search(pattern, text, flags=re.IGNORECASE)
    ]
    return {
        "pass": not issues,
        "issues": issues,
        "path": str(log_path),
        "code": "" if not issues else "ISF-W3002",
    }


def aggregate_spectre_quality(records):
    issues = []
    for stage, log_path in records:
        quality = spectre_log_quality(log_path)
        issues.extend(
            "{}:{}".format(stage, item) for item in quality["issues"]
        )
    return {
        "pass": not issues,
        "issues": issues,
        "code": "" if not issues else "ISF-W3002",
    }


def verify_spectre(psf_dir):
    log_path = Path(psf_dir) / "spectre.out"
    if not log_path.exists():
        raise RuntimeError("Missing {}".format(log_path))
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    if not re.search(r"spectre completes with 0 errors", text):
        raise RuntimeError("Spectre did not complete cleanly: {}".format(log_path))
    (Path(psf_dir) / ".simDone").write_text("owned direct run; spectre 0 errors\n")


def run_spectre(cfg, netlist_dir, force=False):
    netlist_dir = Path(netlist_dir)
    psf_dir = netlist_dir.parent / "psf"
    if force and psf_dir.exists():
        shutil.rmtree(str(psf_dir))
    psf_dir.mkdir(parents=True, exist_ok=True)
    cadence_env = cfg.get("cadence_env")
    execution_contract = {
        "schema_version": 1,
        "execution_mode": "fresh_simulation",
        "spectre": str(cfg["spectre"]),
        "cadence_env": str(cadence_env) if cadence_env else None,
        "cadence_env_applied": bool(cadence_env),
        "license_configured": bool(cfg.get("license_file")),
        "ld_preload_configured": bool(cfg.get("ld_preload")),
        "analysis_python": sys.executable,
    }
    cache_identity = _simulation_cache_identity(cfg, netlist_dir)
    contract_path = netlist_dir.parent / "run_environment.json"
    if (psf_dir / ".simDone").exists() and not force:
        _validated_cached_run(contract_path, cache_identity)
        verify_spectre(psf_dir)
        return "cached"
    spectre_command = (
        "exec {} -64 input.scs +escchars +log ../psf/spectre.out "
        "-format psfxl -raw ../psf +lqtimeout 900 -maxw 1000 -maxn 1000 +logstatus"
    ).format(shlex.quote(str(cfg["spectre"])))
    cmd = (
        ". {} && {}".format(
            shlex.quote(str(cadence_env)), spectre_command
        )
        if cadence_env
        else spectre_command
    )
    child_env = os.environ.copy()
    if cfg.get("ld_preload"):
        child_env["LD_PRELOAD"] = cfg["ld_preload"]
    if cfg.get("license_file"):
        child_env["CDS_LIC_FILE"] = cfg["license_file"]
        child_env["LM_LICENSE_FILE"] = cfg["license_file"]
    completed = subprocess.run(
        ["/bin/bash", "-lc", cmd],
        cwd=str(netlist_dir),
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    (netlist_dir.parent / "spectre_stdout.txt").write_bytes(completed.stdout)
    if completed.returncode != 0:
        raise RuntimeError(
            "Spectre failed code {} in {}".format(completed.returncode, netlist_dir)
        )
    verify_spectre(psf_dir)
    spectre_log = psf_dir / "spectre.out"
    execution_contract.update(
        {
            "simulation_completed": True,
            "recorded_at_epoch": time.time(),
            "spectre_out_mtime_ns": spectre_log.stat().st_mtime_ns,
            "spectre_out_bytes": spectre_log.stat().st_size,
        }
    )
    execution_contract["schema_version"] = 2
    execution_contract["cache_identity"] = cache_identity
    contract_path.write_text(
        _strict_json_dumps(execution_contract, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return "simulated"


def prepare_and_run_point(cfg, point, run_root, force=False):
    traces = physical_profile(cfg)["main"]["traces"]
    name = point_name(point)
    source_netlist = source_input_path(
        cfg, point, VCO_NETLIST_RELATIVE
    )
    case_dir = Path(run_root) / name
    main_netlist = case_dir / "main_vco" / "netlist" / "input.scs"
    prepare_main_netlist(cfg, source_netlist, main_netlist)
    main_status = run_spectre(cfg, main_netlist.parent, force=force)
    wave = sample_periodic_wave(
        cfg,
        case_dir / "main_vco" / "psf",
        list(
            dict.fromkeys(
                (
                    traces["drain"],
                    traces["gate"],
                    traces["source"],
                    traces["bulk"],
                )
            )
        ),
        samples=65,
    )
    dc_status = ensure_dc_flicker_samples(
        cfg, point, case_dir, wave, force=force
    )
    replica_netlist = case_dir / "replica" / "netlist" / "input.scs"
    prepare_replica_netlist(cfg, wave, replica_netlist, point=point)
    replica_status = run_spectre(cfg, replica_netlist.parent, force=force)
    return name, main_status, replica_status, dc_status


def noise_file(psf_dir, analysis, output_kind):
    # Spectre 18.1 names USB (the default noiseout) without a `.usb` infix.
    if output_kind == "usb":
        expected = Path(psf_dir) / "{}.pnoise_hbnoise".format(analysis)
    else:
        expected = Path(psf_dir) / "{}.{}.pnoise_hbnoise".format(analysis, output_kind)
    if expected.exists():
        return expected
    matches = list(Path(psf_dir).glob("{}.*{}.pnoise_hbnoise".format(analysis, output_kind)))
    if len(matches) == 1:
        return matches[0]
    raise RuntimeError("Missing {} {} noise result in {}".format(analysis, output_kind, psf_dir))


def extract_hbnoise_fn(cfg, psf_dir, analysis, output_kind, trace, *, expected_domain):
    return noise_output_psd_at(
        cfg,
        noise_file(psf_dir, analysis, output_kind),
        trace,
        cfg["offset_hz"],
        "fn",
        expected_domain=expected_domain,
    )


def parse_hbnoise_total(text, frequency, *, source_path="", analysis="hbnoise"):
    """Read independent output ASD and named contributor PSDs from one PSF dump.

    Spectre's scalar ``out`` is V/sqrt(Hz), whereas device STRUCT fields
    are V^2/Hz. Field names come from each actual TYPE schema. Device
    ``total`` rows are provenance, excluded from the mechanism list so the
    display never double-counts them with their individual mechanisms.
    """
    sections = {}
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if line in ("HEADER", "TYPE", "SWEEP", "TRACE", "VALUE", "END"):
            section = line
            sections.setdefault(section, [])
        elif section is not None:
            sections[section].append(line)
    schemas, scalar_units = {}, {}
    struct_name = None
    field = None
    scalar_name = None
    for line in sections.get("TYPE", []):
        match = re.match(r'^"([^"]+)" STRUCT\($', line)
        if match:
            struct_name = match.group(1)
            schemas[struct_name] = []
            field = scalar_name = None
            continue
        if line.startswith(") PROP("):
            struct_name = None
            field = scalar_name = None
            continue
        match = re.match(r'^"([^"]+)" FLOAT DOUBLE(?: PROP\()?$', line)
        if match:
            if struct_name is not None:
                field = {"name": match.group(1), "units": None}
                schemas[struct_name].append(field)
            else:
                scalar_name = match.group(1)
            continue
        match = re.match(r'^"units" "([^"]+)"$', line)
        if match:
            if struct_name is not None and field is not None:
                field["units"] = match.group(1)
            elif scalar_name is not None:
                scalar_units[scalar_name] = match.group(1)
    trace_types = {}
    for line in sections.get("TRACE", []):
        match = re.match(r'^"([^"]+)" "([^"]+)"$', line)
        if match:
            trace_types[match.group(1)] = match.group(2)
    if scalar_units.get(trace_types.get("out")) != "V/sqrt(Hz)":
        raise RuntimeError("HBNOISE total out must declare V/sqrt(Hz): " + str(source_path))

    samples = []
    sample = None
    active = None
    for line in sections.get("VALUE", []):
        coordinate = re.match(r'^"(?:relative frequency|freq)"\s+([-+0-9.eE]+)$', line)
        if coordinate:
            if active is not None:
                raise RuntimeError("Unclosed HBNOISE contributor struct")
            sample = {}
            samples.append((float(coordinate.group(1)), sample))
            continue
        if sample is None:
            continue
        if active is not None:
            if line == ")":
                active = None
            else:
                try:
                    sample[active].append(float(line))
                except ValueError as exc:
                    raise RuntimeError("Invalid HBNOISE contributor number") from exc
            continue
        struct = re.match(r'^"([^"]+)" \($', line)
        if struct:
            active = struct.group(1)
            if active in sample:
                raise RuntimeError("Duplicate HBNOISE trace: " + active)
            sample[active] = []
            continue
        scalar = re.match(r'^"([^"]+)"\s+([-+0-9.eE]+)$', line)
        if scalar:
            if scalar.group(1) in sample:
                raise RuntimeError("Duplicate HBNOISE trace: " + scalar.group(1))
            sample[scalar.group(1)] = float(scalar.group(2))
    if active is not None:
        raise RuntimeError("Unclosed HBNOISE contributor struct")
    values = exact_frequency_value(samples, frequency, label=str(source_path) + ":out")
    total_asd = values.get("out")
    if not isinstance(total_asd, (int, float)) or not math.isfinite(total_asd) or total_asd < 0:
        raise RuntimeError("Invalid or missing HBNOISE total output ASD")
    from isf_tool.core.noise_sources import parse_psf_ascii, classify_mechanism
    try:
        typed_types = parse_psf_ascii(text)["types"]
    except ValueError as exc:
        raise RuntimeError("Invalid HBNOISE contributor schema: " + str(exc)) from exc
    contributions, device_totals, unread = [], [], []
    for device, type_name in trace_types.items():
        if type_name not in schemas:
            continue
        fields = schemas[type_name]
        vector = values.get(device)
        if not isinstance(vector, list) or len(vector) != len(fields):
            raise RuntimeError("HBNOISE contributor/schema mismatch for " + device)
        for entry, psd in zip(fields, vector):
            if entry["units"] != "V^2/Hz":
                unread.append({"device": device, "mechanism": entry["name"], "units": entry["units"]})
                continue
            if not math.isfinite(psd) or psd < 0:
                raise RuntimeError("Invalid HBNOISE contributor PSD: " + device + ":" + entry["name"])
            item = {"device": device, "mechanism": entry["name"],
                    "psd_V2_Hz": psd, "asd_V_rtHz": math.sqrt(psd)}
            master = typed_types[type_name].get("master") or type_name
            item.update(primitive=master, noise_family=classify_mechanism(master, entry["name"]))
            (device_totals if entry["name"] == "total" else contributions).append(item)
    header = {}
    for line in sections.get("HEADER", []):
        match = re.match(r'^"([^"]+)"\s+"?([^"\n]+)"?$', line)
        if match:
            header[match.group(1)] = match.group(2)
    if header.get("analysis type") != "pm.pnoise_hbnoise":
        raise RuntimeError("Expected a Spectre PM HBNOISE dataset: " + str(source_path))
    total_psd = total_asd * total_asd
    mechanism_sum = math.fsum(item["psd_V2_Hz"] for item in contributions)
    reference = {
        "schema_version": 1, "scope": "whole_vco", "source": "spectre_raw_psf",
        "path": str(source_path), "analysis": analysis, "trace": "out",
        "noise_kind": "PM_SSB", "raw_quantity": "ASD_V_per_sqrt_Hz",
        "contributor_raw_quantity": "PSD_V2_per_Hz", "offset_Hz": float(frequency),
        "positive_output": header.get("positive output signal"),
        "negative_output": header.get("negative output signal"),
        "relative_harmonic": header.get("relharmnum"),
        "total_out_psd_V2_Hz": total_psd, "mechanism_sum_psd_V2_Hz": mechanism_sum,
        "mechanism_sum_minus_total_psd_V2_Hz": mechanism_sum - total_psd,
        "device_totals": device_totals, "unread_contributor_fields": unread,
        "provenance": "private main_vco HBNOISE; independent of ISF calculation",
    }
    return {"spectre_total_pm_asd_V_rtHz": total_asd,
            "spectre_noise_contributions": contributions,
            "spectre_noise_reference": reference}


def extract_hbnoise_total(cfg, psf_dir, analysis="hbnoise"):
    path = noise_file(psf_dir, analysis, "pm")
    text = run_command([cfg["psf"], "-f", "%.16e", "-i", str(path)],
                       cadence_env=cfg.get("cadence_env"))
    return parse_hbnoise_total(text, cfg["offset_hz"], source_path=str(path), analysis=analysis)


def candidate_sets(id_harmonics, am_asd, pm_asd, dc_coeffs, previous=None):
    harmonics = sorted(am_asd)
    all_sets = []
    _enforce_phase_candidate_limit(len(harmonics))
    previous_selected = {}
    previous_dc = {}
    if previous:
        if isinstance(previous, dict) and "selected" in previous:
            previous_selected = previous.get("selected") or {}
            previous_dc = previous.get("dc") or {}
        else:  # compatibility with an in-memory caller from an older stage
            previous_selected = previous
    for signs in itertools.product((-1, 1), repeat=len(harmonics)):
        selected = {}
        phase_cost = 0.0
        delays = []
        continuity_cost = 0.0
        for k, sign in zip(harmonics, signs):
            theta = math.atan2(id_harmonics[k].imag, id_harmonics[k].real)
            value = np.exp(1j * theta) * (am_asd[k] + 1j * sign * pm_asd[k])
            selected[k] = value
            if abs(dc_coeffs[k]) > 0:
                delta = wrap_phase(math.atan2(value.imag, value.real) - math.atan2(dc_coeffs[k].imag, dc_coeffs[k].real))
                phase_cost += (delta / math.pi) ** 2
                delays.append(delta / float(k))
            if (
                k in previous_selected
                and abs(dc_coeffs.get(k, 0j))
                and abs(previous_dc.get(k, 0j))
            ):
                # Compare Ck=Mdyn/Mdc rather than absolute Mdyn phase.  A
                # different oscillator time origin rotates both coefficients
                # by k*phi and must not change the cross-point score.
                current_correction = value / dc_coeffs[k]
                previous_correction = previous_selected[k] / previous_dc[k]
                continuity_cost += (
                    wrap_phase(
                        np.angle(current_correction)
                        - np.angle(previous_correction)
                    )
                    / math.pi
                ) ** 2
        delay_cost = float(np.var(delays)) if len(delays) > 1 else 0.0
        total = phase_cost + 0.50 * delay_cost + 0.35 * continuity_cost
        all_sets.append(
            {
                "signs": signs,
                "selected": selected,
                "phase_cost": phase_cost,
                "delay_cost": delay_cost,
                "continuity_cost": continuity_cost,
                "score": total,
            }
        )
    all_sets.sort(key=lambda row: row["score"])
    return all_sets


def fit_uniform_delay(dc_coeffs, dyn_coeffs):
    x = []
    y = []
    w = []
    for k in sorted(set(dc_coeffs) & set(dyn_coeffs)):
        if k == 0 or abs(dc_coeffs[k]) == 0 or abs(dyn_coeffs[k]) == 0:
            continue
        x.append(float(k))
        y.append(wrap_phase(np.angle(dyn_coeffs[k]) - np.angle(dc_coeffs[k])))
        w.append(abs(dc_coeffs[k]) ** 2)
    if not x:
        return 0.0
    x = np.asarray(x)
    y = np.unwrap(np.asarray(y))
    w = np.asarray(w)
    slope = float(np.sum(w * x * y) / np.sum(w * x * x))
    return -slope


def parse_hbxf(cfg, psf_dir, source, max_harmonic):
    out = {}
    contract = _transfer_contract_for_reading(cfg)
    factor = 1
    if contract.get('mode') == 'existing_source':
        source = contract.get('source')
        factor = contract.get('direction_factor')
        if not source or isinstance(factor, bool) or factor not in (-1, 1):
            raise RuntimeError('Selected HBXF source or drain-to-source polarity is invalid')
    f0 = exact_hb_fundamental(cfg, psf_dir)
    target_frequency = f0 + float(cfg['offset_hz'])
    # f_in = f_out + sideband*f0 and f_out=f0+offset, hence sideband=k-1.
    for k in range(-max_harmonic, max_harmonic + 1):
        sideband = k - 1
        path = Path(psf_dir) / "hbxf.{}.pxf_hbxf".format(sideband)
        if not path.exists():
            raise RuntimeError("Missing HBXF sideband {} for k={}".format(sideband, k))
        rows = parse_complex_wave(cfg, path, source)
        out[k] = factor * exact_frequency_value(rows, target_frequency, cfg['offset_hz'], str(path))
    return out


def parse_hbac_current(cfg, psf_dir, sense_trace, max_harmonic):
    """HBAC freqaxis=out is k*f0_replica+offset, not the input offset alone."""
    f0 = exact_hb_fundamental(cfg, psf_dir, sense_trace)
    output = {}
    for harmonic in range(max_harmonic + 1):
        path = Path(psf_dir) / 'hbac.{}.pac_hbac'.format(harmonic)
        output[harmonic] = exact_frequency_value(parse_complex_wave(cfg, path, sense_trace),
            harmonic * f0 + float(cfg['offset_hz']), cfg['offset_hz'], str(path))
    return output


def gamma_coefficients(h_pos, carrier, offset, scale_pi=4.0):
    theta = np.angle(carrier)
    vh1 = abs(carrier)
    gamma = {}
    for k, hval in h_pos.items():
        amp = scale_pi * math.pi * offset * abs(hval) / vh1
        phase = theta - np.angle(hval)
        if k == 0:
            gamma[k] = complex(0.5 * amp * math.cos(phase), 0.0)
        else:
            gamma[k] = 0.5 * amp * np.exp(1j * phase)
    return gamma


def gamma_sum(gamma, coeffs):
    value = gamma.get(0, 0j) * coeffs.get(0, 0j)
    for k in sorted(set(gamma) & set(coeffs)):
        if k > 0:
            value += 2.0 * np.real(gamma[k] * np.conj(coeffs[k]))
    return complex(value)


def exact_gamma_from_signed_h(h, rotation, carrier_peak, offset, max_harmonic):
    """Exact real ISF coefficients for Re(rotation*sum(Hk*Mk))."""
    scale = math.sqrt(2.0) * (2.0 * math.pi * offset) / carrier_peak
    gamma = {0: complex(scale * np.real(rotation * h[0]), 0.0)}
    for k in range(1, max_harmonic + 1):
        gamma[k] = 0.5 * scale * (
            np.conj(rotation * h[k]) + rotation * h[-k]
        )
    return gamma


def coherent_sum(h, coeffs):
    return sum((h[k] * coeffs[k] for k in sorted(set(h) & set(coeffs))), 0j)


def pm_projection(sideband_voltage, carrier):
    if abs(carrier) == 0:
        raise ZeroDivisionError("Zero carrier")
    return abs(np.imag(sideband_voltage * np.exp(-1j * np.angle(carrier))))


def dBc_from_pm_asd(pm_asd, carrier_peak):
    if (
        not math.isfinite(pm_asd)
        or not math.isfinite(carrier_peak)
        or pm_asd <= 0
        or carrier_peak <= 0
    ):
        return None
    # SSB noise power divided by carrier power for equal impedance.
    return 10.0 * math.log10(2.0) + 20.0 * (
        math.log10(pm_asd) - math.log10(carrier_peak)
    )


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_phasor_svg(path, rows, title):
    points = [(0.0, 0.0)] + [
        (float(row["cum_real_V_rtHz"]), float(row["cum_imag_V_rtHz"])) for row in rows
    ]
    scale = max(max(abs(x), abs(y)) for x, y in points) or 1.0
    width = height = 620
    center = width / 2.0
    gain = 0.42 * width / scale
    def xy(point):
        return center + point[0] * gain, center - point[1] * gain
    polyline = " ".join("{:.2f},{:.2f}".format(*xy(point)) for point in points)
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="620" height="620" viewBox="0 0 620 620">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<text x="310" y="26" text-anchor="middle" font-family="Arial" font-size="17">{}</text>'.format(title),
        '<line x1="40" y1="310" x2="580" y2="310" stroke="#999"/>',
        '<line x1="310" y1="40" x2="310" y2="580" stroke="#999"/>',
        '<polyline points="{}" fill="none" stroke="#1565c0" stroke-width="2"/>'.format(polyline),
    ]
    for index, point in enumerate(points):
        x, y = xy(point)
        label = "0" if index == 0 else "ΣQ0..{}".format(index - 1)
        parts.append('<circle cx="{:.2f}" cy="{:.2f}" r="4" fill="#d32f2f"/>'.format(x, y))
        parts.append('<text x="{:.2f}" y="{:.2f}" font-family="Arial" font-size="12">{}</text>'.format(x + 7, y - 7, label))
    parts.extend([
        '<text x="570" y="302" text-anchor="end" font-family="Arial" font-size="12">Re [V/√Hz]</text>',
        '<text x="318" y="52" font-family="Arial" font-size="12">Im [V/√Hz]</text>',
        '</svg>',
    ])
    path.write_text("\n".join(parts), encoding="utf-8")


def write_error_svg(path, rows):
    width, height = 900, 430
    margin_l, margin_r, margin_t, margin_b = 70, 25, 45, 85
    values = []
    for row in rows:
        values.extend([row["dc_error_dB"], row["delay_error_dB"], row["dynamic_error_dB"]])
    ymin = min(-3.5, min(values) - 1.0)
    ymax = max(3.5, max(values) + 1.0)
    plot_h = height - margin_t - margin_b
    def py(value):
        return margin_t + (ymax - value) / (ymax - ymin) * plot_h
    step = (width - margin_l - margin_r) / max(len(rows), 1)
    colors = ["#777777", "#ef6c00", "#1565c0"]
    keys = ["dc_error_dB", "delay_error_dB", "dynamic_error_dB"]
    labels = ["DC", "uniform delay", "dynamic AM/PM"]
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="430" viewBox="0 0 900 430">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<text x="450" y="26" text-anchor="middle" font-family="Arial" font-size="18">Prediction error versus Spectre target-gm flicker PM-SSB</text>',
    ]
    for level in (-3, 0, 3):
        parts.append('<line x1="{}" y1="{:.2f}" x2="{}" y2="{:.2f}" stroke="{}" stroke-dasharray="4 4"/>'.format(margin_l, py(level), width-margin_r, py(level), "#d32f2f" if abs(level)==3 else "#999"))
        parts.append('<text x="{}" y="{:.2f}" text-anchor="end" font-family="Arial" font-size="12">{} dB</text>'.format(margin_l-8, py(level)+4, level))
    for index, row in enumerate(rows):
        cx = margin_l + (index + 0.5) * step
        for j, key in enumerate(keys):
            x = cx + (j - 1) * 13
            y = py(row[key])
            parts.append('<circle cx="{:.2f}" cy="{:.2f}" r="5" fill="{}"/>'.format(x, y, colors[j]))
        parts.append('<text x="{:.2f}" y="{}" transform="rotate(-35 {:.2f} {})" text-anchor="end" font-family="Arial" font-size="12">{}</text>'.format(cx+18, height-22, cx+18, height-22, row["point"]))
    for j, label in enumerate(labels):
        x = margin_l + 150 + j * 180
        parts.append('<circle cx="{}" cy="{}" r="5" fill="{}"/><text x="{}" y="{}" font-family="Arial" font-size="12">{}</text>'.format(x, 42, colors[j], x+10, 46, label))
    parts.append('</svg>')
    path.write_text("\n".join(parts), encoding="utf-8")


def analyze_point(cfg, point, run_root, previous_selected=None):
    profile = physical_profile(cfg)
    main = profile["main"]
    traces = main["traces"]
    replica = profile["replica"]
    sense_trace = replica["sense_source"] + ":p"
    name = point_name(point)
    case_dir = Path(run_root) / name
    main_psf = case_dir / "main_vco" / "psf"
    rep_psf = case_dir / "replica" / "psf"
    verify_spectre(main_psf)
    verify_spectre(rep_psf)
    quality_logs = [
        ("main_vco", main_psf / "spectre.out"),
        ("replica", rep_psf / "spectre.out"),
    ]
    local_dc_logs = sorted(
        (case_dir / "dc_flicker").glob("sample_*/psf/spectre.out")
    )
    if local_dc_logs:
        quality_logs.extend(
            ("dc_flicker", path) for path in local_dc_logs
        )
    else:
        legacy_dc_logs = sorted(
            source_case_path(cfg, point).glob(
                "iflicker/sample_*/psf/spectre.out"
            )
        )
        quality_logs.extend(
            ("dc_flicker_legacy", path) for path in legacy_dc_logs
        )
    numerical_quality = aggregate_spectre_quality(quality_logs)
    maxk = cfg["max_harmonic"]
    offset = cfg["offset_hz"]

    workpoint = sample_periodic_wave(
        cfg,
        main_psf,
        list(
            dict.fromkeys(
                (
                    traces["drain"],
                    traces["gate"],
                    traces["source"],
                    traces["bulk"],
                    traces["current"],
                )
            )
        ),
        samples=65,
    )
    workpoint_rows = []
    for index, time_s in enumerate(workpoint["time"]):
        workpoint_rows.append(
            {
                "sample": index,
                "time_s": time_s,
                "wt_rad": 2.0 * math.pi * time_s / workpoint["period"],
                "VG_V": workpoint[traces["gate"]][index],
                "VD_V": workpoint[traces["drain"]][index],
                "VS_V": workpoint[traces["source"]][index],
                "VB_V": workpoint[traces["bulk"]][index],
                "ID_drain_to_source_A": workpoint[traces["current"]][index],
            }
        )
    write_csv(
        case_dir / "device_workpoint.csv",
        list(workpoint_rows[0].keys()),
        workpoint_rows,
    )

    cfg["_analysis_case_dir"] = str(case_dir)
    try:
        _, _, mdc = read_dc_flicker(cfg, point, maxk)
    finally:
        cfg.pop("_analysis_case_dir", None)
    f0 = fundamental(cfg, rep_psf)
    id_rows = parse_complex_wave(cfg, rep_psf / "hb.fd.pss_hb", sense_trace)
    id_h = {k: nearest_complex(id_rows, k * f0) for k in range(maxk + 1)}
    main_id_rows = parse_complex_wave(
        cfg, main_psf / "hb.fd.pss_hb", traces["current"]
    )
    main_id_h = {k: nearest_complex(main_id_rows, k * f0) for k in range(maxk + 1)}
    hbac_raw = parse_hbac_current(cfg, rep_psf, sense_trace, maxk)
    hbac_norm = {
        k: value / hbac_raw[0] if abs(hbac_raw[0]) else 0j
        for k, value in hbac_raw.items()
    }

    k0_usb_psd = extract_hbnoise_fn(
        cfg, rep_psf, "hn0", "usb", replica["noise_trace"], expected_domain="A"
    )
    sqrt_sxi = math.sqrt(max(k0_usb_psd, 0.0))
    am_asd = {}
    pm_asd = {}
    usb_asd = {0: sqrt_sxi}
    lsb_asd = {}
    for k in range(1, maxk + 1):
        am_asd[k] = math.sqrt(max(extract_hbnoise_fn(cfg, rep_psf, "hn{}".format(k), "am", replica["noise_trace"], expected_domain="A"), 0.0))
        pm_asd[k] = math.sqrt(max(extract_hbnoise_fn(cfg, rep_psf, "hn{}".format(k), "pm", replica["noise_trace"], expected_domain="A"), 0.0))
        usb_asd[k] = math.sqrt(max(extract_hbnoise_fn(cfg, rep_psf, "hn{}".format(k), "usb", replica["noise_trace"], expected_domain="A"), 0.0))
        lsb_asd[k] = math.sqrt(max(extract_hbnoise_fn(cfg, rep_psf, "hn{}".format(k), "lsb", replica["noise_trace"], expected_domain="A"), 0.0))

    candidates = candidate_sets(id_h, am_asd, pm_asd, mdc, previous_selected)
    chosen = candidates[0]
    mdyn_pos = {0: complex(sqrt_sxi, 0.0)}
    mdyn_pos.update(chosen["selected"])
    mdyn = {0: mdyn_pos[0]}
    mdc_signed = {0: mdc[0]}
    for k in range(1, maxk + 1):
        mdyn[k] = mdyn_pos[k]
        mdyn[-k] = np.conj(mdyn_pos[k])
        mdc_signed[k] = mdc[k]
        mdc_signed[-k] = np.conj(mdc[k])

    delay_phase = fit_uniform_delay(mdc, mdyn_pos)
    mdelay_pos = {0: mdc[0]}
    for k in range(1, maxk + 1):
        mdelay_pos[k] = mdc[k] * np.exp(-1j * k * delay_phase)
    mdelay = {0: mdelay_pos[0]}
    for k in range(1, maxk + 1):
        mdelay[k] = mdelay_pos[k]
        mdelay[-k] = np.conj(mdelay_pos[k])

    main_f0 = fundamental(cfg, main_psf)
    carrier = carrier_phasor(cfg, main_psf, main_f0)
    h = parse_hbxf(cfg, main_psf, main_injection_trace(profile), maxk)
    vdc = coherent_sum(h, mdc_signed)
    vdelay = coherent_sum(h, mdelay)
    vdyn = coherent_sum(h, mdyn)
    if not abs(carrier):
        raise RuntimeError("zero differential carrier")
    carrier_rotation_i = np.conj(carrier) / abs(carrier)
    carrier_rotation_q = -1j * carrier_rotation_i
    carrier_dc_i = float(np.real(carrier_rotation_i * vdc))
    carrier_dc_q = float(np.real(carrier_rotation_q * vdc))
    carrier_delay_i = float(np.real(carrier_rotation_i * vdelay))
    carrier_delay_q = float(np.real(carrier_rotation_q * vdelay))
    carrier_dyn_i = float(np.real(carrier_rotation_i * vdyn))
    carrier_dyn_q = float(np.real(carrier_rotation_q * vdyn))
    carrier_dc_projection = abs(carrier_dc_i)
    carrier_delay_projection = abs(carrier_delay_i)
    carrier_dyn_projection = abs(carrier_dyn_i)
    pm_dc = abs(vdc)
    pm_delay = abs(vdelay)
    pm_dyn = abs(vdyn)

    spectre_fn_psd = extract_hbnoise_fn(
        cfg, main_psf, "hbnoise", "pm", traces["target_noise"], expected_domain="V"
    )
    spectre_fn_asd = math.sqrt(max(spectre_fn_psd, 0.0))
    spectre_am_fn_psd = extract_hbnoise_fn(
        cfg, main_psf, "hbnoise", "am", traces["target_noise"], expected_domain="V"
    )
    spectre_am_fn_asd = math.sqrt(max(spectre_am_fn_psd, 0.0))
    spectre_am_to_pm_db = (
        20.0 * math.log10(spectre_am_fn_asd / spectre_fn_asd)
        if spectre_am_fn_asd > 0 and spectre_fn_asd > 0
        else float("-inf")
    )
    pm_projection_threshold_db = float(
        cfg.get("max_am_to_pm_for_magnitude_db", -20.0)
    )
    predicted_projection_loss_db = (
        20.0 * math.log10(carrier_dyn_projection / pm_dyn)
        if carrier_dyn_projection > 0 and pm_dyn > 0
        else float("-inf")
    )
    max_projection_loss_db = float(
        cfg.get("max_carrier_projection_loss_db", 0.5)
    )
    pm_projection_valid = bool(
        spectre_fn_asd > 0
        and spectre_am_to_pm_db <= pm_projection_threshold_db
    )
    spectre_total_psd = noise_output_psd_at(
        cfg,
        noise_file(main_psf, "hbnoise", "pm"),
        traces["target_noise"],
        offset,
        "total",
        expected_domain="V",
    )
    spectre_total_asd = math.sqrt(max(spectre_total_psd, 0.0))
    whole_vco_noise = extract_hbnoise_total(cfg, main_psf)
    generated = cfg.get("replica_source") == "generated"
    if generated:
        peer_trace = traces.get("peer_noise")
        peer_rows = [row for row in whole_vco_noise["spectre_noise_contributions"]
                     if row["device"] == peer_trace and row["mechanism"] == "fn"]
        peer_psd = peer_rows[0]["psd_V2_Hz"] if len(peer_rows) == 1 else None
    else:
        peer_psd = noise_output_psd_at(
            cfg, noise_file(main_psf, "hbnoise", "pm"), traces["peer_noise"],
            offset, "fn", expected_domain="V",
        )
    spectre_m0_fn_asd = math.sqrt(peer_psd) if peer_psd is not None else None
    predicted_pair_asd = math.sqrt(2.0) * pm_dyn
    spectre_pair_asd = math.sqrt(spectre_fn_psd + peer_psd) if peer_psd is not None else None

    h_pos = {k: h[k] for k in range(maxk + 1)}
    gamma_single = gamma_coefficients(h_pos, carrier, offset, scale_pi=4.0)
    gamma_legacy = gamma_coefficients(h_pos, carrier, offset, scale_pi=8.0)
    heff_dyn_positive_only = gamma_sum(gamma_single, mdyn_pos)
    heff_dyn_legacy = gamma_sum(gamma_legacy, mdyn_pos)
    heff_from_physical = math.sqrt(2.0) * (2.0 * math.pi * offset) * pm_dyn / abs(carrier)
    heff_from_carrier_projection = (
        math.sqrt(2.0)
        * (2.0 * math.pi * offset)
        * carrier_dyn_projection
        / abs(carrier)
    )
    gamma_i = exact_gamma_from_signed_h(
        h, carrier_rotation_i, abs(carrier), offset, maxk
    )
    gamma_q = exact_gamma_from_signed_h(
        h, carrier_rotation_q, abs(carrier), offset, maxk
    )
    heff_dc_i = gamma_sum(gamma_i, mdc).real
    heff_dc_q = gamma_sum(gamma_q, mdc).real
    heff_delay_i = gamma_sum(gamma_i, mdelay_pos).real
    heff_delay_q = gamma_sum(gamma_q, mdelay_pos).real
    heff_dyn_i = gamma_sum(gamma_i, mdyn_pos).real
    heff_dyn_q = gamma_sum(gamma_q, mdyn_pos).real
    heff_dc = math.hypot(heff_dc_i, heff_dc_q)
    heff_delay = math.hypot(heff_delay_i, heff_delay_q)
    heff_dyn_exact = math.hypot(heff_dyn_i, heff_dyn_q)

    harmonic_rows = []
    cumulative = 0j
    for k in range(0, maxk + 1):
        cval = mdyn_pos[k] / mdc[k] if abs(mdc[k]) else complex(float("nan"), float("nan"))
        dval = mdyn_pos[k] / sqrt_sxi if sqrt_sxi else 0j
        q = h[k] * mdyn_pos[k]
        if k > 0:
            q += h[-k] * np.conj(mdyn_pos[k])
        cumulative += q
        harmonic_rows.append(
            {
                "k": k,
                "Id_mag_A": abs(id_h[k]),
                "Id_phase_deg": phase_deg(id_h[k]),
                "main_Id_mag_A": abs(main_id_h[k]),
                "main_Id_phase_deg": phase_deg(main_id_h[k]),
                "Mdc_mag_A_rtHz": abs(mdc[k]),
                "Mdc_phase_deg": phase_deg(mdc[k]),
                "AM_A_rtHz": am_asd.get(k, float("nan")),
                "PM_A_rtHz": pm_asd.get(k, float("nan")),
                "USB_A_rtHz": usb_asd.get(k, float("nan")),
                "LSB_A_rtHz": lsb_asd.get(k, float("nan")),
                "Mdyn_mag_A_rtHz": abs(mdyn_pos[k]),
                "Mdyn_phase_deg": phase_deg(mdyn_pos[k]),
                "A_k": abs(cval),
                "delta_k_deg": phase_deg(cval),
                "Ck_real": cval.real,
                "Ck_imag": cval.imag,
                "Dk_real": dval.real,
                "Dk_imag": dval.imag,
                "HBAC_norm_mag": abs(hbac_norm[k]),
                "HBAC_norm_phase_deg": phase_deg(hbac_norm[k]),
                "HBAC_vs_Mdyn_phase_deg": math.degrees(wrap_phase(np.angle(hbac_norm[k]) - np.angle(mdyn_pos[k]))) if abs(hbac_norm[k]) and abs(mdyn_pos[k]) else float("nan"),
                "Qk_real_V_rtHz": q.real,
                "Qk_imag_V_rtHz": q.imag,
                "cum_real_V_rtHz": cumulative.real,
                "cum_imag_V_rtHz": cumulative.imag,
            }
        )
    write_csv(
        case_dir / "harmonic_closure.csv",
        list(harmonic_rows[0].keys()),
        harmonic_rows,
    )
    hbac_deltas = [
        abs(float(row["HBAC_vs_Mdyn_phase_deg"]))
        for row in harmonic_rows
        if int(row["k"]) > 0
        and math.isfinite(float(row["HBAC_vs_Mdyn_phase_deg"]))
    ]
    hbac_max_delta = max(hbac_deltas) if hbac_deltas else float("nan")
    hbac_threshold = float(cfg.get("hbac_phase_warn_deg", 45.0))
    hbac_validation_pass = bool(
        hbac_deltas and hbac_max_delta <= hbac_threshold
    )
    write_phasor_svg(
        case_dir / "cumulative_phasor.svg",
        harmonic_rows,
        "{} cumulative coherent harmonic sum".format(name),
    )

    candidate_rows = []
    for rank, item in enumerate(candidates, 1):
        row = {
            "rank": rank,
            "chosen": rank == 1,
            "signs": " ".join("+" if sign > 0 else "-" for sign in item["signs"]),
            "score": item["score"],
            "phase_cost": item["phase_cost"],
            "delay_cost": item["delay_cost"],
            "continuity_cost": item["continuity_cost"],
        }
        for k, value in item["selected"].items():
            row["M{}_phase_deg".format(k)] = phase_deg(value)
        candidate_rows.append(row)
    write_csv(case_dir / "phase_candidates.csv", list(candidate_rows[0].keys()), candidate_rows)

    summary = {
        "point": name,
        "k": point["k"],
        "corner": point["corner"],
        "f0_Hz": main_f0,
        "carrier_peak_V": abs(carrier),
        "sqrt_Sxi_A_rtHz": sqrt_sxi,
        "chosen_signs": "".join("+" if x > 0 else "-" for x in chosen["signs"]),
        "candidate_score": chosen["score"],
        "uniform_delay_s": delay_phase / (2.0 * math.pi * main_f0),
        "dc_pm_asd_V_rtHz": pm_dc,
        "delay_pm_asd_V_rtHz": pm_delay,
        "dynamic_pm_asd_V_rtHz": pm_dyn,
        "dc_carrier_projection_V_rtHz": carrier_dc_projection,
        "delay_carrier_projection_V_rtHz": carrier_delay_projection,
        "dynamic_carrier_projection_V_rtHz": carrier_dyn_projection,
        "spectre_fn_pm_asd_V_rtHz": spectre_fn_asd,
        "spectre_fn_am_asd_V_rtHz": spectre_am_fn_asd,
        "spectre_am_to_pm_dB": spectre_am_to_pm_db,
        "pm_magnitude_projection_valid": pm_projection_valid,
        "pm_magnitude_projection_code": (
            "" if pm_projection_valid else "ISF-W3004"
        ),
        "pm_magnitude_projection_threshold_dB": pm_projection_threshold_db,
        "predicted_carrier_projection_loss_dB": predicted_projection_loss_db,
        "max_carrier_projection_loss_dB": max_projection_loss_db,
        "spectre_target_device_total_pm_asd_V_rtHz": spectre_total_asd,
        "spectre_total_pm_asd_V_rtHz": whole_vco_noise["spectre_total_pm_asd_V_rtHz"],
        "spectre_total_dBc_Hz": dBc_from_pm_asd(whole_vco_noise["spectre_total_pm_asd_V_rtHz"], abs(carrier)),
        "spectre_noise_contributions_json": _strict_json_dumps(whole_vco_noise["spectre_noise_contributions"]),
        "spectre_noise_reference_json": _strict_json_dumps(whole_vco_noise["spectre_noise_reference"]),
        "dynamic_vs_total_device_error_dB": 20.0 * math.log10(pm_dyn / spectre_total_asd) if pm_dyn > 0 and spectre_total_asd > 0 else float("nan"),
        "spectre_m0_fn_pm_asd_V_rtHz": spectre_m0_fn_asd,
        "predicted_pair_fn_pm_asd_V_rtHz": predicted_pair_asd,
        "spectre_pair_fn_pm_asd_V_rtHz": spectre_pair_asd,
        "pair_error_dB": 20.0 * math.log10(predicted_pair_asd / spectre_pair_asd) if predicted_pair_asd > 0 and spectre_pair_asd else float("nan"),
        "dc_error_dB": 20.0 * math.log10(pm_dc / spectre_fn_asd) if pm_dc > 0 and spectre_fn_asd > 0 else float("nan"),
        "delay_error_dB": 20.0 * math.log10(pm_delay / spectre_fn_asd) if pm_delay > 0 and spectre_fn_asd > 0 else float("nan"),
        "dynamic_error_dB": 20.0 * math.log10(pm_dyn / spectre_fn_asd) if pm_dyn > 0 and spectre_fn_asd > 0 else float("nan"),
        "dc_dBc_Hz": dBc_from_pm_asd(pm_dc, abs(carrier)),
        "delay_dBc_Hz": dBc_from_pm_asd(pm_delay, abs(carrier)),
        "dynamic_dBc_Hz": dBc_from_pm_asd(pm_dyn, abs(carrier)),
        "spectre_fn_dBc_Hz": dBc_from_pm_asd(spectre_fn_asd, abs(carrier)),
        "heff_dc": heff_dc,
        "heff_delay": heff_delay,
        "heff_dynamic": heff_dyn_exact,
        "heff_carrier_i_signed": heff_dyn_i,
        "heff_carrier_q_signed": heff_dyn_q,
        "heff_branch_rss": heff_dyn_exact,
        "heff_dynamic_positive_only_4pi": heff_dyn_positive_only.real,
        "heff_dynamic_legacy_8pi": heff_dyn_legacy.real,
        "heff_from_carrier_projection": heff_from_carrier_projection,
        "heff_from_physical_units": heff_from_physical,
        "heff_dynamic_exact_signed": heff_dyn_exact,
        "heff_from_carrier_i_projection_signed": (
            math.sqrt(2.0) * (2.0 * math.pi * offset) * carrier_dyn_i / abs(carrier)
        ),
        "heff_from_carrier_q_projection_signed": (
            math.sqrt(2.0) * (2.0 * math.pi * offset) * carrier_dyn_q / abs(carrier)
        ),
        "gamma_reference": "carrier_referenced_signed_H_iq_v1",
        "gamma_i_algebra_error_dB": 20.0 * math.log10(abs(heff_dyn_i) / (math.sqrt(2.0) * (2.0 * math.pi * offset) * abs(carrier_dyn_i) / abs(carrier))) if abs(heff_dyn_i) > 0 and abs(carrier_dyn_i) > 0 else float("nan"),
        "gamma_q_algebra_error_dB": 20.0 * math.log10(abs(heff_dyn_q) / (math.sqrt(2.0) * (2.0 * math.pi * offset) * abs(carrier_dyn_q) / abs(carrier))) if abs(heff_dyn_q) > 0 and abs(carrier_dyn_q) > 0 else float("nan"),
        "gamma_exact_equivalence_error_dB": 20.0 * math.log10(heff_dyn_exact / heff_from_physical) if heff_dyn_exact > 0 and heff_from_physical > 0 else float("nan"),
        "gamma_to_output_magnitude_error_dB": 20.0 * math.log10(heff_dyn_exact / heff_from_physical) if heff_dyn_exact > 0 and heff_from_physical > 0 else float("nan"),
        "gamma_positive_only_4pi_error_dB": 20.0 * math.log10(abs(heff_dyn_positive_only) / heff_from_physical) if abs(heff_dyn_positive_only) > 0 and heff_from_physical > 0 else float("nan"),
        "gamma_legacy_equivalence_error_dB": 20.0 * math.log10(abs(heff_dyn_legacy) / heff_from_physical) if abs(heff_dyn_legacy) > 0 and heff_from_physical > 0 else float("nan"),
        "physical_vdyn_complex_V_rtHz": "{:.16e}{:+.16e}j".format(vdyn.real, vdyn.imag),
        "numerical_quality_pass": numerical_quality["pass"],
        "numerical_quality_code": numerical_quality["code"],
        "numerical_quality_issues": ";".join(numerical_quality["issues"]),
        "hbac_secondary_validation_pass": hbac_validation_pass,
        "hbac_secondary_validation_code": (
            "" if hbac_validation_pass else "ISF-W3003"
        ),
        "hbac_max_phase_delta_deg": hbac_max_delta,
        "hbac_phase_warn_threshold_deg": hbac_threshold,
    }
    from isf_tool.core.thermal_budget import load_thermal_evidence
    budget = None if generated else load_thermal_evidence(cfg, case_dir, dict(summary, offset_Hz=offset))
    if budget is not None:
        summary["thermal_noise_json"] = _strict_json_dumps(budget)
    (case_dir / "summary.json").write_text(
        _strict_json_dumps(summary, indent=2, sort_keys=True)
    )
    return summary, {"selected": mdyn_pos, "dc": mdc}


def trend_metrics(rows):
    errors = np.array([row["dynamic_error_dB"] for row in rows], dtype=float)
    median = float(np.median(errors))
    residual = errors - median
    trend_qualified = len(rows) >= 2
    return {
        "point_count": len(rows),
        "trend_qualified": trend_qualified,
        "formal_point_count_qualified": len(rows) >= 6,
        "median_offset_dB": median,
        "max_deviation_from_median_dB": float(np.max(np.abs(residual))),
        "absolute_match_all_within_3dB": bool(np.all(np.abs(errors) <= 3.0)),
        "trend_match": bool(
            trend_qualified
            and np.max(np.abs(residual)) <= 1.5
            and np.all(np.abs(residual) <= 3.0)
        ),
    }


def write_report(cfg, run_root, rows):
    profile = physical_profile(cfg)
    main = profile["main"]
    output = main_output(cfg)
    metrics = trend_metrics(rows)
    lines = [
        "# Dynamic ISF closure report",
        "",
        "## Scope and conventions",
        "",
        "- Target: one gm MOS `{}`, intrinsic flicker contributor `{}`, {:.6g} Hz offset.".format(
            main_instance_path(profile),
            main["traces"]["target_noise"] + ".fn",
            float(cfg["offset_hz"]),
        ),
        "- Main output: differential {}-{}, PM, SSB; carrier is differential peak phasor.".format(
            output["negative"], output["positive"]
        ),
        "- Drain current direction: drain to source. Main HBXF source `{}` uses the same direction.".format(
            main["injection"]["source"]
        ),
        "- Replica HBNOISE AM/PM supplies two phase candidates per k>=1. Selection uses DC phase, common-delay smoothness and cross-point continuity only; final VCO noise is not a selector.",
        "- All harmonics share one normalization `sqrt(Sxi)`, defined as replica k=0 USB intrinsic-flicker ASD. `Dk=Mk/sqrt(Sxi)`.",
        "- HBXF mapping: input k*f0+offset to output f0+offset uses sideband index k-1, including signed negative frequencies.",
        "- ISF coefficients use the fixed carrier phasor and both signed HBXF sidebands; they are independent of m(t)/Mdyn. The positive-sideband-only 4*pi approximation and historical 8*pi value are audit diagnostics only.",
        "- Replica HBAC uses a unit deterministic PAC source at intrinsic di-si as a secondary phase diagnostic; it does not independently renormalize any harmonic.",
        "",
        "## Results",
        "",
        "| Point | DC err dB | Delay err dB | Dynamic err dB | Pair err dB | Exact Gamma err dB | Log quality | PM gate | HBAC check | signs |",
        "|---|---:|---:|---:|---:|---:|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {point} | {dc_error_dB:.2f} | {delay_error_dB:.2f} | {dynamic_error_dB:.2f} | {pair_error_dB:.2f} | {gamma_exact_equivalence_error_dB:.2e} | {log_quality} | {pm_quality} | {hbac_quality} | {chosen_signs} |".format(
                log_quality=(
                    "PASS"
                    if row["numerical_quality_pass"]
                    else row["numerical_quality_code"]
                ),
                hbac_quality=(
                    "PASS"
                    if row["hbac_secondary_validation_pass"]
                    else row["hbac_secondary_validation_code"]
                ),
                pm_quality=(
                    "PASS"
                    if row.get("pm_magnitude_projection_valid", True)
                    else row.get("pm_magnitude_projection_code", "ISF-W3004")
                ),
                **row
            )
        )
    lines.extend(
        [
            "",
            "## Match decision",
            "",
            "- Absolute match (all {} selected point(s) within +/-3 dB): `{}`".format(
                metrics["point_count"],
                metrics["absolute_match_all_within_3dB"],
            ),
            "- Median dynamic error: `{:.3f} dB`".format(metrics["median_offset_dB"]),
            "- Maximum deviation from median: `{:.3f} dB`".format(metrics["max_deviation_from_median_dB"]),
            "- Cross-point trend qualified (at least two points): `{}`".format(
                metrics["trend_qualified"]
            ),
            "- Trend match under the agreed criterion: `{}`".format(metrics["trend_match"]),
            "- Formal point-count gate (at least six points): `{}`".format(
                metrics["formal_point_count_qualified"]
            ),
            "- Numerical log quality all pass: `{}`".format(
                all(row["numerical_quality_pass"] for row in rows)
            ),
            "- HBAC secondary phase check all pass: `{}`".format(
                all(row["hbac_secondary_validation_pass"] for row in rows)
            ),
            "",
            "## Audit findings encoded in the flow",
            "",
            "1. The replica waveforms are sampled from one fresh main-VCO HB period with a common t=0; no manual waveform shift is permitted.",
            "   Each point exports VG/VD/VS/VB and the probed target-device drain current in `device_workpoint.csv`.",
            "2. DC and HBNOISE contributor data are PSD and are square-rooted exactly once before coherent voltage summation.",
            "3. The physical-unit closure `Vout=sum(Hk*Ink)` is evaluated before ISF/carrier conversion. Two fixed carrier-I/Q signed-H branches reproduce its real and imaginary components; their RSS equals `|Vout|` without using Mdyn to choose a rotation.",
            "4. Positive/negative harmonics are conjugate pairs; their complex voltages are summed coherently, while independent gm-device sources must later be added in power.",
            "5. AM/PM cannot prove a unique random-noise phasor. Candidate ambiguity remains explicit in every `phase_candidates.csv`; HBAC/PAC injection is a secondary diagnostic only and does not select or break ties between candidates.",
            "",
        ]
    )
    report = Path(run_root) / "REPORT.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    (Path(run_root) / "match_metrics.json").write_text(
        _strict_json_dumps(metrics, indent=2, sort_keys=True)
    )
    write_error_svg(Path(run_root) / "error_comparison.svg", rows)
    return report, metrics


def _selector_value_matches(actual, expected):
    try:
        return abs(float(actual) - float(expected)) <= 1.0e-12
    except (TypeError, ValueError):
        return str(actual) == str(expected)


def select_points(cfg, smoke=False):
    """Select all points, or one deterministic configurable smoke point.

    A new PDK is not required to contain the historical k=0.6/Fhigh point.
    With no explicit ``smoke_point``, the first configured point is selected.
    A mapping matches point fields; a string matches ``source_dir`` or the
    generated point name.
    """

    points = list(cfg.get("points", []))
    if not points:
        raise RuntimeError("No configured design points")
    if not smoke:
        return points
    selector = cfg.get("smoke_point")
    if selector in (None, ""):
        return points[:1]
    if isinstance(selector, dict):
        selected = [
            point
            for point in points
            if all(
                key in point
                and _selector_value_matches(point[key], expected)
                for key, expected in selector.items()
            )
        ]
    elif isinstance(selector, str):
        selected = [
            point
            for point in points
            if point.get("source_dir") == selector
            or point_name(point) == selector
            or _legacy_point_name(point) == selector
        ]
    else:
        raise RuntimeError(
            "smoke_point must be a field mapping or source/name string"
        )
    if not selected:
        raise RuntimeError(
            "Configured smoke_point does not match any design point: {}".format(
                selector
            )
        )
    return selected[:1]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--run-root", required=True)
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run smoke_point from config, or the first configured point",
    )
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--analyze-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    run_root = Path(args.run_root).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    points = select_points(cfg, smoke=args.smoke)
    if not args.analyze_only:
        for point in points:
            name, main_status, replica_status, dc_status = prepare_and_run_point(
                cfg, point, run_root, force=args.force
            )
            from isf_tool.core.thermal_execution import run_optional_thermal
            from types import SimpleNamespace
            if cfg.get("replica_source") != "generated":
                run_optional_thermal(SimpleNamespace(**globals()), cfg, point, run_root / name, force=args.force)
            print(
                "{} main={} dc={} replica={}".format(
                    name, main_status, dc_status, replica_status
                ),
                flush=True,
            )
    if args.prepare_only:
        return 0
    summaries = []
    previous_by_corner = {}
    for point in sorted(points, key=lambda p: (p["corner"], float(p["k"]))):
        previous = previous_by_corner.get(point["corner"])
        summary, selected = analyze_point(cfg, point, run_root, previous)
        previous_by_corner[point["corner"]] = selected
        summaries.append(summary)
        print("{} dynamic_error={:.3f} dB".format(summary["point"], summary["dynamic_error_dB"]), flush=True)
    write_csv(run_root / "summary.csv", list(summaries[0].keys()), summaries)
    all_harmonics = []
    all_candidates = []
    for summary in summaries:
        case_dir = run_root / summary["point"]
        with (case_dir / "harmonic_closure.csv").open() as handle:
            for row in csv.DictReader(handle):
                row = dict(row)
                row["point"] = summary["point"]
                all_harmonics.append(row)
        with (case_dir / "phase_candidates.csv").open() as handle:
            for row in csv.DictReader(handle):
                row = dict(row)
                row["point"] = summary["point"]
                all_candidates.append(row)
    write_csv(
        run_root / "harmonics_all.csv",
        ["point"] + [key for key in all_harmonics[0] if key != "point"],
        all_harmonics,
    )
    write_csv(
        run_root / "phase_candidates_all.csv",
        ["point"] + [key for key in all_candidates[0] if key != "point"],
        all_candidates,
    )
    report, metrics = write_report(cfg, run_root, summaries)
    print("report={}".format(report))
    print(_strict_json_dumps(metrics, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
