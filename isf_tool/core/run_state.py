"""Atomic, lightweight checkpoints for resumable two-testbench runs."""

from __future__ import unicode_literals

import copy
import hashlib
import io
import json
import os
import tempfile
import time

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover
    from collections import Mapping

from .errors import ResumeError


STATE_FILENAME = ".isf_run_state.json"
STATE_SCHEMA_VERSION = 1


def _json_value(value):
    """Return a deterministic JSON-compatible copy for the run contract."""

    if isinstance(value, Mapping):
        return dict(
            (str(key), _json_value(item))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in ("resume", "background")
        )
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        return os.fspath(value)
    except (AttributeError, TypeError):
        return str(value)


def contract_sha256(config, payload):
    value = {
        "config": _json_value(config or {}),
        "payload": _json_value(payload or {}),
    }
    encoded = json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_json(path, value):
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".%s." % os.path.basename(path), suffix=".tmp", dir=parent
    )
    try:
        with io.open(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class RunState(object):
    """Persist successful stage outputs without introducing a database."""

    def __init__(self, run_root, job_id, config, payload, resume=False):
        if not run_root:
            raise ResumeError(
                "resumable execution requires run_root",
                details={"field": "run_root"},
            )
        self.run_root = os.path.realpath(
            os.path.abspath(os.path.expanduser(str(run_root)))
        )
        self.path = os.path.join(self.run_root, STATE_FILENAME)
        self.job_id = str(job_id)
        self.contract_hash = contract_sha256(config, payload)
        if resume:
            self.value = self._load()
            self._validate_existing()
        else:
            if os.path.exists(self.path):
                existing = self._load()
                if (
                    str(existing.get("job_id")) != self.job_id
                    or existing.get("contract_sha256") != self.contract_hash
                ):
                    raise ResumeError(
                        "run_root contains a checkpoint for another run",
                        code="ISF-E5002",
                        details={
                            "path": self.path,
                            "existing_job_id": existing.get("job_id"),
                            "requested_job_id": self.job_id,
                        },
                    )
                self.value = existing
            else:
                now = time.time()
                self.value = {
                    "schema_version": STATE_SCHEMA_VERSION,
                    "job_id": self.job_id,
                    "contract_sha256": self.contract_hash,
                    "run_started_epoch": now,
                    "updated_epoch": now,
                    "status": "running",
                    "stages": {},
                }
                self.save()

    def _load(self):
        if not os.path.isfile(self.path):
            raise ResumeError(
                "checkpoint does not exist",
                details={"path": self.path},
            )
        try:
            with io.open(self.path, "r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (IOError, OSError, ValueError) as exc:
            raise ResumeError(
                "checkpoint cannot be read: %s" % exc,
                details={"path": self.path},
            )
        if not isinstance(value, Mapping):
            raise ResumeError(
                "checkpoint root must be an object",
                details={"path": self.path},
            )
        return dict(value)

    def _validate_existing(self):
        if self.value.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ResumeError(
                "checkpoint schema is not supported",
                code="ISF-E5002",
                details={
                    "path": self.path,
                    "schema_version": self.value.get("schema_version"),
                    "supported": STATE_SCHEMA_VERSION,
                },
            )
        if str(self.value.get("job_id")) != self.job_id:
            raise ResumeError(
                "checkpoint belongs to another job",
                code="ISF-E5002",
                details={
                    "path": self.path,
                    "existing_job_id": self.value.get("job_id"),
                    "requested_job_id": self.job_id,
                },
            )
        if self.value.get("contract_sha256") != self.contract_hash:
            raise ResumeError(
                "run contract changed after interruption",
                code="ISF-E5002",
                details={
                    "path": self.path,
                    "existing": self.value.get("contract_sha256"),
                    "requested": self.contract_hash,
                },
            )

    @property
    def run_started_epoch(self):
        return float(self.value.get("run_started_epoch", time.time()))

    def completed(self, stage):
        record = self.value.get("stages", {}).get(str(stage))
        return bool(isinstance(record, Mapping) and record.get("status") == "succeeded")

    def result(self, stage, default=None):
        record = self.value.get("stages", {}).get(str(stage))
        if not isinstance(record, Mapping) or record.get("status") != "succeeded":
            return copy.deepcopy(default)
        return copy.deepcopy(record.get("result", default))

    def progress(self, stage, default=None):
        """Return durable in-stage metadata such as a scheduler job ID."""

        record = self.value.get("stages", {}).get(str(stage))
        if not isinstance(record, Mapping):
            return copy.deepcopy(default)
        return copy.deepcopy(record.get("progress", default))

    def mark_progress(self, stage, progress):
        record = self.value.setdefault("stages", {}).setdefault(
            str(stage), {}
        )
        record["status"] = "running"
        record["started_epoch"] = record.get("started_epoch", time.time())
        record["progress"] = copy.deepcopy(progress)
        self.value["status"] = "running"
        self.save()

    def mark(self, stage, result=None):
        self.value.setdefault("stages", {})[str(stage)] = {
            "status": "succeeded",
            "finished_epoch": time.time(),
            "result": copy.deepcopy(result),
        }
        self.value["status"] = "running"
        self.save()

    def finish(self):
        self.value["status"] = "succeeded"
        self.save()

    def save(self):
        self.value["updated_epoch"] = time.time()
        _atomic_json(self.path, self.value)
