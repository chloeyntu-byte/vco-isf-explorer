"""Automatic two-testbench orchestration skeleton."""

from __future__ import unicode_literals

import copy
import csv
import hashlib
import json
import os
import re
import time

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover
    from collections import Mapping

from .adapters import (
    TEMPLATE_VARIABLE_WHITELIST,
    adapter_from_config,
    validate_template_context,
)
from .bundle import import_csv_bundle
from .discovery import discover_result_directories, inspect_rdb
from .dual_tb import import_dual_tb_bundle
from .errors import AdapterExecutionError, ValidationError
from .models import annotate_result, empty_result, merge_results, normalize_result
from .physical_profile import load_engine_config, resolve_physical_profile
from .run_state import RunState


DEFAULT_TESTBENCHES = (
    {"name": "vco"},
    {"name": "replica"},
)
MIN_SIGNOFF_POINTS = 6


def _mapping(value, label):
    if not isinstance(value, Mapping):
        raise ValidationError("%s must be an object" % label)
    return dict(value)


def _boolean(value, label):
    if not isinstance(value, bool):
        raise ValidationError("%s must be a boolean" % label)
    return value


def _testbenches(value):
    if value is None:
        return [dict(item) for item in DEFAULT_TESTBENCHES]
    if isinstance(value, Mapping):
        items = []
        for name, raw_spec in value.items():
            if raw_spec is None:
                raw_spec = {}
            spec = _mapping(raw_spec, "testbench %s" % name)
            spec.setdefault("name", str(name))
            items.append(spec)
    elif isinstance(value, (list, tuple)):
        items = []
        for index, raw_spec in enumerate(value):
            if isinstance(raw_spec, str):
                raw_spec = {"name": raw_spec}
            items.append(_mapping(raw_spec, "testbench %d" % (index + 1)))
    else:
        raise ValidationError("testbenches must be an object or list")
    if len(items) != 2:
        raise ValidationError("automatic orchestration requires exactly two testbenches")
    names = []
    for index, item in enumerate(items):
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ValidationError("testbench %d requires a name" % (index + 1))
        names.append(name)
    if len(set(names)) != 2:
        raise ValidationError("testbench names must be unique")
    return items


def _bundle_import(payload, signoff=None):
    if isinstance(payload, Mapping):
        options = dict(payload)
        if signoff is not None:
            # The orchestration run kind is authoritative.  In particular, a
            # formal run must not be downgraded by a bundle payload that embeds
            # ``signoff: false`` and would otherwise bypass signoff gates.
            options["signoff"] = bool(signoff)
        mode = str(payload.get("format", payload.get("mode", ""))).lower()
        if mode in ("dual_tb", "canonical", "two_tb", "two-testbench"):
            return import_dual_tb_bundle(options)
        path = payload.get("path")
        if path not in (None, "") and os.path.isdir(str(path)):
            if (
                os.path.isfile(os.path.join(str(path), "vco_results.csv"))
                and os.path.isfile(os.path.join(str(path), "replica_results.csv"))
            ):
                return import_dual_tb_bundle(options)
    return import_csv_bundle(payload)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _tree_identity(root):
    digest = hashlib.sha256()
    count = 0
    size = 0
    for parent, directories, files in os.walk(root):
        directories.sort()
        for name in sorted(files):
            path = os.path.join(parent, name)
            if not os.path.isfile(path):
                continue
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            with open(path, "rb") as stream:
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    size += len(block)
            digest.update(b"\0")
            count += 1
    return {"sha256": digest.hexdigest(), "files": count, "bytes": size}


