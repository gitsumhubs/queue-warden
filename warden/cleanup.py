"""
The cleanup run: gather victims from both detectors, then clear each through whichever
target owns it.

Ownership matters. Blocklisting has to happen at the *arr, because that is what holds the
release history and decides what to grab next — deleting from the download client alone
just means the same bad release is picked again on the next search.
"""

import logging
import time

from . import detectors, state as state_module

log = logging.getLogger("warden")


def _index_queues(targets):
    """
    One queue read per target, reused by both detectors.

    Returned as (target, records) pairs rather than a flat list so a failing target can be
    reported by name and skipped without taking the run down with it.
    """
    queues = []
    errors = []
    for target in targets:
        try:
            queues.append((target, target.get_queue()))
        except Exception as error:  # noqa: BLE001 - one bad target must not stop the rest
            log.error("[%s] queue read failed: %s", target.name, error)
            errors.append(f"{target.name}: {error}")
    return queues, errors


def _pending_warnings(queues, targets, state, settings, errors):
    """
    Imports that have sat in import-pending with a warning for the whole configured wait.

    Timers are kept per target, so a target whose queue could not be read this pass keeps
    its timers as they were instead of having an outage restart them.
    """
    timers = state.setdefault("pending", {})

    # A target dropped from the config would otherwise leave its timers behind for good.
    names = {target.name for target in targets}
    for name in [name for name in timers if name not in names]:
        del timers[name]

    wait = settings.get("pending_warning_minutes", 0)
    due = []
    waiting = 0
    for target, records in queues:
        known = timers.setdefault(target.name, {})
        before = set(known)
        found, count = detectors.detect_pending_warnings(target, records, known, settings)
        due.extend(found)
        waiting += count

        # Said once, when the timer starts, so there is a window to import it by hand.
        for key in set(known) - before:
            log.info(
                "[%s] import pending with a warning: %s — cleaned up in %dm unless it imports (%s)",
                target.name,
                known[key]["title"],
                wait,
                known[key]["message"] or "no reason given",
            )

    # Counted across everything being timed, not just what is due: when storage or an import
    # path goes away every finished download gets a warning, but they come due a few at a
    # time, and each small batch would look reasonable on its own.
    limit = settings.get("pending_warning_limit", 10)
    if due and waiting > limit:
        message = (
            f"import pending: {waiting} downloads are waiting with a warning, over the limit of "
            f"{limit} — nothing removed, check storage and import paths"
        )
        log.error(message)
        errors.append(message)
        return []

    return due


def _claim(victim, queues):
    """
    Finds the target holding this torrent, and the queue id to remove it by.

    A stall detected at the download client has no target attached yet — the client does
    not know which *arr asked for the torrent, so it is matched back by hash here.
    """
    if victim.target is not None:
        return [(victim.target, victim.queue_id)]

    claims = []
    for target, records in queues:
        for record in records:
            if record["download_id"] and record["download_id"] == victim.download_hash:
                claims.append((target, record["id"]))
                break
    return claims


def run_cleanup(clients, config, state, force_stuck=False):
    """
    One full pass. Returns (deleted, errors).

    Order matters: queue-side detection runs against the same snapshot used for claiming,
    so an item cannot be seen as failed and then missed when it comes time to remove it.
    """
    started = time.monotonic()
    settings = config["detectors"]
    blocklist = config["cleanup"].get("blocklist", True)
    dry_run = config["cleanup"].get("dry_run", False)

    targets = clients["targets"]
    download_client = clients["download_client"]

    queues, errors = _index_queues(targets)

    victims = []

    # Queue side: things the *arrs have already given up on.
    for target, records in queues:
        victims.extend(detectors.detect_failed_imports(target, records, settings["failed_import"]))

    # Queue side, the slow case: finished, flagged by the *arr, and still sitting there.
    victims.extend(_pending_warnings(queues, targets, state, settings["failed_import"], errors))

    # Download-client side: things that are not moving.
    torrents = []
    if download_client is not None and settings["stalled"].get("enabled", True):
        try:
            if download_client.session is None:
                download_client.login()
            torrents = download_client.list_torrents()
            victims.extend(
                detectors.detect_stalled(torrents, state["seen"], settings["stalled"], force_stuck)
            )
        except Exception as error:  # noqa: BLE001
            log.error("Download client read failed: %s", error)
            errors.append(f"download client: {error}")

    # A torrent can trip both detectors at once. Removing it twice would double-count the
    # metrics and log a phantom second cleanup, so the first reason wins.
    unique = {}
    for victim in victims:
        key = victim.download_hash or f"{victim.detector}:{victim.title}"
        unique.setdefault(key, victim)
    victims = list(unique.values())

    if not victims:
        log.info("Nothing to clean up")
        state_module.record_run(state, config["state"], [], errors, time.monotonic() - started, dry_run)
        return [], errors

    deleted = []
    unclaimed = []

    for victim in victims:
        claims = _claim(victim, queues)

        if not claims:
            # No *arr owns it, so there is no release history to blocklist. It still gets
            # removed from the client below, just without a blocklist entry.
            unclaimed.append(victim)
            continue

        removed_by_any = False
        for target, queue_id in claims:
            if dry_run:
                log.info("[%s] DRY RUN would blocklist+remove %s (%s)", target.name, victim.title, victim.reason)
                removed_by_any = True
                continue
            try:
                if target.remove(queue_id, blocklist=blocklist):
                    log.warning("[%s] blocklist+remove %s (%s)", target.name, victim.title, victim.reason)
                    victim.target = target
                    removed_by_any = True
            except Exception as error:  # noqa: BLE001
                log.error("[%s] removal failed for %s: %s", target.name, victim.title, error)
                errors.append(f"{target.name}: {error}")

        if removed_by_any:
            deleted.append(victim)
        else:
            unclaimed.append(victim)

    # Anything no target removed is swept from the client directly, so a stalled torrent
    # belonging to nothing does not accumulate forever.
    if unclaimed and download_client is not None and not dry_run:
        hashes = [victim.download_hash for victim in unclaimed if victim.download_hash]
        if hashes:
            try:
                download_client.delete(hashes)
                for victim in unclaimed:
                    if victim.download_hash:
                        log.warning("[client] removed unclaimed %s (%s)", victim.title, victim.reason)
                        deleted.append(victim)
            except Exception as error:  # noqa: BLE001
                log.error("Direct client removal failed: %s", error)
                errors.append(f"download client delete: {error}")

    if torrents:
        current = {(torrent.get("hash") or "").lower() for torrent in torrents}
        detectors.prune_seen(state["seen"], current, config["state"].get("prune_days", 7))

    state_module.record_run(state, config["state"], deleted, errors, time.monotonic() - started, dry_run)
    log.info(
        "Cleanup complete: %d removed%s, %d error(s)",
        len(deleted),
        " (dry run)" if dry_run else "",
        len(errors),
    )
    return deleted, errors
