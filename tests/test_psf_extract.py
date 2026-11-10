"""Offline tests for exact Maestro/RDB and PSF read-only extraction."""

from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import unittest

from isf_tool.core.errors import DiscoveryError, ValidationError
from isf_tool.core.psf_extract import (
    PsfReader,
    hbxf_sideband_mapping,
    hbxf_sideband_suffix,
    hbxf_sideband_suffixes,
    load_maestro_runs,
    pair_runs,
)


class MaestroRunFixture:
    @staticmethod
    def make(root, filename="history.rdb", raw_names=("raw",), complete=True):
        rdb_path = os.path.join(root, filename)
        connection = sqlite3.connect(rdb_path)
        try:
            connection.executescript(
                """
                CREATE TABLE psfTable(condition TEXT, rawDir TEXT, resultsDir TEXT);
                CREATE TABLE point(pointID INTEGER PRIMARY KEY, designPointNumber INTEGER, cornerID INTEGER);
                CREATE TABLE test(testID INTEGER PRIMARY KEY, name TEXT UNIQUE);
                CREATE TABLE corner(cornerID INTEGER PRIMARY KEY, name TEXT UNIQUE);
                CREATE TABLE testStatus(pointID INTEGER, testID INTEGER, statusCode INTEGER);
                CREATE TABLE parameter(parameterID INTEGER PRIMARY KEY, name TEXT, defaultValue TEXT, testID INTEGER);
                CREATE TABLE parameterValue(pointID INTEGER, parameterID INTEGER, value NUMERIC);
                """
            )
            connection.execute("INSERT INTO point VALUES (17, 1, 8)")
            connection.execute("INSERT INTO test VALUES (4, 'vco_nominal')")
            connection.execute("INSERT INTO corner VALUES (8, 'Fhigh')")
            connection.execute("INSERT INTO testStatus VALUES (17, 4, 3)")
            connection.executemany(
                "INSERT INTO parameter VALUES (?, ?, ?, ?)",
                [(1, "k", "0.4", None), (2, "bias", "1m", None)],
            )
            connection.executemany(
                "INSERT INTO parameterValue VALUES (?, ?, ?)",
                [(17, 1, 0.4), (17, 2, 0.001)],
            )
            for index, raw_name in enumerate(raw_names):
                connection.execute(
                    "INSERT INTO psfTable VALUES (?, ?, ?)",
                    ("row%d" % index, os.path.join(root, raw_name), "results"),
                )
            connection.commit()
        finally:
            connection.close()

        for raw_name in raw_names:
            psf_dir = os.path.join(root, raw_name, "17", "vco_nominal", "psf")
            os.makedirs(psf_dir)
            if complete:
                with io.open(os.path.join(psf_dir, ".simDone"), "w", encoding="utf-8"):
                    pass
            with io.open(os.path.join(psf_dir, "spectre.out"), "w", encoding="utf-8") as stream:
                if complete:
                    stream.write("spectre completes with 0 errors, 0 warnings.\n")
                else:
                    stream.write("FATAL (SPECTRE-1): synthetic failure\n")
        return rdb_path


class MaestroRunTests(unittest.TestCase):
    def test_loads_exact_identity_corner_and_explicit_parameters(self):
        with tempfile.TemporaryDirectory() as root:
            rdb = MaestroRunFixture.make(root)
            runs = load_maestro_runs(rdb, test="vco_nominal")
            self.assertEqual(1, len(runs))
            run = runs[0]
            self.assertEqual("Fhigh", run["corner"])
            self.assertEqual({"k": 0.4, "bias": 0.001}, run["params"])
            self.assertEqual(17, run["point_id"])
            self.assertEqual(4, run["test_id"])
            self.assertTrue(run["complete"])
            self.assertEqual(
                (
                    os.path.realpath(rdb),
                    os.path.realpath(os.path.join(root, "raw")),
                    17,
                    4,
                    "vco_nominal",
                ),
                run["identity"],
            )
            self.assertEqual(
                os.path.realpath(os.path.join(root, "raw", "17", "vco_nominal", "psf")),
                run["psf_dir"],
            )

    def test_completion_gate_fails_closed_but_can_be_inspected(self):
        with tempfile.TemporaryDirectory() as root:
            rdb = MaestroRunFixture.make(root, complete=False)
            with self.assertRaises(DiscoveryError):
                load_maestro_runs(rdb, require_complete=True)
            runs = load_maestro_runs(rdb, require_complete=False)
            self.assertEqual(1, len(runs))
            self.assertFalse(runs[0]["complete"])
            self.assertFalse(runs[0]["zero_errors"])

    def test_multiple_raw_candidates_fail_instead_of_guessing(self):
        with tempfile.TemporaryDirectory() as root:
            rdb = MaestroRunFixture.make(root, raw_names=("raw-a", "raw-b"))
            with self.assertRaises(DiscoveryError):
                load_maestro_runs(rdb)


