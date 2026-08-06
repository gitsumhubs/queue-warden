"""
In-memory ring buffer of recent log records, for the log viewer.

Kept in memory rather than read back off disk so the viewer works identically whether the
daemon logs to a file, to journald, or to a container's stdout — which is three different
places across the deployments this replaces.
"""

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
            self._counter += 1
            self.records.append(
                {
                    # Monotonic id so the viewer can poll for "anything after this" without
                    # relying on timestamps, which collide at sub-second resolution.
                    "id": self._counter,
                    "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                    "level": record.levelname,
                    "message": message[:2000],
                }
            )

    def tail(self, limit=200, after_id=None, level=None):
        with self._lock:
            records = list(self.records)

        if after_id is not None:
            records = [record for record in records if record["id"] > after_id]
        if level:
            wanted = level.upper()
            records = [record for record in records if record["level"] == wanted]

        return records[-limit:]

    def clear(self):
        with self._lock:
            self.records.clear()


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
