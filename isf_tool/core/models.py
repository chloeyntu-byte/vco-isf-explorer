"""Small standard-library data models shared by all core entry points."""

from __future__ import unicode_literals

import copy
import datetime
import json
import math

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover - Python 3.6 compatibility path
    from collections import Mapping

from .errors import ValidationError


POINT_TABLES = ("workpoint", "harmonics", "phase_candidates", "summary")


def utc_now():
    """Return a compact UTC timestamp that is valid JSON text."""

    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def validate_json_compatible(value, label="value"):
    """Reject values that a strict JSON web response cannot represent."""

    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "%s is not strict-JSON compatible: %s" % (label, exc),
            details={"label": label},
        )
    return value


def empty_point_result():
    return {name: [] for name in POINT_TABLES}


def empty_result():
    """Create the stable result envelope consumed by the web layer."""

    return {"summary": [], "points": {}}


def _copy_rows(rows, label):
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValidationError("%s must be a list" % label)
    copied = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValidationError("%s[%d] must be an object" % (label, index))
        copied.append(copy.deepcopy(dict(row)))
    return copied


def normalize_result(value=None):
    """Normalize any result mapping to the required summary/points contract."""

    if value is None:
        return empty_result()
    if not isinstance(value, Mapping):
        raise ValidationError("result must be an object")

    normalized = {}
    for key, item in value.items():
        if key not in ("summary", "points"):
            normalized[str(key)] = copy.deepcopy(item)

    normalized["summary"] = _copy_rows(value.get("summary", []), "summary")
    raw_points = value.get("points", {})
    if not isinstance(raw_points, Mapping):
        raise ValidationError("points must be an object")

    points = {}
    for raw_name, raw_tables in raw_points.items():
        point_name = str(raw_name)
        if not point_name:
            raise ValidationError("point names must not be empty")
        if not isinstance(raw_tables, Mapping):
            raise ValidationError("point %s must be an object" % point_name)
        tables = empty_point_result()
        for table in POINT_TABLES:
            tables[table] = _copy_rows(
                raw_tables.get(table, []),
                "points.%s.%s" % (point_name, table),
            )
        points[point_name] = tables
    normalized["points"] = points
    validate_json_compatible(normalized, "result")
    return normalized


def merge_results(results):
    """Merge imported result envelopes without dropping required empty arrays."""

    merged = empty_result()
    for raw_result in results:
        result = normalize_result(raw_result)
        merged["summary"].extend(result["summary"])
        for point_name, tables in result["points"].items():
            target = merged["points"].setdefault(point_name, empty_point_result())
            for table in POINT_TABLES:
                target[table].extend(tables[table])
    return normalize_result(merged)


def annotate_result(value, **fields):
    """Add provenance fields to every imported row if they are not present."""

    result = normalize_result(value)
    collections = [result["summary"]]
    for tables in result["points"].values():
        collections.extend(tables[name] for name in POINT_TABLES)
    for rows in collections:
        for row in rows:
            for key, item in fields.items():
                row.setdefault(key, item)
    return result


class StageEvent(object):
    """Immutable-enough stage/progress record suitable for JSON serialization."""

    def __init__(
        self,
        sequence,
        job_id,
        stage,
        status,
        progress,
        message="",
        details=None,
        timestamp=None,
    ):
        self.sequence = int(sequence)
        self.job_id = str(job_id)
        self.stage = str(stage)
        self.status = str(status)
        self.progress = float(progress)
        self.message = str(message or "")
        self.details = copy.deepcopy(dict(details or {}))
        self.timestamp = timestamp or utc_now()

    def to_dict(self):
        value = {
            "sequence": self.sequence,
            "job_id": self.job_id,
            "stage": self.stage,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "details": copy.deepcopy(self.details),
            "timestamp": self.timestamp,
        }
        validate_json_compatible(value, "stage event")
        return value


class DiscoveryRecord(object):
    def __init__(self, kind, path, root, depth, markers=None):
        self.kind = str(kind)
        self.path = str(path)
        self.root = str(root)
        self.depth = int(depth)
        self.markers = sorted(set(markers or []))

    def to_dict(self):
        return {
            "kind": self.kind,
            "path": self.path,
            "root": self.root,
            "depth": self.depth,
            "markers": list(self.markers),
        }


class SubmissionResult(object):
    def __init__(
        self,
        adapter,
        status,
        command,
        dry_run,
        returncode=None,
        stdout="",
        stderr="",
        cwd=None,
        started_at=None,
        finished_at=None,
        metadata=None,
    ):
        self.adapter = str(adapter)
        self.status = str(status)
        self.command = list(command)
        self.dry_run = bool(dry_run)
        self.returncode = returncode
        self.stdout = str(stdout or "")
        self.stderr = str(stderr or "")
        self.cwd = None if cwd in (None, "") else str(cwd)
        self.started_at = started_at or utc_now()
        self.finished_at = finished_at or self.started_at
        self.metadata = copy.deepcopy(dict(metadata or {}))

    def to_dict(self):
        value = {
            "adapter": self.adapter,
            "status": self.status,
            "command": list(self.command),
            "dry_run": self.dry_run,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metadata": copy.deepcopy(self.metadata),
        }
        validate_json_compatible(value, "submission result")
        return value
