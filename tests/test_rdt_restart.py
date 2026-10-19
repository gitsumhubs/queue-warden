import pytest

from warden import clients, config as config_module, rdt_restart, state as state_module


SETTINGS = {"enabled": True, "stuck_after_minutes": 60, "recheck_after_minutes": 5, "cooldown_minutes": 60}
HOUR = 3600


def waiting(hash_="aaa", size=1, progress=0):
    return {"hash": hash_, "name": f"Release {hash_}", "state": "downloading", "size": size, "progress": progress, "dlspeed": 0}


def active(hash_="bbb"):
    return {"hash": hash_, "name": f"Active {hash_}", "state": "downloading", "size": 5000, "progress": 0.4, "dlspeed": 2000}


def seeding(hash_="ccc"):
    return {"hash": hash_, "name": f"Seed {hash_}", "state": "uploading", "size": 5000, "progress": 1.0, "dlspeed": 0}


class FakeDownloadClient:
    def __init__(self, torrents=(), login_error=None, list_error=None):
        self.torrents = list(torrents)
        self.login_error = login_error
        self.list_error = list_error
        self.session = None
        self.logins = 0

    def login(self):
        self.logins += 1
        if self.login_error:
            raise self.login_error
        self.session = object()
        return self

    def list_torrents(self):
        if self.list_error:
            raise self.list_error
        return self.torrents


class FakeRestart:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def restart(self, container):
        self.calls.append(container)
        if self.error:
            raise self.error
        return {"success": True}


@pytest.fixture
def state(tmp_path):
    return state_module.load(str(tmp_path / "absent.json"))


def run(state, download, restart, now, dry_run=False, messages=None, settings=SETTINGS):
    sent = messages if messages is not None else []
    return rdt_restart.check(settings, state, download, restart, dry_run=dry_run, notify=sent.append, now=now)


def test_stuck_time_counts_from_when_the_torrent_last_changed(state):
    download = FakeDownloadClient([waiting()])
    restart = FakeRestart()

    assert run(state, download, restart, now=0) == "no_stuck"
    assert run(state, download, restart, now=HOUR - 1) == "no_stuck"
    assert restart.calls == []

    # Once it has sat unchanged for the full hour, it is stuck and gets restarted.
    assert run(state, download, restart, now=HOUR) == "restarted"
    assert restart.calls == ["rdtclient"]


def test_a_change_in_size_or_progress_restarts_the_clock(state):
    download = FakeDownloadClient([waiting(size=1)])
    run(state, download, FakeRestart(), now=0)

    # Real-Debrid resolved it: size is now real, so the clock starts again from this check.
    download.torrents = [waiting(size=900)]
    assert run(state, download, FakeRestart(), now=HOUR) == "no_stuck"

    download.torrents = [waiting(size=900)]
    assert run(state, download, FakeRestart(), now=HOUR + 60) == "no_stuck"


def test_a_healthy_download_blocks_the_restart(state):
    download = FakeDownloadClient([waiting(), active()])
    restart = FakeRestart()
    run(state, download, restart, now=0)

    assert run(state, download, restart, now=HOUR) == "active"
    assert restart.calls == []


def test_a_seeding_torrent_does_not_block_the_restart(state):
    # Blocking on any progress would mean one seeding torrent stops restarts for good.
    download = FakeDownloadClient([waiting(), seeding()])
    restart = FakeRestart()
    run(state, download, restart, now=0)

    assert run(state, download, restart, now=HOUR) == "restarted"


def test_cooldown_stops_a_second_restart(state):
    download = FakeDownloadClient([waiting()])
    restart = FakeRestart()
    run(state, download, restart, now=0)
    run(state, download, restart, now=HOUR)

    # Recheck has run, the torrent is still waiting, and the cooldown has not passed yet.
    assert run(state, download, restart, now=HOUR + 10 * 60) == "cooldown"
    assert restart.calls == ["rdtclient"]

    assert run(state, download, restart, now=2 * HOUR + 60) == "restarted"
    assert restart.calls == ["rdtclient", "rdtclient"]


