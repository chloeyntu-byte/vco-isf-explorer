"""Cadence history renames are exact identities, never newest-directory guesses."""
import copy
import datetime
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from isf_tool.core.discovery import inspect_rdb
from isf_tool.core.errors import DiscoveryError
from isf_tool.core.maestro_pair import _history_binding, _history_records
from isf_tool.core.preflight import _runtime_identity
from isf_tool.core.psf_extract import load_maestro_runs


class RenamedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.maestro = self.root / "setup"
        self.rdb = self.maestro / "results" / "maestro" / "Named_DC.rdb"
        self.rdb.parent.mkdir(parents=True)
        self.declared = self.root / "simulation" / "Interactive.0"
        self.named = self.declared.parent / self.rdb.stem
        self.start = datetime.datetime(2026, 9, 8, 1, 0, 0)
        self.stop = self.start + datetime.timedelta(seconds=3)
        with sqlite3.connect(str(self.rdb)) as connection:
            connection.executescript('''
                CREATE TABLE psfTable(condition TEXT, rawDir TEXT, resultsDir TEXT);
                CREATE TABLE point(pointID INTEGER PRIMARY KEY, designPointNumber INTEGER, cornerID INTEGER);
                CREATE TABLE test(testID INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE corner(cornerID INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE testStatus(pointID INTEGER, testID INTEGER, statusCode INTEGER, startTime TEXT, stopTime TEXT);
                CREATE TABLE parameter(parameterID INTEGER PRIMARY KEY, name TEXT, defaultValue TEXT, testID INTEGER);
                CREATE TABLE parameterValue(pointID INTEGER, parameterID INTEGER, value NUMERIC);
                INSERT INTO point VALUES (17, 1, 1);
                INSERT INTO test VALUES (2, 'REF_DC');
                INSERT INTO corner VALUES (1, '');
            ''')
            connection.execute("INSERT INTO psfTable VALUES (?, ?, ?)", ("_ROWID_=1", str(self.declared), "original"))
            connection.execute("INSERT INTO testStatus VALUES (17, 2, 3, ?, ?)",
                               (str(self.start.timestamp()), str(self.stop.timestamp())))
        self.psf = self.named / "17" / "REF_DC" / "psf"
        self.psf.mkdir(parents=True)
        (self.psf / ".simDone").touch()
        (self.psf / "spectre.out").write_text(self.log())
        self.netlist = self.psf.parent / "netlist" / "input.scs"
        self.netlist.parent.mkdir()
        self.netlist.write_text("parameters VGS=0.6 VDS=1.2\nsimulatorOptions options temp=27\n")

    def log(self, seconds=0):
        interval = datetime.timedelta(seconds=seconds)
        pattern = "%I:%M:%S %p, %a %b %d, %Y"
        return ("Simulation started at: %s, ended at: %s, with elapsed time (wall clock): 3 s.\n"
                "spectre completes with 0 errors, 0 warnings.\n") % (
                    (self.start + interval).strftime(pattern), (self.stop + interval).strftime(pattern))

    def inspect(self):
        return inspect_rdb({"path": str(self.rdb)})

    def test_exact_renamed_history_preserves_declared_path_and_rdb_bytes(self):
        before = hashlib.sha256(self.rdb.read_bytes()).hexdigest()
        report = self.inspect()
        run = report["runs"][0]
        self.assertTrue(run["complete"])
        self.assertEqual(run["declaredRawDir"], str(self.declared))
        self.assertEqual(run["rawDir"], str(self.named))
        self.assertEqual(run["psfDir"], str(self.psf))
        self.assertEqual(report["rawDir"], [str(self.declared)])
        self.assertEqual(report["effectiveRawDir"], [str(self.named)])
        self.assertTrue(run["rawDirResolution"]["run_evidence"]["run_interval"]["matches"])
        self.assertEqual(before, hashlib.sha256(self.rdb.read_bytes()).hexdigest())
        loaded = load_maestro_runs(str(self.rdb), test="REF_DC")
        self.assertEqual(loaded[0]["raw_dir"], str(self.named))
        self.assertEqual(loaded[0]["point_id"], 17)

    def test_selected_history_uses_effective_paths_and_seals_resolution(self):
        _histories, selected = _history_records(str(self.maestro), "REF_DC", requested_history=self.rdb.stem)
        self.assertIsNotNone(selected)
        self.assertTrue(selected["fully_complete"])
        self.assertEqual(selected["stale_raw_dirs"], [])
        self.assertEqual(selected["declared_raw_dirs"], [str(self.declared)])
        self.assertEqual(selected["source_netlists"][0]["path"], str(self.netlist))
        fingerprint, manifest = _history_binding(selected)
        self.assertIn("raw_dir_resolutions", manifest)
        self.assertEqual(manifest["source_netlists"][0]["path"], str(self.netlist))
        changed = copy.deepcopy(selected)
        changed["raw_dir_resolutions"][0]["effective_raw_dir"] = "/different/location"
        self.assertNotEqual(fingerprint, _history_binding(changed)[0])

    def test_declared_and_named_roots_coexisting_is_rejected(self):
        self.declared.mkdir()
        with self.assertRaisesRegex(DiscoveryError, "同时存在"):
            self.inspect()

    def test_root_symlink_is_not_a_rename_proof(self):
        actual = self.named.with_name("other")
        self.named.rename(actual)
        self.named.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(DiscoveryError):
            self.inspect()

    def test_point_test_symlink_escape_is_not_complete(self):
        actual = self.root / "outside"
        self.psf.parent.rename(actual)
        self.psf.parent.symlink_to(actual, target_is_directory=True)
        self.assertFalse(self.inspect()["runs"][0]["complete"])

    def test_named_root_symlink_outside_original_parent_is_rejected(self):
        actual = self.root / "outside"
        self.named.rename(actual)
        self.named.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(DiscoveryError):
            self.inspect()

    def test_complete_nominal_mirror_cannot_substitute_for_exact_point(self):
        mirror = self.named / "psf"
        (self.named / "17").rename(mirror)
        self.assertTrue((mirror / "REF_DC" / "psf" / ".simDone").is_file())
        self.assertFalse(self.inspect()["runs"][0]["complete"])

    def test_missing_netlist_is_not_complete_even_with_zero_error_psf(self):
        self.netlist.unlink()
        self.assertFalse(self.inspect()["runs"][0]["complete"])
        self.assertIsNone(_history_records(str(self.maestro), "REF_DC")[1])

    def test_missing_completion_marker_is_not_complete(self):
        (self.psf / ".simDone").unlink()
        self.assertFalse(self.inspect()["runs"][0]["complete"])

    def test_fatal_log_cannot_be_hidden_by_zero_error_line(self):
        (self.psf / "spectre.out").write_text("FATAL (SPECTRE-1): failed\n" + self.log())
        self.assertFalse(self.inspect()["runs"][0]["complete"])

    def test_other_run_timestamp_is_rejected_without_using_mtime(self):
        (self.psf / "spectre.out").write_text(self.log(seconds=-300))
        run = self.inspect()["runs"][0]
        self.assertFalse(run["complete"])
        self.assertFalse(run["rawDirResolution"]["run_evidence"]["run_interval"]["matches"])

    def test_missing_log_interval_fails_when_rdb_has_run_times(self):
        (self.psf / "spectre.out").write_text("spectre completes with 0 errors.\n")
        self.assertFalse(self.inspect()["runs"][0]["complete"])

    def test_legacy_rdb_without_absolute_times_still_requires_exact_complete_tree(self):
        with sqlite3.connect(str(self.rdb)) as connection:
            connection.execute("UPDATE testStatus SET startTime=NULL, stopTime=NULL")
        self.assertTrue(self.inspect()["runs"][0]["complete"])

    def test_start_time_without_stop_time_is_not_legacy(self):
        with sqlite3.connect(str(self.rdb)) as connection:
            connection.execute("UPDATE testStatus SET stopTime=NULL")
        run = self.inspect()["runs"][0]
        self.assertFalse(run["complete"])
        self.assertFalse(run["rawDirResolution"]["run_evidence"]["run_interval"]["matches"])

    def test_stop_time_without_start_time_is_not_legacy(self):
        with sqlite3.connect(str(self.rdb)) as connection:
            connection.execute("UPDATE testStatus SET startTime=NULL")
        run = self.inspect()["runs"][0]
        self.assertFalse(run["complete"])
        self.assertFalse(run["rawDirResolution"]["run_evidence"]["run_interval"]["matches"])

    def test_no_fallback_to_different_sibling_or_nominal_psf(self):
        self.named.rename(self.named.with_name("newest_other_history"))
        self.assertFalse(self.inspect()["runs"][0]["complete"])
        self.assertEqual(self.inspect()["rawDirResolutions"], [])

    def test_wrong_point_test_tree_does_not_satisfy_run(self):
        self.psf.parent.rename(self.psf.parent.with_name("OTHER_TEST"))
        self.assertFalse(self.inspect()["runs"][0]["complete"])

    def test_missing_one_requested_point_prevents_history_selection(self):
        with sqlite3.connect(str(self.rdb)) as connection:
            connection.execute("INSERT INTO point VALUES (18, 2, 1)")
            connection.execute("INSERT INTO testStatus VALUES (18, 2, 3, ?, ?)",
                               (str(self.start.timestamp()), str(self.stop.timestamp())))
        self.assertEqual(self.inspect()["completion"], {"checked": 2, "complete": 1, "incomplete": 1})
        self.assertIsNone(_history_records(str(self.maestro), "REF_DC")[1])

    def test_normal_existing_raw_root_keeps_legacy_contract(self):
        self.named.rename(self.declared)
        report = self.inspect()
        self.assertTrue(report["runs"][0]["complete"])
        self.assertEqual(report["rawDirResolutions"], [])
        selected = _history_records(str(self.maestro), "REF_DC")[1]
        self.assertNotIn("raw_dir_resolutions", _history_binding(selected)[1])

    def test_discovery_code_is_in_runtime_seal(self):
        identity = _runtime_identity(None)
        record = identity["files"]["discovery_module"]
        self.assertTrue(record["path"].endswith("/core/discovery.py"))
        self.assertEqual(record["sha256"], hashlib.sha256(Path(record["path"]).read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
