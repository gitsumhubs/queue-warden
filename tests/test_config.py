import json

import pytest

from warden import config as config_module


def write(tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_missing_file_falls_back_to_defaults(tmp_path):
    config = config_module.load_config(str(tmp_path / "absent.json"))

    # Defaults ship with empty api keys, so nothing is usable and nothing is acted on.
    assert config["targets"] == []
    assert config["cleanup"]["interval_minutes"] == 5


def test_invalid_json_is_fatal(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json")

    # Defaults alone cannot reach anything, so running on them would be silently useless.
    with pytest.raises(SystemExit):
        config_module.load_config(str(path))


def test_partial_config_still_gets_defaults(tmp_path):
    path = write(tmp_path, {"cleanup": {"interval_minutes": 15}})

    config = config_module.load_config(path)

    assert config["cleanup"]["interval_minutes"] == 15
    assert config["cleanup"]["blocklist"] is True
    assert config["state"]["prune_days"] == 7


def test_targets_without_credentials_are_skipped_quietly(tmp_path):
    path = write(tmp_path, {
        "targets": [
            {"name": "Sonarr", "flavour": "arr", "url": "http://s:8989", "api_key": "k"},
            {"name": "Bookmarkarr", "flavour": "bookmarkarr", "url": "http://b:5000", "api_key": ""},
        ]
    })

    config = config_module.load_config(path)

    assert [t["name"] for t in config["targets"]] == ["Sonarr"]


def test_disabled_target_is_skipped(tmp_path):
    path = write(tmp_path, {
        "targets": [{"name": "Radarr", "flavour": "arr", "url": "http://r:7878", "api_key": "k", "enabled": False}]
    })

    assert config_module.load_config(path)["targets"] == []


def test_unknown_flavour_is_rejected(tmp_path):
    path = write(tmp_path, {
        "targets": [{"name": "Mystery", "flavour": "whatever", "url": "http://m", "api_key": "k"}]
    })

    assert config_module.load_config(path)["targets"] == []


def test_environment_overrides_target_by_name(tmp_path, monkeypatch):
    path = write(tmp_path, {
        "targets": [{"name": "Bookmarkarr", "flavour": "bookmarkarr", "url": "", "api_key": ""}]
    })
    monkeypatch.setenv("BOOKMARKARR_URL", "http://localhost:3018")
    monkeypatch.setenv("BOOKMARKARR_API_KEY", "from-env")

    config = config_module.load_config(path)

    assert config["targets"][0]["url"] == "http://localhost:3018"
    assert config["targets"][0]["api_key"] == "from-env"


def test_empty_environment_variables_do_not_wipe_config(tmp_path, monkeypatch):
    # Compose files pass every variable through with an empty default, which makes them
    # present but blank. Treating that as an override silently erased configured targets.
    path = write(tmp_path, {
        "targets": [{"name": "Sonarr", "flavour": "arr", "url": "http://s:8989", "api_key": "real-key"}],
        "download_client": {"url": "http://rdt:6500", "username": "u", "password": "p"},
    })
    monkeypatch.setenv("SONARR_URL", "")
    monkeypatch.setenv("SONARR_API_KEY", "")
    monkeypatch.setenv("DOWNLOAD_CLIENT_URL", "")

    config = config_module.load_config(path)

    assert config["targets"][0]["url"] == "http://s:8989"
    assert config["targets"][0]["api_key"] == "real-key"
    assert config["download_client"]["url"] == "http://rdt:6500"


def test_missing_config_file_is_created_with_defaults(tmp_path):
    # `docker compose up` with no prior setup must still leave a file the Settings page
    # can save back to.
    path = str(tmp_path / "nested" / "config.json")

    config_module.load_config(path)

    import os
    assert os.path.exists(path)


def test_download_client_without_url_is_disabled(tmp_path):
    path = write(tmp_path, {"download_client": {"url": "", "username": "u", "password": "p"}})

    assert config_module.load_config(path)["download_client"]["enabled"] is False
