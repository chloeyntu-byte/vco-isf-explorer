"""CSV bundle importer for portable, strict-JSON ISF results."""

from __future__ import unicode_literals

import csv
import io
import json
import math
import os
import re
import zipfile

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover
    from collections import Mapping

from .errors import BundleImportError, ValidationError
from .models import empty_point_result, empty_result, normalize_result


_INTEGER_RE = re.compile(r"^[+-]?(?:0|[1-9][0-9]*)$")
_FLOAT_RE = re.compile(
    r"^[+-]?(?:(?:[0-9]+\.[0-9]*)|(?:[0-9]*\.[0-9]+)|(?:[0-9]+[eE][+-]?[0-9]+)|(?:[0-9]+\.[0-9]*[eE][+-]?[0-9]+)|(?:[0-9]*\.[0-9]+[eE][+-]?[0-9]+))$"
)
_GENERIC_POINT_PARENTS = frozenset(
    ("", ".", "bundle", "csv", "data", "output", "outputs", "result", "results")
)
_DUAL_TB_FILENAMES = frozenset(("vco_results.csv", "replica_results.csv"))


def _path_string(value):
    try:
        value = os.fspath(value)
    except (AttributeError, TypeError):
        pass
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, str):
        raise ValidationError("bundle path must be a path string")
    return os.path.realpath(os.path.abspath(os.path.expanduser(value)))


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool):
        raise ValidationError("%s must be an integer" % name)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValidationError("%s must be an integer" % name)
    if number < minimum or number > maximum:
        raise ValidationError(
            "%s must be between %d and %d" % (name, minimum, maximum)
        )
    return number


def _boolean(value, name):
    if isinstance(value, bool):
        return value
    raise ValidationError("%s must be a boolean" % name)


def _payload_options(payload):
    if isinstance(payload, (str, bytes)) or hasattr(payload, "__fspath__"):
        return {"path": payload}
    if not isinstance(payload, Mapping):
        raise ValidationError("bundle payload must be an object or path")
    return dict(payload)


def _logical_table(logical_path):
    filename = logical_path.replace("\\", "/").rsplit("/", 1)[-1]
    stem = filename[:-4] if filename.lower().endswith(".csv") else filename
    token = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
    if "phase_candidate" in token:
        return "phase_candidates"
    if token.startswith("harmonic") or "_harmonic" in token:
        return "harmonics"
    if (
        token.startswith("workpoint")
        or "_workpoint" in token
        or token.startswith("operating_point")
        or "_operating_point" in token
    ):
        return "workpoint"
    if token == "summary" or token.endswith("_summary") or token.startswith("summary_"):
        return "summary"
    return None


def _is_reserved_dual_tb_path(logical_path):
    parts = logical_path.replace("\\", "/").strip("/").split("/")
    lowered = [part.lower() for part in parts]
    return (
        bool(lowered)
        and (lowered[-1] in _DUAL_TB_FILENAMES or "canonical" in lowered[:-1])
    )


def _is_aggregate_table_file(logical_path):
    filename = logical_path.replace("\\", "/").rsplit("/", 1)[-1]
    stem = filename[:-4] if filename.lower().endswith(".csv") else filename
    token = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
    return token.endswith("_all")


def _select_closure_sources(logical_paths, warnings):
    """Prefer per-point detail files over duplicated ``*_all.csv`` tables."""

    candidates = []
    per_point_tables = set()
    for logical_path in logical_paths:
        if _is_reserved_dual_tb_path(logical_path):
            warnings.append("ignored reserved dual-TB CSV: %s" % logical_path)
            continue
        table = _logical_table(logical_path)
        candidates.append((logical_path, table))
        if (
            table is not None
            and not _is_aggregate_table_file(logical_path)
            and _infer_point(logical_path, table) is not None
        ):
            per_point_tables.add(table)

    selected = set()
    for logical_path, table in candidates:
        if (
            table in per_point_tables
            and _is_aggregate_table_file(logical_path)
        ):
            warnings.append(
                "ignored aggregate %s because per-point files are present: %s"
                % (table, logical_path)
            )
            continue
        selected.add(logical_path)
    return selected


