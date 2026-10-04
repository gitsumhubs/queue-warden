"""Bounded tails of persisted logs; byte offsets are stable archive polling cursors."""
from datetime import datetime
import json
import os
from pathlib import Path
import re

LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+) \[(\w+)\] (.*)$")


def path():
    return Path(os.getenv("RDT_LOG_ARCHIVE", "archive/rdt-cleanup.log")).resolve()


def tail(limit=300, after_id=None, level=None, detector=None, archive_path=None):
    archive = Path(archive_path) if archive_path is not None else path()
    if not archive.is_file():
        return []
    with archive.open("rb") as handle:
        handle.seek(0, 2)
        start = max(0, handle.tell() - 512 * 1024)
        handle.seek(start)
        if start:
            handle.readline()
        offset = handle.tell()
        lines = handle.readlines()
    records = []
    for line in lines:
        offset += len(line)
        decoded = line.decode("utf-8", errors="replace").rstrip()
        entry = None
        if archive_path is not None and decoded.startswith("{"):
            try:
                stored = json.loads(decoded)
                entry = {"id": int(stored["id"]), "timestamp": str(stored["timestamp"]),
                         "level": str(stored["level"]), "message": str(stored["message"])[:2000],
                         "detector": str(stored.get("detector", "")), "source": "queue-warden"}
            except (KeyError, TypeError, ValueError):
                continue
        match = LINE.match(decoded) if entry is None else None
        if entry is None and match:
            stamp, severity, message = match.groups()
            timestamp = stamp.replace(" ", "T").replace(",", ".")
            # Earlier Queue Warden files used plain text. Keep them readable after upgrade.
            try:
                cursor = int(datetime.fromisoformat(timestamp).timestamp() * 1_000_000) if archive_path is not None else offset
            except ValueError:
                continue
            entry = {"id": cursor, "timestamp": timestamp, "level": severity, "message": message,
                     "detector": "finished" if "finished" in message.lower() else "",
                     "source": "queue-warden" if archive_path is not None else "rdt-cleanup"}
        if entry is None:
            continue
        if after_id is not None and entry["id"] <= after_id:
            continue
        if level and entry["level"] != level.upper():
            continue
        if detector and entry["detector"] != detector:
            continue
        records.append(entry)
    return records[-max(1, min(limit, 1000)):]


def restore(log_path, limit=1000):
    """Recent live entries spanning the current file and its three rotating backups."""
    log_path = Path(log_path)
    records = []
    for suffix in (".3", ".2", ".1", ""):
        records.extend(tail(limit=1000, archive_path=log_path.with_name(log_path.name + suffix)))
    return records[-max(1, min(limit, 1000)):]
