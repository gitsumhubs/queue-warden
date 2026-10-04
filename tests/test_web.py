import json
import logging

import pytest

from warden import config as config_module, logbuffer, web


class DummyScheduler:
    next_run = None
    interval_set_to = None
    fast_mode = False

    def set_interval(self, minutes):
        self.interval_set_to = minutes

    def set_fast_mode(self, enabled):
        self.fast_mode = enabled


@pytest.fixture
def runtime(tmp_path):
    config = config_module.load_config(str(tmp_path / "absent.json"))
    config["all_targets"] = config.get("all_targets", [])
    state = {"seen": {}, "history": [], "runs": [], "metrics": {"by_detector": {}, "by_target": {}, "last_error": None, "total_deleted": 0, "total_runs": 0}}
    return web.Runtime(
        config,
        {"targets": [], "download_client": None},
        state,
        str(tmp_path / "state.json"),
        DummyScheduler(),
        config_path=str(tmp_path / "config.json"),
    )


@pytest.fixture
def client(runtime):
    app = web.create_app(runtime)
    app.config["TESTING"] = True
    return app.test_client()


def test_pages_render(client):
    for path in ("/", "/logs", "/settings"):
        assert client.get(path).status_code == 200, path


def test_settings_round_trip_persists_and_reloads(client, runtime, tmp_path):
    payload = json.loads(client.get("/api/config").data)
    payload["targets"] = [
        {"name": "Sonarr", "flavour": "arr", "url": "http://s:8989", "api_key": "abc", "enabled": True}
    ]
    payload["cleanup"]["interval_minutes"] = 11

    response = client.post("/api/config", json=payload)

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["success"] is True
    assert body["targets"] == ["Sonarr"]

    # Written to disk, applied to the live runtime, and pushed to the scheduler.
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["cleanup"]["interval_minutes"] == 11
    assert [t.name for t in runtime.clients["targets"]] == ["Sonarr"]
    assert runtime.scheduler.interval_set_to == 11


def test_invalid_settings_are_rejected_wholesale(client, runtime, tmp_path):
    payload = json.loads(client.get("/api/config").data)
    payload["cleanup"]["interval_minutes"] = 0

    response = client.post("/api/config", json=payload)

    assert response.status_code == 400
    assert json.loads(response.data)["success"] is False
    # A rejected save must not have touched the file or the running config.
    assert not (tmp_path / "config.json").exists()
    assert runtime.config["cleanup"]["interval_minutes"] == 5


def test_unknown_target_type_is_rejected(client):
    payload = json.loads(client.get("/api/config").data)
    payload["targets"] = [{"name": "Odd", "flavour": "nope", "url": "http://x", "api_key": "k"}]

    assert client.post("/api/config", json=payload).status_code == 400


def test_saving_does_not_bake_in_environment_overrides(client, runtime, tmp_path, monkeypatch):
    # A container would otherwise permanently inherit whatever the env said at save time.
    monkeypatch.setenv("SONARR_API_KEY", "from-env")
    payload = json.loads(client.get("/api/config").data)
    payload["targets"] = [
        {"name": "Sonarr", "flavour": "arr", "url": "http://s:8989", "api_key": "typed-in-ui", "enabled": True}
    ]

    client.post("/api/config", json=payload)

    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["targets"][0]["api_key"] == "typed-in-ui"


def test_settings_keeps_unusable_targets_for_editing(client, runtime):
    payload = json.loads(client.get("/api/config").data)
    payload["targets"] = [
        {"name": "Sonarr", "flavour": "arr", "url": "http://s:8989", "api_key": "abc", "enabled": True},
        {"name": "Bookmarkarr", "flavour": "bookmarkarr", "url": "http://b:5000", "api_key": "", "enabled": True},
    ]
    client.post("/api/config", json=payload)

    # Only Sonarr is contactable, but both must survive a round trip through the UI.
    assert [t.name for t in runtime.clients["targets"]] == ["Sonarr"]
    assert len(json.loads(client.get("/api/config").data)["targets"]) == 2


def test_log_endpoint_returns_recent_lines(client):
    logbuffer.install()
    logging.getLogger("warden").warning("a distinctive log line")

    logs = json.loads(client.get("/api/logs").data)["logs"]

    assert any("distinctive" in entry["message"] for entry in logs)


