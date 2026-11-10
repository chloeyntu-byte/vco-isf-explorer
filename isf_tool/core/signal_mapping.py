"""Read-only, explicit names and electrical-role mapping for portable Maestro TBs.

No calculator expressions are evaluated and no OA or simulation files are written.
Every selection retains its evidence; ambiguous candidates remain unselected.
"""

from __future__ import unicode_literals

import copy
import os
import re

from .transfer_family import parse_transfer_expression, preferred_transfer_choices, transfer_choices


MAX_NETLIST_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_DEVICES = 10000
_ANALYSES = frozenset(("hb", "pss", "hbnoise", "pnoise", "noise", "dc", "tran", "stb", "hbxf", "pxf", "hbac", "pac", "ac", "sp"))
_SOURCES = frozenset(("vsource", "isource", "iprobe", "ccvs", "vcvs", "vccs", "cccs"))
_SEMANTICS = ("vgs", "vds", "vbs")
_PORTS = {"vgs": (1, 2), "vds": (0, 2), "vbs": (3, 2)}
_ALIASES = {"vgs": ("vgs", "gatesource", "vgstos"), "vds": ("vds", "drainsource", "vdstos"), "vbs": ("vbs", "bulksource", "vbstos")}

# (key, label, required, help). These are semantic roles, never hard-coded TB names.
VCO_MAPPING_SCHEMA = (
    ("periodic_analysis", "周期稳态分析", True, "选择已启用的 HB/PSS 分析；当前内置执行器支持 HB。"),
    ("noise_analysis", "周期噪声分析", True, "选择已启用的 HBNOISE/PNOISE 分析。"),
    ("output_p", "差分输出正端", True, "优先读取噪声分析的正端节点；可以覆盖为已发现的节点。"),
    ("output_n", "差分输出负端", True, "与正端一起决定差分输出方向。"),
    ("transfer_output", "传递幅值系列 / 注入支路来源", False, "选择幅值输出系列（如 harm_mag）或单个输出；表达式用于定位本次结果读取来源，物理对应关系由用户确认。按本次噪声输出端重新测量完整复数响应，不直接采用原 mag 数值。未选择时明确使用私有漏到源注入。"),
    ("transfer_phase_output", "传递相位系列 / 输出", False, "选择相位系列（如 harm_ph）或单个输出；保留来源及表达式供对照，不强制与幅值来源相同。本次完整复数幅相由实际传递源重新测量，不拼接不同来源的幅值和相位。"),
    ("vgs_output", "栅源电压波形 VGS(t)", False, "对应另一 TB 的栅源偏压变量；支持节点电压差，以及网表证明源极接地的单端时域电压。复杂表达式仅供核对。"),
    ("vds_output", "漏源电压波形 VDS(t)", False, "对应另一 TB 的漏源偏压变量，不能与 VGS 对调。"),
    ("vbs_output", "体源电压波形 VBS(t)", False, "对应另一 TB 的体源偏压；源体同节点时可不指定。"),
    ("noise_output_at_offset", "指定频偏的相噪表达式", False, "保留 Maestro 输出名及表达式供对照；内置提取器读取原始噪声数据，不执行 calculator。"),
    ("target_instance", "待分析的噪声器件", False, "从电压波形的节点和真实网表推断；多个候选时需要选择。"),
    ("peer_instance", "配对器件", False, "从相同模型及另一输出支路推断；无法唯一确定时不自动选择。"),
    ("drain", "目标器件漏端 D", False, "来自所选器件的网表端口顺序，手动覆盖必须与真实连接一致。"),
    ("gate", "目标器件栅端 G", False, "来自所选器件的网表端口顺序。"),
    ("source", "目标器件源端 S", False, "来自所选器件的网表端口顺序。"),
    ("bulk", "目标器件体端 B", False, "来自所选器件的网表端口顺序。"),
    ("current_trace", "已有目标电流保存项（供对照）", False, "此项供对照。计算使用私有副本新增的零伏漏极探针；其实际名称在执行设置中显示并可覆盖。"),
    ("noise_trace", "已有器件噪声保存项", False, "仅列出已保存或表达式明确引用的噪声路径；不猜测 PDK 内部 primitive。"),
)
REPLICA_MAPPING_SCHEMA = (
    ("noise_analysis", "噪声分析", True, "选择已启用的独立 noise 分析。"),
    ("vgs_variable", "栅源偏压变量 VGS", True, "与 VCO 的 VGS(t) 对应；按电源连接或变量语义识别。"),
    ("vds_variable", "漏源偏压变量 VDS", True, "与 VCO 的 VDS(t) 对应；按电源连接或变量语义识别。"),
    ("vbs_variable", "体源偏压变量 VBS", False, "与 VCO 的 VBS(t) 对应；源体同电位时可不指定。"),
    ("vgs_source", "栅源偏置电压源", False, "选择真实连接目标栅源端的电压源；本次动态副本接入 VCO 的 VGS(t)。"),
    ("vds_source", "漏源偏置电压源", False, "选择真实连接目标漏源端的电压源，可经零伏电流探针连接。"),
    ("vbs_source", "体源偏置电压源", False, "选择真实连接目标体源端的电压源；源体短接时由脚本生成零电压。"),
    ("vgs_pwl_variable", "VGS 波形文件全局变量", False, "Maestro global 中保存 VGS 周期波形文件名的变量；运行时采用任务目录中的新波形。"),
    ("vds_pwl_variable", "VDS 波形文件全局变量", False, "Maestro global 中保存 VDS 周期波形文件名的变量；变量名可任意命名。"),
    ("vbs_pwl_variable", "VBS 波形文件全局变量", False, "Maestro global 中保存 VBS 周期波形文件名的变量；缺失时在任务中生成，明确显示来源。"),
    ("vs_pwl_variable", "源极共模 VS 波形文件全局变量", False, "保存 VCO 源极对地电压，供私有动态副本的 VS 源保留公共电平；不要求原噪声 TB 有对应电压源。原 Maestro 未声明时在本次任务中生成。"),
    ("noise_output_at_offset", "指定频偏的噪声表达式", False, "显示 Maestro 输出名供对照；计算读取 raw noise，不执行 calculator。"),
    ("output_p", "噪声测量正端", False, "读取 noise 分析的输出节点。"),
    ("output_n", "噪声测量负端", False, "读取 noise 分析的参考节点。"),
    ("dc_instance", "噪声 TB 中的目标器件", False, "用于确认栅源、漏源偏压与器件模型。"),
    ("dc_sense_source", "漏电流检测源", False, "读取实际零伏检测源或电流探针，必须与漏端支路一致。"),
    ("dc_transducer", "电流转电压元件", False, "读取 noise 输出上的受控源及其 probe 引用。"),
    ("dc_noise_output", "原始噪声输出正端", False, "用于执行配置，与噪声分析正端保持一致。"),
)


def _logical(name):
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def canonical_net(name):
    value = str(name or "").strip().lstrip("/").replace("/", ".")
    # Cadence's built-in global ground is emitted as node 0 by Spectre.
    return "0" if value == "gnd!" else value


def _unique(values):
    return sorted(set(str(value) for value in values if value not in (None, "", "nil")))


