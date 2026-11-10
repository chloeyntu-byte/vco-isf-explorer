"""UI smoke tests: theme application and the bundled example (needs a display)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import tkinter as tk
from tkinter import ttk

from isf_tool.tk import fieldwork_tk as fw


def _display_available() -> bool:
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


@unittest.skipUnless(_display_available(), "no X display available")
class ThemeSmokeTests(unittest.TestCase):
    def test_apply_theme_header_and_status_chip(self):
        root = tk.Tk()
        try:
            fw.apply(root, product="isf")
            fw.header(root, "VCO ISF Explorer", "subtitle", product="isf").pack(fill="x")
            chip = fw.StatusChip(root)
            chip.set("running", "Sweep 3/8")
            ttk.Button(root, text="Run", style="Primary.TButton").pack()
            root.update()
            self.assertEqual(fw.accent("isf"), "#93D65E")
        finally:
            root.destroy()

    def test_main_window_builds_and_has_single_primary_button(self):
        from isf_tool.tk import app
        from isf_tool.tk.controller import IsfController

        root = tk.Tk()
        try:
            with tempfile.TemporaryDirectory() as workspace:
                instance = app.IsfApplication(root, IsfController(Path(workspace)))
                root.update()
                for _ in range(2):
                    instance._toggle_theme()
                    root.update()
                self.assertEqual("dark", instance.theme_var.get())

                def primaries(widget):
                    found = []
                    for child in widget.winfo_children():
                        if isinstance(child, ttk.Button) and str(child.cget("style")) == "Primary.TButton":
                            found.append(child)
                        found.extend(primaries(child))
                    return found

                for tab in (instance.setup_tab, instance.run_tab, instance.result_tab, instance.error_tab):
                    self.assertLessEqual(len(primaries(tab)), 1)
                instance.queue.close()
        finally:
            root.destroy()


class ExampleBundleTests(unittest.TestCase):
    def test_generated_example_imports_as_replay_with_expected_isf_shape(self):
        from isf_tool import example_data
        from isf_tool.core.result_presentation import operating_points, phase_noise_series
        from isf_tool.core.service import ToolService

        with tempfile.TemporaryDirectory() as directory:
            bundle = example_data.build(os.path.join(directory, "ideal_lc"))
            service = ToolService(workspace=os.path.join(directory, "ws"))
            job = service.import_as_job({"path": bundle, "format": "canonical"})
            result = service.get_result(job["job_id"])
            self.assertEqual("replay", result["run_kind"])
            self.assertFalse(result["signoff"])
            identities = [item["id"] for item in operating_points(result)]
            self.assertEqual(["lc_ideal", "lc_mild_asym", "lc_strong_asym"], identities)
            series = phase_noise_series(result, "lc_ideal", scope="pair", noise_kind="flicker")[0]
            values = [point[1] for point in series["points"]]
            self.assertTrue(all(value is not None for value in values))
            # Flicker up-conversion: phase noise falls monotonically with offset.
            self.assertTrue(all(a > b for a, b in zip(values, values[1:])))

    def test_shipped_example_matches_generator(self):
        from isf_tool import example_data

        shipped = Path(__file__).resolve().parents[1] / "examples" / "ideal_lc"
        if not shipped.is_dir():
            self.skipTest("examples/ not present (installed package)")
        with tempfile.TemporaryDirectory() as directory:
            fresh = Path(example_data.build(os.path.join(directory, "x")))
            for name in ("vco_results.csv", "replica_results.csv", "workpoint.csv", "metadata.json"):
                _assert_same_numbers(self, (fresh / name).read_text(), (shipped / name).read_text(), name)


def _assert_same_numbers(case, fresh, shipped, name, rel=1e-9):
    """Equal up to floating-point formatting: NumPy versions may differ in the last printed digit."""
    import math
    import re as _re
    tok = _re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")
    a, b = tok.split(fresh), tok.split(shipped)
    case.assertEqual(a, b, name + ": non-numeric content differs")
    for x, y in zip(tok.findall(fresh), tok.findall(shipped)):
        if x != y:
            case.assertTrue(math.isclose(float(x), float(y), rel_tol=rel, abs_tol=1e-15), "%s: %s != %s" % (name, x, y))


if __name__ == "__main__":
    unittest.main()
