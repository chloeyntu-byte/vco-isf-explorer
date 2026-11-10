"""Per-invocation native Maestro context; records locate, never certify results.

Only the Ocean child receives captured Cadence environment variables.  Context
refresh opens the same setup read-only and preserves the selected history; it
does not select a newer result or recursively discover another library.
"""
from __future__ import unicode_literals

import copy
import json
import os
import re
import subprocess
import tempfile
import uuid

from .errors import DiscoveryError, ValidationError

SCHEMA = "ISF_MAESTRO_CONTEXT_1"
MAX_CONTEXT_BYTES = 1024 * 1024
MAX_FIELD_CHARS = 16384
MAX_HISTORIES = 512
RUNTIME_ENV_NAMES = frozenset(("PATH", "CDS_LIC_FILE", "LM_LICENSE_FILE", "LD_LIBRARY_PATH"))
_TOKEN = re.compile(r'\s*("(?:[^"\\\r\n]|\\.)*"|nil|0)(?=\s|$)')


def _text(value, field, optional=False):
    if value in (None, 0) and optional:
        return None
    if not isinstance(value, str) or (not value and not optional):
        raise ValidationError("Maestro context %s must be a string" % field)
    if len(value) > MAX_FIELD_CHARS or any(ord(c) < 32 for c in value):
        raise ValidationError("Maestro context %s is too long or contains control characters" % field)
    return value or None


def _absolute(value, field, work_dir=None, optional=False, resolve=True):
    value = _text(value, field, optional=optional)
    if value is None:
        return None
    if not os.path.isabs(value):
        if not work_dir:
            raise ValidationError("Maestro context %s must be absolute" % field)
        value = os.path.join(work_dir, value)
    # Preserve Ocean's invocation name: Cadence executable aliases may all
    # resolve to one generic wrapper which relies on argv[0].
    return os.path.realpath(value) if resolve else os.path.abspath(value)


def _fields(line):
    fields, pos = [], 0
    while pos < len(line):
        if not line[pos:].strip():
            break
        match = _TOKEN.match(line, pos)
        if not match:
            raise ValidationError("Invalid quoted Maestro context field")
        token = match.group(1)
        if token in ("nil", "0"):
            fields.append(None)
        else:
            try:
                fields.append(json.loads(token))
            except ValueError as exc:
                raise ValidationError("Invalid Maestro context string: %s" % exc)
        pos = match.end()
    return fields


def _normalize_context(context):
    if not isinstance(context, dict) or context.get("schema") != SCHEMA:
        raise ValidationError("Unsupported Maestro context schema")
    result = {"schema": SCHEMA}
    for key in ("library", "cell", "view"):
        value = _text(context.get(key), key)
        if value in (".", "..") or "/" in value or "\\" in value:
            raise ValidationError("Invalid Maestro context %s identity" % key)
        result[key] = value
    work_dir = _absolute(context.get("work_dir"), "work_dir")
    view_dir = _absolute(context.get("maestro_dir", context.get("view_dir")), "maestro_dir", work_dir)
    if os.path.basename(view_dir) == "maestro.sdb":
        view_dir = os.path.dirname(view_dir)
    result.update(work_dir=work_dir, maestro_dir=view_dir, view_dir=view_dir)
    result["ocean"] = _absolute(context.get("ocean"), "ocean", work_dir, resolve=False)
    result["selected_history"] = _text(context.get("selected_history"), "selected_history", optional=True)
    env = context.get("runtime_env", {})
    if not isinstance(env, dict) or set(env) - RUNTIME_ENV_NAMES:
        raise ValidationError("Unsupported Maestro runtime environment variable")
    result["runtime_env"] = {key: _text(value, key, optional=True) for key, value in env.items()}
    histories = context.get("histories", [])
    if not isinstance(histories, list) or len(histories) > MAX_HISTORIES:
        raise ValidationError("Maestro history catalog exceeds its limit")
    result["histories"] = []
    names = set()
    for record in histories:
        if not isinstance(record, dict):
            raise ValidationError("Maestro history entries must be objects")
        name = _text(record.get("name"), "history name")
        if name in names:
            raise ValidationError("Duplicate native Maestro history name")
        names.add(name)
        rdb = _absolute(record.get("rdb_file", record.get("path")), "rdb_file", work_dir, optional=True)
        if rdb and not rdb.lower().endswith(".rdb"):
            raise ValidationError("Native Maestro history does not identify an RDB file")
        result["histories"].append({
            "name": name, "rdb_file": rdb,
            "results_dir": _absolute(record.get("results_dir"), "results_dir", work_dir, optional=True),
        })
    result["launch_id"] = _text(context.get("launch_id") or uuid.uuid4().hex, "launch_id")
    return result


