"""Read-only discovery of Maestro, PSF, and RDB result directories."""

from __future__ import unicode_literals

import os
import re
import sqlite3
import datetime
import math

try:
    from urllib.parse import quote
except ImportError:  # pragma: no cover
    from urllib import quote

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover
    from collections import Mapping

from .errors import DiscoveryError, ValidationError
from .models import DiscoveryRecord


_PSF_DIR_NAMES = frozenset(("psf", "psfbin", "psfascii"))
_RDB_DIR_NAMES = frozenset(("rdb", "results.rdb"))


def _bounded_integer(value, name, minimum, maximum):
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


def _normalize_payload(payload):
    if isinstance(payload, (str, bytes)):
        return {"roots": [payload]}
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise ValidationError("discovery payload must be an object")
    return dict(payload)


def _normalize_roots(payload):
    roots = payload.get("roots")
    if roots is None:
        roots = payload.get("root", payload.get("path", []))
    if isinstance(roots, (str, bytes)):
        roots = [roots]
    if roots is None:
        roots = []
    if not isinstance(roots, (list, tuple)):
        raise ValidationError("roots must be a path or a list of paths")

    normalized = []
    seen = set()
    for raw_root in roots:
        if not isinstance(raw_root, (str, bytes)):
            raise ValidationError("each discovery root must be a path string")
        root = os.path.realpath(os.path.abspath(os.path.expanduser(raw_root)))
        if root not in seen:
            normalized.append(root)
            seen.add(root)
    return normalized


def _classify_directory(path, directory_names, file_names):
    base = os.path.basename(path).lower()
    dirs = set(name.lower() for name in directory_names)
    files = set(name.lower() for name in file_names)
    records = []

    if base in _RDB_DIR_NAMES or base.endswith(".rdb"):
        records.append(("rdb", ["directory-name:%s" % base]))

    psf_markers = []
    if base in _PSF_DIR_NAMES or base.startswith("psf."):
        psf_markers.append("directory-name:%s" % base)
    if "logfile" in files and (
        "spectre.out" in files
        or "runobjfile" in files
        or any(name.endswith((".tran", ".ac", ".noise", ".pss")) for name in files)
    ):
        psf_markers.append("psf-file-markers")
    if psf_markers:
        records.append(("psf", psf_markers))

    maestro_markers = []
    if "maestro" in base:
        maestro_markers.append("directory-name:%s" % base)
    if "maestro.log" in files or "maestro.sdb" in files:
        maestro_markers.append("maestro-file-markers")
    if "results" in dirs and ("maestro" in dirs or "history" in dirs):
        maestro_markers.append("maestro-directory-markers")
    if maestro_markers:
        records.append(("maestro", maestro_markers))

    return records


def _is_sqlite_file(path):
    try:
        with open(path, "rb") as stream:
            return stream.read(16) == b"SQLite format 3\x00"
    except (IOError, OSError):
        return False


