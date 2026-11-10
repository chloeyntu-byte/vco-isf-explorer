"""Portable sweep import must recompute CSVs and fence every child bundle."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from isf_tool.core.errors import ValidationError
from isf_tool.core.offset_sweep import import_offset_sweep
from isf_tool.core.orchestrator import _tree_identity
from synthetic_fixture import make_canonical, write_canonical_bundle


class PortableSweepTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "bundle"
        self.manifest = {"schema_version": 1, "complete": True,
                         "offsets_hz": [1000, 10000], "children": []}
        for hz in self.manifest["offsets_hz"]:
            data = make_canonical(signoff=False, point_count=2)
            data["metadata"]["offset_hz"] = hz
            for row in data["vco_results"]:
                row["offset_Hz"] = hz
            relative = "offsets/%sHz" % hz
            path = write_canonical_bundle(self.root / relative, canonical=data)
            self.manifest["children"].append({"offset_hz": hz, "bundle_path": relative,
                "bundle_tree_sha256": _tree_identity(path)["sha256"]})
        self.save_manifest()

    def save_manifest(self):
        (self.root / "offset_sweep.json").write_text(json.dumps(self.manifest))

    def test_relocated_bundle_recomputes_without_trusting_saved_plots(self):
        (self.root / "result.json").write_text('{"summary":[{"dynamic_dBc_Hz":999}]}')
        moved = self.root.parent / "relocated"
        shutil.move(str(self.root), str(moved))
        result = import_offset_sweep({"path": str(moved)})
        self.assertEqual(len(result["summary"]), 4)
        self.assertEqual(result["offset_sweep"]["operating_point_count"], 2)
        self.assertFalse(result["signoff"])
        self.assertTrue(all(row["dynamic_dBc_Hz"] < 0 for row in result["summary"]))

    def test_content_change_is_rejected(self):
        path = self.root / self.manifest["children"][0]["bundle_path"] / "metadata.json"
        path.write_text(path.read_text() + "\n")
        with self.assertRaises(ValidationError):
            import_offset_sweep({"path": str(self.root)})

    def test_missing_duplicate_and_wrong_frequency_are_rejected(self):
        original = copy.deepcopy(self.manifest)
        for change in ("missing", "duplicate", "wrong"):
            with self.subTest(change=change):
                self.manifest = copy.deepcopy(original)
                if change == "missing":
                    self.manifest["children"].pop()
                elif change == "duplicate":
                    self.manifest["children"].append(copy.deepcopy(self.manifest["children"][0]))
                else:
                    self.manifest["children"][0]["offset_hz"] = 99
                self.save_manifest()
                with self.assertRaises(ValidationError):
                    import_offset_sweep({"path": str(self.root)})

    def test_absolute_parent_and_symlink_escape_are_rejected(self):
        external = self.root.parent / "outside"
        external.mkdir()
        (self.root / "escape").symlink_to(external, target_is_directory=True)
        for path in (str(external), "../outside", "escape"):
            with self.subTest(path=path):
                self.manifest["children"][0]["bundle_path"] = path
                self.save_manifest()
                with self.assertRaises(ValidationError):
                    import_offset_sweep({"path": str(self.root)})

    def test_partial_and_self_claimed_signoff_are_rejected(self):
        with self.assertRaises(ValidationError):
            import_offset_sweep({"path": str(self.root), "signoff": True})
        self.manifest["complete"] = False
        self.save_manifest()
        with self.assertRaises(ValidationError):
            import_offset_sweep({"path": str(self.root)})


if __name__ == "__main__":
    unittest.main()
