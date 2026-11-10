import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from isf_tool.cli import main
from isf_tool.core.environment_report import format_environment_report, inspect_environment
from isf_tool.core.preflight import detect_environment
from isf_tool.core.service import ToolService


def _executable(path, text="#!/bin/sh\nexit 0\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o700)
    return str(path)


class ToolDiscoveryTests(unittest.TestCase):
    def test_native_context_precedes_bridge_and_path_but_respects_manual_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native = _executable(root / "invoking tools" / "spectre")
            bridge_tool = _executable(root / "bridge tools" / "spectre")
            manual = _executable(root / "manual tools" / "spectre")
            path_tool = _executable(root / "path" / "spectre")
            psf = _executable(root / "invoking tools" / "psf")
            bridge = root / "bridge.sh"
            bridge.write_text(f'export PATH="{Path(bridge_tool).parent}:$PATH"\n', encoding="utf-8")
            config = {"profiles": {"environment": {"cadence_env": str(bridge), "executables": {}}}}
            context = {"runtime_env": {"PATH": str(Path(native).parent)}}
            with mock.patch.dict(os.environ, {"PATH": str(Path(path_tool).parent)}):
                detected = detect_environment(config, maestro_context=context)
                self.assertEqual(native, detected["executables"]["spectre"])
                self.assertEqual(psf, detected["executables"]["psf"])
                self.assertEqual("maestro-context", detected["tool_details"]["spectre"]["source"])
                self.assertEqual(bridge_tool, detect_environment(config)["executables"]["spectre"])
                pins = config["profiles"]["environment"]["executables"]
                pins["spectre"] = manual
                self.assertEqual(manual, detect_environment(config, maestro_context=context)["executables"]["spectre"])
                pins["spectre"] = str(root / "missing" / "spectre")
                invalid = detect_environment(config, maestro_context=context)["tool_details"]["spectre"]
                self.assertEqual("invalid", invalid["status"])
                self.assertIsNone(invalid["path"])

    def test_environment_diagnostic_reads_bridge_without_sourcing_or_leaking_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spectre = _executable(root / "bridge tools" / "spectre")
            psf = _executable(root / "bridge tools" / "psf")
            marker = root / "did-run"
            workspace = root / "not-created"
            bridge = root / "cadence_bridge_env.sh"
            bridge.write_text(f'export PATH="{Path(spectre).parent}:$PATH"\ntouch "{marker}"\n', encoding="utf-8")
            config = {"project_root": str(root), "execution": {"dry_run": False, "allow_execute": True}}
            context = {"runtime_env": {"CDS_LIC_FILE": "45678@local-license-private.invalid"}}
            with mock.patch("isf_tool.core.preflight._probe_cadence_env", side_effect=AssertionError("sourced bridge")):
                report = inspect_environment(config, workspace, context)
            self.assertEqual(spectre, report["tools"]["spectre"]["path"])
            self.assertEqual(psf, report["tools"]["psf"]["path"])
            self.assertEqual("not_tested", report["native_execution"])
            self.assertTrue(report["license_environment_present"])
            self.assertNotIn("local-license-private.invalid", json.dumps(report))
            self.assertIn("许可证", format_environment_report(report))
            self.assertFalse(marker.exists())
            self.assertFalse(workspace.exists())

    def test_invalid_explicit_bridge_and_analysis_python_do_not_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = root / "cadence_bridge_env.sh"
            bridge.write_text("PATH=/example/bin\n", encoding="utf-8")
            config = {
                "project_root": str(root),
                "profiles": {"environment": {"cadence_env": str(root / "missing-bridge"), "python": str(root / "missing-python")}},
            }
            detected = detect_environment(config)
            self.assertIsNone(detected["cadence_env"])
            self.assertEqual("invalid", detected["cadence_env_details"]["status"])
            self.assertIsNone(detected["analysis_python"]["executable"])
            self.assertFalse(detected["analysis_python"]["probe_deferred"])

    def test_tool_service_does_not_serialize_native_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spectre = _executable(root / "native" / "spectre")
            context = {"runtime_env": {"PATH": str(Path(spectre).parent), "CDS_LIC_FILE": "private-test-value"}}
            service = ToolService(root, maestro_context=context)
            report = service.preflight({"config": {"source_mode": "maestro_pair"}})
            self.assertEqual(spectre, report["resolved_config"]["detection"]["environment"]["executables"]["spectre"])
            self.assertNotIn("private-test-value", json.dumps(report))


class DiagnosticEntryPointTests(unittest.TestCase):
    def test_cli_is_headless_read_only_and_returns_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "absent workspace"
            output = io.StringIO()
            report = {"status": "attention", "native_execution": "not_tested"}
            with mock.patch("isf_tool.core.environment_report.inspect_environment", return_value=report) as inspect, contextlib.redirect_stdout(output):
                self.assertEqual(1, main(["--diagnose", "--workspace", str(workspace), "--json"]))
            inspect.assert_called_once_with({}, workspace, None)
            self.assertEqual(report, json.loads(output.getvalue()))
            self.assertFalse(workspace.exists())

    @unittest.skipUnless(shutil.which("bash") and (shutil.which("timeout") or shutil.which("gtimeout")), "launcher utilities required")
    def test_launcher_auto_detection_and_bad_explicit_override(self):
        source = Path(__file__).resolve().parents[1] / "cadence" / "vco_isf_launch"
        with tempfile.TemporaryDirectory(prefix="isf launcher ") as directory:
            root = Path(directory)
            launcher = root / "cadence" / source.name
            launcher.parent.mkdir()
            shutil.copy2(source, launcher)
            context = root / "context file"
            context.touch()
            fake_python = _executable(root / ".venv" / "bin" / "python", '#!/bin/sh\nif [ "$1" = -I ]; then\n  echo ready\nelse\n  printf "%s\\n" "$2" "$3" "$4"\nfi\n')
            environment = os.environ.copy()
            environment.pop("VCO_ISF_PYTHON", None)
            command = [str(launcher), str(context)]
            result = subprocess.run(command, text=True, capture_output=True, env=environment, timeout=15)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(["run.py", "--maestro-context-file", str(context)], [Path(result.stdout.splitlines()[0]).name, *result.stdout.splitlines()[1:]])
            result = subprocess.run(command, text=True, capture_output=True, env={**environment, "VCO_ISF_PYTHON": fake_python}, timeout=15)
            self.assertEqual(0, result.returncode, result.stderr)
            result = subprocess.run(command, text=True, capture_output=True, env={**environment, "VCO_ISF_PYTHON": "/bin/true"}, timeout=15)
            self.assertEqual(2, result.returncode)
            self.assertIn("VCO_ISF_PYTHON", result.stderr)


if __name__ == "__main__":
    unittest.main()
