"""UI-neutral facade for the portable ISF backend core."""

from __future__ import unicode_literals

import copy
import datetime
import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
import uuid

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover
    from collections import Mapping

from .bundle import import_csv_bundle
from .discovery import discover_result_directories, inspect_rdb
from .dual_tb import import_dual_tb_bundle
from .errors import (
    CoreError,
    JobFailedError,
    JobNotFoundError,
    JobNotReadyError,
    PreflightError,
    ResumeError,
    ValidationError,
)
from .error_catalog import load_error_catalog
from .events import EventRecorder
from .models import normalize_result, utc_now, validate_json_compatible
from .maestro_pair import discover_maestro_setups, inspect_maestro_pair
from .orchestrator import DualTBOrchestrator, _validate_bundle_provenance
from .preflight import preflight as run_preflight
from .preflight import resolve_config, _runtime_identity
from .portable_setup import build_portable_setup, engine_content_sha256
from .waveform_exchange import inspect_waveform_exchange
from .run_state import STATE_FILENAME
from . import ui_state
from .offset_sweep import SWEEP_STATE, OffsetSweepStopped, is_sweep, run_offset_sweep


DEFAULT_CONFIG = {
    "schema_version": 2,
    "source_mode": "maestro_pair",
    "profiles": {
        "auto_detect": True,
        "environment": {"mode": "auto", "executables": {}},
        "pdk": "auto",
        "design": {"mode": "auto"},
    },
    "discovery": {
        "roots": [],
        "max_depth": 8,
        "max_results": 500,
        "include_hidden": False,
        "follow_symlinks": False,
        "strict": False,
    },
    "input_policy": {
        "preferred": "maestro-pair",
        "allowed": ["maestro-pair", "rdb", "psf", "canonical"],
        "allow_degraded": False,
    },
    "analysis": {
        "offset": {"value": 10.0, "unit": "kHz", "hz": 10000.0},
        "max_harmonic": 3,
        "phase_samples": 512,
        "period_count": 8,
        "enforce_common_time_origin": True,
        "allow_manual_shift": False,
    },
    "execution": {
        "dry_run": True,
        "allow_execute": False,
        "background": False,
        "adapter_mode": "bundled",
        "resume_enabled": True,
        "require_preflight": False,
    },
    "testbenches": {
        "vco": {"name": "vco", "role": "vco"},
        "replica": {"name": "replica", "role": "flicker"},
    },
}

_RUN_CONFIG_KEYS = frozenset(
    (
        "schema_version",
        "source_mode",
        "profiles",
        "project_root",
        "discovery",
        "input_policy",
        "analysis",
        "execution",
        "testbenches",
        "manual_import",
        "mapping",
        "validation",
        "auto",
        "config_path",
        "portable_setup",
        "run_kind",
        "replica_performs_exchange",
        "replica_produces_bundle",
    )
)


def _deep_merge(base, overlay):
    value = copy.deepcopy(base)
    for key, item in overlay.items():
        if key in ("mapping_overrides", "portable_setup"):
            # These are complete user selections, including an explicit reset.
            value[key] = copy.deepcopy(item)
        elif isinstance(item, Mapping) and isinstance(value.get(key), Mapping):
            value[key] = _deep_merge(value[key], item)
        else:
            value[key] = copy.deepcopy(item)
    return value


def _without_persisted_execution_consent(config):
    """Return a config copy whose Live consent cannot survive load/save."""

    sanitized = ui_state.strip_retired_noise_objects(copy.deepcopy(dict(config or {})))
    execution = sanitized.get("execution")
    if isinstance(execution, Mapping):
        execution = copy.deepcopy(dict(execution))
        execution["allow_execute"] = False
        sanitized["execution"] = execution
    return sanitized


def _seal_run_contract(config, payload):
    """Fold legacy top-level execution overrides into one preflighted config.

    The API historically accepted fields such as ``dry_run`` and
    ``testbenches`` beside the embedded config.  Leaving those values in the
    payload allowed preflight and the orchestrator to observe different
    contracts.  Preserve compatibility by migrating them once, then remove
    every migrated field from the runtime payload.
    """

    sealed = copy.deepcopy(config)
    overlay = {}
    for key in _RUN_CONFIG_KEYS:
        if key in payload:
            value = copy.deepcopy(payload.pop(key))
            if key == "testbenches" and isinstance(value, (list, tuple)):
                normalized = {}
                for index, item in enumerate(value):
                    if isinstance(item, str):
                        item = {"name": item}
                    if not isinstance(item, Mapping):
                        raise ValidationError(
                            "testbench %d must be an object" % (index + 1)
                        )
                    item = dict(item)
                    name = item.get("name")
                    if not isinstance(name, str) or not name:
                        raise ValidationError(
                            "testbench %d requires a name" % (index + 1)
                        )
                    if name in normalized:
                        raise ValidationError(
                            "testbench names must be unique"
                        )
                    normalized[name] = item
                value = normalized
            overlay[key] = value

    execution_overlay = {}
    for key in ("dry_run", "allow_execute", "adapter_mode", "resume_enabled"):
        if key in payload:
            execution_overlay[key] = copy.deepcopy(payload.pop(key))
    for payload_key, execution_key in (
        ("adapter", "adapter"),
        ("exchange_adapter", "exchange_adapter"),
        ("extract_adapter", "extract_adapter"),
    ):
        if payload_key in payload:
            execution_overlay[execution_key] = copy.deepcopy(
                payload.pop(payload_key)
            )
    if execution_overlay:
        existing = overlay.get("execution", {})
        if not isinstance(existing, Mapping):
            raise ValidationError("execution must be an object")
        overlay["execution"] = _deep_merge(existing, execution_overlay)
    if overlay:
        sealed = _deep_merge(sealed, overlay)
    return sealed


