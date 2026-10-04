from copy import deepcopy
import importlib
import json

import pytest

from warden import config as config_module, state as state_module


def legacy_state():
    return {"metrics": {"total_deletions": 12, "deletions_by_reason": {"finished": 7, "bad-state": 5},
        "deletion_history": [{"timestamp": "2026-10-01T10:00:00+00:00", "name": "Old movie", "reason": "finished 61m ago"}],
        "runs": [{"timestamp": "2026-10-01T10:00:00+00:00", "deleted": 1}]}}


def test_rdt_import_preserves_existing_qw_data_and_is_idempotent():
    migrate = importlib.import_module("warden.migrate_rdt")
    cfg = deepcopy(config_module.DEFAULT_CONFIG)
    cfg["cleanup"]["dry_run"] = False
    cfg["download_client"]["password"] = "keep-current"
    state = state_module._empty()
    state["metrics"]["total_deleted"] = 4
    state["history"] = [{"timestamp": "2026-10-03T10:00:00+00:00", "title": "New movie"}]
    source_cfg = {"cleanup": {"finished_threshold_minutes": 85}}
    migrate.merge(source_cfg, legacy_state(), cfg, state)
    assert cfg["download_client"]["password"] == "keep-current"
    assert cfg["cleanup"]["dry_run"] is False
    assert cfg["detectors"]["finished"]["threshold_minutes"] == 85
    assert state["metrics"]["total_deleted"] == 16
    assert state["metrics"]["by_detector"]["finished"] == 7
    assert [item["title"] for item in state["history"]] == ["New movie", "Old movie"]
    assert state["history"][1]["source"] == "rdt-cleanup"
    migrate.merge(source_cfg, legacy_state(), cfg, state)
    assert state["metrics"]["total_deleted"] == 16
    assert len(state["history"]) == 2


def test_corrupt_destination_state_aborts_without_overwriting(tmp_path, monkeypatch):
    migrate = importlib.import_module("warden.migrate_rdt")
    source = tmp_path / "rdt"
    (source / "state").mkdir(parents=True)
    (source / "config.json").write_text(json.dumps({"cleanup": {"finished_threshold_minutes": 60}}))
    (source / "state/state.json").write_text(json.dumps(legacy_state()))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_module.DEFAULT_CONFIG))
    state_path = tmp_path / "state.json"
    state_path.write_text("{corrupt")
    original_config = config_path.read_bytes()
    monkeypatch.setattr("sys.argv", ["migrate", "--source", str(source), "--config", str(config_path),
                                     "--state", str(state_path), "--archive", str(tmp_path / "archive")])
    with pytest.raises(json.JSONDecodeError):
        migrate.main()
    assert state_path.read_text() == "{corrupt"
    assert config_path.read_bytes() == original_config
