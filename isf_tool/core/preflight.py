"""Lightweight environment/profile detection and fail-closed preflight checks."""

from __future__ import unicode_literals

import copy
import fnmatch
import glob
import hashlib
import importlib.metadata
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover
    from collections import Mapping

from .discovery import discover_result_directories
from .errors import ProfileError, ValidationError
from .error_catalog import catalog_path
from .maestro_pair import inspect_maestro_pair
from .generated_flicker import replica_source
from .portable_setup import build_portable_setup, engine_content_sha256
from .waveform_exchange import inspect_waveform_exchange
from .physical_profile import (
    inspect_engine_contract,
    load_engine_config,
    resolve_device_subckt_file,
    resolve_physical_profile,
)
from .source_paths import VCO_NETLIST_RELATIVE, resolve_source_path
from ..resources import CONFIGS_ROOT, PACKAGE_ROOT, RUNTIME_ROOT, script_path


PROFILE_SCHEMA_VERSION = 2
TOOL_ROOT = os.fspath(RUNTIME_ROOT)
CORE_ROOT = os.path.dirname(os.path.abspath(__file__))
RUNNER_PATH = os.fspath(script_path("cadence_dual_tb_runner.py"))
ENGINE_PATH = os.fspath(script_path("dynamic_isf_closure_engine.py"))
EXPORTER_PATH = os.fspath(script_path("export_audited_bundle.py"))
CALCULATION_PATH = os.fspath(PACKAGE_ROOT / "calculation.py")
DUAL_TB_PATH = os.path.join(CORE_ROOT, "dual_tb.py")
PHYSICAL_PROFILE_PATH = os.path.join(CORE_ROOT, "physical_profile.py")
ORCHESTRATOR_PATH = os.path.join(CORE_ROOT, "orchestrator.py")
SOURCE_PATHS_PATH = os.path.join(CORE_ROOT, "source_paths.py")
CANONICAL_FILES = frozenset(
    ("vco_results.csv", "replica_results.csv", "metadata.json")
)
_PYTHON_PROBE_CACHE = {}


def _deep_merge(base, overlay):
    value = copy.deepcopy(base)
    for key, item in dict(overlay or {}).items():
        if isinstance(item, Mapping) and isinstance(value.get(key), Mapping):
            value[key] = _deep_merge(value[key], item)
        else:
            value[key] = copy.deepcopy(item)
    return value


def _path(value, base=None):
    if value in (None, ""):
        return None
    try:
        value = os.fspath(value)
    except (AttributeError, TypeError):
        return None
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, str):
        return None
    value = os.path.expanduser(value)
    if not os.path.isabs(value):
        value = os.path.join(base or TOOL_ROOT, value)
    return os.path.realpath(os.path.abspath(value))


def _sha256(path):
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _runtime_identity(engine_config_path, inline_engine=None):
    files = {}

    def add(label, path):
        path = _path(path)
        files[label] = {"path": path, "sha256": _sha256(path)}
        return path

    add("engine_config", engine_config_path)
    add("runner", RUNNER_PATH)
    add("engine", ENGINE_PATH)
    add("exporter", EXPORTER_PATH)
    add("calculation", CALCULATION_PATH)
    add("thermal_calculation", os.fspath(PACKAGE_ROOT / "thermal.py"))
    add("dual_tb", DUAL_TB_PATH)
    add("physical_profile_module", PHYSICAL_PROFILE_PATH)
    add("source_paths_module", SOURCE_PATHS_PATH)
    add("portable_setup_module", os.path.join(CORE_ROOT, "portable_setup.py"))
    add("signal_mapping_module", os.path.join(CORE_ROOT, "signal_mapping.py"))
    for module in ("discovery", "offsets", "offset_sweep", "result_presentation",
                   "maestro_pair", "psf_extract", "service", "preflight", "waveform_exchange",
                   "transfer_family", "noise_sources", "thermal_ports", "thermal_execution", "thermal_budget",
                   "generated_flicker", "limits"):
        add(module + "_module", os.path.join(CORE_ROOT, module + ".py"))
    add("orchestrator", ORCHESTRATOR_PATH)

    profile_id = None
    profile_sha256 = None
    profile_info = None
    engine_config = copy.deepcopy(inline_engine)
    if engine_config is not None:
        try:
            profile_info = resolve_physical_profile(engine_config, allow_legacy=False)
            profile_id = profile_info["profile"]["profile_id"]
            profile_sha256 = profile_info["sha256"]
        except ProfileError:
            pass
    if engine_config_path and os.path.isfile(engine_config_path):
        try:
            engine_config = load_engine_config(engine_config_path)
            profile_info = resolve_physical_profile(
                engine_config,
                config_path=engine_config_path,
                allow_legacy=True,
            )
            profile_id = profile_info["profile"]["profile_id"]
            profile_sha256 = profile_info["sha256"]
            profile_source = profile_info.get("source")
            if (
                isinstance(profile_source, str)
                and os.path.isfile(profile_source)
            ):
                add("physical_profile", profile_source)
        except Exception:
            engine_config = None
    if isinstance(engine_config, Mapping):
        base = os.path.dirname(engine_config_path or "")
        source_root = _path(engine_config.get("source_root"), base)
        model_path = add(
            "model", _path(engine_config.get("model_file"), base)
        )
        if profile_info is not None:
            configured_subckt = _path(
                engine_config.get("device_subckt_file"), base
            )
            device_subckt = resolve_device_subckt_file(
                model_path,
                profile_info["profile"],
                configured=configured_subckt,
            )
            if device_subckt and device_subckt != model_path:
                add("device_subckt_model", device_subckt)
        cadence_env = _path(engine_config.get("cadence_env"), base)
        if cadence_env:
            add("cadence_env", cadence_env)
        add("spectre", _path(engine_config.get("spectre"), base))
        add("psf", _path(engine_config.get("psf"), base))
        points = engine_config.get("points", [])
        if isinstance(points, list):
            for index, point in enumerate(points):
                if not isinstance(point, Mapping) or not point.get("source_dir"):
                    continue
                try:
                    source_netlist = (
                        resolve_source_path(
                            source_root,
                            point["source_dir"],
                            VCO_NETLIST_RELATIVE,
                        )
                        if source_root
                        else None
                    )
                except ValueError:
                    source_netlist = None
                add("source_netlist_%03d" % index, source_netlist)

    return {
        "engine_config_path": _path(engine_config_path),
        "engine_config_sha256": _sha256(_path(engine_config_path)),
        "runner_path": RUNNER_PATH,
        "runner_sha256": _sha256(RUNNER_PATH),
        "engine_path": ENGINE_PATH,
        "engine_sha256": _sha256(ENGINE_PATH),
        "physical_profile_id": profile_id,
        "physical_profile_sha256": profile_sha256,
        "files": files,
    }


def _first_path(*values):
    for value in values:
        candidate = _path(value)
        if candidate:
            return candidate
    return None


def _configured_roots(config):
    roots = []
    project_root = config.get("project_root", config.get("projectRoot"))
    if project_root:
        roots.append(project_root)
    discovery = config.get("discovery", {})
    if isinstance(discovery, Mapping):
        value = discovery.get("roots", discovery.get("root", []))
        if isinstance(value, (str, bytes)):
            value = [value]
        if isinstance(value, (list, tuple)):
            roots.extend(value)
    normalized = []
    for value in roots:
        candidate = _path(value)
        if candidate and candidate not in normalized:
            normalized.append(candidate)
    return normalized


def _executable_path(value):
    """Keep the invocation name: Cadence wrappers dispatch using argv[0]."""
    if not value:
        return None
    try:
        value = os.path.expanduser(os.fsdecode(os.fspath(value)))
    except (TypeError, AttributeError):
        return None
    return os.path.abspath(value if os.path.isabs(value) else os.path.join(TOOL_ROOT, value))


def _find_executable(name, configured=None):
    if configured:
        return _available_executable(configured)
    return _find_path_executable(name) or _find_install_executable(name)


def _find_path_executable(name):
    path = shutil.which(name)
    return _available_executable(os.path.abspath(path)) if path else None


def _available_executable(value):
    candidate = _executable_path(value)
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


def _find_install_executable(name):
    candidate = _available_executable(os.path.expanduser("~/bin/%s" % name))
    if candidate:
        return candidate
    for variable in ("CDS_INST_DIR", "CDSHOME", "CDS_ROOT", "SPECTRE_HOME", "MMSIM_HOME"):
        root = _path(os.environ.get(variable))
        if not root:
            continue
        for suffix in _cadence_bin_suffixes(name):
            candidate = _available_executable(os.path.join(root, *suffix))
            if candidate:
                return candidate
    return None


def _cadence_env_path(environment, config=None):
    explicit = (environment.get("cadence_env") if isinstance(environment, Mapping) else None) or os.environ.get("ISF_CADENCE_ENV")
    if explicit:
        path = _path(explicit)
        return path if path and os.path.isfile(path) and os.access(path, os.R_OK) else None
    candidates = [os.path.expanduser("~/cadence_bridge_env.sh")]
    roots = _configured_roots(config or {}) + [TOOL_ROOT]
    for root in roots:
        current = _path(root)
        # A portable release may be unpacked below several validation/project
        # directories while the site-local bridge stays near the user/project
        # root.  Keep the search bounded, but do not assume a shallow layout.
        for _index in range(32):
            if not current:
                break
            candidates.append(os.path.join(current, "cadence_bridge_env.sh"))
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    for value in candidates:
        candidate = _path(value) if value else None
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.R_OK):
            return candidate
    return None


def _cadence_bin_suffixes(name):
    return (
        ("bin", name),
        ("tools", "bin", name),
        ("tools.lnx86", "bin", name),
        ("tools.lnx86", "dfII", "bin", name),
        ("tools.lnx86", "dfII", "bin", "64bit", name),
        ("tools.lnx86", "spectre", "bin", "64bit", name),
    )