def load_maestro_context(path, consume=True):
    """Parse the bounded SKILL %L protocol without eval or shell expansion.

    Delete only a successfully consumed context file, never a source TB or RDB.
    No completion status, default history or filesystem ownership is inferred.
    """
    with open(path, "rb") as stream:
        raw = stream.read(MAX_CONTEXT_BYTES + 1)
    if len(raw) > MAX_CONTEXT_BYTES:
        raise ValidationError("Maestro context file exceeds its size limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("Maestro context must be UTF-8: %s" % exc)
    lines = text.splitlines()
    if not lines or lines[0] != SCHEMA:
        raise ValidationError("Unsupported Maestro context protocol")
    context = {"schema": SCHEMA, "histories": [], "runtime_env": {}}
    have_context = False
    for line in lines[1:]:
        if not line.strip():
            continue
        if len(line) > 8 * MAX_FIELD_CHARS or len(line) < 2 or not line[1].isspace():
            raise ValidationError("Invalid Maestro context record")
        kind, values = line[0], _fields(line[1:])
        if kind == "C" and len(values) == 7 and not have_context:
            context.update(zip(("library", "cell", "view", "work_dir", "maestro_dir", "selected_history", "ocean"), values))
            have_context = True
        elif kind == "H" and len(values) == 3:
            context["histories"].append(dict(zip(("name", "results_dir", "rdb_file"), values)))
        elif kind == "R" and len(values) == 1 and "LD_LIBRARY_PATH" not in context["runtime_env"]:
            context["runtime_env"]["LD_LIBRARY_PATH"] = values[0]
        elif kind == "E" and len(values) == 2 and values[0] in (RUNTIME_ENV_NAMES - {"LD_LIBRARY_PATH"}) and values[0] not in context["runtime_env"]:
            context["runtime_env"][values[0]] = values[1]
        else:
            raise ValidationError("Invalid or duplicate Maestro context record: %s" % kind)
    if not have_context:
        raise ValidationError("Maestro context has no source identity")
    result = _normalize_context(context)
    if consume:
        os.unlink(path)
    return result


def _skill_string(value):
    return "nil" if value is None else json.dumps(value, ensure_ascii=False)


def _refresh_script(context, output_path):
    values = {key: _skill_string(context[key]) for key in ("library", "cell", "view", "selected_history")}
    values.update(output=_skill_string(output_path), schema=SCHEMA)
    return '''procedure(isfRefreshContext()
  let((session setup object port entry name)
    session = maeOpenSetup(%(library)s %(cell)s %(view)s ?mode "r")
    unless(session error("Cannot open source Maestro read-only.\\n"))
    setup = axlGetMainSetupDB(session)
    object = ddGetObj(%(library)s %(cell)s %(view)s)
    unless(object error("Cannot resolve source Maestro view.\\n"))
    port = outfile(%(output)s)
    unless(port error("Cannot create context refresh output.\\n"))
    fprintf(port "%(schema)s\\n")
    fprintf(port "C\\t%%L\\t%%L\\t%%L\\t%%L\\t%%L\\t%%L\\t%%L\\n"
      axlGetSessionLibName(session) axlGetSessionCellName(session)
      axlGetSessionViewName(session) getWorkingDir() object->readPath
      %(selected_history)s prependInstallPath("bin/ocean"))
    foreach(name cadr(axlGetHistory(setup))
      entry = axlGetHistoryEntry(setup name)
      when(entry
        fprintf(port "H\\t%%L\\t%%L\\t%%L\\n" name
          axlGetHistoryResultsDir(entry) axlGetHistoryResults(entry))
      )
    )
    close(port)
    maeCloseSession(?session session)
    t
  )
)
if(errset(isfRefreshContext() t) then exit(0) else exit(1))
''' % values


def refresh_maestro_context(context):
    """Enumerate this setup's current native histories using a read-only child.

    The source identity, working directory, library mapping and chosen history
    are retained. Errors leave the caller's existing snapshot intact. A missing
    or incomplete selected RDB is never replaced with another result.
    """
    original = _normalize_context(copy.deepcopy(context))
    env = os.environ.copy()
    for key, value in original["runtime_env"].items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    env.update(LC_ALL="C", LANG="C")
    with tempfile.TemporaryDirectory(prefix="isf-maestro-context-") as temporary:
        script = os.path.join(temporary, "refresh.il")
        output = os.path.join(temporary, "context.txt")
        log = os.path.join(temporary, "ocean.log")
        with open(script, "w", encoding="utf-8") as stream:
            stream.write(_refresh_script(original, output))
        try:
            with open(log, "wb") as stream:
                completed = subprocess.run(
                    [original["ocean"], "-nograph", "-replay", script],
                    cwd=original["work_dir"], env=env, stdin=subprocess.DEVNULL,
                    stdout=stream, stderr=subprocess.STDOUT, timeout=60, check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DiscoveryError("Cannot refresh this Maestro's native histories: %s" % exc)
        if completed.returncode != 0 or not os.path.isfile(output):
            with open(log, "rb") as stream:
                stream.seek(max(0, os.path.getsize(log) - 4000))
                detail = stream.read().decode("utf-8", "replace")
            raise DiscoveryError("Native Maestro history refresh failed (exit %s): %s" % (completed.returncode, detail))
        refreshed = load_maestro_context(output)
    for key in ("library", "cell", "view", "work_dir", "maestro_dir"):
        if refreshed[key] != original[key]:
            raise DiscoveryError("Maestro source identity changed during refresh: %s" % key)
    # The child reports native history paths, not a new source or environment.
    result = copy.deepcopy(original)
    result["histories"] = refreshed["histories"]
    return result


__all__ = ["SCHEMA", "load_maestro_context", "refresh_maestro_context"]
