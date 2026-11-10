"""Relocation invariants for the lightweight Cadence adapter."""

from __future__ import unicode_literals

import importlib.util
import os
import tempfile
import unittest

from isf_tool.core.preflight import _cadence_env_path, _find_cadence_companion


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER_PATH = os.path.join(PROJECT_ROOT, "scripts", "cadence_dual_tb_runner.py")


class RelocationTests(unittest.TestCase):
    def test_default_dynamic_engine_is_bundled_with_the_tool(self):
        spec = importlib.util.spec_from_file_location("portable_runner", RUNNER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        engine = os.path.realpath(str(module.DEFAULT_ENGINE))
        scripts_root = os.path.realpath(os.path.join(PROJECT_ROOT, "scripts"))
        self.assertEqual(os.path.commonpath((engine, scripts_root)), scripts_root)
        self.assertTrue(os.path.isfile(engine))

    def test_deep_relocation_finds_cadence_environment_and_companion_psf(self):
        with tempfile.TemporaryDirectory() as root:
            bridge = os.path.join(root, "cadence_bridge_env.sh")
            with open(bridge, "w") as stream:
                stream.write("#!/bin/sh\n")
            nested = os.path.join(root, *("level_%02d" % index for index in range(9)))
            os.makedirs(nested)
            self.assertEqual(
                _cadence_env_path({}, {"project_root": nested}),
                os.path.realpath(bridge),
            )

            install = os.path.join(root, "cadence", "IC618")
            virtuoso = os.path.join(install, "bin", "virtuoso")
            psf = os.path.join(
                install, "tools.lnx86", "dfII", "bin", "64bit", "psf"
            )
            os.makedirs(os.path.dirname(virtuoso))
            os.makedirs(os.path.dirname(psf))
            for path in (virtuoso, psf):
                with open(path, "w") as stream:
                    stream.write("#!/bin/sh\n")
                os.chmod(path, 0o755)
            self.assertEqual(
                _find_cadence_companion("psf", {"virtuoso": virtuoso}),
                os.path.realpath(psf),
            )


if __name__ == "__main__":
    unittest.main()