def _parameters(text):
    text = str(text or "")
    result, position = {}, 0
    while position < len(text):
        match = re.search(r"([^\s=]+)\s*=\s*", text[position:])
        if not match:
            break
        name = match.group(1)
        start = position + match.end()
        end, nesting, quoted, escaped = start, [], False, False
        while end < len(text):
            char = text[end]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = not quoted
            elif not quoted:
                if char in "([{":
                    nesting.append(char)
                elif char in ")]}":
                    if nesting:
                        nesting.pop()
                elif char.isspace() and not nesting:
                    break
            end += 1
        result[name] = text[start:end]
        position = max(end, start + 1)
    return result


def _lines(text):
    """Join the two Spectre continuation forms without evaluating expressions."""
    result, pending = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("*"):
            continue
        if line.startswith("+"):
            if pending:
                pending += " " + line[1:].strip()
            elif result:
                result[-1] += " " + line[1:].strip()
            continue
        if pending:
            line = pending + " " + line
            pending = ""
        if line.endswith("\\"):
            pending = line[:-1].strip()
        else:
            result.append(line)
    if pending:
        result.append(pending)
    return result


def parse_netlist(text, source="generated netlist", additional_texts=None):
    """Extract names and expand only subcircuits defined in this input file.

    External PDK includes are inventoried, never parsed or guessed. ``terminals``
    contains resolved hierarchical nets; ``local_terminals`` retains definition
    spelling so a runner can construct a checked local edit.
    """
    result = {"devices": [], "sources": [], "analyses": [], "parameters": [], "nets": [], "saves": [], "includes": [], "subcircuits": [], "warnings": []}
    definitions, top, scope = {}, [], None
    globals_ = {"0"}
    statements = [(source, line) for line in _lines(text)]
    for extra_source, extra_text in additional_texts or []:
        statements.extend((extra_source, line) for line in _lines(extra_text))
    for source, line in statements:
        tokens = line.split()
        if not tokens:
            continue
        lower = tokens[0].lower()
        if lower in ("subckt", "inline"):
            parts = tokens[1:] if lower == "subckt" else tokens[2:]
            if parts:
                scope = parts[0]
                definitions[scope] = {"name": scope, "ports": [p for p in parts[1:] if "=" not in p], "instances": []}
            continue
        if lower == "ends":
            scope = None
            continue
        if lower == "global":
            globals_.update(tokens[1:])
        if lower == "parameters":
            for name, expression in _parameters(line[len(tokens[0]):]).items():
                result["parameters"].append({"name": name, "expression": expression, "scope": scope or "", "source": source})
            continue
        if lower == "include":
            match = re.search(r'include\s+"([^"]+)"', line)
            if match:
                result["includes"].append({"name": match.group(1), "expression": line, "source": source})
            continue
        if lower == "save":
            for name in tokens[1:]:
                result["saves"].append({"name": name, "scope": scope or "", "source": source})
            continue
        match = re.match(r"^(\S+)\s*\(([^)]*)\)\s*(\S+)(.*)$", line)
        if match:
            name, nodes, model, tail = match.groups()
            record = {"name": name, "model": model, "terminals": nodes.split(), "parameters": _parameters(tail), "parameter_text": tail.strip(), "scope": scope or "", "definition": line, "source": source}
            if model.lower() in _ANALYSES:
                record["analysis_type"] = model.lower()
                result["analyses"].append(record)
            elif scope:
                definitions[scope]["instances"].append(record)
            else:
                top.append(record)
        elif len(tokens) > 1 and tokens[1].lower() in _ANALYSES:
            result["analyses"].append({"name": tokens[0], "analysis_type": tokens[1].lower(), "terminals": [], "parameters": _parameters(" ".join(tokens[2:])), "source": source})

    def expand(records, prefix="", port_map=None, stack=()):
        port_map = port_map or {}
        for item in records:
            if len(result["devices"]) >= MAX_EXPANDED_DEVICES:
                result["warnings"].append("层级器件数超过只读发现限制；请缩小 TB 或手动提供经过核对的执行配置。")
                return
            record = copy.deepcopy(item)
            name = prefix + item["name"]
            local = list(item["terminals"])
            nodes = [port_map.get(n, n if n in globals_ or not prefix else prefix + n) for n in local]
            record.update({"name": name, "local_name": item["name"], "local_terminals": local, "terminals": nodes, "hierarchy_path": prefix.rstrip(".")})
            result["devices"].append(record)
            if item["model"].lower() in _SOURCES:
                result["sources"].append(record)
            if item["model"] in definitions:
                sub = definitions[item["model"]]
                if item["model"] in stack or len(stack) >= 12 or len(nodes) != len(sub["ports"]):
                    result["warnings"].append("无法安全展开层级 %s；端口数量或递归层级需人工核对。" % name)
                    continue
                expand(sub["instances"], name + ".", dict(zip(sub["ports"], nodes)), stack + (item["model"],))
    expand(top)
    result["subcircuits"] = list(definitions.values())
    result["nets"] = _unique(list(globals_) + [node for item in result["devices"] for node in item["terminals"]] + [node for item in result["analyses"] for node in item["terminals"]])
    return result


def inspect_history_topology(history, selected_test=None):
    merged = parse_netlist("")
    merged["files"] = []
    seen = {}
    for entry in (history or {}).get("source_netlists", []):
        if selected_test and entry.get("test") and entry.get("test") != selected_test:
            continue
        path = entry.get("path")
        if not path or path in merged["files"]:
            continue
        try:
            if os.path.getsize(path) > MAX_NETLIST_BYTES:
                raise ValueError("网表超过只读发现大小限制")
            with open(path, "r", encoding="utf-8", errors="replace") as stream:
                main_text = stream.read()
            # Only read includes inside this bound generated-netlist tree. PDK
            # and installation includes remain visible inventory entries.
            root = os.path.realpath(os.path.dirname(path))
            pending, local_texts, visited = [(path, main_text)], [], {os.path.realpath(path)}
            total_bytes = len(main_text.encode("utf-8"))
            local_warnings = []
            while pending:
                parent_path, parent_text = pending.pop(0)
                for statement in _lines(parent_text):
                    match = re.match(r'include\s+"([^"]+)"', statement, re.I)
                    if not match:
                        continue
                    candidate = os.path.realpath(os.path.join(os.path.dirname(parent_path), match.group(1)))
                    if candidate in visited or os.path.commonpath([root, candidate]) != root:
                        continue
                    visited.add(candidate)
                    if len(visited) > 64:
                        local_warnings.append("本地 include 数超过只读限制；部分层级未解析。")
                        continue
                    try:
                        total_bytes += os.path.getsize(candidate)
                        if total_bytes > MAX_NETLIST_BYTES:
                            local_warnings.append("本地 include 总量超过只读限制；部分层级未解析。")
                            continue
                        with open(candidate, "r", encoding="utf-8", errors="replace") as stream:
                            included_text = stream.read()
                    except OSError as exc:
                        local_warnings.append("本地 include 不可读取 %s: %s" % (candidate, exc))
                        continue
                    local_texts.append((candidate, included_text))
                    pending.append((candidate, included_text))
            current = parse_netlist(main_text, source=path, additional_texts=local_texts)
            current["warnings"].extend(local_warnings)
        except (OSError, ValueError) as exc:
            merged["warnings"].append("%s: %s" % (path, exc))
            continue
        merged["files"].append(path)
        for key in ("devices", "sources", "analyses", "parameters", "saves", "includes", "subcircuits"):
            for record in current[key]:
                identity = (key, record.get("scope", ""), record["name"])
                old = seen.get(identity)
                if old is None:
                    merged[key].append(record)
                    seen[identity] = record
                elif key in ("devices", "sources") and (old["model"] != record["model"] or old["terminals"] != record["terminals"]):
                    old["ambiguous"] = True
                    merged["warnings"].append("不同 history 点的器件 %s 拓扑不同；不能共用一个自动映射。" % record["name"])
                if old is not None and key == "sources" and old.get("parameters") != record.get("parameters"):
                    old["voltage_constraint_ambiguous"] = True
        merged["nets"] = _unique(merged["nets"] + current["nets"])
        merged["warnings"].extend(current["warnings"])
    merged["warnings"] = _unique(merged["warnings"])
    return merged


