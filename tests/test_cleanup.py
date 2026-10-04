import pytest

from warden import cleanup, state as state_module


CONFIG = {
    "detectors": {
        "stalled": {
            "enabled": True,
            "states": ["stalleddl", "error", "missingfiles"],
            "stuck_min_age_minutes": 10,
            "stuck_min_streak": 2,
        },
        "failed_import": {"enabled": True, "states": ["importblocked", "failed"]},
    },
    "cleanup": {"interval_minutes": 5, "blocklist": True, "dry_run": False},
    "state": {"prune_days": 7, "max_history": 200, "max_runs": 100},
}


class FakeTarget:
    def __init__(self, name, records, removable=True):
        self.name = name
        self._records = records
        self.removable = removable
        self.removed = []

    def get_queue(self):
        return self._records

    def remove(self, queue_id, blocklist=True):
        self.removed.append((queue_id, blocklist))
        return self.removable


class FailingTarget(FakeTarget):
    def get_queue(self):
        raise RuntimeError("unreachable")


class FakeClient:
    def __init__(self, torrents):
        self.torrents = torrents
        self.session = object()
        self.deleted = []

    def login(self):
        return self

    def list_torrents(self):
        return self.torrents

    def delete(self, hashes, delete_files=True):
        self.deleted.extend(hashes)


def fresh_state():
    return state_module.load("/nonexistent/state.json")


def test_failed_import_is_removed_through_its_own_target():
    target = FakeTarget("Sonarr", [{"id": 7, "download_id": "aaa", "title": "Blocked", "state": "importblocked", "status": ""}])
    clients = {"targets": [target], "download_client": None}

    deleted, errors = cleanup.run_cleanup(clients, CONFIG, fresh_state())

    assert errors == []
    assert [v.title for v in deleted] == ["Blocked"]
    assert target.removed == [(7, True)]


def test_stalled_torrent_is_matched_back_to_the_arr_that_asked_for_it():
    # The download client has no idea which *arr owns a torrent, so the hash is the join key.
    target = FakeTarget("Radarr", [{"id": 9, "download_id": "deadbeef", "title": "Stalled Film", "state": "downloading", "status": "downloading"}])
    client = FakeClient([{"hash": "DEADBEEF", "state": "error", "progress": 0.3, "dlspeed": 0, "name": "Stalled Film"}])
    clients = {"targets": [target], "download_client": client}

    deleted, errors = cleanup.run_cleanup(clients, CONFIG, fresh_state())

    assert errors == []
    assert target.removed == [(9, True)]
    assert deleted[0].target.name == "Radarr"


def test_unclaimed_torrent_is_swept_from_the_client():
    # Nothing owns it, so there is no release history to blocklist — but it must still go.
    client = FakeClient([{"hash": "orphan", "state": "error", "progress": 0.1, "dlspeed": 0, "name": "Orphan"}])
    clients = {"targets": [], "download_client": client}

    deleted, _ = cleanup.run_cleanup(clients, CONFIG, fresh_state())

    assert client.deleted == ["orphan"]
    assert len(deleted) == 1


def test_one_unreachable_target_does_not_stop_the_others():
    good = FakeTarget("Sonarr", [{"id": 1, "download_id": "a", "title": "Blocked", "state": "failed", "status": ""}])
    bad = FailingTarget("Radarr", [])
    clients = {"targets": [good, bad], "download_client": None}

    deleted, errors = cleanup.run_cleanup(clients, CONFIG, fresh_state())

    assert len(deleted) == 1
    assert any("Radarr" in error for error in errors)


def test_an_item_tripping_both_detectors_is_only_removed_once():
    # Otherwise the metrics double-count and the log shows a phantom second cleanup.
    target = FakeTarget("Sonarr", [{"id": 4, "download_id": "dup", "title": "Dup", "state": "failed", "status": ""}])
    client = FakeClient([{"hash": "dup", "state": "error", "progress": 0.5, "dlspeed": 0, "name": "Dup"}])
    clients = {"targets": [target], "download_client": client}

    deleted, _ = cleanup.run_cleanup(clients, CONFIG, fresh_state())

    assert len(deleted) == 1
    assert len(target.removed) == 1


def test_dry_run_removes_nothing_but_still_reports():
    target = FakeTarget("Sonarr", [{"id": 2, "download_id": "b", "title": "Blocked", "state": "importblocked", "status": ""}])
    clients = {"targets": [target], "download_client": None}
    config = {**CONFIG, "cleanup": {**CONFIG["cleanup"], "dry_run": True}}
    state = fresh_state()

    deleted, _ = cleanup.run_cleanup(clients, config, state)

    assert target.removed == []
    assert len(deleted) == 1
    # A dry run must not inflate the lifetime counters.
    assert state["metrics"]["total_deleted"] == 0


