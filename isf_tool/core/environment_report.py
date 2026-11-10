"""Read-only environment diagnostics for the desktop and command line."""

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping

from .preflight import _target_directory_status, detect_environment


_PYTHON_CHECK = """
import json, os

rows = {}
try:
    import numpy
    rows['numpy'] = {'status': 'pass', 'detail': numpy.__version__}
except Exception as error:
    rows['numpy'] = {'status': 'fail', 'detail': type(error).__name__}

stage = 'tk_import'
try:
    import tkinter
    stage = 'tcl_init'
    interpreter = tkinter.Tcl()
    rows['tcl'] = {'status': 'pass', 'detail': interpreter.call('info', 'patchlevel')}
    stage = 'tk_init'
    if os.environ.get('DISPLAY') or os.name == 'nt':
        interpreter.loadtk()
        interpreter.withdraw()
        rows['tk'] = {'status': 'pass', 'detail': interpreter.call('package', 'require', 'Tk')}
        interpreter.destroy()
    else:
        rows['tk'] = {'status': 'not_tested', 'detail': 'DISPLAY 未设置'}
except Exception as error:
    target = 'tk' if stage == 'tk_init' else 'tcl'
    rows[target] = {'status': 'fail', 'detail': stage + ' / ' + type(error).__name__}
    rows.setdefault('tk', {'status': 'not_tested', 'detail': '依赖 Tcl 初始化'})
print(json.dumps(rows))
"""


def _check_running_python():
    # A child can test actual Tk on the display without touching the active window.
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-I", "-c", _PYTHON_CHECK],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=5, check=True,
        )
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {"runtime": {"status": "fail", "detail": type(error).__name__}}


def inspect_environment(config=None, workspace=None, maestro_context=None):
    started = time.monotonic()
    passive = dict(config or {})
    context = maestro_context if isinstance(maestro_context, Mapping) else {}
    if not passive.get("project_root") and context.get("work_dir"):
        passive["project_root"] = context["work_dir"]
    # Diagnostics never grant consent to source a bridge or run native tools.
    passive["execution"] = {"dry_run": True, "allow_execute": False}
    detected = detect_environment(passive, workspace, maestro_context)
    python_checks = _check_running_python()
    analysis = detected["analysis_python"]
    if analysis.get("probe_deferred") and analysis.get("source") != "automatic":
        python_checks["analysis"] = {"status": "not_tested", "detail": analysis.get("candidate")}
    elif not analysis.get("executable"):
        python_checks["analysis"] = {"status": "fail", "detail": analysis.get("candidate") or "未识别"}
    target = _target_directory_status(detected["workspace"]["path"]) if workspace else None
    inherited = context.get("runtime_env") or {}
    license_present = any((inherited.get(key) if key in inherited else os.environ.get(key))
                          for key in ("CDS_LIC_FILE", "LM_LICENSE_FILE"))
    tools = detected["tool_details"]
    required_tools_found = all(tools[key]["status"] == "found" for key in ("spectre", "psf"))
    needs_attention = (
        not detected["python"]["supported"] or not required_tools_found
        or detected["cadence_env_details"]["status"] == "invalid"
        or any(item["status"] == "invalid" for item in tools.values())
        or any(item["status"] == "fail" for item in python_checks.values())
        or python_checks.get("analysis", {}).get("status") == "not_tested"
        or (target and not target["ok"])
    )
    return {
        "schema": "isf.environment.v1",
        "status": "attention" if needs_attention else "ready",
        "duration_seconds": round(time.monotonic() - started, 3),
        "platform": detected["platform"],
        "python": detected["python"],
        "python_checks": python_checks,
        "tools": tools,
        "cadence_env": detected["cadence_env_details"],
        "workspace": {**detected["workspace"], "target": target},
        "license_environment_present": license_present,
        "native_execution": "not_tested",
    }


def format_environment_report(report):
    platform = report["platform"]
    python = report["python"]
    sources = {"profile": "手动设置", "maestro-context": "当前 Maestro", "cadence-bridge": "环境脚本线索",
               "PATH": "PATH", "installation-hint": "安装目录线索", "companion-tool": "同安装目录",
               "project-hint": "工程目录线索", "ISF_CADENCE_ENV": "ISF_CADENCE_ENV"}
    status = {"pass": "通过", "fail": "失败", "not_tested": "未测试", "found": "已找到", "missing": "未找到", "invalid": "设置无效"}
    lines = [
        "环境就绪" if report["status"] == "ready" else "环境检查发现待处理项",
        f"{platform['system']} · {platform['machine']} · {platform['libc']} {platform['libc_version']}",
        f"Python {python['version']} · {python['executable']}",
    ]
    labels = {"numpy": "NumPy", "tcl": "Tcl", "tk": "Tk", "analysis": "分析解释器", "runtime": "Python 依赖"}
    for key, item in report["python_checks"].items():
        lines.append(f"{labels[key]}  {status[item['status']]} · {item['detail']}")
    lines.append("")
    for name, item in {**report["tools"], "Cadence 环境脚本": report["cadence_env"]}.items():
        path = item.get("path") or item.get("configured")
        parts = [name, status[item["status"]]]
        if item.get("source"):
            parts.append(sources[item["source"]])
        if path:
            parts.append(path)
        lines.append("  ·  ".join(parts))
    if report["workspace"]["target"]:
        lines.append("工作区  ·  " + report["workspace"]["target"]["detail"])
    lines.append("许可证变量  ·  " + ("已设置" if report["license_environment_present"] else "未检测到"))
    lines.extend(("", "此检查只定位工具和本机 Python 依赖。Cadence 启动、许可证签出、PSF 读取和仿真未测试。"))
    return "\n".join(lines)