def _validate_bundle_provenance(
    bundle_payload,
    job_id,
    run_started,
    run_root,
    run_kind,
    run_config=None,
):
    if not isinstance(bundle_payload, Mapping) or bundle_payload.get("path") in (None, ""):
        raise AdapterExecutionError(
            "automatic execution requires a canonical bundle directory",
            code="ISF-E3001",
            details={"bundle": bundle_payload},
        )
    path = os.path.realpath(os.path.abspath(os.path.expanduser(str(bundle_payload["path"]))))
    required = (
        "vco_results.csv", "replica_results.csv", "workpoint.csv",
        "metadata.json", "manifest.json",
    )
    missing = [name for name in required if not os.path.isfile(os.path.join(path, name))]
    if missing:
        raise AdapterExecutionError(
            "canonical extraction is incomplete",
            code="ISF-E3001",
            details={"path": path, "missing": missing},
        )
    stale = [
        name for name in required
        if os.path.getmtime(os.path.join(path, name)) < run_started - 2.0
    ]
    if stale:
        raise AdapterExecutionError(
            "canonical bundle predates the current job",
            code="ISF-E3002",
            details={"path": path, "stale": stale},
        )
    with open(os.path.join(path, "metadata.json"), "r") as stream:
        metadata = json.load(stream)
    with open(os.path.join(path, "manifest.json"), "r") as stream:
        manifest = json.load(stream)
    if metadata.get("job_id") != job_id or manifest.get("job_id") != job_id:
        raise AdapterExecutionError(
            "canonical bundle does not belong to the current job",
            code="ISF-E3002",
            details={
                "job_id": job_id,
                "metadata_job_id": metadata.get("job_id"),
                "manifest_job_id": manifest.get("job_id"),
            },
        )
    try:
        generated = float(metadata.get("generated_at_epoch"))
    except (TypeError, ValueError):
        generated = float("nan")
    if not (generated >= run_started - 2.0 and generated <= time.time() + 5.0):
        raise AdapterExecutionError(
            "canonical bundle generation timestamp is outside the current job",
            code="ISF-E3002",
            details={"generated_at_epoch": metadata.get("generated_at_epoch")},
        )
    manifest_files = manifest.get("files", {})
    hash_errors = {}
    verified_files = []
    for name in required:
        file_path = os.path.join(path, name)
        actual = _sha256(file_path)
        actual_bytes = os.path.getsize(file_path)
        if name != "manifest.json":
            expected = manifest_files.get(name, {}).get("sha256")
            expected_bytes = manifest_files.get(name, {}).get("bytes")
            if expected != actual or expected_bytes != actual_bytes:
                hash_errors[name] = {
                    "expected": expected,
                    "actual": actual,
                    "expected_bytes": expected_bytes,
                    "actual_bytes": actual_bytes,
                }
        verified_files.append(
            {
                "name": name,
                "path": file_path,
                "sha256": actual,
                "bytes": actual_bytes,
                "status": "verified",
            }
        )
    if hash_errors:
        raise AdapterExecutionError(
            "canonical bundle hash verification failed",
            code="ISF-E3002",
            details={"files": hash_errors},
        )

    psf_runs = metadata.get("psf_runs", [])
    if run_kind == "signoff":
        source_root = os.path.realpath(str(metadata.get("source_run_root", "")))
        expected_root = os.path.realpath(str(run_root or ""))
        if not expected_root or source_root != expected_root:
            raise AdapterExecutionError(
                "signoff bundle does not originate from this job's simulation root",
                code="ISF-E3002",
                details={"source_run_root": source_root, "expected": expected_root},
            )
        if (
            metadata.get("run_kind") != "signoff"
            or metadata.get("signoff") is not True
            or not isinstance(psf_runs, list)
            or not psf_runs
        ):
            raise AdapterExecutionError(
                "signoff bundle lacks PSF provenance", code="ISF-E3002"
            )
        required_identity = (
            "engine_sha256",
            "config_sha256",
            "physical_profile_id",
            "physical_profile_sha256",
            "runtime_identity",
        )
        missing_identity = [
            key for key in required_identity if not metadata.get(key)
        ]
        if (
            missing_identity
            or metadata.get("physical_profile_explicit") is not True
        ):
            raise AdapterExecutionError(
                "signoff bundle lacks an explicit physical profile identity",
                code="ISF-E3002",
                details={"missing": missing_identity},
            )
        config_path = os.path.realpath(str(metadata.get("config", "")))
        engine_path = os.path.realpath(str(metadata.get("engine", "")))
        identity_errors = {}
        for label, file_path, metadata_key in (
            ("config", config_path, "config_sha256"),
            ("engine", engine_path, "engine_sha256"),
        ):
            if not os.path.isfile(file_path):
                identity_errors[label] = {
                    "path": file_path,
                    "reason": "file is absent",
                }
                continue
            actual_hash = _sha256(file_path)
            if actual_hash != metadata.get(metadata_key):
                identity_errors[label] = {
                    "path": file_path,
                    "expected": metadata.get(metadata_key),
                    "actual": actual_hash,
                }
        runtime_identity = (
            run_config.get("runtime_identity", {})
            if isinstance(run_config, Mapping)
            else {}
        )
        metadata_runtime_identity = metadata.get("runtime_identity", {})
        execution = (
            run_config.get("execution", {})
            if isinstance(run_config, Mapping)
            else {}
        )
        bundled = (
            isinstance(run_config, Mapping)
            and bool(runtime_identity)
            and isinstance(execution, Mapping)
            and str(execution.get("adapter_mode", "bundled")).lower()
            == "bundled"
        )
        expected_files = (
            runtime_identity.get("files", {})
            if bundled and isinstance(runtime_identity, Mapping)
            else {}
        )
        if bundled and not isinstance(expected_files, Mapping):
            identity_errors["runtime_files"] = {
                "reason": "preflight runtime file identity is absent"
            }
            expected_files = {}
        source_mode = str(
            run_config.get("source_mode", "")
            if isinstance(run_config, Mapping)
            else ""
        ).lower()
        pair_runtime = (
            runtime_identity.get("maestro_pair", {})
            if isinstance(runtime_identity, Mapping)
            else {}
        )
        pair_mode = bool(
            bundled
            and source_mode in ("maestro_pair", "maestro-pair", "pair")
            and isinstance(pair_runtime, Mapping)
            and pair_runtime
        )
        required_runtime_labels = set((
            "engine_config",
            "runner",
            "engine",
            "exporter",
            "calculation",
            "dual_tb",
            "physical_profile_module",
            "source_paths_module",
            "orchestrator",
            "model",
            "spectre",
            "psf",
        ))
        if bundled:
            required_runtime_labels.update(expected_files)
        if pair_mode:
            required_runtime_labels.update(
                (
                    "engine_template",
                    "maestro_source_manifest",
                    "private_flicker_template",
                )
            )
        if not isinstance(metadata_runtime_identity, Mapping):
            identity_errors["runtime_identity"] = {
                "reason": "metadata runtime identity is invalid"
            }
            metadata_runtime_identity = {}
        for label in sorted(required_runtime_labels):
            record = metadata_runtime_identity.get(label)
            if not isinstance(record, Mapping):
                identity_errors["runtime_%s" % label] = {
                    "reason": "identity record is absent"
                }
                continue
            file_path = os.path.realpath(str(record.get("path", "")))
            expected_hash = record.get("sha256")
            if not os.path.isfile(file_path):
                identity_errors["runtime_%s" % label] = {
                    "path": file_path,
                    "reason": "file is absent",
                }
                continue
            actual_hash = _sha256(file_path)
            if not expected_hash or actual_hash != expected_hash:
                identity_errors["runtime_%s" % label] = {
                    "path": file_path,
                    "expected": expected_hash,
                    "actual": actual_hash,
                }
        if bundled:
            if pair_mode:
                expected_template = os.path.realpath(
                    str(pair_runtime.get("engine_template_path", ""))
                )
                actual_template_record = metadata_runtime_identity.get(
                    "engine_template", {}
                )
                actual_template = os.path.realpath(
                    str(actual_template_record.get("path", ""))
                )
                if expected_template != actual_template:
                    identity_errors["engine_template_path"] = {
                        "expected": expected_template,
                        "actual": actual_template,
                    }
                expected_template_hash = pair_runtime.get(
                    "engine_template_sha256"
                )
                if expected_template_hash != actual_template_record.get("sha256"):
                    identity_errors["engine_template_sha256"] = {
                        "expected": expected_template_hash,
                        "actual": actual_template_record.get("sha256"),
                    }
                materialized_record = metadata_runtime_identity.get(
                    "engine_config", {}
                )
                if os.path.realpath(
                    str(materialized_record.get("path", ""))
                ) != config_path:
                    identity_errors["materialized_config_path"] = {
                        "expected": config_path,
                        "actual": materialized_record.get("path"),
                    }
                expected_bindings = pair_runtime.get("bindings", {})
                actual_bindings = metadata.get("maestro_pair_binding", {})
                if pair_runtime.get("replica_source") != actual_bindings.get("replica_source"):
                    identity_errors["maestro_replica_source"] = {
                        "expected": pair_runtime.get("replica_source"),
                        "actual": actual_bindings.get("replica_source"),
                    }
                for role in ("vco", "flicker"):
                    expected_binding = expected_bindings.get(role) or {}
                    actual_binding = actual_bindings.get(role) or {}
                    for key in (
                        "maestro_dir",
                        "test",
                        "binding_fingerprint",
                        "history",
                        "history_fingerprint",
                    ):
                        if expected_binding.get(key) != actual_binding.get(key):
                            identity_errors[
                                "maestro_pair_{}_{}".format(role, key)
                            ] = {
                                "expected": expected_binding.get(key),
                                "actual": actual_binding.get(key),
                            }
            else:
                expected_config = os.path.realpath(
                    str(runtime_identity.get("engine_config_path", ""))
                )
                if config_path != expected_config:
                    identity_errors["config_path"] = {
                        "expected": expected_config,
                        "actual": config_path,
                    }
                expected_hash = runtime_identity.get("engine_config_sha256")
                if expected_hash != metadata.get("config_sha256"):
                    identity_errors["engine_config_sha256"] = {
                        "expected": expected_hash,
                        "actual": metadata.get("config_sha256"),
                    }
            expected_engine_hash = runtime_identity.get("engine_sha256")
            if expected_engine_hash != metadata.get("engine_sha256"):
                identity_errors["engine_sha256"] = {
                    "expected": expected_engine_hash,
                    "actual": metadata.get("engine_sha256"),
                }
            for label in sorted(required_runtime_labels):
                expected_record = expected_files.get(label)
                actual_label = (
                    "engine_template"
                    if pair_mode and label == "engine_config"
                    else label
                )
                actual_record = metadata_runtime_identity.get(actual_label)
                if pair_mode and label in (
                    "engine_template",
                    "maestro_source_manifest",
                    "private_flicker_template",
                ) and not isinstance(expected_record, Mapping):
                    # These files are created after Preflight.  They were
                    # hash-checked above as runtime-owned artifacts and have no
                    # preflight path by design.
                    continue
                if not isinstance(expected_record, Mapping) or not isinstance(
                    actual_record, Mapping
                ):
                    identity_errors["preflight_%s" % label] = {
                        "reason": "runtime identity record is absent"
                    }
                    continue
                expected_path = os.path.realpath(
                    str(expected_record.get("path", ""))
                )
                actual_path = os.path.realpath(
                    str(actual_record.get("path", ""))
                )
                if (
                    expected_path != actual_path
                    or expected_record.get("sha256")
                    != actual_record.get("sha256")
                ):
                    identity_errors["preflight_%s" % label] = {
                        "expected": expected_record,
                        "actual": actual_record,
                    }
        if not identity_errors:
            try:
                profile_info = resolve_physical_profile(
                    load_engine_config(config_path),
                    config_path=config_path,
                    allow_legacy=False,
                )
                if (
                    profile_info["sha256"]
                    != metadata.get("physical_profile_sha256")
                    or profile_info["profile"]["profile_id"]
                    != metadata.get("physical_profile_id")
                ):
                    identity_errors["physical_profile"] = {
                        "expected_id": metadata.get("physical_profile_id"),
                        "actual_id": profile_info["profile"]["profile_id"],
                        "expected_sha256": metadata.get(
                            "physical_profile_sha256"
                        ),
                        "actual_sha256": profile_info["sha256"],
                    }
            except Exception as exc:
                identity_errors["physical_profile"] = {"reason": str(exc)}
        if identity_errors:
            raise AdapterExecutionError(
                "signoff engine/profile identity verification failed",
                code="ISF-E3002",
                details={"identity": identity_errors},
            )
        with open(os.path.join(path, "vco_results.csv"), "r") as stream:
            points = set(
                str(row.get("point", "")).strip()
                for row in csv.DictReader(stream)
                if str(row.get("point", "")).strip()
            )
        if len(points) < MIN_SIGNOFF_POINTS:
            raise AdapterExecutionError(
                "formal signoff requires at least %d design points"
                % MIN_SIGNOFF_POINTS,
                code="ISF-E3001",
                details={"points": sorted(points), "count": len(points)},
            )
        expected_pairs = set(
            (point, role) for point in points for role in ("vco", "replica")
        )
        observed_pairs = set()
        for record in psf_runs:
            psf_dir = os.path.realpath(str(record.get("psf_dir", "")))
            netlist_path = os.path.realpath(
                str(record.get("netlist_path", ""))
            )
            pair = (str(record.get("point", "")), str(record.get("role", "")))
            if pair in observed_pairs:
                raise AdapterExecutionError(
                    "duplicate PSF provenance record",
                    code="ISF-E3002",
                    details={"record": record},
                )
            observed_pairs.add(pair)
            try:
                inside = os.path.commonpath((psf_dir, expected_root)) == expected_root
            except ValueError:
                inside = False
            sim_done = os.path.join(psf_dir, ".simDone")
            spectre_out = os.path.join(psf_dir, "spectre.out")
            expected_netlist_path = os.path.realpath(
                os.path.join(psf_dir, os.pardir, "netlist", "input.scs")
            )
            exists = os.path.isfile(sim_done) and os.path.isfile(spectre_out)
            fresh = exists and min(
                os.path.getmtime(sim_done), os.path.getmtime(spectre_out)
            ) >= run_started - 2.0
            actual_hash = _sha256(spectre_out) if exists else None
            hash_matches = actual_hash == record.get("spectre_out_sha256")
            actual_netlist_hash = (
                _sha256(netlist_path) if os.path.isfile(netlist_path) else None
            )
            netlist_matches = (
                netlist_path == expected_netlist_path
                and actual_netlist_hash == record.get("netlist_sha256")
            )
            actual_psf_identity = (
                _tree_identity(psf_dir) if os.path.isdir(psf_dir) else None
            )
            psf_tree_matches = bool(
                actual_psf_identity
                and actual_psf_identity["sha256"]
                == record.get("psf_tree_sha256")
                and actual_psf_identity["files"]
                == record.get("psf_file_count")
                and actual_psf_identity["bytes"]
                == record.get("psf_bytes")
            )
            log_text = ""
            if exists:
                with open(spectre_out, "r", errors="replace") as stream:
                    log_text = stream.read()
            zero_errors = bool(
                re.search(r"spectre\s+completes\s+with\s+0\s+errors?", log_text, re.I)
            )
            fatal = bool(re.search(r"\bfatal\b|^\s*error\b", log_text, re.I | re.M))
            if (
                not inside
                or not fresh
                or not hash_matches
                or not netlist_matches
                or not psf_tree_matches
                or not zero_errors
                or fatal
                or record.get("zero_errors") is not True
            ):
                raise AdapterExecutionError(
                    "signoff PSF provenance is stale, external, or incomplete",
                    code="ISF-E3002",
                    details={
                        "record": record,
                        "run_root": expected_root,
                        "actual_spectre_sha256": actual_hash,
                        "netlist_path": netlist_path,
                        "expected_netlist_path": expected_netlist_path,
                        "expected_netlist_sha256": record.get(
                            "netlist_sha256"
                        ),
                        "actual_netlist_sha256": actual_netlist_hash,
                        "expected_psf_tree_sha256": record.get(
                            "psf_tree_sha256"
                        ),
                        "actual_psf_identity": actual_psf_identity,
                        "actual_zero_errors": zero_errors,
                        "actual_fatal": fatal,
                    },
                )
        if observed_pairs != expected_pairs:
            raise AdapterExecutionError(
                "signoff PSF provenance does not cover every point and role",
                code="ISF-E3002",
                details={
                    "missing": sorted(expected_pairs - observed_pairs),
                    "unexpected": sorted(observed_pairs - expected_pairs),
                },
            )
    return {
        "path": path,
        "job_id": job_id,
        "run_kind": run_kind,
        "generated_at_epoch": generated,
        "manifest_sha256": _sha256(os.path.join(path, "manifest.json")),
        "psf_runs": len(psf_runs) if isinstance(psf_runs, list) else 0,
        "files": verified_files,
    }