def _find_cadence_companion(name, discovered):
    """Find a Cadence helper beside an already discovered installation.

    Some site bridge scripts expose ``virtuoso`` and ``spectre`` but omit the
    standalone ``psf`` reader from PATH.  Probe a small fixed set of standard
    install-relative locations instead of requiring a second UI override.
    """

    checked = set()
    for anchor in dict(discovered or {}).values():
        current = os.path.dirname(_executable_path(anchor)) if anchor else None
        for _index in range(12):
            if not current or current in checked:
                break
            checked.add(current)
            for suffix in _cadence_bin_suffixes(name):
                candidate = os.path.join(current, *suffix)
                found = _available_executable(candidate)
                if found:
                    return found
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return None


def _probe_cadence_env(path):
    if not path:
        return {}
    command = (
        ". {env} >/dev/null 2>&1; "
        "for tool in spectre psf virtuoso ocean runSimulation; do "
        "value=$(command -v \"$tool\" 2>/dev/null || true); "
        "printf '%s=%s\\n' \"$tool\" \"$value\"; done"
    ).format(env=shlex.quote(path))
    try:
        completed = subprocess.run(
            ["/bin/bash", "-lc", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    output = {}
    for line in (completed.stdout or "").splitlines():
        name, separator, value = line.partition("=")
        candidate = _executable_path(value) if separator and value else None
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            output[name] = candidate

    # Some IC6 installations expose ``bin/psf`` through a wrapper symlink
    # whose target is absent after the installation tree is relocated.  The
    # actual 64-bit reader remains below tools.lnx86 and is safe to use
    # directly.  Derive that location from the detected Virtuoso executable
    # instead of baking a machine-specific Cadence root into configuration.
    if not output.get("psf") and output.get("virtuoso"):
        ic_root = os.path.dirname(os.path.dirname(output["virtuoso"]))
        for relative in (
            os.path.join("tools.lnx86", "dfII", "bin", "64bit", "psf"),
            os.path.join("tools", "dfII", "bin", "64bit", "psf"),
        ):
            candidate = os.path.join(ic_root, relative)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                output["psf"] = _executable_path(candidate)
                break
    return output


def _analysis_python_candidates(configured=None):
    explicit = configured or os.environ.get("ISF_PYTHON")
    if explicit:
        return [_executable_path(explicit)]
    values = [
        sys.executable,
        os.path.join(os.environ.get("VIRTUAL_ENV", ""), "bin", "python")
        if os.environ.get("VIRTUAL_ENV")
        else None,
        os.path.join(TOOL_ROOT, ".venv", "bin", "python"),
        os.path.join(os.path.dirname(TOOL_ROOT), ".venv", "bin", "python"),
        shutil.which("python3.11"),
        shutil.which("python3.12"),
        shutil.which("python3.13"),
    ]
    patterns = (
        os.path.expanduser("~/.venvs/*/bin/python3.11"),
        os.path.expanduser("~/.venvs/*/bin/python"),
        os.path.expanduser("~/venvs/*/bin/python3.11"),
        "/opt/miniforge3/envs/*/bin/python3.11",
        "/opt/conda/envs/*/bin/python3.11",
    )
    for pattern in patterns:
        values.extend(sorted(glob.glob(pattern))[:12])
    output = []
    for value in values:
        candidate = _available_executable(value)
        if (
            candidate
            and candidate not in output
        ):
            output.append(candidate)
    return output[:24]


def _probe_analysis_python(path):
    cached = _PYTHON_PROBE_CACHE.get(path)
    if cached is not None:
        return copy.deepcopy(cached)
    script = (
        "import json,sys,numpy;"
        "print(json.dumps({'version':list(sys.version_info[:3]),"
        "'numpy':numpy.__version__}))"
    )
    try:
        completed = subprocess.run(
            [path, "-B", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=3.0,
            check=False,
        )
        if completed.returncode != 0:
            return None
        value = json.loads(completed.stdout.strip().splitlines()[-1])
        version = value.get("version", [])
        if list(version[:2]) < [3, 11]:
            return None
        result = {
            "executable": path,
            "version": ".".join(str(item) for item in version),
            "numpy_version": str(value.get("numpy")),
        }
        _PYTHON_PROBE_CACHE[path] = copy.deepcopy(result)
        return result
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def _spectre_identity(path, allow_active_probe=False):
    """Reject unrelated executables without requiring a simulator license."""

    if not path:
        return {"ok": False, "detail": "not configured"}
    basename = os.path.basename(str(path)).lower()
    if "spectre" in basename:
        return {"ok": True, "detail": "executable name identifies Spectre"}
    if not allow_active_probe:
        return {
            "ok": False,
            "deferred": True,
            "detail": "version probe deferred until explicit Live authorization",
        }
    try:
        completed = subprocess.run(
            [path, "-W"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=3.0,
            check=False,
        )
        text = (completed.stdout or "") + "\n" + (completed.stderr or "")
        if "spectre" in text.lower():
            return {
                "ok": True,
                "detail": "version output identifies Spectre",
                "version_output": text.strip()[:500],
            }
        return {
            "ok": False,
            "detail": "executable identity is not Spectre",
            "returncode": completed.returncode,
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": "identity probe failed: %s" % exc}


def _pdk_from_model_file(path):
    path = _path(path)
    if not path:
        return None
    current = os.path.dirname(path)
    fallback = None
    while current and current != os.path.dirname(current):
        name = os.path.basename(current)
        lowered = name.lower()
        if "pdk" in lowered:
            return {
                "mode": "auto",
                "name": name,
                "root": current,
                "source": "engine-model-file",
            }
        if name.lower() == "models":
            fallback = os.path.dirname(current)
        current = os.path.dirname(current)
    if fallback:
        return {
            "mode": "auto",
            "name": os.path.basename(fallback),
            "root": fallback,
            "source": "engine-model-file",
        }
    return None


def _passive_analysis_python(path):
    """Describe the current interpreter without starting configured code."""

    candidate = _available_executable(path)
    current = _executable_path(sys.executable)
    if not candidate or os.path.dirname(candidate) != os.path.dirname(current) or os.path.realpath(candidate) != os.path.realpath(current):
        return None
    if sys.version_info < (3, 11):
        return None
    try:
        numpy_version = importlib.metadata.version("numpy")
    except importlib.metadata.PackageNotFoundError:
        return None
    return {
        "executable": candidate,
        "version": platform.python_version(),
        "numpy_version": str(numpy_version),
        "probe": "current-process-metadata",
    }


def _detect_analysis_python(configured=None, allow_active_probe=False):
    attempted = []
    source = "profile" if configured else "ISF_PYTHON" if os.environ.get("ISF_PYTHON") else "automatic"
    for candidate in _analysis_python_candidates(configured):
        attempted.append(candidate)
        if not _available_executable(candidate):
            continue
        result = _passive_analysis_python(candidate)
        if result is None and allow_active_probe:
            result = _probe_analysis_python(candidate)
        if result is not None:
            result["attempted"] = attempted
            result["probe_deferred"] = False
            result["source"] = source
            return result
    return {
        "executable": None,
        "version": None,
        "numpy_version": None,
        "attempted": attempted,
        "candidate": attempted[0] if attempted else None,
        "probe_deferred": bool(attempted and _available_executable(attempted[0]) and not allow_active_probe),
        "source": source,
    }


def _canonical_candidates(root, max_depth, max_results):
    output = []
    if not root or not os.path.isdir(root):
        return output
    for current, directories, files in os.walk(root):
        relative = os.path.relpath(current, root)
        depth = 0 if relative == "." else len(relative.split(os.sep))
        directories[:] = sorted(
            name for name in directories if not name.startswith(".")
        )
        if depth >= max_depth:
            directories[:] = []
        if CANONICAL_FILES.issubset(set(files)):
            output.append(
                {
                    "kind": "canonical",
                    "path": os.path.realpath(current),
                    "root": root,
                    "depth": depth,
                    "markers": ["canonical-two-tb"],
                }
            )
            if len(output) >= max_results:
                break
    return output


def _role_score(path, role):
    text = str(path or "").lower().replace("-", "_")
    tokens = [item for item in text.replace(os.sep, "_").split("_") if item]
    main_positive = ("vco", "osc", "oscillator", "main", "tank")
    replica_positive = ("replica", "noise", "flicker", "device", "mos")
    positive = main_positive if role == "vco" else replica_positive
    negative = replica_positive if role == "vco" else main_positive
    score = sum(3 for token in positive if token in tokens)
    score += sum(1 for token in positive if token in text)
    score -= sum(2 for token in negative if token in tokens)
    return score


def _rank_candidates(findings, role, pattern=None):
    ranked = []
    kind_weight = {"rdb": 30, "psf": 20, "canonical": 10, "maestro": 5}
    for item in findings:
        path = str(item.get("path", ""))
        if pattern:
            pattern_text = str(pattern)
            if not (
                fnmatch.fnmatch(path, pattern_text)
                or fnmatch.fnmatch(os.path.basename(path), pattern_text)
                or pattern_text.lower() in path.lower()
            ):
                continue
        value = copy.deepcopy(item)
        value["role_score"] = _role_score(item.get("path"), role)
        value["score"] = kind_weight.get(item.get("kind"), 0) + value["role_score"]
        ranked.append(value)
    ranked.sort(key=lambda item: (-item["score"], item.get("path", "")))
    return ranked


def _select_unique(ranked):
    if not ranked:
        return None, "missing"
    first = ranked[0]
    if len(ranked) > 1 and ranked[1]["score"] == first["score"]:
        return None, "ambiguous"
    if first.get("role_score", 0) <= 0 and first.get("kind") != "canonical":
        return None, "ambiguous"
    return first, "selected"


def _detect_engine_config(project_root):
    if not project_root or not os.path.isdir(project_root):
        return None, []
    patterns = (
        os.path.join(project_root, "configs", "*dynamic*isf*.json"),
        os.path.join(project_root, "*dynamic*isf*.json"),
        os.path.join(project_root, "configs", "*closure*.json"),
    )
    candidates = []
    for pattern in patterns:
        candidates.extend(glob.glob(pattern))
    candidates = sorted(set(os.path.realpath(path) for path in candidates if os.path.isfile(path)))
    return (candidates[0] if len(candidates) == 1 else None), candidates


def _detect_pair_engine_config(pair_report):
    """Select a bundled template on PDK plus generated-netlist topology."""

    if not isinstance(pair_report, Mapping):
        return None, []
    model_basenames = set()
    for role in ("vco", "flicker"):
        report = pair_report.get("roles", {}).get(role)
        test = report.get("selected_test", {}) if isinstance(report, Mapping) else {}
        for model in test.get("models", []):
            if model.get("path"):
                model_basenames.add(os.path.basename(str(model["path"])).lower())
    pdk = pair_report.get("pair", {}).get("pdk") or {}
    if not isinstance(pdk, Mapping):
        pdk = {}
    pdk_text = "%s %s" % (pdk.get("name", ""), pdk.get("root", ""))
    pdk_text = pdk_text.lower()
    vco_report = pair_report.get("roles", {}).get("vco") or {}
    selected_history = (
        vco_report.get("selected_history")
        if isinstance(vco_report, Mapping)
        else None
    )
    source_netlists = (
        selected_history.get("source_netlists", [])
        if isinstance(selected_history, Mapping)
        else []
    )
    candidates = []
    pattern = os.path.join(os.fspath(CONFIGS_ROOT), "engine_pair_*.json")
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, "r") as stream:
                value = json.load(stream)
        except (IOError, OSError, ValueError):
            continue
        match = value.get("pair_match", {})
        if not isinstance(match, Mapping):
            continue
        expected_models = set(
            str(item).lower() for item in match.get("model_basenames", [])
        )
        tokens = [str(item).lower() for item in match.get("pdk_tokens", [])]
        model_hits = len(model_basenames & expected_models)
        token_hits = sum(1 for token in tokens if token and token in pdk_text)
        score = model_hits * 10 + token_hits
        if score:
            topology_match = None
            topology_errors = []
            if source_netlists:
                topology_report = inspect_engine_contract(
                    value,
                    config_path=path,
                    source_from_maestro=True,
                    maestro_source_netlists=source_netlists,
                )
                topology_errors = [
                    item
                    for item in topology_report.get("errors", [])
                    if item.get("field") in (
                        "target_instance",
                        "hb",
                        "hbnoise",
                        "physical_profile",
                    )
                ]
                topology_match = not topology_errors
                if not topology_match:
                    continue
                score += 100
            candidates.append(
                {
                    "path": os.path.realpath(path),
                    "score": score,
                    "model_hits": model_hits,
                    "token_hits": token_hits,
                    "topology_match": topology_match,
                    "topology_errors": topology_errors,
                }
            )
    candidates.sort(key=lambda item: (-item["score"], item["path"]))
    if not candidates:
        return None, []
    if len(candidates) > 1 and candidates[0]["score"] == candidates[1]["score"]:
        return None, candidates
    return candidates[0]["path"], candidates


def _pair_model_candidate(pair_report, configured=None):
    model_sets = []
    for role in ("vco", "flicker"):
        report = pair_report.get("roles", {}).get(role) if pair_report else None
        test = report.get("selected_test", {}) if isinstance(report, Mapping) else {}
        model_sets.append(
            set(
                os.path.realpath(str(item["path"]))
                for item in test.get("models", [])
                if item.get("path") and os.path.isfile(str(item["path"]))
            )
        )
    common = sorted(model_sets[0] & model_sets[1]) if len(model_sets) == 2 else []
    basename = os.path.basename(str(configured or "")).lower()
    matching = [path for path in common if os.path.basename(path).lower() == basename]
    if len(matching) == 1:
        return matching[0]
    unique_files = sorted(set(common))
    return unique_files[0] if len(unique_files) == 1 else None


def _absolute_path_entries(value):
    if not isinstance(value, str):
        return []
    return [item for item in value.split(os.pathsep)[:128] if os.path.isabs(item)]


def _literal_bridge_path(script):
    """Read only a conventional literal PATH prefix; never interpret shell code."""
    if not script:
        return []
    try:
        with open(script, "rb") as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            return []
        lines = data.decode("utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    for line in lines:
        statement = line.strip().removeprefix("export ")
        if not statement.startswith("PATH="):
            continue
        try:
            words = shlex.split(statement[5:], comments=True)
        except ValueError:
            continue
        if len(words) != 1:
            continue
        entries = words[0].split(os.pathsep)
        if entries[-1] in ("$PATH", "${PATH}"):
            entries.pop()
        if all(os.path.isabs(item) and not any(char in item for char in "$`*?[]") for item in entries):
            return entries[:128]
    return []


def _search_tool_path(name, directories):
    for directory in directories:
        candidate = _available_executable(os.path.join(directory, name))
        if candidate:
            return candidate
    return None


def _resolve_tool(name, configured, native, bridge_tools, bridge_path, found):
    native_env = native.get("runtime_env") or {}
    native_path = _absolute_path_entries(native_env.get("PATH"))
    if configured:
        command = os.fsdecode(os.fspath(configured)) if isinstance(configured, (str, os.PathLike)) else ""
        path = _available_executable(command)
        if not path and command and os.sep not in command:
            path = (
                _search_tool_path(command, native_path)
                or bridge_tools.get(command)
                or _search_tool_path(command, bridge_path)
                or _find_executable(command)
            )
        return {"path": path, "source": "profile", "configured": command, "status": "found" if path else "invalid"}
    candidates = (
        ("maestro-context", lambda: _available_executable(native.get(name)) or _search_tool_path(name, native_path)),
        ("cadence-bridge", lambda: bridge_tools.get(name) or _search_tool_path(name, bridge_path)),
        ("PATH", lambda: _find_path_executable(name)),
        ("installation-hint", lambda: _find_install_executable(name)),
        ("companion-tool", lambda: _find_cadence_companion(name, found)),
    )
    for source, find in candidates:
        path = find()
        if path:
            return {"path": path, "source": source, "status": "found"}
    return {"path": None, "source": None, "status": "missing"}


def detect_environment(config=None, workspace=None, maestro_context=None):
    config = dict(config or {})
    profiles = config.get("profiles", {})
    environment = profiles.get("environment", {}) if isinstance(profiles, Mapping) else {}
    if not isinstance(environment, Mapping):
        environment = {}
    executables = environment.get("executables", {})
    if not isinstance(executables, Mapping):
        executables = {}
    execution = config.get("execution", {})
    if not isinstance(execution, Mapping):
        execution = {}
    active_probes = bool(
        not execution.get("dry_run", True)
        and execution.get("allow_execute", False)
    )
    analysis_python = _detect_analysis_python(
        environment.get("python"), allow_active_probe=active_probes
    )
    cadence_env = _cadence_env_path(environment, config)
    cadence_tools = _probe_cadence_env(cadence_env) if active_probes else {}
    bridge_path = _literal_bridge_path(cadence_env)
    native = maestro_context if isinstance(maestro_context, Mapping) else {}
    tool_details = {}
    found = {}
    for name in ("ocean", "virtuoso", "spectre", "psf", "runSimulation"):
        item = _resolve_tool(name, executables.get(name), native, cadence_tools, bridge_path, found)
        tool_details[name] = item
        if item["path"]:
            found[name] = item["path"]
    explicit_bridge = environment.get("cadence_env") or os.environ.get("ISF_CADENCE_ENV")
    bridge_source = (
        "profile" if environment.get("cadence_env") else
        "ISF_CADENCE_ENV" if explicit_bridge else
        "project-hint" if cadence_env else None
    )
    libc_name, libc_version = platform.libc_ver()
    return {
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "supported": sys.version_info >= (3, 11),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "libc": libc_name,
            "libc_version": libc_version,
        },
        "numpy": {
            "available": analysis_python.get("executable") is not None,
            "version": analysis_python.get("numpy_version"),
            "python": analysis_python.get("executable"),
        },
        "analysis_python": analysis_python,
        "active_probes": active_probes,
        "cadence_env_probe": (
            "executed" if active_probes and cadence_env else
            "deferred" if cadence_env else
            "not-configured"
        ),
        "executables": {name: item["path"] for name, item in tool_details.items()},
        "tool_details": tool_details,
        "cadence_env": cadence_env,
        "cadence_env_details": {
            "path": cadence_env or (_path(explicit_bridge) if explicit_bridge else None),
            "source": bridge_source,
            "status": "found" if cadence_env else "invalid" if explicit_bridge else "missing",
        },
        "workspace": {
            "path": _path(workspace) if workspace else None,
            "exists": bool(workspace and os.path.isdir(_path(workspace))),
            "writable": bool(
                workspace
                and os.path.isdir(_path(workspace))
                and os.access(_path(workspace), os.W_OK)
            ),
        },
        "tool_root": TOOL_ROOT,
        "runner": RUNNER_PATH,
    }


def detect_pdk(config=None):
    config = dict(config or {})
    profiles = config.get("profiles", {})
    value = profiles.get("pdk", "auto") if isinstance(profiles, Mapping) else "auto"
    if isinstance(value, Mapping):
        explicit_name = value.get("name")
        explicit_root = _path(value.get("root"))
        if explicit_name or explicit_root:
            return {
                "mode": "explicit",
                "name": explicit_name or os.path.basename(explicit_root),
                "root": explicit_root,
                "source": "profile",
            }
    elif isinstance(value, str) and value not in ("", "auto"):
        return {"mode": "explicit", "name": value, "root": None, "source": "profile"}

    for variable in ("PDK_ROOT", "PDK_HOME", "CDS_PDK_ROOT", "TECHDIR"):
        candidate = _path(os.environ.get(variable))
        if candidate and os.path.isdir(candidate):
            return {
                "mode": "auto",
                "name": os.path.basename(candidate.rstrip(os.sep)),
                "root": candidate,
                "source": variable,
            }

    roots = _configured_roots(config)
    keywords = ("pdk", "models", "model", "tech")
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            names = sorted(os.listdir(root))[:500]
        except OSError:
            continue
        for name in names:
            lowered = name.lower()
            candidate = os.path.join(root, name)
            if any(keyword in lowered for keyword in keywords) and os.path.isdir(candidate):
                return {
                    "mode": "auto",
                    "name": name,
                    "root": os.path.realpath(candidate),
                    "source": "project-root",
                }
    return {"mode": "auto", "name": None, "root": None, "source": None}


def detect_inputs(config=None):
    config = dict(config or {})
    roots = _configured_roots(config)
    discovery_config = config.get("discovery", {})
    if not isinstance(discovery_config, Mapping):
        discovery_config = {}
    max_depth = int(discovery_config.get("max_depth", 8))
    max_results = int(discovery_config.get("max_results", 500))
    findings = []
    warnings = []
    for root in roots:
        if not os.path.isdir(root):
            warnings.append("root is not a directory: %s" % root)
            continue
        result = discover_result_directories(
            {
                "roots": [root],
                "max_depth": max_depth,
                "max_results": max_results,
                "include_hidden": False,
                "follow_symlinks": False,
                "strict": False,
            }
        )
        findings.extend(result.get("findings", []))
        warnings.extend(result.get("warnings", []))
        findings.extend(_canonical_candidates(root, max_depth, max_results))
    unique = {}
    for item in findings:
        unique[(item.get("kind"), item.get("path"))] = item
    findings = sorted(unique.values(), key=lambda item: (item["kind"], item["path"]))
    vco_ranked = _rank_candidates(
        findings, "vco", discovery_config.get("main_pattern")
    )
    replica_ranked = _rank_candidates(
        findings, "replica", discovery_config.get("replica_pattern")
    )
    vco, vco_state = _select_unique(vco_ranked)
    replica, replica_state = _select_unique(replica_ranked)
    if vco and replica and vco.get("path") == replica.get("path"):
        if vco.get("kind") != "canonical":
            replica = None
            replica_state = "ambiguous"
    return {
        "roots": roots,
        "findings": findings,
        "warnings": warnings,
        "ranked": {
            "vco": vco_ranked[:12],
            "replica": replica_ranked[:12],
        },
        "selected": {"vco": vco, "replica": replica},
        "selection": {"vco": vco_state, "replica": replica_state},
    }


def _default_commands(python_executable=None, run_kind="replay"):
    base = [python_executable or sys.executable, RUNNER_PATH]
    common = ["--config", "{config_path}", "--run-root", "{run_root}"]
    export_mode = (
        "--formal-signoff" if str(run_kind).lower() == "signoff" else "--non-signoff"
    )
    return {
        "submitter": "runSimulation",
        "vco_command": base + ["--stage", "vco"] + common,
        "exchange_command": base + ["--stage", "exchange"] + common,
        "replica_command": base + ["--stage", "replica"] + common,
        "extract_command": base
        + ["--stage", "extract"]
        + common
        + [
            "--output",
            "{bundle_path}",
            "--job-id",
            "{job_id}",
            export_mode,
        ],
    }


def resolve_config(config=None, workspace=None, detected=None, maestro_context=None):
    from .ui_state import strip_retired_noise_objects
    raw = strip_retired_noise_objects(copy.deepcopy(dict(config or {})))
    source_mode_hint = str(
        raw.get("source_mode", raw.get("mode", "auto"))
    ).lower()
    pair_mode_hint = source_mode_hint in ("maestro_pair", "maestro-pair", "pair")
    schema = raw.get("schema_version", PROFILE_SCHEMA_VERSION)
    try:
        schema = int(schema)
    except (TypeError, ValueError):
        raise ProfileError("schema_version must be an integer")
    if schema not in (1, PROFILE_SCHEMA_VERSION):
        raise ProfileError(
            "unsupported profile schema_version: %s" % schema,
            details={"supported": [1, PROFILE_SCHEMA_VERSION]},
        )
    if schema == 1:
        legacy_input = raw.get("input", {})
        if isinstance(legacy_input, Mapping):
            manual = raw.setdefault("manual_import", {})
            if isinstance(manual, Mapping):
                manual = dict(manual)
                manual.setdefault("bundle_path", legacy_input.get("bundle_root", ""))
                manual.setdefault("vco_csv_path", legacy_input.get("vco_csv", ""))
                manual.setdefault(
                    "replica_csv_path", legacy_input.get("replica_csv", "")
                )
                raw["manual_import"] = manual
            testbenches = raw.setdefault("testbenches", {})
            if isinstance(testbenches, Mapping):
                testbenches = copy.deepcopy(dict(testbenches))
                if legacy_input.get("vco_rdb"):
                    testbenches.setdefault("vco", {})["path"] = legacy_input[
                        "vco_rdb"
                    ]
                if legacy_input.get("replica_rdb"):
                    testbenches.setdefault("replica", {})["path"] = legacy_input[
                        "replica_rdb"
                    ]
                raw["testbenches"] = testbenches
        if raw.get("offset_hz") is not None:
            raw.setdefault("analysis", {})["offset"] = {
                "value": float(raw["offset_hz"]) / 1000.0,
                "unit": "kHz",
                "hz": float(raw["offset_hz"]),
            }
        if raw.get("max_harmonic") is not None:
            raw.setdefault("analysis", {})["max_harmonic"] = raw[
                "max_harmonic"
            ]
    raw["schema_version"] = PROFILE_SCHEMA_VERSION
    raw["profiles"] = _deep_merge(
        {
            "auto_detect": True,
            "environment": {"mode": "auto", "executables": {}},
            "pdk": "auto",
            "design": {"mode": "auto"},
        },
        raw.get("profiles", {}),
    )
    raw["analysis"] = _deep_merge(
        {
            "offset": {"value": 10.0, "unit": "kHz", "hz": 10000.0},
            "max_harmonic": 3,
            "phase_samples": 512,
            "period_count": 8,
            "enforce_common_time_origin": True,
            "allow_manual_shift": False,
        },
        raw.get("analysis", {}),
    )
    from .offsets import normalize_offset_analysis
    raw["analysis"] = normalize_offset_analysis(raw["analysis"])
    raw["execution"] = _deep_merge(
        {
            "dry_run": True,
            "allow_execute": False,
            "background": True,
            "adapter_mode": "bundled",
            "resume_enabled": True,
        },
        raw.get("execution", {}),
    )
    for field in ("dry_run", "allow_execute", "resume_enabled"):
        if not isinstance(raw["execution"].get(field), bool):
            raise ValidationError("execution.%s must be a boolean" % field)
    raw["input_policy"] = _deep_merge(
        {
            "preferred": "maestro-pair" if pair_mode_hint else "rdb",
            "allowed": ["maestro-pair", "rdb", "psf", "canonical"],
            "allow_degraded": False,
        },
        raw.get("input_policy", {}),
    )
    if not isinstance(raw["input_policy"].get("allow_degraded"), bool):
        raise ValidationError("input_policy.allow_degraded must be a boolean")
    raw["discovery"] = _deep_merge(
        {
            "roots": [],
            "max_depth": 8,
            "max_results": 500,
            "include_hidden": False,
            "follow_symlinks": False,
            "strict": False,
        },
        raw.get("discovery", {}),
    )
    raw["testbenches"] = _deep_merge(
        {
            "vco": {"name": "vco", "role": "vco"},
            "replica": {"name": "replica", "role": "flicker"},
        },
        raw.get("testbenches", {}),
    )
    environment_detection = detect_environment(raw, workspace, maestro_context)
    analysis_python = environment_detection.get("analysis_python", {}).get(
        "executable"
    )
    requested_run_kind = str(raw.get("run_kind") or "").lower()
    dry_run = bool(raw["execution"].get("dry_run", True))
    if dry_run:
        requested_run_kind = "plan"
    elif not requested_run_kind:
        requested_run_kind = (
            "replay" if pair_mode_hint and replica_source(raw["testbenches"]) == "generated"
            else "signoff"
        )
    if requested_run_kind not in ("plan", "replay", "signoff"):
        raise ValidationError("run_kind must be plan, replay, or signoff")
    raw["run_kind"] = requested_run_kind
    raw["auto"] = _deep_merge(
        dict(
            _default_commands(analysis_python, requested_run_kind),
            timeout_s=7200,
            dry_run=True,
        ),
        raw.get("auto", {}),
    )
    defaults = _default_commands(analysis_python, requested_run_kind)
    for key in (
        "submitter",
        "vco_command",
        "exchange_command",
        "replica_command",
        "extract_command",
    ):
        if raw["auto"].get(key) in (None, "", []):
            raw["auto"][key] = copy.deepcopy(defaults[key])

    project_root = _first_path(raw.get("project_root"))
    roots = _configured_roots(raw)
    if project_root is None and len(roots) == 1:
        project_root = roots[0]
        raw["project_root"] = project_root
    if project_root and not raw["discovery"].get("roots"):
        raw["discovery"]["roots"] = [project_root]

    detected = detected or detect_inputs(raw)
    auto_detect = bool(raw["profiles"].get("auto_detect", True))
    source_mode = str(raw.get("source_mode", raw.get("mode", "auto"))).lower()
    pair_mode = source_mode in ("maestro_pair", "maestro-pair", "pair")
    if auto_detect and not pair_mode:
        selected = detected.get("selected", {})
        for role in ("vco", "replica"):
            item = selected.get(role)
            spec = raw["testbenches"].setdefault(role, {"name": role})
            if item and not spec.get("path"):
                spec["path"] = item.get("path")
                spec["source_kind"] = item.get("kind")
                spec["detected"] = True
        if not raw.get("config_path"):
            engine, candidates = _detect_engine_config(project_root)
            if engine:
                raw["config_path"] = engine
            raw.setdefault("detection", {})["engine_config_candidates"] = candidates

    if (
        not bool(raw["execution"].get("dry_run", True))
        and str(raw["execution"].get("adapter_mode", "bundled")).lower()
        == "bundled"
        and raw.get("config_path")
    ):
        # The bundled live runner consumes source_root/points from its physical
        # engine config and writes fresh PSF below run_root.  Historical
        # auto-discovered result paths must not be overlaid as if they were the
        # actual execution input.
        for role in ("vco", "replica"):
            spec = raw["testbenches"].get(role, {})
            if isinstance(spec, Mapping) and spec.get("detected"):
                spec = dict(spec)
                for key in ("path", "source_kind", "detected"):
                    spec.pop(key, None)
                raw["testbenches"][role] = spec

    raw["auto"]["dry_run"] = bool(raw["execution"].get("dry_run", True))
    resolved_engine_config = _path(raw.get("config_path"))
    raw["runtime_identity"] = _runtime_identity(resolved_engine_config)
    raw.setdefault("detection", {})["inputs"] = detected
    raw["detection"]["pdk"] = detect_pdk(raw)
    raw["detection"]["environment"] = environment_detection
    raw["detection"]["resolved_at_runtime"] = True
    return raw


def _target_directory_status(value, fallback=None):
    """Check whether a directory can be used without creating it."""

    target = _path(value or fallback)
    if not target:
        return {"ok": False, "path": None, "detail": "path is unresolved"}
    if os.path.exists(target):
        if not os.path.isdir(target):
            return {"ok": False, "path": target, "detail": "path exists but is not a directory"}
        writable = os.access(target, os.W_OK | os.X_OK)
        return {
            "ok": bool(writable),
            "path": target,
            "detail": "existing writable directory" if writable else "directory is not writable",
        }
    ancestor = target
    while ancestor and not os.path.exists(ancestor):
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            break
        ancestor = parent
    writable = bool(
        ancestor
        and os.path.isdir(ancestor)
        and os.access(ancestor, os.W_OK | os.X_OK)
    )
    return {
        "ok": writable,
        "path": target,
        "detail": (
            "parent can create directory: %s" % ancestor
            if writable
            else "nearest existing parent is not writable: %s" % (ancestor or "unresolved")
        ),
    }


def _check(check_id, category, label, status, detail, code=None, detected=None, field=None):
    return {
        "id": check_id,
        "category": category,
        "label": label,
        "status": status,
        "code": code,
        "detail": detail,
        "detected": detected,
        "field": field,
    }


def _chosen_input(config, detected):
    source_mode = str(config.get("source_mode", config.get("mode", "auto"))).lower()
    manual = config.get("manual_import", {})
    if not isinstance(manual, Mapping):
        manual = {}
    bundle = _path(manual.get("bundle_path", config.get("bundle_path")))
    if bundle and os.path.isdir(bundle):
        kind = "canonical" if CANONICAL_FILES.issubset(set(os.listdir(bundle))) else "bundle"
        return {"kind": kind, "path": bundle, "source": "manual"}
    vco_csv = _path(manual.get("vco_csv_path", manual.get("vco_csv")))
    replica_csv = _path(manual.get("replica_csv_path", manual.get("replica_csv")))
    if vco_csv and replica_csv and os.path.isfile(vco_csv) and os.path.isfile(replica_csv):
        return {"kind": "csv", "path": os.path.dirname(vco_csv), "source": "manual"}
    if source_mode == "manual":
        return None
    testbenches = config.get("testbenches", {})
    if isinstance(testbenches, Mapping):
        vco_spec = testbenches.get("vco", {})
        replica_spec = testbenches.get("replica", {})
        if isinstance(vco_spec, Mapping) and isinstance(replica_spec, Mapping):
            vco_path = _path(vco_spec.get("path"))
            replica_path = _path(replica_spec.get("path"))
            if (
                not vco_spec.get("detected")
                and not replica_spec.get("detected")
                and
                vco_path
                and replica_path
                and os.path.exists(vco_path)
                and os.path.exists(replica_path)
            ):
                kinds = []
                for path, spec in ((vco_path, vco_spec), (replica_path, replica_spec)):
                    kind = spec.get("source_kind")
                    if not kind:
                        if os.path.isfile(path) and path.lower().endswith(".rdb"):
                            kind = "rdb"
                        elif os.path.isdir(path) and CANONICAL_FILES.issubset(
                            set(os.listdir(path))
                        ):
                            kind = "canonical"
                        else:
                            kind = "psf"
                    kinds.append(kind)
                kind = "rdb" if "rdb" in kinds else (
                    "canonical" if kinds == ["canonical", "canonical"] else "psf"
                )
                return {
                    "kind": kind,
                    "path": vco_path,
                    "replica_path": replica_path,
                    "source": "explicit-testbenches",
                }
    selected = detected.get("selected", {})
    vco = selected.get("vco")
    replica = selected.get("replica")
    if vco and replica:
        kind = "canonical" if vco.get("kind") == replica.get("kind") == "canonical" else (
            "rdb" if "rdb" in (vco.get("kind"), replica.get("kind")) else "psf"
        )
        return {
            "kind": kind,
            "path": vco.get("path"),
            "replica_path": replica.get("path"),
            "source": "auto",
        }
    return None


def preflight(config=None, workspace=None, maestro_context=None):
    raw = copy.deepcopy(dict(config or {}))
    requested_source_mode = str(
        raw.get("source_mode", raw.get("mode", "auto"))
    ).lower()
    pair_mode = requested_source_mode in ("maestro_pair", "maestro-pair", "pair")
    if pair_mode:
        # The primary workflow inspects only the two chosen Maestro setups.
        # Generic RDB/PSF scanning remains available to replay mode but must
        # not compete with or silently replace an explicit TB binding.
        detected_inputs = {
            "roots": _configured_roots(raw),
            "findings": [],
            "warnings": [],
            "ranked": {"vco": [], "replica": []},
            "selected": {"vco": None, "replica": None},
            "selection": {"vco": "pair-mode", "replica": "pair-mode"},
            "mode": "maestro-pair",
        }
    else:
        detected_inputs = detect_inputs(raw)
    resolved = resolve_config(raw, workspace=workspace, detected=detected_inputs, maestro_context=maestro_context)
    environment = resolved["detection"]["environment"]
    execution = resolved.get("execution", {})
    live = not bool(execution.get("dry_run", True))
    adapter_mode = str(execution.get("adapter_mode", "bundled")).lower()
    pdk = resolved["detection"]["pdk"]
    pair_report = None
    portable_report = None
    generated_replica = pair_mode and replica_source(resolved.get("testbenches")) == "generated"
    pair_roles = [("vco", "vco", "VCO")]
    if not generated_replica:
        pair_roles.append(("flicker", "replica", "Flicker"))
    if pair_mode:
        analysis = resolved.get("analysis", {})
        offset = analysis.get("offset", {}) if isinstance(analysis, Mapping) else {}
        offset_hz = (
            offset.get("hz", 10000.0)
            if isinstance(offset, Mapping)
            else 10000.0
        )
        discovery = resolved.get("discovery", {})
        pair_report = inspect_maestro_pair(
            {
                "testbenches": resolved.get("testbenches", {}),
                "roots": detected_inputs.get("roots", []),
                "offset_hz": offset_hz,
                "max_depth": (
                    discovery.get("max_depth", 6)
                    if isinstance(discovery, Mapping)
                    else 6
                ),
                "max_results": (
                    discovery.get("max_results", 100)
                    if isinstance(discovery, Mapping)
                    else 100
                ),
                "include_histories": True,
            }
        )
        # Seal the exact histories and logical mappings selected during this
        # Preflight into the resolved config.  Do this only after the user has
        # confirmed the structural TB fingerprint; otherwise Preflight must not
        # silently turn discovery into acceptance.
        resolved_testbenches = resolved.setdefault("testbenches", {})
        for pair_role, config_role, _ in pair_roles:
            binding = pair_report.get("bindings", {}).get(pair_role, {})
            spec = resolved_testbenches.get(config_role, {})
            if not isinstance(spec, Mapping):
                continue
            if (
                spec.get("binding_fingerprint")
                and spec.get("binding_fingerprint")
                == binding.get("binding_fingerprint")
            ):
                sealed = copy.deepcopy(dict(spec))
                sealed["mapping"] = copy.deepcopy(binding.get("mapping", {}))
                if binding.get("history"):
                    sealed["history"] = binding.get("history")
                    sealed["history_fingerprint"] = binding.get(
                        "history_fingerprint"
                    )
                resolved_testbenches[config_role] = sealed
        pair_pdk = pair_report.get("pair", {}).get("pdk")
        if isinstance(pair_pdk, Mapping) and pair_pdk.get("root"):
            pdk = dict(pair_pdk)
            resolved["detection"]["pdk"] = copy.deepcopy(pdk)
        if not resolved.get("config_path"):
            pair_engine, pair_engine_candidates = _detect_pair_engine_config(
                pair_report
            )
            resolved.setdefault("detection", {})[
                "pair_engine_config_candidates"
            ] = pair_engine_candidates
            # File templates remain expert options. Normal use derives an
            # inspectable template from this pair, never from a matching name.
            portable_report = build_portable_setup(pair_report, resolved, environment)
            resolved["detection"]["portable_setup"] = portable_report
            pair_report["portable_setup"] = portable_report
    config_path = _path(resolved.get("config_path"))
    engine_config = None
    engine_load_error = None
    if portable_report is not None and portable_report.get("ok"):
        engine_config = copy.deepcopy(portable_report["engine"])
        resolved["detected_engine"] = copy.deepcopy(engine_config)
        resolved["runtime_identity"] = _runtime_identity(None, engine_config)
        resolved["runtime_identity"]["detected_engine_sha256"] = engine_content_sha256(engine_config)
    else:
        resolved.pop("detected_engine", None)
    if config_path and os.path.isfile(config_path):
        try:
            engine_config = load_engine_config(config_path)
        except ProfileError as exc:
            engine_load_error = exc
    if pair_mode and engine_config is not None:
        engine_config = copy.deepcopy(dict(engine_config))
        current_model = _pair_model_candidate(
            pair_report, engine_config.get("model_file")
        )
        if current_model and portable_report is None:
            engine_config["model_file"] = current_model
        for executable in ("spectre", "psf"):
            detected_executable = environment.get("executables", {}).get(executable)
            configured_executable = _path(
                engine_config.get(executable), os.path.dirname(config_path or "")
            )
            if (
                portable_report is None
                and (not configured_executable or not os.path.isfile(configured_executable))
                and detected_executable
            ):
                engine_config[executable] = detected_executable
        cadence_env = environment.get("cadence_env")
        configured_cadence_env = _path(
            engine_config.get("cadence_env"), os.path.dirname(config_path or "")
        )
        if (
            portable_report is None and cadence_env
            and (
                not configured_cadence_env
                or not os.path.isfile(configured_cadence_env)
            )
        ):
            engine_config["cadence_env"] = cadence_env
        analysis = resolved.get("analysis", {})
        offset = analysis.get("offset", {}) if isinstance(analysis, Mapping) else {}
        if isinstance(offset, Mapping) and offset.get("hz") is not None:
            engine_config["offset_hz"] = float(offset["hz"])
        if isinstance(analysis, Mapping) and analysis.get("max_harmonic") is not None:
            engine_config["max_harmonic"] = int(analysis["max_harmonic"])
        resolved.setdefault("detection", {})["effective_engine_config"] = {
            "template": config_path,
            "model_file": engine_config.get("model_file"),
            "spectre": engine_config.get("spectre"),
            "psf": engine_config.get("psf"),
            "cadence_env": engine_config.get("cadence_env"),
            "source": "maestro-history-materialized-at-run-time",
        }
        # The preflight identity is anchored to the portable engine template,
        # while execution creates a second, private materialized config.  Seal
        # the effective Cadence paths and both selected Maestro histories now;
        # the exporter/orchestrator later compare the template separately from
        # the private config instead of treating them as the same file.
        runtime_identity = resolved.setdefault(
            "runtime_identity", _runtime_identity(config_path)
        )
        runtime_files = runtime_identity.setdefault("files", {})

        def seal_runtime_file(label, path):
            normalized = _path(path)
            runtime_files[label] = {
                "path": normalized,
                "sha256": _sha256(normalized),
            }

        seal_runtime_file("model", engine_config.get("model_file"))
        seal_runtime_file("spectre", engine_config.get("spectre"))
        seal_runtime_file("psf", engine_config.get("psf"))
        if engine_config.get("cadence_env"):
            seal_runtime_file("cadence_env", engine_config.get("cadence_env"))
        try:
            effective_profile = resolve_physical_profile(
                engine_config,
                config_path=config_path,
                allow_legacy=True,
            )
            effective_subckt = resolve_device_subckt_file(
                engine_config.get("model_file"),
                effective_profile["profile"],
                configured=engine_config.get("device_subckt_file"),
            )
            if effective_subckt and _path(effective_subckt) != _path(
                engine_config.get("model_file")
            ):
                seal_runtime_file("device_subckt_model", effective_subckt)
        except Exception:
            pass
        compact_bindings = {}
        for role, _, _ in pair_roles:
            role_report = pair_report.get("roles", {}).get(role) or {}
            binding = pair_report.get("bindings", {}).get(role) or {}
            compact_bindings[role] = {
                key: copy.deepcopy(binding.get(key))
                for key in (
                    "maestro_dir",
                    "test",
                    "binding_fingerprint",
                    "history",
                    "history_fingerprint",
                )
            }
            if role_report.get("maestro_sdb"):
                seal_runtime_file(
                    "maestro_%s_sdb" % role, role_report.get("maestro_sdb")
                )
            if role_report.get("active_state"):
                seal_runtime_file(
                    "maestro_%s_state" % role,
                    role_report.get("active_state"),
                )
            history = role_report.get("selected_history") or {}
            if history.get("path"):
                seal_runtime_file(
                    "maestro_%s_history" % role, history.get("path")
                )
            seen_sources = set()
            source_index = 0
            for source in history.get("source_netlists", []):
                source_path = _path(source.get("path"))
                if not source_path or source_path in seen_sources:
                    continue
                seen_sources.add(source_path)
                seal_runtime_file(
                    "maestro_%s_source_%03d" % (role, source_index),
                    source_path,
                )
                source_index += 1
        runtime_identity["maestro_pair"] = {
            "schema_version": 1,
            "engine_template_path": config_path,
            "engine_template_sha256": _sha256(config_path),
            "bindings": compact_bindings,
        }
        if generated_replica:
            runtime_identity["maestro_pair"]["replica_source"] = "generated"
    if (
        engine_config is not None
        and not (pdk.get("root") and os.path.isdir(pdk.get("root")))
    ):
        inferred_pdk = _pdk_from_model_file(engine_config.get("model_file"))
        if inferred_pdk is not None:
            pdk = inferred_pdk
            resolved["detection"]["pdk"] = copy.deepcopy(pdk)
    if portable_report is not None and engine_config is not None:
        # Freeze exactly the final values being checked, not a pre-resolution
        # copy. Explicit manual overrides are never replaced by fallbacks.
        resolved["detected_engine"] = copy.deepcopy(engine_config)
        resolved["runtime_identity"]["detected_engine_sha256"] = engine_content_sha256(engine_config)
    engine_report = None
    if engine_config is not None:
        maestro_source_netlists = []
        maestro_vco_mapping = {}
        maestro_flicker_netlists = []
        maestro_flicker_mapping = {}
        if pair_report:
            vco_report = pair_report.get("roles", {}).get("vco") or {}
            history = (
                vco_report.get("selected_history")
                if isinstance(vco_report, Mapping)
                else None
            )
            if isinstance(history, Mapping):
                maestro_source_netlists = history.get("source_netlists", [])
            if isinstance(vco_report, Mapping):
                maestro_vco_mapping = vco_report.get("mapping", {})
            flicker_report = pair_report.get("roles", {}).get("flicker") or {}
            flicker_history = (
                flicker_report.get("selected_history")
                if isinstance(flicker_report, Mapping)
                else None
            )
            if isinstance(flicker_history, Mapping):
                maestro_flicker_netlists = flicker_history.get(
                    "source_netlists", []
                )
            if isinstance(flicker_report, Mapping):
                maestro_flicker_mapping = flicker_report.get("mapping", {})
        engine_report = inspect_engine_contract(
            engine_config,
            config_path=config_path,
            app_config=resolved,
            pdk=pdk,
            require_explicit=live and adapter_mode == "bundled",
            check_paths=live and adapter_mode == "bundled",
            source_from_maestro=pair_mode,
            maestro_source_netlists=maestro_source_netlists,
            maestro_vco_mapping=maestro_vco_mapping,
            maestro_flicker_netlists=maestro_flicker_netlists,
            maestro_flicker_mapping=maestro_flicker_mapping,
        )
        engine_spectre = engine_report.get("resolved_paths", {}).get("spectre")
        if (
            engine_spectre
            and os.path.isfile(engine_spectre)
            and os.access(engine_spectre, os.X_OK)
            and not environment["executables"].get("spectre")
            and environment["tool_details"]["spectre"]["status"] != "invalid"
        ):
            environment["executables"]["spectre"] = engine_spectre
    checks = []

    python = environment["python"]
    checks.append(
        _check(
            "python",
            "environment",
            "Python 3.11+",
            "pass" if python["supported"] else "fail",
            "%s · %s" % (python["version"], python["executable"]),
            None if python["supported"] else "ISF-E1101",
        )
    )
    numpy_available = environment["numpy"]["available"]
    numpy_deferred = bool(
        environment.get("analysis_python", {}).get("probe_deferred")
    )
    checks.append(
        _check(
            "numpy",
            "environment",
            "NumPy automatic extraction capability",
            (
                "pass"
                if numpy_available
                else ("warn" if numpy_deferred else ("fail" if live else "warn"))
            ),
            (
                "%s · NumPy %s"
                % (
                    environment["numpy"].get("python"),
                    environment["numpy"].get("version"),
                )
                if numpy_available
                else (
                    "configured interpreter probe deferred until explicit Live authorization"
                    if numpy_deferred
                    else "manual import remains available"
                )
            ),
            (
                None
                if numpy_available
                else ("ISF-W1101" if numpy_deferred or not live else "ISF-E1101")
            ),
            field=None if numpy_deferred else "profiles.environment.python",
        )
    )
    workspace_ok = environment["workspace"]["writable"] if workspace else True
    checks.append(
        _check(
            "workspace",
            "environment",
            "private workspace",
            "pass" if workspace_ok else "fail",
            environment["workspace"]["path"] or "in-memory service",
            None if workspace_ok else "ISF-E1003",
        )
    )
    automatic_root = os.path.join(_path(workspace), "jobs") if workspace else None
    for check_id, label, configured, fallback, field in (
        (
            "run_target",
            "Run directory can be created",
            resolved.get("run_root"),
            automatic_root,
            "run_root",
        ),
        (
            "bundle_target",
            "Result Bundle directory can be created",
            resolved.get("bundle_path"),
            automatic_root,
            "bundle_path",
        ),
    ):
        target_status = _target_directory_status(configured, fallback)
        checks.append(
            _check(
                check_id,
                "environment",
                label,
                "pass" if target_status["ok"] else "fail",
                "%s · %s"
                % (target_status.get("path") or "automatic", target_status["detail"]),
                None if target_status["ok"] else "ISF-E1003",
                detected=target_status,
                field=field,
            )
        )
    checks.append(
        _check(
            "error_catalog",
            "environment",
            "Local error catalog",
            "pass" if os.path.isfile(catalog_path()) else "fail",
            catalog_path(),
            None if os.path.isfile(catalog_path()) else "ISF-E1003",
        )
    )

    if pair_mode:
        pair_issues = pair_report.get("issues", []) if pair_report else []
        pair_role_failures = []
        for role, config_role, label in pair_roles:
            role_report = (
                pair_report.get("roles", {}).get(role) if pair_report else None
            )
            role_errors = [
                issue
                for issue in pair_issues
                if issue.get("severity") == "error"
                and str(issue.get("field") or "").startswith(
                    "testbenches.%s" % config_role
                )
            ]
            role_ok = bool(role_report and role_report.get("selected_test")) and not role_errors
            if not role_ok:
                pair_role_failures.append("maestro_%s" % role)
            detail = "not selected"
            if role_report and role_report.get("selected_test"):
                selected_test = role_report["selected_test"]
                design = selected_test.get("design", {})
                detail = "%s/%s/%s · test=%s" % (
                    design.get("lib") or "?",
                    design.get("cell") or "?",
                    design.get("view") or "?",
                    selected_test.get("name") or "?",
                )
                if role_errors:
                    detail += " · " + role_errors[0].get("detail", "contract error")
            checks.append(
                _check(
                    "maestro_%s" % role,
                    "input",
                    label + (" Maestro" if role == "vco" else "-noise Maestro"),
                    "pass" if role_ok else "fail",
                    detail,
                    None if role_ok else "ISF-E1201",
                    detected=role_report,
                    field=(role_errors[0].get("field") if role_errors else None)
                    or "testbenches.%s.maestro_dir" % config_role,
                )
            )

        changed = [
            issue for issue in pair_issues if issue.get("code") == "ISF-E1204"
        ]
        configured_pair = resolved.get("testbenches", {})
        missing_binding = [
            role for role, config_role, _ in pair_roles
            for spec in [configured_pair.get(config_role)]
            if not isinstance(spec, Mapping) or not spec.get("binding_fingerprint")
        ]
        binding_guard_ok = not changed and not missing_binding
        checks.append(
            _check(
                "maestro_change_guard",
                "reliability",
                "Maestro Variable/output change protection",
                "pass" if binding_guard_ok else "fail",
                (
                    changed[0].get("detail")
                    if changed
                    else (
                        "binding not confirmed: %s" % ", ".join(missing_binding)
                        if missing_binding
                        else "binding fingerprint unchanged"
                    )
                ),
                None if binding_guard_ok else "ISF-E1204",
                detected={"changed": changed, "missing": missing_binding},
                field=(
                    changed[0].get("field")
                    if changed
                    else ("testbenches" if missing_binding else None)
                ),
            )
        )

        pair_errors = [
            issue for issue in pair_issues if issue.get("severity") == "error"
        ]
        pair_warnings = [
            issue
            for issue in pair_issues
            if issue.get("severity") in ("warning", "info")
        ]
        pair_ok = bool(pair_report and pair_report.get("ok"))
        pair_root_detail = next(
            (item.get("detail") or item.get("message") for item in pair_errors
             if item.get("detail") or item.get("message")),
            "First correct the above TB selection or name mapping",
        )
        pair_blocked = not pair_ok and bool(pair_role_failures)
        pair_status = (
            ("warn" if pair_blocked else "fail") if not pair_ok
            else ("warn" if pair_warnings else "pass")
        )
        checks.append(
            _check(
                "maestro_pair_contract",
                "input",
                "VCO and generated noise-replica inputs" if generated_replica else "dual Maestro Physical input contract",
                pair_status,
                (
                    ("Blocked by upstream binding: " if pair_blocked else "") + pair_root_detail
                    if not pair_ok else
                    "%s · %s"
                    % (
                        (
                            pair_report.get("pair", {}).get("pdk") or {}
                        ).get("name", "PDK unresolved"),
                        (
                            "%d non-blocking notes (expand to inspect)" % len(pair_warnings)
                            if pair_warnings
                            else "Mappings complete"
                        ),
                    )
                    if pair_report
                    else "inspection unavailable"
                ),
                (
                    "ISF-W1201" if pair_blocked else
                    pair_errors[0].get("code")
                    if pair_errors
                    else (
                        pair_warnings[0].get("code") if pair_warnings else None
                    )
                ),
                detected={"issues": pair_issues, "bindings": pair_report.get("bindings", {}) if pair_report else {},
                          "blocked_by": pair_role_failures if pair_blocked else []},
                field=(pair_errors[0].get("field") if pair_errors else None),
            )
        )

        for pair_role, config_role, label in pair_roles:
            role_report = (
                pair_report.get("roles", {}).get(pair_role)
                if pair_report
                else None
            )
            history = (
                role_report.get("selected_history")
                if isinstance(role_report, Mapping)
                else None
            )
            history_ok = bool(history)
            checks.append(
                _check(
                    "maestro_%s_execution_history" % pair_role,
                    "execution",
                    "%s Generated netlist/PSF history" % label,
                    "pass" if history_ok else ("fail" if live else "warn"),
                    (
                        "%s · %d complete run(s)"
                        % (
                            history.get("name"),
                            history.get("complete_runs", 0),
                        )
                        if history_ok
                        else "rerun selected %s Maestro on this host before Live"
                        % label
                    ),
                    None
                    if history_ok
                    else ("ISF-E1205" if live else "ISF-W1203"),
                    detected=history,
                    field="testbenches.%s.history" % config_role,
                )
            )

        if generated_replica and config_path:
            checks.append(_check(
                "generated_replica_expert_template", "input", "Generated replica configuration", "fail",
                "Generated replica uses VCO generated from operating point. Clear the expert configuration path, or switch to existing Flicker Maestro. ",
                "ISF-E1101", field="config_path",
            ))
        if generated_replica and resolved["run_kind"] == "signoff" and not (
            (pair_report.get("roles", {}).get("vco") or {}).get("mapping", {}).get("peer_instance")
        ):
            checks.append(_check(
                "generated_replica_signoff_peer", "input", "Signoff two-device reference", "fail",
                "Current VCO No confirmed second measured flicker Device. Select a peer device, or use engineering analysis. ",
                "ISF-E1202", field="testbenches.vco.mapping_overrides.peer_instance",
            ))

    pdk_root_ok = bool(pdk.get("root") and os.path.isdir(pdk.get("root")))
    pdk_hint = bool(pdk.get("name") or pdk.get("root"))
    pdk_status = "pass" if pdk_root_ok else ("fail" if live else ("warn" if not pdk_hint else "warn"))
    checks.append(
        _check(
            "pdk",
            "profile",
            "PDK Profile",
            pdk_status,
            (
                "%s · %s · %s"
                % (pdk.get("name"), pdk.get("source"), pdk.get("root") or "root unresolved")
                if pdk_hint
                else "not identified"
            ),
            (
                None
                if pdk_root_ok
                else ("ISF-E1101" if live else "ISF-W1102")
            ),
            detected=pdk,
            field="profiles.pdk",
        )
    )

    source_mode = str(
        resolved.get("source_mode", resolved.get("mode", "auto"))
    ).lower()
    roots = detected_inputs.get("roots", [])
    pair_bindings = pair_report.get("bindings", {}) if pair_report else {}
    pair_paths_ok = bool(pair_bindings.get("vco") and (generated_replica or pair_bindings.get("flicker")))
    root_ok = (
        pair_paths_ok
        if pair_mode
        else source_mode == "manual" or any(os.path.isdir(root) for root in roots)
    )
    checks.append(
        _check(
            "project_root",
            "input",
            ("VCO History boundary" if generated_replica else "dual TB Search boundary") if pair_mode else "Project/Result root",
            "pass" if root_ok else "fail",
            (
                ("explicit VCO Maestro" if generated_replica else "explicit Maestro pair")
                if pair_mode and pair_paths_ok
                else (", ".join(roots) if roots else "not configured")
            ),
            None if root_ok else ("ISF-E1201" if pair_mode else "ISF-E1102"),
            field="testbenches" if pair_mode else "project_root",
        )
    )

    if pair_mode and pair_paths_ok:
        chosen = {
            "kind": "maestro-pair",
            "source": "explicit-testbenches",
            "path": pair_bindings["vco"].get("maestro_dir"),
            "replica_path": pair_bindings.get("flicker", {}).get("maestro_dir"),
            "vco_test": pair_bindings["vco"].get("test"),
            "flicker_test": pair_bindings.get("flicker", {}).get("test"),
        }
    else:
        chosen = _chosen_input(resolved, detected_inputs)
    if (
        live
        and adapter_mode == "bundled"
        and engine_config is not None
        and not pair_mode
    ):
        source_root = (
            engine_report.get("resolved_paths", {}).get("source_root")
            if engine_report
            else _path(engine_config.get("source_root"), os.path.dirname(config_path))
        )
        chosen = {
            "kind": "engine-source",
            "path": source_root,
            "source": "physical-profile",
            "points": len(engine_config.get("points", []))
            if isinstance(engine_config.get("points"), list)
            else 0,
        }
    selection = detected_inputs.get("selection", {})
    ambiguous = not pair_mode and source_mode != "manual" and (
        not chosen or chosen.get("source") == "auto"
    ) and (
        selection.get("vco") == "ambiguous" or selection.get("replica") == "ambiguous"
    )
    if ambiguous:
        checks.append(
            _check(
                "input_resolution",
                "input",
                "Automatic input workflow detection",
                "fail",
                "multiple equally ranked VCO/replica candidates",
                "ISF-E1103",
                detected=detected_inputs.get("ranked"),
                field="testbenches",
            )
        )
    elif chosen is None:
        checks.append(
            _check(
                "input_resolution",
                "input",
                "Automatic input workflow detection",
                "fail",
                "no complete input pair or manual bundle",
                "ISF-E1102",
                detected=detected_inputs.get("findings"),
                field="manual_import.bundle_path",
            )
        )
    else:
        policy = resolved.get("input_policy", {})
        allowed = policy.get("allowed", [])
        preferred = policy.get("preferred", "rdb")
        engine_source = chosen["kind"] in ("engine-source", "maestro-pair")
        degraded = (
            False
            if engine_source
            else chosen["kind"] not in allowed or chosen["kind"] != preferred
        )
        allow_degraded = bool(policy.get("allow_degraded", False))
        if (not engine_source and chosen["kind"] not in allowed) or (
            degraded and live and not allow_degraded
        ):
            status = "fail"
            code = "ISF-E1104"
        elif degraded:
            status = "warn"
            code = "ISF-W1103"
        else:
            status = "pass"
            code = None
        checks.append(
            _check(
                "input_resolution",
                "input",
                "Automatic input workflow detection",
                status,
                "%s · %s" % (chosen["kind"], chosen.get("path")),
                code,
                detected=chosen,
            )
        )

    spectre = environment["executables"].get("spectre")
    spectre_setting = environment["tool_details"]["spectre"]
    spectre_identity = (
        _spectre_identity(
            spectre,
            allow_active_probe=bool(environment.get("active_probes")),
        )
        if spectre
        else {
            "ok": False,
            "deferred": bool(
                spectre_setting["status"] != "invalid" and environment.get("cadence_env")
                and not environment.get("active_probes")
            ),
            "detail": (
                "Configured Spectre is unavailable: %s" % spectre_setting.get("configured")
                if spectre_setting["status"] == "invalid"
                else "Cadence environment probe deferred until explicit Live authorization"
                if environment.get("cadence_env")
                and not environment.get("active_probes")
                else "not found"
            ),
        }
    )
    spectre_ok = bool(spectre and spectre_identity.get("ok"))
    spectre_deferred = bool(spectre_identity.get("deferred"))
    checks.append(
        _check(
            "spectre",
            "execution",
            "Spectre Executable",
            "pass" if spectre_ok else (
                "warn" if spectre_deferred else ("fail" if live else "warn")
            ),
            (
                "%s · %s" % (spectre, spectre_identity.get("detail"))
                if spectre
                else spectre_identity.get("detail", "not found in configured path/PATH")
            ),
            (
                None
                if spectre_ok
                else ("ISF-W1103" if spectre_deferred or not live else "ISF-E4003")
            ),
            field=(
                None
                if spectre_deferred
                else "profiles.environment.executables.spectre"
            ),
        )
    )
    for tool in ("psf", "cadence_env"):
        item = environment["cadence_env_details"] if tool == "cadence_env" else environment["tool_details"][tool]
        if item["status"] == "invalid":
            field = "profiles.environment." + ("cadence_env" if tool == "cadence_env" else "executables.psf")
            checks.append(_check(tool, "environment", tool, "fail" if live else "warn",
                "Configured path is unavailable: %s" % (item.get("configured") or item.get("path")),
                "ISF-E1101" if live else "ISF-W1103", field=field))
    config_ok = bool(
        ((config_path and os.path.isfile(config_path)) or (portable_report and portable_report.get("ok")))
        and engine_load_error is None
        and isinstance(engine_config, Mapping)
    )
    engine_blocked = bool(
        not config_ok and portable_report is not None and not config_path
        and pair_report and not pair_report.get("ok") and pair_errors
    )
    checks.append(
        _check(
            "engine_config",
            "execution",
            "Simulation execution settings",
            "pass" if config_ok else ("warn" if engine_blocked or not live else "fail"),
            (
                (config_path or "generated from the current pair of TB Generated; see Advanced settings for details")
                if config_ok
                else (
                    "Blocked by upstream binding: " + pair_root_detail
                    if engine_blocked else
                    "%s · %s" % (config_path, engine_load_error.message)
                    if engine_load_error is not None
                    else (config_path or ("; ".join(item["message"] for item in portable_report.get("errors", [])[:4]) if portable_report else "First detect both TB signal mappings"))
                )
            ),
            None if config_ok else ("ISF-W1103" if engine_blocked or not live else "ISF-E1101"),
            detected={"blocked_by": ["maestro_pair_contract"] if engine_blocked else []},
            field="portable_setup" if portable_report is not None else "config_path",
        )
    )
    if pair_mode:
        exchange = inspect_waveform_exchange(pair_report, engine_config)
        pair_report["waveform_exchange"] = exchange
        resolved["detection"]["waveform_exchange"] = exchange
        exchange_blocked = bool(exchange.get("blocked_by") and not pair_report.get("ok"))
        checks.append(_check("waveform_exchange", "execution", "VCO → Noise TB Waveform exchange script",
            "pass" if exchange["ok"] else ("warn" if exchange_blocked or not live else "fail"), exchange["summary"],
            None if exchange["ok"] else ("ISF-W1103" if exchange_blocked or not live else "ISF-E1101"),
            field="testbenches", detected=exchange))
    physical_ok = bool(engine_report and engine_report.get("ok"))
    physical_required = live and adapter_mode == "bundled"
    physical_blocked_on_config = physical_required and engine_config is None
    physical_detail = (
        ("Complete signal mappings and unresolved execution settings in Advanced settings"
         if portable_report is not None else
         "select a Cadence engine JSON; its physical_profile or physical_profile_path defines the device/node contract")
        if engine_config is None
        else "not inspected"
    )
    physical_detected = engine_report
    if engine_report is not None:
        profile_info = engine_report.get("profile") or {}
        profile = profile_info.get("profile") or {}
        physical_detail = "%s · %s · %s" % (
            profile.get("profile_id", "profile unavailable"),
            str(profile_info.get("sha256", ""))[:12] or "no hash",
            "explicit" if profile_info.get("explicit") else "legacy replay-only",
        )
        if engine_report.get("errors"):
            physical_detail += " · " + engine_report["errors"][0].get(
                "message", "contract mismatch"
            )
    checks.append(
        _check(
            "physical_profile",
            "profile",
            "Device, Node and noise mapping validation",
            (
                "pass"
                if physical_ok
                else (
                    "warn"
                    if physical_blocked_on_config
                    else ("fail" if physical_required else "warn")
                )
            ),
            physical_detail,
            None
            if physical_ok
            else (
                "ISF-W1103"
                if physical_blocked_on_config or not physical_required
                else "ISF-E1105"
            ),
            detected=physical_detected,
            field=("portable_setup" if portable_report is not None else ("config_path" if engine_config is None else "physical_profile")),
        )
    )
    run_kind = str(resolved.get("run_kind") or ("signoff" if live else "plan")).lower()
    points = engine_config.get("points", []) if isinstance(engine_config, Mapping) else []
    point_count = len(points) if isinstance(points, list) else 0
    formal_requested = bool(live and run_kind == "signoff")
    formal_scope_ok = not formal_requested or point_count >= 6
    checks.append(
        _check(
            "formal_signoff_scope",
            "reliability",
            "Formal Signoff Design-point coverage",
            "pass" if formal_scope_ok else "fail",
            (
                "%d configured design point(s) · formal quality gate" % point_count
                if formal_requested
                else "%s run · formal gate not requested" % run_kind
            ),
            None if formal_scope_ok else "ISF-E3002",
            detected={"run_kind": run_kind, "point_count": point_count},
            field="config_path" if not formal_scope_ok else None,
        )
    )
    auto = resolved.get("auto", {})
    extract_command = auto.get("extract_command", []) if isinstance(auto, Mapping) else []
    if isinstance(extract_command, str):
        try:
            extract_tokens = shlex.split(extract_command)
        except ValueError:
            extract_tokens = []
    elif isinstance(extract_command, (list, tuple)):
        extract_tokens = [str(value) for value in extract_command]
    else:
        extract_tokens = []
    export_contract_ok = bool(
        not formal_requested
        or adapter_mode != "bundled"
        or "--formal-signoff" in extract_tokens
    )
    checks.append(
        _check(
            "export_contract",
            "reliability",
            "Result export mode matches the run contract",
            "pass" if export_contract_ok else "fail",
            (
                "formal signoff exporter enabled"
                if formal_requested and export_contract_ok
                else "%s evidence export" % run_kind
            ),
            None if export_contract_ok else "ISF-E3002",
            detected={"run_kind": run_kind, "extract_command": extract_tokens},
            field="auto.extract_command" if not export_contract_ok else None,
        )
    )
    allow_execute = bool(execution.get("allow_execute", False))
    checks.append(
        _check(
            "execution_authority",
            "execution",
            "Execution authorization",
            "pass" if (not live or allow_execute) else "fail",
            "dry-run" if not live else ("enabled" if allow_execute else "disabled"),
            None if (not live or allow_execute) else "ISF-E1101",
            field="execution.allow_execute",
        )
    )
    auto = resolved.get("auto", {})
    if adapter_mode == "bundled":
        adapter_ok = bool(
            os.path.isfile(environment.get("runner", RUNNER_PATH))
            if environment.get("runner")
            else os.path.isfile(RUNNER_PATH)
        )
        adapter_detail = RUNNER_PATH
    elif adapter_mode in ("custom", "local"):
        adapter_ok = all(
            auto.get(key)
            for key in (
                "vco_command",
                "exchange_command",
                "replica_command",
                "extract_command",
            )
        )
        adapter_detail = "four explicit stage commands"
    elif adapter_mode in ("protocol", "cluster"):
        adapters = execution.get("adapters", {})
        adapter_ok = bool(execution.get("adapter")) or (
            isinstance(adapters, Mapping)
            and all(
                isinstance(adapters.get(role), Mapping)
                for role in ("vco", "exchange", "replica", "extract")
            )
        )
        adapter_detail = "submit/poll/collect/verify protocol · four stages"
    else:
        adapter_ok = False
        adapter_detail = "unsupported adapter_mode: %s" % adapter_mode
    checks.append(
        _check(
            "adapter",
            "execution",
            "Run Adapter",
            "pass" if adapter_ok else ("fail" if live else "warn"),
            "%s · %s" % (adapter_mode, adapter_detail),
            None if adapter_ok else "ISF-E4001",
            field="execution.adapter_mode",
        )
    )
    resume_enabled = bool(execution.get("resume_enabled", True))
    checks.append(
        _check(
            "resume",
            "reliability",
            "Resume state",
            "pass" if resume_enabled else "warn",
            "atomic .isf_run_state.json" if resume_enabled else "disabled",
            None if resume_enabled else "ISF-E5001",
        )
    )

    fail_count = sum(1 for item in checks if item["status"] == "fail")
    warn_count = sum(1 for item in checks if item["status"] == "warn")
    required_user_fields = sorted(
        set(item["field"] for item in checks if item["status"] == "fail" and item.get("field"))
    )
    return {
        "ok": fail_count == 0,
        "status": "fail" if fail_count else ("warn" if warn_count else "pass"),
        "summary": {
            "pass": sum(1 for item in checks if item["status"] == "pass"),
            "warn": warn_count,
            "fail": fail_count,
        },
        "checks": checks,
        "required_user_fields": required_user_fields,
        "detected": {
            "environment": environment,
            "pdk": pdk,
            "maestro_pair": pair_report,
            "inputs": detected_inputs,
            "chosen_input": chosen,
            "engine_contract": engine_report,
        },
        "resolved_config": resolved,
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
    }