def test_log_endpoint_filters_by_level_and_cursor(client):
    handler = logbuffer.install()
    handler.clear()
    logging.getLogger("warden").info("info line")
    logging.getLogger("warden").error("error line")

    errors = json.loads(client.get("/api/logs?level=ERROR").data)["logs"]
    assert [entry["message"] for entry in errors] == ["error line"]

    everything = json.loads(client.get("/api/logs").data)["logs"]
    after = json.loads(client.get(f"/api/logs?after={everything[-1]['id']}").data)["logs"]
    assert after == []


def test_logs_can_be_cleared(client):
    handler = logbuffer.install()
    logging.getLogger("warden").info("to be cleared")

    client.post("/api/logs/clear")

    assert json.loads(client.get("/api/logs").data)["logs"] == []


def test_dashboard_lists_downloads_waiting_on_import(client, runtime):
    # The wait is the window to import by hand, so it has to be visible before removal.
    runtime.state["pending"] = {
        "Sonarr": {"aaa": {"first_seen": 0, "title": "Stuck Show", "message": "No files found"}}
    }

    body = client.get("/").data.decode()

    assert "Waiting on import" in body
    assert "Stuck Show" in body
    assert json.loads(client.get("/api/status").data)["waiting_on_import"] == 1


def test_finished_settings_round_trip_and_invalid_save_is_atomic(client, runtime, tmp_path):
    payload = client.get("/api/config").get_json()
    payload["detectors"]["finished"] = {"enabled": True, "threshold_minutes": 85}
    assert client.post("/api/config", json=payload).status_code == 200
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["detectors"]["finished"]["threshold_minutes"] == 85
    payload["detectors"]["finished"]["threshold_minutes"] = -10
    assert client.post("/api/config", json=payload).status_code == 400
    assert runtime.config["detectors"]["finished"]["threshold_minutes"] == 85


def test_dashboard_and_api_show_completed_downloads_waiting_for_cleanup(client, runtime, monkeypatch):
    monkeypatch.setattr(web.detectors, "now_ts", lambda: 10000)
    runtime.clients["download_client"] = object()
    runtime.state["finished"] = [{"hash": "abc", "title": "Finished film", "finished_at": 9400, "hold": ""}]
    assert "Finished film" in client.get("/").data.decode()
    status = client.get("/api/status").get_json()
    assert status["waiting_on_finished"] == 1
    waiting = client.get("/api/finished").get_json()["items"]
    assert waiting[0]["remaining"] == 50


def test_finished_activity_is_visible_in_log_view(client):
    handler = logbuffer.install()
    handler.clear()
    logging.getLogger("warden").warning("finished film removed", extra={"detector": "finished"})
    logging.getLogger("warden").warning("unrelated warning")
    logs = client.get("/api/logs?detector=finished").get_json()["logs"]
    assert [entry["message"] for entry in logs] == ["finished film removed"]


def test_fast_mode_control_is_reported_in_status(client):
    response = client.post("/api/fast-mode", json={"enabled": True})
    assert response.status_code == 200
    assert client.get("/api/status").get_json()["fast_mode"] is True
    client.post("/api/fast-mode", json={"enabled": False})
    assert client.get("/api/status").get_json()["fast_mode"] is False


def test_fast_mode_rejects_non_boolean_input(client):
    assert client.post("/api/fast-mode", json={"enabled": "false"}).status_code == 400


def test_archived_rdt_logs_are_viewable_and_downloadable(client, tmp_path, monkeypatch):
    archive = tmp_path / "rdt-cleanup.log"
    archive.write_text("2026-10-01 12:00:00,123 [INFO] unrelated\n2026-10-01 12:05:00,123 [INFO] finished movie removed\n")
    monkeypatch.setenv("RDT_LOG_ARCHIVE", str(archive))
    logs = client.get("/api/logs?source=rdt&detector=finished").get_json()["logs"]
    assert [entry["message"] for entry in logs] == ["finished movie removed"]
    download = client.get("/logs/rdt-archive")
    assert download.status_code == 200
    assert download.data == archive.read_bytes()


def test_current_persistent_log_can_be_downloaded(client, tmp_path, monkeypatch):
    path = tmp_path / "cleanup.log"
    path.write_text("2026-10-04 12:05:00,123 [INFO] completed cleanup\n")
    monkeypatch.setenv("LOG_PATH", str(path))
    response = client.get("/logs/current")
    assert response.status_code == 200
    assert response.data == path.read_bytes()