def _mapping(value, label):
    if not isinstance(value, Mapping):
        raise ValidationError("%s must be an object" % label)
    return dict(value)


def _absolute_path(value, label):
    try:
        value = os.fspath(value)
    except (AttributeError, TypeError):
        pass
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, str) or not value:
        raise ValidationError("%s must be a path string" % label)
    return os.path.realpath(os.path.abspath(os.path.expanduser(value)))


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _runtime_identity_errors(identity):
    if not isinstance(identity, Mapping):
        return {"runtime_identity": {"reason": "identity is absent"}}
    records = identity.get("files")
    if not isinstance(records, Mapping):
        records = {
            "engine_config": {
                "path": identity.get("engine_config_path"),
                "sha256": identity.get("engine_config_sha256"),
            },
            "runner": {
                "path": identity.get("runner_path"),
                "sha256": identity.get("runner_sha256"),
            },
            "engine": {
                "path": identity.get("engine_path"),
                "sha256": identity.get("engine_sha256"),
            },
        }
    errors = {}
    for label, record in records.items():
        if not isinstance(record, Mapping):
            errors[str(label)] = {"reason": "identity record is invalid"}
            continue
        path = record.get("path")
        expected = record.get("sha256")
        if path in (None, "") and expected in (None, ""):
            continue
        if not path or not expected:
            errors[str(label)] = {
                "path": path,
                "expected": expected,
                "reason": "identity record is incomplete",
            }
            continue
        path = os.path.realpath(str(path))
        if not os.path.isfile(path):
            errors[str(label)] = {
                "path": path,
                "reason": "file is absent",
            }
            continue
        actual = _file_sha256(path)
        if actual != expected:
            errors[str(label)] = {
                "path": path,
                "expected": expected,
                "actual": actual,
            }
    return errors


def _demote_unverified_signoff(result, reason):
    result = copy.deepcopy(dict(result or {}))
    source_claims = copy.deepcopy(result.get("source_claims", {}))
    source_claims["persisted_run_kind"] = result.get("run_kind")
    source_claims["persisted_signoff"] = result.get("signoff")
    source_claims["persisted_provenance"] = copy.deepcopy(
        result.get("provenance")
    )
    result["source_claims"] = source_claims
    result["run_kind"] = "replay"
    result["signoff"] = False
    result["provenance"] = None
    result["trust_warning"] = {
        "code": "ISF-E3002",
        "message": str(reason),
    }
    dual_tb = result.get("dual_tb")
    if isinstance(dual_tb, Mapping):
        dual_tb = copy.deepcopy(dict(dual_tb))
        dual_tb["signoff"] = False
        metadata = dual_tb.get("metadata")
        if isinstance(metadata, Mapping):
            metadata = copy.deepcopy(dict(metadata))
            metadata["run_kind"] = "replay"
            metadata["signoff"] = False
            dual_tb["metadata"] = metadata
        result["dual_tb"] = dual_tb
    return result


