import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from isf_tool.core.preflight import _find_executable, _find_cadence_companion, _probe_cadence_env


class CadenceInvocationTests(unittest.TestCase):
    def test_wrapper_alias_preserved_for_explicit_path_search_and_env_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / "share" / "bin" / "cdnWrapper"
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text("#!/bin/sh\nexit 0\n")
            wrapper.chmod(0o700)
            bindir = root / "tools.lnx86" / "dfII" / "bin"
            bindir.mkdir(parents=True)
            alias = bindir / "psf"
            alias.symlink_to(wrapper)
            expected = str(alias)
            self.assertNotEqual(expected, os.path.realpath(alias))
            self.assertEqual(expected, _find_executable("psf", alias))
            with mock.patch("isf_tool.core.preflight.shutil.which", return_value=expected):
                self.assertEqual(expected, _find_executable("psf"))
            self.assertEqual(expected, _find_cadence_companion("psf", {"virtuoso": str(root / "bin" / "virtuoso")}))
            with mock.patch("isf_tool.core.preflight.subprocess.run", return_value=mock.Mock(stdout="psf=" + expected + "\n")):
                self.assertEqual(expected, _probe_cadence_env("/unused/bridge.sh")["psf"])


if __name__ == "__main__":
    unittest.main()