def _output_name(record):
    name = record.get("name")
    return record.get("signal") if name in (None, "", "nil") else name


def _time_voltage_call(text):
    function = re.match(r'(\w+)\(', text).group(1).lower()
    if function == "vh":
        return False
    domain = re.search(r'\(\s*\x27(\w+)', text) or re.search(r'\?result\s+[\"\x27]([^\"\s)]+)', text)
    if not domain:
        return True
    allowed = {"tran", "hb_td", "pss_td"}
    if function == "vtime":
        allowed.update(("hb", "pss"))
    return domain.group(1).lower() in allowed


def voltage_pair(expression):
    """Recognize direct time voltages; a single node is referenced to ground.

    Examples: v("/g" ?result "hb_td")-v("/s" ?result "hb_td"),
    (vtime('hb "/g") - vtime('hb "/s")), VT("/g" "/s").
    """
    text = str(expression or "").strip()
    function = r'(?:v|vt|vtime|vh)'
    call = r"(?P<fn>" + function + r")\(\s*(?:'\w+\s+)?\"(?P<node>[^\"]+)\"(?:\s+\?result\s+(?:\"[^\"]+\"|'\w+))?\s*\)"
    matches = list(re.finditer(call, text, re.I))
    if len(matches) == 1:
        match = matches[0]
        before = text[:match.start()].strip(" ()\t\r\n")
        after = text[match.end():].strip(" ()\t\r\n")
        if not before and not after and _time_voltage_call(match.group()):
            return [canonical_net(match.group("node")), "0"]
    if len(matches) == 2:
        between = text[matches[0].end():matches[1].start()].strip(" ()\t\r\n")
        before = text[:matches[0].start()].strip(" ()\t\r\n")
        after = text[matches[1].end():].strip(" ()\t\r\n")
        if between == "-" and not before and not after:
            # vh is frequency-domain; it is catalogued but cannot stand for V(t).
            if not all(_time_voltage_call(match.group()) for match in matches):
                return None
            return [canonical_net(match.group("node")) for match in matches]
    differential = re.fullmatch(r'\(*\s*(?:v|vt)\(\s*"([^"]+)"\s+"([^"]+)"(?:\s+\?result\s+"[^"]+")?\s*\)\s*\)*', text, re.I)
    if differential and _time_voltage_call(text.lstrip(" (\t\r\n")):
        return [canonical_net(differential.group(1)), canonical_net(differential.group(2))]
    return None


def transfer_output_reference(expression):
    """Decode a narrow raw HBXF anchor, without evaluating calculator code."""
    reference = parse_transfer_expression(expression)
    if reference:
        reference['source'] = canonical_net(reference['expression_source'])
    return reference


def _zero_voltage_edges(topology):
    """Only proven constant-zero ideal voltage branches imply equal V(t).

    Do not equate nodes because they are called VSS, or because a sine/PWL
    source has a zero DC operating point. Parameters are never evaluated.
    """
    edges = []
    for item in topology.get("sources", []):
        if item.get("ambiguous") or item.get("voltage_constraint_ambiguous") or len(item.get("terminals", [])) != 2:
            continue
        model = str(item.get("model", "")).lower()
        params = {key.lower(): str(value).lower() for key, value in item.get("parameters", {}).items()}
        if model == "iprobe" and not params:
            edges.append(item)
        elif model == "vsource" and set(params).issubset({"dc", "type"}) and params.get("type", "dc") == "dc":
            # Explicit numeric zero only; no unknown defaults or expressions.
            if re.fullmatch(r'[+-]?(?:0+(?:\.0*)?|\.0+)(?:e[+-]?\d+)?', params.get("dc", "")):
                edges.append(item)
    return edges


def _voltage_equivalence(topology):
    parents = {}
    def representative(node):
        node = canonical_net(node)
        while node in parents and parents[node] != node:
            node = parents[node]
        return node
    evidence = _zero_voltage_edges(topology)
    for item in evidence:
        a, b = (representative(node) for node in item["terminals"])
        if a != b:
            parents[max(a, b)] = min(a, b)
    topology["voltage_equivalence"] = {
        "nodes": {node: representative(node) for node in topology.get("nets", [])},
        "evidence": [{"name": item["name"], "terminals": item["terminals"], "definition": item.get("definition", ""), "source": item.get("source", "")} for item in evidence],
    }
    return representative


def _semantic_name(name):
    logical = _logical(name)
    return [semantic for semantic, aliases in _ALIASES.items() if any(alias in logical for alias in aliases)]


def _mos_devices(topology):
    definitions = {item["name"] for item in topology.get("subcircuits", [])}
    return [item for item in topology.get("devices", []) if len(item["terminals"]) == 4 and item["model"] not in definitions and (re.search(r"mos|fet", item["model"], re.I) or item.get("local_name", item["name"]).lower().startswith("m")) and not item.get("ambiguous")]


