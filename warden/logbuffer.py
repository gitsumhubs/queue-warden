"""Recent log records for the live viewer, restored from the rotating log on startup."""

import logging
import threading
from collections import deque
from datetime import datetime, timezone

MAX_RECORDS = 1000


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity=MAX_RECORDS):
        super().__init__()
        self.records = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._counter = 0

    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken log line must never break the app
            message = str(record.msg)

        with self._lock:
            self._counter = max(self._counter + 1, int(record.created * 1_000_000))
            record.warden_id = self._counter
            self.records.append(
                {
                    # Time-based floor survives restarts; the counter breaks timestamp ties.
                    "id": self._counter,
                    "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                    "level": record.levelname,
                    "message": message[:2000],
                    "detector": getattr(record, "detector", ""),
                }
            )

    def tail(self, limit=200, after_id=None, level=None, detector=None):
        with self._lock:
            records = list(self.records)

        if after_id is not None:
            records = [record for record in records if record["id"] > after_id]
        if level:
            wanted = level.upper()
            records = [record for record in records if record["level"] == wanted]
        if detector:
            records = [record for record in records if record.get("detector") == detector]

        return records[-limit:]

    def clear(self):
        with self._lock:
            self.records.clear()

    def restore(self, records):
        """Prime the live viewer from disk before emitting fresh records."""
        with self._lock:
            for record in records:
                self.records.append(dict(record))
                self._counter = max(self._counter, record["id"])


_handler = None


def install(level=logging.INFO):
    """Attaches the buffer to the root logger. Idempotent."""
    global _handler
    if _handler is None:
        _handler = RingBufferHandler()
        _handler.setLevel(level)
        logging.getLogger().addHandler(_handler)
    return _handler


def get_handler():
    return _handler
