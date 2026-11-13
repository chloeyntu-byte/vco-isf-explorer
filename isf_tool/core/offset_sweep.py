"""Isolated scalar jobs aggregated along an explicit frequency-offset axis."""
from __future__ import annotations

import copy
from collections.abc import Mapping
import hashlib
import json
import os
import shutil
import tempfile
import time

from .errors import JobFailedError, PreflightError, ResumeError, ValidationError
from .models import POINT_TABLES, empty_result, normalize_result
from .offsets import expand_offsets, offset_key, offset_label

SWEEP_STATE = "offset_sweep.json"


class OffsetSweepStopped(ResumeError):
    pass


def is_sweep(config):
    analysis = config.get("analysis") or {}
    if not analysis.get("offsets") or analysis["offsets"].get("mode", "single") == "single":
        return False
    return len(expand_offsets(config.get("analysis", {}))) > 1


def scalar_config(config, hz):
    # Import at call time to avoid service/sweep initialization cycles.
    from .service import _RUN_CONFIG_KEYS
    value = {key: copy.deepcopy(item) for key, item in config.items() if key in _RUN_CONFIG_KEYS}
    value.setdefault("analysis", {})["offsets"] = {"mode": "single"}
    value["analysis"]["offset"] = {"hz": hz, "value": hz, "unit": "Hz"}
    value.setdefault("execution", {})["background"] = True
    # Auto-generated templates are private job artifacts, not user-owned input.
    if config.get("detected_engine") and os.path.basename(str(value.get("config_path", ""))) == ".isf_detected_engine.json":
        value.pop("config_path", None)
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _point_contract(scalar, identity, tables):
    """Compare fixed operating evidence, not offset-dependent runtime hashes."""
    metadata = (scalar.get("dual_tb") or {}).get("metadata") or {}
    records = metadata.get("operating_points") or {}
    if not isinstance(records, Mapping):
        raise ValidationError("Physical operating-point evidence must be an object")
    fixed = {}
    if identity in records:
        record = records[identity]
        if not isinstance(record, Mapping):
            raise ValidationError("Invalid physical operating-point evidence: %s" % identity)
        # RDB paths, run timestamps, whole-contract/runtime hashes and noise
        # offsets are deliberately excluded. The restored source netlist hash
        # precedes per-frequency analysis generation and must remain fixed.
        fixed = {key: copy.deepcopy(record[key]) for key in (
            "params", "environment", "corner", "test", "effective_netlist_sha256"
        ) if key in record}
    rows = list(tables["summary"]) + [row for row in scalar["summary"]
        if str(row.get("operating_point_id") or row.get("point") or "") == identity]
    summaries = [{key: copy.deepcopy(row[key]) for key in ("corner", "operating_parameters")
                  if key in row} for row in rows]
    if summaries and any(value != summaries[0] for value in summaries[1:]):
        raise ValidationError("operating point at the same offset summary evidence mismatch: %s" % identity)
    summary = summaries[0] if summaries else {}
    for summary_key, metadata_key in (("corner", "corner"), ("operating_parameters", "params")):
        if summary_key in summary and metadata_key in fixed and summary[summary_key] != fixed[metadata_key]:
            raise ValidationError("Operating point summary differs from raw parameter evidence: %s" % identity)
    # Presence is evidence too: do not silently merge a modern audited point
    # with a legacy child that lost its operating contract.
    return {"metadata_present": identity in records, "metadata": fixed, "summary": summary}


