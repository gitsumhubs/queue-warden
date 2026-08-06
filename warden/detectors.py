"""
The two ways a download goes wrong, each watched from the end that can actually see it.

`stalled` watches the download client: the transfer is not moving. The *arr still reads
those as "downloading", so it will never notice them itself.

`failed_import` watches the queues: the transfer finished and the *arr could not file the
result. The torrent looks perfectly healthy at that point, so the download client has
nothing to report.

Neither detector can see the other's cases, which is why both exist.
"""

import time


class Victim:
    """One item to be cleared, plus why — the reason is what makes a run reviewable."""

    def __init__(self, title, reason, category, detector, download_hash=None, target=None, queue_id=None):
        self.title = title
        self.reason = reason
        self.category = category
        self.detector = detector
        self.download_hash = (download_hash or "").lower()
        self.target = target
        self.queue_id = queue_id

    def to_dict(self):
        return {
            "title": self.title,
            "reason": self.reason,
            "category": self.category,
            "detector": self.detector,
            "hash": self.download_hash,
            "target": self.target.name if self.target else None,
        }


def now_ts():
    return int(time.time())


def update_seen(seen, torrent_hash, progress, download_speed):
    """
    Tracks how long a torrent has been sitting at zero bytes per second.

    A streak rather than a single reading, because a torrent momentarily reporting 0 B/s
    between pieces is normal; several consecutive checks at zero is not.
    """
    entry = seen.get(torrent_hash)
    if entry is None:
        entry = {"first_seen": now_ts(), "last_progress": progress, "zero_streak": 0}
        seen[torrent_hash] = entry

    if download_speed <= 0 and progress <= entry["last_progress"]:
        entry["zero_streak"] += 1
    else:
        entry["zero_streak"] = 0

    entry["last_progress"] = max(progress, entry["last_progress"])
    entry["last_checked"] = now_ts()
    return entry


def detect_stalled(torrents, seen, settings, force_stuck=False):
    """
    Download-client side. Returns victims for torrents in a bad state or stuck at 0 B/s.

    `force_stuck` is the manual "run now, I know it is stuck" override from the web UI,
    which skips the age and streak requirements.
    """
    victims = []
    if not settings.get("enabled", True):
        return victims

    bad_states = {state.lower() for state in settings.get("states", [])}
    min_age = settings.get("stuck_min_age_minutes", 10) * 60
    min_streak = settings.get("stuck_min_streak", 2)

    for torrent in torrents:
        torrent_hash = (torrent.get("hash") or "").lower()
        if not torrent_hash:
            continue

        try:
            progress = float(torrent.get("progress", 0))
        except (TypeError, ValueError):
            progress = 0.0
        try:
            speed = int(torrent.get("dlspeed", 0))
        except (TypeError, ValueError):
            speed = 0

        entry = update_seen(seen, torrent_hash, progress, speed)
        state = str(torrent.get("state") or "").lower()
        name = torrent.get("name") or torrent_hash

        if state in bad_states or "error" in state:
            victims.append(Victim(name, f"client state: {state}", "state", "stalled", torrent_hash))
            continue

        # Complete torrents sitting at 0 B/s are seeding, not stuck.
        if progress >= 1.0:
            continue

        age = now_ts() - entry["first_seen"]
        if force_stuck and speed <= 0:
            victims.append(Victim(name, "stuck at 0 B/s (manual run)", "stuck", "stalled", torrent_hash))
        elif entry["zero_streak"] >= min_streak and age >= min_age:
            minutes = age // 60
            victims.append(
                Victim(
                    name,
                    f"stuck at 0 B/s for {entry['zero_streak']} checks over {minutes}m",
                    "stuck",
                    "stalled",
                    torrent_hash,
                )
            )

    return victims


def detect_failed_imports(target, records, settings):
    """
    Queue side. Returns victims for items the target has already given up on.

    Deliberately conservative: only states that mean "this is not going to resolve itself"
    count. Anything still queued, downloading, or importing is left alone.
    """
    victims = []
    if not settings.get("enabled", True):
        return victims

    bad_states = {state.lower().replace("_", "") for state in settings.get("states", [])}

    for record in records:
        state = (record.get("state") or "").replace("_", "")
        status = (record.get("status") or "").replace("_", "")

        if state in bad_states or status in bad_states:
            reason = record.get("state") or record.get("status") or "failed"
            victims.append(
                Victim(
                    record["title"],
                    f"{target.name} state: {reason}",
                    "failed_import",
                    "failed_import",
                    record.get("download_id"),
                    target=target,
                    queue_id=record.get("id"),
                )
            )

    return victims


def prune_seen(seen, current_hashes, prune_days):
    """Drops tracking for torrents the client no longer lists, so state cannot grow forever."""
    cutoff = now_ts() - prune_days * 86400
    for torrent_hash in list(seen):
        if torrent_hash in current_hashes:
            continue
        if seen[torrent_hash].get("last_checked", 0) < cutoff:
            del seen[torrent_hash]
