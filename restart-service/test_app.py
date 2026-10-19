from unittest.mock import MagicMock

import pytest

import app as restart_app


TOKEN = "test-token"


@pytest.fixture
def docker_client():
    return MagicMock()


@pytest.fixture
def client(docker_client):
    return restart_app.create_app(TOKEN, docker_client).test_client()


def authorised(name):
    return f"/restart/{name}", {"X-Restart-Token": TOKEN}


def test_allowed_container_is_restarted_through_docker(client, docker_client):
    path, headers = authorised("rdtclient")

    response = client.post(path, headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"success": True, "message": "restarted rdtclient"}
    docker_client.containers.get.assert_called_once_with("rdtclient")
    docker_client.containers.get.return_value.restart.assert_called_once_with(timeout=20)


@pytest.mark.parametrize("name", ["queue-warden", "postgres", "RDTCLIENT", "rdtclient-2", "rdtclient%2F..", "../rdtclient"])
def test_other_container_names_are_rejected_without_touching_docker(client, docker_client, name):
    path, headers = authorised(name)

    response = client.post(path, headers=headers)

    assert response.status_code == 404
    docker_client.containers.get.assert_not_called()


def test_missing_token_is_rejected(client, docker_client):
    response = client.post("/restart/rdtclient")

    assert response.status_code == 401
    docker_client.containers.get.assert_not_called()


def test_wrong_token_is_rejected(client, docker_client):
    response = client.post("/restart/rdtclient", headers={"X-Restart-Token": "guess"})

    assert response.status_code == 401
    docker_client.containers.get.assert_not_called()


def test_docker_failure_is_reported_as_an_error(client, docker_client):
    docker_client.containers.get.side_effect = RuntimeError("daemon unavailable")
    path, headers = authorised("rdtclient")

    response = client.post(path, headers=headers)

    assert response.status_code == 502
    assert "daemon unavailable" in response.get_json()["message"]


def test_refuses_to_start_without_a_token(monkeypatch):
    monkeypatch.delenv("RESTART_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        restart_app.load_token()


def test_loads_token_from_environment(monkeypatch):
    monkeypatch.setenv("RESTART_TOKEN", "from-env")
    assert restart_app.load_token() == "from-env"