def test_dry_run_reports_what_it_would_do_without_calling_restart(state):
    download = FakeDownloadClient([waiting()])
    restart = FakeRestart()
    run(state, download, restart, now=0, dry_run=True)

    assert run(state, download, restart, now=HOUR, dry_run=True) == "would_restart"
    assert restart.calls == []
    assert state["rdt_restart"]["last_restart"] is None


def test_recheck_reports_recovered_when_the_torrent_resolves(state):
    download = FakeDownloadClient([waiting()])
    restart = FakeRestart()
    messages = []
    run(state, download, restart, now=0)
    run(state, download, restart, now=HOUR, messages=messages)

    download.torrents = [{**waiting(), "size": 900, "progress": 0.0}]
    run(state, download, restart, now=HOUR + 5 * 60, messages=messages)

    assert state["rdt_restart"]["last_result"]["result"] == "recovered"
    assert state["rdt_restart"]["pending"] is None
    assert any("rdtclient recheck: recovered" in message for message in messages)


def test_recheck_reports_unchanged_when_it_is_still_waiting(state):
    download = FakeDownloadClient([waiting()])
    restart = FakeRestart()
    run(state, download, restart, now=0)
    run(state, download, restart, now=HOUR)

    run(state, download, restart, now=HOUR + 5 * 60)

    assert state["rdt_restart"]["last_result"]["result"] == "unchanged"


def test_recheck_reports_error_when_rdtclient_is_unreachable(state):
    download = FakeDownloadClient([waiting()])
    restart = FakeRestart()
    run(state, download, restart, now=0)
    run(state, download, restart, now=HOUR)

    download.list_error = ConnectionError("connection refused")
    assert run(state, download, restart, now=HOUR + 5 * 60) == "error"
    assert state["rdt_restart"]["last_result"]["result"] == "error"


def test_failed_login_never_restarts(state):
    download = FakeDownloadClient(login_error=RuntimeError("Download client login failed: 403"))
    restart = FakeRestart()

    assert run(state, download, restart, now=HOUR) == "error"
    assert restart.calls == []
    assert "login failed" in state["metrics"]["last_error"]["message"]


def test_restart_service_call_names_rdtclient(state):
    download = FakeDownloadClient([waiting("aaa"), waiting("ddd")])
    restart = FakeRestart()
    run(state, download, restart, now=0)

    assert run(state, download, restart, now=HOUR) == "restarted"
    assert restart.calls == ["rdtclient"]
    assert state["rdt_restart"]["pending"]["stuck"] == ["aaa", "ddd"]


def test_restart_failure_is_recorded_and_waits_out_the_cooldown(state):
    download = FakeDownloadClient([waiting()])
    restart = FakeRestart(error=RuntimeError("restart service unreachable"))
    messages = []
    run(state, download, restart, now=0)

    assert run(state, download, restart, now=HOUR, messages=messages) == "restart_failed"
    assert "restart service unreachable" in state["metrics"]["last_error"]["message"]
    assert state["rdt_restart"]["last_result"]["result"] == "error"
    assert state["rdt_restart"]["pending"] is None

    # The attempt started the cooldown, so the next pass does not hammer the service.
    assert run(state, download, restart, now=HOUR + 60, messages=messages) == "cooldown"
    assert restart.calls == ["rdtclient"]


def test_disabled_check_does_nothing(state):
    download = FakeDownloadClient([waiting()])
    restart = FakeRestart()
    off = {**SETTINGS, "enabled": False}

    assert run(state, download, restart, now=HOUR, settings=off) == "disabled"
    assert restart.calls == []


def test_restart_state_survives_a_round_trip_to_disk(state, tmp_path):
    download = FakeDownloadClient([waiting()])
    run(state, download, FakeRestart(), now=0)
    path = str(tmp_path / "state.json")
    state_module.save(path, state)

    reloaded = state_module.load(path)

    assert reloaded["rdt_restart"]["seen"]["aaa"]["first_seen"] == 0


