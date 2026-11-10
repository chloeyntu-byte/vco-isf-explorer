"""Portable per-operating-point VCO -> replica waveform handoff.

No simulator or Calculator code is evaluated during binding. Both the static
probe and the live writer use the same finite, periodic table contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from .errors import ValidationError

CHANNELS = (("VGS", "gate"), ("VDS", "drain"), ("VBS", "bulk"), ("VS", "source"))
PATH_POLICY = "每个工况/频偏独立：本次 VCO HB → 当前运行目录 replica/netlist/waveforms；只使用网表相对路径，不复用旧 TB 的绝对 PWL 路径。私有全局 ISF_F0/ISF_T0 始终取本次 HB 基波/周期。"


def channel_variables(mapping=None):
    mapping = mapping or {}
    values = {semantic: str(mapping.get(semantic.lower() + "_pwl_variable") or "ISF_" + semantic + "_PWL_FILE")
              for semantic, _ in CHANNELS}
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) for value in values.values()):
        raise ValueError("波形文件全局变量名只能由字母、数字和下划线组成，不能以数字开头")
    if len(set(values.values())) != len(values):
        raise ValueError("VGS/VDS/VBS/VS 不能共用同一个波形文件变量")
    if {"ISF_F0", "ISF_T0"} & set(values.values()):
        raise ValueError("ISF_F0/ISF_T0 保留给本次基波频率和周期，不能用作波形文件变量")
    return values


def waveform_tables(wave, traces):
    """Canonical differential voltages plus source common mode, in one timebase."""
    period, f0 = float(wave["period"]), float(wave["f0"])
    times = [float(value) for value in wave["time"]]
    if not math.isfinite(period) or period <= 0 or not math.isfinite(f0) or f0 <= 0:
        raise ValueError("HB 周期/基波频率无效")
    if abs(period * f0 - 1) > 1e-6:
        raise ValueError("HB 周期与基波频率不一致")
    if len(times) < 3 or any(not math.isfinite(t) for t in times):
        raise ValueError("周期波形至少需要三个有限时间点")
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("周期波形时间轴必须严格递增")
    if abs(times[0]) > period * 1e-9 or abs(times[-1] - period) > period * 1e-6:
        raise ValueError("波形必须覆盖同一完整周期，从 0 到 T0")
    values = {}
    for _, terminal in CHANNELS:
        trace = traces.get(terminal)
        if trace not in wave:
            raise ValueError("VCO 缺少 %s 端子波形：%s" % (terminal, trace))
        data = [float(value) for value in wave[trace]]
        if len(data) != len(times) or any(not math.isfinite(v) for v in data):
            raise ValueError("VCO 端子波形长度不一致或含非有限值：%s" % terminal)
        if abs(data[-1] - data[0]) > max(1e-12, max(abs(v) for v in data) * 1e-9):
            raise ValueError("VCO 端子波形首尾未闭合：%s" % terminal)
        values[terminal] = data
    result = {}
    for semantic, terminal in CHANNELS:
        data = values[terminal] if semantic == "VS" else [a - b for a, b in zip(values[terminal], values["source"])]
        result[semantic] = "".join("%.17e %.17e\n" % pair for pair in zip(times, data))
    return result


def export_waveforms(wave, traces, netlist_dir, mapping=None, provenance=None):
    """Only write newly generated task-local waveform assets; never touch a TB."""
    netlist_dir = Path(netlist_dir)
    variables = channel_variables(mapping)
    tables = waveform_tables(wave, traces)
    folder = netlist_dir / "waveforms"
    if folder.is_symlink() or (folder / "manifest.json").is_symlink():
        raise ValueError("波形目录和清单不能是符号链接")
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "f0_hz": float(wave["f0"]), "period_s": float(wave["period"]),
                "samples": len(wave["time"]), "provenance": provenance or {}, "channels": []}
    for semantic, terminal in CHANNELS:
        relative = "waveforms/" + semantic.lower() + ".pwl"
        content = tables[semantic].encode("ascii")
        path = netlist_dir / relative
        if path.is_symlink() or folder.is_symlink():
            raise ValueError("波形输出目录不能包含符号链接")
        path.write_bytes(content)
        manifest["channels"].append({"semantic": semantic, "variable": variables[semantic],
            "relative_file": relative, "sha256": hashlib.sha256(content).hexdigest(),
            "positive_trace": traces[terminal], "negative_trace": "0" if semantic == "VS" else traces["source"]})
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return manifest


def validate_waveform_assets(netlist_dir):
    """Missing/changed files fail before simulation or cache reuse, even after moving."""
    root = Path(netlist_dir).resolve()
    path = root / "waveforms/manifest.json"
    if not path.exists():
        # A generated netlist advertising the contract must never lose it.
        netlist = root / "input.scs"
        if netlist.is_file() and "// ISF waveform-exchange-v1" in netlist.read_text(encoding="utf-8"):
            raise ValueError("波形交换清单缺失；请重新生成当前工况")
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    channels = manifest.get("channels", [])
    if manifest.get("schema_version") != 1 or {row.get("semantic") for row in channels} != {row[0] for row in CHANNELS} or len(channels) != 4:
        raise ValueError("波形交换清单不完整")
    for row in channels:
        relative = Path(row["relative_file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("波形必须使用当前网表内的相对路径")
        asset = root / relative
        if asset.is_symlink() or root not in asset.resolve().parents or not asset.is_file():
            raise ValueError("当前工况波形文件缺失或越界：" + str(relative))
        if hashlib.sha256(asset.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("当前工况波形文件校验不一致：" + str(relative))
    return manifest


def set_root_parameters(text, replacements):
    """Replace/add root parameters, keeping subckt defaults and other variables intact."""
    from .maestro_pair import _assignment_spans, _root_statements
    edits, found = [], set()
    for start, _, statement in _root_statements(text):
        if not re.match(r"(?i)^\s*parameters\b", statement):
            continue
        for name, begin, end in _assignment_spans(statement):
            if name in replacements:
                if name in found:
                    raise ValueError("全局参数重复声明：" + name)
                found.add(name)
                edits.append((start + begin, start + end, str(replacements[name])))
    for begin, end, replacement in sorted(edits, reverse=True):
        text = text[:begin] + replacement + text[end:]
    missing = {key: value for key, value in replacements.items() if key not in found}
    if missing:
        text += "\nparameters " + " ".join(key + "=" + str(value) for key, value in missing.items()) + "\n"
    return text


def prepare_dc_bias_sources(text, mapping):
    """Shared static/live preparation of mapped root sources for the Mdc baseline."""
    from .maestro_pair import _root_statements, _assignment_spans
    statements = list(_root_statements(text))
    root_parameters = [name for _, _, statement in statements
        if re.match(r"(?i)^\s*parameters\b", statement)
        for name, _, _ in _assignment_spans(statement)]
    source_edits = []
    for semantic in ("vgs", "vds", "vbs"):
        source = mapping.get(semantic + "_source")
        variable = mapping.get(semantic + "_variable")
        if variable and root_parameters.count(variable) != 1:
            raise ValueError("偏置变量必须在所选历史网表根层唯一定义：" + variable)
        if not source or not variable:
            continue
        matches = []
        for begin, end, statement in statements:
            match = re.match(r"^\s*" + re.escape(source) + r"\s*\(([^)]*)\)\s+vsource\b", statement)
            if match:
                matches.append((begin, end, "%s (%s) vsource dc=%s type=dc\n" % (source, match.group(1), variable)))
        if len(matches) != 1:
            raise ValueError("偏置源须位于所选噪声网表根层且唯一；暂不自动改层级内部源：" + source)
        source_edits.extend(matches)
    if len({begin for begin, _, _ in source_edits}) != len(source_edits):
        raise ValueError("多个偏置用途不能覆盖同一个电压源")
    for begin, end, replacement in sorted(source_edits, reverse=True):
        text = text[:begin] + replacement + text[end:]
    return text


def inspect_waveform_exchange(pair_report, engine=None):
    """Static executable preparation contract, explicitly not a completed simulation."""
    roles = pair_report.get("roles", {})
    vco, replica = roles.get("vco") or {}, roles.get("flicker") or {}
    generated = pair_report.get("pair", {}).get("replica_source") == "generated"
    vm = vco.get("mapping", {})
    if generated:
        from .generated_flicker import DC_MAPPING
        rm = DC_MAPPING
    else:
        rm = replica.get("mapping", {})
    traces = (engine or {}).get("physical_profile", {}).get("main", {}).get("traces", {})
    if not traces:
        traces = {terminal: vm.get(terminal) for _, terminal in CHANNELS}
    checks, rows = [], []
    def check(name, ok, detail):
        checks.append({"id": name, "status": "pass" if ok else "fail", "detail": detail})
    check("terminals", all(traces.get(terminal) for _, terminal in CHANNELS), "按所选目标器件验证 D/G/S/B；缺少命名 output 时按端子差分导出，不能把未知源极假设为地。")
    check("source_history", bool((vco.get("selected_history") or {}).get("source_netlists")), "需要已选择 VCO history 的可读网表；运行时在私有副本补充保存四端波形。")
    if generated:
        check("replica_generated", bool((engine or {}).get("device_subckt_file")),
              "从选定 VCO 工况继承目标 MOS 的实例参数、活动模型和温度，生成私有噪声网表。")
    else:
        check("replica_history", bool((replica.get("selected_history") or {}).get("source_netlists")), "需要已选择噪声 TB history 的可读网表，以保留器件参数和模型。")
    mapping_errors = [item.get("message", "信号电气对应无效") for report in (vco, replica)
        for item in report.get("mapping_issues", []) if item.get("severity") == "error"]
    check("electrical_mapping", not mapping_errors, "；".join(mapping_errors) if mapping_errors else "已选节点、命名输出、偏置源与目标器件电气对应有效。")
    for role, report in (("vco", vco), ("flicker", replica)):
        unique_paths = {record.get("path") for record in (report.get("selected_history") or {}).get("source_netlists", [])}
        try:
            for path in unique_paths:
                if not path or not Path(path).is_file() or Path(path).stat().st_size > 32 * 1024 * 1024:
                    raise ValueError("网表不可读或超过安全读取大小：%s" % path)
                text = Path(path).read_text(encoding="utf-8", errors="replace")
                if role == "flicker":
                    prepare_dc_bias_sources(text, rm)
                    analysis = rm.get("noise_analysis") or "noise"
                    if not re.search(r"(?m)^\s*" + re.escape(analysis) + r"\s*\([^)]*\)\s+noise\b", text):
                        raise ValueError("噪声基线分析在网表不存在：" + analysis)
                else:
                    if vm.get("periodic_analysis_type", "hb") != "hb":
                        raise ValueError("当前波形执行器需要 HB；不把 shooting PSS 结果冒充 HB")
                    for key, kind in (("periodic_analysis", "hb"), ("noise_analysis", "hbnoise")):
                        name = vm.get(key) or kind
                        if not re.search(r"(?m)^\s*" + re.escape(name) + r"\s+(?:\([^\n)]*\)\s+)?" + kind + r"\b", text):
                            raise ValueError("私有波形准备需要已选 %s 分析 %s；当前网表缺失或类型不匹配" % (kind, name))
            if unique_paths:
                check(role + "_netlist_prepare", True, "已读取所选工况网表并执行同一套偏置源准备检查；不会访问旧 PWL 文件或修改原 TB。")
        except (OSError, ValueError, TypeError, ValidationError) as exc:
            check(role + "_netlist_prepare", False, str(exc))
    variables = {}
    try:
        variables = channel_variables(rm)
        fixture_traces = {terminal: terminal for _, terminal in CHANNELS}
        fixture = {"f0": 1.0, "period": 1.0, "time": [0, .5, 1],
                   "gate": [1, 2, 1], "drain": [2, 3, 2], "source": [.2, .3, .2], "bulk": [.1, .2, .1]}
        tables = waveform_tables(fixture, fixture_traces)
        check("script_contract", len(tables) == 4, "已自检同一套波形生成代码：时间轴、差分、电平、文件全局变量和相对路径；尚未执行新 HB 仿真。")
    except (ValueError, KeyError, TypeError) as exc:
        check("script_contract", False, str(exc))
    cross = {row.get("semantic"): row for row in pair_report.get("cross_tb_mapping", [])}
    for semantic, terminal in CHANNELS:
        item = cross.get(semantic, {})
        named = vm.get(semantic.lower() + "_output")
        negative = "0" if semantic == "VS" else traces.get("source")
        rows.append({"semantic": semantic, "source_kind": item.get("vco_source", "named_waveform" if named else "pin_derived"),
            "source_output": named, "source_expression": "V(%s)-V(%s)" % (traces.get(terminal), negative),
            "source_nodes": [traces.get(terminal), negative],
            "destination_variable": rm.get(semantic.lower() + "_variable"), "destination_source": "私有 VS（源极公共电平）" if semantic == "VS" else rm.get(semantic.lower() + "_source"),
            "pwl_variable": variables.get(semantic), "relative_file": "waveforms/" + semantic.lower() + ".pwl",
            "scope": "Maestro global / 本次私有网表 global；旧 history 未定义时由脚本补齐", "status": "pass" if traces.get(terminal) and traces.get("source") else "fail",
            "detail": "周期仿真读取本次差分 PWL；另按相同波形采样 DC 偏置计算 Mdc 基线。源极公共电平通过独立 VS 波形保留。"})
    ok = all(item["status"] == "pass" for item in checks)
    failures = [item for item in checks if item["status"] == "fail"]
    blocked = bool(mapping_errors) and all(item["id"] == "electrical_mapping" for item in failures)
    summary = "波形交换脚本静态检查通过；真实波形将在本次 VCO 仿真后生成并逐文件校验。"
    if not ok:
        summary = ("受上游名称对应阻塞：" + mapping_errors[0] + "；并非已确认缺少 PWL 文件。"
                   if blocked else "波形交换检查未通过：" + failures[0]["detail"])
    return {"schema_version": 1, "status": "pass" if ok else "fail", "ok": ok, "checked": "static_prepare",
        "summary": summary, "blocked_by": ["electrical_mapping"] if blocked else [],
        "root_causes": mapping_errors,
        "checks": checks, "rows": rows, "path_policy": PATH_POLICY, "dc_baseline_required": True}
