"""Temporary Canvas only: real pointer geometry, no service or simulator."""

import os
import tkinter as tk
import unittest
from types import SimpleNamespace

from isf_tool.tk.charts import LineChart


@unittest.skipUnless(os.environ.get("ISF_GUI_TEST") == "1", "requires explicitly selected GUI display")
class InteractiveChartsTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.lower()
        self.root.geometry("900x470")
        self.chart = LineChart(self.root, width=900, height=450)
        self.chart.pack(fill="both", expand=True)
        self.chart.set_axes(x_label="频偏 (Hz)", y_label="相噪 (dBc/Hz)", log_x=True)
        self.rows = [{"id": "flicker", "name": "flicker", "points": [[1000, -80], [10000, None], [100000, -120]],
                      "samples": [{"point": "tt@1k"}, {"point": "tt@10k"}, {"point": "tt@100k"}]},
                     {"id": "thermal", "name": "thermal", "points": [[1000, -95], [10000, -115], [100000, -135]],
                      "samples": [{"point": "tt@1k"}, {"point": "tt@10k"}, {"point": "tt@100k"}]}]
        self.chart.set_series(self.rows)
        self.root.update_idletasks()

    def tearDown(self):
        self.root.destroy()

    def test_series_and_raw_samples_are_preserved_and_missing_values_break_lines(self):
        self.assertEqual("flicker", self.chart._series[0]["id"])
        self.assertEqual(self.rows[0]["samples"], self.chart._series[0]["samples"])
        self.assertEqual(self.rows[0]["points"], self.chart._series[0]["points"])
        self.assertEqual(5, len(self.chart._hit_samples))
        # The gap has two isolated markers, not a line spanning absent data.
        self.assertEqual(["oval", "oval"], [self.chart.type(item) for item in self.chart.find_withtag("curve-0")])
        self.assertIn("line", [self.chart.type(item) for item in self.chart.find_withtag("curve-1")])

    def test_hover_uses_actual_hz_not_log_coordinate_and_keeps_sample_identity(self):
        target = next(row for row in self.chart._hit_samples if row["series_id"] == "flicker")
        self.chart._hover(SimpleNamespace(x=target["canvas_x"], y=target["canvas_y"]))
        self.assertEqual("tt@1k", self.chart.hover_sample["sample"]["point"])
        labels = [self.chart.itemcget(item, "text") for item in self.chart.find_withtag("hover") if self.chart.type(item) == "text"]
        self.assertIn("1000 Hz", labels[0])
        self.assertIn("-80 dBc/Hz", labels[0])
        self.chart._clear_hover()
        self.assertIsNone(self.chart.hover_sample)

    def test_legend_and_curve_selection_highlight_without_changing_data(self):
        self.assertTrue(self.chart.tag_bind("legend-1", "<Button-1>"))
        self.assertTrue(self.chart.tag_bind("curve-1", "<Button-1>"))
        self.chart._toggle_series("thermal")
        self.root.update_idletasks()
        self.assertEqual("thermal", self.chart.selected_series_id)
        line = next(item for item in self.chart.find_withtag("curve-1") if self.chart.type(item) == "line")
        self.assertEqual(3.5, float(self.chart.itemcget(line, "width")))
        self.assertEqual(self.rows[1]["points"], self.chart._series[1]["points"])
        self.chart.set_series(self.rows)
        self.assertEqual("thermal", self.chart.selected_series_id)
        self.chart._toggle_series("thermal")
        self.assertIsNone(self.chart.selected_series_id)
        self.chart.clear()
        self.assertEqual([], self.chart._hit_samples)

    def test_x_window_clips_strokes_and_uses_only_real_samples_for_hover(self):
        self.chart.set_x_limits((3.5, 4.5))
        self.root.update_idletasks()
        self.assertEqual([(10000., -115.)], [row["point"] for row in self.chart._hit_samples])
        left, _, right, _ = self.chart._plot
        for item in self.chart.find_withtag("curve-1"):
            if self.chart.type(item) == "line":
                self.assertTrue(all(left - 1e-6 <= x <= right + 1e-6 for x in self.chart.coords(item)[::2]))
        self.chart.set_hidden(["thermal"])
        self.root.update_idletasks()
        self.assertFalse(self.chart._hit_samples)
        self.assertEqual(self.rows[1]["points"], self.chart._series[1]["points"])

    def many_series(self):
        return [{"id": f"thermal-band-{index}",
                 "name": f"Thermal 独立输入频带 m={index} · 独立源功率和",
                 "points": [[1000, -80 - index], [10000, -100 - index], [100000, -120 - index]],
                 "samples": [{"point": "tt@1k"}, {"point": "tt@10k"}, {"point": "tt@100k"}]}
                for index in range(45)]

    def test_45_real_canvas_series_keep_plot_and_all_samples_at_regular_and_compact_sizes(self):
        rows = self.many_series()
        for width, height in ((900, 450), (420, 260)):
            with self.subTest(width=width, height=height):
                self.root.geometry(f"{width}x{height}")
                self.chart.configure(width=width, height=height)
                self.chart.set_series(rows)
                self.root.update_idletasks()
                self.assertEqual(135, len(self.chart._hit_samples))
                rectangle = self.chart.coords(self.chart.find_withtag("plot-frame")[0])
                self.assertGreater(rectangle[3] - rectangle[1], 80)
                self.assertEqual(46, len(self.chart._series_selector["values"]))
                self.assertIn("图例已折叠", self.chart._series_selector["values"][0])
                self.assertTrue(self.chart.find_withtag("legend-overflow"))
                legend_rows = set()
                for index in range(45):
                    self.assertTrue(self.chart.find_withtag(f"curve-{index}"))
                    self.assertTrue(self.chart.tag_bind(f"curve-{index}", "<Button-1>"))
                    for item in self.chart.find_withtag(f"legend-{index}"):
                        if self.chart.type(item) == "text":
                            legend_rows.add(self.chart.coords(item)[1])
                            self.assertLess(self.chart.bbox(item)[3], rectangle[1])
                self.assertLessEqual(len(legend_rows), 2)
                self.assertEqual(rows[-1]["points"], self.chart._series[-1]["points"])

    def test_hidden_legend_curve_remains_selectable_visible_and_hoverable(self):
        self.chart.set_series(self.many_series())
        self.root.update_idletasks()
        self.assertFalse(self.chart.find_withtag("legend-44"))
        self.chart._series_selector.current(45)
        self.chart._series_selector.event_generate("<<ComboboxSelected>>")
        self.root.update_idletasks()
        self.assertEqual("thermal-band-44", self.chart.selected_series_id)
        self.assertEqual(45, self.chart._series_selector.current())
        self.assertTrue(self.chart.find_withtag("legend-44"), "selected hidden curve must join the visible legend")
        label = next(item for item in self.chart.find_withtag("legend-44") if self.chart.type(item) == "text")
        self.assertIn("●", self.chart.itemcget(label, "text"))
        self.assertIn("m=44", self.chart.itemcget(label, "text"))
        line = next(item for item in self.chart.find_withtag("curve-44") if self.chart.type(item) == "line")
        self.assertEqual(3.5, float(self.chart.itemcget(line, "width")))
        target = next(row for row in self.chart._hit_samples if row["series_id"] == "thermal-band-44")
        self.chart._hover(SimpleNamespace(x=target["canvas_x"], y=target["canvas_y"]))
        self.assertEqual("thermal-band-44", self.chart.hover_sample["series_id"])
        self.assertEqual((1000, -124), self.chart.hover_sample["point"])
        self.assertEqual("tt@1k", self.chart.hover_sample["sample"]["point"])
        self.chart._series_selector.current(0)
        self.chart._series_selector.event_generate("<<ComboboxSelected>>")
        self.root.update_idletasks()
        self.assertIsNone(self.chart.selected_series_id)
        self.assertEqual(135, len(self.chart._hit_samples))
        self.chart.set_series(self.rows)
        self.root.update_idletasks()
        self.assertFalse(self.chart.find_withtag("legend-overflow"))
        self.chart.clear()
        self.root.update_idletasks()
        self.assertFalse(self.chart.find_withtag("plot-frame"))
        self.assertEqual([], self.chart._selector_ids)


if __name__ == "__main__":
    unittest.main()