def test_target_reporting_no_removal_leaves_the_item_unclaimed():
    # Bookmarkarr answers false when it did not recognise the release, and that must not
    # be recorded as a successful cleanup.
    target = FakeTarget("Bookmarkarr", [{"id": "h1", "download_id": "h1", "title": "Unknown", "state": "failed", "status": ""}], removable=False)
    clients = {"targets": [target], "download_client": None}

    deleted, _ = cleanup.run_cleanup(clients, CONFIG, fresh_state())

    assert deleted == []


def test_run_is_recorded_in_history_and_metrics():
    target = FakeTarget("Sonarr", [{"id": 5, "download_id": "c", "title": "Blocked", "state": "failed", "status": ""}])
    clients = {"targets": [target], "download_client": None}
    state = fresh_state()

    cleanup.run_cleanup(clients, CONFIG, state)

    assert state["metrics"]["total_deleted"] == 1
    assert state["metrics"]["by_detector"]["failed_import"] == 1
    assert state["metrics"]["by_target"]["Sonarr"] == 1
    assert len(state["runs"]) == 1
    assert state["history"][0]["title"] == "Blocked"


def test_quiet_run_still_records_a_run():
    clients = {"targets": [FakeTarget("Sonarr", [])], "download_client": None}
    state = fresh_state()

    deleted, errors = cleanup.run_cleanup(clients, CONFIG, state)

    assert deleted == [] and errors == []
    assert state["metrics"]["total_runs"] == 1


PENDING_CONFIG = {
    **CONFIG,
    "detectors": {
        **CONFIG["detectors"],
        "failed_import": {
            **CONFIG["detectors"]["failed_import"],
            "pending_warning_minutes": 120,
            "pending_warning_limit": 2,
        },
    },
}


def stuck(queue_id, download_id):
    return {
        "id": queue_id,
        "download_id": download_id,
        "title": f"Stuck {download_id}",
        "state": "importpending",
        "status": "completed",
        "tracked_status": "warning",
        "messages": ["No files found are eligible for import"],
    }


def state_with_old_timers(target_name, *download_ids):
    state = fresh_state()
    old = cleanup.detectors.now_ts() - 3 * 3600
    state["pending"] = {target_name: {download_id: {"first_seen": old} for download_id in download_ids}}
    return state


def test_import_stuck_with_a_warning_is_left_alone_until_the_wait_runs_out():
    target = FakeTarget("Sonarr", [stuck(11, "aaa")])
    clients = {"targets": [target], "download_client": None}
    state = fresh_state()

    deleted, errors = cleanup.run_cleanup(clients, PENDING_CONFIG, state)

    assert deleted == [] and errors == []
    assert target.removed == []
    assert "aaa" in state["pending"]["Sonarr"]

    state["pending"]["Sonarr"]["aaa"]["first_seen"] -= 3 * 3600
    deleted, errors = cleanup.run_cleanup(clients, PENDING_CONFIG, state)

    assert errors == []
    assert target.removed == [(11, True)]
    assert [victim.title for victim in deleted] == ["Stuck aaa"]


def test_many_stuck_imports_at_once_are_held_back_as_a_storage_problem():
    # Three waiting against a limit of two: more likely a missing mount than three bad releases.
    target = FakeTarget("Sonarr", [stuck(number, f"d{number}") for number in (1, 2, 3)])
    clients = {"targets": [target], "download_client": None}

    deleted, errors = cleanup.run_cleanup(clients, PENDING_CONFIG, state_with_old_timers("Sonarr", "d1", "d2", "d3"))

    assert deleted == []
    assert target.removed == []
    assert any("import pending" in error for error in errors)


def test_the_limit_counts_what_is_waiting_not_only_what_is_due():
    # In an outage items come due a few at a time; one due item among many waiting is still held.
    target = FakeTarget("Sonarr", [stuck(number, f"d{number}") for number in (1, 2, 3)])
    clients = {"targets": [target], "download_client": None}

    deleted, errors = cleanup.run_cleanup(clients, PENDING_CONFIG, state_with_old_timers("Sonarr", "d1"))

    assert deleted == []
    assert any("import pending" in error for error in errors)


def test_an_unreachable_target_keeps_its_import_timers():
    clients = {"targets": [FailingTarget("Sonarr", [])], "download_client": None}
    state = state_with_old_timers("Sonarr", "aaa")

    cleanup.run_cleanup(clients, PENDING_CONFIG, state)

    assert "aaa" in state["pending"]["Sonarr"]


def test_timers_for_a_target_no_longer_configured_are_dropped():
    clients = {"targets": [FakeTarget("Sonarr", [])], "download_client": None}
    state = state_with_old_timers("Lidarr", "aaa")

    cleanup.run_cleanup(clients, PENDING_CONFIG, state)

    assert "Lidarr" not in state["pending"]
