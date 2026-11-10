"""Locate bundled runtime assets in a source checkout or installed wheel."""

from __future__ import annotations

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_ROOT.parent
_REQUIRED_SCRIPTS = (
    "cadence_dual_tb_runner.py",
    "dynamic_isf_closure_engine.py",
    "export_audited_bundle.py",
    "extract_dual_tb_results.py",
)


def _runtime_root() -> Path:
    source_scripts = SOURCE_ROOT / "scripts"
    if all((source_scripts / name).is_file() for name in _REQUIRED_SCRIPTS):
        return SOURCE_ROOT
    return PACKAGE_ROOT / "_runtime"


RUNTIME_ROOT = _runtime_root()
SCRIPTS_ROOT = RUNTIME_ROOT / "scripts"
CONFIGS_ROOT = RUNTIME_ROOT / "configs"


def script_path(name: str) -> Path:
    """Return one bundled runtime script without assuming a checkout layout."""

    if name not in _REQUIRED_SCRIPTS:
        raise ValueError("unknown bundled script: %s" % name)
    return SCRIPTS_ROOT / name


def config_path(name: str) -> Path:
    """Return a bundled, synthetic example configuration path."""

    candidate = CONFIGS_ROOT / name
    if candidate.parent != CONFIGS_ROOT:
        raise ValueError("invalid bundled config name: %s" % name)
    return candidate


__all__ = [
    "PACKAGE_ROOT",
    "SOURCE_ROOT",
    "RUNTIME_ROOT",
    "SCRIPTS_ROOT",
    "CONFIGS_ROOT",
    "script_path",
    "config_path",
]
