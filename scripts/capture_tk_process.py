"""Capture the desktop synthetic examples on a dedicated 1600×1000 display."""

import argparse
import os
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from tkinter import ttk

from PIL import ImageGrab

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from isf_tool.core.offsets import offset_label
from isf_tool.tk.app import IsfApplication
from isf_tool.tk.controller import IsfController
from isf_tool.tk.sweep_view import choose_points


VIEWS = {
    1: "01-process",
    2: "02-harmonics",
    3: "03-navigator",
    4: "04-contributions",
    6: "06-bands",
    8: "08-metrics",
}


def wait_for(root, predicate):
    deadline = time.monotonic() + 30
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
    app._show_error = lambda exc, title: errors.append(RuntimeError(f"{title}: {exc}"))
    root.geometry("1600x1000+0+0")

    def save(name, *, settle=True):
        if settle:
            root.update()
        if errors:
            raise RuntimeError("Desktop callback failed") from errors[0]
        image = ImageGrab.grab(xdisplay=display)
        if image.size != (1600, 1000):
            raise RuntimeError(f"Unexpected screenshot dimensions: {image.size}")
        suffix = "-light" if app.theme_var.get() == "light" else ""
        path = out / f"{name}{suffix}.png"
        image.save(path, optimize=True)
        print(f"{path.name} {image.width}x{image.height}")

    def open_example(button, count):
        previous = app.current_result_job_id
        button.invoke()
        root.update_idletasks()
        if count == 1:
            assert app.status_progress.winfo_ismapped()
            save("replay-running", settle=False)
        wait_for(root, lambda: errors or (app.current_result is not None and
                 app.current_result_job_id != previous and len(app._operating_points) == count))
        if errors:
            raise RuntimeError("Synthetic replay failed") from errors[0]
        assert len(app.current_result["summary"]) == 7 * count
        assert not app.current_result["signoff"]
        app.result_offset_var.set(offset_label(10_000))
        app._result_offset_changed()
        assert app.point_var.get()

    try:
        wait_for(root, lambda: app._foreground_operations == 0)
        assert app.theme_var.get() == "dark"
        ui = app.results_view
        for theme in ("dark", "light"):
            if app.theme_var.get() != theme:
                app.theme_button.invoke()
            app.notebook.select(app.testbench_page)
            app.status_var.set("Ready")
            save("testbench-setup")

            open_example(app.load_example_button, 1)
            ui.show(0)
            root.update()
            assert ui.overview.headlines.winfo_ismapped()
            save("00-overview")

            open_example(app.load_sweep_button, 6)
            ui.show(0)
            root.update()
            assert ui.overview.table.tree.winfo_ismapped()
            assert len(ui.overview.table.tree.get_children()) == 6
            assert ui.overview.chart.winfo_ismapped()
            save("00-sweep")

            for view, name in VIEWS.items():
                ui.show(view)
                root.update()
                if not ui.chart.hits:
                    raise RuntimeError(f"No data drawn for view {view:02}")
                if view == 3:
                    assert ui.point_nav.winfo_ismapped() and "VBIAS=" in ui.point_identity.get()
                save(name)

            ui.show(0)
            dialog = choose_points(root, app._operating_points,
                                   app._operating_points[0]["offsets_hz"],
                                   lambda _: None, lambda: None)
            dialog.geometry("655x400+472+300")
            tree = next(w for w in dialog.winfo_children()[0].winfo_children()[1].winfo_children()
                        if isinstance(w, ttk.Treeview))
            tree.selection_set(tree.get_children()[1:4])
            save("select-points")
            dialog.destroy()
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