class PairingTests(unittest.TestCase):
    @staticmethod
    def record(identity, corner, k):
        return {
            "identity": identity,
            "corner": corner,
            "params": {"k": k},
            "ambiguous_params": {},
        }

    def test_pairs_by_corner_and_sweep_not_point_id(self):
        vco = [
            self.record(("v", 91), "Fhigh", 0.4),
            self.record(("v", 92), "Flow", 0.6),
        ]
        replica = [
            self.record(("r", 3), "Flow", "0.600"),
            self.record(("r", 4), "Fhigh", 0.4),
        ]
        pairs = pair_runs(vco, replica, ["k"])
        self.assertEqual(2, len(pairs))
        by_corner = {row["key"]["corner"]: row for row in pairs}
        self.assertEqual(("v", 91), by_corner["Fhigh"]["vco"]["identity"])
        self.assertEqual(("r", 4), by_corner["Fhigh"]["replica"]["identity"])

    def test_duplicate_and_missing_keys_fail_closed(self):
        run = self.record(("v", 1), "Fhigh", 0.4)
        with self.assertRaises(ValidationError):
            pair_runs([run, dict(run, identity=("v", 2))], [run], ["k"])
        with self.assertRaises(ValidationError):
            pair_runs(
                [self.record(("v", 1), "Fhigh", 0.4)],
                [self.record(("r", 1), "Fhigh", 0.6)],
                ["k"],
            )


class SidebandTests(unittest.TestCase):
    def test_signed_harmonic_to_hbxf_suffix(self):
        self.assertEqual(-4, hbxf_sideband_suffix(-3))
        self.assertEqual(2, hbxf_sideband_suffix(3))
        self.assertEqual(
            {-3: -4, -2: -3, -1: -2, 0: -1, 1: 0, 2: 1, 3: 2},
            hbxf_sideband_mapping(3),
        )
        self.assertEqual(list(range(-4, 3)), hbxf_sideband_suffixes(3))


class PsfReaderTests(unittest.TestCase):
    def test_parses_fundamental_real_complex_and_struct_without_writes(self):
        calls = []

        def runner(command):
            calls.append(command)
            if "-s" not in command:
                return '"fundamental" 5.00456e9\n'
            trace = command[command.index("-t") + 1]
            if trace == "VG":
                return (
                    '"time" 0\n"VG" 0.2\n'
                    '"time" 1e-10\n"VG" 0.3\n'
                )
            if trace == "OUTP":
                return (
                    '"freq" 5.00456e9\n"OUTP" (1.25e-1 -2.5e-2)\n'
                    '"freq" 1.000912e10\n"OUTP" (2e-2 3e-2)\n'
                )
            if trace == "M1.m0":
                return (
                    '"relative frequency" 9.12e3\n'
                    '"M1.m0" (\n0\n1\n2\n4e-30\n)\n'
                    '"relative frequency" 1e4\n'
                    '"M1.m0" (\n0\n1\n2\n9e-30\n)\n'
                )
            return {"returncode": 1, "stdout": "", "stderr": "unknown trace"}

        reader = PsfReader("/cadence/bin/psf", runner=runner)
        self.assertEqual(5.00456e9, reader.fundamental("/readonly/psf"))
        self.assertEqual([(0.0, 0.2), (1e-10, 0.3)], reader.real_wave("td", "VG"))
        complex_rows = reader.complex_wave("fd", "OUTP")
        self.assertEqual(complex(0.125, -0.025), complex_rows[0][1])
        struct = reader.struct_noise("noise", "M1.m0")
        self.assertEqual([0.0, 1.0, 2.0, 9e-30], struct[1e4])
        self.assertEqual(9e-30, reader.struct_value_at("noise", "M1.m0", 1e4, 3))
        self.assertTrue(all(isinstance(command, list) for command in calls))
        self.assertTrue(all(command[0] == "/cadence/bin/psf" for command in calls))
        self.assertFalse(any(".simDone" in token for command in calls for token in command))


if __name__ == "__main__":
    unittest.main()
