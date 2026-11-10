#!/usr/bin/env python3
"""Stage adapter for the existing Cadence 18.1 VCO/replica flow.

The script deliberately keeps Cadence execution in child processes and writes
only below ``--run-root``/``--output``.  It provides the four commands expected
by the portable orchestrator: VCO, working-point exchange, replica, extraction.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
TOOL_ROOT = HERE.parent
BOOTSTRAP_ROOTS = [TOOL_ROOT]
if TOOL_ROOT.name == "_runtime" and TOOL_ROOT.parent.name == "isf_tool":
    BOOTSTRAP_ROOTS.append(TOOL_ROOT.parent.parent)
for bootstrap_root in reversed(BOOTSTRAP_ROOTS):
    if str(bootstrap_root) not in sys.path:
        sys.path.insert(0, str(bootstrap_root))

from isf_tool.core.source_paths import (
    VCO_NETLIST_RELATIVE,
    resolve_source_path,
)
from isf_tool.core.maestro_pair import (
    inspect_maestro_pair, restore_operating_point_text,
    operating_nonbias_parameters, operating_template_key, match_flicker_operating_source,
    replaceable_bias_variables,
)
from isf_tool.core.generated_flicker import DC_MAPPING, generated_fixture


DEFAULT_ENGINE = HERE / "dynamic_isf_closure_engine.py"
DEFAULT_EXPORTER = HERE / "export_audited_bundle.py"
APP_CONFIG_NAME = ".isf_resolved_config.json"
PAIR_ENGINE_CONFIG_NAME = ".isf_maestro_engine_config.json"
PAIR_SOURCE_MANIFEST_NAME = ".isf_maestro_source_manifest.json"


def file_sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def tree_identity(root: Path):
    """Return a deterministic identity for a private/generated netlist tree."""

    root = root.expanduser().resolve()
    records = []
    total = 0
    for current, directories, files in os.walk(str(root), topdown=True, followlinks=False):
        current_path = Path(current)
        for name in sorted(directories):
            path = current_path / name
            if path.is_symlink():
                raise RuntimeError(
                    "generated Maestro netlist contains a directory symlink: {}".format(path)
                )
        directories[:] = sorted(directories)
        for name in sorted(files):
            path = current_path / name
            if path.is_symlink():
                raise RuntimeError(
                    "generated Maestro netlist contains a file symlink: {}".format(path)
                )
            if not path.is_file():
                continue
            relative = str(path.relative_to(root))
            size = path.stat().st_size
            total += size
            records.append(
                {
                    "path": relative,
                    "type": "file",
                    "bytes": size,
                    "sha256": file_sha256(path),
                }
            )
    encoded = json.dumps(
        records, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "root": str(root),
        "files": records,
        "file_count": len(records),
        "bytes": total,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def copy_regular_tree(source: Path, target: Path):
    """Copy a Maestro-generated tree while refusing symlink escapes."""

    source = source.expanduser().resolve()
    if not source.is_dir():
        raise RuntimeError("generated Maestro netlist directory is absent: {}".format(source))
    if target.exists():
        shutil.rmtree(str(target))
    target.mkdir(parents=True, exist_ok=True)
    for current, directories, files in os.walk(str(source), topdown=True, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(source)
        target_current = target / relative
        target_current.mkdir(parents=True, exist_ok=True)
        for name in sorted(directories):
            path = current_path / name
            if path.is_symlink():
                raise RuntimeError(
                    "generated Maestro netlist contains a directory symlink: {}".format(path)
                )
        directories[:] = sorted(directories)
        for name in sorted(files):
            path = current_path / name
            if path.is_symlink():
                raise RuntimeError(
                    "generated Maestro netlist contains a file symlink: {}".format(path)
                )
            if path.is_file():
                shutil.copy2(str(path), str(target_current / name))
    source_identity = tree_identity(source)
    target_identity = tree_identity(target)
    if source_identity["sha256"] != target_identity["sha256"]:
        raise RuntimeError("private Maestro netlist tree copy hash mismatch")
    return source_identity, target_identity


def read_json(path: Path):
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise RuntimeError("JSON root must be an object: {}".format(path))
    return value


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".{}.tmp-{}".format(path.name, os.getpid()))
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))


def safe_name(value, fallback):
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._")
    return text or fallback


def is_maestro_pair_config(app_config):
    mode = str(app_config.get("source_mode", "")).lower()
    return mode in ("maestro_pair", "maestro-pair", "pair")


def _selected_model_paths(role_report):
    test = (role_report or {}).get("selected_test") or {}
    return set(
        os.path.realpath(str(item.get("path")))
        for item in test.get("models", [])
        if item.get("path") and os.path.isfile(str(item.get("path")))
    )


def _choose_current_model(engine_config, pair_report):
    vco_models = _selected_model_paths(pair_report.get("roles", {}).get("vco"))
    flicker_models = _selected_model_paths(
        pair_report.get("roles", {}).get("flicker")
    )
    common = sorted(vco_models & flicker_models)
    configured = engine_config.get("model_file")
    if configured and os.path.isfile(str(configured)):
        configured_path = os.path.realpath(str(configured))
        if common and configured_path not in common:
            raise RuntimeError(
                "engine model_file differs from the model shared by the two "
                "bound Maestro tests"
            )
        if configured_path in common:
            return configured_path
    basename = os.path.basename(str(configured or "")).lower()
    matching = [
        path for path in common
        if os.path.basename(path).lower() == basename
    ]
    if len(matching) == 1:
        return matching[0]
    unique_files = sorted(set(common))
    if len(unique_files) == 1:
        return unique_files[0]
    raise RuntimeError(
        "engine model_file is unavailable and the Maestro pair does not "
        "provide one unique replacement"
    )


def _apply_detected_environment(engine_config, app_config):
    detection = app_config.get("detection", {})
    environment = detection.get("environment", {}) if isinstance(detection, dict) else {}
    executables = environment.get("executables", {}) if isinstance(environment, dict) else {}
    for name in ("spectre", "psf"):
        configured = engine_config.get(name)
        if configured and os.path.isfile(str(configured)) and os.access(str(configured), os.X_OK):
            continue
        detected = executables.get(name) if isinstance(executables, dict) else None
        if detected and os.path.isfile(str(detected)) and os.access(str(detected), os.X_OK):
            engine_config[name] = os.path.abspath(str(detected))
    configured_env = engine_config.get("cadence_env")
    detected_env = environment.get("cadence_env") if isinstance(environment, dict) else None
    if not configured_env or not os.path.isfile(str(configured_env)):
        if detected_env and os.path.isfile(str(detected_env)):
            engine_config["cadence_env"] = os.path.realpath(str(detected_env))
        else:
            engine_config.pop("cadence_env", None)
    for optional_path in ("ld_preload", "license_file"):
        value = engine_config.get(optional_path)
        if value and "@" not in str(value) and not all(
            os.path.exists(item) for item in str(value).split(os.pathsep)
        ):
            engine_config.pop(optional_path, None)
    for name in ("spectre", "psf"):
        value = engine_config.get(name)
        if not value or not os.path.isfile(str(value)):
            raise RuntimeError(
                "{} is not available in the bound Cadence environment".format(name)
            )


def _point_template(templates, source, index, used):
    corner = str(source.get("corner") or "")
    for template_index, template in enumerate(templates):
        if template_index in used:
            continue
        if corner and str(template.get("corner") or "") == corner:
            used.add(template_index)
            return dict(template)
    if index < len(templates) and index not in used:
        used.add(index)
        return dict(templates[index])
    for template_index, template in enumerate(templates):
        if template_index not in used:
            used.add(template_index)
            return dict(template)
    return {}


def _nonbias_parameters(source, bias_variables=()):
    return operating_nonbias_parameters(source, bias_variables)


def _flicker_template_key(source, bias_variables=()):
    """Bias sampling may be replaced; every other corner distinction is retained."""
    return operating_template_key(source, bias_variables)


def _flicker_source_for_point(sources, vco_source, index=None, vco_count=None, bias_variables=()):
    """Choose a deterministic flicker template without cross-result fitting."""

    return match_flicker_operating_source(sources, vco_source, bias_variables)


def _restore_private_operating_point(target, source):
    contract = source.get('operating_point') or {}
    before = target.read_text(encoding='utf-8')
    after, changes = restore_operating_point_text(before, contract)
    expected = contract.get('effective_netlist_sha256')
    actual = hashlib.sha256(after.encode('utf-8')).hexdigest()
    if expected and actual != expected:
        raise RuntimeError('Maestro operating-point preview changed before private materialization')
    if after != before:
        target.write_text(after, encoding='utf-8')
    return {'contract': contract, 'restored_values': changes,
            'before_sha256': hashlib.sha256(before.encode('utf-8')).hexdigest(),
            'effective_netlist_sha256': actual}


def materialize_maestro_engine(engine, engine_template_path, run_root, app_config):
    """Seal the VCO history and materialize a private replica source."""

    analysis = app_config.get("analysis", {})
    offset = analysis.get("offset", {}) if isinstance(analysis, dict) else {}
    offset_hz = float(offset.get("hz", 10000.0)) if isinstance(offset, dict) else 10000.0
    pair_report = inspect_maestro_pair(
        {
            "testbenches": app_config.get("testbenches", {}),
            "offset_hz": offset_hz,
            "include_histories": True,
        }
    )
    if not pair_report.get("ok"):
        errors = [
            "{}: {}".format(item.get("code"), item.get("detail"))
            for item in pair_report.get("issues", [])
            if item.get("severity") == "error"
        ]
        raise RuntimeError("Maestro pair contract failed: {}".format("; ".join(errors)))
    vco_report = pair_report.get("roles", {}).get("vco") or {}
    flicker_report = pair_report.get("roles", {}).get("flicker") or {}
    generated = pair_report.get("pair", {}).get("replica_source") == "generated"
    vco_history = vco_report.get("selected_history")
    flicker_history = flicker_report.get("selected_history") or {}
    if not vco_history or not vco_history.get("fully_complete"):
        raise RuntimeError(
            "selected VCO Maestro has no fully complete local history with generated netlists"
        )
    if not generated and not flicker_history.get("fully_complete"):
        raise RuntimeError(
            "selected flicker Maestro has no fully complete local history with generated netlists"
        )
    sources = list(vco_history.get("source_netlists", []))
    flicker_sources = list(flicker_history.get("source_netlists", []))
    if not sources:
        raise RuntimeError("selected VCO history contains no generated input.scs")
    if not generated and not flicker_sources:
        raise RuntimeError("selected flicker history contains no generated input.scs")

    def expected_tree_hashes(role):
        manifest = (
            pair_report.get("bindings", {})
            .get(role, {})
            .get("history_manifest", {})
        )
        output = {}
        for item in manifest.get("source_netlists", []):
            path = item.get("path")
            tree = item.get("netlist_tree") or {}
            if path and tree.get("sha256"):
                output[str(Path(path).expanduser().resolve())] = tree["sha256"]
        return output

    expected_vco_trees = expected_tree_hashes("vco")
    expected_flicker_trees = expected_tree_hashes("flicker")
    # ADE sweeps commonly point every design point at one generated template.
    # Copy that physical template once; the VGS/VDS values are replaced from
    # the VCO waveform for every DC sample, so sweep-point duplication carries
    # no additional topology information.
    flicker_mapping = dict(DC_MAPPING if generated else (
        pair_report.get('bindings', {}).get('flicker', {}).get('mapping', {})
        or flicker_report.get('mapping', {})))
    bias_variables = replaceable_bias_variables(flicker_mapping)
    unique_flicker_sources = {}
    for source in flicker_sources:
        key = _flicker_template_key(source, bias_variables)
        if key not in unique_flicker_sources:
            unique_flicker_sources[key] = dict(source, template_key=key, represented_points=[])
        unique_flicker_sources[key]['represented_points'].append(source.get('operating_point') or {
            'point_id': source.get('point_id'), 'corner': source.get('corner')})
    flicker_sources = list(unique_flicker_sources.values())

    engine_template_path = Path(engine_template_path).expanduser().resolve()
    engine_config = engine.load_config(engine_template_path)
    engine_config = dict(
        (key, value)
        for key, value in engine_config.items()
        if not str(key).startswith("_")
    )
    if generated:
        if engine_config.get("replica_source") != "generated":
            raise RuntimeError("automatic replica requires its generated engine template")
    else:
        engine_config["model_file"] = _choose_current_model(engine_config, pair_report)
    _apply_detected_environment(engine_config, app_config)
    engine_config["offset_hz"] = offset_hz
    # Legacy saved engine controls cannot revive the retired object branch.
    engine_config.pop("additional_noise_objects", None)
    engine_config.pop("additional_noise_objects_report", None)
    if analysis.get("max_harmonic") is not None:
        engine_config["max_harmonic"] = int(analysis["max_harmonic"])

    source_root = run_root / "_maestro_sources"
    if source_root.exists():
        shutil.rmtree(str(source_root))
    source_root.mkdir(parents=True, exist_ok=True)
    templates = list(engine_config.get("points", []))
    points = []
    source_records = []
    flicker_records = []
    used_templates = set()
    model_cache = {}
    sources.sort(
        key=lambda item: (
            int(item.get("design_point") or item.get("point_id") or 0),
            str(item.get("corner") or ""),
            str(item.get("path") or ""),
        )
    )
    for index, source in enumerate(sources):
        source_path = Path(source["path"]).expanduser().resolve()
        if not source_path.is_file():
            raise RuntimeError("generated VCO netlist disappeared: {}".format(source_path))
        corner = safe_name(source.get("corner"), "nominal")
        source_dir = "point_{:03d}_{}".format(index + 1, corner)
        target_dir = source_root / source_dir / "vco" / "netlist"
        source_tree, private_tree = copy_regular_tree(source_path.parent, target_dir)
        expected_tree = expected_vco_trees.get(str(source_path))
        if expected_tree and source_tree["sha256"] != expected_tree:
            raise RuntimeError("VCO Maestro history changed during materialization")
        target = target_dir / "input.scs"
        operating_point = _restore_private_operating_point(target, source)
        private_tree = tree_identity(target_dir)
        template = _point_template(templates, source, index, used_templates)
        template["source_dir"] = source_dir
        template["corner"] = source.get("corner") or template.get("corner") or "nominal"
        if "k" not in template:
            template["k"] = float(source.get("design_point") or index)
        template["maestro_point_id"] = source.get("point_id")
        template["maestro_design_point"] = source.get("design_point")
        template['operating_point'] = operating_point['contract']
        if generated:
            instance = vco_report.get("mapping", {}).get("target_instance")
            fixture, evidence, _ = generated_fixture(
                source_path, instance, offset_hz,
                text=target.read_text(encoding="utf-8"), read_cache=model_cache,
            )
            model_fields = ("model_file", "device_subckt_file", "device_subckt_section")
            if any(evidence[key] != engine_config.get(key) for key in model_fields):
                raise RuntimeError("active MOS definition changed since Preflight: " + instance)
            flicker_target = source_root / source_dir / "flicker" / "netlist" / "input.scs"
            flicker_target.parent.mkdir(parents=True)
            flicker_target.write_text(fixture, encoding="utf-8")
            fixture_sha256 = file_sha256(flicker_target)
            template["flicker_template_netlist"] = str(flicker_target)
            template["flicker_template_key"] = fixture_sha256
            flicker_records.append({
                "source": str(source_path),
                "source_sha256": file_sha256(source_path),
                "private_copy": str(flicker_target),
                "private_copy_sha256": fixture_sha256,
                "point_id": source.get("point_id"),
                "corner": source.get("corner"),
                "replica_source": "generated",
                "model": evidence,
            })
        else:
            matched_flicker = _flicker_source_for_point(
                flicker_sources, source, bias_variables=bias_variables
            )
            template['_flicker_template_key'] = matched_flicker['template_key']
        points.append(template)
        source_records.append(
            {
                "source": str(source_path),
                "source_sha256": file_sha256(source_path),
                "private_copy": str(target),
                "private_copy_sha256": file_sha256(target),
                "source_tree": source_tree,
                "private_tree": private_tree,
                "point_id": source.get("point_id"),
                "design_point": source.get("design_point"),
                "corner": source.get("corner"),
                "test": source.get("test"),
                "psf_dir": source.get("psf_dir"),
                "operating_point": operating_point,
            }
        )

    copied_flicker = {}
    flicker_sources.sort(
        key=lambda item: (
            int(item.get("design_point") or item.get("point_id") or 0),
            str(item.get("corner") or ""),
            str(item.get("path") or ""),
        )
    )
    for index, source in enumerate(flicker_sources):
        source_path = Path(source["path"]).expanduser().resolve()
        if not source_path.is_file():
            raise RuntimeError("generated flicker netlist disappeared: {}".format(source_path))
        target_dir = source_root / "flicker" / "template_{:03d}".format(index + 1) / "netlist"
        source_tree, private_tree = copy_regular_tree(source_path.parent, target_dir)
        expected_tree = expected_flicker_trees.get(str(source_path))
        if expected_tree and source_tree["sha256"] != expected_tree:
            raise RuntimeError("flicker Maestro history changed during materialization")
        target = target_dir / "input.scs"
        operating_point = _restore_private_operating_point(target, source)
        private_tree = tree_identity(target_dir)
        copied_flicker[source['template_key']] = str(target)
        flicker_records.append(
            {
                "source": str(source_path),
                "source_sha256": file_sha256(source_path),
                "private_copy": str(target),
                "private_copy_sha256": file_sha256(target),
                "source_tree": source_tree,
                "private_tree": private_tree,
                "point_id": source.get("point_id"),
                "design_point": source.get("design_point"),
                "corner": source.get("corner"),
                "test": source.get("test"),
                "psf_dir": source.get("psf_dir"),
                "operating_point": operating_point,
                "template_key": source['template_key'],
                "represented_points": source.get('represented_points', []),
            }
        )
    if not generated:
        for point in points:
            key = point.pop('_flicker_template_key')
            point["flicker_template_netlist"] = copied_flicker[key]
            point['flicker_template_key'] = key

    engine_config["source_root"] = str(source_root)
    engine_config["points"] = points
    engine_config["flicker_template_netlist"] = flicker_records[0]["private_copy"]
    engine_config["flicker_mapping"] = flicker_mapping
    engine_config["vco_mapping"] = dict(
        pair_report.get("bindings", {}).get("vco", {}).get("mapping", {})
        or vco_report.get("mapping", {})
    )
    engine_config["maestro_pair_binding"] = {
        "schema_version": 1,
        "vco": pair_report.get("bindings", {}).get("vco"),
        "flicker": pair_report.get("bindings", {}).get("flicker"),
        "vco_history": vco_history.get("path"),
        "vco_history_fingerprint": vco_report.get("history_fingerprint"),
        "flicker_history": flicker_history.get("path"),
        "flicker_history_fingerprint": flicker_report.get("history_fingerprint"),
        "engine_template": str(engine_template_path),
        "engine_template_sha256": file_sha256(engine_template_path),
        "app_config_sha256": file_sha256(run_root / APP_CONFIG_NAME),
    }
    if generated:
        engine_config["maestro_pair_binding"]["replica_source"] = "generated"
    engine_path = run_root / PAIR_ENGINE_CONFIG_NAME
    manifest_path = run_root / PAIR_SOURCE_MANIFEST_NAME
    write_json(engine_path, engine_config)
    write_json(
        manifest_path,
        {
            "schema_version": 1,
            "read_only_sources": True,
            "private_source_root": str(source_root),
            "engine_config": str(engine_path),
            "engine_config_sha256": file_sha256(engine_path),
            "app_config_sha256": file_sha256(run_root / APP_CONFIG_NAME),
            "bindings": pair_report.get("bindings", {}),
            "sources": source_records,
            "roles": {
                "vco": source_records,
                "flicker": flicker_records,
            },
        },
    )
    return engine_path


def execution_config_path(engine, requested_path, run_root, stage):
    requested = Path(requested_path).expanduser().resolve()
    app_path = run_root / APP_CONFIG_NAME
    if not app_path.is_file():
        requested_value = read_json(requested)
        if not is_maestro_pair_config(requested_value):
            return requested
        # Manual/portable CLI use may pass the resolved UI/preflight config
        # directly.  Persist it only inside the private run root, matching the
        # orchestrator contract, and derive the bundled engine template from
        # its sealed detection record.
        write_json(app_path, requested_value)
    app_config = read_json(app_path)
    if not is_maestro_pair_config(app_config):
        return requested
    try:
        requested_value = read_json(requested)
    except (OSError, RuntimeError, ValueError):
        requested_value = {}
    if is_maestro_pair_config(requested_value):
        effective = (
            app_config.get("detection", {}).get("effective_engine_config", {})
            if isinstance(app_config.get("detection"), dict)
            else {}
        )
        runtime_identity = app_config.get("runtime_identity", {})
        engine_template = (
            app_config.get("config_path")
            or effective.get("template")
            or (
                runtime_identity.get("engine_config_path")
                if isinstance(runtime_identity, dict)
                else None
            )
        )
        if not engine_template:
            raise RuntimeError(
                "resolved Maestro-pair config does not identify its engine template"
            )
        requested = Path(engine_template).expanduser().resolve()
    materialized = run_root / PAIR_ENGINE_CONFIG_NAME
    manifest = run_root / PAIR_SOURCE_MANIFEST_NAME
    if stage == "vco" or not materialized.is_file() or not manifest.is_file():
        return materialize_maestro_engine(
            engine, requested, run_root, app_config
        )
    manifest_value = read_json(manifest)
    if manifest_value.get("app_config_sha256") != file_sha256(app_path):
        raise RuntimeError(
            "resolved app config changed after Maestro source materialization"
        )
    if manifest_value.get("engine_config_sha256") != file_sha256(materialized):
        raise RuntimeError("materialized Maestro engine config hash mismatch")
    for record in manifest_value.get("roles", {}).get("flicker", []):
        if record.get("replica_source") == "generated":
            target = Path(record["private_copy"])
            if not target.is_file() or record["private_copy_sha256"] != file_sha256(target):
                raise RuntimeError("generated MOS fixture changed after materialization: " + str(target))
    return materialized


def load_module(path: Path, name: str):
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_engine_config(engine, path: Path):
    """Load through one absolute config identity so its relative paths are stable."""
    config_path = path.expanduser().resolve()
    return config_path, engine.load_config(config_path)


def selected_points(engine, config, smoke=False):
    """Use the engine's single source of truth for smoke-point selection."""

    return engine.select_points(config, smoke=smoke)