def test_rdt_settings_are_validated():
    config, errors = config_module.normalise({"detectors": {"rdt_stuck": {"stuck_after_minutes": 0}}})
    assert any("stuck time" in error for error in errors)


def test_restart_token_comes_from_the_environment_and_never_reaches_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("RESTART_SERVICE_TOKEN", "secret-token")
    config = config_module.load_config(str(tmp_path / "config.json"))
    assert config["restart_service"]["token"] == "secret-token"

    config_module.save_config(config, str(tmp_path / "config.json"))

    assert "secret-token" not in (tmp_path / "config.json").read_text()


class RecordingSession:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body or {"success": True, "message": "restarted rdtclient"}
        self.calls = []

    def __call__(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers})

        class Response:
            status_code = self.status

            def json(inner):
                return self.body

        return Response()


def test_restart_client_sends_the_token_and_container_name(monkeypatch):
    session = RecordingSession()
    monkeypatch.setattr(clients.requests, "post", session)

    clients.RestartServiceClient("http://restart-service:8080/", "tok").restart("rdtclient")

    assert session.calls[0]["url"] == "http://restart-service:8080/restart/rdtclient"
    assert session.calls[0]["headers"] == {"X-Restart-Token": "tok"}


def test_restart_client_raises_when_the_service_refuses(monkeypatch):
    monkeypatch.setattr(clients.requests, "post", RecordingSession(status=401, body={"message": "missing or wrong token"}))

    with pytest.raises(RuntimeError, match="missing or wrong token"):
        clients.RestartServiceClient("http://restart-service:8080", "bad").restart("rdtclient")


def test_waiting_torrent_is_left_to_rdtclient_recovery_when_it_is_on(tmp_path, monkeypatch):
    from warden import cleanup

    class DeletingClient(FakeDownloadClient):
        def __init__(self, torrents):
            super().__init__(torrents)
            self.deleted = []

        def delete(self, hashes, delete_files=True):
            self.deleted.extend(hashes)

    monkeypatch.setattr(config_module.os, "getenv", lambda name, default=None: default)
    config = config_module.load_config(str(tmp_path / "config.json"))
    config["detectors"]["stalled"]["stuck_min_age_minutes"] = 0
    config["detectors"]["stalled"]["stuck_min_streak"] = 1
    config["cleanup"]["dry_run"] = False
    config["detectors"]["rdt_stuck"]["enabled"] = True
    download = DeletingClient([waiting("aaa")])
    state = state_module.load(str(tmp_path / "absent.json"))

    # Two passes: the stalled detector would otherwise remove it on the second, with no hour wait at all.
    for _ in range(2):
        cleanup.run_cleanup({"targets": [], "download_client": download}, config, state)

    assert download.deleted == []


def test_waiting_torrent_is_still_removed_by_stalled_when_recovery_is_off(tmp_path, monkeypatch):
    from warden import cleanup

    class DeletingClient(FakeDownloadClient):
        def __init__(self, torrents):
            super().__init__(torrents)
            self.deleted = []

        def delete(self, hashes, delete_files=True):
            self.deleted.extend(hashes)

    monkeypatch.setattr(config_module.os, "getenv", lambda name, default=None: default)
    config = config_module.load_config(str(tmp_path / "config.json"))
    config["detectors"]["stalled"]["stuck_min_age_minutes"] = 0
    config["detectors"]["stalled"]["stuck_min_streak"] = 1
    config["cleanup"]["dry_run"] = False
    download = DeletingClient([waiting("aaa")])
    state = state_module.load(str(tmp_path / "absent.json"))

    for _ in range(2):
        cleanup.run_cleanup({"targets": [], "download_client": download}, config, state)

    assert set(download.deleted) == {"aaa"}
