"""Read-only Maestro RDB and Cadence PSF extraction primitives.

This module deliberately stops at deterministic discovery and parsing.  It
does not run simulations, edit Maestro state, create completion markers, or
guess missing traces.  Higher-level VCO/replica extraction can build on these
small contracts once each testbench's trace names are configured explicitly.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from .discovery import inspect_rdb
from .errors import DiscoveryError, ValidationError


_NUMBER = r"[-+]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eEdD][-+]?\d+)?"
_AXIS_RE = re.compile(
    r'^\s*"(?P<label>time|freq|harmonic|relative frequency)"\s+'
    r'"?(?P<value>%s)"?\s*$' % _NUMBER
)


def _path(value, label):
    try:
        value = os.fspath(value)
    except (AttributeError, TypeError):
        pass
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, str) or not value:
        raise ValidationError("%s must be a non-empty path" % label)
    return os.path.realpath(os.path.abspath(os.path.expanduser(value)))


def _sqlite_value(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _row_value(row, *names):
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _id_token(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value)


def _is_global_test_scope(value):
    return value is None or str(value).strip() == ""


def _read_parameter_tables(rdb_path):
    """Read explicit sweep values using a SQLite read-only URI."""

    uri = "file:%s?mode=ro" % quote(rdb_path, safe="/")
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise DiscoveryError("cannot open RDB parameter tables read-only: %s" % exc)

    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        table_rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_by_lower = {str(row[0]).lower(): str(row[0]) for row in table_rows}
        parameter_table = table_by_lower.get("parameter")
        value_table = table_by_lower.get("parametervalue")
        if parameter_table is None and value_table is None:
            if 'model' in table_by_lower and connection.execute('SELECT 1 FROM model LIMIT 1').fetchone():
                raise DiscoveryError('RDB model-group overrides require separate per-corner netlists', details={'path': rdb_path})
            return {}, {}
        if parameter_table is None or value_table is None:
            raise DiscoveryError(
                "RDB has only one of parameter/parameterValue; parameter metadata is incomplete",
                details={"path": rdb_path},
            )

        def all_rows(table_name):
            quoted = '"%s"' % table_name.replace('"', '""')
            return [
                {str(key): _sqlite_value(row[key]) for key in row.keys()}
                for row in connection.execute("SELECT * FROM %s" % quoted).fetchall()
            ]

        parameters = all_rows(parameter_table)
        values = all_rows(value_table)
        local_values = all_rows(table_by_lower['localparametervalue']) if 'localparametervalue' in table_by_lower else []
        models = all_rows(table_by_lower['model']) if 'model' in table_by_lower else []
        # A modelGroup can change include sections without changing input.scs.
        # Until that ADE contract is decoded, never silently run its template.
        if models:
            raise DiscoveryError(
                "RDB contains model-group overrides whose effective includes cannot be proven; generate separate per-corner netlists before Live execution",
                details={"path": rdb_path, "model_records": models},
            )
    except sqlite3.Error as exc:
        raise DiscoveryError("cannot query RDB parameter tables: %s" % exc)
    finally:
        connection.close()

    by_parameter_id = {}
    for row in parameters:
        parameter_id = _row_value(row, "parameterID")
        name = _row_value(row, "name")
        if parameter_id is None or name in (None, ""):
            raise DiscoveryError(
                "RDB parameter row lacks parameterID or name",
                details={"path": rdb_path, "row": row},
            )
        token = _id_token(parameter_id)
        if token in by_parameter_id:
            raise DiscoveryError(
                "RDB contains duplicate parameterID %s" % parameter_id,
                details={"path": rdb_path},
            )
        by_parameter_id[token] = row

    by_point = {}
    for row, is_local in [(row, False) for row in values] + [(row, True) for row in local_values]:
        point_id = _row_value(row, "pointID")
        parameter_id = _row_value(row, "parameterID")
        if point_id is None or parameter_id is None:
            raise DiscoveryError(
                "RDB parameterValue row lacks pointID or parameterID",
                details={"path": rdb_path, "row": row},
            )
        metadata = by_parameter_id.get(_id_token(parameter_id))
        if metadata is None:
            raise DiscoveryError(
                "RDB parameterValue references unknown parameterID %s" % parameter_id,
                details={"path": rdb_path, "row": row},
            )
        record = dict(metadata)
        record["value"] = _row_value(row, "value")
        if is_local:
            local_test = _row_value(row, "testID")
            if _is_global_test_scope(local_test):
                raise DiscoveryError("localParameterValue lacks a test scope", details={"path": rdb_path})
            record['testID'] = local_test
            record['value_source'] = 'localParameterValue'
        else:
            record['value_source'] = 'parameterValue'
        by_point.setdefault(_id_token(point_id), []).append(record)
    return by_parameter_id, by_point


def _parameters_for_run(records, test_id):
    relevant = []
    selected = {}
    ambiguous = {}
    run_test_token = _id_token(test_id)

    for raw_record in records:
        record = dict(raw_record)
        scope = _row_value(record, "testID")
        if _is_global_test_scope(scope):
            priority = 0
        elif _id_token(scope) == run_test_token:
            priority = 2 if record.get('value_source') == 'localParameterValue' else 1
        else:
            continue
        name = str(_row_value(record, "name"))
        value = record.get("value")
        relevant.append(record)
        selected.setdefault(name, []).append((priority, value, record))

    params = {}
    for name, choices in selected.items():
        priority = max(item[0] for item in choices)
        best = [item for item in choices if item[0] == priority]
        values = []
        for _, value, _ in best:
            if not any(value == existing for existing in values):
                values.append(value)
        if len(values) == 1:
            params[name] = values[0]
        else:
            ambiguous[name] = [dict(item[2]) for item in best]
    return params, relevant, ambiguous


def _test_matches(run, selector):
    if selector is None:
        return True
    selector_text = str(selector)
    return selector_text in (str(run.get("test")), str(run.get("testID")))


def load_maestro_runs(rdb_path, test=None, require_complete=True):
    """Return exact, uniquely identified point/test runs from a Maestro RDB.

    ``inspect_rdb`` remains the authority for exact PSF completion checks.
    Parameter values are then read separately through SQLite ``mode=ro`` plus
    ``query_only``.  A point/test exposed through more than one raw directory
    is rejected instead of selecting a candidate by timestamp.
    """

    if not isinstance(require_complete, bool):
        raise ValidationError("require_complete must be a boolean")
    canonical_rdb = _path(rdb_path, "RDB path")
    inspected = inspect_rdb(
        {"path": canonical_rdb, "verify_completion": True}
    )
    if inspected.get("truncated_tables"):
        raise DiscoveryError(
            "RDB inspection was truncated; run identity cannot be proven unique",
            details={
                "path": canonical_rdb,
                "tables": inspected.get("truncated_tables"),
            },
        )

    _, parameter_values = _read_parameter_tables(canonical_rdb)
    source_runs = [run for run in inspected.get("runs", []) if _test_matches(run, test)]
    if not source_runs:
        label = "all tests" if test is None else "test %s" % test
        raise DiscoveryError(
            "RDB contains no inspected runs for %s" % label,
            details={"path": canonical_rdb, "test": test},
        )

    runs = []
    identities = {}
    logical_candidates = {}
    incomplete = []
    for source in source_runs:
        raw_dir_value = source.get("rawDir")
        point_id = source.get("pointID")
        test_id = source.get("testID")
        test_name = source.get("test")
        psf_value = source.get("psfDir")
        if raw_dir_value in (None, "") or point_id is None or test_name in (None, ""):
            raise DiscoveryError(
                "RDB run lacks rawDir, pointID, test, or exact psfDir",
                details={"path": canonical_rdb, "run": source},
            )

        raw_dir = _path(raw_dir_value, "RDB rawDir")
        if psf_value in (None, ""):
            raise DiscoveryError(
                "RDB run has no exact point/test PSF path",
                details={"path": canonical_rdb, "run": source},
            )
        psf_dir = _path(psf_value, "PSF directory")
        expected_psf = os.path.realpath(
            os.path.join(raw_dir, str(point_id), str(test_name), "psf")
        )
        if psf_dir != expected_psf:
            raise DiscoveryError(
                "RDB PSF path is not the exact rawDir/point/test/psf path",
                details={"reported": psf_dir, "expected": expected_psf},
            )

        identity = (canonical_rdb, raw_dir, point_id, test_id, test_name)
        if identity in identities:
            raise DiscoveryError(
                "one run identity has multiple PSF candidates",
                details={"identity": list(identity), "psf_dirs": [identities[identity], psf_dir]},
            )
        logical_identity = (canonical_rdb, point_id, test_id, test_name)
        if logical_identity in logical_candidates:
            previous = logical_candidates[logical_identity]
            raise DiscoveryError(
                "one Maestro point/test resolves through multiple raw directories",
                details={
                    "pointID": point_id,
                    "testID": test_id,
                    "test": test_name,
                    "candidates": [previous, psf_dir],
                },
            )
        identities[identity] = psf_dir
        logical_candidates[logical_identity] = psf_dir

        params, parameter_records, ambiguous_params = _parameters_for_run(
            parameter_values.get(_id_token(point_id), []), test_id
        )
        record = {
            "identity": identity,
            "rdb": canonical_rdb,
            "raw_dir": raw_dir,
            "point_id": point_id,
            "test_id": test_id,
            "test": test_name,
            "corner": source.get("corner"),
            "params": params,
            "parameter_records": parameter_records,
            "ambiguous_params": ambiguous_params,
            "psf_dir": psf_dir,
            "complete": bool(source.get("complete")),
            "zero_errors": bool(source.get("zero_errors")),
            "status_code": source.get("statusCode"),
            "sim_done": source.get("simDone"),
            "spectre_out": source.get("spectreOut"),
        }
        runs.append(record)
        if require_complete and not record["complete"]:
            incomplete.append(
                {
                    "identity": list(identity),
                    "psf_dir": psf_dir,
                    "sim_done": record["sim_done"],
                    "spectre_out": record["spectre_out"],
                }
            )

    if incomplete:
        raise DiscoveryError(
            "%d selected Maestro run(s) lack proven Spectre completion" % len(incomplete),
            details={"path": canonical_rdb, "incomplete": incomplete},
        )
    runs.sort(
        key=lambda row: (
            str(row.get("corner")),
            str(row.get("test")),
            str(row.get("point_id")),
            row.get("raw_dir", ""),
        )
    )
    return runs


def _run_sequence(value, label):
    if isinstance(value, Mapping):
        value = value.get("runs")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValidationError("%s must be a run list or an object with runs" % label)
    return list(value)


def _pair_token(value):
    if value is None:
        raise ValidationError("pairing values must not be null")
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValidationError("pairing values must be finite")
        return ("number", Decimal(str(value)).normalize())
    text = str(value).strip()
    if not text:
        return ("text", "")
    try:
        numeric = Decimal(text)
        if numeric.is_finite():
            return ("number", numeric.normalize())
    except InvalidOperation:
        pass
    return ("text", text)


def _pair_key(run, sweep_keys):
    if not isinstance(run, Mapping):
        raise ValidationError("each run must be an object")
    corner = run.get("corner")
    if corner in (None, ""):
        raise ValidationError(
            "run lacks a corner required for VCO/replica pairing",
            details={"identity": run.get("identity")},
        )
    params = run.get("params", {})
    if not isinstance(params, Mapping):
        raise ValidationError("run params must be an object")
    ambiguous = run.get("ambiguous_params", {})
    key = [("corner", ("text", str(corner)))]
    display = {"corner": corner}
    for name in sweep_keys:
        if isinstance(ambiguous, Mapping) and name in ambiguous:
            raise ValidationError(
                "sweep parameter %s is ambiguous in a run" % name,
                details={"identity": run.get("identity"), "parameter": name},
            )
        if name not in params:
            raise ValidationError(
                "run lacks sweep parameter %s" % name,
                details={"identity": run.get("identity"), "available": sorted(params)},
            )
        display[name] = params[name]
        key.append((name, _pair_token(params[name])))
    return tuple(key), display


def pair_runs(vco, replica, sweep_keys):
    """Pair VCO and replica runs only by corner and configured sweep keys."""

    vco_runs = _run_sequence(vco, "vco")
    replica_runs = _run_sequence(replica, "replica")
    if sweep_keys is None:
        sweep_keys = []
    if not isinstance(sweep_keys, Sequence) or isinstance(sweep_keys, (str, bytes)):
        raise ValidationError("sweep_keys must be a list of parameter names")
    normalized_keys = []
    for raw_name in sweep_keys:
        name = str(raw_name).strip()
        if not name:
            raise ValidationError("sweep_keys must not contain empty names")
        if name in normalized_keys:
            raise ValidationError("duplicate sweep key: %s" % name)
        normalized_keys.append(name)

    def index(runs, label):
        result = {}
        displays = {}
        for run in runs:
            key, display = _pair_key(run, normalized_keys)
            if key in result:
                raise ValidationError(
                    "%s runs contain a duplicate corner+sweep key" % label,
                    details={
                        "key": display,
                        "identities": [result[key].get("identity"), run.get("identity")],
                    },
                )
            result[key] = run
            displays[key] = display
        return result, displays

    vco_index, vco_display = index(vco_runs, "VCO")
    replica_index, replica_display = index(replica_runs, "replica")
    vco_keys = set(vco_index)
    replica_keys = set(replica_index)
    if vco_keys != replica_keys:
        missing_replica = [vco_display[key] for key in sorted(vco_keys - replica_keys, key=repr)]
        missing_vco = [replica_display[key] for key in sorted(replica_keys - vco_keys, key=repr)]
        raise ValidationError(
            "VCO and replica runs are not one-to-one by corner+sweep keys",
            details={
                "sweep_keys": normalized_keys,
                "missing_in_replica": missing_replica,
                "missing_in_vco": missing_vco,
            },
        )

    return [
        {
            "key": vco_display[key],
            "vco": vco_index[key],
            "replica": replica_index[key],
        }
        for key in sorted(vco_keys, key=repr)
    ]


def hbxf_sideband_suffix(harmonic):
    """Map signed input harmonic k to Cadence HBXF file suffix k-1."""

    if isinstance(harmonic, bool):
        raise ValidationError("harmonic must be an integer")
    try:
        integer = int(harmonic)
    except (TypeError, ValueError):
        raise ValidationError("harmonic must be an integer")
    if harmonic != integer:
        raise ValidationError("harmonic must be an integer")
    return integer - 1


def hbxf_sideband_mapping(max_harmonic):
    """Return ``{k: k-1}`` for every signed harmonic ``-K..K``."""

    if isinstance(max_harmonic, bool):
        raise ValidationError("max_harmonic must be a non-negative integer")
    try:
        maximum = int(max_harmonic)
    except (TypeError, ValueError):
        raise ValidationError("max_harmonic must be a non-negative integer")
    if max_harmonic != maximum or maximum < 0:
        raise ValidationError("max_harmonic must be a non-negative integer")
    return {harmonic: harmonic - 1 for harmonic in range(-maximum, maximum + 1)}


def hbxf_sideband_suffixes(max_harmonic):
    return list(hbxf_sideband_mapping(max_harmonic).values())


def _float_token(value):
    try:
        number = float(str(value).replace("D", "e").replace("d", "e"))
    except (TypeError, ValueError):
        raise DiscoveryError("invalid numeric value in PSF output: %r" % value)
    if not math.isfinite(number):
        raise DiscoveryError("non-finite numeric value in PSF output: %r" % value)
    return number


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


class PsfReader:
    """Minimal read-only wrapper around the IC618 ``psf`` executable.

    An injected runner receives exactly one argument: the command argument
    list.  It may return text/bytes, ``subprocess.CompletedProcess``, or a
    mapping with ``returncode``, ``stdout`` and ``stderr``.
    """

    def __init__(self, executable="psf", runner=None, env=None):
        try:
            executable = os.fspath(executable)
        except (AttributeError, TypeError):
            pass
        if isinstance(executable, bytes):
            executable = os.fsdecode(executable)
        if not isinstance(executable, str) or not executable or "\x00" in executable:
            raise ValidationError("psf executable must be a non-empty path or command")
        if runner is not None and not callable(runner):
            raise ValidationError("runner must be callable")
        if env is not None and not isinstance(env, Mapping):
            raise ValidationError("env must be an environment mapping")
        self.executable = executable
        self.runner = runner
        self.env = None if env is None else {str(key): str(value) for key, value in env.items()}

    def _run(self, command):
        if self.runner is None:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=self.env,
            )
            result = completed
        else:
            result = self.runner(list(command))

        if isinstance(result, Mapping):
            returncode = int(result.get("returncode", 0))
            stdout = _text(result.get("stdout", ""))
            stderr = _text(result.get("stderr", ""))
        elif hasattr(result, "returncode") and hasattr(result, "stdout"):
            returncode = int(result.returncode)
            stdout = _text(result.stdout)
            stderr = _text(getattr(result, "stderr", ""))
        elif isinstance(result, (str, bytes)):
            returncode = 0
            stdout = _text(result)
            stderr = ""
        else:
            raise DiscoveryError(
                "PSF runner returned an unsupported result type: %s"
                % type(result).__name__
            )
        if returncode != 0:
            raise DiscoveryError(
                "psf command failed with exit code %d" % returncode,
                details={
                    "command": list(command),
                    "stderr": stderr[-4000:],
                    "stdout": stdout[-4000:],
                },
            )
        return stdout

    @staticmethod
    def _dataset(value):
        try:
            value = os.fspath(value)
        except (AttributeError, TypeError):
            pass
        if isinstance(value, bytes):
            value = os.fsdecode(value)
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValidationError("PSF dataset must be a non-empty path")
        return value

    @staticmethod
    def _trace(value):
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValidationError("PSF trace must be a non-empty string")
        return value

    def dataset_text(self, dataset):
        return self._run([self.executable, "-i", self._dataset(dataset)])

    def trace_text(self, dataset, trace):
        return self._run(
            [
                self.executable,
                "-s",
                "-t",
                self._trace(trace),
                "-f",
                "%.16e",
                "-i",
                self._dataset(dataset),
            ]
        )

    def fundamental(self, psf_dir_or_dataset):
        path = self._dataset(psf_dir_or_dataset)
        if os.path.basename(path) != "hb.fd.pss_hb":
            path = os.path.join(path, "hb.fd.pss_hb")
        text = self.dataset_text(path)
        match = re.search(
            r'^\s*"fundamental"\s+"?(%s)"?\s*$' % _NUMBER,
            text,
            flags=re.MULTILINE,
        )
        if match is None:
            raise DiscoveryError("HB fundamental is absent in %s" % path)
        return _float_token(match.group(1))

    def real_wave(self, dataset, trace):
        trace = self._trace(trace)
        text = self.trace_text(dataset, trace)
        trace_re = re.compile(
            r'^\s*"%s"\s+"?(%s)"?\s*$' % (re.escape(trace), _NUMBER)
        )
        current = None
        rows = []
        for raw_line in text.splitlines():
            axis = _AXIS_RE.match(raw_line)
            if axis:
                current = _float_token(axis.group("value"))
                continue
            value = trace_re.match(raw_line)
            if current is not None and value:
                rows.append((current, _float_token(value.group(1))))
        if not rows:
            raise DiscoveryError("real trace %s is absent in %s" % (trace, dataset))
        return rows

    def complex_wave(self, dataset, trace):
        trace = self._trace(trace)
        text = self.trace_text(dataset, trace)
        trace_re = re.compile(
            r'^\s*"%s"\s+\(\s*(%s)\s*,?\s+(%s)\s*\)\s*$'
            % (re.escape(trace), _NUMBER, _NUMBER)
        )
        current = None
        rows = []
        for raw_line in text.splitlines():
            axis = _AXIS_RE.match(raw_line)
            if axis:
                current = _float_token(axis.group("value"))
                continue
            value = trace_re.match(raw_line)
            if current is not None and value:
                rows.append(
                    (
                        current,
                        complex(_float_token(value.group(1)), _float_token(value.group(2))),
                    )
                )
        if not rows:
            raise DiscoveryError("complex trace %s is absent in %s" % (trace, dataset))
        return rows

    def struct_noise(self, dataset, trace):
        trace = self._trace(trace)
        text = self.trace_text(dataset, trace)
        start_re = re.compile(r'^\s*"%s"\s+\(\s*$' % re.escape(trace))
        numeric_line_re = re.compile(
            r'^(?:\s*"[^"]+"\s+)?\s*(%s)\s*$' % _NUMBER
        )
        current = None
        collecting = False
        values = []
        rows = {}
        for raw_line in text.splitlines():
            axis = _AXIS_RE.match(raw_line)
            if axis and not collecting:
                current = _float_token(axis.group("value"))
                continue
            if current is not None and not collecting and start_re.match(raw_line):
                collecting = True
                values = []
                continue
            if collecting and raw_line.strip() == ")":
                if current in rows:
                    raise DiscoveryError(
                        "struct trace %s has duplicate axis value %s" % (trace, current)
                    )
                rows[current] = values
                collecting = False
                continue
            if collecting:
                value = numeric_line_re.match(raw_line)
                if value:
                    values.append(_float_token(value.group(1)))
        if collecting:
            raise DiscoveryError("unterminated struct trace %s in %s" % (trace, dataset))
        if not rows:
            raise DiscoveryError("struct trace %s is absent in %s" % (trace, dataset))
        return rows

    def struct_value_at(self, dataset, trace, frequency, field_index=3):
        rows = self.struct_noise(dataset, trace)
        target = _float_token(frequency)
        if isinstance(field_index, bool):
            raise ValidationError("field_index must be a non-negative integer")
        try:
            index = int(field_index)
        except (TypeError, ValueError):
            raise ValidationError("field_index must be a non-negative integer")
        if field_index != index or index < 0:
            raise ValidationError("field_index must be a non-negative integer")
        actual = min(rows, key=lambda item: abs(item - target))
        if index >= len(rows[actual]):
            raise DiscoveryError(
                "struct field %d is absent for trace %s at %s Hz"
                % (index, trace, actual)
            )
        return rows[actual][index]

    read_fundamental = fundamental
    read_real_wave = real_wave
    read_complex_wave = complex_wave
    read_struct_noise = struct_noise


__all__ = [
    "PsfReader",
    "hbxf_sideband_mapping",
    "hbxf_sideband_suffix",
    "hbxf_sideband_suffixes",
    "load_maestro_runs",
    "pair_runs",
]