def aggregate_results(entries, requested_offsets, complete=False):
    """Namespaced display keys; never mix offsets in scalar recomputation."""
    result = empty_result()
    physical_sets = []
    operating_contracts = {}
    child_kinds = []
    child_plans = []
    replica_identity = None
    for entry in entries:
        hz = float(entry["offset_hz"])
        scalar = normalize_result(entry["result"])
        metadata = (scalar.get("dual_tb") or {}).get("metadata") or {}
        replica = {key: copy.deepcopy(metadata[key]) for key in (
            "replica_source", "flicker_scope", "target_noise_trace", "peer_noise_trace"
        ) if key in metadata}
        if replica_identity is not None and replica != replica_identity:
            raise ValidationError("at different offsets flicker replica source or target device differs")
        replica_identity = replica
        child_kinds.append(scalar.get("run_kind"))
        if scalar.get("run_kind") == "plan":
            child_plans.append(dict({key: copy.deepcopy(scalar[key]) for key in (
                "submissions", "testbenches", "validation", "extraction", "discovery"
            ) if key in scalar}, offset_hz=hz, job_id=entry.get("job_id")))
        physical = set()
        for name, tables in scalar["points"].items():
            identity = str(name)
            contract = _point_contract(scalar, identity, tables)
            if identity in operating_contracts and operating_contracts[identity] != contract:
                raise ValidationError("Same-name operating-point evidence differs across offsets, refusing to mix: %s" % identity,
                                      details={"operating_point_id": identity, "offset_hz": hz})
            operating_contracts[identity] = contract
            key = "%s@%sHz" % (identity, offset_key(hz))
            physical.add(identity)
            target = {}
            for table in POINT_TABLES:
                target[table] = []
                for row in tables[table]:
                    value = _annotate(row, identity, key, hz, entry)
                    target[table].append(value)
            result["points"][key] = target
        for row in scalar["summary"]:
            identity = str(row.get("operating_point_id") or row.get("point") or "")
            if not identity or identity not in physical:
                raise ValidationError("Offset child result summary and point identity mismatch")
            key = "%s@%sHz" % (identity, offset_key(hz))
            result["summary"].append(_annotate(row, identity, key, hz, entry))
        physical_sets.append(physical)
    if physical_sets and any(points != physical_sets[0] for points in physical_sets[1:]):
        raise ValidationError("Physical operating-point sets differ across offsets, refusing to mix results")
    result["offset_sweep"] = {
        "schema_version": 1, "complete": bool(complete), "scope": "flicker",
        "offsets_hz": list(requested_offsets),
        "completed_offsets_hz": [float(e["offset_hz"]) for e in entries],
        "operating_point_count": len(physical_sets[0]) if physical_sets else 0,
        "operating_point_contracts": operating_contracts,
        "children": [{key: e.get(key) for key in ("offset_hz", "job_id", "workspace", "bundle_path", "result_sha256")} for e in entries],
        "note": "Each offset is calculated independently; total phase noise uses the selected device flicker sum of complex harmonic contributions. ",
    }
    # A sweep does not multiply the number of physical signoff points.
    # The aggregate is a presentation manifest; child bundles retain their own gates.
    all_plans = bool(child_kinds) and all(kind == "plan" for kind in child_kinds)
    if child_plans and not all_plans:
        raise ValidationError("Execution plans and numerical results cannot be mixed in an offset curve")
    result["run_kind"] = "plan" if all_plans else "replay"
    if all_plans:
        result["dry_run"] = True
        result["offset_sweep"]["plans"] = child_plans
    if replica_identity:
        result["dual_tb"] = {"metadata": replica_identity}
    result["signoff"] = False
    result["summary"].sort(key=lambda row: (row["operating_point_id"], row["offset_Hz"]))
    return normalize_result(result)


def _annotate(row, identity, key, hz, entry):
    value = copy.deepcopy(row)
    for field in ("offset_Hz", "offset_hz"):
        if value.get(field) is not None and float(value[field]) != hz:
            raise ValidationError("Child result offset differs from the request")
        value[field] = hz
    value["point"] = key
    value["operating_point_id"] = identity
    value["child_job_id"] = entry.get("job_id")
    return value


