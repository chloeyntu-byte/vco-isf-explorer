"""Non-executable defaults for one exact Maestro source across menu sessions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
from typing import Any, Mapping


FIELDS = (
    "offset_mode", "offset_value", "offset_unit", "offset_values", "offset_start",
    "offset_stop", "offset_spacing", "offset_step", "offset_intervals",
    "offset_points_per_decade", "max_harmonic", "phase_samples", "period_count",
)
MAX_BYTES = 64 * 1024


def preference_path(directory: Path, context: Mapping[str, Any]) -> Path:
    identity = tuple(str(context[key]) for key in ("work_dir", "maestro_dir", "library", "cell", "view"))
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()
    return directory / f"{digest}.json"


def mapping_key(role: Mapping[str, Any]) -> str:
    fingerprints = (role.get("binding_fingerprint"), role.get("history_fingerprint"))
    if all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) for value in fingerprints):
        return "/".join(fingerprints)
    return ""


def _private(info: os.stat_result, kind: Any) -> bool:
    return kind(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077


def _payload(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = value.get("fields", {})
    bindings = value.get("bindings", {})
    if not isinstance(fields, dict) or not isinstance(bindings, dict):
        raise ValueError("invalid Maestro preference fields")
    return {
        "fields": {
            key: fields[key] for key in FIELDS if key in fields
            and type(fields[key]) in (str, int, float) and len(str(fields[key])) <= 8192
        },
        "theme": "dark" if value.get("theme") == "dark" else "light",
        "bindings": {
            key: {field: data for field, data in overrides.items()
                  if isinstance(field, str) and isinstance(data, str) and len(field) <= 128 and len(data) <= 2048}
            for key, overrides in list(bindings.items())[-8:]
            if isinstance(key, str) and re.fullmatch(r"[a-f0-9]{64}/[a-f0-9]{64}", key) and isinstance(overrides, dict)
        },
    }


def read_preferences(path: Path) -> dict[str, Any]:
    try:
        if not _private(path.parent.lstat(), stat.S_ISDIR):
            raise ValueError("Maestro preference directory is not private")
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    with os.fdopen(descriptor, "rb") as stream:
        if not _private(os.fstat(stream.fileno()), stat.S_ISREG):
            raise ValueError("Maestro preference file is not private")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Maestro preference file exceeds its size limit")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("revision"), str):
        raise ValueError("unsupported Maestro preference schema")
    return {"revision": value["revision"], **_payload(value)}


def write_preferences(path: Path, value: Mapping[str, Any], expected_revision: str | None) -> dict[str, Any]:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    if not _private(path.parent.lstat(), stat.S_ISDIR):
        raise ValueError("Maestro preference directory is not private")
    descriptor = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(descriptor, "r+b") as lock:
        if not _private(os.fstat(lock.fileno()), stat.S_ISREG):
            raise ValueError("Maestro preference lock is not private")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if read_preferences(path).get("revision") != expected_revision:
            raise ValueError("本项目偏好已被另一窗口更新，请重新打开以读取最新设置。")
        result = {"schema": 1, "revision": uuid.uuid4().hex, **_payload(value)}
        encoded = (json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
        if len(encoded) > MAX_BYTES:
            raise ValueError("Maestro preference file exceeds its size limit")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.stem}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return result