def _infer_point(logical_path, table):
    normalized = logical_path.replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part]
    lower_parts = [part.lower() for part in parts]
    if "points" in lower_parts:
        index = lower_parts.index("points")
        if index + 1 < len(parts) - 1:
            return parts[index + 1]

    # A closure bundle's immediate parent is authoritative.  Parsing the
    # filename first turns ``k_.../device_workpoint.csv`` into point "device".
    if len(parts) >= 2:
        parent = parts[-2]
        if parent.lower() not in _GENERIC_POINT_PARENTS and table != "summary":
            return parent

    filename = parts[-1] if parts else normalized
    stem = filename[:-4] if filename.lower().endswith(".csv") else filename
    table_suffixes = {
        "workpoint": ("workpoint", "workpoints", "operating_point"),
        "harmonics": ("harmonics", "harmonics_all", "harmonic"),
        "phase_candidates": ("phase_candidates", "phase_candidates_all"),
        "summary": ("summary",),
    }[table]
    lower_stem = stem.lower()
    for suffix in table_suffixes:
        for separator in ("__", "--", "_"):
            marker = separator + suffix
            if lower_stem.endswith(marker) and len(stem) > len(marker):
                return stem[: -len(marker)]

    return None


def _coerce_value(value):
    if value is None:
        return None
    stripped = value.strip()
    lowered = stripped.lower()
    if stripped == "" or lowered in ("null", "none"):
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if _INTEGER_RE.match(stripped):
        try:
            return int(stripped)
        except ValueError:  # pragma: no cover
            pass
    if _FLOAT_RE.match(stripped):
        try:
            number = float(stripped)
            if math.isfinite(number):
                return number
        except ValueError:  # pragma: no cover
            pass
    return stripped


def _read_rows(stream, logical_path, delimiter, coerce_types, budget, max_rows):
    reader = csv.DictReader(stream, delimiter=delimiter)
    raw_headers = reader.fieldnames
    if raw_headers is None:
        return []
    headers = [(header or "").strip() for header in raw_headers]
    if not headers or any(not header for header in headers):
        raise BundleImportError(
            "CSV has an empty header: %s" % logical_path,
            details={"file": logical_path},
        )
    if len(set(headers)) != len(headers):
        raise BundleImportError(
            "CSV has duplicate headers: %s" % logical_path,
            details={"file": logical_path},
        )
    reader.fieldnames = headers

    rows = []
    for raw_row in reader:
        extras = raw_row.pop(None, None)
        if extras and any(str(item).strip() for item in extras if item is not None):
            raise BundleImportError(
                "CSV row has more fields than headers: %s line %d"
                % (logical_path, reader.line_num),
                details={"file": logical_path, "line": reader.line_num},
            )
        if not any(value not in (None, "") for value in raw_row.values()):
            continue
        budget["rows"] += 1
        if budget["rows"] > max_rows:
            raise BundleImportError(
                "CSV bundle exceeds max_rows",
                details={"max_rows": max_rows},
            )
        row = {}
        for header in headers:
            value = raw_row.get(header)
            row[header] = _coerce_value(value) if coerce_types else value
        rows.append(row)
    return rows


def _point_from_row(row, point_columns):
    for column in point_columns:
        value = row.get(column)
        if value not in (None, ""):
            return str(value)
    return None


def _add_table_rows(result, table, rows, logical_path, point_columns, default_point):
    inferred_point = _infer_point(logical_path, table) or default_point
    if table == "summary":
        result["summary"].extend(dict(row) for row in rows)

    for row in rows:
        point_name = _point_from_row(row, point_columns) or inferred_point
        if point_name is None:
            if table == "summary":
                continue
            point_name = "default"
        point = result["points"].setdefault(str(point_name), empty_point_result())
        point[table].append(dict(row))


def _safe_zip_name(name):
    normalized = name.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if normalized.startswith("/") or any(part == ".." for part in parts):
        raise BundleImportError(
            "unsafe zip member path: %s" % name,
            details={"member": name},
        )
    return "/".join(parts)


