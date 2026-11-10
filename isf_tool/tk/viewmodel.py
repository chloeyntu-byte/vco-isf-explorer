"""Pure view-model helpers for the native Tk interface.

The functions in this module deliberately know nothing about Tk widgets or
``ToolService``.  They translate the stable nested configuration/result
contracts into small dictionaries that a GUI can render without duplicating
the EDA execution core.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from typing import Any

from ..core.offsets import expand_offsets, frequency_hz
from ..core.result_presentation import enrich_result, enrich_summary, noise_contribution_rows
from ..core.thermal_budget import calculated_noise_dbc
from ..core.ui_state import strip_retired_noise_objects


N_A = "N/A"
_MISSING = object()
_UNIT_SCALE = {"Hz": 1.0, "kHz": 1_000.0, "MHz": 1_000_000.0}

CATALOG_KIND_LABELS = {
    "net": "网名 · Net", "output": "输出 · Output", "variable": "变量 · Variable",
    "parameter": "网表参数", "device": "器件", "source": "电源 / 探针",
    "analysis": "分析", "save": "保存信号", "trace": "波形引用",
    "model": "模型", "include": "引用文件", "subcircuit": "子电路",
}


def catalog_review_rows(report: Mapping[str, Any] | None, query: str = "", kind: str = "") -> list[dict[str, Any]]:
    """Expose every discovered name; semantic VGS/VDS/VBS rows are a separate view."""
    roles = _nested(report or {}, "roles", default={})
    query = query.strip().casefold()
    result = []
    for role in ("vco", "flicker"):
        for index, item in enumerate(_nested(roles, role, "signal_catalog", default=[]) or []):
            if not isinstance(item, Mapping) or (kind and item.get("kind") != kind):
                continue
            row = copy.deepcopy(dict(item))
            row.update(iid=f"{role}:{index}", role=role, role_label="VCO" if role == "vco" else "Flicker")
            row["kind_label"] = CATALOG_KIND_LABELS.get(str(row.get("kind")), str(row.get("kind") or "其他"))
            details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
            if not row.get("expression"):
                row["expression"] = details.get("value", details.get("default", ""))
            if query and query not in str(row).casefold():
                continue
            result.append(row)
    return result


def catalog_summary(report: Mapping[str, Any] | None) -> str:
    rows = catalog_review_rows(report)
    counts = {kind: sum(row.get("kind") == kind for row in rows) for kind in ("net", "output", "variable")}
    return f"网名 {counts['net']} 项 · Outputs {counts['output']} 项 · Variables {counts['variable']} 项 · 全部 {len(rows)} 项"


def portable_issue_rows(setup: Mapping[str, Any] | None, pair_report: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Translate execution errors to real UI fields without changing acceptance gates."""
    setup = setup or {}
    fields = {str(row.get("key")): row for row in setup.get("fields", []) if isinstance(row, Mapping)}
    mappings = {f"{row['role']}:{row['key']}": row for row in mapping_review_rows(pair_report)}
    aliases = {
        "main.target_instance": "vco:target_instance", "main.instance_path": "vco:target_instance",
        "main.traces.peer_noise": "vco:peer_instance", "replica.dc_instance": "replica:dc_instance",
        "replica.dc_sense_source": "replica:dc_sense_source", "replica.dc_transducer": "replica:dc_transducer",
        "replica.dc_noise_output": "replica:output_p", "replica.dc_noise_reference": "replica:output_n",
        "main.output.positive": "vco:output_n", "main.output.negative": "vco:output_p",
    }
    missing_target = mappings.get("vco:target_instance", {})
    if missing_target and (not missing_target.get("value") or missing_target.get("status") in {"missing", "ambiguous", "invalid"}):
        for key in ("main.device_subckt", "main.traces.target_noise", "main.terminals.drain", "main.terminals.gate", "main.terminals.source", "main.terminals.bulk"):
            aliases[key] = "vco:target_instance"
    rows = []
    for error in setup.get("errors", []):
        if not isinstance(error, Mapping):
            continue
        key = str(error.get("field") or "portable_setup")
        mapping_iid = aliases.get(key, "")
        for role in ("vco", "replica"):
            prefix = f"testbenches.{role}.mapping_overrides."
            if key.startswith(prefix):
                mapping_iid = f"{role}:{key[len(prefix):]}"
        target = mappings.get(mapping_iid, {})
        field_label = str(fields.get(key, {}).get("label") or target.get("label") or "仿真执行设置")
        destination_label = str(target.get("label") or field_label)
        message = str(error.get("message") or "请检查该项的识别结果。")
        if not message.startswith(field_label):
            message = field_label + "：" + message
        rows.append({"field": key, "label": field_label, "message": message,
                     "mapping_iid": mapping_iid if mapping_iid in mappings else "",
                     "target_label": destination_label, "tab": "mapping" if mapping_iid in mappings else "physical"})
    priority = {"vco:target_instance": 0, "replica:dc_instance": 1, "vco:peer_instance": 2}
    return sorted(rows, key=lambda row: priority.get(row["mapping_iid"], 3))


