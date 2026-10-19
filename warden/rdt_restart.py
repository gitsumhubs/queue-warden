"""
rdtclient stuck-download recovery.

rdtclient resolves each torrent through Real-Debrid before it has a real size. While it waits,
the qBittorrent API reports the torrent as "downloading" with size 1, progress 0 and no speed.
A torrent that stays like that for an hour means rdtclient has stopped resolving it, and a
restart is the only fix on offer. It is only attempted when nothing else is transferring,
because the restart interrupts every active download.

The restart itself goes through restart-service, the one part of the system with Docker access.
"""

import logging

from . import detectors, state as state_module

log = logging.getLogger("warden")

CONTAINER = "rdtclient"


def _size(torrent):
    try:
        return int(torrent.get("size", 0))
    except (TypeError, ValueError):
        return 0


def _progress(torrent):
    try:
        return float(torrent.get("progress", 0))
    except (TypeError, ValueError):
        return 0.0


def _speed(torrent):
    try:
        return int(torrent.get("dlspeed", 0))
    except (TypeError, ValueError):
        return 0


def is_waiting(torrent):
    """Queued at rdtclient, waiting for Real-Debrid to resolve it. The stuck state."""
    if str(torrent.get("state") or "").lower() != "downloading":
        return False
    return _size(torrent) <= 1 or (_progress(torrent) <= 0 and _speed(torrent) <= 0)


def is_resolved(torrent):
    """rdtclient has a real size or some progress for it: Real-Debrid has answered."""
    return _size(torrent) > 1 or _progress(torrent) > 0


def is_active(torrent):
    """
    A real transfer moving data right now.

    Finished and seeding torrents do not count. Blocking on any torrent with progress would
    mean a single seeding torrent stops restarts for good.
    """
    return not detectors.is_finished(torrent) and not is_waiting(torrent) and _speed(torrent) > 0


def find_stuck(seen, torrents, now, stuck_after_seconds):
    """
    Waiting torrents whose size and progress have not changed for the whole window.

    `seen` holds, per hash, when the torrent's size or progress last changed. It is persisted,
    so a restart of Queue Warden does not reset the clock.
    """
    stuck = []
    current = set()
    for torrent in torrents:
        torrent_hash = str(torrent.get("hash") or "").lower()
        if not torrent_hash:
            continue
        current.add(torrent_hash)

        size, progress = _size(torrent), _progress(torrent)
        entry = seen.get(torrent_hash)
        if entry is None or entry["size"] != size or entry["progress"] != progress:
            entry = {"first_seen": now, "size": size, "progress": progress}
            seen[torrent_hash] = entry

        if is_waiting(torrent) and now - entry["first_seen"] >= stuck_after_seconds:
            stuck.append(torrent)

    for torrent_hash in set(seen) - current:
        del seen[torrent_hash]
    return stuck


def _read(download_client):
    if download_client.session is None:
        download_client.login()
    try:
        return download_client.list_torrents()
    except Exception:
        # Drop the session so the next check logs in again rather than failing forever.
        download_client.session = None
        raise


def _recheck(rs, torrents, read_error, now):
    """Compares the torrents we restarted for against what rdtclient reports now."""
    stuck_hashes = rs["pending"]["stuck"]
    if torrents is None:
        result, message = "error", f"rdtclient unreachable or login failed: {read_error}"
    else:
        by_hash = {str(torrent.get("hash") or "").lower(): torrent for torrent in torrents}
        still = [h for h in stuck_hashes if h in by_hash and not is_resolved(by_hash[h])]
        if still:
            result = "unchanged"
            message = f"{len(still)} of {len(stuck_hashes)} download(s) still waiting after the restart"
        else:
            result = "recovered"
            message = f"all {len(stuck_hashes)} download(s) now resolving"

    rs["last_result"] = {"result": result, "message": message, "timestamp": state_module.now_iso()}
    rs["pending"] = None
    log.info("[rdtclient] recheck %s: %s", result, message, extra={"detector": "rdt_stuck"})
    return result, message


def check(settings, state, download_client, restart_client, dry_run, notify=lambda message: None, now=None):
    """
    One pass of the rdtclient check. Returns the outcome, for logs and tests.

    Ordering matters. A restart is followed by a recheck once `recheck_after_minutes` has
    passed, and no second restart happens while that recheck is still pending.
    """
    if not settings.get("enabled", False) or download_client is None or restart_client is None:
        return "disabled"

    now = detectors.now_ts() if now is None else now
    rs = state.setdefault("rdt_restart", {})
    rs.setdefault("seen", {})
    rs.setdefault("last_restart", None)
    rs.setdefault("pending", None)
    rs.setdefault("last_result", None)

    try:
        torrents, read_error = _read(download_client), None
    except Exception as error:  # noqa: BLE001
        torrents, read_error = None, error

    if rs["pending"] and now - rs["pending"]["restarted_at"] >= settings.get("recheck_after_minutes", 5) * 60:
        result, message = _recheck(rs, torrents, read_error, now)
        notify(f"Queue Warden rdtclient recheck: {result} — {message}")

    if read_error is not None:
        log.error("[rdtclient] check failed: %s", read_error, extra={"detector": "rdt_stuck"})
        state_module.record_error(state, f"rdtclient check failed: {read_error}")
        return "error"

    stuck = find_stuck(rs["seen"], torrents, now, settings.get("stuck_after_minutes", 60) * 60)
    if not stuck:
        return "no_stuck"

    if any(is_active(torrent) for torrent in torrents):
        log.info("[rdtclient] %d stuck, but a healthy download is active; not restarting", len(stuck),
                 extra={"detector": "rdt_stuck"})
        return "active"

    if rs["pending"]:
        return "awaiting_recheck"

    cooldown = settings.get("cooldown_minutes", 60) * 60
    if rs["last_restart"] is not None and now - rs["last_restart"] < cooldown:
        return "cooldown"

    names = ", ".join(torrent.get("name") or torrent.get("hash") for torrent in stuck)
    if dry_run:
        log.info("[rdtclient] DRY RUN would restart rdtclient: %d stuck (%s)", len(stuck), names,
                 extra={"detector": "rdt_stuck"})
        return "would_restart"

    # Set before the call, so a failure still starts the cooldown rather than retrying every pass.
    rs["last_restart"] = now
    try:
        restart_client.restart(CONTAINER)
    except Exception as error:  # noqa: BLE001
        log.error("[rdtclient] restart failed: %s", error, extra={"detector": "rdt_stuck"})
        state_module.record_error(state, f"rdtclient restart failed: {error}")
        rs["last_result"] = {"result": "error", "message": f"restart failed: {error}",
                             "timestamp": state_module.now_iso()}
        notify(f"Queue Warden could not restart rdtclient: {error}")
        return "restart_failed"

    rs["pending"] = {"restarted_at": now, "stuck": [str(t.get("hash") or "").lower() for t in stuck]}
    log.warning("[rdtclient] restarted rdtclient to clear %d stuck download(s): %s", len(stuck), names,
                extra={"detector": "rdt_stuck"})
    notify(f"Queue Warden restarted rdtclient to clear {len(stuck)} stuck download(s): {names}")
    return "restarted"
