import time

import pytest

from warden import detectors


STALL_SETTINGS = {
    "enabled": True,
    "states": ["stalleddl", "stalledup", "error", "missingfiles"],
    "stuck_min_age_minutes": 10,
    "stuck_min_streak": 2,
}

IMPORT_SETTINGS = {"enabled": True, "states": ["importblocked", "failed", "downloadfailed"]}


class FakeTarget:
    name = "Sonarr"


def torrent(hash_="abc", state="downloading", progress=0.5, dlspeed=0, name="Some Release"):
    return {"hash": hash_, "state": state, "progress": progress, "dlspeed": dlspeed, "name": name}


def test_bad_client_state_is_caught_immediately():
    victims = detectors.detect_stalled([torrent(state="missingfiles")], {}, STALL_SETTINGS)

    assert len(victims) == 1
    assert victims[0].category == "state"
    assert "missingfiles" in victims[0].reason


def test_single_zero_speed_check_is_not_enough():
    # A torrent momentarily reporting 0 B/s between pieces is normal; only a streak counts.
    seen = {}
    victims = detectors.detect_stalled([torrent()], seen, STALL_SETTINGS)

    assert victims == []
    assert seen["abc"]["zero_streak"] == 1


def test_stuck_requires_both_streak_and_age():
    seen = {"abc": {"first_seen": detectors.now_ts(), "last_progress": 0.5, "zero_streak": 5}}

    # Streak is satisfied but the torrent is seconds old, so it is left alone.
    assert detectors.detect_stalled([torrent()], seen, STALL_SETTINGS) == []

    seen["abc"]["first_seen"] = detectors.now_ts() - 3600
    victims = detectors.detect_stalled([torrent()], seen, STALL_SETTINGS)
    assert len(victims) == 1
    assert victims[0].category == "stuck"


def test_progress_resets_the_streak():
    seen = {}
    detectors.detect_stalled([torrent(progress=0.10)], seen, STALL_SETTINGS)
    detectors.detect_stalled([torrent(progress=0.10)], seen, STALL_SETTINGS)
    assert seen["abc"]["zero_streak"] == 2

    detectors.detect_stalled([torrent(progress=0.40, dlspeed=5000)], seen, STALL_SETTINGS)
    assert seen["abc"]["zero_streak"] == 0


def test_completed_torrents_at_zero_speed_are_seeding_not_stuck():
    seen = {"abc": {"first_seen": detectors.now_ts() - 7200, "last_progress": 1.0, "zero_streak": 9}}

    assert detectors.detect_stalled([torrent(progress=1.0)], seen, STALL_SETTINGS) == []


def test_force_stuck_skips_age_and_streak():
    victims = detectors.detect_stalled([torrent()], {}, STALL_SETTINGS, force_stuck=True)

    assert len(victims) == 1
    assert "manual run" in victims[0].reason


def test_disabled_detector_returns_nothing():
    settings = {**STALL_SETTINGS, "enabled": False}

    assert detectors.detect_stalled([torrent(state="error")], {}, settings) == []


def test_failed_import_matches_tracked_state():
    records = [
        {"id": 1, "download_id": "aaa", "title": "Blocked Show", "state": "importblocked", "status": "warning"},
        {"id": 2, "download_id": "bbb", "title": "Healthy Show", "state": "importing", "status": "downloading"},
    ]

    victims = detectors.detect_failed_imports(FakeTarget(), records, IMPORT_SETTINGS)

    assert len(victims) == 1
    assert victims[0].title == "Blocked Show"
    assert victims[0].queue_id == 1


def test_failed_import_matches_status_when_state_is_empty():
    # Some queue entries carry the verdict on status rather than trackedDownloadState.
    records = [{"id": 3, "download_id": "ccc", "title": "Failed Item", "state": "", "status": "failed"}]

    victims = detectors.detect_failed_imports(FakeTarget(), records, IMPORT_SETTINGS)

    assert len(victims) == 1


