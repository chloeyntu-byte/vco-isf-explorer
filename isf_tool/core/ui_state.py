"""Named, non-executable UI snapshots under one workspace-owned directory."""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import re
import stat
import uuid


MAX_STATE_BYTES = 4 * 1024 * 1024
VIEW_FIELDS = frozenset(("job_id", "point", "operating_point_id", "offset_hz",
    "noise_kind", "x_scale", "cumulative_k", "theme", "selected_tab",
    "harmonic_curve", "log_offset", "advanced_visible"))
_AUTHORITY = frozenset(("allowexecute", "executionauthorized", "executionconsent"))
_CONFIRMATIONS = frozenset(("bindingfingerprint", "historyfingerprint", "mainfingerprint",
    "mainhistoryfingerprint", "replicafingerprint", "replicahistoryfingerprint"))
_RETIRED_NOISE_KEYS = frozenset(("additionalnoiseobjects", "additionalnoiseobjectsreport",
    "additionalnoiseobjectconfig"))


# Compatibility only: display labels stored by releases before the English UI.
LEGACY_HARMONIC_CURVES = {
    "不显示累计曲线": "none",
    "全谐波相干总和": "coherent_total",
    "flicker 全谐波相干总和 · 对称 gm pair": "coherent_total",
    "flicker 全谐波相干总和 · 单器件": "coherent_total",
}
LEGACY_CUMULATIVE_LABEL = r"^累计至 k=(\d+) · 相干总和$"


def harmonic_curve_key(value):
    """Migrate old display labels without changing unknown extension values."""
    if not isinstance(value, str):
        return value
    if value in LEGACY_HARMONIC_CURVES:
        return LEGACY_HARMONIC_CURVES[value]
    match = re.fullmatch(LEGACY_CUMULATIVE_LABEL, value)
    return "cumulative_" + match.group(1) if match else value


def strip_retired_noise_objects(value):
    """Drop removed object-extension controls without mutating saved inputs."""
    if isinstance(value, dict):
        return {key: strip_retired_noise_objects(item) for key, item in value.items()
                if not isinstance(key, str) or key.replace("_", "").lower() not in _RETIRED_NOISE_KEYS}
    if isinstance(value, list):
        return [strip_retired_noise_objects(item) for item in value]
    return value


def _safe_data(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("state object keys must be strings")
            normalized = key.replace("_", "").lower()
            if normalized in _AUTHORITY:
                result[key] = False
            elif normalized in _CONFIRMATIONS:
                result[key] = ""
            else:
                result[key] = _safe_data(item)
        return result
    if isinstance(value, list):
        return [_safe_data(item) for item in value]
    return value


def validate_state(value):
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported UI state schema")
    config, view = value.get("config"), value.get("view", {})
    if not isinstance(config, dict) or not isinstance(view, dict):
        raise ValueError("state config/view must be objects")
    if set(view) - VIEW_FIELDS or any(isinstance(item, (dict, list)) for item in view.values()):
        raise ValueError("unsupported UI state display fields")
    name = value.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError("state name must contain 1 to 120 characters")
    saved_at = value.get("saved_at")
    if not isinstance(saved_at, str) or len(saved_at) > 64:
        raise ValueError("state timestamp is missing or invalid")
    clean = _safe_data({"schema_version": 1, "name": name.strip(),
                        "saved_at": saved_at, "config": strip_retired_noise_objects(config), "view": view})
    if "harmonic_curve" in clean["view"]:
        clean["view"]["harmonic_curve"] = harmonic_curve_key(clean["view"]["harmonic_curve"])
    encoded = json.dumps(clean, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise ValueError("UI state exceeds 4 MiB")
    return clean


def state_directory(workspace, *, create=False):
    if not workspace:
        raise ValueError("UI states require a workspace")
    root = Path(workspace).resolve()
    directory = root / "states"
    if directory.is_symlink() or directory.resolve() != directory:
        raise ValueError("state directory must not redirect outside the workspace")
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def state_path(workspace, state_id):
    if not isinstance(state_id, str) or not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-f0-9]{12}\.json", state_id):
        raise ValueError("invalid UI state id")
    path = state_directory(workspace) / state_id
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("UI state cannot be a symbolic link")
    return path


def load_state(workspace, state_id):
    path = state_path(workspace, state_id)
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("UI state must be a regular file")
        data = stream.read(MAX_STATE_BYTES + 1)
    if len(data) > MAX_STATE_BYTES:
        raise ValueError("UI state exceeds 4 MiB")
    state = validate_state(json.loads(data.decode("utf-8")))
    return {"state": state, "path": str(path), "id": state_id}


def save_state(workspace, payload):
    if not isinstance(payload, dict) or set(payload) - {"name", "config", "view"}:
        raise ValueError("UI state payload must be an object")
    now = datetime.datetime.now(datetime.timezone.utc)
    state = validate_state({"schema_version": 1, "name": payload.get("name") or now.strftime("State %Y-%m-%d %H:%M:%S"),
        "saved_at": now.isoformat(), "config": payload.get("config"), "view": payload.get("view", {})})
    directory = state_directory(workspace, create=True)
    state_id = now.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:12] + ".json"
    path = directory / state_id
    encoded = (json.dumps(state, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise ValueError("UI state exceeds 4 MiB")
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {"id": state_id, "name": state["name"], "saved_at": state["saved_at"],
            "path": str(path), "state": state}


def list_states(workspace):
    directory = state_directory(workspace)
    rows, warnings = [], []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json"), reverse=True):
            try:
                loaded = load_state(workspace, path.name)
                rows.append({"id": path.name, "name": loaded["state"]["name"], "saved_at": loaded["state"]["saved_at"]})
            except (OSError, ValueError, UnicodeError) as exc:
                warnings.append({"id": path.name, "reason": str(exc)})
    return {"states": rows, "directory": str(directory), "warnings": warnings}
