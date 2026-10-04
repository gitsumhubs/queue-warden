"""
Persisted state: stall tracking, import-pending timers, run history, and lifetime counters.

Kept on disk because the detectors that wait need to remember what an item looked like on
previous passes, and because the run history is the main thing that makes the daemon
reviewable after the fact rather than just a log to grep.
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone

log = logging.getLogger("warden")

_lock = threading.Lock()


def _empty():
    return {
        "seen": {},
        # Per target: downloads sitting in import-pending with a warning, and since when.
        "pending": {},
        "history": [],
        "runs": [],
        "metrics": {
            "total_deleted": 0,
            "total_runs": 0,
            "by_detector": {"stalled": 0, "failed_import": 0},
            "by_target": {},
            "last_error": None,
        },
    }


def load(path):
    if not os.path.exists(path):
        return _empty()
    try:
        with open(path) as handle:
            state = json.load(handle)
    except (json.JSONDecodeError, OSError) as error:
        # State is a cache, never the source of truth: a corrupt file costs stall streaks
        # and history, not correctness, so starting fresh beats refusing to run.
        log.warning("Discarding unreadable state at %s: %s", path, error)
        return _empty()

    base = _empty()
    base.update(state)
    base["metrics"] = {**_empty()["metrics"], **state.get("metrics", {})}
    return base


def save(path, state):
    with _lock:
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            # Written via a temp file so an interrupted write cannot leave a half-parsed
            # state that the next start would discard.
            temp = f"{path}.tmp"
            with open(temp, "w") as handle:
                json.dump(state, handle, indent=2)
            os.replace(temp, path)
        except OSError as error:
            log.error("Could not save state to %s: %s", path, error)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def record_run(state, limits, deleted, errors, duration_seconds, dry_run=False):
    """Appends one run summary and folds its counts into the lifetime metrics."""
    run = {
        "timestamp": now_iso(),
        "deleted": len(deleted),
        "errors": errors,
        "duration_seconds": round(duration_seconds, 2),
        "dry_run": dry_run,
        "items": [victim.to_dict() for victim in deleted],
    }

    state["runs"].insert(0, run)
    del state["runs"][limits.get("max_runs", 100):]

    for victim in deleted:
        entry = victim.to_dict()
        entry["timestamp"] = run["timestamp"]
        state["history"].insert(0, entry)
    del state["history"][limits.get("max_history", 200):]

    metrics = state["metrics"]
    metrics["total_runs"] += 1
    metrics["last_run"] = run["timestamp"]

    if not dry_run:
        metrics["total_deleted"] += len(deleted)
        for victim in deleted:
            metrics["by_detector"][victim.detector] = metrics["by_detector"].get(victim.detector, 0) + 1
            if victim.target:
                name = victim.target.name
                metrics["by_target"][name] = metrics["by_target"].get(name, 0) + 1

    return run


def record_error(state, message):
    state["metrics"]["last_error"] = {"message": str(message)[:500], "timestamp": now_iso()}


def clear_error(state):
    state["metrics"]["last_error"] = None
