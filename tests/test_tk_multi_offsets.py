"""No-EDA regressions for the native multi-offset and result controls."""

import copy
import unittest
from unittest import mock

from isf_tool.tk import app
from isf_tool.tk.charts import chart_series, line_segments
from isf_tool.tk.controller import IsfController
from isf_tool.tk.viewmodel import (
    active_mapping_rows, build_result_view, flatten_config, merge_config,
    offset_preview, result_sample_choices,
)


class TkMultiOffsetTests(unittest.TestCase):
    def test_legacy_single_roundtrip_does_not_create_sweep(self):
        config = {"analysis": {"offset": {"value": 10, "unit": "kHz", "hz": 10000}}}
        flat = flatten_config(config)
        self.assertEqual("single", flat["offset_mode"])
        self.assertNotIn("offsets", merge_config(config, flat)["analysis"])
        self.assertEqual([10000], offset_preview(flat)["values_hz"])

    def test_recipe_roundtrip_preserves_modes_and_first_legacy_offset(self):
        cases = [
            ({"mode": "list", "values": "1M,1k,10k,1k"}, [1000, 10000, 1000000]),
            ({"mode": "linear", "start": "1k", "stop": "10k", "spacing": "intervals", "intervals": "3"}, [1000, 4000, 7000, 10000]),
            ({"mode": "log", "start": "1k", "stop": "100M", "points_per_decade": "1"}, [1000, 10000, 100000, 1000000, 10000000, 100000000]),
        ]
        for spec, expected in cases:
            with self.subTest(mode=spec["mode"]):
                raw = {"analysis": {"offset": {"value": 10, "unit": "kHz"}, "offsets": spec}, "vendor": {"keep": 1}}
                fields = flatten_config(raw)
                preview = offset_preview(fields, 2)
                self.assertEqual(expected, preview["values_hz"])
                self.assertEqual(2 * len(expected), preview["total_samples"])
                merged = merge_config(raw, fields)
                self.assertEqual(expected[0], merged["analysis"]["offset"]["hz"])
                self.assertEqual(spec["mode"], merged["analysis"]["offsets"]["mode"])
                self.assertEqual({"keep": 1}, merged["vendor"])

    def test_step_appends_endpoint_and_preview_explains_extra_point(self):
        fields = {"offset_mode": "linear", "offset_start": "1k", "offset_stop": "10k", "offset_spacing": "step", "offset_step": "4k"}
        preview = offset_preview(fields)
        self.assertEqual([1000, 5000, 9000, 10000], preview["values_hz"])
        self.assertTrue(preview["endpoint_appended"])
        self.assertIsNone(preview["total_samples"])

    def test_multioffset_does_not_read_hidden_single_field_and_rejects_huge_sweep(self):
        fields = {"offset_mode": "list", "offset_values": "1k,10k", "offset_value": "not-used", "offset_unit": "Hz"}
        self.assertEqual(1000, merge_config({}, fields)["analysis"]["offset"]["hz"])
        with self.assertRaises(Exception):
            offset_preview({"offset_mode": "linear", "offset_start": "1", "offset_stop": "1000000", "offset_spacing": "step", "offset_step": "1"})

    def test_actual_mapping_default_hides_reference_but_keeps_unresolved_overrides(self):
        report = {"roles": {"vco": {"mapping_fields": [
            {"key": "transfer_output", "active": True, "group": "signal"},
            {"key": "noise_output_at_offset", "active": False, "group": "reference"},
            {"key": "current_trace", "active": True, "group": "execution_detail"},
            {"key": "old_override", "active": False, "status": "invalid"},
        ]}}}
        self.assertEqual(["transfer_output", "old_override"], [row["key"] for row in active_mapping_rows(report)])
        self.assertEqual(4, len(active_mapping_rows(report, True)))

    def test_result_identity_and_q_canonical_aliases_are_preserved(self):
        result = {"summary": [], "points": {}}
        for op in ("same-corner-SW0", "same-corner-SW1"):
            for hz in (1000, 10000):
                point = f"opaque-{op}-{hz}"
                summary = {"point": point, "operating_point_id": op, "corner": "tt", "offset_Hz": hz,
                           "carrier_peak_V": 1, "dynamic_pm_asd_V_rtHz": 1e-6, "spectre_fn_pm_asd_V_rtHz": 2e-6}
                result["summary"].append(summary)
                result["points"][point] = {"harmonics": [{"k": 0, "Q_real_V_rtHz": 1e-6, "Q_imag_V_rtHz": -2e-6, "cum_real_V_rtHz": 1e-6, "cum_imag_V_rtHz": -2e-6}]}
        original = copy.deepcopy(result)
        choices = result_sample_choices(result, "same-corner-SW1")
        self.assertEqual([1000, 10000], [choice["offset_hz"] for choice in choices])
        self.assertTrue(all("SW1" in choice["point"] for choice in choices))
        view = build_result_view(result, point=choices[1]["point"])
        self.assertEqual(10000, view["metrics"]["offset_hz"])
        self.assertEqual(-2e-6, view["harmonic_rows"][0]["q_imag"])
        self.assertIsInstance(view["summary_rows"][0]["calculated_dbc"], float)
        self.assertIsInstance(view["harmonic_rows"][0]["harmonic_dbc"], float)
        self.assertEqual(original, result)

    def test_log_chart_keeps_missing_samples_as_gaps_and_never_interpolates(self):
        raw = [{"name": "reference", "points": [[1000, -100], [10000, None], [100000, -130], [0, -150]]}]
        result = chart_series(raw, log_x=True)
        self.assertEqual([(3.0, -100.0), None, (5.0, -130.0), None], result[0]["points"])
        self.assertEqual([[(3.0, -100.0)], [(5.0, -130.0)]], line_segments(result[0]["points"]))
        self.assertEqual(1000, raw[0]["points"][0][0])

    def test_stop_facade_calls_cooperative_api_only(self):
        service = mock.Mock()
        service.stop_after_current_offset.return_value = {"ok": True}
        controller = IsfController("/tmp/tk-offset-test", service=service)
        self.assertEqual({"ok": True}, controller.stop_after_current_offset("job-1"))
        service.stop_after_current_offset.assert_called_once_with("job-1")
        service.resume_job.assert_not_called()

    def test_resume_invalidates_previously_loaded_terminal_partial_result(self):
        instance = app.IsfApplication.__new__(app.IsfApplication)
        instance.root = mock.Mock()
        instance._close_check_pending = False
        instance._job_command_pending = False
        instance.current_job_id = "interrupted-a"
        instance._result_terminal_loaded = True
        instance.job_status_var = mock.Mock()
        instance._schedule_poll = mock.Mock()
        instance.controller = mock.Mock()
        instance.controller.jobs.return_value = {"jobs": []}

        def submit(_label, function, success, **_kwargs):
            success(function())

        instance._submit = submit
        with mock.patch.object(app.messagebox, "askyesno", return_value=True):
            instance._resume_job()
        instance.controller.resume.assert_called_once_with("interrupted-a")
        self.assertFalse(instance._result_terminal_loaded)
        self.assertFalse(instance._job_command_pending)
        instance._schedule_poll.assert_called_once_with(100)


if __name__ == "__main__":
    unittest.main()
