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
        raise ValidationError("物理工况证据必须为对象")
    fixed = {}
    if identity in records:
        record = records[identity]
        if not isinstance(record, Mapping):
            raise ValidationError("物理工况证据无效：%s" % identity)
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
        raise ValidationError("同一频偏的工况 summary 证据不一致：%s" % identity)
    summary = summaries[0] if summaries else {}
    for summary_key, metadata_key in (("corner", "corner"), ("operating_parameters", "params")):
        if summary_key in summary and metadata_key in fixed and summary[summary_key] != fixed[metadata_key]:
            raise ValidationError("工况 summary 与原始参数证据不一致：%s" % identity)
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
            raise ValidationError("不同频偏的 flicker 副本来源或目标器件不一致")
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
                raise ValidationError("不同频偏的同名工况证据不一致，拒绝混合：%s" % identity,
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
                raise ValidationError("频偏子结果的 summary 与 point 身份不一致")
            key = "%s@%sHz" % (identity, offset_key(hz))
            result["summary"].append(_annotate(row, identity, key, hz, entry))
        physical_sets.append(physical)
    if physical_sets and any(points != physical_sets[0] for points in physical_sets[1:]):
        raise ValidationError("不同频偏的物理工况集合不一致，拒绝混合结果")
    result["offset_sweep"] = {
        "schema_version": 1, "complete": bool(complete), "scope": "flicker",
        "offsets_hz": list(requested_offsets),
        "completed_offsets_hz": [float(e["offset_hz"]) for e in entries],
        "operating_point_count": len(physical_sets[0]) if physical_sets else 0,
        "operating_point_contracts": operating_contracts,
        "children": [{key: e.get(key) for key in ("offset_hz", "job_id", "workspace", "bundle_path", "result_sha256")} for e in entries],
        "note": "各频偏独立计算；总相噪为指定器件 flicker 的各谐波复数贡献总和。",
    }
    # A sweep does not multiply the number of physical signoff points.
    # The aggregate is a presentation manifest; child bundles retain their own gates.
    all_plans = bool(child_kinds) and all(kind == "plan" for kind in child_kinds)
    if child_plans and not all_plans:
        raise ValidationError("执行计划与数值结果不能混合为频偏曲线")
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
            raise ValidationError("子任务结果频偏与请求不一致")
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
        raise ValidationError("多频偏任务需要独立工作目录")
    path = os.path.join(root, SWEEP_STATE)
    configs = [scalar_config(config, hz) for hz in offsets]
    contract = _digest(configs)
    manifest = {"schema_version": 1, "job_id": job_id, "contract_sha256": contract,
                "offsets_hz": offsets, "children": []}
    if payload.get("resume"):
        with open(path, encoding="utf-8") as stream:
            manifest = json.load(stream)
        if manifest.get("job_id") != job_id or manifest.get("contract_sha256") != contract:
            raise ResumeError("频偏任务清单身份不符，不能复用")
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
            raise PreflightError("频偏 %s 预检失败" % offset_label(hz),
                                 details={"offset_hz": hz, "failed_checks": [
                                     c for c in report.get("checks", []) if c.get("status") == "fail"]})
        children.append((child_service, child_config, workspace))
        emit("sweep_preflight", "running", 5 * (index + 1) / len(offsets),
             "频偏预检 %d/%d：%s" % (index + 1, len(offsets), offset_label(hz)))
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
            raise OffsetSweepStopped("已停止后续频偏；当前已完成结果保留，可继续剩余频偏")
        child_service, child_config, workspace = child
        previous = next((item for item in manifest["children"] if item["offset_hz"] == hz), None)
        child_id = previous.get("job_id") if previous else None
        if child_id:
            old = child_service.get_job(child_id)
            if previous.get("status") == "succeeded" and old["status"] != "succeeded":
                raise ResumeError("已完成子结果损坏或缺失，拒绝当成普通失败重试", details={"offset_hz": hz})
            snapshot = child_service._jobs[child_id].get("_config_snapshot", {})
            if _runtime_identity_errors(snapshot.get("runtime_identity", {})):
                raise ResumeError("子任务运行输入已改变，不能复用", details={"offset_hz": hz})
            if old["status"] == "succeeded":
                result = child_service.get_result(child_id)
                result_path = os.path.join(workspace, "jobs", child_id, "result.json")
                with open(result_path, "rb") as stream:
                    actual_hash = hashlib.sha256(stream.read()).hexdigest()
                if previous.get("result_sha256") and actual_hash != previous["result_sha256"]:
                    raise ResumeError("子任务结果校验失败", details={"offset_hz": hz})
            elif old["status"] in ("failed", "interrupted"):
                child_service.resume_job(child_id, {"background": True})
            else:
                raise ResumeError("子任务仍在运行，不能重复启动", details={"offset_hz": hz})
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
                     "%s（%d/%d）：%s" % (offset_label(hz), index + 1, len(offsets), status.get("message", "")),
                     {"offset_hz": hz, "child_job_id": child_id})
                last = state
            if status["status"] not in ("queued", "running"):
                break
            time.sleep(0.1)
        previous["status"] = status["status"]
        service._atomic_json(path, manifest)
        if status["status"] != "succeeded":
            save_partial()
            raise JobFailedError("频偏 %s 子任务失败；已完成频偏保留" % offset_label(hz),
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
                        raise ValidationError("已有子 bundle 与当前结果不符，拒绝覆盖：%s" % target)
                else:
                    temporary = tempfile.mkdtemp(prefix=".isf-export-", dir=os.path.dirname(target))
                    staging = os.path.join(temporary, "bundle")
                    shutil.copytree(child["bundle_path"], staging)
                    if _tree_identity(staging) != expected:
                        raise ValidationError("子 bundle 暂存校验失败")
                    os.rename(staging, target)
                    os.rmdir(temporary)
                child["bundle_tree_sha256"] = expected["sha256"]
                child["bundle_path"] = relative
            else:
                child["bundle_path"] = None
        service._atomic_json(os.path.join(bundle, "offset_sweep.json"), export)
        service._atomic_json(os.path.join(bundle, "result.json"), result)
    emit("sweep_complete", "succeeded", 100, "全部 %d 个频偏完成" % len(offsets))
    return result


def import_offset_sweep(payload):
    """Recompute each portable scalar bundle; never trust a saved plot JSON."""
    from .dual_tb import import_dual_tb_bundle
    root = os.path.realpath(os.path.expanduser(str(payload["path"])))
    if payload.get("signoff"):
        raise ValidationError("聚合清单用于比较；签核需逐个频偏验证原始标量 bundle")
    with open(os.path.join(root, SWEEP_STATE), encoding="utf-8") as stream:
        manifest = json.load(stream)
    if manifest.get("schema_version") != 1 or not manifest.get("complete"):
        raise ValidationError("仅能导入完整的多频偏聚合清单")
    offsets = expand_offsets({"offsets": {"mode": "list", "values": manifest.get("offsets_hz", [])}})
    entries = []
    seen = set()
    for child in manifest.get("children", []):
        hz = float(child["offset_hz"])
        relative = child.get("bundle_path")
        if hz not in offsets or hz in seen or not relative or os.path.isabs(relative):
            raise ValidationError("多频偏子 bundle 身份或相对路径无效")
        seen.add(hz)
        path = os.path.realpath(os.path.join(root, relative))
        if os.path.commonpath((root, path)) != root or path == root:
            raise ValidationError("多频偏子 bundle 不得越出聚合目录")
        from .orchestrator import _tree_identity
        if child.get("bundle_tree_sha256") != _tree_identity(path)["sha256"]:
            raise ValidationError("多频偏子 bundle 内容校验失败")
        scalar = import_dual_tb_bundle({"path": path, "signoff": False})
        entries.append(dict(child, result=scalar))
    if seen != set(offsets):
        raise ValidationError("多频偏清单缺少子结果")
    return aggregate_results(entries, offsets, complete=True)
