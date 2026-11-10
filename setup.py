"""Build hook that keeps runtime scripts/configs available after wheel install."""

from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


ROOT = Path(__file__).resolve().parent
RUNTIME_SCRIPTS = (
    "cadence_dual_tb_runner.py",
    "dynamic_isf_closure_engine.py",
    "export_audited_bundle.py",
    "extract_dual_tb_results.py",
)
RUNTIME_CONFIGS = (
    "README.md",
    "app_config.example.json",
    "engine_pair_example_flat.json",
    "engine_pair_example_hierarchical.json",
    "physical_profile_example_flat.json",
    "physical_profile_example_hierarchical.json",
)


class build_py(_build_py):
    def run(self):
        super().run()
        runtime = Path(self.build_lib) / "isf_tool" / "_runtime"
        script_target = runtime / "scripts"
        script_target.mkdir(parents=True, exist_ok=True)
        for name in RUNTIME_SCRIPTS:
            shutil.copy2(ROOT / "scripts" / name, script_target / name)
        config_target = runtime / "configs"
        config_target.mkdir(parents=True, exist_ok=True)
        for name in RUNTIME_CONFIGS:
            shutil.copy2(ROOT / "configs" / name, config_target / name)


setup(cmdclass={"build_py": build_py})