def source_case_path(config, point):
    """Resolve one point below source_root, including symlink-aware checks."""
    resolve_source_path(
        config["source_root"],
        point["source_dir"],
        VCO_NETLIST_RELATIVE,
    )
    return Path(
        resolve_source_path(
            config["source_root"], point["source_dir"]
        )
    )


def source_netlist_path(config, point):
    return Path(
        resolve_source_path(
            config["source_root"],
            point["source_dir"],
            VCO_NETLIST_RELATIVE,
        )
    )


def run_vco(engine, config, points, run_root, force=False):
    for point in points:
        name = engine.point_name(point)
        source_netlist = source_netlist_path(config, point)
        target = run_root / name / "main_vco" / "netlist" / "input.scs"
        engine.prepare_main_netlist(config, source_netlist, target)
        status = engine.run_spectre(config, target.parent, force=force)
        print("{} vco={}".format(name, status), flush=True)


def exchange_workpoint(engine, config, points, run_root, force=False):
    traces = engine.physical_profile(config)["main"]["traces"]
    for point in points:
        name = engine.point_name(point)
        main_psf = run_root / name / "main_vco" / "psf"
        engine.verify_spectre(main_psf)
        wave = engine.sample_periodic_wave(
            config,
            main_psf,
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
        dc_status = engine.ensure_dc_flicker_samples(
            config, point, run_root / name, wave, force=force
        )
        target = run_root / name / "replica" / "netlist" / "input.scs"
        engine.prepare_replica_netlist(config, wave, target, point=point)
        print(
            "{} exchange=prepared dc={}".format(name, dc_status),
            flush=True,
        )


def run_replica(engine, config, points, run_root, force=False):
    from isf_tool.core.thermal_execution import run_optional_thermal
    for point in points:
        name = engine.point_name(point)
        netlist = run_root / name / "replica" / "netlist" / "input.scs"
        if not netlist.is_file():
            raise RuntimeError("replica netlist is absent; run exchange first: {}".format(netlist))
        status = engine.run_spectre(config, netlist.parent, force=force)
        print("{} replica={}".format(name, status), flush=True)
        if config.get("replica_source") != "generated":
            thermal = run_optional_thermal(engine, config, point, run_root / name, force=force)
            print("{} thermal={}".format(name, thermal["status"]), flush=True)


def analyze(engine, config_path, run_root, smoke=False):
    argv = ["--config", str(config_path), "--run-root", str(run_root), "--analyze-only"]
    if smoke:
        argv.append("--smoke")
    code = engine.main(argv)
    if code not in (None, 0):
        raise RuntimeError("dynamic ISF analysis returned {}".format(code))


def export_canonical(
    engine_path,
    config_path,
    run_root,
    output,
    job_id=None,
    non_signoff=False,
    formal_signoff=False,
):
    exporter = load_module(DEFAULT_EXPORTER, "isf_export_audited_bundle")
    arguments = [
            "--engine", str(engine_path),
            "--run-root", str(run_root),
            "--config", str(config_path),
            "--output", str(output),
        ]
    if job_id:
        arguments.extend(["--job-id", str(job_id)])
    if non_signoff:
        arguments.append("--non-signoff")
    if formal_signoff:
        arguments.append("--formal-signoff")
    exporter.main(arguments)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("vco", "exchange", "replica", "extract", "all"), required=True)
    parser.add_argument("--engine", type=Path, default=DEFAULT_ENGINE)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--job-id")
    parser.add_argument("--smoke", action="store_true")
    export_mode = parser.add_mutually_exclusive_group()
    export_mode.add_argument(
        "--non-signoff",
        action="store_true",
        help="retain a job id but export replay evidence instead of formal signoff",
    )
    export_mode.add_argument(
        "--formal-signoff",
        action="store_true",
        help="explicitly request the six-point formal provenance and quality gate",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.smoke and args.formal_signoff:
        parser.error("--smoke cannot be combined with --formal-signoff")

    engine_path = args.engine.expanduser().resolve()
    run_root = args.run_root.expanduser().resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    engine = load_module(engine_path, "dynamic_isf_closure_engine")
    materialize_stage = "vco" if args.stage == "all" else args.stage
    effective_config = execution_config_path(
        engine, args.config, run_root, materialize_stage
    )
    config_path, config = load_engine_config(engine, effective_config)
    points = selected_points(engine, config, args.smoke)

    if args.stage in ("vco", "all"):
        run_vco(engine, config, points, run_root, force=args.force)
    if args.stage in ("exchange", "all"):
        exchange_workpoint(engine, config, points, run_root, force=args.force)
    if args.stage in ("replica", "all"):
        run_replica(engine, config, points, run_root, force=args.force)
    if args.stage in ("extract", "all"):
        if args.output is None:
            parser.error("--output is required for extract/all")
        analyze(engine, config_path, run_root, smoke=args.smoke)
        export_canonical(
            engine_path,
            config_path,
            run_root,
            args.output.expanduser().resolve(),
            job_id=args.job_id,
            non_signoff=bool(args.smoke or args.non_signoff),
            formal_signoff=args.formal_signoff,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