class ToolService(object):
    """Stateful JSON-compatible facade shared by Tk and Web adapters."""

    def __init__(self, workspace=None, config=None, command_runner=None, *, maestro_context=None):
        if isinstance(workspace, Mapping) and config is None:
            config = workspace
            workspace = None
        self.workspace = (
            _absolute_path(workspace, "workspace") if workspace is not None else None
        )
        self.config_path = (
            os.path.join(self.workspace, "config.json") if self.workspace else None
        )
        self._config = _deep_merge(
            DEFAULT_CONFIG,
            _without_persisted_execution_consent(config),
        )
        self._runner = command_runner
        self._environment_options = {"maestro_context": copy.deepcopy(maestro_context)} if maestro_context else {}
        self._jobs = {}
        self._threads = {}
        self._lock = threading.RLock()
        if config is None and self.config_path and os.path.isfile(self.config_path):
            self.load_config()
        self._load_persisted_jobs()

    def _job_directory(self, job_id):
        if not self.workspace:
            return None
        return os.path.join(self.workspace, "jobs", str(job_id))

    def _active_job_ids_locked(self):
        return sorted(
            str(job_id)
            for job_id, job in self._jobs.items()
            if job.get("status") in ("queued", "running")
        )

    @staticmethod
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
        except Exception as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise ValidationError(
                "atomic JSON write failed: %s" % exc,
                code="ISF-E1003",
                details={"path": path},
            )

    def _persist_job(self, job_id):
        directory = self._job_directory(job_id)
        if directory is None:
            return
        with self._lock:
            job = copy.deepcopy(self._jobs.get(str(job_id)))
        if job is None:
            return
        result = job.pop("result", None)
        request_snapshot = job.pop("_request", None)
        config_snapshot = job.pop("_config_snapshot", None)
        job["result_available"] = result is not None
        if result is not None:
            result_path = os.path.join(directory, "result.json")
            self._atomic_json(result_path, result)
            job["result_sha256"] = _file_sha256(result_path)
        else:
            job.pop("result_sha256", None)
        if request_snapshot is not None and config_snapshot is not None:
            self._atomic_json(
                os.path.join(directory, "request.json"),
                {
                    "request": request_snapshot,
                    "config": config_snapshot,
                },
            )
        self._atomic_json(os.path.join(directory, "job.json"), job)

    def _revalidate_persisted_signoff(
        self, job_id, result, request_snapshot, config_snapshot
    ):
        if not isinstance(request_snapshot, Mapping) or not isinstance(
            config_snapshot, Mapping
        ):
            raise ValidationError(
                "persisted signoff lacks its request/config snapshot",
                code="ISF-E3002",
            )
        run_root = request_snapshot.get("run_root")
        state_path = (
            os.path.join(str(run_root), STATE_FILENAME) if run_root else None
        )
        if not state_path or not os.path.isfile(state_path):
            raise ValidationError(
                "persisted signoff checkpoint is absent",
                code="ISF-E3002",
                details={"path": state_path},
            )
        try:
            with io.open(state_path, "r", encoding="utf-8") as stream:
                state = json.load(stream)
            run_started = float(state["run_started_epoch"])
        except (IOError, OSError, ValueError, KeyError, TypeError) as exc:
            raise ValidationError(
                "persisted signoff checkpoint is invalid: %s" % exc,
                code="ISF-E3002",
                details={"path": state_path},
            )
        bundle_payload = request_snapshot.get("bundle")
        if not isinstance(bundle_payload, Mapping):
            bundle_path = request_snapshot.get("bundle_path")
            bundle_payload = (
                {"path": bundle_path}
                if bundle_path not in (None, "")
                else None
            )
        verified = _validate_bundle_provenance(
            bundle_payload,
            str(job_id),
            run_started,
            run_root,
            "signoff",
            config_snapshot,
        )
        stored = result.get("provenance")
        if (
            not isinstance(stored, Mapping)
            or stored.get("job_id") != verified.get("job_id")
            or stored.get("manifest_sha256")
            != verified.get("manifest_sha256")
            or stored.get("psf_runs") != verified.get("psf_runs")
        ):
            raise ValidationError(
                "persisted result provenance differs from current evidence",
                code="ISF-E3002",
            )
        return verified

    def _load_persisted_jobs(self):
        if not self.workspace:
            return
        root = os.path.join(self.workspace, "jobs")
        if not os.path.isdir(root):
            return
        for name in sorted(os.listdir(root)):
            directory = os.path.join(root, name)
            job_path = os.path.join(directory, "job.json")
            if not os.path.isfile(job_path):
                continue
            try:
                with io.open(job_path, "r", encoding="utf-8") as stream:
                    job = json.load(stream)
                if not isinstance(job, Mapping) or str(job.get("id")) != name:
                    continue
                result_path = os.path.join(directory, "result.json")
                result = None
                result_integrity_error = None
                if job.get("result_available") and os.path.isfile(result_path):
                    expected_result_hash = job.get("result_sha256")
                    actual_result_hash = _file_sha256(result_path)
                    if (
                        expected_result_hash
                        and actual_result_hash != expected_result_hash
                    ):
                        result_integrity_error = {
                            "code": "ISF-E3002",
                            "kind": "provenance_error",
                            "message": "persisted result hash mismatch",
                            "details": {
                                "path": result_path,
                                "expected": expected_result_hash,
                                "actual": actual_result_hash,
                            },
                        }
                    else:
                        with io.open(
                            result_path, "r", encoding="utf-8"
                        ) as stream:
                            result = normalize_result(json.load(stream))
                job = dict(job)
                job.pop("result_available", None)
                job["result"] = result
                request_path = os.path.join(directory, "request.json")
                if os.path.isfile(request_path):
                    with io.open(request_path, "r", encoding="utf-8") as stream:
                        request_value = json.load(stream)
                    if isinstance(request_value, Mapping):
                        job["_request"] = copy.deepcopy(
                            request_value.get("request", {})
                        )
                        job["_config_snapshot"] = copy.deepcopy(
                            request_value.get("config", {})
                        )
                claimed_signoff = bool(
                    isinstance(result, Mapping)
                    and (
                        result.get("signoff") is True
                        or result.get("run_kind") == "signoff"
                        or (
                            isinstance(result.get("dual_tb"), Mapping)
                            and result["dual_tb"].get("signoff") is True
                        )
                    )
                )
                if claimed_signoff:
                    if not job.get("result_sha256"):
                        result = _demote_unverified_signoff(
                            result,
                            "legacy persisted result has no integrity hash",
                        )
                    else:
                        try:
                            verified = self._revalidate_persisted_signoff(
                                name,
                                result,
                                job.get("_request"),
                                job.get("_config_snapshot"),
                            )
                            result = copy.deepcopy(dict(result))
                            result["provenance"] = verified
                        except Exception as exc:
                            result = _demote_unverified_signoff(
                                result, str(exc)
                            )
                    job["result"] = result
                if result_integrity_error is not None:
                    job["result"] = None
                    job["status"] = "failed"
                    job["error"] = result_integrity_error
                    job["message"] = result_integrity_error["message"]
                    job["finished_at"] = utc_now()
                    job["updated_at"] = job["finished_at"]
                if job.get("status") in ("queued", "running"):
                    job["status"] = "interrupted"
                    job["message"] = "Process restarted before job completion"
                    job["error"] = ResumeError(
                        job["message"],
                        details={"resume_available": bool(job.get("_request"))},
                    ).to_dict()
                    job["finished_at"] = utc_now()
                    job["updated_at"] = job["finished_at"]
                    job["result_available"] = job.get("result") is not None
                    self._atomic_json(
                        job_path,
                        dict(
                            (key, value)
                            for key, value in job.items()
                            if key not in ("result", "_request", "_config_snapshot")
                        ),
                    )
                self._jobs[name] = job
            except (IOError, OSError, ValueError, ValidationError):
                continue

    def health(self):
        with self._lock:
            counts = {
                "queued": 0,
                "running": 0,
                "succeeded": 0,
                "failed": 0,
                "interrupted": 0,
            }
            for job in self._jobs.values():
                counts[job["status"]] = counts.get(job["status"], 0) + 1
        return {
            "status": "ok",
            "service": "isf-portable-tool-core",
            "version": "1.1.0",
            "jobs": counts,
            "capabilities": {
                "discovery": ["maestro-pair", "maestro", "psf", "sqlite-rdb"],
                "primary_input": "vco-maestro+flicker-maestro",
                "imports": ["closure-csv-bundle", "canonical-two-tb"],
                "adapters": ["runSimulation", "custom_shell", "protocol"],
                "background_jobs": True,
                "dry_run_default": True,
                "preflight": True,
                "resume": True,
                "error_catalog": True,
            },
        }

    def error_catalog(self):
        return load_error_catalog()

    def preview_offsets(self, payload):
        from .offsets import MAX_OFFSET_POINTS, expand_offsets
        payload = _mapping(payload or {}, "offset preview payload")
        values = expand_offsets(_mapping(payload.get("analysis", {}), "analysis"))
        return {"ok": True, "offsets_hz": values, "count": len(values),
                "max_points": MAX_OFFSET_POINTS, "endpoint_policy": "include_stop"}

    def preflight(self, payload=None):
        payload = _mapping(payload or {}, "preflight payload")
        embedded = payload.get("config")
        with self._lock:
            config_snapshot = copy.deepcopy(self._config)
        if isinstance(embedded, Mapping):
            config_snapshot = _deep_merge(config_snapshot, embedded)
        elif "config" not in payload:
            config_snapshot = _deep_merge(config_snapshot, payload)
        return run_preflight(config_snapshot, workspace=self.workspace, **self._environment_options)

    def list_states(self):
        return ui_state.list_states(self.workspace)

    def save_state(self, payload):
        return ui_state.save_state(self.workspace, payload)

    def load_state(self, payload):
        return ui_state.load_state(self.workspace, payload.get("id") if isinstance(payload, Mapping) else payload)

    def load_config(self, payload=None):
        target = None
        if payload is not None:
            if isinstance(payload, Mapping):
                target = payload.get("path")
            else:
                target = payload
        if target is None:
            target = self.config_path
        if target is None:
            with self._lock:
                return copy.deepcopy(self._config)
        target = _absolute_path(target, "config path")
        if not os.path.isfile(target):
            if target == self.config_path:
                with self._lock:
                    return copy.deepcopy(self._config)
            raise ValidationError(
                "config file does not exist: %s" % target, code="ISF-E1003"
            )
        try:
            with io.open(target, "r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (ValueError, UnicodeError, IOError) as exc:
            raise ValidationError(
                "cannot load config: %s" % exc, code="ISF-E1003"
            )
        loaded = _without_persisted_execution_consent(
            _mapping(loaded, "config")
        )
        validate_json_compatible(loaded, "config")
        with self._lock:
            self._config = _deep_merge(DEFAULT_CONFIG, loaded)
            self.config_path = target
            return copy.deepcopy(self._config)

    def save_config(self, payload=None, path=None):
        if payload is None:
            with self._lock:
                new_config = copy.deepcopy(self._config)
        else:
            payload = _mapping(payload, "config payload")
            if "config" in payload and set(payload).issubset(set(("config", "path"))):
                new_config = _mapping(payload["config"], "config")
                path = path or payload.get("path")
            else:
                new_config = payload
        new_config = _without_persisted_execution_consent(new_config)
        validate_json_compatible(new_config, "config")
        target = path or self.config_path
        if target is None:
            raise ValidationError(
                "save_config requires a workspace or path", code="ISF-E1003"
            )
        target = _absolute_path(target, "config path")
        parent = os.path.dirname(target)
        if not os.path.isdir(parent):
            raise ValidationError(
                "config parent directory does not exist: %s" % parent,
                code="ISF-E1003",
            )

        backup = None
        if os.path.isfile(target):
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = "%s.bak-%s" % (target, stamp)
            suffix = 1
            while os.path.exists(backup):
                backup = "%s.bak-%s-%d" % (target, stamp, suffix)
                suffix += 1
            shutil.copy2(target, backup)

        descriptor, temporary = tempfile.mkstemp(
            prefix=".%s." % os.path.basename(target), suffix=".tmp", dir=parent
        )
        try:
            with io.open(descriptor, "w", encoding="utf-8", closefd=True) as stream:
                json.dump(new_config, stream, ensure_ascii=False, allow_nan=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except Exception as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise ValidationError(
                "cannot save config: %s" % exc,
                code="ISF-E1003",
                details={"path": target},
            )

        with self._lock:
            self._config = _deep_merge(DEFAULT_CONFIG, new_config)
            self.config_path = target
        return {
            "ok": True,
            "path": target,
            "backup": backup,
            "config": copy.deepcopy(self._config),
        }

    def _configured_roots(self, payload=None):
        roots = []
        values = [payload or {}, self._config]
        for value in values:
            if not isinstance(value, Mapping):
                continue
            project_root = value.get("project_root", value.get("projectRoot"))
            if project_root:
                roots.append(project_root)
            discovery = value.get("discovery", {})
            if isinstance(discovery, Mapping):
                configured = discovery.get("roots", discovery.get("root", []))
                if isinstance(configured, (str, bytes)):
                    configured = [configured]
                if isinstance(configured, (list, tuple)):
                    roots.extend(configured)
        if self.workspace:
            roots.append(self.workspace)
        normalized = []
        for root in roots:
            try:
                candidate = _absolute_path(root, "configured root")
            except ValidationError:
                continue
            if candidate not in normalized:
                normalized.append(candidate)
        return normalized

    def path_is_allowed(self, path):
        candidate = _absolute_path(path, "path")
        for root in self._configured_roots():
            try:
                if os.path.commonpath((candidate, root)) == root:
                    return True
            except ValueError:
                continue
        return False

    def discover(self, payload):
        payload = _mapping(payload or {}, "discovery payload")
        nested = payload.get("discovery")
        options = dict(nested) if isinstance(nested, Mapping) else dict(payload)
        if not options.get("roots") and not options.get("root") and not options.get("path"):
            roots = self._configured_roots(payload)
            options["roots"] = [root for root in roots if root != self.workspace]
        configured = self._config.get("discovery", {})
        if isinstance(configured, Mapping):
            options = _deep_merge(configured, options)
        return discover_result_directories(options)

    def inspect_rdb(self, payload):
        return inspect_rdb(payload)

    def discover_maestro(self, payload):
        payload = _mapping(payload or {}, "Maestro discovery payload")
        return discover_maestro_setups(payload)

    def inspect_maestro_pair(self, payload):
        payload = _mapping(payload or {}, "Maestro pair payload")
        report = inspect_maestro_pair(payload)
        # Inspection is always passive, even when the form currently says Live.
        inspection_config = copy.deepcopy(payload)
        inspection_config["source_mode"] = "maestro_pair"
        inspection_config["execution"] = {"dry_run": True, "allow_execute": False}
        resolved = resolve_config(inspection_config, workspace=self.workspace)
        report["portable_setup"] = build_portable_setup(
            report, resolved, resolved["detection"]["environment"]
        )
        report["waveform_exchange"] = inspect_waveform_exchange(report, report["portable_setup"].get("engine"))
        if not report["waveform_exchange"]["ok"]:
            report["ok"], report["status"] = False, "fail"
            # Keep the root mapping error blocking; do not count its dependent
            # waveform check as another independent missing input.
            severity = "info" if report["waveform_exchange"].get("blocked_by") else "error"
            report["issues"].append({"severity": severity, "role": "pair", "code": "waveform_exchange",
                                     "message": report["waveform_exchange"]["summary"],
                                     "blocked_by": report["waveform_exchange"].get("blocked_by", [])})
            report["summary"][severity] = report["summary"].get(severity, 0) + 1
        return report

    def import_bundle(self, payload):
        payload = _mapping(payload, "bundle payload")
        mode = str(payload.get("format", payload.get("bundle_format", ""))).lower()
        path = payload.get("path")
        if path is not None and os.path.isfile(os.path.join(_absolute_path(path, "bundle path"), SWEEP_STATE)):
            from .offset_sweep import import_offset_sweep
            return import_offset_sweep(payload)
        if mode in ("dual_tb", "canonical", "two_tb", "two-testbench"):
            return import_dual_tb_bundle(payload)
        if path is not None:
            candidate = _absolute_path(path, "bundle path")
            if os.path.isdir(candidate) and not os.path.isfile(
                os.path.join(candidate, "summary.csv")
            ):
                direct_dual = os.path.isfile(os.path.join(candidate, "vco_results.csv")) and os.path.isfile(
                    os.path.join(candidate, "replica_results.csv")
                )
                if direct_dual:
                    return import_dual_tb_bundle(payload)
        return import_csv_bundle(payload)

    def import_dual_tb(self, payload):
        return import_dual_tb_bundle(payload)

    def import_as_job(self, payload):
        """Import synchronously and retain the result behind the normal job API."""
        payload = _mapping(payload, "bundle payload")
        job_id = uuid.uuid4().hex
        now = utc_now()
        # A user-selected bundle is replay evidence, never job-owned signoff
        # evidence.  Only the live orchestrator's provenance validator may
        # grant signoff after checking current-job ownership, timestamps,
        # hashes and PSF completion.
        import_payload = copy.deepcopy(payload)
        import_payload["signoff"] = False
        result = normalize_result(self.import_bundle(import_payload))
        source_claims = {
            "run_kind": result.get("run_kind"),
            "signoff": result.get("signoff"),
            "provenance": copy.deepcopy(result.get("provenance")),
        }
        dual_tb = result.get("dual_tb")
        if isinstance(dual_tb, Mapping):
            source_claims["dual_tb_signoff"] = dual_tb.get("signoff")
            source_claims["metadata_run_kind"] = (
                dual_tb.get("metadata", {}).get("run_kind")
                if isinstance(dual_tb.get("metadata"), Mapping)
                else None
            )
            source_claims["metadata_signoff"] = (
                dual_tb.get("metadata", {}).get("signoff")
                if isinstance(dual_tb.get("metadata"), Mapping)
                else None
            )
            dual_tb = copy.deepcopy(dict(dual_tb))
            dual_tb["signoff"] = False
            metadata = dual_tb.get("metadata")
            if isinstance(metadata, Mapping):
                metadata = copy.deepcopy(dict(metadata))
                metadata["run_kind"] = "replay"
                metadata["signoff"] = False
                dual_tb["metadata"] = metadata
            result["dual_tb"] = dual_tb
        result["run_kind"] = "replay"
        result["signoff"] = False
        result["provenance"] = None
        result["source_claims"] = source_claims
        events = [
            {
                "sequence": 1,
                "job_id": job_id,
                "stage": "import",
                "status": "succeeded",
                "progress": 90.0,
                "message": "Result bundle imported",
                "details": {},
                "timestamp": now,
            },
            {
                "sequence": 2,
                "job_id": job_id,
                "stage": "complete",
                "status": "succeeded",
                "progress": 100.0,
                "message": "Import completed",
                "details": {},
                "timestamp": now,
            },
        ]
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "status": "succeeded",
                "progress": 100.0,
                "message": "Import completed",
                "created_at": now,
                "started_at": now,
                "finished_at": now,
                "updated_at": now,
                "background": False,
                "events": events,
                "result": result,
                "error": None,
            }
        self._persist_job(job_id)
        return {
            "ok": True,
            "job_id": job_id,
            "points": len(result.get("points", {})),
            "summary_rows": len(result.get("summary", [])),
        }

    def _on_event(self, job_id, event):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["events"].append(copy.deepcopy(event))
            job["progress"] = event["progress"]
            job["message"] = event["message"]
            job["updated_at"] = event["timestamp"]
        self._persist_job(job_id)

    def _execute_job(self, job_id, payload, config_snapshot):
        recorder = EventRecorder(
            job_id, on_event=lambda event: self._on_event(job_id, event)
        )
        with self._lock:
            job = self._jobs[job_id]
            job["status"] = "running"
            job["started_at"] = utc_now()
            job["updated_at"] = job["started_at"]
        try:
            recorder.emit("queued", "succeeded", 0, "Job started")
            if is_sweep(config_snapshot):
                result = run_offset_sweep(self, job_id, payload, config_snapshot, recorder.emit)
            else:
                orchestrator = DualTBOrchestrator(
                    config=config_snapshot, command_runner=self._runner
                )
                result = orchestrator.run(job_id, payload, emit=recorder.emit)
            with self._lock:
                job = self._jobs[job_id]
                job["result"] = normalize_result(result)
                job["status"] = "succeeded"
                job["progress"] = 100.0
                job["finished_at"] = utc_now()
                job["updated_at"] = job["finished_at"]
                job["message"] = "Run completed"
            self._persist_job(job_id)
        except Exception as exc:
            error = exc.to_dict() if isinstance(exc, CoreError) else {
                "code": "ISF-E9000",
                "kind": "internal_error",
                "message": str(exc),
                "details": {},
            }
            if not isinstance(exc, CoreError):
                from .error_catalog import enrich_error

                try:
                    error = enrich_error(error)
                except Exception:
                    pass
            try:
                recorder.emit("run", "failed", recorder.progress, error["message"])
            except Exception:
                pass
            with self._lock:
                job = self._jobs[job_id]
                job["error"] = error
                job["status"] = "interrupted" if isinstance(exc, OffsetSweepStopped) else "failed"
                job["finished_at"] = utc_now()
                job["updated_at"] = job["finished_at"]
                job["message"] = error["message"]
            self._persist_job(job_id)
        finally:
            with self._lock:
                self._threads.pop(job_id, None)

    def start_run(self, payload):
        payload = copy.deepcopy(_mapping(payload or {}, "run payload"))
        embedded = payload.get("config")
        with self._lock:
            config_snapshot = copy.deepcopy(self._config)
        if isinstance(embedded, Mapping):
            config_snapshot = _deep_merge(config_snapshot, embedded)
        payload.pop("config", None)
        config_snapshot = _seal_run_contract(config_snapshot, payload)
        config_snapshot = resolve_config(config_snapshot, workspace=self.workspace, **self._environment_options)
        # The resolved snapshot is authoritative from this point onward.
        # Keeping the raw embedded object in the payload would let the
        # orchestrator shallow-merge it a second time and erase generated
        # commands, detected paths or nested adapter fields.
        execution = config_snapshot.get("execution", {})
        requested_dry_run = (
            execution.get("dry_run", True)
            if isinstance(execution, Mapping)
            else True
        )
        require_preflight = bool(
            payload.get("require_preflight", False)
            or not bool(requested_dry_run)
            or (
                isinstance(execution, Mapping)
                and execution.get("require_preflight", False)
            )
        )
        if require_preflight:
            report = run_preflight(config_snapshot, workspace=self.workspace, **self._environment_options)
            if not report.get("ok"):
                failed = [
                    item
                    for item in report.get("checks", [])
                    if item.get("status") == "fail"
                ]
                raise PreflightError(
                    "run blocked by preflight",
                    details={
                        "failed_checks": failed,
                        "required_user_fields": report.get(
                            "required_user_fields", []
                        ),
                    },
                )
            resolved_after_preflight = report.get("resolved_config")
            if isinstance(resolved_after_preflight, Mapping):
                config_snapshot = copy.deepcopy(dict(resolved_after_preflight))
                execution = config_snapshot.get("execution", {})
        payload.pop("require_preflight", None)
        payload.pop("discovery_job_id", None)
        default_background = (
            execution.get("background", False) if isinstance(execution, Mapping) else False
        )
        background = payload.get("background", default_background)
        if not isinstance(background, bool):
            raise ValidationError("background must be a boolean")
        job_id = uuid.uuid4().hex
        if self.workspace:
            job_root = os.path.join(self.workspace, "jobs", job_id)
            if payload.get("run_root") in (None, ""):
                payload["run_root"] = os.path.join(job_root, "simulation")
            if payload.get("bundle_path") in (None, ""):
                payload["bundle_path"] = os.path.join(job_root, "canonical")
        run_root = payload.get("run_root")
        if run_root not in (None, ""):
            run_root = _absolute_path(run_root, "run root")
            payload["run_root"] = run_root
            app_config_path = os.path.join(
                run_root, ".isf_resolved_config.json"
            )
            payload["app_config_path"] = app_config_path
        now = utc_now()
        with self._lock:
            active = self._active_job_ids_locked()
            if active:
                raise ValidationError(
                    "another job is already queued or running",
                    details={"active_job_ids": active},
                )
            self._jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "progress": 0.0,
                "message": "Job queued",
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "updated_at": now,
                "background": background,
                "events": [],
                "result": None,
                "error": None,
                "_request": copy.deepcopy(payload),
                "_config_snapshot": copy.deepcopy(config_snapshot),
            }
        try:
            if run_root not in (None, ""):
                if not os.path.isdir(run_root):
                    os.makedirs(run_root)
                detected_engine = config_snapshot.get("detected_engine")
                if isinstance(detected_engine, Mapping) and not config_snapshot.get("config_path"):
                    expected = config_snapshot.get("runtime_identity", {}).get("detected_engine_sha256")
                    if expected != engine_content_sha256(detected_engine):
                        raise PreflightError("detected execution settings changed after preflight")
                    # Materialize only inside this job, after all preflight
                    # gates. No public config file or history is edited.
                    engine_path = os.path.join(run_root, ".isf_detected_engine.json")
                    self._atomic_json(engine_path, detected_engine)
                    old_identity = config_snapshot.get("runtime_identity", {})
                    identity = _runtime_identity(engine_path)
                    identity["files"] = dict(old_identity.get("files", {}), **identity["files"])
                    pair_identity = copy.deepcopy(old_identity.get("maestro_pair", {}))
                    pair_identity["engine_template_path"] = engine_path
                    pair_identity["engine_template_sha256"] = _file_sha256(engine_path)
                    identity["maestro_pair"] = pair_identity
                    config_snapshot["runtime_identity"] = identity
                    config_snapshot["config_path"] = engine_path
                    config_snapshot.setdefault("detection", {}).setdefault("effective_engine_config", {})["template"] = engine_path
                    payload["config_path"] = engine_path
                    with self._lock:
                        self._jobs[job_id]["_config_snapshot"] = copy.deepcopy(config_snapshot)
                        self._jobs[job_id]["_request"] = copy.deepcopy(payload)
                self._atomic_json(app_config_path, config_snapshot)
            self._persist_job(job_id)
        except Exception:
            with self._lock:
                self._jobs.pop(job_id, None)
            raise
        if background:
            try:
                thread = threading.Thread(
                    target=self._execute_job,
                    args=(job_id, copy.deepcopy(payload), config_snapshot),
                    name="isf-job-%s" % job_id[:8],
                )
                thread.daemon = True
                with self._lock:
                    self._threads[job_id] = thread
                thread.start()
            except Exception as exc:
                failure = ValidationError(
                    "cannot start background worker: %s" % exc,
                    details={"job_id": job_id},
                )
                with self._lock:
                    self._threads.pop(job_id, None)
                    job = self._jobs.get(job_id)
                    if job is not None:
                        job["status"] = "failed"
                        job["finished_at"] = utc_now()
                        job["updated_at"] = job["finished_at"]
                        job["message"] = str(failure)
                        job["error"] = failure.to_dict()
                try:
                    self._persist_job(job_id)
                except Exception:
                    pass
                raise failure
        else:
            self._execute_job(job_id, copy.deepcopy(payload), config_snapshot)
        return job_id

    def resume_job(self, job_id, payload=None):
        job_id = str(job_id)
        payload = _mapping(payload or {}, "resume payload")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFoundError("unknown job: %s" % job_id)
            if job.get("status") not in ("failed", "interrupted"):
                raise ResumeError(
                    "only failed or interrupted jobs can be resumed",
                    details={"job_id": job_id, "status": job.get("status")},
                )
            if job_id in self._threads:
                raise ResumeError(
                    "job already has an active worker",
                    details={"job_id": job_id},
                )
            active = self._active_job_ids_locked()
            if active:
                raise ResumeError(
                    "another job is already queued or running",
                    details={"job_id": job_id, "active_job_ids": active},
                )
            request_snapshot = copy.deepcopy(job.get("_request"))
            config_snapshot = copy.deepcopy(job.get("_config_snapshot"))
        if not isinstance(request_snapshot, Mapping) or not isinstance(
            config_snapshot, Mapping
        ):
            raise ResumeError(
                "job has no request snapshot",
                details={"job_id": job_id},
            )
        execution = config_snapshot.get("execution", {})
        resume_enabled = (
            execution.get("resume_enabled", False)
            if isinstance(execution, Mapping)
            else False
        )
        if not isinstance(resume_enabled, bool) or not resume_enabled:
            raise ResumeError(
                "resume is disabled for this job",
                details={"job_id": job_id},
            )
        runtime_identity = config_snapshot.get("runtime_identity")
        live_bundled = bool(
            not execution.get("dry_run", True)
            and str(execution.get("adapter_mode", "bundled")).lower()
            == "bundled"
        )
        identity_errors = (
            _runtime_identity_errors(runtime_identity)
            if runtime_identity or live_bundled
            else {}
        )
        if live_bundled and isinstance(runtime_identity, Mapping):
            if not runtime_identity.get("physical_profile_sha256"):
                identity_errors["physical_profile"] = {
                    "reason": "profile identity is absent"
                }
            records = runtime_identity.get("files", {})
            required_labels = (
                "engine_config",
                "runner",
                "engine",
                "exporter",
                "calculation",
                "dual_tb",
                "physical_profile_module",
                "orchestrator",
                "model",
                "spectre",
                "psf",
            )
            if not isinstance(records, Mapping):
                records = {}
            for label in required_labels:
                record = records.get(label)
                if not isinstance(record, Mapping) or not record.get(
                    "sha256"
                ):
                    identity_errors["required_%s" % label] = {
                        "reason": "required runtime identity is absent"
                    }
            legacy_sources = any(
                str(label).startswith("source_netlist_")
                and isinstance(record, Mapping)
                and record.get("sha256")
                for label, record in records.items()
            )
            pair_identity = runtime_identity.get("maestro_pair") or {}
            pair_bindings = pair_identity.get("bindings") or {}
            pair_sources = all(
                isinstance(pair_bindings.get(role), Mapping)
                and pair_bindings[role].get("binding_fingerprint")
                and pair_bindings[role].get("history_fingerprint")
                and any(str(label).startswith("maestro_%s_source_" % role)
                        and isinstance(record, Mapping) and record.get("sha256")
                        for label, record in records.items())
                for role in ("vco", "flicker")
            )
            if not legacy_sources and not pair_sources:
                identity_errors["source_netlists"] = {
                    "reason": "source netlist identities are absent"
                }
        if identity_errors:
            raise ResumeError(
                "runtime files changed after interruption",
                code="ISF-E5002",
                details={
                    "job_id": job_id,
                    "identity": identity_errors,
                },
            )
        run_root = request_snapshot.get("run_root")
        checkpoint_name = SWEEP_STATE if is_sweep(config_snapshot) else STATE_FILENAME
        state_path = os.path.join(str(run_root), checkpoint_name) if run_root else None
        if not state_path or not os.path.isfile(state_path):
            raise ResumeError(
                "job checkpoint is missing",
                details={"job_id": job_id, "path": state_path},
            )

        request_snapshot = dict(request_snapshot)
        request_snapshot["resume"] = True
        background = payload.get(
            "background", request_snapshot.get("background", job.get("background", True))
        )
        if not isinstance(background, bool):
            raise ValidationError("background must be a boolean")
        request_snapshot["background"] = background
        now = utc_now()
        with self._lock:
            active = self._active_job_ids_locked()
            if active:
                raise ResumeError(
                    "another job is already queued or running",
                    details={"job_id": job_id, "active_job_ids": active},
                )
            job = self._jobs[job_id]
            previous_job = copy.deepcopy(job)
            job["status"] = "queued"
            job["progress"] = 0.0
            job["message"] = "Resume queued"
            job["cancel_requested"] = False
            job["finished_at"] = None
            job["updated_at"] = now
            job["background"] = background
            if not is_sweep(config_snapshot):
                job["result"] = None
            job["error"] = None
            job["_request"] = copy.deepcopy(request_snapshot)
            job["_config_snapshot"] = copy.deepcopy(config_snapshot)
        try:
            self._persist_job(job_id)
            if background:
                thread = threading.Thread(
                    target=self._execute_job,
                    args=(job_id, copy.deepcopy(request_snapshot), config_snapshot),
                    name="isf-resume-%s" % job_id[:8],
                )
                thread.daemon = True
                with self._lock:
                    self._threads[job_id] = thread
                thread.start()
            else:
                self._execute_job(
                    job_id, copy.deepcopy(request_snapshot), config_snapshot
                )
        except Exception:
            with self._lock:
                self._threads.pop(job_id, None)
                self._jobs[job_id] = previous_job
            try:
                self._persist_job(job_id)
            except Exception:
                pass
            raise
        return {"ok": True, "job_id": job_id, "status": "queued"}

    def stop_after_current_offset(self, job_id):
        """Cooperative stop: never kills Cadence or interrupts a scalar job."""
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None:
                raise JobNotFoundError("unknown job: %s" % job_id)
            if job.get("status") not in ("queued", "running") or not is_sweep(job.get("_config_snapshot", {})):
                raise ValidationError("This action applies only to running multi-offset jobs")
            job["cancel_requested"] = True
            job["message"] = "will stop after the current offset completes, completed results will be retained"
        self._persist_job(str(job_id))
        return {"ok": True, "job_id": str(job_id), "stop_after_current_offset": True}

    def get_job(self, job_id):
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None:
                raise JobNotFoundError("unknown job: %s" % job_id)
            value = copy.deepcopy(job)
        value.pop("result", None)
        request_snapshot = value.pop("_request", None)
        config_snapshot = value.pop("_config_snapshot", None) or {}
        value["is_offset_sweep"] = is_sweep(config_snapshot)
        value["offset_sweep"] = None
        if value["is_offset_sweep"]:
            from .offsets import expand_offsets
            completed = (job.get("result") or {}).get("offset_sweep", {}).get("completed_offsets_hz", [])
            value["offset_sweep"] = {
                "offsets_hz": expand_offsets(config_snapshot["analysis"]),
                "completed_offsets_hz": completed,
                "stop_requested": bool(job.get("cancel_requested")),
            }
        value["result_available"] = job["result"] is not None
        run_root = (
            request_snapshot.get("run_root")
            if isinstance(request_snapshot, Mapping)
            else None
        )
        value["resume_available"] = bool(
            value.get("status") in ("failed", "interrupted")
            and run_root
            and any(os.path.isfile(os.path.join(str(run_root), name)) for name in (STATE_FILENAME, SWEEP_STATE))
        )
        stages = []
        stage_index = {}
        for event in value["events"]:
            stage = {
                "key": event["stage"],
                "name": event["stage"],
                "status": event["status"],
                "message": event["message"],
                "progress": event["progress"],
                "timestamp": event["timestamp"],
            }
            if event["stage"] in stage_index:
                stages[stage_index[event["stage"]]] = stage
            else:
                stage_index[event["stage"]] = len(stages)
                stages.append(stage)
        value["stages"] = stages
        return value

    def list_jobs(self):
        with self._lock:
            values = []
            for job in self._jobs.values():
                values.append(
                    {
                        "id": job["id"],
                        "status": job["status"],
                        "message": job.get("message"),
                        "created_at": job.get("created_at"),
                        "updated_at": job.get("updated_at"),
                        "result_available": job.get("result") is not None,
                        "is_offset_sweep": is_sweep(job.get("_config_snapshot") or {}),
                        "resume_available": bool(
                            job.get("status") in ("failed", "interrupted")
                            and isinstance(job.get("_request"), Mapping)
                            and job["_request"].get("run_root")
                            and any(os.path.isfile(os.path.join(
                                str(job["_request"]["run_root"]), name))
                                for name in (STATE_FILENAME, SWEEP_STATE))
                        ),
                    }
                )
        values.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return {"jobs": values}

    def get_result(self, job_id):
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None:
                raise JobNotFoundError("unknown job: %s" % job_id)
            if job["status"] in ("failed", "interrupted") and not (
                isinstance(job.get("result"), Mapping) and job["result"].get("offset_sweep")
            ):
                raise JobFailedError(
                    "job failed: %s" % job_id, details=copy.deepcopy(job["error"] or {})
                )
            if job["result"] is None:
                raise JobNotReadyError(
                    "job result is not ready: %s" % job_id,
                    details={"status": job["status"]},
                )
            return copy.deepcopy(job["result"])
