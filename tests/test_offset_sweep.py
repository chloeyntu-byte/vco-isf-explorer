import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from isf_tool.core.errors import ValidationError
from isf_tool.core.models import empty_point_result
from isf_tool.core.offsets import expand_offsets, frequency_hz, normalize_offset_analysis
from isf_tool.core.offset_sweep import aggregate_results, is_sweep, scalar_config
from isf_tool.core.service import ToolService


def fixture(hz, points=("cornerA", "cornerB")):
    result = {"summary": [], "points": {}}
    for name in points:
        row = {"point": name, "corner": name, "offset_Hz": hz,
               "dynamic_dBc_Hz": -100.0, "spectre_fn_dBc_Hz": -101.0}
        result["summary"].append(row)
        tables = empty_point_result()
        tables["summary"] = [dict(row)]
        tables["harmonics"] = [{"point": name, "k": 0, "Q_real_V_rtHz": 1e-9}]
        result["points"][name] = tables
    return result


class OffsetInputTests(unittest.TestCase):
    def test_service_preview_matches_authoritative_expansion(self):
        analysis = {"offsets": {"mode": "log", "start": "1k", "stop": "100M", "points_per_decade": 1}}
        preview = ToolService().preview_offsets({"analysis": analysis})
        self.assertEqual(preview["offsets_hz"], expand_offsets(analysis))
        self.assertEqual(preview["count"], 6)
        self.assertEqual(preview["endpoint_policy"], "include_stop")

    def test_explicit_engineering_units(self):
        values = expand_offsets({"offsets": {"mode": "list", "values": "1k,10k,1M,100M"}})
        self.assertEqual(values, [1e3, 1e4, 1e6, 1e8])

    def test_sort_deduplicate_not_interpolate(self):
        self.assertEqual(expand_offsets({"offsets": {"mode": "list", "values": "10k 1k 1e3"}}), [1e3, 1e4])

    def test_log_decades_include_both_ends(self):
        self.assertEqual(expand_offsets({"offsets": {"mode": "log", "start": "1k", "stop": "100M",
                                                  "points_per_decade": 1}}), [1e3, 1e4, 1e5, 1e6, 1e7, 1e8])

    def test_nondecade_end_is_exact(self):
        values = expand_offsets({"offsets": {"mode": "log", "start": "1k", "stop": "25k", "points_per_decade": 2}})
        self.assertEqual(values[0], 1000)
        self.assertEqual(values[-1], 25000)
        self.assertAlmostEqual(values[1], math.sqrt(10) * 1000)

    def test_linear_step_appends_end(self):
        self.assertEqual(expand_offsets({"offsets": {"mode": "linear", "start": 1, "stop": 10, "step": 4}}), [1, 5, 9, 10])

    def test_intervals_are_not_points(self):
        self.assertEqual(expand_offsets({"offsets": {"mode": "linear", "start": 1, "stop": 10, "intervals": 3}}), [1, 4, 7, 10])

    def test_invalid_values_and_units(self):
        for value in (None, True, 0, -1, "nan", "inf", "1m", "1/2", "1e999"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                frequency_hz(value)

    def test_empty_items_are_errors(self):
        for values in ("", "1k,,10k", "1k,", ",1k"):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                expand_offsets({"offsets": {"mode": "list", "values": values}})

    def test_point_limit_before_large_allocation(self):
        for spec in ({"mode": "linear", "start": 1, "stop": 1e12, "step": 1},
                     {"mode": "log", "start": 1, "stop": 1e300, "points_per_decade": 100},
                     {"mode": "linear", "start": 1, "stop": 10, "intervals": 256}):
            with self.subTest(spec=spec), self.assertRaises(ValidationError):
                expand_offsets({"offsets": spec})

    def test_legacy_single_and_untrusted_derived_values(self):
        legacy = {"offset": {"value": 10, "unit": "kHz", "hz": 10000}}
        self.assertEqual(normalize_offset_analysis(legacy), legacy)
        self.assertFalse(is_sweep({}))
        self.assertEqual(expand_offsets({"offsets": {"mode": "list", "values": "1k", "values_hz": [99999]}}), [1000])


class OffsetAggregationTests(unittest.TestCase):
    def test_two_corner_two_offset_not_one_dimension(self):
        result = aggregate_results([{"offset_hz": hz, "result": fixture(hz)} for hz in (1000, 10000)], [1000, 10000], True)
        self.assertEqual(len(result["points"]), 4)
        self.assertEqual({r["operating_point_id"] for r in result["summary"]}, {"cornerA", "cornerB"})
        self.assertEqual({r["offset_Hz"] for r in result["summary"]}, {1000, 10000})
        self.assertEqual(result["offset_sweep"]["operating_point_count"], 2)
        self.assertFalse(result["signoff"])
        for tables in result["points"].values():
            self.assertEqual(len(tables["harmonics"]), 1)

    def test_offset_mismatch_and_missing_corner_fail(self):
        with self.assertRaises(ValidationError):
            aggregate_results([{"offset_hz": 1000, "result": fixture(10000)}], [1000])
        with self.assertRaises(ValidationError):
            aggregate_results([{"offset_hz": 1000, "result": fixture(1000)},
                               {"offset_hz": 10000, "result": fixture(10000, ("cornerA",))}], [1000, 10000])

    def test_single_child_config_never_retains_sweep(self):
        value = scalar_config({"analysis": {"offsets": {"mode": "list", "values": "1k,10k"}},
                               "execution": {"allow_execute": True}}, 10000)
        self.assertFalse(is_sweep(value))
        self.assertTrue(value["execution"]["allow_execute"])


class OffsetServiceTests(unittest.TestCase):
    def test_consecutive_restarts_keep_partial_result_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "jobs" / "restart-test"
            directory.mkdir(parents=True)
            partial = aggregate_results(
                [{"offset_hz": 1000, "result": fixture(1000)}], [1000, 10000])
            (directory / "result.json").write_text(json.dumps(partial))
            (directory / "job.json").write_text(json.dumps({
                "id": "restart-test", "events": [], "status": "running", "result_available": True}))
            for _ in range(3):
                service = ToolService(workspace=tmp)
                self.assertTrue(service.get_job("restart-test")["result_available"])
                self.assertEqual(service.get_result("restart-test")["offset_sweep"]["completed_offsets_hz"], [1000])

    def test_resume_reuses_completed_offset_and_retries_only_failure(self):
        config = {"source_mode": "auto", "analysis": {"offsets": {"mode": "list", "values": "1k,10k"}},
                  "execution": {"dry_run": True, "background": False}}
        seen = []

        def run(orchestrator, job_id, payload, emit):
            hz = orchestrator.config["analysis"]["offset"]["hz"]
            seen.append(hz)
            (Path(payload["run_root"]) / ".isf_run_state.json").write_text("{}")
            if hz == 10000 and seen.count(hz) == 1:
                raise ValidationError("synthetic interruption")
            return fixture(hz)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ToolService, "preflight", return_value={"ok": True}), \
                mock.patch("isf_tool.core.service._runtime_identity_errors", return_value={}), \
                mock.patch("isf_tool.core.service.DualTBOrchestrator.run", autospec=True, side_effect=run):
            service = ToolService(workspace=tmp)
            job_id = service.start_run({"config": config, "background": False})
            self.assertEqual(service.get_job(job_id)["status"], "failed")
            service.resume_job(job_id, {"background": False})
            self.assertEqual(service.get_job(job_id)["status"], "succeeded", service.get_job(job_id).get("error"))
            self.assertEqual(seen, [1000, 10000, 10000])
            self.assertTrue(service.get_result(job_id)["offset_sweep"]["complete"])

    def test_stop_finishes_current_offset_and_preserves_checkpoint(self):
        config = {"source_mode": "auto", "analysis": {"offsets": {"mode": "list", "values": "1k,10k"}},
                  "execution": {"dry_run": True, "background": False}}
        seen = []

        def run(orchestrator, job_id, payload, emit):
            hz = orchestrator.config["analysis"]["offset"]["hz"]
            seen.append(hz)
            parent_id = next(iter(service._jobs))
            service.stop_after_current_offset(parent_id)
            return fixture(hz)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ToolService, "preflight", return_value={"ok": True}), \
                mock.patch("isf_tool.core.service.DualTBOrchestrator.run", autospec=True, side_effect=run):
            service = ToolService(workspace=tmp)
            job_id = service.start_run({"config": config, "background": False})
            job = service.get_job(job_id)
            self.assertEqual(job["status"], "interrupted", job.get("error"))
            self.assertTrue(job["resume_available"])
            self.assertTrue(service.list_jobs()["jobs"][0]["resume_available"])
            self.assertEqual(seen, [1000])
            self.assertEqual(service.get_result(job_id)["offset_sweep"]["completed_offsets_hz"], [1000])

    def test_resume_preflight_failure_does_not_erase_partial_result(self):
        config = {"source_mode": "auto", "analysis": {"offsets": {"mode": "list", "values": "1k,10k"}},
                  "execution": {"dry_run": True, "background": False}}

        def run(orchestrator, job_id, payload, emit):
            hz = orchestrator.config["analysis"]["offset"]["hz"]
            (Path(payload["run_root"]) / ".isf_run_state.json").write_text("{}")
            if hz == 10000:
                raise ValidationError("synthetic failure")
            return fixture(hz)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ToolService, "preflight", return_value={"ok": True}) as preflight, \
                mock.patch("isf_tool.core.service._runtime_identity_errors", return_value={}), \
                mock.patch("isf_tool.core.service.DualTBOrchestrator.run", autospec=True, side_effect=run):
            service = ToolService(workspace=tmp)
            job_id = service.start_run({"config": config, "background": False})
            preflight.return_value = {"ok": False, "checks": []}
            service.resume_job(job_id, {"background": False})
            self.assertEqual(service.get_job(job_id)["status"], "failed")
            self.assertTrue(service.get_job(job_id)["result_available"])
            restored = ToolService(workspace=tmp)
            self.assertEqual(restored.get_result(job_id)["offset_sweep"]["completed_offsets_hz"], [1000])

    def test_scalar_orchestrator_reused_once_per_offset(self):
        config = {"source_mode": "auto", "analysis": {"offsets": {"mode": "list", "values": "10k,1k"}},
                  "execution": {"dry_run": True, "background": False}}
        seen = []

        def run(orchestrator, job_id, payload, emit):
            hz = orchestrator.config["analysis"]["offset"]["hz"]
            seen.append((hz, payload["run_root"], job_id))
            return fixture(hz)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ToolService, "preflight", return_value={"ok": True}), \
                mock.patch("isf_tool.core.service.DualTBOrchestrator.run", autospec=True, side_effect=run):
            service = ToolService(workspace=tmp)
            job_id = service.start_run({"config": config, "background": False})
            job = service.get_job(job_id)
            self.assertEqual(job["status"], "succeeded", job.get("error"))
            result = service.get_result(job_id)
            self.assertEqual(len(result["summary"]), 4)
            self.assertEqual([row[0] for row in seen], [1000, 10000])
            self.assertEqual(len({row[1] for row in seen}), 2)
            self.assertEqual(len({row[2] for row in seen}), 2)
            manifest = json.loads((Path(tmp) / "jobs" / job_id / "simulation/offset_sweep.json").read_text())
            self.assertTrue(manifest["complete"])

    def test_child_failure_preserves_completed_partial_results(self):
        config = {"source_mode": "auto", "analysis": {"offsets": {"mode": "list", "values": "1k,10k"}},
                  "execution": {"dry_run": True, "background": False}}

        def run(orchestrator, job_id, payload, emit):
            hz = orchestrator.config["analysis"]["offset"]["hz"]
            if hz == 10000:
                raise ValidationError("synthetic child failure")
            return fixture(hz)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ToolService, "preflight", return_value={"ok": True}), \
                mock.patch("isf_tool.core.service.DualTBOrchestrator.run", autospec=True, side_effect=run):
            service = ToolService(workspace=tmp)
            job_id = service.start_run({"config": config, "background": False})
            job = service.get_job(job_id)
            self.assertEqual(job["status"], "failed")
            self.assertTrue(job["result_available"])
            self.assertTrue(job["resume_available"])
            result = service.get_result(job_id)
            self.assertFalse(result["offset_sweep"]["complete"])
            self.assertEqual(result["offset_sweep"]["completed_offsets_hz"], [1000])


if __name__ == "__main__":
    unittest.main()