def test_failed_import_ignores_in_flight_items():
    records = [
        {"id": 4, "download_id": "d", "title": "Queued", "state": "", "status": "queued"},
        {"id": 5, "download_id": "e", "title": "Downloading", "state": "downloading", "status": "downloading"},
        {"id": 6, "download_id": "f", "title": "Importing", "state": "importpending", "status": "completed"},
    ]

    assert detectors.detect_failed_imports(FakeTarget(), records, IMPORT_SETTINGS) == []


def test_prune_drops_only_torrents_the_client_no_longer_lists():
    old = detectors.now_ts() - 30 * 86400
    seen = {
        "gone": {"last_checked": old},
        "still_here": {"last_checked": old},
        "recent": {"last_checked": detectors.now_ts()},
    }

    detectors.prune_seen(seen, {"still_here"}, prune_days=7)

    assert "gone" not in seen
    assert "still_here" in seen
    assert "recent" in seen


PENDING_SETTINGS = {**IMPORT_SETTINGS, "pending_warning_minutes": 120, "pending_warning_limit": 10}


def pending(id_=1, download_id="aaa", tracked_status="warning"):
    return {
        "id": id_,
        "download_id": download_id,
        "title": "Stuck Show",
        "state": "importpending",
        "status": "completed",
        "tracked_status": tracked_status,
        "messages": ["Single episode file contains all episodes in seasons"],
    }


def long_ago():
    return detectors.now_ts() - 3 * 3600


def test_import_pending_without_a_warning_is_never_timed():
    # This is what every healthy download looks like in the moment before it is filed.
    timers = {}
    victims, waiting = detectors.detect_pending_warnings(
        FakeTarget(), [pending(tracked_status="ok")], timers, PENDING_SETTINGS
    )

    assert victims == []
    assert waiting == 0
    assert timers == {}


def test_a_fresh_warning_starts_a_timer_but_removes_nothing():
    timers = {}
    victims, waiting = detectors.detect_pending_warnings(FakeTarget(), [pending()], timers, PENDING_SETTINGS)

    assert victims == []
    assert waiting == 1
    assert timers["aaa"]["title"] == "Stuck Show"


def test_a_warning_that_outlasts_the_wait_is_removed():
    timers = {"aaa": {"first_seen": long_ago()}}
    victims, _ = detectors.detect_pending_warnings(FakeTarget(), [pending()], timers, PENDING_SETTINGS)

    assert len(victims) == 1
    assert victims[0].queue_id == 1
    assert victims[0].detector == "failed_import"
    assert "Single episode file" in victims[0].reason


def test_the_timer_restarts_when_the_warning_clears():
    timers = {"aaa": {"first_seen": long_ago()}}
    detectors.detect_pending_warnings(FakeTarget(), [pending(tracked_status="ok")], timers, PENDING_SETTINGS)
    assert timers == {}

    # The same download picking up a warning again later starts from zero.
    victims, _ = detectors.detect_pending_warnings(FakeTarget(), [pending()], timers, PENDING_SETTINGS)
    assert victims == []


def test_a_season_pack_is_one_download_not_one_per_episode():
    timers = {"aaa": {"first_seen": long_ago()}}
    records = [pending(id_=number) for number in range(1, 31)]

    victims, waiting = detectors.detect_pending_warnings(FakeTarget(), records, timers, PENDING_SETTINGS)

    assert len(victims) == 1
    assert waiting == 1


def test_zero_minutes_turns_the_rule_off_and_drops_its_timers():
    timers = {"aaa": {"first_seen": long_ago()}}
    settings = {**PENDING_SETTINGS, "pending_warning_minutes": 0}

    victims, waiting = detectors.detect_pending_warnings(FakeTarget(), [pending()], timers, settings)

    assert victims == []
    assert waiting == 0
    assert timers == {}