def _deduplicate_result_rows(result):
    removed = 0

    def deduplicate(rows):
        nonlocal_removed = [0]
        seen = set()
        unique = []
        for row in rows:
            key = json.dumps(
                row, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
            )
            if key in seen:
                nonlocal_removed[0] += 1
                continue
            seen.add(key)
            unique.append(row)
        return unique, nonlocal_removed[0]

    result["summary"], count = deduplicate(result["summary"])
    removed += count
    for tables in result["points"].values():
        for table in ("workpoint", "harmonics", "phase_candidates", "summary"):
            tables[table], count = deduplicate(tables[table])
            removed += count
    return removed


def import_csv_bundle(payload):
    """Import a directory, one CSV, or a ZIP of CSV files.

    Recognized tables are ``summary``, ``workpoint``, ``harmonics``, and
    ``phase_candidates``.  A ``point`` or ``point_id`` column groups rows into
    the stable per-point result envelope.
    """

    options = _payload_options(payload)
    if "path" not in options:
        raise ValidationError("bundle payload requires path")
    source_path = _path_string(options["path"])
    encoding = options.get("encoding", "utf-8-sig")
    if not isinstance(encoding, str) or not encoding:
        raise ValidationError("encoding must be a non-empty string")
    delimiter = options.get("delimiter", ",")
    if not isinstance(delimiter, str) or len(delimiter) != 1:
        raise ValidationError("delimiter must be one character")
    coerce_types = _boolean(options.get("coerce_types", True), "coerce_types")
    follow_symlinks = _boolean(
        options.get("follow_symlinks", False), "follow_symlinks"
    )
    max_files = _integer(options.get("max_files", 1000), "max_files", 1, 100000)
    max_file_bytes = _integer(
        options.get("max_file_bytes", 32 * 1024 * 1024),
        "max_file_bytes",
        1,
        1024 * 1024 * 1024,
    )
    max_total_bytes = _integer(
        options.get("max_total_bytes", 128 * 1024 * 1024),
        "max_total_bytes",
        1,
        4 * 1024 * 1024 * 1024,
    )
    max_rows = _integer(
        options.get("max_rows", 500000), "max_rows", 1, 10000000
    )
    point_columns = options.get("point_columns", ["point", "point_id"])
    if isinstance(point_columns, str):
        point_columns = [point_columns]
    if not isinstance(point_columns, (list, tuple)) or not point_columns:
        raise ValidationError("point_columns must be a non-empty list")
    if any(not isinstance(column, str) or not column for column in point_columns):
        raise ValidationError("point_columns entries must be non-empty strings")
    default_point = options.get("default_point")
    if default_point is not None:
        default_point = str(default_point)
        if not default_point:
            raise ValidationError("default_point must not be empty")

    if not os.path.exists(source_path):
        raise BundleImportError(
            "bundle path does not exist: %s" % source_path,
            details={"path": source_path},
        )

    result = empty_result()
    warnings = []
    table_counts = {
        "summary": 0,
        "workpoint": 0,
        "harmonics": 0,
        "phase_candidates": 0,
    }
    budget = {"rows": 0, "bytes": 0, "files": 0, "recognized_files": 0}

    def consume(stream, logical_path, size):
        if size > max_file_bytes:
            raise BundleImportError(
                "CSV file exceeds max_file_bytes: %s" % logical_path,
                details={"file": logical_path, "size": size},
            )
        budget["files"] += 1
        budget["bytes"] += size
        if budget["files"] > max_files:
            raise BundleImportError("CSV bundle exceeds max_files")
        if budget["bytes"] > max_total_bytes:
            raise BundleImportError("CSV bundle exceeds max_total_bytes")

        table = _logical_table(logical_path)
        if table is None:
            warnings.append("ignored unrecognized CSV: %s" % logical_path)
            return
        budget["recognized_files"] += 1
        try:
            rows = _read_rows(
                stream, logical_path, delimiter, coerce_types, budget, max_rows
            )
        except UnicodeError as exc:
            raise BundleImportError(
                "cannot decode CSV %s using %s: %s" % (logical_path, encoding, exc),
                details={"file": logical_path, "encoding": encoding},
            )
        _add_table_rows(
            result, table, rows, logical_path, point_columns, default_point
        )
        table_counts[table] += len(rows)

    if os.path.isdir(source_path):
        csv_paths = []
        for current, dirs, files in os.walk(
            source_path, topdown=True, followlinks=follow_symlinks
        ):
            dirs.sort()
            files.sort()
            if not follow_symlinks:
                dirs[:] = [
                    name
                    for name in dirs
                    if not os.path.islink(os.path.join(current, name))
                ]
            for filename in files:
                path = os.path.join(current, filename)
                if not filename.lower().endswith(".csv"):
                    continue
                if not follow_symlinks and os.path.islink(path):
                    warnings.append("ignored CSV symlink: %s" % path)
                    continue
                csv_paths.append(path)
        logical_by_path = dict(
            (path, os.path.relpath(path, source_path).replace(os.sep, "/"))
            for path in csv_paths
        )
        selected = _select_closure_sources(list(logical_by_path.values()), warnings)
        for path in csv_paths:
            logical_path = logical_by_path[path]
            if logical_path not in selected:
                continue
            size = os.path.getsize(path)
            with io.open(path, "r", encoding=encoding, newline="") as stream:
                consume(stream, logical_path, size)
    elif zipfile.is_zipfile(source_path):
        try:
            with zipfile.ZipFile(source_path, "r") as archive:
                members = sorted(archive.infolist(), key=lambda item: item.filename)
                logical_members = []
                for info in members:
                    if info.is_dir() or not info.filename.lower().endswith(".csv"):
                        continue
                    logical_members.append((info, _safe_zip_name(info.filename)))
                selected = _select_closure_sources(
                    [logical_path for _info, logical_path in logical_members], warnings
                )
                for info, logical_path in logical_members:
                    if logical_path not in selected:
                        continue
                    if info.flag_bits & 0x1:
                        raise BundleImportError(
                            "encrypted CSV members are not supported: %s" % logical_path
                        )
                    with archive.open(info, "r") as raw_stream:
                        with io.TextIOWrapper(
                            raw_stream, encoding=encoding, newline=""
                        ) as stream:
                            consume(stream, logical_path, info.file_size)
        except (zipfile.BadZipFile, RuntimeError) as exc:
            raise BundleImportError("cannot read zip bundle: %s" % exc)
    elif source_path.lower().endswith(".csv") and os.path.isfile(source_path):
        logical_path = os.path.basename(source_path)
        selected = _select_closure_sources([logical_path], warnings)
        if logical_path not in selected:
            raise BundleImportError(
                "canonical dual-TB CSV requires import_dual_tb_bundle",
                details={"path": source_path},
            )
        size = os.path.getsize(source_path)
        with io.open(source_path, "r", encoding=encoding, newline="") as stream:
            consume(stream, logical_path, size)
    else:
        raise BundleImportError(
            "bundle must be a directory, CSV file, or ZIP file: %s" % source_path
        )

    if budget["recognized_files"] <= 0:
        raise BundleImportError(
            "bundle contains no recognized result CSV files",
            details={"path": source_path},
        )

    duplicates_removed = _deduplicate_result_rows(result)
    table_counts = {
        "summary": len(result["summary"]),
        "workpoint": sum(len(point["workpoint"]) for point in result["points"].values()),
        "harmonics": sum(len(point["harmonics"]) for point in result["points"].values()),
        "phase_candidates": sum(
            len(point["phase_candidates"]) for point in result["points"].values()
        ),
    }

    result["bundle"] = {
        "path": source_path,
        "files": budget["files"],
        "bytes": budget["bytes"],
        "rows": budget["rows"],
        "table_rows": table_counts,
        "duplicates_removed": duplicates_removed,
        "warnings": warnings,
    }
    return normalize_result(result)


class CSVBundleImporter(object):
    """Object form for dependency injection into services/orchestrators."""

    def import_bundle(self, payload):
        return import_csv_bundle(payload)


# Concise alias for direct callers.
import_bundle = import_csv_bundle