def discover_result_directories(payload):
    """Discover result directories without opening or modifying their contents.

    ``payload`` accepts ``roots``, ``max_depth``, ``max_results``,
    ``include_hidden``, ``follow_symlinks``, and ``strict``.  Missing roots are
    warnings unless ``strict`` is true.
    """

    options = _normalize_payload(payload)
    roots = _normalize_roots(options)
    max_depth = _bounded_integer(options.get("max_depth", 8), "max_depth", 0, 64)
    max_results = _bounded_integer(
        options.get("max_results", 2000), "max_results", 1, 100000
    )
    include_hidden = _boolean(options.get("include_hidden", False), "include_hidden")
    follow_symlinks = _boolean(
        options.get("follow_symlinks", False), "follow_symlinks"
    )
    strict = _boolean(options.get("strict", False), "strict")

    findings = []
    warnings = []
    seen = set()
    truncated = False

    for root in roots:
        if not os.path.isdir(root):
            message = "discovery root is not a readable directory: %s" % root
            if strict:
                raise DiscoveryError(message, details={"root": root})
            warnings.append(message)
            continue

        walk_errors = []

        def on_walk_error(exc):
            walk_errors.append(str(exc))

        for current, dirs, files in os.walk(
            root, topdown=True, onerror=on_walk_error, followlinks=follow_symlinks
        ):
            relative = os.path.relpath(current, root)
            depth = 0 if relative == "." else len(relative.split(os.sep))

            dirs.sort()
            files.sort()
            if not include_hidden:
                dirs[:] = [name for name in dirs if not name.startswith(".")]
                files = [name for name in files if not name.startswith(".")]
            if depth >= max_depth:
                dirs[:] = []

            for kind, markers in _classify_directory(current, dirs, files):
                canonical = os.path.realpath(current)
                key = (kind, canonical)
                if key in seen:
                    continue
                findings.append(
                    DiscoveryRecord(kind, canonical, root, depth, markers).to_dict()
                )
                seen.add(key)
                if len(findings) >= max_results:
                    truncated = True
                    dirs[:] = []
                    break
            if truncated:
                break

            if depth < max_depth:
                for filename in files:
                    if not filename.lower().endswith(".rdb"):
                        continue
                    rdb_path = os.path.join(current, filename)
                    if not os.path.isfile(rdb_path) or not _is_sqlite_file(rdb_path):
                        continue
                    canonical = os.path.realpath(rdb_path)
                    key = ("rdb", canonical)
                    if key in seen:
                        continue
                    findings.append(
                        DiscoveryRecord(
                            "rdb",
                            canonical,
                            root,
                            depth + 1,
                            ["file-extension:.rdb", "sqlite-header"],
                        ).to_dict()
                    )
                    seen.add(key)
                    if len(findings) >= max_results:
                        truncated = True
                        dirs[:] = []
                        break
            if truncated:
                break

        warnings.extend(walk_errors)
        if truncated:
            break

    findings.sort(key=lambda item: (item["kind"], item["path"]))
    counts = {"maestro": 0, "psf": 0, "rdb": 0}
    for finding in findings:
        counts[finding["kind"]] += 1

    return {
        "roots": roots,
        "findings": findings,
        "counts": counts,
        "warnings": warnings,
        "truncated": truncated,
    }


# Short alias convenient for non-service callers.
discover = discover_result_directories


def _rdb_payload(payload):
    if isinstance(payload, (str, bytes)):
        return {"path": payload}
    if not isinstance(payload, Mapping):
        raise ValidationError("RDB inspection payload must be an object or path")
    return dict(payload)


