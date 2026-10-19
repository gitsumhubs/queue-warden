"""
The two ways a download goes wrong, each watched from the end that can actually see it.

`stalled` watches the download client: the transfer is not moving. The *arr still reads
those as "downloading", so it will never notice them itself.

`failed_import` watches the queues: the transfer finished and the *arr could not file the
result. The torrent looks perfectly healthy at that point, so the download client has
nothing to report. It has a slow case too — an import the *arr never gives up on, just
leaves pending with a warning — which is only acted on after a wait.

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


FINISHED_STATES = {"uploading", "stalledup", "pausedup", "queuedup", "forcedup", "checkingup", "complete", "completed", "finished"}


def is_finished(torrent):
    """rdt-client's completion states, excluding incomplete/error transfers."""
    state = str(torrent.get("state") or "").lower()
    try:
        progress = float(torrent.get("progress", 0))
    except (TypeError, ValueError):
        progress = 0
    if state in FINISHED_STATES:
        return True
    return (progress >= 1.0 and "queued" not in state and "error" not in state
            and not state.endswith("dl")
            and state not in {"downloading", "metadl", "missingfiles", "moving", "allocating"})


def _positive_int(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _finished_at(torrent, first_seen, now):
    for key in ("completion_on", "completionOn"):
        completed = _positive_int(torrent.get(key))
        if completed:
            return min(now, completed)
    seeding = _positive_int(torrent.get("seeding_time"))
    if seeding:
        return now - seeding
    for key in ("added_on", "addedOn"):
        added = _positive_int(torrent.get(key))
        if added:
            return min(now, added)
    # A client with no completion clock still gets swept, after a full observed wait.
    return first_seen


def detect_finished(torrents, queues, seen, settings, queues_available=True):
    """Completed cleanup never blocklists, and never takes files from a pending import."""
    if not settings.get("enabled", False):
        seen.clear()
        return [], []
    owners = {}
    for target, records in queues:
        for record in records:
            download_id = (record.get("download_id") or "").lower()
            if download_id:
                owners.setdefault(download_id, set()).add(target.name)

    now = now_ts()
    threshold = settings.get("threshold_minutes", 60) * 60
    victims, waiting = [], []
    current = set()
    for torrent in torrents:
        torrent_hash = str(torrent.get("hash") or "").lower()
        if not torrent_hash or not is_finished(torrent):
            continue
        current.add(torrent_hash)
        first_seen = seen.setdefault(torrent_hash, now)
        completed = _finished_at(torrent, first_seen, now)
        title = torrent.get("name") or torrent_hash
        hold = ""
        if not queues_available:
            hold = "Queue unavailable; waiting to verify import"
        elif torrent_hash in owners:
            hold = "Waiting on " + ", ".join(sorted(owners[torrent_hash])) + " import"
        waiting.append({"hash": torrent_hash, "title": title, "finished_at": completed, "hold": hold})
        if not hold and now - completed >= threshold:
            victims.append(Victim(title, f"finished {(now - completed) // 60}m ago; no pending import", "finished", "finished", torrent_hash))
    for torrent_hash in set(seen) - current:
        del seen[torrent_hash]
    return victims, waiting


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

        # An idle completed upload is handled by the completion limit, not blocklisted.
        if is_finished(torrent):
            continue

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
            # The state alone does not say why the *arr gave up, so carry the tracked status and
            # its first message too, the same way the import-pending reason does.
            detail = f"{target.name} state: {reason}"
            if record.get("tracked_status"):
                detail += f" ({record['tracked_status']})"
            messages = record.get("messages") or []
            if messages:
                detail += f" — {messages[0][:160]}"
            victims.append(
                Victim(
                    record["title"],
                    detail,
                    "failed_import",
                    "failed_import",
                    record.get("download_id"),
                    target=target,
                    queue_id=record.get("id"),
                )
            )

    return victims


# Where a download sits between finishing and being imported. Every healthy download passes
# through it, which is why it cannot simply be listed as a terminal state.
PENDING_STATE = "importpending"
UNHAPPY = ("warning", "error")


def detect_pending_warnings(target, records, timers, settings):
    """
    Queue side, the slow case. Returns (victims, waiting).

    Some imports never reach a terminal state: the *arr cannot work out what the file is,
    attaches a warning, and leaves the item in import-pending until someone imports it by
    hand. The state alone says nothing — a healthy download sits there too for the moment
    before it is filed — so two things have to hold together: the *arr has flagged the item,
    and it has stayed flagged for the whole wait. `timers` holds this target's timers, keyed
    by download.

    `waiting` is how many downloads are being timed, due or not. The caller uses it to tell
    a few bad releases from storage going away, which puts a warning on everything at once.
    """
    wait = settings.get("pending_warning_minutes", 0) * 60
    if not settings.get("enabled", True) or wait <= 0:
        # Switched off: forget the timers, so turning it back on starts from zero rather
        # than removing things on the strength of a wait nobody was watching.
        timers.clear()
        return [], 0

    current = {}
    for record in records:
        if (record.get("state") or "").replace("_", "") != PENDING_STATE:
            continue
        if record.get("tracked_status") not in UNHAPPY:
            continue
        # A season pack is one queue row per episode but a single download, and removing
        # any one row removes them all.
        current.setdefault(record.get("download_id") or f"queue:{record.get('id')}", record)

    # Imported, fixed by hand, or gone. The timer must not survive to be picked up by an
    # unrelated warning on the same download later.
    for key in [key for key in timers if key not in current]:
        del timers[key]

    victims = []
    now = now_ts()
    for key, record in current.items():
        messages = record.get("messages") or []
        message = messages[0][:160] if messages else ""

        entry = timers.setdefault(key, {"first_seen": now})
        entry["title"] = record["title"]
        entry["message"] = message

        age = now - entry["first_seen"]
        if age < wait:
            continue

        reason = f"{target.name} state: {PENDING_STATE}, warning for {age // 60}m"
        if message:
            reason = f"{reason} — {message}"
        victims.append(
            Victim(
                record["title"],
                reason,
                "import_pending",
                "failed_import",
                record.get("download_id"),
                target=target,
                queue_id=record.get("id"),
            )
        )

    return victims, len(current)


def prune_seen(seen, current_hashes, prune_days):
    """Drops tracking for torrents the client no longer lists, so state cannot grow forever."""
    cutoff = now_ts() - prune_days * 86400
    for torrent_hash in list(seen):
        if torrent_hash in current_hashes:
            continue
        if seen[torrent_hash].get("last_checked", 0) < cutoff:
            del seen[torrent_hash]