def run_offset_sweep(service, job_id, payload, config, emit):
    from .service import ToolService, _runtime_identity_errors
    offsets = expand_offsets(config["analysis"])
    root = payload.get("run_root")
    if not root:
        raise ValidationError("Multi-offset jobs require separate work directories")
    path = os.path.join(root, SWEEP_STATE)
    configs = [scalar_config(config, hz) for hz in offsets]
    contract = _digest(configs)
    manifest = {"schema_version": 1, "job_id": job_id, "contract_sha256": contract,
                "offsets_hz": offsets, "children": []}
    if payload.get("resume"):
        with open(path, encoding="utf-8") as stream:
            manifest = json.load(stream)
        if manifest.get("job_id") != job_id or manifest.get("contract_sha256") != contract:
            raise ResumeError("Offset job manifest identity mismatch, cannot be reused")
    entries = []
    service._atomic_json(path, manifest)
    # Check every requested frequency before any new simulation is submitted.
    children = []
    for index, (hz, child_config) in enumerate(zip(offsets, configs)):
        workspace = os.path.join(root, "offsets", "%03d_%sHz" % (index + 1, offset_key(hz)))
        os.makedirs(workspace, exist_ok=True)
        child_service = ToolService(workspace=workspace, command_runner=service._runner)
        report = child_service.preflight({"config": child_config})
        if not report.get("ok"):
            raise PreflightError("Offset %s Preflight failed" % offset_label(hz),
                                 details={"offset_hz": hz, "failed_checks": [
                                     c for c in report.get("checks", []) if c.get("status") == "fail"]})
        children.append((child_service, child_config, workspace))
        emit("sweep_preflight", "running", 5 * (index + 1) / len(offsets),
             "Offset preflight %d/%d: %s" % (index + 1, len(offsets), offset_label(hz)))
    service._atomic_json(path, manifest)

    def save_partial():
        if entries:
            partial = aggregate_results(entries, offsets)
            with service._lock:
                service._jobs[job_id]["result"] = partial
            service._persist_job(job_id)

    for index, (hz, child) in enumerate(zip(offsets, children)):
        with service._lock:
            stopped = service._jobs[job_id].get("cancel_requested", False)
        if stopped:
            save_partial()
            raise OffsetSweepStopped("Remaining offsets stopped; completed results are retained, remaining offsets can be resumed")
        child_service, child_config, workspace = child
        previous = next((item for item in manifest["children"] if item["offset_hz"] == hz), None)
        child_id = previous.get("job_id") if previous else None
        if child_id:
            old = child_service.get_job(child_id)
            if previous.get("status") == "succeeded" and old["status"] != "succeeded":
                raise ResumeError("Completed child result is corrupt or missing, cannot retry as an ordinary failure", details={"offset_hz": hz})
            snapshot = child_service._jobs[child_id].get("_config_snapshot", {})
            if _runtime_identity_errors(snapshot.get("runtime_identity", {})):
                raise ResumeError("Child job inputs have changed, cannot be reused", details={"offset_hz": hz})
            if old["status"] == "succeeded":
                result = child_service.get_result(child_id)
                result_path = os.path.join(workspace, "jobs", child_id, "result.json")
                with open(result_path, "rb") as stream:
                    actual_hash = hashlib.sha256(stream.read()).hexdigest()
                if previous.get("result_sha256") and actual_hash != previous["result_sha256"]:
                    raise ResumeError("Child result validation failed", details={"offset_hz": hz})
            elif old["status"] in ("failed", "interrupted"):
                child_service.resume_job(child_id, {"background": True})
            else:
                raise ResumeError("Child job is still running, cannot start twice", details={"offset_hz": hz})
        else:
            child_id = child_service.start_run({"config": child_config, "background": True})
            previous = {"offset_hz": hz, "workspace": workspace, "job_id": child_id, "status": "running"}
            manifest["children"].append(previous)
            service._atomic_json(path, manifest)
        last = None
        while True:
            status = child_service.get_job(child_id)
            state = (status["status"], status.get("progress", 0), status.get("message", ""))
            if state != last:
                progress = 5 + 94 * (index + min(float(status.get("progress", 0)), 100) / 100) / len(offsets)
                emit("offset_%s" % offset_key(hz), status["status"], progress,
                     "%s (%d/%d): %s" % (offset_label(hz), index + 1, len(offsets), status.get("message", "")),
                     {"offset_hz": hz, "child_job_id": child_id})
                last = state
            if status["status"] not in ("queued", "running"):
                break
            time.sleep(0.1)
        previous["status"] = status["status"]
        service._atomic_json(path, manifest)
        if status["status"] != "succeeded":
            save_partial()
            raise JobFailedError("Offset %s Child job failed; completed offsets retained" % offset_label(hz),
                                 details={"offset_hz": hz, "child_job_id": child_id, "error": status.get("error")})
        result = child_service.get_result(child_id)
        result_path = os.path.join(workspace, "jobs", child_id, "result.json")
        with open(result_path, "rb") as stream:
            previous["result_sha256"] = hashlib.sha256(stream.read()).hexdigest()
        previous["bundle_path"] = child_service._jobs[child_id]["_request"].get("bundle_path")
        entries.append(dict(previous, result=result))
        service._atomic_json(path, manifest)
        save_partial()
    result = aggregate_results(entries, offsets, complete=True)
    manifest["complete"] = True
    service._atomic_json(path, manifest)
    bundle = payload.get("bundle_path")
    if bundle:
        # Explicit aggregate export; scalar v2 bundles stay unchanged.
        export = copy.deepcopy(manifest)
        for child in export["children"]:
            relative = os.path.join("offsets", offset_key(child["offset_hz"]) + "Hz")
            target = os.path.join(bundle, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if child.get("bundle_path") and os.path.isdir(child["bundle_path"]):
                from .orchestrator import _tree_identity
                expected = _tree_identity(child["bundle_path"])
                if os.path.exists(target):
                    if not os.path.isdir(target) or _tree_identity(target) != expected:
                        raise ValidationError("Existing child bundle does not match the current result, refusing to overwrite: %s" % target)
                else:
                    temporary = tempfile.mkdtemp(prefix=".isf-export-", dir=os.path.dirname(target))
                    staging = os.path.join(temporary, "bundle")
                    shutil.copytree(child["bundle_path"], staging)
                    if _tree_identity(staging) != expected:
                        raise ValidationError("child bundle staging validation failed")
                    os.rename(staging, target)
                    os.rmdir(temporary)
                child["bundle_tree_sha256"] = expected["sha256"]
                child["bundle_path"] = relative
            else:
                child["bundle_path"] = None
        service._atomic_json(os.path.join(bundle, "offset_sweep.json"), export)
        service._atomic_json(os.path.join(bundle, "result.json"), result)
    emit("sweep_complete", "succeeded", 100, "All %d offsets completed" % len(offsets))
    return result


def import_offset_sweep(payload):
    """Recompute each portable scalar bundle; never trust a saved plot JSON."""
    from .dual_tb import import_dual_tb_bundle
    root = os.path.realpath(os.path.expanduser(str(payload["path"])))
    if payload.get("signoff"):
        raise ValidationError("Aggregate manifests are for comparison; signoff requires validating the raw scalar result at every offset bundle")
    with open(os.path.join(root, SWEEP_STATE), encoding="utf-8") as stream:
        manifest = json.load(stream)
    if manifest.get("schema_version") != 1 or not manifest.get("complete"):
        raise ValidationError("Only complete multi-offset aggregate manifests can be imported")
    offsets = expand_offsets({"offsets": {"mode": "list", "values": manifest.get("offsets_hz", [])}})
    entries = []
    seen = set()
    for child in manifest.get("children", []):
        hz = float(child["offset_hz"])
        relative = child.get("bundle_path")
        if hz not in offsets or hz in seen or not relative or os.path.isabs(relative):
            raise ValidationError("Multi-offset child bundle invalid identity or relative path")
        seen.add(hz)
        path = os.path.realpath(os.path.join(root, relative))
        if os.path.commonpath((root, path)) != root or path == root:
            raise ValidationError("Multi-offset child bundle must stay inside the aggregate directory")
        from .orchestrator import _tree_identity
        if child.get("bundle_tree_sha256") != _tree_identity(path)["sha256"]:
            raise ValidationError("Multi-offset child bundle content validation failed")
        scalar = import_dual_tb_bundle({"path": path, "signoff": False})
        entries.append(dict(child, result=scalar))
    if seen != set(offsets):
        raise ValidationError("Multi-offset manifest is missing child results")
    return aggregate_results(entries, offsets, complete=True)