def _row_value(row, *names):
    lowered = dict((str(key).lower(), value) for key, value in row.items())
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _json_sqlite_value(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _file_status(path):
    exists = os.path.isfile(path)
    status = {"path": path, "exists": exists, "is_file": exists}
    if exists:
        try:
            stat_result = os.stat(path)
            status["size"] = stat_result.st_size
            status["mtime"] = stat_result.st_mtime
        except OSError:
            status["exists"] = False
            status["is_file"] = False
    return status


_SPECTRE_COMPLETION_RE = re.compile(
    r"\bspectre\s+complet(?:e|es|ed)\s+with\s+0\s+errors?\b", re.IGNORECASE
)
_ZERO_ERROR_RE = re.compile(r"\b(?:0\s+errors?|no\s+errors?)\b", re.IGNORECASE)
_ERROR_RECORD_RE = re.compile(
    r"\bfatal\b|^\s*error\b|\berror\s*\([A-Z0-9_-]+\)|"
    r"\bspectre\b.*\b[1-9][0-9]*\s+errors?\b",
    re.IGNORECASE,
)
_SIMULATION_TIMES_RE = re.compile(
    r"Simulation started at:\s*(.*?),\s*ended at:\s*(.*?),\s*with elapsed time",
    re.IGNORECASE,
)


def _spectre_terminal_status(path, max_bytes):
    status = _file_status(path)
    status.update(
        {
            "zero_errors": False,
            "terminal_summary": None,
            "fatal_or_error": False,
            "error_records": [],
            "tail_bytes": 0,
        }
    )
    if not status["is_file"]:
        return status

    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            start = max(0, size - max_bytes)
            stream.seek(start, os.SEEK_SET)
            data = stream.read(max_bytes)
    except (IOError, OSError) as exc:
        status["read_error"] = str(exc)
        return status

    status["tail_bytes"] = len(data)
    text = data.decode("utf-8", "replace")
    completion_lines = []
    error_records = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _SPECTRE_COMPLETION_RE.search(line):
            completion_lines.append(line)
            continue
        if _ERROR_RECORD_RE.search(line) and not _ZERO_ERROR_RE.search(line):
            error_records.append(line)

    status["terminal_summary"] = completion_lines[-1] if completion_lines else None
    status["error_records"] = error_records[-20:]
    status["fatal_or_error"] = bool(error_records)
    status["zero_errors"] = bool(completion_lines) and not error_records
    intervals = list(_SIMULATION_TIMES_RE.finditer(text))
    if intervals:
        status["simulation_times"] = dict(zip(("start", "stop"), intervals[-1].groups()))
    return status


def _history_run_root(rdb_path, declared):
    """Resolve only Cadence's exact same-parent history rename convention."""
    history_name = os.path.splitext(os.path.basename(rdb_path))[0]
    candidate = os.path.join(os.path.dirname(declared), history_name)
    if os.path.abspath(candidate) == os.path.abspath(declared):
        return declared, None
    if not os.path.lexists(candidate):
        return declared, None
    if os.path.lexists(declared):
        raise DiscoveryError("RDB rawDir and the same-name history directory also exists, refusing to guess run ownership",
                             details={"declared_raw_dir": declared, "named_history_dir": candidate})
    parent = os.path.realpath(os.path.dirname(declared))
    effective = os.path.realpath(candidate)
    if (history_name in ("", ".", "..") or os.path.islink(candidate)
            or not os.path.isdir(candidate) or effective == parent
            or os.path.commonpath((parent, effective)) != parent):
        raise DiscoveryError("named history directory is invalid or outside the original rawDir parent directory",
                             details={"declared_raw_dir": declared, "named_history_dir": candidate})
    return effective, {"method": "exact_rdb_stem_same_parent", "rdb": rdb_path,
                       "history_name": history_name, "declared_raw_dir": declared,
                       "effective_raw_dir": effective}


def _renamed_run_evidence(root, psf_dir, status, terminal):
    """A renamed result still needs its exact point/test tree and run interval."""
    case = os.path.dirname(psf_dir)
    netlist = os.path.join(case, "netlist", "input.scs")
    paths = [os.path.dirname(case), case, psf_dir, os.path.dirname(netlist), netlist,
             os.path.join(psf_dir, ".simDone"), os.path.join(psf_dir, "spectre.out")]
    evidence = {"netlist": netlist, "netlist_present": os.path.isfile(netlist),
                "paths_inside_history": all(not os.path.islink(path) and
                    os.path.commonpath((root, os.path.realpath(path))) == root for path in paths)}
    valid = evidence["netlist_present"] and evidence["paths_inside_history"]
    expected = [_row_value(status, key) for key in ("startTime", "stopTime")]
    if all(value not in (None, "") for value in expected):
        interval = terminal.get("simulation_times") or {}
        try:
            start, stop = (float(value) for value in expected)
            actual_start, actual_stop = (datetime.datetime.strptime(interval[key],
                "%I:%M:%S %p, %a %b %d, %Y").timestamp() for key in ("start", "stop"))
            matches = (all(math.isfinite(value) for value in (start, stop)) and start <= stop
                       and start - 2 <= actual_start <= actual_stop <= stop + 2)
            evidence["run_interval"] = {"checked": True, "matches": matches,
                "rdb_start": start, "rdb_stop": stop, "spectre_start": actual_start,
                "spectre_stop": actual_stop, "tolerance_seconds": 2}
            valid = valid and matches
        except (KeyError, TypeError, ValueError, OverflowError):
            evidence["run_interval"] = {"checked": True, "matches": False,
                                         "reason": "Cannot prove RDB and Spectre log run times correspond"}
            valid = False
    elif any(value not in (None, "") for value in expected):
        evidence["run_interval"] = {"checked": True, "matches": False,
                                     "reason": "RDB provides only one endpoint of the run interval, cannot be accepted as a legacy format without timestamps"}
        valid = False
    else:
        evidence["run_interval"] = {"checked": False, "reason": "RDB no absolute run times provided"}
    return bool(valid), evidence


def inspect_rdb(payload):
    """Inspect a Maestro RDB through a read-only SQLite connection.

    The returned ``runs`` list verifies the exact
    ``rawDir/<pointID>/<test>/psf/{.simDone,spectre.out}`` paths.  No fallback
    to a nominal ``rawDir/psf`` directory is used for completion status. A
    missing rawDir may resolve to its exact renamed-history sibling, with
    additional point/test/netlist/log evidence; the RDB is never modified.
    """

    options = _rdb_payload(payload)
    raw_path = options.get("path", options.get("rdb"))
    if raw_path is None:
        raise ValidationError("RDB inspection requires path")
    if isinstance(raw_path, bytes):
        raw_path = os.fsdecode(raw_path)
    if not isinstance(raw_path, str):
        raise ValidationError("RDB path must be a path string")
    path = os.path.realpath(os.path.abspath(os.path.expanduser(raw_path)))
    max_records = _bounded_integer(
        options.get("max_records", 100000), "max_records", 1, 1000000
    )
    verify_completion = _boolean(
        options.get("verify_completion", True), "verify_completion"
    )
    spectre_tail_bytes = _bounded_integer(
        options.get("spectre_tail_bytes", 256 * 1024),
        "spectre_tail_bytes",
        1024,
        4 * 1024 * 1024,
    )

    if not os.path.isfile(path):
        raise DiscoveryError("RDB file does not exist: %s" % path)
    if not _is_sqlite_file(path):
        raise DiscoveryError("RDB is not a SQLite 3 file: %s" % path)

    uri = "file:%s?mode=ro" % quote(path, safe="/")
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise DiscoveryError("cannot open RDB read-only: %s" % exc)

    truncated_tables = []
    warnings = []
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        table_rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [str(row[0]) for row in table_rows]
        by_lower_name = dict((name.lower(), name) for name in table_names)

        def read_table(logical_name):
            actual_name = by_lower_name.get(logical_name.lower())
            if actual_name is None:
                warnings.append("RDB table is absent: %s" % logical_name)
                return []
            quoted_name = '"%s"' % actual_name.replace('"', '""')
            rows = connection.execute(
                "SELECT * FROM %s LIMIT ?" % quoted_name, (max_records + 1,)
            ).fetchall()
            if len(rows) > max_records:
                truncated_tables.append(actual_name)
                rows = rows[:max_records]
            return [
                dict((str(key), _json_sqlite_value(row[key])) for key in row.keys())
                for row in rows
            ]

        points = read_table("point")
        tests = read_table("test")
        corners = read_table("corner")
        psf_rows = read_table("psfTable")
        test_statuses = read_table("testStatus")
    except sqlite3.Error as exc:
        raise DiscoveryError("cannot query RDB: %s" % exc)
    finally:
        connection.close()

    corner_by_id = dict(
        (_row_value(row, "cornerID"), _row_value(row, "name")) for row in corners
    )
    for point in points:
        corner_id = _row_value(point, "cornerID")
        point.setdefault("corner", corner_by_id.get(corner_id))

    point_by_id = dict((_row_value(row, "pointID"), row) for row in points)
    test_by_id = dict((_row_value(row, "testID"), row) for row in tests)

    expected_pairs = []
    if test_statuses:
        for status in test_statuses:
            point_id = _row_value(status, "pointID")
            test_id = _row_value(status, "testID")
            expected_pairs.append(
                (point_by_id.get(point_id, {"pointID": point_id}),
                 test_by_id.get(test_id, {"testID": test_id}), status)
            )
    else:
        for point in points:
            for test in tests:
                if len(expected_pairs) >= max_records:
                    truncated_tables.append("point_x_test")
                    break
                expected_pairs.append((point, test, {}))
            if len(expected_pairs) >= max_records:
                break

    raw_directories = []
    for row in psf_rows:
        raw_dir = _row_value(row, "rawDir")
        if raw_dir is not None:
            raw_directories.append(str(raw_dir))

    runs = []
    resolutions = []
    if verify_completion:
        for psf_row in psf_rows:
            raw_dir_value = _row_value(psf_row, "rawDir")
            if raw_dir_value in (None, ""):
                continue
            raw_dir = os.path.expanduser(str(raw_dir_value))
            if not os.path.isabs(raw_dir):
                raw_dir = os.path.abspath(os.path.join(os.path.dirname(path), raw_dir))
            declared_raw_dir = raw_dir
            raw_dir, resolution = _history_run_root(path, raw_dir)
            if resolution and resolution not in resolutions:
                resolutions.append(resolution)
            condition = _row_value(psf_row, "condition")
            results_dir = _row_value(psf_row, "resultsDir")

            for point, test, status in expected_pairs:
                point_id = _row_value(point, "pointID")
                test_id = _row_value(test, "testID")
                test_name = _row_value(test, "name")
                safe_test = (
                    test_name is not None
                    and str(test_name) not in ("", ".", "..")
                    and "/" not in str(test_name)
                    and "\\" not in str(test_name)
                )
                safe_point = (
                    point_id is not None
                    and str(point_id) not in ("", ".", "..")
                    and "/" not in str(point_id)
                    and "\\" not in str(point_id)
                )
                if safe_test and safe_point:
                    psf_dir = os.path.join(raw_dir, str(point_id), str(test_name), "psf")
                    sim_done = _file_status(os.path.join(psf_dir, ".simDone"))
                    spectre_out = _spectre_terminal_status(
                        os.path.join(psf_dir, "spectre.out"), spectre_tail_bytes
                    )
                    complete = sim_done["is_file"] and spectre_out["zero_errors"]
                    rename_evidence = None
                    if resolution:
                        verified, rename_evidence = _renamed_run_evidence(raw_dir, psf_dir, status, spectre_out)
                        complete = complete and verified
                else:
                    psf_dir = None
                    sim_done = {"path": None, "exists": False, "is_file": False}
                    spectre_out = {
                        "path": None,
                        "exists": False,
                        "is_file": False,
                        "zero_errors": False,
                        "terminal_summary": None,
                        "fatal_or_error": False,
                        "error_records": [],
                        "tail_bytes": 0,
                    }
                    complete = False
                    rename_evidence = None

                corner_id = _row_value(point, "cornerID")
                runs.append(
                    {
                        "condition": condition,
                        "rawDir": raw_dir,
                        "declaredRawDir": declared_raw_dir,
                        "rawDirResolution": dict(resolution, run_evidence=rename_evidence) if resolution else None,
                        "resultsDir": results_dir,
                        "pointID": point_id,
                        "designPointNumber": _row_value(point, "designPointNumber"),
                        "cornerID": corner_id,
                        "corner": corner_by_id.get(corner_id),
                        "testID": test_id,
                        "test": test_name,
                        "statusCode": _row_value(status, "statusCode"),
                        "psfDir": psf_dir,
                        "simDone": sim_done,
                        "spectreOut": spectre_out,
                        "zero_errors": spectre_out["zero_errors"],
                        "complete": complete,
                    }
                )

    complete_count = sum(1 for run in runs if run["complete"])
    return {
        "path": path,
        "tables": table_names,
        "points": points,
        "tests": tests,
        "corners": corners,
        "rawDir": raw_directories,
        "effectiveRawDir": sorted(set(run["rawDir"] for run in runs)),
        "rawDirResolutions": resolutions,
        "psfTable": psf_rows,
        "runs": runs,
        "completion": {
            "checked": len(runs),
            "complete": complete_count,
            "incomplete": len(runs) - complete_count,
        },
        "warnings": warnings,
        "truncated_tables": sorted(set(truncated_tables)),
        "read_only": True,
    }
