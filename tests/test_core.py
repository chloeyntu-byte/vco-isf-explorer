"""Offline regression tests for the portable backend core."""

from __future__ import unicode_literals

import glob
import csv
import io
import json
import math
import os
import sqlite3
import tempfile
import unittest

from isf_tool.core import dual_tb as dual_tb_module
from isf_tool.core.adapters import (
    CustomShellAdapter,
    RunSimulationAdapter,
    TEMPLATE_VARIABLE_WHITELIST,
    render_template,
)
from isf_tool.core.bundle import import_csv_bundle
from isf_tool.core.discovery import discover_result_directories, inspect_rdb
from isf_tool.core.dual_tb import import_dual_tb_bundle, recompute_dual_tb
from isf_tool.core.errors import BundleImportError, TemplateValidationError
from isf_tool.core.limits import MAX_PHASE_CANDIDATE_HARMONICS
from isf_tool.core.service import ToolService

try:
    from .synthetic_fixture import write_closure_bundle
except ImportError:  # unittest discovery loads test modules as top-level names
    from synthetic_fixture import write_closure_bundle


class SyntheticBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.bundle_root = write_closure_bundle(
            os.path.join(cls.temporary.name, "six_points")
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_canonical_pair_uses_independent_power_sum(self):
        canonical_root = os.path.join(self.bundle_root, "canonical")

        def read_csv(name):
            with io.open(os.path.join(canonical_root, name), "r", encoding="utf-8") as stream:
                return [dict(row) for row in csv.DictReader(stream)]

        with io.open(os.path.join(canonical_root, "metadata.json"), "r", encoding="utf-8") as stream:
            metadata = json.load(stream)
        vco_rows = read_csv("vco_results.csv")
        replica_rows = read_csv("replica_results.csv")
        workpoint_rows = read_csv("workpoint.csv")
        baseline = recompute_dual_tb(
            {
                "vco_results": vco_rows,
                "replica_results": replica_rows,
                "workpoint": workpoint_rows,
                "metadata": metadata,
                "options": {"signoff": False},
            }
        )
        singles = dict(
            (row["point"], float(row["dynamic_pm_asd_V_rtHz"]))
            for row in baseline["summary"]
        )
        for row in vco_rows:
            single = singles[row["point"]]
            predicted_pair = math.sqrt(single * single + (2.0 * single) ** 2)
            row["second_device_predicted_pm_asd_V_rtHz"] = 2.0 * single
            row["spectre_pair_pm_asd_V_rtHz"] = predicted_pair * (10.0 ** (1.0 / 20.0))
        metadata.update(
            {
                "device_count": 2,
                "second_device_model": "symmetric_independent",
                "independent_device_combination": "power",
            }
        )
        combined = recompute_dual_tb(
            {
                "vco_results": vco_rows,
                "replica_results": replica_rows,
                "workpoint": workpoint_rows,
                "metadata": metadata,
                # This mutation intentionally exercises replay behavior.
                # Formal signoff is covered by the v2 I/Q contract tests.
                "options": {"signoff": False},
            }
        )
        for row in combined["summary"]:
            single = singles[row["point"]]
            self.assertAlmostEqual(
                math.sqrt(5.0) * single,
                row["predicted_pair_pm_asd_V_rtHz"],
                places=18,
            )
            self.assertAlmostEqual(-1.0, row["pair_error_dB"], places=10)
            self.assertEqual("independent_power_sum", row["device_combination"])

    def test_closure_bundle_prefers_per_point_details(self):
        result = import_csv_bundle({"path": self.bundle_root})

        self.assertEqual(6, len(result["points"]))
        self.assertEqual(6, len(result["summary"]))
        for point_name, tables in result["points"].items():
            self.assertTrue(point_name.startswith("point_"), point_name)
            self.assertEqual(65, len(tables["workpoint"]), point_name)
            self.assertEqual(4, len(tables["harmonics"]), point_name)
            self.assertEqual(8, len(tables["phase_candidates"]), point_name)
            self.assertEqual(1, len(tables["summary"]), point_name)

        warnings = "\n".join(result["bundle"]["warnings"])
        self.assertIn("ignored aggregate harmonics", warnings)
        self.assertIn("ignored aggregate phase_candidates", warnings)
        self.assertNotIn("vco_results", [
            key for point in result["points"].values() for key in point
        ])

    def test_canonical_two_tb_import_recomputes_all_points(self):
        result = import_dual_tb_bundle(
            {
                "path": os.path.join(self.bundle_root, "canonical"),
                "signoff": False,
            }
        )
        self.assertEqual(6, len(result["summary"]))
        self.assertEqual(6, len(result["points"]))
        for tables in result["points"].values():
            self.assertEqual(4, len(tables["harmonics"]))
            self.assertEqual(8, len(tables["phase_candidates"]))
        first = dict((row["point"], row) for row in result["summary"])[
            "point_01_PDK_A_typ"
        ]
        self.assertGreater(first["dynamic_pm_asd_V_rtHz"], 0.0)
        self.assertAlmostEqual(-2.0, first["dynamic_error_dB"], places=11)

    def test_am_pm_only_selection_uses_cross_point_continuity(self):
        canonical_root = os.path.join(self.bundle_root, "canonical")

        def read(name):
            with io.open(os.path.join(canonical_root, name), "r", encoding="utf-8") as stream:
                return [dict(row) for row in csv.DictReader(stream)]

        vco_rows = read("vco_results.csv")
        replica_rows = read("replica_results.csv")
        expected = {}
        for row in replica_rows:
            expected.setdefault(row["point"], row["phase_candidate"])
            for key in (
                "Mdyn_real_A_rtHz", "Mdyn_imag_A_rtHz", "phase_candidate",
                "phase_score", "Dk_real", "Dk_imag", "sqrt_Sxi_A_rtHz",
            ):
                row.pop(key, None)
        calculated = recompute_dual_tb(
            {
                "vco_results": vco_rows,
                "replica_results": replica_rows,
                "workpoint": [],
                "metadata": {},
                "options": {"signoff": False},
            }
        )
        selected = dict((row["point"], row["chosen_signs"]) for row in calculated["summary"])
        self.assertEqual(expected, selected)

    def test_phase_candidate_search_rejects_exponential_input(self):
        harmonics = range(1, MAX_PHASE_CANDIDATE_HARMONICS + 2)
        id_harmonics = dict((item, 1.0 + 0.0j) for item in harmonics)
        am_asd = dict((item, 1.0) for item in harmonics)
        pm_asd = dict((item, 1.0) for item in harmonics)
        dc_coeffs = dict((item, 1.0 + 0.0j) for item in harmonics)

        with self.assertRaises(BundleImportError) as caught:
            dual_tb_module._candidate_sets(
                id_harmonics,
                am_asd,
                pm_asd,
                dc_coeffs,
            )

        self.assertEqual("ISF-E3001", caught.exception.code)
        self.assertEqual(
            MAX_PHASE_CANDIDATE_HARMONICS + 1,
            caught.exception.details["harmonic_count"],
        )
        self.assertEqual(
            MAX_PHASE_CANDIDATE_HARMONICS,
            caught.exception.details["maximum"],
        )


class RDBDiscoveryTests(unittest.TestCase):
    def _make_rdb(self, root):
        raw_dir = os.path.join(root, "raw")
        rdb_path = os.path.join(root, "Interactive.0.rdb")
        connection = sqlite3.connect(rdb_path)
        try:
            connection.executescript(
                """
                CREATE TABLE psfTable(condition TEXT PRIMARY KEY, rawDir TEXT, resultsDir TEXT);
                CREATE TABLE point(pointID INTEGER PRIMARY KEY, designPointNumber INTEGER, merit FLOAT, cornerID INTEGER, groupID INTEGER);
                CREATE TABLE test(testID INTEGER PRIMARY KEY, name TEXT UNIQUE);
                CREATE TABLE corner(cornerID INTEGER PRIMARY KEY, name TEXT UNIQUE);
                CREATE TABLE testStatus(pointID INTEGER, testID INTEGER, statusCode INTEGER);
                """
            )
            connection.execute(
                "INSERT INTO psfTable VALUES (?, ?, ?)",
                ("_ROWID_=1", raw_dir, "$PROJECT/results/Interactive.0"),
            )
            connection.executemany(
                "INSERT INTO point VALUES (?, ?, ?, ?, ?)",
                [(1, 1, 0.0, 1, None), (2, 1, 0.0, 2, None)],
            )
            connection.executemany(
                "INSERT INTO corner VALUES (?, ?)", [(1, "Fhigh"), (2, "Flow")]
            )
            connection.execute("INSERT INTO test VALUES (?, ?)", (1, "vco_nominal"))
            connection.executemany(
                "INSERT INTO testStatus VALUES (?, ?, ?)", [(1, 1, 3), (2, 1, 3)]
            )
            connection.commit()
        finally:
            connection.close()

        for point_id in (1, 2):
            psf = os.path.join(raw_dir, str(point_id), "vco_nominal", "psf")
            os.makedirs(psf)
            with io.open(os.path.join(psf, ".simDone"), "w", encoding="utf-8"):
                pass
            with io.open(os.path.join(psf, "spectre.out"), "w", encoding="utf-8") as stream:
                if point_id == 1:
                    stream.write("spectre completes with 0 errors, 2 warnings, and 1 notice.\n")
                else:
                    stream.write("FATAL (SPECTRE-1): synthetic failure\n")
                    stream.write("spectre completes with 0 errors, 0 warnings, and 0 notices.\n")
        return rdb_path, raw_dir

    def test_discovers_file_rdb_and_inspects_exact_completion(self):
        with tempfile.TemporaryDirectory() as root:
            rdb_path, raw_dir = self._make_rdb(root)
            os.makedirs(os.path.join(root, "maestro"))
            os.makedirs(os.path.join(root, "psf"))

            discovered = discover_result_directories({"roots": [root]})
            rdb_findings = [
                item for item in discovered["findings"] if item["kind"] == "rdb"
            ]
            self.assertEqual([rdb_path], [item["path"] for item in rdb_findings])
            self.assertIn("sqlite-header", rdb_findings[0]["markers"])

            inspected = inspect_rdb(rdb_path)
            self.assertTrue(inspected["read_only"])
            self.assertEqual([raw_dir], inspected["rawDir"])
            self.assertEqual(2, len(inspected["points"]))
            self.assertEqual(1, len(inspected["tests"]))
            self.assertEqual(2, len(inspected["corners"]))
            by_point = dict((run["pointID"], run) for run in inspected["runs"])
            self.assertTrue(by_point[1]["complete"])
            self.assertTrue(by_point[1]["zero_errors"])
            self.assertTrue(by_point[1]["spectreOut"]["zero_errors"])
            self.assertFalse(by_point[2]["complete"])
            self.assertFalse(by_point[2]["zero_errors"])
            self.assertFalse(by_point[2]["spectreOut"]["zero_errors"])
            self.assertTrue(by_point[2]["spectreOut"]["fatal_or_error"])
            self.assertEqual(
                os.path.join(raw_dir, "1", "vco_nominal", "psf", ".simDone"),
                by_point[1]["simDone"]["path"],
            )


class AdapterTests(unittest.TestCase):
    def test_template_whitelist_matches_execution_contract(self):
        for name in (
            "run_simulation",
            "netlist_dir",
            "vco_result",
            "replica_workspace",
            "result_dir",
            "test",
        ):
            self.assertIn(name, TEMPLATE_VARIABLE_WHITELIST)
        rendered = render_template(
            "{run_simulation} {netlist_dir}",
            {"run_simulation": "/fake/runSimulation", "netlist_dir": "/tmp/netlist"},
        )
        self.assertEqual("/fake/runSimulation /tmp/netlist", rendered)
        with self.assertRaises(TemplateValidationError):
            render_template("{run_dir.__class__}", {"run_dir": "/tmp"})
        with self.assertRaises(TemplateValidationError):
            render_template("{untrusted}", {})

    def test_adapters_do_not_invoke_runner_in_dry_run(self):
        calls = []

        def runner(*args):
            calls.append(args)
            return {"returncode": 0, "stdout": "", "stderr": ""}

        run_sim = RunSimulationAdapter(
            {
                "kind": "runSimulation",
                "command": ["{run_simulation}", "--netlist", "{netlist_dir}"],
            },
            runner=runner,
        )
        result = run_sim.submit(
            {"run_simulation": "/fake/runSimulation", "netlist_dir": "/tmp/a b"},
            dry_run=True,
        )
        self.assertEqual("dry-run", result["status"])
        self.assertEqual("/tmp/a b", result["command"][-1])

        shell = CustomShellAdapter(
            {"kind": "custom_shell", "command": "echo {replica_workspace}"},
            runner=runner,
        )
        shell_result = shell.submit(
            {"replica_workspace": "/tmp/a b; unsafe"}, dry_run=True
        )
        self.assertEqual("dry-run", shell_result["status"])
        self.assertIn("'/tmp/a b; unsafe'", shell_result["command"][-1])
        self.assertEqual([], calls)


class ToolServiceTests(unittest.TestCase):
    def test_config_and_synchronous_job_api(self):
        with tempfile.TemporaryDirectory() as workspace:
            runner_calls = []

            def runner(*args):
                runner_calls.append(args)
                return {"returncode": 0, "stdout": "", "stderr": ""}

            service = ToolService(workspace, command_runner=runner)
            self.assertEqual("ok", service.health()["status"])
            first_save = service.save_config(
                {"project_root": workspace, "execution": {"dry_run": True}}
            )
            self.assertTrue(first_save["ok"])
            second_save = service.save_config(
                {"project_root": workspace, "execution": {"dry_run": True}}
            )
            self.assertTrue(os.path.isfile(second_save["backup"]))
            self.assertEqual(workspace, service.load_config()["project_root"])
            self.assertTrue(service.path_is_allowed(os.path.join(workspace, "x.txt")))

            job_id = service.start_run(
                {
                    "dry_run": True,
                    "testbenches": [
                        {
                            "name": "vco",
                            "run_simulation": "/fake/runSimulation",
                            "netlist_dir": "/tmp/vco netlist",
                            "adapter": {
                                "kind": "runSimulation",
                                "command": ["{run_simulation}", "{netlist_dir}"],
                            },
                        },
                        {
                            "name": "replica",
                            "adapter": {
                                "kind": "custom_shell",
                                "command": "echo {replica_workspace}",
                            },
                        },
                    ],
                    "replica_workspace": "/tmp/replica workspace",
                    "exchange_adapter": {
                        "kind": "custom_shell",
                        "command": "copy {vco_result} {replica_workspace}",
                    },
                    "vco_result": "/tmp/vco result",
                }
            )
            job = service.get_job(job_id)
            self.assertEqual("succeeded", job["status"])
            self.assertEqual(100.0, job["progress"])
            self.assertTrue(job["result_available"])
            self.assertEqual(100.0, job["events"][-1]["progress"])
            self.assertEqual(
                sorted(event["progress"] for event in job["events"]),
                [event["progress"] for event in job["events"]],
            )
            result = service.get_result(job_id)
            self.assertEqual([], result["summary"])
            self.assertEqual({}, result["points"])
            self.assertEqual(4, len(result["submissions"]))
            self.assertEqual(
                ["vco", "exchange", "replica", "extract"],
                [item["testbench"] for item in result["submissions"]],
            )
            self.assertIn("exchange", [event["stage"] for event in job["events"]])
            self.assertIn("extract", [event["stage"] for event in job["events"]])
            self.assertEqual([], runner_calls)
            self.assertTrue(glob.glob(os.path.join(workspace, "config.json.bak-*")))

    def test_service_imports_synthetic_fixture(self):
        with tempfile.TemporaryDirectory() as workspace:
            fixture = write_closure_bundle(
                os.path.join(workspace, "synthetic_fixture")
            )
            service = ToolService(workspace)
            result = service.import_bundle({"path": fixture})
            self.assertEqual(6, len(result["points"]))
            imported = service.import_as_job({"path": fixture})
            self.assertTrue(imported["ok"])
            self.assertEqual(6, imported["points"])
            job = service.get_job(imported["job_id"])
            self.assertEqual("succeeded", job["status"])
            self.assertEqual(6, len(service.get_result(imported["job_id"])["points"]))
            restarted = ToolService(workspace)
            self.assertEqual("succeeded", restarted.get_job(imported["job_id"])["status"])
            self.assertEqual(6, len(restarted.get_result(imported["job_id"])["points"]))


if __name__ == "__main__":
    unittest.main()
