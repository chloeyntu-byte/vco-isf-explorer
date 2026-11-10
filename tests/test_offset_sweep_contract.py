"""Fixed workpoint evidence must survive frequency aggregation and relocation."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from isf_tool.core.errors import ValidationError
from isf_tool.core.models import empty_point_result
from isf_tool.core.offset_sweep import aggregate_results, import_offset_sweep
from isf_tool.core.orchestrator import _tree_identity
from isf_tool.core.service import ToolService
from synthetic_fixture import make_canonical, write_canonical_bundle


def scalar(hz, modern=True):
    row = {"point": "same-point", "operating_point_id": "same-point",
           "corner": "Fhigh", "offset_Hz": hz, "f0_Hz": 5.89073e9,
           "operating_parameters": {"SW": 0}}
    tables = empty_point_result()
    tables["summary"] = [copy.deepcopy(row)]
    result = {"summary": [row], "points": {"same-point": tables}, "run_kind": "replay"}
    if modern:
        result["dual_tb"] = {"metadata": {"operating_points": {"same-point": {
            "params": {"SW": 0}, "corner": "Fhigh", "test": "vco_nominal",
            "environment": {"settings": {"temp": "27"}, "model_sections": {"model.scs": ["tt"]}},
            "effective_netlist_sha256": "same-fixed-netlist", "sha256": "runtime-one",
            "rdb": "/old/location/history.rdb"}}}}
    return result


def rows(result):
    return result["summary"] + result["points"]["same-point"]["summary"]


def combine(first, second):
    return aggregate_results([{"offset_hz": 1000, "result": first},
                              {"offset_hz": 10000, "result": second}], [1000, 10000], True)


class FixedOperatingContractTests(unittest.TestCase):
    def test_same_name_different_effective_parameter_is_rejected(self):
        first, second = scalar(1000), scalar(10000)
        for row in rows(second):
            row["operating_parameters"] = {"SW": 1}
        second["dual_tb"]["metadata"]["operating_points"]["same-point"]["params"] = {"SW": 1}
        with self.assertRaises(ValidationError):
            combine(first, second)

    def test_fixed_environment_test_and_source_netlist_changes_are_rejected(self):
        for key, changed in (("environment", {"settings": {"temp": "125"}}),
                             ("test", "other_test"), ("effective_netlist_sha256", "other-netlist")):
            with self.subTest(key=key):
                second = scalar(10000)
                second["dual_tb"]["metadata"]["operating_points"]["same-point"][key] = changed
                with self.assertRaises(ValidationError):
                    combine(scalar(1000), second)

    def test_same_name_different_corner_is_rejected_without_modern_metadata(self):
        second = scalar(10000, modern=False)
        for row in rows(second):
            row["corner"] = "Flow"
        with self.assertRaises(ValidationError):
            combine(scalar(1000, modern=False), second)

    def test_summary_parameters_are_checked_without_modern_metadata(self):
        second = scalar(10000, modern=False)
        for row in rows(second):
            row["operating_parameters"] = {"SW": 1}
        with self.assertRaises(ValidationError):
            combine(scalar(1000, modern=False), second)

    def test_one_child_losing_metadata_is_rejected(self):
        with self.assertRaises(ValidationError):
            combine(scalar(1000), scalar(10000, modern=False))

    def test_one_child_losing_individual_fixed_field_is_rejected(self):
        second = scalar(10000)
        del second["dual_tb"]["metadata"]["operating_points"]["same-point"]["environment"]
        with self.assertRaises(ValidationError):
            combine(scalar(1000), second)

    def test_one_child_losing_summary_parameters_is_rejected(self):
        second = scalar(10000, modern=False)
        for row in rows(second):
            del row["operating_parameters"]
        with self.assertRaises(ValidationError):
            combine(scalar(1000, modern=False), second)

    def test_legacy_without_parameter_evidence_on_both_sides_is_compatible(self):
        first, second = scalar(1000, modern=False), scalar(10000, modern=False)
        for result in (first, second):
            for row in rows(result):
                del row["operating_parameters"]
        result = combine(first, second)
        self.assertEqual(result["offset_sweep"]["operating_point_count"], 1)
        self.assertFalse(result["signoff"])

    def test_runtime_location_and_f0_roundoff_are_not_workpoint_identity(self):
        second = scalar(10000)
        record = second["dual_tb"]["metadata"]["operating_points"]["same-point"]
        record.update(sha256="runtime-two", rdb="/relocated/history.rdb", offset_hz=10000)
        for row in rows(second):
            row["f0_Hz"] += 1e-3
        self.assertTrue(combine(scalar(1000), second)["offset_sweep"]["complete"])

    def test_summary_and_metadata_must_agree(self):
        first, second = scalar(1000), scalar(10000)
        for result in (first, second):
            for row in rows(result):
                row["operating_parameters"] = {"SW": 1}
        with self.assertRaises(ValidationError):
            combine(first, second)

    def test_global_and_per_point_summary_must_agree(self):
        second = scalar(10000)
        second["points"]["same-point"]["summary"][0]["corner"] = "Flow"
        with self.assertRaises(ValidationError):
            combine(scalar(1000), second)

    def test_all_plans_keep_plan_mode_and_each_child_plan(self):
        entries = [{"offset_hz": hz, "job_id": str(hz), "result": {
            "run_kind": "plan", "dry_run": True, "summary": [], "points": {},
            "submissions": [{"dry_run": True, "name": "vco"}],
            "extraction": {"status": "planned"}}} for hz in (1000, 10000)]
        result = aggregate_results(entries, [1000, 10000], True)
        self.assertEqual(result["run_kind"], "plan")
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["signoff"])
        self.assertEqual([p["offset_hz"] for p in result["offset_sweep"]["plans"]], [1000, 10000])
        self.assertEqual(result["offset_sweep"]["plans"][0]["extraction"]["status"], "planned")

    def test_plan_and_numeric_results_cannot_mix(self):
        entries = [{"offset_hz": 1000, "result": {"run_kind": "plan"}},
                   {"offset_hz": 10000, "result": {"run_kind": "replay"}}]
        with self.assertRaises(ValidationError):
            aggregate_results(entries, [1000, 10000], True)


class SweepHomePathTests(unittest.TestCase):
    def test_direct_and_service_import_expand_home_consistently(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            manifest = {"schema_version": 1, "complete": True, "offsets_hz": [1000, 10000], "children": []}
            for hz in manifest["offsets_hz"]:
                data = make_canonical(signoff=False, point_count=1)
                data["metadata"]["offset_hz"] = hz
                for row in data["vco_results"]:
                    row["offset_Hz"] = hz
                relative = "offsets/%dHz" % hz
                path = write_canonical_bundle(bundle / relative, canonical=data)
                manifest["children"].append({"offset_hz": hz, "bundle_path": relative,
                    "bundle_tree_sha256": _tree_identity(path)["sha256"]})
            (bundle / "offset_sweep.json").write_text(json.dumps(manifest))
            with mock.patch.dict(os.environ, {"HOME": temporary}):
                direct = import_offset_sweep({"path": "~/bundle"})
                through_service = ToolService().import_bundle({"path": "~/bundle"})
            self.assertEqual(direct, through_service)
            self.assertEqual(len(direct["summary"]), 2)


if __name__ == "__main__":
    unittest.main()