def deep_merge(base: Mapping[str, Any] | None, overlay: Mapping[str, Any] | None) -> dict[str, Any]:
    """Recursively merge mappings without mutating either input.

    Lists and all scalar values are replaced, not concatenated.  Every value
    is deep-copied so callers can safely retain the original raw configuration
    as the source of hidden/advanced fields.
    """

    result: dict[str, Any] = copy.deepcopy(dict(base or {}))
    for key, value in dict(overlay or {}).items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = deep_merge(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _unwrap_mapping(value: Mapping[str, Any] | None, wrapper: str) -> dict[str, Any]:
    source: Mapping[str, Any] = value or {}
    wrapped = source.get(wrapper)
    if isinstance(wrapped, Mapping):
        source = wrapped
    return copy.deepcopy(dict(source))


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _nested(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    value: Any = mapping
    for key in keys:
        if not isinstance(value, Mapping) or key not in value:
            return default
        value = value[key]
    return value


def _field(fields: Mapping[str, Any], *names: str) -> tuple[bool, Any]:
    for name in names:
        if name in fields:
            return True, fields[name]
    return False, None


def finite_number(value: Any) -> float | None:
    """Return a finite float, preserving missing/non-finite values as None."""

    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _number_or_na(value: Any) -> float | str:
    number = finite_number(value)
    return number if number is not None else N_A


def _strict_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _strict_integer(value: Any, name: str) -> int:
    number = finite_number(value)
    if number is None or not number.is_integer():
        raise ValueError(f"{name} must be an integer")
    return int(number)


def flatten_config(raw_config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Expose the small editable Tk surface from a nested config.

    The returned keys are intentionally stable Python names.  ``merge_config``
    consumes the same shape and keeps every raw field not represented here.
    """

    raw = _unwrap_mapping(raw_config, "config")
    testbenches = raw.get("testbenches", {})
    if not isinstance(testbenches, Mapping):
        testbenches = {}
    vco = _first(testbenches, "vco", "main", "primary", default={})
    replica = _first(testbenches, "replica", "flicker", "noise", default={})
    if not isinstance(vco, Mapping):
        vco = {}
    if not isinstance(replica, Mapping):
        replica = {}

    analysis = raw.get("analysis", {})
    execution = raw.get("execution", {})
    discovery = raw.get("discovery", {})
    profiles = raw.get("profiles", {})
    auto = raw.get("auto", {})
    if not isinstance(analysis, Mapping):
        analysis = {}
    if not isinstance(execution, Mapping):
        execution = {}
    if not isinstance(discovery, Mapping):
        discovery = {}
    if not isinstance(profiles, Mapping):
        profiles = {}
    if not isinstance(auto, Mapping):
        auto = {}

    offset = analysis.get("offset", {})
    if not isinstance(offset, Mapping):
        offset = {"value": offset}
    offset_unit = str(offset.get("unit") or "kHz")
    offset_value = finite_number(offset.get("value"))
    offset_hz = finite_number(_first(offset, "hz", "offset_hz"))
    if offset_hz is None and offset_value is not None and offset_unit in _UNIT_SCALE:
        offset_hz = offset_value * _UNIT_SCALE[offset_unit]
    if offset_value is None and offset_hz is not None and offset_unit in _UNIT_SCALE:
        offset_value = offset_hz / _UNIT_SCALE[offset_unit]
    offsets = analysis.get("offsets") or {}
    if not isinstance(offsets, Mapping):
        offsets = {}
    values = offsets.get("values", "1k,10k,1M")
    if isinstance(values, (list, tuple)):
        values = ", ".join(str(value) for value in values)

    roots = discovery.get("roots", [])
    single_root = (
        roots[0]
        if isinstance(roots, Sequence)
        and not isinstance(roots, (str, bytes))
        and len(roots) == 1
        else ""
    )
    environment = profiles.get("environment", {})
    pdk = profiles.get("pdk", {})
    if not isinstance(environment, Mapping):
        environment = {}
    executables = environment.get("executables", {})
    if not isinstance(executables, Mapping):
        executables = {}
    if not isinstance(pdk, Mapping):
        pdk = {}

    dry_run = execution.get("dry_run", True)
    allow_execute = execution.get("allow_execute", False)
    return {
        "source_mode": str(raw.get("source_mode") or "maestro_pair"),
        "project_name": str(raw.get("project_name") or ""),
        "project_root": str(raw.get("project_root") or single_root or ""),
        "vco_path": str(
            _first(vco, "maestro_dir", "path", default="")
            or _first(raw, "main_tb", "mainTb", default="")
            or ""
        ),
        "vco_test": str(
            vco.get("test") or _first(raw, "main_test", "mainTest", default="") or ""
        ),
        "vco_history": str(
            _first(vco, "history", "selected_history", default="")
            or _first(raw, "main_history", "mainHistory", default="")
            or ""
        ),
        "vco_binding_fingerprint": str(
            vco.get("binding_fingerprint")
            or _first(raw, "main_fingerprint", "mainFingerprint", default="")
            or ""
        ),
        "vco_history_fingerprint": str(
            vco.get("history_fingerprint")
            or _first(
                raw,
                "main_history_fingerprint",
                "mainHistoryFingerprint",
                default="",
            )
            or ""
        ),
        "vco_device": str(_first(vco, "device", "instance", default="") or ""),
        "vco_mapping_overrides": clean_mapping_overrides(vco.get("mapping_overrides")),
        "replica_source": str(replica.get("source") or ""),
        "replica_path": str(
            _first(replica, "maestro_dir", "path", default="")
            or _first(raw, "replica_tb", "replicaTb", default="")
            or ""
        ),
        "replica_test": str(
            replica.get("test")
            or _first(raw, "replica_test", "replicaTest", default="")
            or ""
        ),
        "replica_history": str(
            _first(replica, "history", "selected_history", default="")
            or _first(raw, "replica_history", "replicaHistory", default="")
            or ""
        ),
        "replica_binding_fingerprint": str(
            replica.get("binding_fingerprint")
            or _first(
                raw,
                "replica_fingerprint",
                "replicaFingerprint",
                default="",
            )
            or ""
        ),
        "replica_history_fingerprint": str(
            replica.get("history_fingerprint")
            or _first(
                raw,
                "replica_history_fingerprint",
                "replicaHistoryFingerprint",
                default="",
            )
            or ""
        ),
        "replica_device": str(_first(replica, "device", "instance", default="") or ""),
        "replica_mapping_overrides": clean_mapping_overrides(replica.get("mapping_overrides")),
        "portable_overrides": clean_mapping_overrides(_nested(raw, "portable_setup", "overrides")),
        "offset_hz": offset_hz,
        "offset_value": offset_value,
        "offset_unit": offset_unit,
        "offset_mode": str(offsets.get("mode") or "single"),
        "offset_values": str(values),
        "offset_start": str(offsets.get("start", "1k")),
        "offset_stop": str(offsets.get("stop", "100M")),
        "offset_spacing": str(offsets.get("spacing") or "step"),
        "offset_step": str(offsets.get("step", "1k")),
        "offset_intervals": str(offsets.get("intervals", "5")),
        "offset_points_per_decade": str(offsets.get("points_per_decade", "1")),
        "max_harmonic": _number_or_na(analysis.get("max_harmonic")),
        "phase_samples": _number_or_na(analysis.get("phase_samples")),
        "period_count": _number_or_na(analysis.get("period_count")),
        "dry_run": dry_run if isinstance(dry_run, bool) else True,
        "allow_execute": allow_execute if isinstance(allow_execute, bool) else False,
        "adapter_mode": str(execution.get("adapter_mode") or "bundled"),
        "engine_config": str(
            raw.get("config_path")
            or _first(raw, "engine_config_path", "engineConfigPath", default="")
            or ""
        ),
        "run_root": str(raw.get("run_root") or auto.get("run_root") or ""),
        "bundle_path": str(raw.get("bundle_path") or auto.get("bundle_path") or ""),
        "analysis_python": str(environment.get("python") or ""),
        "spectre_executable": str(executables.get("spectre") or ""),
        "pdk_name": str(pdk.get("name") or ""),
        "pdk_root": str(pdk.get("root") or ""),
    }


def merge_config(raw_config: Mapping[str, Any] | None, fields: Mapping[str, Any]) -> dict[str, Any]:
    """Merge editable fields into a raw nested config without data loss."""

    if not isinstance(fields, Mapping):
        raise TypeError("fields must be a mapping")
    merged = _unwrap_mapping(raw_config, "config")
    merged.setdefault("schema_version", 2)
    merged.setdefault("source_mode", "maestro_pair")

    present, value = _field(fields, "source_mode", "sourceMode")
    if present:
        merged["source_mode"] = str(value or "maestro_pair")
    present, value = _field(fields, "project_name", "projectName")
    if present:
        merged["project_name"] = str(value or "")
    present, value = _field(fields, "project_root", "projectRoot")
    if present:
        project_root = str(value or "")
        existing_discovery = merged.get("discovery", {})
        existing_roots = (
            existing_discovery.get("roots", [])
            if isinstance(existing_discovery, Mapping)
            else []
        )
        single_existing_root = (
            str(existing_roots[0])
            if isinstance(existing_roots, Sequence)
            and not isinstance(existing_roots, (str, bytes))
            and len(existing_roots) == 1
            else ""
        )
        effective_existing = str(merged.get("project_root") or single_existing_root or "")
        # An empty Tk field represents "no single root selected" when the
        # raw config owns several discovery roots.  Only an explicit new,
        # non-empty selection may collapse that list to one root.
        multiple_existing_roots = bool(
            isinstance(existing_roots, Sequence)
            and not isinstance(existing_roots, (str, bytes))
            and len(existing_roots) > 1
        )
        if project_root != effective_existing and not (
            not project_root and multiple_existing_roots
        ):
            merged["project_root"] = project_root
            discovery = merged.setdefault("discovery", {})
            if not isinstance(discovery, dict):
                discovery = {}
                merged["discovery"] = discovery
            discovery["roots"] = [project_root] if project_root else []

    testbenches = merged.setdefault("testbenches", {})
    if not isinstance(testbenches, dict):
        testbenches = {}
        merged["testbenches"] = testbenches
    role_specs = {
        "vco": {
            "target": "vco",
            "role": "vco",
            "fields": {
                "path": ("vco_path", "main_tb", "mainTb"),
                "test": ("vco_test", "main_test", "mainTest"),
                "history": ("vco_history", "main_history", "mainHistory"),
                "binding_fingerprint": (
                    "vco_binding_fingerprint",
                    "main_fingerprint",
                    "mainFingerprint",
                ),
                "history_fingerprint": (
                    "vco_history_fingerprint",
                    "main_history_fingerprint",
                    "mainHistoryFingerprint",
                ),
                "device": ("vco_device", "main_device", "mainDevice"),
            },
        },
        "replica": {
            "target": "replica",
            "role": "flicker",
            "fields": {
                "path": ("replica_path", "replica_tb", "replicaTb"),
                "test": ("replica_test", "replicaTest"),
                "history": ("replica_history", "replicaHistory"),
                "binding_fingerprint": ("replica_binding_fingerprint", "replica_fingerprint", "replicaFingerprint"),
                "history_fingerprint": ("replica_history_fingerprint", "replicaHistoryFingerprint"),
                "device": ("replica_device", "replicaDevice"),
            },
        },
    }
    for name, description in role_specs.items():
        spec = testbenches.setdefault(description["target"], {})
        if not isinstance(spec, dict):
            spec = {}
            testbenches[description["target"]] = spec
        spec.setdefault("name", name)
        spec.setdefault("role", description["role"])
        for target_key, aliases in description["fields"].items():
            present, value = _field(fields, *aliases)
            if not present:
                continue
            text = str(value or "")
            field_exists = target_key in spec or (
                target_key == "path" and "maestro_dir" in spec
            )
            if not text and not field_exists:
                # Flattened legacy configs expose absent optional fields as
                # blanks.  A no-op round trip must not create blank canonical
                # values that shadow a still-valid legacy top-level value.
                continue
            spec[target_key] = text
            if target_key == "path":
                spec["maestro_dir"] = text
        present, value = _field(fields, f"{name}_mapping_overrides")
        if present:
            # Replacement lets a removed override return to auto detection.
            spec["mapping_overrides"] = clean_mapping_overrides(value)

    present, value = _field(fields, "replica_source")
    if present and value:
        if value not in ("generated", "maestro"):
            raise ValueError("unsupported device noise reference source")
        testbenches["replica"]["source"] = value

    present, value = _field(fields, "portable_overrides")
    if present:
        setup = merged.setdefault("portable_setup", {})
        if not isinstance(setup, dict):
            setup = {}
            merged["portable_setup"] = setup
        setup.setdefault("mode", "auto")
        setup["overrides"] = clean_mapping_overrides(value)

    analysis = merged.setdefault("analysis", {})
    if not isinstance(analysis, dict):
        analysis = {}
        merged["analysis"] = analysis
    offset = analysis.setdefault("offset", {})
    if not isinstance(offset, dict):
        offset = {}
        analysis["offset"] = offset

    hz_present, raw_hz = _field(fields, "offset_hz", "offsetHz")
    value_present, raw_value = _field(fields, "offset_value", "offsetValue")
    unit_present, raw_unit = _field(fields, "offset_unit", "offsetUnit")
    if str(fields.get("offset_mode") or "single") != "single":
        hz_present = value_present = unit_present = False
    unit = str(raw_unit) if unit_present else str(offset.get("unit") or "kHz")
    if unit not in _UNIT_SCALE:
        raise ValueError("offset_unit must be Hz, kHz, or MHz")
    if value_present:
        offset_value = finite_number(raw_value)
        if offset_value is None and raw_value not in (None, ""):
            raise ValueError("offset_value must be finite")
        if offset_value is not None or offset:
            offset["value"] = offset_value
            offset["unit"] = unit
            offset["hz"] = None if offset_value is None else offset_value * _UNIT_SCALE[unit]
    elif hz_present:
        offset_hz = finite_number(raw_hz)
        if offset_hz is None and raw_hz not in (None, ""):
            raise ValueError("offset_hz must be finite")
        offset["hz"] = offset_hz
        offset["unit"] = unit
        offset["value"] = None if offset_hz is None else offset_hz / _UNIT_SCALE[unit]
    elif unit_present:
        offset["unit"] = unit
        old_hz = finite_number(offset.get("hz"))
        if old_hz is not None:
            offset["value"] = old_hz / _UNIT_SCALE[unit]

    for target, aliases in (
        ("max_harmonic", ("max_harmonic", "harmonics")),
        ("phase_samples", ("phase_samples", "phaseSamples")),
        ("period_count", ("period_count", "periodCount")),
    ):
        present, value = _field(fields, *aliases)
        if present:
            if value in (None, "", N_A) and target not in analysis:
                continue
            analysis[target] = _strict_integer(value, target)

    if "offset_mode" in fields:
        mode = str(fields["offset_mode"] or "single")
        if mode != "single" or "offsets" in analysis:
            spec = dict(analysis.get("offsets") or {})
            spec["mode"] = mode
            for name in ("values", "start", "stop", "spacing", "step", "intervals", "points_per_decade"):
                if "offset_" + name in fields:
                    spec[name] = copy.deepcopy(fields["offset_" + name])
            spec.pop("values_hz", None)
            analysis["offsets"] = spec
        if mode != "single":
            values = expand_offsets(analysis)
            # Keep the legacy scalar field as the first requested sample.
            analysis["offset"] = {"value": values[0], "unit": "Hz", "hz": values[0]}

    execution = merged.setdefault("execution", {})
    if not isinstance(execution, dict):
        execution = {}
        merged["execution"] = execution
    present, value = _field(fields, "dry_run", "dryRun")
    if present:
        execution["dry_run"] = _strict_bool(value, "dry_run")
    present, value = _field(fields, "allow_execute", "allowExecute")
    if present:
        execution["allow_execute"] = _strict_bool(value, "allow_execute")
    present, value = _field(fields, "adapter_mode", "adapterMode")
    if present:
        execution["adapter_mode"] = str(value or "bundled")

    for target, aliases in (
        ("config_path", ("engine_config", "engine_config_path", "engineConfigPath")),
        ("run_root", ("run_root", "runRoot")),
    ):
        present, value = _field(fields, *aliases)
        if present:
            text = str(value or "")
            if text or target in merged:
                merged[target] = text
    present, value = _field(fields, "bundle_path", "auto_bundle_path", "autoBundlePath")
    if present:
        bundle_path = str(value or "")
        existing_auto = merged.get("auto")
        auto_has_bundle = isinstance(existing_auto, Mapping) and "bundle_path" in existing_auto
        if bundle_path or "bundle_path" in merged or auto_has_bundle:
            merged["bundle_path"] = bundle_path
            auto = merged.setdefault("auto", {})
            if not isinstance(auto, dict):
                auto = {}
                merged["auto"] = auto
            auto["bundle_path"] = bundle_path

    profiles = merged.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        profiles = {}
        merged["profiles"] = profiles
    environment = profiles.setdefault("environment", {})
    if not isinstance(environment, dict):
        environment = {}
        profiles["environment"] = environment
    executables = environment.setdefault("executables", {})
    if not isinstance(executables, dict):
        executables = {}
        environment["executables"] = executables
    present, value = _field(fields, "analysis_python", "analysisPython")
    if present:
        text = str(value or "")
        if text or "python" in environment:
            environment["python"] = text
    present, value = _field(fields, "spectre_executable", "spectreExecutable")
    if present:
        text = str(value or "")
        if text or "spectre" in executables:
            executables["spectre"] = text
    pdk_name_present, pdk_name = _field(fields, "pdk_name", "pdkName")
    pdk_root_present, pdk_root = _field(fields, "pdk_root", "pdkRoot")
    if pdk_name_present or pdk_root_present:
        existing = profiles.get("pdk")
        name_text = str(pdk_name or "") if pdk_name_present else ""
        root_text = str(pdk_root or "") if pdk_root_present else ""
        # A blank pair is the Tk representation of the portable ``"auto"``
        # sentinel.  Do not replace that hidden sentinel with an empty object
        # during a no-op form round trip.
        if not (not name_text and not root_text and not isinstance(existing, Mapping)):
            pdk = copy.deepcopy(dict(existing)) if isinstance(existing, Mapping) else {}
            if pdk_name_present:
                pdk["name"] = name_text
            if pdk_root_present:
                pdk["root"] = root_text
            profiles["pdk"] = pdk
    return strip_retired_noise_objects(merged)


def invalidate_role(raw_config: Mapping[str, Any] | None, role: str) -> dict[str, Any]:
    """Return a copy with both fingerprints for one Maestro role cleared."""

    normalized = str(role).lower()
    if normalized in {"vco", "main", "primary"}:
        key = "vco"
    elif normalized in {"replica", "flicker", "noise"}:
        key = "replica"
    else:
        raise ValueError("role must be vco/main or replica/flicker")
    result = _unwrap_mapping(raw_config, "config")
    testbenches = result.setdefault("testbenches", {})
    if not isinstance(testbenches, dict):
        testbenches = {}
        result["testbenches"] = testbenches
    spec = testbenches.setdefault(key, {})
    if not isinstance(spec, dict):
        spec = {}
        testbenches[key] = spec
    spec["binding_fingerprint"] = ""
    spec["history_fingerprint"] = ""
    spec.pop("mapping_overrides", None)
    spec.pop("mapping", None)
    result.pop("mapping", None)
    result.pop("detection", None)
    setup = result.get("portable_setup")
    if isinstance(setup, dict):
        setup["overrides"] = {}
        for name in ("engine", "fields", "binding_fingerprint"):
            setup.pop(name, None)
    return result


def clean_mapping_overrides(value: Any) -> dict[str, Any]:
    """Keep explicit selections only; blank means return to detection."""

    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): copy.deepcopy(item.strip() if isinstance(item, str) else item)
        for key, item in value.items()
        if item is not None and (not isinstance(item, str) or item.strip())
    }


def offset_preview(fields: Mapping[str, Any], operating_point_count: int | None = None) -> dict[str, Any]:
    """Use the execution parser to preview sites; never infer noise values."""

    mode = str(fields.get("offset_mode") or "single")
    unit = str(fields.get("offset_unit") or "Hz")
    analysis = {"offset": {"value": fields.get("offset_value"), "unit": unit},
                "offsets": {"mode": mode}}
    for name in ("values", "start", "stop", "spacing", "step", "intervals", "points_per_decade"):
        if "offset_" + name in fields:
            analysis["offsets"][name] = fields["offset_" + name]
    values = expand_offsets(analysis)
    endpoint_appended = False
    if mode == "linear" and str(fields.get("offset_spacing") or "step") == "step":
        ratio = (frequency_hz(fields["offset_stop"]) - frequency_hz(fields["offset_start"])) / frequency_hz(fields["offset_step"])
        endpoint_appended = not math.isclose(ratio, round(ratio), rel_tol=1e-12, abs_tol=1e-12)
    count = operating_point_count if operating_point_count and operating_point_count > 0 else None
    return {"values_hz": values, "offset_count": len(values), "operating_point_count": count,
            "total_samples": len(values) * count if count else None,
            "endpoint_appended": endpoint_appended}


def active_mapping_rows(report: Mapping[str, Any], show_all: bool = False) -> list[dict[str, Any]]:
    rows = mapping_review_rows(report)
    if show_all:
        return rows
    return [row for row in rows if row.get("active", True) and row.get("group") != "execution_detail"
            or row.get("required") or row.get("status") in {"invalid", "ambiguous", "error", "pending", "stale"}]


def mapping_candidate_names(row: Mapping[str, Any], selected: Any = None, expand_families: bool = False) -> list[str]:
    """Compact backend-provided families; never infer membership from names."""
    candidates = list(dict.fromkeys(str(name) for name in row.get("candidates", []) if name is not None))
    current = str(selected if selected is not None else row.get("value") or row.get("detected") or "")
    details = row.get("candidate_details")
    hidden = set()
    if not expand_families and isinstance(details, Mapping):
        for name, info in details.items():
            if name not in candidates or not isinstance(info, Mapping) or info.get("kind") != "transfer_family" or info.get("errors"):
                continue
            members = [str(item["name"]) for item in info.get("members", []) if isinstance(item, Mapping) and item.get("name")]
            if len(members) >= 2:
                hidden.update(members)
    result = [name for name in candidates if name not in hidden or name == current]
    if current and current not in result:
        result.append(current)
    return result


def mapping_selection_details(row: Mapping[str, Any], selected: Any = None) -> list[str]:
    """Show the selected backend evidence, not stale evidence after a manual edit."""
    current = str(selected if selected is not None else row.get("value") or row.get("detected") or "")
    details = row.get("candidate_details")
    info = details.get(current) if isinstance(details, Mapping) else None
    validated = str(row.get("value") or row.get("detected") or "")
    if not isinstance(info, Mapping) and current == validated:
        info = row.get("selected_details")
    if not isinstance(info, Mapping) or info.get("kind") not in {"transfer_family", "transfer_output"}:
        return ["新输入尚无可核对的传递来源；请应用并验证。"] if current and isinstance(details, Mapping) and current != validated else []
    lines = []
    if current != validated:
        lines.append("候选表达式说明；本次手动选择尚待应用验证。")
    selected_details = row.get("selected_details") if isinstance(row.get("selected_details"), Mapping) else {}
    consumption = info.get("role_consumption") or selected_details.get("role_consumption")
    if consumption:
        lines.append("实际使用：" + str(consumption))
    members = [item for item in info.get("members", []) if isinstance(item, Mapping)]
    if info.get("kind") == "transfer_family":
        lines.append("系列 %s：%d 个已发现成员；短名不是新增的 Maestro 输出。" % (info.get("family_name") or current, len(members)))
    else:
        lines.append("精确输出：%s；保留该单项选择，不自动改成系列。" % current)
    lines.append("按所选输出定位原始来源与成员；电路对应关系由用户确认。")
    lines.append("原始电流源：%s；分析：%s；读取代表项：%s" % (info.get("source") or "未确认", info.get("analysis") or "未确认", info.get("anchor_output") or "未确认"))
    sidebands = info.get("sidebands", [])
    if sidebands:
        lines.append("表达式边带：%s；算法使用边带 = k − 1，不能按名称后缀直接当作 k。" % ", ".join(str(value) for value in sidebands))
    for member in members:
        lines.append("%s → 边带 %s；源 %s；分析 %s\n  %s" % (member.get("name") or "未命名", member.get("sideband", "未确认"), member.get("source") or "未确认", member.get("analysis") or "未确认", member.get("expression") or "表达式未提供"))
    lines.extend("读取提示：" + str(error) for error in info.get("errors", []) if error)
    lines.extend("系列提示（不阻止运行）：" + str(note) for note in info.get("notes", []) if note)
    if info.get("missing_sidebands"):
        lines.append("缺少边带：" + ", ".join(str(value) for value in info["missing_sidebands"]))
    return lines


def mapping_source_text(row: Mapping[str, Any]) -> str:
    """Explain provenance independently from a friendly signal name."""
    presence = {"named_output": "Maestro 命名输出", "script_derived": "脚本按器件端口求差",
                "unresolved": "Maestro 缺失 / 尚未对应"}.get(str(row.get("maestro_presence") or ""), "")
    if row.get("maestro_presence") == "script_derived" and row.get("candidate_kind") == "variable":
        presence = "脚本生成波形文件变量（Maestro 未声明）"
    details = row.get("selected_details") if isinstance(row.get("selected_details"), Mapping) else {}
    scope = str(details.get("scope") or row.get("scope") or row.get("variable_scope") or "")
    scope_text = {"global": "全局变量", "test": "Test 局部变量（需改为全局）"}.get(scope, scope)
    if scope == "test" and (details.get("shadows_global") or "global_expression" in details):
        scope_text = "全局已存在 / Test 保留同名值"
    derivation = row.get("effective_derivation")
    if isinstance(derivation, Mapping):
        derivation = derivation.get("expression") or derivation.get("description") or ""
    return "；".join(str(item) for item in (presence, scope_text, derivation, row.get("source")) if item)


def waveform_exchange_view(report: Mapping[str, Any] | None) -> dict[str, Any]:
    """Present only backend-proven exchange evidence; absence is not success."""
    exchange = _nested(report or {}, "waveform_exchange", default={})
    if not isinstance(exchange, Mapping) or not exchange:
        return {"summary": "波形传递尚未检查；识别并绑定后显示 VCO → 噪声 TB 的路径与来源。", "rows": []}
    channels = exchange.get("channels", exchange.get("rows", []))
    if isinstance(channels, Mapping):
        channels = [{"semantic": key, **dict(value)} for key, value in channels.items() if isinstance(value, Mapping)]
    rows = []
    def readable(value: Any) -> str:
        if isinstance(value, Mapping):
            return str(value.get("expression") or value.get("name") or value.get("variable") or value.get("description") or json.dumps(dict(value), ensure_ascii=False))
        return str(value or "未提供")
    for item in channels if isinstance(channels, list) else []:
        if isinstance(item, Mapping):
            rows.append({"semantic": readable(item.get("semantic") or item.get("quantity")),
                         "source": " · ".join(str(value) for value in (item.get("source_output"), item.get("source_expression"), item.get("source_kind")) if value) or readable(item.get("source") or item.get("vco_source")),
                         "destination": " → ".join(str(value) for value in (item.get("pwl_variable") or item.get("destination_variable"), item.get("destination_source")) if value) or readable(item.get("destination")),
                         "path_policy": readable(item.get("relative_file") or item.get("path_policy") or exchange.get("path_policy")),
                         "status": readable(item.get("status")) + (" · " + str(item["detail"]) if item.get("detail") else "")})
    return {"summary": str(exchange.get("summary") or exchange.get("message") or
                           ("波形传递静态检查通过；实际运行时仍需验证生成文件和仿真结果。" if exchange.get("ok") else "波形传递检查未通过，请处理以下缺项。")),
            "rows": rows, "path_policy": str(exchange.get("path_policy") or ""),
            "checks": [str(item.get("detail") or "") for item in exchange.get("checks", []) if isinstance(item, Mapping) and item.get("status") not in {"pass", "ok"}]}


def result_sample_choices(result: Mapping[str, Any], operating_point_id: str) -> list[dict[str, Any]]:
    """Link controls to explicit result identities, never parse encoded names."""
    source = enrich_result(result)
    rows = [row for row in source.get("summary", []) if str(row.get("operating_point_id") or row.get("point") or "") == str(operating_point_id)]
    rows.sort(key=lambda row: finite_number(row.get("offset_Hz")) or math.inf)
    return [{"point": str(row.get("point") or ""), "offset_hz": finite_number(row.get("offset_Hz")),
             "label": f"{float(row['offset_Hz']):.12g} Hz" if finite_number(row.get("offset_Hz")) is not None else "频偏未记录"} for row in rows]


def mapping_review_rows(report: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize mapping evidence without inventing defaults or role pairing."""

    rows = []
    roles = _nested(report or {}, "roles", default={})
    for role, report_role in (("vco", "vco"), ("replica", "flicker")):
        fields = _nested(roles, report_role, "mapping_fields", default=[])
        for field in fields if isinstance(fields, list) else []:
            if isinstance(field, Mapping) and field.get("key"):
                row = copy.deepcopy(dict(field))
                row["role"] = role
                if role == "replica" and str(row["key"]).endswith("_pwl_variable"):
                    channels = _nested(report or {}, "waveform_exchange", "rows", default=[])
                    for channel in channels if isinstance(channels, list) else []:
                        if isinstance(channel, Mapping) and str(channel.get("semantic") or "").lower() + "_pwl_variable" == row["key"]:
                            row["effective_value"] = channel.get("pwl_variable")
                issues = _nested(roles, report_role, "mapping_issues", default=[])
                row["issues"] = [str(issue.get("message") or "") for issue in issues if isinstance(issue, Mapping) and issue.get("key") == row["key"]]
                rows.append(row)
    return rows


def cross_tb_review_rows(report: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Pair by voltage meaning, never by output/variable display order."""

    report = report or {}
    cross = report.get("cross_tb_mapping", _nested(report, "pair", "cross_tb_mapping", default=[]))
    resolved = {
        str(item.get("semantic") or "").lower(): item
        for item in (cross if isinstance(cross, list) else [])
        if isinstance(item, Mapping)
    }
    fields = {(row["role"], row["key"]): row for row in mapping_review_rows(report)}
    rows = []
    for voltage, label in (("vgs", "栅源电压 VGS"), ("vds", "漏源电压 VDS"), ("vbs", "体源电压 VBS")):
        if voltage in resolved:
            mapping = resolved[voltage]
            source_kind = str(mapping.get("vco_source") or "unresolved")
            status = str(mapping.get("status") or "unresolved")
            rows.append({
                "key": voltage, "label": label,
                "vco": mapping.get("vco_value") or "",
                "replica": mapping.get("replica_value") or "",
                "expression": mapping.get("vco_expression") or "",
                "source": {"pin_derived": "原始节点计算", "named_waveform": "已命名周期波形", "reference_only": "表达式仅供对照", "unresolved": "未识别"}.get(source_kind, source_kind),
                "status": {"mapped": "已对应", "derived": "按节点对应", "reference": "仅对照 / 不可计算", "invalid": "需修正", "unresolved": "未完成"}.get(status, status),
                "help": str(mapping.get("help") or ""),
            })
            continue
        vco = fields.get(("vco", f"{voltage}_output"), {})
        replica = fields.get(("replica", f"{voltage}_variable"), {})
        required = bool(vco.get("required") or replica.get("required"))
        uncertain = any(str(field.get("status")) in {"ambiguous", "invalid", "error", "missing", "pending"} for field in (vco, replica))
        rows.append({
            "key": voltage, "label": label,
            "vco": vco.get("value") or "", "replica": replica.get("value") or "",
            "status": "需确认" if uncertain else ("已对应" if vco.get("value") and replica.get("value") else ("待补充" if required else "可选 / 未识别")),
            "source": "命名输出 / 变量", "expression": "", "help": "点击重新识别可查看原始节点计算或表达式对照状态。",
        })
    return rows


def _history_tests(history: Mapping[str, Any]) -> set[str]:
    values = history.get("tests", [])
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return set()
    names: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            value = value.get("name")
        if value not in (None, ""):
            names.add(str(value))
    return names


def history_is_complete(history: Mapping[str, Any] | None, test_name: str | None = None) -> bool:
    """Apply the same conservative complete-history rule as the Web UI."""

    if not isinstance(history, Mapping) or history.get("error"):
        return False
    if test_name and test_name not in _history_tests(history):
        return False
    if history.get("fully_complete") is True:
        return True
    complete = finite_number(history.get("complete_runs"))
    checked = finite_number(history.get("checked_runs"))
    netlists = history.get("source_netlists")
    return bool(
        complete is not None
        and checked is not None
        and checked > 0
        and complete == checked
        and isinstance(netlists, Sequence)
        and not isinstance(netlists, (str, bytes))
        and len(netlists) == int(complete)
    )


def select_history_candidate(
    histories: Sequence[Mapping[str, Any]] | None,
    requested: str | None = None,
    test_name: str | None = None,
    *, explicit: bool = False,
) -> dict[str, Any]:
    """Select an explicit history, or auto-select only one complete history."""

    all_histories = [copy.deepcopy(dict(item)) for item in histories or () if isinstance(item, Mapping)]
    complete = [item for item in all_histories if history_is_complete(item, test_name)]
    requested_text = str(requested or "")
    selected = None
    if requested_text:
        for item in complete:
            if requested_text in {str(item.get("path") or ""), str(item.get("name") or "")}:
                selected = item
                break
    if selected is None and not requested_text and len(complete) == 1 and not explicit:
        selected = complete[0]
    return {
        "selected": selected,
        "history_count": len(all_histories),
        "complete_count": len(complete),
        "requires_manual": selected is None and len(complete) > 1,
        "complete_histories": complete,
    }


def maestro_pair_payload(config: Mapping[str, Any], include_fingerprints: bool = False) -> dict[str, Any]:
    """Build the direct ``ToolService.inspect_maestro_pair`` payload."""

    raw = _unwrap_mapping(config, "config")
    flat = flatten_config(config) if "testbenches" in config or "config" in config else dict(config)
    vco = {
        "path": str(flat.get("vco_path") or ""),
        "maestro_dir": str(flat.get("vco_path") or ""),
        "test": str(flat.get("vco_test") or ""),
        "history": str(flat.get("vco_history") or ""),
    }
    replica = {
        "path": str(flat.get("replica_path") or ""),
        "maestro_dir": str(flat.get("replica_path") or ""),
        "test": str(flat.get("replica_test") or ""),
        "history": str(flat.get("replica_history") or ""),
    }
    if flat.get("replica_source"):
        replica["source"] = str(flat["replica_source"])
    for name, spec in (("vco", vco), ("replica", replica)):
        original = raw.get("testbenches", {}).get(name, {})
        for key in ("history_catalog", "explicit_history_selection"):
            if key in original:
                spec[key] = copy.deepcopy(original[key])
    if include_fingerprints:
        vco["binding_fingerprint"] = str(flat.get("vco_binding_fingerprint") or "")
        vco["history_fingerprint"] = str(flat.get("vco_history_fingerprint") or "")
        replica["binding_fingerprint"] = str(flat.get("replica_binding_fingerprint") or "")
        replica["history_fingerprint"] = str(flat.get("replica_history_fingerprint") or "")
    vco["mapping_overrides"] = clean_mapping_overrides(flat.get("vco_mapping_overrides"))
    replica["mapping_overrides"] = clean_mapping_overrides(flat.get("replica_mapping_overrides"))
    offset_hz = finite_number(flat.get("offset_hz"))
    payload = {
        "testbenches": {"vco": vco, "replica": replica},
        "roots": [str(flat["project_root"])] if flat.get("project_root") else [],
        "offset_hz": offset_hz,
        "max_depth": 6,
        "max_results": 100,
        "include_histories": True,
        "portable_setup": {"mode": "auto", "overrides": clean_mapping_overrides(flat.get("portable_overrides"))},
        "config_path": str(flat.get("engine_config") or ""),
    }
    for key in ("profiles", "analysis"):
        if isinstance(raw.get(key), Mapping):
            payload[key] = copy.deepcopy(dict(raw[key]))
    return payload


def apply_maestro_context(config: Mapping[str, Any], context: Mapping[str, Any], *, clear_noise: bool = False) -> dict[str, Any]:
    """Apply an invoking-window selection without accepting stale bindings."""
    result = invalidate_role(config, "vco")
    catalog = copy.deepcopy(list(context.get("histories") or []))
    selected = str(context.get("selected_history") or "")
    matches = [row for row in catalog if selected and selected in {str(row.get("name") or ""), str(row.get("rdb_file") or "")}]
    history = str(matches[0].get("rdb_file") or "") if len(matches) == 1 else ""
    spec = result.setdefault("testbenches", {}).setdefault("vco", {})
    spec.update(path=context["maestro_dir"], maestro_dir=context["maestro_dir"],
                test="", history=history, history_catalog=catalog, explicit_history_selection=True)
    result["project_root"] = str(context.get("work_dir") or "")
    if clear_noise:
        result["testbenches"]["replica"] = {"name": "replica", "role": "flicker", "source": "generated"}
    if isinstance(result.get("execution"), dict):
        result["execution"]["allow_execute"] = False
    return result


def binding_ready(config: Mapping[str, Any]) -> bool:
    flat = flatten_config(config) if "testbenches" in config or "config" in config else config
    roles = ("vco",) if flat.get("replica_source") == "generated" else ("vco", "replica")
    required = (f"{role}_{field}" for role in roles for field in ("path", "history", "binding_fingerprint", "history_fingerprint"))
    return all(str(flat.get(key) or "") for key in required)


def _wrapped_degrees(value: float) -> float:
    wrapped = (value + 180.0) % 360.0
    return wrapped - 360.0 if wrapped >= 180.0 else wrapped


def _row_value(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in row:
            number = finite_number(row[key])
            if number is not None:
                return number
    return None


def _owned_signoff(source: Mapping[str, Any], job_id: str) -> bool:
    provenance = source.get("provenance")
    dual_tb = source.get("dual_tb")
    if not isinstance(provenance, Mapping) or not isinstance(dual_tb, Mapping):
        return False
    return bool(
        str(source.get("run_kind") or "").lower() == "signoff"
        and dual_tb.get("signoff") is True
        and str(provenance.get("job_id") or "")
        == str(job_id or source.get("job_id") or source.get("id") or "")
        and provenance.get("manifest_sha256")
        and (finite_number(provenance.get("psf_runs")) or 0) > 0
    )


def _owned_offset_hz(value: Any) -> float | None:
    """Read an offset only from a result-owned snapshot/evidence mapping."""

    if not isinstance(value, Mapping):
        return None
    mappings = [value]
    wrapped = value.get("config")
    if isinstance(wrapped, Mapping):
        mappings.append(wrapped)
    for item in mappings:
        for candidate in (
            _first(item, "offset_Hz", "offset_hz"),
            _nested(item, "metrics", "offset_Hz"),
            _nested(item, "metrics", "offset_hz"),
            _nested(item, "analysis", "offset", "hz"),
            _nested(item, "analysis", "offset", "offset_hz"),
        ):
            number = finite_number(candidate)
            if number is not None:
                return number
        offset = _nested(item, "analysis", "offset")
        if isinstance(offset, Mapping):
            number = finite_number(offset.get("value"))
            unit = str(offset.get("unit") or "")
            if number is not None and unit in _UNIT_SCALE:
                return number * _UNIT_SCALE[unit]
    return None


def _explicit_offset_hz(
    source: Mapping[str, Any],
    summary: Mapping[str, Any],
    _config: Mapping[str, Any] | None,
) -> float | None:
    for candidate in (
        _first(summary, "offset_Hz", "offset_hz"),
        _nested(source, "metrics", "offset_Hz"),
        _nested(source, "metrics", "offset_hz"),
        _first(source, "offset_Hz", "offset_hz"),
        _nested(source, "dual_tb", "metadata", "offset_Hz"),
        _nested(source, "dual_tb", "metadata", "offset_hz"),
    ):
        value = finite_number(candidate)
        if value is not None:
            return value
    for key in ("job_snapshot", "config_snapshot", "evidence", "provenance"):
        value = _owned_offset_hz(source.get(key))
        if value is not None:
            return value
    # ``config`` is retained in the public function contract for callers that
    # use it for other rendering context.  It is the mutable current workspace
    # config, not result-owned evidence, so it must never supply this metric.
    return None


def _summary_view(row: Mapping[str, Any], noise_scope: str = "single", noise_kind: str = "flicker") -> dict[str, Any]:
    row = enrich_summary(row)
    calculated = calculated_noise_dbc(row, noise_kind=noise_kind, scope=noise_scope)
    reference = row.get("spectre_total_dBc_Hz")
    return {
        "point": str(row.get("point") or N_A),
        "f0_hz": _number_or_na(row.get("f0_Hz")),
        "operating_point_id": str(row.get("operating_point_id") or row.get("point") or N_A),
        "corner": str(row.get("corner") or N_A),
        "offset_hz": _number_or_na(row.get("offset_Hz")),
        "calculated_dbc": _number_or_na(calculated_noise_dbc(row, noise_kind=noise_kind, scope=noise_scope)),
        "spectre_dbc": _number_or_na(row.get("spectre_total_dBc_Hz")),
        "dynamic_error_db": _number_or_na(row.get("dynamic_error_dB")),
        "calculated_vs_total_db": calculated - reference if calculated is not None and reference is not None else N_A,
        "pair_error_db": _number_or_na(row.get("pair_error_dB")),
        "predicted_pair_asd": _number_or_na(
            _first(row, "predicted_pair_pm_asd_V_rtHz", "predicted_pair_fn_pm_asd_V_rtHz")
        ),
        "reference_pair_asd": _number_or_na(
            _first(row, "spectre_pair_pm_asd_V_rtHz", "spectre_pair_fn_pm_asd_V_rtHz")
        ),
    }


def result_quality_text(summary: Mapping[str, Any]) -> str:
    """Explain recorded quality gates without equating a close curve with truth."""
    labels = (("numerical_quality_pass", "数值质量"),
              ("hbac_secondary_validation_pass", "HBAC 相位辅助检查"),
              ("pm_magnitude_projection_valid", "PM 投影前提"))
    fields = enrich_summary(summary)
    statuses = [label + "：" + ("通过" if fields.get(key) is True else
                               "未通过" if fields.get(key) is False else "未提供")
                for key, label in labels]
    failed = any(fields.get(key) is False for key, _label in labels)
    text = ("质量检查未通过；运行完成不代表验证通过。" if failed else
            "辅助检查不等于独立验证通过；曲线接近不能单独证明计算可信。")
    text += " " + "；".join(statuses) + "。"
    if summary.get("numerical_quality_issues"):
        text += " 数值告警：" + str(summary["numerical_quality_issues"]) + "。"
    if fields.get("hbac_secondary_validation_pass") is False:
        delta = finite_number(summary.get("hbac_max_phase_delta_deg"))
        threshold = finite_number(summary.get("hbac_phase_warn_threshold_deg"))
        if delta is not None and threshold is not None:
            text += f" HBAC 最大相位差 {delta:.3g}°，门限 {threshold:.3g}°。"
    thermal = fields.get("thermal_noise") or {}
    if thermal.get("status") == "unavailable":
        text += " Thermal 未完成：" + str(thermal.get("reason") or "未采集") + "。"
    elif thermal.get("status") == "available":
        text += " Thermal：已验证 PM/SSB 口径；gm MOS 周期白噪声近似 + LC 无源源谱，其他有源器件及 shot noise 未纳入计算。"
        quality_record = thermal.get("numerical_quality")
        thermal_quality = quality_record.get("pass") if isinstance(quality_record, Mapping) else None
        text += " Thermal 仿真数值质量：" + ("通过" if thermal_quality is True else
                                           "未通过" if thermal_quality is False else "未提供") + "。"
    return text


def _harmonic_view(row: Mapping[str, Any], index: int, reference_asd: float | None) -> dict[str, Any]:
    ref_mag = _row_value(row, "Mdc_mag_A_rtHz", "mdc_mag")
    dyn_mag = _row_value(row, "Mdyn_mag_A_rtHz", "mdyn_mag")
    ref_phase = _row_value(row, "Mdc_phase_deg", "mdc_phase_deg")
    dyn_phase = _row_value(row, "Mdyn_phase_deg", "mdyn_phase_deg")
    amplitude = _row_value(row, "A_k", "amplitude")
    if amplitude is None and ref_mag is not None and ref_mag > 0 and dyn_mag is not None:
        amplitude = dyn_mag / ref_mag
    delta = _row_value(row, "delta_k_deg", "delta_deg")
    if delta is None and ref_phase is not None and dyn_phase is not None:
        delta = _wrapped_degrees(dyn_phase - ref_phase)
    ck_real = _row_value(row, "Ck_real", "ck_real")
    ck_imag = _row_value(row, "Ck_imag", "ck_imag")
    if amplitude is not None and delta is not None:
        radians = math.radians(delta)
        if ck_real is None:
            ck_real = amplitude * math.cos(radians)
        if ck_imag is None:
            ck_imag = amplitude * math.sin(radians)
    q_real = _row_value(row, "Q_real_V_rtHz", "Qk_real_V_rtHz", "Qk_real", "q_real")
    q_imag = _row_value(row, "Q_imag_V_rtHz", "Qk_imag_V_rtHz", "Qk_imag", "q_imag")
    q_mag = _row_value(row, "Q_mag_V_rtHz", "Qk_mag_V_rtHz", "q_mag")
    q_phase = _row_value(row, "Q_phase_deg", "Qk_phase_deg", "q_phase")
    if q_real is not None and q_imag is not None:
        if q_mag is None:
            q_mag = math.hypot(q_real, q_imag)
        if q_phase is None:
            q_phase = math.degrees(math.atan2(q_imag, q_real))
    cumulative_real = _row_value(row, "cum_real_V_rtHz", "cum_real")
    cumulative_imag = _row_value(row, "cum_imag_V_rtHz", "cum_imag")
    cumulative_error = None
    if cumulative_real is not None and cumulative_imag is not None and reference_asd and reference_asd > 0:
        magnitude = math.hypot(cumulative_real, cumulative_imag)
        if magnitude > 0:
            cumulative_error = 20.0 * math.log10(magnitude / reference_asd)
    k = finite_number(row.get("k"))
    return {
        "k": int(k) if k is not None and k.is_integer() else (k if k is not None else index),
        "mdc_mag": ref_mag if ref_mag is not None else N_A,
        "mdyn_mag": dyn_mag if dyn_mag is not None else N_A,
        "amplitude": amplitude if amplitude is not None else N_A,
        "delta_deg": delta if delta is not None else N_A,
        "ck_real": ck_real if ck_real is not None else N_A,
        "ck_imag": ck_imag if ck_imag is not None else N_A,
        "q_real": q_real if q_real is not None else N_A,
        "q_imag": q_imag if q_imag is not None else N_A,
        "q_mag": q_mag if q_mag is not None else N_A,
        "q_phase_deg": q_phase if q_phase is not None else N_A,
        "cumulative_real": cumulative_real if cumulative_real is not None else N_A,
        "cumulative_imag": cumulative_imag if cumulative_imag is not None else N_A,
        "cumulative_error_db": cumulative_error if cumulative_error is not None else N_A,
        "harmonic_dbc": _number_or_na(row.get("harmonic_equivalent_dBc_Hz")),
        "cumulative_dbc": _number_or_na(row.get("cumulative_dBc_Hz")),
    }


def _phase_series(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    descriptors = (
        ("ISF-I", ("isf_i", "isf", "ISF", "gamma"), False),
        ("ISF-Q", ("isf_q",), True),
    )
    output: list[dict[str, Any]] = []
    count = len(rows)
    for name, keys, dashed in descriptors:
        points: list[tuple[float, float] | None] = []
        has_value = False
        for index, row in enumerate(rows):
            y = _row_value(row, *keys)
            if y is None:
                # ``charts.line_segments`` treats this sentinel as a hard
                # break, so missing samples cannot be visually bridged.
                points.append(None)
                continue
            wt = finite_number(row.get("wt_rad"))
            x = wt / (2.0 * math.pi) if wt is not None else (index / (count - 1) if count > 1 else 0.0)
            points.append((x, y))
            has_value = True
        if has_value:
            output.append({"name": name, "dash": dashed, "points": points})
    return output


def build_result_view(
    raw_result: Mapping[str, Any] | None,
    job_id: str = "",
    point: str | None = None,
    config: Mapping[str, Any] | None = None,
    noise_scope: str = "single",
    noise_kind: str = "flicker",
) -> dict[str, Any]:
    """Map a canonical core result into table/ISF/Qk presentation data."""

    if noise_scope not in {"single", "pair"}:
        raise ValueError("noise scope must be single or pair")
    if noise_kind not in {"flicker", "thermal", "both"}:
        raise ValueError("invalid noise kind")
    source = _unwrap_mapping(raw_result, "result")
    if "data" in source and isinstance(source["data"], Mapping):
        source = copy.deepcopy(dict(source["data"]))
    source = enrich_result(source)
    raw_summary = source.get("summary", [])
    summary = (
        [dict(row) for row in raw_summary if isinstance(row, Mapping)]
        if isinstance(raw_summary, Sequence)
        and not isinstance(raw_summary, (str, bytes))
        else []
    )
    raw_points = source.get("points", {})
    points = dict(raw_points) if isinstance(raw_points, Mapping) else {}
    point_names = sorted(str(name) for name in points)
    if point is None:
        selected = point_names[0] if point_names else (
            str(summary[0].get("point") or "") if summary else ""
        )
    else:
        selected = str(point) if str(point) in points else ""
    selected_summary = next(
        (row for row in summary if str(row.get("point") or "") == selected),
        summary[0] if point is None and summary else {},
    )
    point_data = points.get(selected, {}) if selected else {}
    if not isinstance(point_data, Mapping):
        point_data = {}
    workpoint_raw = point_data.get("workpoint", [])
    harmonic_raw = point_data.get("harmonics", [])
    workpoint = (
        [dict(row) for row in workpoint_raw if isinstance(row, Mapping)]
        if isinstance(workpoint_raw, Sequence)
        and not isinstance(workpoint_raw, (str, bytes))
        else []
    )
    harmonics = (
        [dict(row) for row in harmonic_raw if isinstance(row, Mapping)]
        if isinstance(harmonic_raw, Sequence)
        and not isinstance(harmonic_raw, (str, bytes))
        else []
    )

    resolved_job_id = str(job_id or source.get("job_id") or source.get("id") or "")
    signoff = _owned_signoff(source, resolved_job_id)
    source_kind = str(source.get("run_kind") or "").lower()
    run_kind = "signoff" if signoff else ("plan" if source_kind == "plan" else "replay")
    h0 = next((row for row in harmonics if finite_number(row.get("k")) == 0), {})
    final = harmonics[-1] if harmonics else {}
    f0 = _row_value(selected_summary, "f0_Hz", "f0_hz")
    mdc = _row_value(selected_summary, "mDC_cycle_mean_A_rtHz")
    if mdc is None:
        mdc = _row_value(h0, "Mdc_mag_A_rtHz", "mdc_mag")
    mdyn = _row_value(selected_summary, "mDyn_rms_A_rtHz")
    if mdyn is None:
        mdyn = _row_value(h0, "Mdyn_mag_A_rtHz", "mdyn_mag")
    qsum_real = _row_value(final, "cum_real_V_rtHz", "cum_real")
    qsum_imag = _row_value(final, "cum_imag_V_rtHz", "cum_imag")
    reference_asd = _row_value(selected_summary, "spectre_fn_pm_asd_V_rtHz")
    harmonic_rows = [_harmonic_view(row, index, reference_asd) for index, row in enumerate(harmonics)]
    if noise_scope == "pair":
        for displayed, raw in zip(harmonic_rows, harmonics):
            displayed["harmonic_dbc"] = _number_or_na(raw.get("pair_harmonic_equivalent_dBc_Hz"))
            displayed["cumulative_dbc"] = _number_or_na(raw.get("pair_cumulative_dBc_Hz"))
            displayed["cumulative_error_db"] = _number_or_na(raw.get("pair_cumulative_error_dB"))
    qk_points = [
        {"k": row["k"], "real": row["cumulative_real"], "imag": row["cumulative_imag"]}
        for row in harmonic_rows
        if row["cumulative_real"] != N_A and row["cumulative_imag"] != N_A
    ]
    dual_tb = source.get("dual_tb") if isinstance(source.get("dual_tb"), Mapping) else {}
    metadata = dual_tb.get("metadata") if isinstance(dual_tb.get("metadata"), Mapping) else source.get("metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = {}
    offset_hz = _explicit_offset_hz(source, selected_summary, config)
    return {
        "job_id": resolved_job_id,
        "point": selected or N_A,
        "point_names": point_names,
        "signoff": signoff,
        "run_kind": run_kind,
        "metadata": copy.deepcopy(dict(metadata)),
        "summary_rows": [_summary_view(row, noise_scope, noise_kind) for row in summary],
        "selected_summary": copy.deepcopy(dict(selected_summary)),
        "noise_contribution_rows": noise_contribution_rows(selected_summary, scope=noise_scope, noise_kind=noise_kind),
        "reference_note": ("VCO 总仿真相噪未采集：旧结果显示 N/A，不以器件 flicker 代替。"
                           if finite_number(selected_summary.get("spectre_total_dBc_Hz")) is None else ""),
        "contribution_note": ("未采集各器件 / 噪声机制明细；旧结果不能补出未保存的贡献。"
                              if not isinstance(selected_summary.get("spectre_noise_contributions"), list)
                              or not selected_summary.get("spectre_noise_contributions") else ""),
        "quality_text": result_quality_text(selected_summary),
        "metrics": {
            "calculated_dbc": _number_or_na(calculated_noise_dbc(selected_summary, noise_kind=noise_kind, scope=noise_scope)),
            "spectre_dbc": _number_or_na(selected_summary.get("spectre_total_dBc_Hz")),
            "frequency_hz": f0 if f0 is not None else N_A,
            "period_s": (1.0 / f0) if f0 is not None and f0 > 0 else N_A,
            "offset_hz": offset_hz if offset_hz is not None else N_A,
            "mdc": mdc if mdc is not None else N_A,
            "mdyn": mdyn if mdyn is not None else N_A,
            "qsum_real": qsum_real if qsum_real is not None else N_A,
            "qsum_imag": qsum_imag if qsum_imag is not None else N_A,
            "dynamic_error_db": _number_or_na(selected_summary.get("dynamic_error_dB")),
            "pair_error_db": _number_or_na(selected_summary.get("pair_error_dB")),
        },
        "harmonic_rows": harmonic_rows,
        "isf_series": _phase_series(workpoint),
        "qk_points": qk_points,
    }


__all__ = [
    "N_A",
    "binding_ready",
    "build_result_view",
    "deep_merge",
    "finite_number",
    "flatten_config",
    "history_is_complete",
    "invalidate_role",
    "maestro_pair_payload",
    "merge_config",
    "select_history_candidate",
]
