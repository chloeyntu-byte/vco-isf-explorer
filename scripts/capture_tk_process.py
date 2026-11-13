"""Capture public synthetic results; requires Pillow and a user-owned DISPLAY."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from isf_tool.example_data import result
from isf_tool.tk.app import IsfApplication


def pump(root, seconds=.25):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        root.update()
        time.sleep(.01)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--display', required=True)
    parser.add_argument('--out', type=Path, default=Path('docs/screenshots'))
    parser.add_argument('--result', type=Path, help='Optional user-provided result JSON')
    parser.add_argument('--size', default='1600x1000')
    args = parser.parse_args()
    if os.environ.get('DISPLAY') != args.display:
        parser.error('DISPLAY must match --display')
    from PIL import ImageGrab
    args.out.mkdir(parents=True, exist_ok=True)
    data = json.loads(args.result.read_text()) if args.result else result()
    errors = []
    with tempfile.TemporaryDirectory() as workspace:
        root = tk.Tk()
        root.report_callback_exception = lambda *e: errors.append(str(e[1]))
        with mock.patch.object(IsfApplication, '_submit'), mock.patch.object(IsfApplication, '_refresh_jobs'):
            app = IsfApplication(root, mock.Mock(workspace=Path(workspace)))
        try:
            root.geometry(args.size + '+0+0')
            pump(root)
            w, h = map(int, args.size.split('x'))
            ImageGrab.grab(bbox=(0,0,w,h), xdisplay=args.display).save(args.out/'01-empty-state.png')
            app.current_job_id = 'synthetic'
            app.current_job = dict(id='synthetic', status='succeeded')
            app.noise_kind_var.set('both')
            app._result_loaded(data, 'synthetic')
            app._set_window_title('Synthetic ideal LC')
            app.status_var.set('合成示例 · Synthetic analytic LC · replay only')
            app.theme_var.set('dark'); app._apply_theme()
            pump(root)
            records = []
            for name, view in [('01-overview',0), ('02-process',1), ('03-harmonics',2),
                               ('04-bands',6), ('05-contributions',4), ('06-thermal-bands',7)]:
                ui = app.results_view
                ui.show(view); pump(root)
                if ui.table.rows:
                    key = next(iter(ui.table.rows))
                    ui.table.tree.selection_set(key); pump(root)
                    assert ui.chart.selected_key == key
                    assert ui.table.tree.bbox(key)
                assert not errors, errors
                w, h = map(int, args.size.split('x'))
                assert (root.winfo_width(),root.winfo_height()) == (w,h)
                ImageGrab.grab(bbox=(0,0,w,h), xdisplay=args.display).save(args.out/(name+'.png'))
                records.append(dict(file=name+'.png',view=view,rows=len(ui.table.rows),synthetic=not bool(args.result)))
            ui.overlay.set(True);ui.show(1);pump(root)
            ImageGrab.grab(bbox=(0,0,w,h),xdisplay=args.display).save(args.out/'07-overlay.png')
            app.status_var.set('运行中 · Synthetic replay demonstration')
            pump(root)
            ImageGrab.grab(bbox=(0,0,w,h),xdisplay=args.display).save(args.out/'06-running.png')
            (args.out/'captures.json').write_text(json.dumps(dict(captures=records,callback_errors=errors),indent=2))
        finally:
            app.queue.close()
            for timer in root.tk.call('after','info'):
                root.tk.call('after','cancel',timer)
            root.destroy()


if __name__ == '__main__':
    main()
