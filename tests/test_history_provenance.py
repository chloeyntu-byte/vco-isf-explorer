import os
from pathlib import Path
import tempfile
import unittest

from isf_tool.core.maestro_pair import _history_binding, _source_design_header


class HistoryProvenanceTests(unittest.TestCase):
    def test_root_config_header_not_nested_schematic_and_not_mtime_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.scs"
            path.write_text("// Design library name: TEST\n// Design cell name: oscillator\n"
                "// Design view name: config\nsimulator lang=spectre\n"
                "// Library name: TEST\n// Cell name: child\n// View name: schematic\n")
            record = dict(name="Interactive.2", source_netlists=[dict(path=str(path), point_id="1")])
            fingerprint, manifest = _history_binding(record)
            source = manifest["source_netlists"][0]
            self.assertEqual(source["source_design"], dict(library="TEST", cell="oscillator", view="config"))
            self.assertEqual(source["source_mtime"], path.stat().st_mtime)
            os.utime(path, (1, 1))
            after_fingerprint, after = _history_binding(record)
            self.assertEqual(fingerprint, after_fingerprint)
            self.assertEqual(after["source_netlists"][0]["source_mtime"], 1)

    def test_missing_and_ambiguous_headers_stay_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.scs"
            for content in ("// View name: schematic\nsimulator lang=spectre\n",
                            "// Design view name: config\n// Design view name: schematic\n"):
                path.write_text(content)
                self.assertIsNone(_source_design_header(path)["view"])


if __name__ == "__main__":
    unittest.main()
