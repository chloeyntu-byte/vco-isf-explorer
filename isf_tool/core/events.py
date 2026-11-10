"""Thread-safe stage event and progress recording."""

from __future__ import unicode_literals

import threading

from .errors import ValidationError
from .models import StageEvent


class EventRecorder(object):
    """Record monotonic 0..100 progress and optionally publish each event."""

    def __init__(self, job_id, on_event=None):
        self.job_id = str(job_id)
        self._on_event = on_event
        self._events = []
        self._progress = 0.0
        self._lock = threading.RLock()

    @property
    def progress(self):
        with self._lock:
            return self._progress

    def emit(self, stage, status, progress, message="", details=None):
        try:
            progress_value = float(progress)
        except (TypeError, ValueError):
            raise ValidationError("progress must be numeric")
        if not 0.0 <= progress_value <= 100.0:
            raise ValidationError("progress must be between 0 and 100")

        with self._lock:
            if progress_value < self._progress:
                raise ValidationError(
                    "progress must be monotonic",
                    details={"previous": self._progress, "new": progress_value},
                )
            event = StageEvent(
                len(self._events) + 1,
                self.job_id,
                stage,
                status,
                progress_value,
                message=message,
                details=details,
            )
            self._events.append(event)
            self._progress = progress_value
            callback = self._on_event

        if callback is not None:
            callback(event.to_dict())
        return event.to_dict()

    def snapshot(self):
        with self._lock:
            return [event.to_dict() for event in self._events]