def get_signal_catalog(test, topology):
    catalog = []
    for kind, records in (("output", test.get("outputs", [])), ("variable", test.get("variables", []))):
        for record in records:
            name = _output_name(record) if kind == "output" else record.get("name")
            if name:
                details = copy.deepcopy(record)
                if kind == "output":
                    details["voltage_pair"] = voltage_pair(record.get("expression"))
                    details['transfer_reference'] = transfer_output_reference(record.get('expression'))
                    details["usage"] = "reference; calculator is never executed"
                if kind == "variable":
                    details.setdefault("scope", "unknown")
                source = "Maestro global variable" if details.get("scope") == "global" else "Maestro selected test"
                catalog.append({"kind": kind, "name": name, "expression": record.get("expression") or record.get("signal") or "", "source": source, "details": details})
                if kind == "output":
                    # Inventory explicit raw-data references, including names the
                    # mapper does not use. This does not claim PSF availability.
                    for function, trace in re.findall(r'(getData|i|it|itime|ih)\(\s*(?:\x27\w+\s+)?"([^"]+)"', record.get("expression") or "", re.I):
                        catalog.append({"kind": "trace", "name": trace, "expression": record.get("expression") or "", "source": "Maestro output %s" % name, "details": {"function": function, "availability": "expression-reference-only"}})
    for analysis in test.get("analyses", []):
        catalog.append({"kind": "analysis", "name": analysis["name"], "expression": "", "source": "Maestro active.state", "details": copy.deepcopy(analysis)})
    for model in test.get("models", []):
        if model.get("path"):
            catalog.append({"kind": "model", "name": model["path"], "expression": "section=%s" % (model.get("section") or "(default)"), "source": "Maestro selected test model setup", "details": copy.deepcopy(model)})
    for kind, key in (("device", "devices"), ("source", "sources"), ("parameter", "parameters"), ("save", "saves"), ("include", "includes"), ("subcircuit", "subcircuits"), ("analysis", "analyses")):
        for record in topology.get(key, []):
            catalog.append({"kind": kind, "name": record["name"], "expression": record.get("expression") or record.get("definition") or "", "source": record.get("source") or "generated netlist", "details": copy.deepcopy(record)})
    nets = list(topology.get("nets", []))
    for analysis in test.get("analyses", []):
        for key in ("p", "n", "pnode", "nnode", "outputp", "outputn"):
            value = analysis.get("fields", {}).get(key)
            if value:
                nets.append(value)
    for output in test.get("outputs", []):
        if output.get("signal"):
            nets.append(output["signal"])
        for node in voltage_pair(output.get("expression")) or []:
            nets.append(node)
    for net in _unique(nets):
        catalog.append({"kind": "net", "name": net, "expression": "", "source": "generated netlist / Maestro analysis or output", "details": {"canonical_name": canonical_net(net)}})
    return catalog


def _noise_outputs(outputs, offset_hz):
    names = {record["name"]: record for record in outputs}
    def is_noise(record, visited=()):
        name = record["name"]
        text = "%s %s" % (name, record["expression"])
        if re.search(r"noise|flicker|\bpn\b|phase.?noise|(?:^|_)pn(?:_|$)", text, re.I):
            return True
        if name in visited:
            return False
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", record["expression"])
        return any(token in names and is_noise(names[token], visited + (name,)) for token in tokens)
    candidates = []
    for output in outputs:
        if not is_noise(output):
            continue
        expression = output["expression"]
        values = [float(value) for value in re.findall(r"(?<![A-Za-z_])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", expression)]
        if any(abs(value - offset_hz) <= max(offset_hz * 1e-9, 1e-6) for value in values):
            candidates.append(output["name"])
    return _unique(candidates)


