"""Capture the desktop synthetic example on a user-supplied 1600×1000 display."""

import argparse
import os
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk

from PIL import ImageGrab

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from isf_tool.core.offsets import offset_label
from isf_tool.tk.app import IsfApplication
from isf_tool.tk.controller import IsfController


VIEWS = {
    0: "00-overview",
    1: "01-process",
    2: "02-harmonics",
    4: "04-contributions",
    6: "06-bands",
    8: "08-metrics",
}


def wait_for(root, predicate):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(0.02)
    raise RuntimeError("Timed out waiting for the synthetic replay")


def capture(out, display, workspace):
    root = tk.Tk(screenName=display)
    if (root.winfo_screenwidth(), root.winfo_screenheight()) != (1600, 1000):
        root.destroy()
        raise ValueError("Use a dedicated 1600×1000 display")
    errors = []
    root.report_callback_exception = lambda _, exc, tb: errors.append(exc)
    app = IsfApplication(root, IsfController(workspace))
    root.geometry("1600x1000+0+0")

    def save(name, *, settle=True):
        if settle:
            root.update()
        if errors:
            raise RuntimeError("Desktop callback failed") from errors[0]
        image = ImageGrab.grab(xdisplay=display)
        if image.size != (1600, 1000):
            raise RuntimeError(f"Unexpected screenshot dimensions: {image.size}")
        image.save(out / f"{name}.png")
        print(f"{name}.png {image.width}x{image.height}")

    try:
        wait_for(root, lambda: app._foreground_operations == 0)
        app.notebook.select(app.testbench_page)
        save("testbench-setup")

        app.load_example_button.invoke()
        root.update_idletasks()
        save("replay-running", settle=False)
        wait_for(root, lambda: app.current_result is not None)
        if len(app.current_result["summary"]) != 21 or app.current_result["signoff"]:
            raise RuntimeError("Unexpected replay result")
        app.operating_point_var.set("lc_mild_asym")
        app._operating_point_changed()
        app.result_offset_var.set(offset_label(10_000))
        app._result_offset_changed()
        if not app.point_var.get():
            raise RuntimeError("The synthetic replay has no selected sample")
        app.status_var.set("Synthetic ideal-LC replay loaded")
        ui = app.results_view
        for view, name in VIEWS.items():
            ui.show(view)
            root.update()
            if not ui.chart.hits:
                raise RuntimeError(f"No data drawn for view {view:02}")
            save(name)
        ui.show(0)
        app.theme_button.invoke()
        save("00-overview-light")
    finally:
        app.queue.close()
        root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--display", default=os.environ.get("DISPLAY"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not args.display or args.display != os.environ.get("DISPLAY"):
        parser.error("Set DISPLAY to the dedicated capture display")
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="isf-capture-") as workspace:
        capture(args.out, args.display, Path(workspace))


if __name__ == "__main__":
    main()
