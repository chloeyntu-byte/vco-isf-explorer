"""Read-only, inspectable construction of a pair-specific execution template.

The UI edits named fields; JSON is an implementation detail saved only when a
job starts. Ambiguous topology/model choices remain unresolved. No simulator,
calculator expression or shell environment is executed here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from collections.abc import Mapping

from .errors import ValidationError
from .generated_flicker import DC_MAPPING, generated_fixture
from .physical_profile import (
    REQUIRED_CONTRACTS, resolve_device_subckt_file, validate_physical_profile,
)


def _net(value):
    return str(value or "").strip().lstrip("/").replace("/", ".")


def _at(value, path, default=None):
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return default
        value = value.get(part)
    return default if value is None else value


def _put(value, path, item):
    parts = path.split(".")
    for part in parts[:-1]:
        value = value.setdefault(part, {})
    value[parts[-1]] = item


def _unique(values):
    values = list(dict.fromkeys(item for item in values if item not in (None, "")))
    return values[0] if len(values) == 1 else None


def _device(report, name):
    records = report.get("topology", {}).get("devices", [])
    found = [item for item in records if item.get("name") == name]
    return found[0] if len(found) == 1 else {}


def _intrinsics(model_path, subckt):
    if not model_path or not subckt:
        return None, []
    stub = {"replica": {"original_subckt": subckt}}
    path = resolve_device_subckt_file(model_path, stub)
    if not path:
        return None, []
    # PDK reads are bounded and never evaluated.
    with open(path, encoding="utf-8", errors="replace") as stream:
        text = stream.read(32 * 1024 * 1024)
    blocks = re.findall(
        r"(?ims)^\s*(?:inline\s+)?subckt\s+" + re.escape(subckt)
        + r"\b.*?^\s*ends(?:\s+" + re.escape(subckt) + r")?\s*$", text,
    )
    return path, _intrinsics_in_blocks(blocks, subckt)


def _intrinsics_in_blocks(blocks, subckt):
    candidates, occupied_names = {}, set()
    for block in blocks:
        is_inline = bool(re.match(r"(?i)^\s*inline\s+subckt\b", block))
        for match in re.finditer(
            r"(?im)^\s*(\S+)\s*\(\s*([^\n)]+)\)\s+(\S+)([^\n]*)", block
        ):
            instance, pins, primitive, params = match.groups()
            pins = pins.split()
            occupied_names.update([instance] + pins)
            if len(pins) == 4 and (instance.lower().startswith("m") or re.search(r"\bl\s*=", params)):
                candidate = {"instance": instance, "primitive": primitive, "terminals": pins}
                if is_inline and instance.lower() == subckt.lower():
                    candidate["inline_alias"] = True
                candidates[json.dumps(candidate, sort_keys=True)] = candidate
    for candidate in candidates.values():
        candidate["occupied_names"] = sorted(occupied_names)
    return list(candidates.values())


def build_portable_setup(pair_report, app_config=None, environment=None):
    """Return editable values, evidence and a template, without writing files."""
    config = dict(app_config or {})
    environment = dict(environment or {})
    settings = config.get("portable_setup") or {}
    overrides = settings.get("overrides") or {}
    if not isinstance(overrides, Mapping):
        overrides = {}
    roles = pair_report.get("roles", {})
    vco, replica = roles.get("vco") or {}, roles.get("flicker") or {}
    generated = pair_report.get("pair", {}).get("replica_source") == "generated"
    vm = vco.get("mapping", {})
    rm = DC_MAPPING if generated else replica.get("mapping", {})
    target = _device(vco, vm.get("target_instance"))
    dc = target if generated else _device(replica, rm.get("dc_instance"))
    model_name = target.get("model") or dc.get("model")
    model_records = (vco.get("selected_test") or {}).get("models", [])
    model_paths = list(dict.fromkeys(item.get("path") for item in model_records if item.get("path")))
    matching_model_paths = []
    intrinsic_cache = {}
    generated_models, generated_errors = {}, []
    if generated and target:
        history = vco.get("selected_history") or {}
        paths = list(dict.fromkeys(row["path"] for row in history.get("source_netlists", []) if row.get("path")))
        read_cache = {}
        for path in paths:
            try:
                _, evidence, block = generated_fixture(path, target["name"], 10000.0, read_cache=read_cache)
                identity = (evidence["model_file"], evidence["device_subckt_file"],
                            evidence["device_subckt_section"])
                generated_models[identity] = block
            except (OSError, ValueError, ValidationError) as exc:
                generated_errors.append({"field": "testbenches.vco.history", "message": str(exc)})
        if len(generated_models) != 1 and not generated_errors:
            generated_errors.append({"field": "testbenches.vco.history",
                                     "message": "所选工况必须共享一个可确认的活动 MOS 定义。"})
    if not generated and model_name and len(model_paths) > 1:
        for candidate_path in model_paths:
            try:
                evidence = _intrinsics(candidate_path, model_name)
                intrinsic_cache[(candidate_path, model_name)] = evidence
                if evidence[0] and evidence[1]:
                    matching_model_paths.append(candidate_path)
            except (OSError, ValueError):
                pass
    generated_model = next(iter(generated_models)) if len(generated_models) == 1 else (None, None, None)
    model_path = generated_model[0] if generated else _unique(matching_model_paths or model_paths)
    engine = {
        "name": "Detected Maestro pair",
        "source_root": ".", "points": [],
        "physical_profile": {"schema_version": 1, "profile_id": "detected-maestro-pair-v1",
                             "contracts": copy.deepcopy(REQUIRED_CONTRACTS)},
    }
    profile = engine["physical_profile"]
    fields, errors = [], list(generated_errors)
    if generated:
        engine["replica_source"] = "generated"
        profile["replica_source"] = "generated"
    for role_name, report in (('vco', vco), ('replica', replica)):
        for issue in report.get('mapping_issues', []):
            if issue.get('severity') == 'error':
                errors.append({'field': 'testbenches.%s.mapping_overrides.%s' % (role_name, issue.get('key', 'mapping')),
                               'message': issue.get('message', '名称对应未通过电气校验')})
    known = set()

    def field(key, label, detected, *, candidates=(), required=True, kind="text", help="", source="当前绑定网表", editable=True):
        known.add(key)
        supplied = overrides.get(key)
        manual = supplied not in (None, "")
        if manual and not editable:
            errors.append({"field": key, "message": "%s 是执行器固定约定，不能覆盖。" % label})
        value = supplied if manual and editable else detected
        try:
            if kind == "list":
                if isinstance(value, str):
                    value = [part for part in re.split(r"[,\s]+", value.strip()) if part]
                value = list(value or [])
            elif kind == "integer":
                if isinstance(value, bool) or float(value) != int(value) or int(value) <= 0:
                    raise ValueError("必须为正整数")
                value = int(value)
            elif value is not None:
                value = str(value).strip()
                if any(char in value for char in ("\n", "\r", "\x00")):
                    raise ValueError("不能包含换行或控制字符")
        except (TypeError, ValueError) as exc:
            errors.append({"field": key, "message": "%s：%s" % (label, exc)})
            value = None
        missing = value in (None, "", [])
        if required and missing:
            errors.append({"field": key, "message": "%s 未能唯一识别，请在高级设置中选择。" % label})
        if key.startswith("engine."):
            _put(engine, key[7:], value)
        else:
            _put(profile, key, value)
        display = lambda item: ", ".join(str(part) for part in item) if isinstance(item, list) else ("" if item is None else str(item))
        fields.append({"key": key, "label": label, "value": display(value), "detected": display(detected),
                       "candidates": [display(item) for item in candidates if item not in (None, "")],
                       "source": "手动覆盖" if manual else source,
                       "status": "missing" if missing and required else ("manual" if manual else "detected"),
                       "required": required, "help": help, "editable": editable,
                       "active": True, "group": "execution_detail",
                       "consumption_mode": "private_netlist", "used_by": ["private Spectre preparation"]})
        return value

    model_path = field("engine.model_file", "器件模型文件", model_path, candidates=model_paths, editable=not generated,
                       help="按目标 MOS 子电路在模型 include 链中的定义缩小候选；多个匹配入口时才需要选择。")
    subckt = field("main.device_subckt", "VCO 目标器件模型名称", target.get("model"), candidates=[model_name])
    original = field("replica.original_subckt", "副本目标器件模型", dc.get("model"), candidates=[model_name], editable=not generated)
    instance = field("main.target_instance", "VCO 目标器件（网表局部名）", target.get("local_name") or str(target.get("name") or "").split(".")[-1],
                     help="先在信号对应中选择目标器件；不会默认绑定 M1。")
    full_instance = field("main.instance_path", "VCO 目标器件完整层级", target.get("name"), required=True)
    peer = _device(vco, vm.get("peer_instance"))
    pins = target.get("local_terminals") or target.get("terminals") or []
    for index, pin in enumerate(("drain", "gate", "source", "bulk")):
        node = (pins[index] if len(pins) == 4 else None) or vm.get(pin)
        field("main.terminals." + pin, {"drain":"漏极", "gate":"栅极", "source":"源极", "bulk":"体端"}[pin] + "节点", _net(node) or None)
    field("main.output.positive", "差分输出负端（内部兼容字段）", _net(vm.get("output_n")),
          help="实际输出始终按信号对应的正端减负端；此字段保留旧执行器约定。")
    field("main.output.negative", "差分输出正端（内部兼容字段）", _net(vm.get("output_p")))
    # Generated private names are collision-free and are all shown to the user.
    existing = {item.get("name") for item in vco.get("signal_catalog", [])}
    # The new probe lives inside the target definition. Fully qualified names
    # in the catalog alone do not protect its local namespace.
    target_scope = target.get("scope", "")
    for item in vco.get("topology", {}).get("devices", []):
        if item.get("scope", "") == target_scope:
            existing.add(item.get("local_name") or str(item.get("name", "")).split(".")[-1])
            existing.update(item.get("local_terminals") or item.get("terminals") or [])
    reserved_before_generation = set(existing)
    def fresh(base):
        name, index = base, 1
        while name in existing:
            name, index = base + "_" + str(index), index + 1
        existing.add(name)
        return name
    probe_node = field("main.drain_probe.node", "私有副本新增漏极探针节点", fresh("ISF_DRAIN"), source="自动生成（仅私有副本）")
    probe_source = field("main.drain_probe.source", "私有副本新增电流探针名称", fresh("V_ISF_DRAIN"), source="自动生成（仅私有副本）")
    injection = field("main.injection.source", "私有副本新增注入源名称", fresh("I_ISF_INJECT"), source="自动生成（仅私有副本）")
    chosen_private_names = set()
    for key, name in (("main.drain_probe.node", probe_node), ("main.drain_probe.source", probe_source), ("main.injection.source", injection)):
        if name in reserved_before_generation or name in chosen_private_names:
            errors.append({"field": key, "message": "新增私有名称 %s 与目标层级中的现有节点/实例或另一新增名称冲突。" % name})
        chosen_private_names.add(name)
    field("main.injection.positive", "注入源正端", _at(profile, "main.terminals.drain"))
    field("main.injection.negative", "注入源负端", _at(profile, "main.terminals.source"))
    hierarchy = str(full_instance or "").rsplit(".", 1)[0] if "." in str(full_instance or "") else ""
    qualify = lambda name: hierarchy + "." + name if hierarchy and name != "0" else name
    actual_pins = target.get("terminals") or []
    # Local pins are edited inside the selected subcircuit; PSF uses the nets
    # resolved through its instance port bindings (which need not be parent.pin).
    # This evidence is derived from the bound netlist parser, never an override.
    if len(actual_pins) == 4:
        profile["main"]["resolved_terminals"] = {
            pin: _net(actual_pins[index])
            for index, pin in enumerate(("drain", "gate", "source", "bulk"))
        }
    for index, pin in enumerate(("drain", "gate", "source", "bulk")):
        field("main.traces." + pin, {"drain":"漏极", "gate":"栅极", "source":"源极", "bulk":"体端"}[pin] + "保存波形路径",
              (actual_pins[index] if len(actual_pins) == 4 else qualify(_at(profile, "main.terminals." + pin, ""))) or None)
    probe_trace = qualify(str(probe_source or "")) + ":p"
    field("main.drain_probe.trace", "新增电流探针完整波形路径", probe_trace)
    field("main.traces.current", "用于计算的漏极电流波形", probe_trace,
          help="由私有副本中的零伏漏极探针测量；已有 Maestro 电流输出仅供对照。")
    field("main.injection.trace", "注入源完整层级", qualify(str(injection or "")))
    transfer = vm.get('transfer_contract') or {'mode': 'private_injection'}
    if transfer.get('mode') == 'invalid' or vm.get('transfer_output') and transfer.get('mode') != 'existing_source':
        errors.append({'field': 'testbenches.vco.mapping_overrides.transfer_output',
                       'message': '无法从指定输出取得原始传递结果的读取位置；不会静默改用其他注入源。'})
    source_trace = transfer.get('source') if transfer.get('mode') == 'existing_source' else _at(profile, 'main.injection.trace')
    field('engine.transfer_source', '本次实际读取的传递电流源', source_trace, editable=False,
          help=transfer.get('notice') or '未选已有来源：读取私有副本新增的漏到源注入。')
    field('engine.transfer_consumption_mode', '传递测量口径', transfer.get('consumption_mode') or 'private_injection', editable=False)
    for item in fields:
        if item['key'] in ('engine.transfer_source', 'engine.transfer_consumption_mode'):
            item.update(consumption_mode=transfer.get('consumption_mode') or 'private_injection',
                        used_by=['signed HBXF transfer H[k]', 'ISF calculation'])
    try:
        if generated:
            subckt_path = generated_model[1]
            intrinsic_candidates = _intrinsics_in_blocks([generated_models[generated_model]], original) if subckt_path else []
        else:
            evidence = intrinsic_cache.get((model_path, original))
            subckt_path, intrinsic_candidates = evidence if evidence is not None else _intrinsics(model_path, original)
    except (OSError, ValueError) as exc:
        subckt_path, intrinsic_candidates = None, []
        errors.append({"field": "engine.device_subckt_file", "message": str(exc)})
    field("engine.device_subckt_file", "目标器件子电路定义文件", subckt_path, required=True, editable=not generated,
          help="从模型 include 链只读查找，不能通过填写错误路径跳过模型校验。")
    intrinsic_name = field("replica.intrinsic.instance", "模型内部噪声 MOS 名称", _unique([item["instance"] for item in intrinsic_candidates]), candidates=list(dict.fromkeys(item["instance"] for item in intrinsic_candidates)))
    matching_intrinsics = [item for item in intrinsic_candidates if item["instance"] == intrinsic_name]
    primitive = field("replica.intrinsic.primitive", "模型内部 MOS 模型", _unique([item["primitive"] for item in matching_intrinsics]), candidates=list(dict.fromkeys(item["primitive"] for item in matching_intrinsics)))
    matching_intrinsics = [item for item in matching_intrinsics if item["primitive"] == primitive]
    selected_intrinsic = matching_intrinsics[0] if len(matching_intrinsics) == 1 else {}
    profile["replica"]["intrinsic"]["inline_alias"] = bool(selected_intrinsic.get("inline_alias"))
    intrinsic_terminals = field("replica.intrinsic.terminals", "模型内部 MOS 四端（D G S B）", selected_intrinsic.get("terminals"), kind="list")
    exact_intrinsics = [item for item in matching_intrinsics if item["terminals"] == intrinsic_terminals]
    if intrinsic_name and primitive and len(exact_intrinsics) != 1:
        errors.append({"field": "replica.intrinsic.instance", "message": "内部 MOS 的实例、模型与四端不能唯一对应，未选择第一个候选。"})
    intrinsic_occupied = {name for item in intrinsic_candidates for name in item.get("occupied_names", [])}
    intrinsic_injection, suffix = "I_ISF_NOISE", 1
    while intrinsic_injection in intrinsic_occupied:
        intrinsic_injection, suffix = "I_ISF_NOISE_" + str(suffix), suffix + 1
    actual_injection = field("replica.intrinsic.injection_source", "动态副本内部注入源", intrinsic_injection, source="自动生成（仅私有副本）")
    if actual_injection in intrinsic_occupied:
        errors.append({"field": "replica.intrinsic.injection_source", "message": "内部注入源名称与器件模型中的现有实例/节点冲突。"})
    # Spectre aliases an inline subcircuit's same-named primitive to its
    # external instance. The private dynamic copy is an ordinary subcircuit,
    # so only original VCO/DC traces use this alias.
    noise_suffix = "" if selected_intrinsic.get("inline_alias") else "." + str(intrinsic_name)
    target_noise = str(full_instance) + noise_suffix if full_instance and intrinsic_name else None
    peer_noise = str(peer.get("name")) + noise_suffix if peer and intrinsic_name else None
    if generated and peer and peer.get("model") != target.get("model"):
        errors.append({"field": "testbenches.vco.mapping_overrides.peer_instance",
                       "message": "自动副本的实测对称参考需要同型号配对器件。"})
    field("main.traces.target_noise", "VCO 目标器件噪声贡献路径",
          target_noise if generated else vm.get("noise_trace") or target_noise, editable=not generated)
    field("main.traces.peer_noise", "VCO 配对器件噪声贡献路径", peer_noise, required=not generated,
          editable=not generated,
          help="用于现有双器件噪声合成；在信号对应中选择配对器件。")
    generated_instance = field("replica.instance", "动态副本器件名称", "M_ISF_REPLICA", source="自动生成（仅私有副本）")
    field("replica.generated_subckt", "动态副本模型名称", original + "_isf_dynamic" if original else None, source="自动生成（仅私有副本）")
    parameters = dc.get("parameter_text" if generated else "parameters")
    if isinstance(parameters, Mapping):
        parameters = " ".join("%s=%s" % item for item in parameters.items())
    field("replica.device_parameters", "动态副本器件尺寸与参数", parameters, editable=not generated,
          help=("来自每个 VCO 工况的所选 MOS 实例" if generated else
                "来自噪声 TB 器件；运行时保留该 TB 的参数声明与尺寸设置。"))
    sections = [] if generated else list(dict.fromkeys(item.get("section") for item in model_records if item.get("path") == model_path and item.get("section")))
    field("replica.model_sections", "模型工艺角段名称", sections, kind="list", required=False, editable=not generated,
          help="此处来自 VCO 模型声明；两个 TB 的完整文件/section 均在名称清单中。Maestro 双 TB 运行的 DC/动态副本保留 replica 原网表的 section。")
    sense = field("replica.sense_source", "动态副本电流探针名称", "V_ISF_SENSE", source="自动生成（仅私有副本）")
    field("replica.noise_trace", "动态副本噪声贡献路径", str(generated_instance) + "." + str(intrinsic_name) if generated_instance and intrinsic_name else None)
    dc_instance = field("replica.dc_instance", "噪声 TB 目标器件实例", rm.get("dc_instance"), editable=not generated)
    selected_sense = field("replica.dc_sense_source", "噪声 TB 电流检测源", rm.get("dc_sense_source"), editable=not generated,
                          candidates=[item['name'] for item in replica.get('topology', {}).get('sources', []) if item.get('model', '').lower() in ('vsource', 'iprobe')])
    if selected_sense and rm.get('dc_sense_source') and selected_sense != rm['dc_sense_source']:
        errors.append({'field': 'replica.dc_sense_source', 'message': '执行详情中的检测源覆盖与信号对应不同；请在信号对应中统一选择，避免旧设置覆盖本次选择。'})
    field("replica.dc_sense_drain_terminal", "检测源连接漏极的一端", "negative", editable=False,
          help="当前执行器要求零伏检测源负端连接 MOS 漏极。")
    selected_transducer = field("replica.dc_transducer", "噪声 TB 电流转电压元件", rm.get("dc_transducer"), editable=not generated,
                               candidates=[item['name'] for item in replica.get('topology', {}).get('sources', []) if item.get('model', '').lower() == 'ccvs'])
    if selected_transducer and rm.get('dc_transducer') and selected_transducer != rm['dc_transducer']:
        errors.append({'field': 'replica.dc_transducer', 'message': '执行详情中的读出元件覆盖与信号对应不同；请在信号对应中统一选择。'})
    aliases = replica.get("topology", {}).get("net_aliases", {})
    def mapped_node(value):
        return _net(aliases.get(value, aliases.get(_net(value), value)))
    field("replica.dc_noise_output", "噪声 TB 测量正端", mapped_node(rm.get("output_p") or rm.get("dc_noise_output")), editable=not generated)
    field("replica.dc_noise_reference", "噪声 TB 测量负端", mapped_node(rm.get("output_n")), editable=not generated)
    field("main.traces.dc_noise", "噪声 TB 目标器件噪声贡献路径", str(dc_instance) + noise_suffix if dc_instance and intrinsic_name else None)
    for executable, label in (("spectre", "Spectre 仿真程序"), ("psf", "波形读取程序")):
        field("engine." + executable, label, environment.get("executables", {}).get(executable), source="本机自动检测", required=True)
    field("engine.cadence_env", "Cadence 运行环境脚本", environment.get("cadence_env"), source="本机自动检测", required=False)
    offset = _at(config, "analysis.offset.hz", config.get("offset_hz", 10000.0))
    engine["offset_hz"] = float(offset)
    engine["max_harmonic"] = int(_at(config, "analysis.max_harmonic", 3))
    for key, label, default in (("hb_max_harmonic", "HB 最大谐波数", 12), ("main_max_sideband", "噪声最大边带数", 10), ("dc_noise_samples", "周期内 DC 噪声采样数", 24)):
        field("engine." + key, label, default, kind="integer", source="执行默认值（可覆盖）")
    engine["vco_mapping"] = copy.deepcopy(vm)
    engine["flicker_mapping"] = copy.deepcopy(rm)
    if generated:
        engine["device_subckt_section"] = generated_model[2]
    for key in overrides:
        if key not in known and overrides[key] not in (None, ""):
            errors.append({"field": key, "message": "未知的执行设置，请重新识别后选择。"})
    if target and dc and target.get("model") != dc.get("model"):
        errors.append({"field": "replica.original_subckt", "message": "两个 TB 所选器件模型不同，请确认器件对应关系。"})
    for semantic in ("vgs", "vds", "vbs"):
        selected = vm.get(semantic + "_output")
        output = next((item for item in vco.get("signal_catalog", []) if item.get("kind") == "output" and item.get("name") == selected), {})
        if selected and not output.get("details", {}).get("voltage_pair"):
            errors.append({"field": "testbenches.vco.mapping_overrides." + semantic + "_output",
                           "message": "%s 的表达式不能作为原始两节点时域波形；请选择明确的节点电压差，或清空该项并按器件端口提取。" % selected})
    if not errors:
        try:
            validate_physical_profile(profile)
        except Exception as exc:
            errors.append({"field": "portable_setup", "message": str(exc)})
    return {"ok": not errors, "mode": "auto", "fields": fields, "errors": errors,
            "engine": engine, "read_only": True,
            "notice": "修改只影响私有仿真副本；自动识别缺失或歧义时请在信号对应中选择。"}


def engine_content_sha256(engine):
    return hashlib.sha256(json.dumps(engine, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
