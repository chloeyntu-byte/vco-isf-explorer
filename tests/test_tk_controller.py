from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from isf_tool.tk.controller import BackgroundTaskQueue, IsfController


class FakeService:
    def __init__(self) -> None:
        self.calls = []
        self.job_rows = [{"id": "job-1", "status": "queued"}]

    def _record(self, name, payload=None):
        self.calls.append((name, payload))

    def health(self):
        self._record("health")
        return {"status": "ok"}

    def error_catalog(self):
        self._record("error_catalog")
        return {"entries": []}

    def load_config(self, payload=None):
        self._record("load_config", payload)
        return {"loaded": payload}

    def save_config(self, payload):
        self._record("save_config", payload)
        return {"ok": True, "config": payload["config"]}

    def discover_maestro(self, payload):
        self._record("discover_maestro", payload)
        return {"findings": []}

    def inspect_maestro_pair(self, payload):
        self._record("inspect_maestro_pair", payload)
        return {"ok": True}

    def preflight(self, payload):
        self._record("preflight", payload)
        return {"ok": True}

    def import_as_job(self, payload):
        self._record("import_as_job", payload)
        return {"ok": True, "job_id": "replay-1"}

    def start_run(self, payload):
        self._record("start_run", payload)
        return "job-1"

    def resume_job(self, job_id, payload):
        self._record("resume_job", (job_id, payload))
        return {"ok": True, "job_id": job_id}

    def get_job(self, job_id):
        self._record("get_job", job_id)
        return {"id": job_id, "status": "queued"}

    def list_jobs(self):
        self._record("list_jobs")
        return {"jobs": list(self.job_rows)}

    def get_result(self, job_id):
        self._record("get_result", job_id)
        return {"job_id": job_id, "summary": [], "points": {}}


class FakeRoot:
    def __init__(self) -> None:
        self.callbacks = []

    def after(self, _milliseconds, callback):
        self.callbacks.append(callback)
        return "after-id"

    def run_next(self):
        callback = self.callbacks.pop(0)
        callback()


class TkControllerTests(unittest.TestCase):
    def test_controller_is_a_direct_service_facade_and_keeps_run_paths_top_level(self):
        service = FakeService()
        with tempfile.TemporaryDirectory() as temporary:
            controller = IsfController(Path(temporary), service=service)
            self.assertEqual({"status": "ok"}, controller.health())
            self.assertEqual({"entries": []}, controller.error_catalog())
            controller.load_config()
            controller.load_config("/tmp/input.json")
            controller.save_config({"schema_version": 2}, "/tmp/output.json")
            controller.discover_maestro({"roots": [temporary]})
            controller.inspect_pair({"testbenches": {}})
            controller.preflight({"schema_version": 2})
            controller.import_bundle("/tmp/canonical", "canonical")
            job_id = controller.start_run(
                {"execution": {"dry_run": True}},
                run_root="/tmp/run-root",
                bundle_path="/tmp/bundle",
            )
            controller.resume(job_id)
            controller.job(job_id)
            controller.jobs()
            controller.result(job_id)

        start_payload = next(payload for name, payload in service.calls if name == "start_run")
        self.assertEqual("job-1", job_id)
        self.assertTrue(start_payload["background"])
        self.assertTrue(start_payload["require_preflight"])
        self.assertEqual("/tmp/run-root", start_payload["run_root"])
        self.assertEqual("/tmp/bundle", start_payload["bundle_path"])
        self.assertEqual(
            {"path": "/tmp/canonical", "format": "canonical", "signoff": False},
            next(payload for name, payload in service.calls if name == "import_as_job"),
        )
        self.assertNotIn("http", " ".join(name for name, _payload in service.calls).lower())

    def test_active_job_detection_is_fail_closed_for_queued_and_running(self):
        service = FakeService()
        with tempfile.TemporaryDirectory() as temporary:
            controller = IsfController(temporary, service=service)
            service.job_rows = [{"id": "a", "status": "succeeded"}]
            self.assertFalse(controller.has_active_jobs())
            service.job_rows.append({"id": "b", "status": "running"})
            self.assertTrue(controller.has_active_jobs())

    def test_background_queue_serializes_service_work_and_returns_on_after_callback(self):
        root = FakeRoot()
        task_queue = BackgroundTaskQueue(root, interval_ms=1)
        worker_finished = threading.Event()
        order = []
        callback_threads = []
        main_thread = threading.get_ident()

        def first():
            order.append(("first", threading.get_ident()))
            return 1

        def second():
            order.append(("second", threading.get_ident()))
            worker_finished.set()
            return 2

        task_queue.submit(first, lambda value: callback_threads.append((value, threading.get_ident())))
        task_queue.submit(second, lambda value: callback_threads.append((value, threading.get_ident())))
        self.assertTrue(worker_finished.wait(2.0))
        root.run_next()
        task_queue.close()

        self.assertEqual(["first", "second"], [item[0] for item in order])
        self.assertEqual(order[0][1], order[1][1])
        self.assertNotEqual(main_thread, order[0][1])
        self.assertEqual([(1, main_thread), (2, main_thread)], callback_threads)


if __name__ == "__main__":
    unittest.main()