def discover_signal_mapping(test, role, history=None, overrides=None, offset_hz=10000.0):
    role = "vco" if role == "vco" else "replica"
    topology = inspect_history_topology(history, test.get("name"))
    equivalent = _voltage_equivalence(topology)
    def matches_voltage(pair, device, semantic):
        a, b = _PORTS[semantic]
        return bool(pair) and [equivalent(node) for node in pair] == [equivalent(device["terminals"][index]) for index in (a, b)]
    catalog = get_signal_catalog(test, topology)
    schema = VCO_MAPPING_SCHEMA if role == "vco" else REPLICA_MAPPING_SCHEMA
    issues, fields, mapping = [], [], {}
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict):
        issues.append({"key": "mapping_overrides", "severity": "error", "message": "手动覆盖必须是名称到字符串的对象。"})
        overrides = {}
    valid_keys = {item[0] for item in schema}
    for key in overrides:
        if key not in valid_keys:
            issues.append({"key": key, "severity": "error", "message": "未知的手动映射字段 %s；请删除旧覆盖后重新检测。" % key})
    outputs = [record for record in catalog if record["kind"] == "output"]
    output_names = _unique(record["name"] for record in outputs)
    variables = _unique(record["name"] for record in catalog if record["kind"] == "variable" or (record["kind"] == "parameter" and not record["details"].get("scope")))
    nets = _unique(record["name"] for record in catalog if record["kind"] == "net")
    devices = _mos_devices(topology)
    device_names = _unique(item["name"] for item in devices)
    by_device = {item["name"]: item for item in devices}
    analyses = []
    disabled = set()
    for item in test.get("analyses", []):
        if not item.get("enabled"):
            disabled.add(item["name"])
        else:
            analyses.append({"name": item["name"], "analysis_type": item["name"], "fields": item.get("fields", {}), "terminals": [item.get("fields", {}).get("p"), item.get("fields", {}).get("n")]})
    for item in topology.get("analyses", []):
        if item["name"] not in disabled and not any(existing["name"] == item["name"] for existing in analyses):
            analyses.append(dict(item, fields=item["parameters"]))
    definitions = {item[0]: item for item in schema}

    def candidate_kind(key):
        if key.endswith('_variable'):
            return 'variable'
        if key.endswith('_output') or key in ('transfer_output', 'transfer_phase_output', 'noise_output_at_offset'):
            return 'net' if key == 'dc_noise_output' else 'output'
        if key.endswith('_analysis'):
            return 'analysis'
        if key.endswith('_instance'):
            return 'device'
        if key.endswith('_source') or key == 'dc_transducer':
            return 'source'
        if key.endswith('_trace'):
            return 'trace'
        return 'net'

    def choose(key, candidates, detected=None, source="自动识别", expected=None):
        candidates = _unique(candidates)
        evidence = _unique(detected if isinstance(detected, (list, tuple)) else [detected])
        auto = evidence[0] if len(evidence) == 1 else None
        raw = overrides.get(key, "")
        valid_string = isinstance(raw, str)
        manual = raw.strip() if valid_string else ""
        kind = candidate_kind(key)
        if manual and kind in ('net', 'device', 'source') and manual not in candidates:
            aliases = [name for name in candidates if canonical_net(name) == canonical_net(manual)]
            if aliases:
                if kind == 'net':
                    # Keep the user's familiar /schematic/net spelling visible.
                    candidates = _unique(candidates + [manual])
                else:
                    manual = aliases[0]
        value = manual or auto
        status = "manual" if manual else ("auto" if auto else ("ambiguous" if len(evidence) > 1 else "missing"))
        if not valid_string:
            value, status = None, "invalid"
            issues.append({"key": key, "severity": "error", "message": "手动覆盖必须是一个名称字符串；留空表示自动。"})
        elif manual and manual not in candidates:
            value, status = None, "invalid"
            reference_only = key in ('noise_output_at_offset', 'current_trace')
            issues.append({"key": key, "severity": "warning" if reference_only else "error",
                           "message": ("参考名称 %s 在当前 TB/test/history 中不存在；参考项已失效，不影响原始数据计算，可重新选择或清空。" if reference_only
                                       else "手动名称 %s 在当前 TB/test/history 中不存在；请重新选择或清空以自动识别。") % manual})
        elif manual and kind == 'net' and topology.get('files') and canonical_net(manual) not in {canonical_net(node) for node in topology.get('nets', [])}:
            value, status = None, 'invalid'
            issues.append({'key': key, 'severity': 'error', 'message': '节点 %s 只出现在 Maestro 声明中，所选 history 的真实网表不存在该节点；请选匹配 history。' % manual})
        if len(evidence) > 1 and not manual:
            issues.append({"key": key, "severity": "warning", "message": "存在多个候选：%s；请在高级设置中选择，未自动绑定第一个。" % ", ".join(evidence)})
        if manual and expected and canonical_net(manual) != canonical_net(expected):
            status = "invalid"
            issues.append({"key": key, "severity": "error", "message": "覆盖 %s 与已选器件的真实连接 %s 不一致。" % (manual, expected)})
        _key, label, required, help_text = definitions[key]
        selected_record = next((record for record in catalog if record['kind'] == kind and record['name'] == value), {})
        details = copy.deepcopy(selected_record.get('details', {}))
        if kind == 'variable':
            details.setdefault('scope', 'netlist' if value in variables else 'unresolved')
            details['global_required'] = True
        field = {"key": key, "label": label, "value": value, "detected": auto, "candidates": candidates, "source": source, "status": status, "help": help_text, "required": required,
                 'candidate_kind': kind, 'editable': True, 'allow_custom': True,
                 'selected_details': details, 'role': role, 'candidate_count': len(candidates)}
        fields.append(field)
        mapping[key] = value
        return value

    periodic = None
    if role == "vco":
        names = [item["name"] for item in analyses if item["analysis_type"] in ("hb", "pss")]
        periodic = choose("periodic_analysis", names, names, "已启用的周期分析")
    types = ("hbnoise", "pnoise") if role == "vco" else ("noise", "hbnoise", "pnoise")
    names = [item["name"] for item in analyses if item["analysis_type"] in types]
    noise = choose("noise_analysis", names, names, "已启用的噪声分析")
    analysis = next((item for item in analyses if item["name"] == noise), {})
    periodic_record = next((item for item in analyses if item["name"] == periodic), {})
    for key in ("output_p", "output_n"):
        index = 0 if key == "output_p" else 1
        nodes = analysis.get("terminals", [])
        other_nodes = periodic_record.get("terminals", [])
        value = nodes[index] if len(nodes) > index and nodes[index] else (other_nodes[index] if len(other_nodes) > index else None)
        choose(key, nets, value, "已选噪声分析的 %s 端" % ("正" if index == 0 else "负"))
    topology["net_aliases"] = {}
    for key, index in (("output_p", 0), ("output_n", 1)):
        actual_analyses = [item for item in topology.get("analyses", []) if item["name"] == noise]
        if not actual_analyses:
            actual_analyses = [item for item in topology.get("analyses", []) if item["analysis_type"] == analysis.get("analysis_type")]
        if len(actual_analyses) == 1:
            actual_nodes = actual_analyses[0].get("terminals", [])
            state_nodes = analysis.get("terminals", [])
            if len(actual_nodes) > index and len(state_nodes) > index and state_nodes[index]:
                if canonical_net(state_nodes[index]) == canonical_net(actual_nodes[index]):
                    topology["net_aliases"][state_nodes[index]] = actual_nodes[index]
                    topology["net_aliases"][canonical_net(state_nodes[index])] = actual_nodes[index]
                else:
                    issues.append({"key": key, "severity": "error", "message": "当前 Maestro 的噪声节点 %s 与所选 history 网表节点 %s 不一致；请选择匹配的 history，不能将不同节点当作别名。" % (state_nodes[index], actual_nodes[index])})
        value = mapping.get(key)
        mapping[key + "_netlist"] = topology["net_aliases"].get(value, canonical_net(value)) if value else None
    mapping["periodic_analysis_type"] = periodic_record.get("analysis_type")
    mapping["noise_analysis_type"] = analysis.get("analysis_type")
    mapping["noise_analysis_fields"] = analysis.get("fields", {})
    choose("noise_output_at_offset", output_names, _noise_outputs(outputs, offset_hz), "表达式依赖与 %.6g Hz 频偏（仅对照，不执行 calculator）" % offset_hz)

    semantic_outputs = {}
    if role == "vco":
        requested_target = next((device for device in devices if canonical_net(device['name']) == canonical_net(overrides.get('target_instance'))), None)
        for semantic in _SEMANTICS:
            candidates = []
            for output in outputs:
                pair = output["details"].get("voltage_pair")
                ports = _PORTS[semantic]
                matches = [device for device in ([requested_target] if requested_target else devices) if matches_voltage(pair, device, semantic)]
                # When topology and a recognizable expression exist, electrical
                # meaning outranks an arbitrary (possibly misleading) label.
                if matches or (not pair or not devices) and semantic in _semantic_name(output["name"]):
                    candidates.append(output["name"])
            semantic_outputs[semantic] = choose(semantic + "_output", output_names, candidates, "输出表达式的节点差与电压语义")
        matched_devices = list(devices)
        constrained = False
        for semantic, output_name in semantic_outputs.items():
            output = next((item for item in outputs if item["name"] == output_name), {})
            pair = output.get("details", {}).get("voltage_pair")
            if pair:
                constrained = True
                a, b = _PORTS[semantic]
                matched_devices = [device for device in matched_devices if matches_voltage(pair, device, semantic)]
        target_candidates = [item["name"] for item in matched_devices] if constrained else device_names
        if constrained and devices and not matched_devices:
            issues.append({"key": "target_instance", "severity": "error", "message": "所选 VGS/VDS/VBS 波形的节点差无法对应同一个真实器件；请按栅源、漏源、体源的电气含义核对。"})
        target = choose("target_instance", device_names, target_candidates, "电压波形的节点差与真实器件端口" if constrained else "实际网表 MOS 候选")
        device = by_device.get(target, {})
        peer_candidates = [item["name"] for item in devices if target and item["name"] != target and item["model"] == device["model"] and canonical_net(item["terminals"][0]) in (canonical_net(mapping.get("output_p")), canonical_net(mapping.get("output_n"))) and canonical_net(item["terminals"][0]) != canonical_net(device["terminals"][0])]
        choose("peer_instance", [name for name in device_names if name != target], peer_candidates, "相同器件模型、另一差分输出支路")
        for index, key in enumerate(("drain", "gate", "source", "bulk")):
            value = device.get("terminals", [None] * 4)[index]
            choose(key, nets, value, "所选器件的 D/G/S/B 端口", expected=value)
        transfer_records = {}
        transfer_errors = {}
        for output in outputs:
            reference = output['details'].get('transfer_reference')
            if not reference:
                continue
            name = output['name']
            transfer_records[name] = dict(reference, output_name=name, expression=output['expression'])
            selected_analyses = [item for item in analyses if item['name'] == reference['analysis'] and item['analysis_type'] in ('hbxf', 'pxf')]
            actual_sources = [item for item in topology.get('sources', [])
                              if canonical_net(item['name']) == reference['source'] and not item.get('ambiguous')]
            actual = actual_sources[0] if len(actual_sources) == 1 else {}
            terminals = actual.get('terminals', [])
            expected = [mapping.get('drain'), mapping.get('source')]
            # Preserve the historical orientation when it is directly known;
            # other explicitly declared sources are accepted in their own +1
            # direction. This is an observation/default, not a binding gate.
            factor, direction_basis = 1, 'user_declared'
            if all(expected) and len(terminals) == 2:
                normalized, wanted = [equivalent(node) for node in terminals], [equivalent(node) for node in expected]
                if wanted[0] != wanted[1] and normalized == wanted[::-1]:
                    factor, direction_basis = -1, 'topology_reverse'
                elif wanted[0] != wanted[1] and normalized == wanted:
                    direction_basis = 'topology_forward'
            nodes = selected_analyses[0].get('terminals', []) if len(selected_analyses) == 1 else []
            transfer_records[name].update(mode='existing_source', source=actual.get('name', reference['source']),
                source_terminals=terminals, direction_factor=factor, direction_basis=direction_basis,
                original_output=list(nodes), effective_output=[mapping.get('output_p_netlist'), mapping.get('output_n_netlist')],
                algorithm_harmonic=reference['sideband'] + 1, source_definition=actual.get('definition'),
                consumption_mode='source_binding_remeasured',
                notice='由 %s 定位 %s / %s；本次输出 %s−%s，方向因子 %s（%s）；重新测量完整复数，不采用原幅值数值。' %
                    (name, reference['analysis'], actual.get('name', reference['source']), mapping.get('output_p_netlist'), mapping.get('output_n_netlist'), factor,
                     '已知反向支路，沿用原换向' if direction_basis == 'topology_reverse' else '已知同向支路' if direction_basis == 'topology_forward' else '按用户声明的来源方向'))
        transfer_records, transfer_errors, transfer_details, transfer_families = transfer_choices(outputs, transfer_records, transfer_errors)
        transfer_candidates = output_names + list(transfer_families)
        preferred = preferred_transfer_choices(transfer_records, transfer_errors, transfer_families, 'magnitude')
        if not preferred:
            preferred = preferred_transfer_choices(transfer_records, transfer_errors, transfer_families, 'complex')
        selected_transfer = choose('transfer_output', transfer_candidates, preferred,
            '输出系列及表达式中的结果读取来源（物理绑定由用户确认）')
        if selected_transfer and selected_transfer not in transfer_records and selected_transfer not in transfer_errors:
            transfer_errors[selected_transfer] = '该命名输出不能静态对应 HBXF/PXF 的原始电流源与边带；不能将任意 Calculator 数值当作复数传递响应。'
        if selected_transfer and selected_transfer in transfer_errors:
            issues.append({'key': 'transfer_output', 'severity': 'error', 'message': transfer_errors[selected_transfer]})
            mapping['transfer_contract'] = {'mode': 'invalid', 'output_name': selected_transfer}
        elif selected_transfer:
            mapping['transfer_contract'] = transfer_records[selected_transfer]
            mapping['transfer_analysis'] = transfer_records[selected_transfer]['analysis']
        elif overrides.get('transfer_output'):
            mapping['transfer_contract'] = {'mode': 'invalid', 'output_name': overrides['transfer_output']}
        else:
            mapping['transfer_contract'] = {'mode': 'private_injection', 'source': None,
                'direction_factor': 1, 'sideband': 0, 'algorithm_harmonic': 1,
                'notice': '使用私有副本新增的漏到源注入；没有采用 Maestro 幅值输出。'}
        anchor = mapping['transfer_contract']
        phase_candidates = preferred_transfer_choices(transfer_records, transfer_errors, transfer_families, 'phase', anchor)
        phase_output = choose('transfer_phase_output', transfer_candidates, phase_candidates,
                              '相位输出系列及读取来源（保留对照，不重复校验物理绑定）')
        if phase_output:
            phase = transfer_records.get(phase_output, {})
            if phase_output in transfer_errors or phase.get('mode') != 'existing_source':
                issues.append({'key': 'transfer_phase_output', 'severity': 'error', 'message': transfer_errors.get(phase_output) or '请选择能够识别原始电流源和边带的 phase/phaseDeg 输出。'})
            else:
                if anchor.get('mode') == 'private_injection':
                    mapping['transfer_contract'] = dict(phase, phase_output_name=phase_output)
                    mapping['transfer_analysis'] = phase['analysis']
                mapping['transfer_contract']['phase_output_name'] = phase_output
                mapping['transfer_contract']['phase_reference'] = {
                    key: copy.deepcopy(phase.get(key)) for key in ('output_name', 'source', 'analysis', 'sideband', 'view', 'expression', 'family')}
                mapping['transfer_contract']['phase_reference']['consumption_mode'] = 'reference_only_remeasured_complex'
                mapping['transfer_contract']['phase_reference']['used_as_transfer_source'] = anchor.get('mode') == 'private_injection'
                if phase.get('family'):
                    mapping['transfer_contract']['phase_family'] = copy.deepcopy(phase['family'])
        for field in fields:
            if field['key'] not in ('transfer_output', 'transfer_phase_output'):
                continue
            field['candidate_details'] = copy.deepcopy(transfer_details)
            if field['value'] in transfer_details:
                field['selected_details'] = copy.deepcopy(transfer_details[field['value']])
            if field['key'] == 'transfer_phase_output':
                field['selected_details']['role_consumption'] = '参考来源；本次幅相从实际传递源重测完整复数，不合成不同来源。'
            field_errors = [item['message'] for item in issues if item['key'] == field['key'] and item['severity'] == 'error']
            if field_errors:
                field['status'] = 'invalid'
                field['selected_details']['errors'] = field_errors
            field['selection_kind'] = field['selected_details'].get('kind', 'transfer_output')
        traces = _unique(record["name"] for record in catalog if record["kind"] in ("save", "trace") or (record["kind"] == "output" and record["details"].get("signal")))
        choose("current_trace", traces, [name for name in traces if target and (name.startswith(target + ":") or name.startswith("/" + target.replace(".", "/") + "/"))], "已有 save/信号输出")
        choose("noise_trace", traces, [name for name in traces if target and name.startswith(target + ".") and "noise" in name.lower()], "已明确保存的噪声路径")
    else:
        sources = topology.get("sources", [])
        source_by_name = {canonical_net(item['name']): item for item in sources}
        transducers = [item for item in sources if item["model"].lower() == "ccvs" and len(item["terminals"]) == 2
                       and canonical_net(item["terminals"][0]) == canonical_net(mapping.get("output_p"))
                       and canonical_net(item["terminals"][1]) == canonical_net(mapping.get("output_n"))]
        manual_sense = canonical_net(overrides.get('dc_sense_source'))
        manual_transducer = canonical_net(overrides.get('dc_transducer'))
        selected_readouts = [item for item in transducers if not manual_transducer or canonical_net(item['name']) == manual_transducer]
        probe_names = [manual_sense] if manual_sense else [canonical_net(item.get('parameters', {}).get('probe')) for item in selected_readouts]
        linked_devices = [item['name'] for item in devices if any(item['terminals'][0] in source_by_name.get(probe, {}).get('terminals', []) for probe in probe_names)]
        target = choose("dc_instance", device_names, linked_devices or device_names, "检测支路连接的 MOS 候选（可手动选择器件或检测源）")
        device = by_device.get(target, {})
        # Bias names follow electrical connectivity, not the spelling of VGS/VDS.
        zero_edges = [item["terminals"] for item in _zero_voltage_edges(topology)]
        def connected(node):
            reached = {node}
            for _index in range(len(zero_edges) + 1):
                before = len(reached)
                for edge in zero_edges:
                    if set(edge) & reached:
                        reached.update(edge)
                if len(reached) == before:
                    break
            return reached
        semantic_variables = {}
        for semantic in _SEMANTICS:
            inferred = []
            bias_sources = []
            if device:
                a, b = _PORTS[semantic]
                positive, negative = connected(device["terminals"][a]), connected(device["terminals"][b])
                for item in sources:
                    nodes = item["terminals"]
                    expression = item["parameters"].get("dc")
                    if item['model'].lower() == 'vsource' and len(nodes) == 2 and nodes[0] in positive and nodes[1] in negative and positive != negative:
                        bias_sources.append(item)
                        if expression in variables:
                            inferred.append(expression)
            chosen_source = choose(semantic + '_source', [item['name'] for item in sources if item['model'].lower() == 'vsource'],
                                   [item['name'] for item in bias_sources], '偏置源与目标器件端口的真实连接（含零伏探针等价连接）')
            actual_source = next((item for item in sources if item['name'] == chosen_source), {})
            if chosen_source and chosen_source not in [item['name'] for item in bias_sources] and device:
                issues.append({'key': semantic + '_source', 'severity': 'error', 'message': '所选电压源不对应目标器件的 %s 正负端；请按真实连接选择。' % semantic.upper()})
            if actual_source:
                inferred = [actual_source.get('parameters', {}).get('dc')] if actual_source.get('parameters', {}).get('dc') in variables else []
            if not inferred:
                inferred = [name for name in variables if semantic in _semantic_name(name)]
            semantic_variables[semantic] = _unique(inferred)
            bias_variable = choose(semantic + "_variable", variables, inferred, "实际偏置电源的 dc 参数与 D/G/S/B 连接；名称为辅助")
            if bias_variable and actual_source and actual_source.get('parameters', {}).get('dc') != bias_variable:
                issues.append({'key': semantic + '_variable', 'severity': 'error', 'message': '变量 %s 未直接驱动已选 %s 的 dc 参数，不能假称已影响偏置。' % (bias_variable, chosen_source)})
            file_reference = actual_source.get('parameters', {}).get('file', '')
            if file_reference not in variables:
                file_reference = None
            pwl_candidates = [file_reference] if file_reference else [name for name in variables
                if semantic in _semantic_name(name) and re.search(r'pwl|wave.*file|file.*wave', name, re.I)]
            pwl_variable = choose(semantic + '_pwl_variable', variables, pwl_candidates,
                                  '已选电压源的 file 变量，或已声明的周期波形文件 global 变量')
            if pwl_variable:
                record = next((item for item in catalog if item['kind'] == 'variable' and item['name'] == pwl_variable), {})
                expression = str(record.get('expression', '')).strip()
                if expression and re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[a-zA-Z]*', expression):
                    issues.append({'key': semantic + '_pwl_variable', 'severity': 'error', 'message': '波形文件变量必须保存文件名字符串；%s 当前是数值变量。' % pwl_variable})
        vs_globals = [item['name'] for item in catalog if item['kind'] == 'variable'
                      and item['name'] == 'ISF_VS_PWL_FILE' and item['details'].get('scope') == 'global']
        vs_variable = choose('vs_pwl_variable', variables, vs_globals,
                             '私有 VS 源的共模波形文件变量；不要求原 TB 存在 VS 电压源')
        if vs_variable:
            record = next((item for item in catalog if item['kind'] == 'variable' and item['name'] == vs_variable), {})
            expression = str(record.get('expression', '')).strip()
            if expression and re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[a-zA-Z]*', expression):
                issues.append({'key': 'vs_pwl_variable', 'severity': 'error', 'message': 'VS 共模波形文件变量必须保存文件名字符串；%s 当前是数值变量。' % vs_variable})
        if manual_sense:
            transducers = [item for item in transducers if canonical_net(item.get('parameters', {}).get('probe')) == manual_sense]
        elif device:
            transducers = [item for item in transducers if device['terminals'][0] in source_by_name.get(canonical_net(item.get('parameters', {}).get('probe')), {}).get('terminals', [])]
        transducer = choose("dc_transducer", [item["name"] for item in sources if item["model"].lower() == "ccvs"], [item["name"] for item in transducers], "噪声输出和已选电流检测源共同约束的电流转电压元件")
        chosen = next((item for item in sources if item["name"] == transducer), {})
        sense = chosen.get("parameters", {}).get("probe")
        actual_sense = choose("dc_sense_source", [item["name"] for item in sources if item["model"].lower() in ("vsource", "iprobe")], sense, "受控源 probe 引用；手选后同步定位检测支路与目标器件")
        sense_record = source_by_name.get(canonical_net(actual_sense), {})
        if chosen and (len(chosen.get('terminals', [])) != 2 or [canonical_net(node) for node in chosen['terminals']] != [canonical_net(mapping.get('output_p')), canonical_net(mapping.get('output_n'))]):
            issues.append({'key': 'dc_transducer', 'severity': 'error', 'message': '所选电流转电压元件未连接所选 noise 正负测量节点。'})
        if chosen and actual_sense and canonical_net(sense) != canonical_net(actual_sense):
            issues.append({'key': 'dc_sense_source', 'severity': 'error', 'message': '所选检测源与电流转电压元件的 probe 引用不同，请同时选择对应读出元件。'})
        if actual_sense and device:
            valid_zero = any(item['name'] == sense_record.get('name') for item in _zero_voltage_edges(topology))
            if not valid_zero or device['terminals'][0] not in sense_record.get('terminals', []):
                issues.append({'key': 'dc_sense_source', 'severity': 'error', 'message': '检测源必须是连接目标 MOS 漏极的零伏电压源或理想电流探针；偏置电源和其他支路不能替代。'})
        choose("dc_noise_output", nets, mapping.get("output_p"), "noise 分析正端", expected=mapping.get("output_p"))
        for semantic in _SEMANTICS:
            selected = mapping.get(semantic + "_variable")
            others = [other for other in _SEMANTICS if other != semantic and selected in semantic_variables[other]]
            if selected and others and selected not in semantic_variables[semantic]:
                issues.append({"key": semantic + "_variable", "severity": "error", "message": "%s 实际对应 %s，不能作为 %s 偏压。" % (selected, "/".join(others).upper(), semantic.upper())})

    for semantic, output_name in semantic_outputs.items():
        if not output_name:
            continue
        output = next((item for item in outputs if item["name"] == output_name), {})
        pair = output.get("details", {}).get("voltage_pair")
        names = _semantic_name(output_name)
        if names and semantic not in names:
            issues.append({"key": semantic + "_output", "severity": "info", "message": "%s 的名称与 %s 电压语义不同；实际对应关系以表达式的节点差和器件端口为准。" % (output_name, semantic.upper())})
        if not pair:
            issues.append({"key": semantic + "_output", "severity": "warning", "message": "%s 不是可静态识别的两节点时域电压差；表达式仅供对照，不会执行 calculator。请选择明确波形或手动选择器件端口。" % output_name})
        elif mapping.get("target_instance"):
            device = by_device.get(mapping["target_instance"], {})
            a, b = _PORTS[semantic]
            expected = [canonical_net(device["terminals"][a]), canonical_net(device["terminals"][b])]
            if not matches_voltage(pair, device, semantic):
                issues.append({"key": semantic + "_output", "severity": "error", "message": "%s 的节点差 %s 与所选器件 %s 的 %s %s 不一致。" % (output_name, pair, mapping["target_instance"], semantic.upper(), expected)})
    for warning in topology["warnings"]:
        issues.append({"key": "topology", "severity": "warning", "message": warning})
    for field in fields:
        if any(issue["key"] == field["key"] and issue["severity"] == "error" for issue in issues):
            field["status"] = "invalid"
        key = field['key']
        reference_only = key in ('noise_output_at_offset', 'current_trace')
        field.update(active=not reference_only, group='execution_detail',
                     consumption_mode='private_netlist', used_by=['private Spectre preparation'])
        if key in ('output_p', 'output_n', 'target_instance', 'peer_instance', 'transfer_output', 'transfer_phase_output', 'dc_instance', 'dc_sense_source', 'dc_transducer'):
            field.update(group='signal', consumption_mode='raw_signal', used_by=['flicker-noise extraction'])
        if key in ('transfer_output', 'transfer_phase_output'):
            field.update(consumption_mode='source_binding_remeasured' if mapping['transfer_contract']['mode'] == 'existing_source' else mapping['transfer_contract']['mode'],
                         used_by=['signed HBXF transfer H[k]', 'ISF calculation'])
            field['help'] += ' ' + mapping['transfer_contract'].get('notice', '')
        if key == 'vs_pwl_variable' or any(key == semantic + suffix for semantic in _SEMANTICS for suffix in ('_output', '_variable', '_source', '_pwl_variable')):
            field.update(group='cross_tb', consumption_mode='raw_voltage_pair' if key.endswith('_output') else 'netlist_bias_parameter',
                         used_by=['VCO periodic bias → current TB noise sampling'])
            field['active'] = True
            if key.endswith('_output'):
                semantic = key[:3]
                device = by_device.get(mapping.get('target_instance'), {})
                nodes = device.get('terminals', [])
                if len(nodes) == 4:
                    a, b = _PORTS[semantic]
                    field['effective_derivation'] = 'V(%s)-V(%s)' % (nodes[a], nodes[b])
                    field['selected_details']['effective_nodes'] = [nodes[a], nodes[b]]
                field['maestro_presence'] = 'named_output' if field['value'] else ('script_derived' if len(nodes) == 4 else 'unresolved')
                if not field['value'] and len(nodes) == 4:
                    field['source'] = 'Maestro 缺少对应 output；脚本按真实器件端口提取'
                    field['help'] += ' 实际使用 %s；这不是已有 Maestro output。' % field['effective_derivation']
                pair = field['selected_details'].get('voltage_pair')
                if pair and len(nodes) == 4 and pair[1] == '0' and canonical_net(nodes[2]) != '0':
                    field['selected_details']['single_ended_ground_proof'] = copy.deepcopy(topology['voltage_equivalence']['evidence'])
                    field['help'] += ' 该命名输出是单端对地电压；网表零伏连接证明源端与地等电位后才作为器件端电压。'
            elif key.endswith('_pwl_variable'):
                field['consumption_mode'] = 'task_local_pwl_parameter'
                field['maestro_presence'] = 'global_variable' if field['selected_details'].get('scope') == 'global' else ('test_variable' if field['value'] else 'script_derived')
                if not field['value']:
                    field['source'] = 'Maestro 未声明；任务内生成周期波形文件'
            elif key.endswith('_source'):
                field['consumption_mode'] = 'periodic_bias_source'
            if key.endswith('_variable'):
                scope = field['selected_details'].get('scope', 'unresolved')
                field['scope'] = scope
                field['global_required'] = True
                if field['value'] and scope == 'test':
                    details = field['selected_details']
                    if details.get('shadows_global') or 'global_expression' in details:
                        message = '变量 %s 已存在全局变量，test 另有同名值（可能缓存或局部默认）；本次按已选 history 实际值恢复，不需要重复迁移。' % field['value']
                        severity = 'info'
                    else:
                        message = '变量 %s 当前为 test 局部作用域；便携配置要求移到 Maestro global。' % field['value']
                        severity = 'warning'
                    field['help'] += ' ' + message
                    issues.append({'key': key, 'severity': severity, 'message': message})
        if key == 'noise_trace':
            field.update(active=bool(field['value']), consumption_mode='raw_noise_contributor', used_by=['target flicker extraction'])
        if reference_only:
            field.update(active=False, group='reference', consumption_mode='reference_only', used_by=[])
        if field['status'] == 'invalid':
            field['active'] = True  # Invalid saved overrides must remain visible.
    order = {item[0]: index for index, item in enumerate(schema)}
    fields.sort(key=lambda item: order[item["key"]])
    return {"mapping": mapping, "mapping_fields": fields, "signal_catalog": catalog, "topology": topology, "mapping_issues": issues, "mapping_overrides": copy.deepcopy(overrides)}


