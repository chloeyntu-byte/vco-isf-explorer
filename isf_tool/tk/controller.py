"""Thread-safe bridge between Tk callbacks and :class:`ToolService`."""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any, Callable

from ..core.service import ToolService


TERMINAL_JOB_STATES = frozenset(("succeeded", "failed", "interrupted"))
ACTIVE_JOB_STATES = frozenset(("queued", "running"))


class BackgroundTaskQueue:
    """Run slow service calls serially and deliver callbacks on Tk's thread."""

    def __init__(self, root: Any, interval_ms: int = 80):
        self.root = root
        self.interval_ms = int(interval_ms)
        self.tasks: queue.Queue[Any] = queue.Queue()
        self.results: queue.Queue[Any] = queue.Queue()
        self.closed = False
        self.worker = threading.Thread(
            target=self._work,
            name="isf-tk-service",
            daemon=True,
        )
        self.worker.start()
        self._schedule()

    def _schedule(self) -> None:
        if self.closed:
            return
        try:
            self.root.after(self.interval_ms, self._drain)
        except Exception:
            self.close()

    def submit(
        self,
        function: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if self.closed:
            raise RuntimeError("task queue is closed")
        self.tasks.put((function, on_success, on_error))

    def _work(self) -> None:
        while True:
            item = self.tasks.get()
            if item is None:
                return
            function, on_success, on_error = item
            try:
                value = function()
            except Exception as exc:  # returned to the Tk thread
                self.results.put((False, exc, on_error))
            else:
                self.results.put((True, value, on_success))

    def _drain(self) -> None:
        try:
            while not self.closed:
                try:
                    ok, value, callback = self.results.get_nowait()
                except queue.Empty:
                    break
                if callback is not None:
                    callback(value)
        finally:
            self._schedule()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.tasks.put(None)


class IsfController:
    """Small direct-call facade used by the native UI and its tests."""

    def __init__(self, workspace: Path | str, service: ToolService | None = None, *, maestro_context: dict[str, Any] | None = None):
        self.workspace = Path(workspace).expanduser().resolve()
        self.service = service or ToolService(self.workspace, maestro_context=maestro_context)

    def health(self) -> dict[str, Any]:
        return self.service.health()

    def error_catalog(self) -> dict[str, Any]:
        return self.service.error_catalog()

    def load_config(self, path: str | None = None) -> dict[str, Any]:
        return self.service.load_config({"path": path} if path else None)

    def save_config(self, config: dict[str, Any], path: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"config": config}
        if path:
            payload["path"] = path
        return self.service.save_config(payload)

    def list_states(self) -> dict[str, Any]:
        return self.service.list_states()

    def save_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.service.save_state(payload)

    def load_state(self, state_id: str) -> dict[str, Any]:
        return self.service.load_state({"id": state_id})

    def discover_maestro(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.service.discover_maestro(payload)

    def inspect_pair(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.service.inspect_maestro_pair(payload)

    def inspect_maestro(self, payload: dict[str, Any]) -> dict[str, Any]:
        from ..core.maestro_pair import inspect_maestro
        return inspect_maestro(payload)

    def refresh_maestro_context(self, context: dict[str, Any]) -> dict[str, Any]:
        from ..core.maestro_context import refresh_maestro_context
        return refresh_maestro_context(context)

    def preflight(self, config: dict[str, Any]) -> dict[str, Any]:
        return self.service.preflight({"config": config})

    def import_bundle(self, path: str, bundle_format: str = "canonical") -> dict[str, Any]:
        return self.service.import_as_job(
            {"path": path, "format": bundle_format, "signoff": False}
        )

    def start_run(
        self,
        config: dict[str, Any],
        run_root: str | None = None,
        bundle_path: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "config": config,
            "background": True,
            "require_preflight": True,
        }
        if run_root:
            payload["run_root"] = run_root
        if bundle_path:
            payload["bundle_path"] = bundle_path
        return self.service.start_run(payload)

    def resume(self, job_id: str) -> dict[str, Any]:
        return self.service.resume_job(job_id, {"background": True})

    def stop_after_current_offset(self, job_id: str) -> dict[str, Any]:
        return self.service.stop_after_current_offset(job_id)

    def job(self, job_id: str) -> dict[str, Any]:
        return self.service.get_job(job_id)

    def jobs(self) -> dict[str, Any]:
        return self.service.list_jobs()

    def result(self, job_id: str) -> dict[str, Any]:
        return self.service.get_result(job_id)

    def has_active_jobs(self) -> bool:
        return any(
            item.get("status") in ACTIVE_JOB_STATES
            for item in self.service.list_jobs().get("jobs", [])
        )


__all__ = [
    "ACTIVE_JOB_STATES",
    "TERMINAL_JOB_STATES",
    "BackgroundTaskQueue",
    "IsfController",
]