class DualTBOrchestrator(object):
    def __init__(self, config=None, command_runner=None):
        self.config = dict(config or {})
        self.command_runner = command_runner

    def _adapter_config(self, tb, index, payload, run_config):
        adapter = tb.get("adapter")
        execution = run_config.get("execution", {})
        if adapter is None and isinstance(execution, Mapping):
            adapters = execution.get("adapters", {})
            if isinstance(adapters, Mapping):
                adapter = adapters.get(tb.get("name"))
        if adapter is None and isinstance(execution, Mapping):
            adapter = execution.get("adapter")

        auto = run_config.get("auto", {})
        if adapter is None and isinstance(auto, Mapping):
            command_key = "vco_command" if index == 0 else "replica_command"
            command = auto.get(command_key)
            submitter = auto.get("submitter")
            if command and submitter:
                adapter = {
                    "kind": submitter,
                    "command": command,
                    "timeout": auto.get("timeout_s", 7200),
                }
        return adapter

    def _context(self, job_id, tb, index, payload):
        supplied = payload.get("template_vars", {})
        values = validate_template_context(supplied)

        def first(*candidates):
            for candidate in candidates:
                if candidate not in (None, ""):
                    return candidate
            return ""

        name = tb["name"]
        builtins = {
            "job_id": job_id,
            "tb": name,
            "tb_name": name,
            "testbench": name,
            "tb_index": index,
            "test": first(tb.get("test"), tb.get("path"), name),
            "run_simulation": first(tb.get("run_simulation"), payload.get("run_simulation")),
            "run_dir": first(tb.get("run_dir"), payload.get("run_dir")),
            "run_root": first(payload.get("run_root"), payload.get("run_dir")),
            "netlist_dir": first(tb.get("netlist_dir"), payload.get("netlist_dir")),
            "work_dir": first(tb.get("work_dir"), payload.get("work_dir")),
            "output_dir": first(tb.get("output_dir"), payload.get("output_dir")),
            "result_dir": first(tb.get("result_dir"), payload.get("result_dir")),
            "results_dir": first(tb.get("results_dir"), payload.get("results_dir")),
            "config_path": first(payload.get("config_path")),
            "bundle_path": first(payload.get("bundle_path")),
            "project_dir": first(payload.get("project_dir"), payload.get("project_root")),
            "maestro_dir": first(tb.get("maestro_dir"), payload.get("maestro_dir")),
            "rdb_dir": first(tb.get("rdb_dir"), payload.get("rdb_dir")),
            "psf_dir": first(tb.get("psf_dir"), payload.get("psf_dir")),
            "vco_result": first(payload.get("vco_result")),
            "replica_workspace": first(payload.get("replica_workspace")),
            "point": first(tb.get("point"), payload.get("point")),
            "corner": first(tb.get("corner"), payload.get("corner")),
            "submission_key": "%s:%s" % (job_id, name),
        }
        values.update(builtins)
        return dict((key, values[key]) for key in TEMPLATE_VARIABLE_WHITELIST if key in values)

    def _exchange_adapter_config(self, payload, run_config):
        adapter = None
        execution = run_config.get("execution", {})
        auto = run_config.get("auto", {})
        if adapter is None and isinstance(execution, Mapping):
            adapters = execution.get("adapters", {})
            if isinstance(adapters, Mapping):
                adapter = adapters.get("exchange")
            if adapter is None:
                adapter = execution.get("exchange_adapter")
        if adapter is None and isinstance(auto, Mapping):
            adapter = auto.get("exchange_adapter")
            command = auto.get("exchange_command")
            if adapter is None and command:
                adapter = {
                    "kind": "runSimulation" if isinstance(command, (list, tuple)) else "custom_shell",
                    "command": command,
                    "timeout": auto.get("timeout_s", 7200),
                }
        return adapter

    def _extract_adapter_config(self, payload, run_config):
        adapter = None
        execution = run_config.get("execution", {})
        auto = run_config.get("auto", {})
        if adapter is None and isinstance(execution, Mapping):
            adapters = execution.get("adapters", {})
            if isinstance(adapters, Mapping):
                adapter = adapters.get("extract")
            if adapter is None:
                adapter = execution.get("extract_adapter")
        if adapter is None and isinstance(auto, Mapping):
            adapter = auto.get("extract_adapter")
            command = auto.get("extract_command")
            if adapter is None and command:
                adapter = {
                    "kind": "runSimulation" if isinstance(command, (list, tuple)) else "custom_shell",
                    "command": command,
                    "timeout": auto.get("timeout_s", 7200),
                }
        return adapter

    def _validate_tb_result(self, tb, dry_run):
        rdb = tb.get("rdb", tb.get("rdb_path"))
        if not rdb:
            return {"checked": False, "reason": "no rdb configured"}
        if dry_run and not os.path.isfile(str(rdb)):
            return {"checked": False, "reason": "dry-run result does not exist", "path": str(rdb)}
        inspected = inspect_rdb({"path": rdb, "verify_completion": True})
        completion = inspected.get("completion", {})
        if not dry_run and (
            not completion.get("checked")
            or completion.get("incomplete")
        ):
            raise AdapterExecutionError(
                "%s result failed completion validation" % tb["name"],
                code="ISF-E2002",
                details={"rdb": rdb, "completion": completion},
            )
        return {
            "checked": True,
            "path": inspected.get("path"),
            "completion": completion,
            "runs": inspected.get("runs", []),
        }

    def run(self, job_id, payload, emit=None):
        payload = _mapping(payload, "run payload")
        run_config = copy.deepcopy(self.config)
        context_payload = copy.deepcopy(run_config)
        context_payload.update(payload)

        def event(stage, status, progress, message, details=None):
            if emit is not None:
                emit(stage, status, progress, message, details or {})

        event("validate", "running", 5, "Validating two-testbench run")
        configured_tbs = run_config.get("testbenches")
        testbenches = _testbenches(configured_tbs)
        execution = run_config.get("execution", {})
        auto = run_config.get("auto", {})
        default_dry_run = True
        if isinstance(execution, Mapping) and "dry_run" in execution:
            default_dry_run = execution["dry_run"]
        elif isinstance(auto, Mapping) and "dry_run" in auto:
            default_dry_run = auto["dry_run"]
        dry_run = _boolean(default_dry_run, "execution.dry_run")
        run_kind = run_config.get("run_kind")
        if run_kind in (None, ""):
            run_kind = "plan" if dry_run else "signoff"
        run_kind = str(run_kind).lower()
        if run_kind not in ("plan", "replay", "signoff"):
            raise ValidationError("run_kind must be plan, replay, or signoff")
        if dry_run and run_kind != "plan":
            run_kind = "plan"
        allow_execute = _boolean(
            execution.get("allow_execute", False)
            if isinstance(execution, Mapping)
            else False,
            "execution.allow_execute",
        )
        if not dry_run and not allow_execute:
            raise ValidationError("external execution is disabled; use dry_run or allow_execute")

        resume_requested = _boolean(payload.get("resume", False), "resume")
        resume_enabled = _boolean(
            execution.get("resume_enabled", False)
            if isinstance(execution, Mapping)
            else False,
            "execution.resume_enabled",
        )
        if resume_requested and not resume_enabled:
            from .errors import ResumeError

            raise ResumeError(
                "resume is disabled by the execution profile",
                details={"field": "execution.resume_enabled"},
            )
        checkpoint = None
        if resume_enabled or resume_requested:
            run_root = context_payload.get("run_root")
            if run_root not in (None, ""):
                checkpoint = RunState(
                    run_root,
                    job_id,
                    run_config,
                    payload,
                    resume=resume_requested,
                )
            elif resume_requested:
                from .errors import ResumeError

                raise ResumeError("resume requires the original run_root")
        run_started = (
            checkpoint.run_started_epoch if checkpoint is not None else time.time()
        )
        event("validate", "succeeded", 10, "Run contract validated")

        discovery_result = None
        if payload.get("discovery"):
            event("discover", "running", 15, "Discovering configured result roots")
            discovery_result = discover_result_directories(payload["discovery"])
            event("discover", "succeeded", 20, "Discovery completed")

        submissions = []
        imported_results = []

        def invoke_adapter(adapter, context, stage):
            if getattr(adapter, "adapter_name", None) != "protocol":
                return adapter.submit(context, dry_run=dry_run)
            resume_metadata = (
                checkpoint.progress(stage)
                if resume_requested and checkpoint is not None
                else None
            )

            def persist_progress(value):
                if checkpoint is not None:
                    checkpoint.mark_progress(stage, value)

            return adapter.submit(
                context,
                dry_run=dry_run,
                resume=resume_metadata,
                on_progress=persist_progress,
            )

        def submit_tb(index, tb, start_progress, finish_progress):
            stage = "submit_%s" % tb["name"]
            event(stage, "running", start_progress, "Submitting %s" % tb["name"])
            restored = bool(
                resume_requested
                and checkpoint is not None
                and checkpoint.completed(stage)
            )
            if restored:
                submission = checkpoint.result(stage)
                if not isinstance(submission, Mapping):
                    from .errors import ResumeError

                    raise ResumeError(
                        "checkpoint stage has no submission result",
                        code="ISF-E5002",
                        details={"stage": stage, "path": checkpoint.path},
                    )
            else:
                adapter_config = self._adapter_config(tb, index, payload, run_config)
                if adapter_config is None:
                    if not dry_run:
                        raise ValidationError("%s requires an execution adapter in live mode" % tb["name"])
                    submission = {
                        "adapter": "none",
                        "status": "planned",
                        "command": [],
                        "dry_run": True,
                        "returncode": None,
                        "stdout": "",
                        "stderr": "",
                        "cwd": None,
                        "metadata": {},
                    }
                else:
                    adapter = adapter_from_config(adapter_config, runner=self.command_runner)
                    submission = invoke_adapter(
                        adapter,
                        self._context(job_id, tb, index, context_payload),
                        stage,
                    )
                    if submission["status"] == "failed":
                        raise AdapterExecutionError(
                            "%s submission failed" % tb["name"],
                            code="ISF-E4005",
                            details={"submission": submission},
                        )
                if checkpoint is not None:
                    checkpoint.mark(stage, submission)
            submission = copy.deepcopy(dict(submission))
            submission["testbench"] = tb["name"]
            submission.setdefault("metadata", {})["restored_from_checkpoint"] = restored
            submissions.append(submission)
            event(
                stage,
                "succeeded",
                finish_progress,
                (
                    "%s restored from checkpoint" % tb["name"]
                    if restored
                    else "%s submission finished" % tb["name"]
                ),
            )
            if tb.get("bundle") is not None:
                if not dry_run and run_kind == "signoff":
                    raise ValidationError("signoff runs cannot import a preconfigured testbench bundle")
                imported_results.append(
                    annotate_result(
                        _bundle_import(tb["bundle"], signoff=run_kind == "signoff"),
                        testbench=tb["name"],
                    )
                )

        vco_tb, replica_tb = testbenches
        submit_tb(0, vco_tb, 22, 34)
        event("validate_vco", "running", 36, "Validating VCO result ownership and completion")
        vco_validation = self._validate_tb_result(vco_tb, dry_run)
        event("validate_vco", "succeeded", 42, "VCO result validation completed", vco_validation)

        exchange_context = copy.deepcopy(context_payload)
        exchange_context.setdefault(
            "vco_result",
            vco_tb.get("result_dir", vco_tb.get("psf_dir", vco_tb.get("rdb", ""))),
        )
        exchange_context.setdefault(
            "replica_workspace",
            replica_tb.get("work_dir", replica_tb.get("run_dir", replica_tb.get("path", ""))),
        )
        exchange_adapter_config = self._exchange_adapter_config(payload, run_config)
        event("exchange", "running", 45, "Exporting the VCO working point to the replica workspace")
        exchange_restored = bool(
            resume_requested
            and checkpoint is not None
            and checkpoint.completed("exchange")
        )
        if exchange_restored:
            exchange_submission = checkpoint.result("exchange")
            if not isinstance(exchange_submission, Mapping):
                from .errors import ResumeError

                raise ResumeError(
                    "checkpoint exchange stage has no result",
                    code="ISF-E5002",
                    details={"path": checkpoint.path},
                )
        else:
            if exchange_adapter_config is None:
                self_exchange = bool(
                    run_config.get("replica_performs_exchange", False)
                )
                if not dry_run and not self_exchange:
                    raise ValidationError("live mode requires an exchange adapter or replica_performs_exchange=true")
                exchange_submission = {
                    "adapter": "none",
                    "status": "planned" if dry_run else "skipped",
                    "command": [],
                    "dry_run": dry_run,
                    "returncode": None,
                    "stdout": "",
                    "stderr": "",
                    "cwd": None,
                    "metadata": {"reason": "replica command performs its own exchange"},
                }
            else:
                exchange_adapter = adapter_from_config(
                    exchange_adapter_config, runner=self.command_runner
                )
                exchange_submission = invoke_adapter(
                    exchange_adapter,
                    self._context(
                        job_id, {"name": "exchange"}, 0, exchange_context
                    ),
                    "exchange",
                )
                if exchange_submission["status"] == "failed":
                    raise AdapterExecutionError(
                        "VCO-to-replica exchange failed",
                        code="ISF-E4005",
                        details={"submission": exchange_submission},
                    )
            if checkpoint is not None:
                checkpoint.mark("exchange", exchange_submission)
        exchange_submission = copy.deepcopy(dict(exchange_submission))
        exchange_submission["testbench"] = "exchange"
        exchange_submission.setdefault("metadata", {})[
            "restored_from_checkpoint"
        ] = exchange_restored
        submissions.append(exchange_submission)
        event(
            "exchange",
            "succeeded",
            55,
            (
                "VCO working-point exchange restored from checkpoint"
                if exchange_restored
                else "VCO working-point exchange completed"
            ),
        )

        submit_tb(1, replica_tb, 58, 70)
        event("validate_replica", "running", 72, "Validating replica result ownership and completion")
        replica_validation = self._validate_tb_result(replica_tb, dry_run)
        event(
            "validate_replica",
            "succeeded",
            78,
            "Replica result validation completed",
            replica_validation,
        )

        bundle_payload = payload.get("bundle")
        if bundle_payload is None and payload.get("bundle_path") not in (None, ""):
            bundle_payload = {"path": payload["bundle_path"]}
        if bundle_payload is None and isinstance(auto, Mapping):
            bundle_payload = auto.get("bundle")
            if bundle_payload is None and auto.get("bundle_path") not in (None, ""):
                bundle_payload = {"path": auto["bundle_path"]}

        extract_context = copy.deepcopy(context_payload)
        if isinstance(bundle_payload, Mapping) and bundle_payload.get("path") not in (None, ""):
            extract_context["bundle_path"] = bundle_payload["path"]

        event("extract", "running", 80, "Extracting exact point/test PSF results")
        extract_adapter_config = self._extract_adapter_config(payload, run_config)
        extract_restored = bool(
            resume_requested
            and checkpoint is not None
            and checkpoint.completed("extract")
        )
        if extract_restored:
            extract_submission = checkpoint.result("extract")
            if not isinstance(extract_submission, Mapping):
                from .errors import ResumeError

                raise ResumeError(
                    "checkpoint extract stage has no result",
                    code="ISF-E5002",
                    details={"path": checkpoint.path},
                )
        else:
            if extract_adapter_config is None:
                replica_produces_bundle = bool(
                    run_config.get("replica_produces_bundle", False)
                )
                if not dry_run and not replica_produces_bundle:
                    raise ValidationError("live mode requires an extract adapter or replica_produces_bundle=true")
                extract_submission = {
                    "adapter": "none",
                    "status": "planned" if dry_run else "skipped",
                    "command": [],
                    "dry_run": dry_run,
                    "returncode": None,
                    "stdout": "",
                    "stderr": "",
                    "cwd": None,
                    "metadata": {"reason": "bundle is produced by the replica command"},
                }
            else:
                extract_adapter = adapter_from_config(
                    extract_adapter_config, runner=self.command_runner
                )
                extract_submission = invoke_adapter(
                    extract_adapter,
                    self._context(
                        job_id, {"name": "extract"}, 0, extract_context
                    ),
                    "extract",
                )
                if extract_submission["status"] == "failed":
                    raise AdapterExecutionError(
                        "PSF extraction failed",
                        code="ISF-E4005",
                        details={"submission": extract_submission},
                    )
            if checkpoint is not None:
                checkpoint.mark("extract", extract_submission)
        extract_submission = copy.deepcopy(dict(extract_submission))
        extract_submission["testbench"] = "extract"
        extract_submission.setdefault("metadata", {})[
            "restored_from_checkpoint"
        ] = extract_restored
        submissions.append(extract_submission)
        event(
            "extract",
            "succeeded",
            88,
            (
                "PSF extraction restored from checkpoint"
                if extract_restored
                else "PSF extraction completed"
            ),
        )

        provenance = None
        if not dry_run:
            provenance = _validate_bundle_provenance(
                bundle_payload,
                job_id,
                run_started,
                context_payload.get("run_root"),
                run_kind,
                run_config,
            )

        event("import", "running", 90, "Importing available result bundles")
        import_bundle_now = bundle_payload is not None
        if dry_run and isinstance(bundle_payload, Mapping):
            planned_path = bundle_payload.get("path")
            if planned_path not in (None, "") and not os.path.exists(str(planned_path)):
                import_bundle_now = False
        if import_bundle_now:
            imported_results.append(
                _bundle_import(bundle_payload, signoff=run_kind == "signoff")
            )
        elif not dry_run and not imported_results:
            raise AdapterExecutionError(
                "automatic run completed without an importable result bundle",
                details={"hint": "configure bundle_path or an extract command that writes it"},
            )
        if len(imported_results) == 1:
            result = normalize_result(imported_results[0])
        elif imported_results:
            result = merge_results(imported_results)
        else:
            result = empty_result()
        result["submissions"] = submissions
        result["testbenches"] = [
            {"name": item["name"], "path": item.get("path")} for item in testbenches
        ]
        result["dry_run"] = dry_run
        result["run_kind"] = run_kind
        result["provenance"] = provenance
        if discovery_result is not None:
            result["discovery"] = discovery_result
        result["validation"] = {"vco": vco_validation, "replica": replica_validation}
        result["extraction"] = extract_submission
        event("import", "succeeded", 96, "Result import completed")
        result = normalize_result(result)
        event("complete", "succeeded", 100, "Two-testbench run completed")
        if checkpoint is not None:
            checkpoint.finish()
        return result


AutomaticDualTBOrchestrator = DualTBOrchestrator