def cross_tb_mapping(vco, replica):
    rows = []
    for semantic in _SEMANTICS:
        vco_key, replica_key = semantic + "_output", semantic + "_variable"
        vco_mapping = (vco or {}).get("mapping", {})
        source = vco_mapping.get(vco_key)
        destination = (replica or {}).get("mapping", {}).get(replica_key)
        errors = [issue for report, key in ((vco, vco_key), (replica, replica_key)) for issue in (report or {}).get("mapping_issues", []) if issue["key"] in (key, "target_instance", "dc_instance") and issue["severity"] == "error"]
        expression = None
        source_kind = "unresolved"
        if source:
            output = next((item for item in (vco or {}).get("signal_catalog", []) if item["kind"] == "output" and item["name"] == source), {})
            expression = output.get("expression")
            source_kind = "named_waveform" if voltage_pair(expression) else "reference_only"
        elif vco_mapping.get("target_instance"):
            positive_key = {"vgs": "gate", "vds": "drain", "vbs": "bulk"}[semantic]
            positive, negative = vco_mapping.get(positive_key), vco_mapping.get("source")
            if positive and negative:
                expression = "V(%s)-V(%s)" % (positive, negative)
                source, source_kind = expression, "pin_derived"
        if errors:
            status = "invalid"
        elif source_kind == "reference_only":
            status = "reference"
        elif source and destination:
            status = "derived" if source_kind == "pin_derived" else "mapped"
        else:
            status = "unresolved"
        help_text = "%s 波形对应 %s 偏压；按电气含义配对。" % (semantic.upper(), semantic.upper())
        if source_kind == "pin_derived":
            help_text += "使用私有 HB 运行的原始节点波形；无需创建或改名 Maestro output。"
        elif source_kind == "reference_only":
            help_text += "此表达式仅供对照，未建立可执行波形映射；清空覆盖并选择真实器件端口可按节点提取。"
        replica_mapping = (replica or {}).get('mapping', {})
        variable_record = next((item for item in (replica or {}).get('signal_catalog', []) if item['kind'] == 'variable' and item['name'] == destination), {})
        rows.append({"semantic": semantic.upper(), "vco_key": vco_key, "vco_value": source, "vco_expression": expression, "vco_source": source_kind, "replica_key": replica_key, "replica_value": destination, "status": status, "help": help_text,
                     'replica_source': replica_mapping.get(semantic + '_source'),
                     'pwl_variable': replica_mapping.get(semantic + '_pwl_variable'),
                     'scope': variable_record.get('details', {}).get('scope', 'unknown'), 'global_required': True})
    return rows
