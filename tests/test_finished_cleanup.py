"""Completed sweeps must never bypass an import wait or blocklist a good release."""
from copy import deepcopy

import pytest

from warden import cleanup, config as config_module, detectors
from tests.test_cleanup import FakeClient, FakeTarget, FailingTarget, fresh_state


def config():
    cfg = deepcopy(config_module.DEFAULT_CONFIG)
    cfg["cleanup"]["dry_run"] = False
    return cfg


def completed(**overrides):
    return {
        "hash": "done", "name": "Imported film", "state": "uploading",
        "progress": 1.0, "dlspeed": 0, "completion_on": 6000, **overrides,
    }


def run(torrents, targets=None, cfg=None, state=None):
    client = FakeClient(torrents)
    active = {"targets": targets or [], "download_client": client}
    state = state if state is not None else fresh_state()
    deleted, errors = cleanup.run_cleanup(active, cfg or config(), state)
    return client, deleted, errors, state


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(detectors, "now_ts", lambda: 10000)


def test_old_completed_download_is_removed_without_blocklisting():
    target = FakeTarget("Radarr", [])
    client, deleted, errors, state = run([completed()], [target])
    assert errors == []
    assert client.deleted == ["done"]
    assert target.removed == []
    assert len(deleted) == 1 and deleted[0].detector == "finished"
    assert state["metrics"]["by_detector"]["finished"] == 1
    assert state["history"][0]["category"] == "finished"


def test_young_completion_is_shown_waiting_with_time_remaining():
    client, deleted, _, state = run([completed(completion_on=9400)])
    assert client.deleted == [] and deleted == []
    assert state["finished"][0]["finished_at"] == 9400
    assert state["finished"][0]["title"] == "Imported film"


@pytest.mark.parametrize("tracked", ["importpending", "importing", "completed", "downloading"])
def test_any_item_still_in_an_arr_queue_is_held(tracked):
    target = FakeTarget("Sonarr", [{"id": 1, "download_id": "done", "title": "Pending", "state": tracked, "status": "completed"}])
    client, deleted, _, state = run([completed()], [target])
    assert client.deleted == [] and deleted == []
    assert target.removed == []
    assert "Sonarr" in state["finished"][0]["hold"]


def test_unreadable_queue_holds_completed_cleanup():
    client, deleted, errors, state = run([completed()], [FailingTarget("Radarr", [])])
    assert errors and client.deleted == [] and deleted == []
    assert "unavailable" in state["finished"][0]["hold"]


def test_dry_run_reports_completion_without_deleting_or_inflating_counts():
    cfg = config()
    cfg["cleanup"]["dry_run"] = True
    client, deleted, _, state = run([completed()], cfg=cfg)
    assert client.deleted == []
    assert len(deleted) == 1 and deleted[0].category == "finished"
    assert state["metrics"]["total_deleted"] == 0
    assert state["runs"][0]["dry_run"] is True


def test_finished_cleanup_runs_even_when_stall_detection_is_disabled():
    cfg = config()
    cfg["detectors"]["stalled"]["enabled"] = False
    client, deleted, _, _ = run([completed()], cfg=cfg)
    assert client.deleted == ["done"] and len(deleted) == 1


def test_finished_cleanup_can_be_disabled():
    cfg = config()
    cfg["detectors"]["finished"] = {"enabled": False, "threshold_minutes": 60}
    client, deleted, _, _ = run([completed()], cfg=cfg)
    assert client.deleted == [] and deleted == []


@pytest.mark.parametrize("extra", [
    {"completion_on": 0, "seeding_time": 4000},
    {"completion_on": 0, "added_on": 6000},
    {"completion_on": "invalid", "completionOn": 6000},
])
def test_rdt_completion_age_fallbacks_are_preserved(extra):
    client, deleted, _, _ = run([completed(**extra)])
    assert client.deleted == ["done"] and len(deleted) == 1


def test_missing_completion_clock_waits_from_first_observation(monkeypatch):
    torrent = completed(completion_on=0)
    client, deleted, _, state = run([torrent])
    assert client.deleted == [] and deleted == []
    monkeypatch.setattr(detectors, "now_ts", lambda: 13600)
    client, deleted, _, _ = run([torrent], state=state)
    assert client.deleted == ["done"] and len(deleted) == 1


def test_completed_stalled_upload_is_not_blocklisted_or_removed_early():
    cfg = config()
    cfg["detectors"]["finished"] = {"enabled": True, "threshold_minutes": 90}
    client, deleted, _, _ = run([completed(state="stalledUP")], cfg=cfg)
    assert client.deleted == [] and deleted == []


def test_incomplete_download_is_never_finished_cleanup():
    client, deleted, _, _ = run([completed(state="downloading", progress=0.4, dlspeed=100)])
    assert client.deleted == [] and deleted == []


def test_finished_removal_failure_is_recorded_and_retained_for_retry():
    class FailingClient(FakeClient):
        def delete(self, hashes, delete_files=True):
            raise RuntimeError("client refused deletion")
    state = fresh_state()
    deleted, errors = cleanup.run_cleanup(
        {"targets": [], "download_client": FailingClient([completed()])}, config(), state,
    )
    assert deleted == []
    assert any("refused" in message for message in errors)
    assert state["finished"][0]["hash"] == "done"
    assert state["metrics"]["total_deleted"] == 0


def test_client_outage_keeps_observed_completion_timer():
    class UnreadableClient(FakeClient):
        def list_torrents(self):
            raise RuntimeError("client offline")
    _, _, _, state = run([completed(completion_on=0)])
    before = dict(state["finished_seen"])
    cleanup.run_cleanup({"targets": [], "download_client": UnreadableClient([])}, config(), state)
    assert state["finished_seen"] == before
    assert "unavailable" in state["finished"][0]["hold"]


def test_enabled_target_with_missing_credentials_holds_completed_files():
    cfg = config()
    cfg["all_targets"] = [{"name": "Sonarr", "enabled": True, "url": "http://sonarr", "api_key": ""}]
    client, deleted, _, state = run([completed()], cfg=cfg)
    assert client.deleted == [] and deleted == []
    assert "unavailable" in state["finished"][0]["hold"]


def test_duplicate_name_does_not_hide_an_incomplete_enabled_target():
    cfg = config()
    cfg["all_targets"] = [
        {"name": "Sonarr", "flavour": "arr", "enabled": True, "url": "http://sonarr", "api_key": "key"},
        {"name": "Sonarr", "flavour": "arr", "enabled": True, "url": "http://second-sonarr", "api_key": ""},
    ]
    client, deleted, _, _ = run([completed()], [FakeTarget("Sonarr", [])], cfg=cfg)
    assert client.deleted == [] and deleted == []


@pytest.mark.parametrize("state", ["pausedDL", "stoppedDL", "checkingDL", "downloading"])
def test_nearly_complete_transfer_is_never_swept(state):
    cfg = config()
    cfg["detectors"]["stalled"]["enabled"] = False
    client, deleted, _, _ = run([completed(state=state, progress=0.999, completion_on=0, added_on=6000)], cfg=cfg)
    assert client.deleted == [] and deleted == []
