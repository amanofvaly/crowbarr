import base64

import pytest
from fastapi.testclient import TestClient

from crowbarr.app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, background=False)
    with TestClient(app) as client:
        yield client


def auth(client):
    return {"X-Api-Key": client.app.state.store.token}


def test_dashboard_and_health_public_but_data_private(client):
    assert client.get("/").status_code == 200
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers=auth(client)).status_code == 200
    assert client.get("/static/vendor/bootstrap.min.css").status_code == 200


def test_settings_secrets_are_write_only_and_preserved(client, tmp_path):
    payload = {"roots": [str(tmp_path)], "sonarr": {"url": "http://sonarr:8989", "api_key": "secret-value"}}
    response = client.put("/api/settings", headers=auth(client), json=payload)
    assert response.status_code == 200
    assert "secret-value" not in response.text
    assert response.json()["sonarr"]["has_api_key"]
    payload["sonarr"]["api_key"] = ""
    client.put("/api/settings", headers=auth(client), json=payload)
    assert client.app.state.store.get().sonarr.api_key == "secret-value"
    assert "secret-value" not in client.get("/api/settings", headers=auth(client)).text


def test_invalid_settings_do_not_echo_credentials(client):
    response = client.put(
        "/api/settings",
        headers=auth(client),
        json={"sonarr": {"url": "http://sonarr", "api_key": "secret-value"}, "device": "bad"},
    )
    assert response.status_code == 422
    assert "secret-value" not in response.text


@pytest.mark.parametrize("name", ["sonarr", "radarr", "bazarr"])
def test_arr_basic_auth_hooks_and_test_event(client, name):
    token = client.app.state.store.token
    headers = {"Authorization": "Basic " + base64.b64encode(f"crowbarr:{token}".encode()).decode()}
    service = client.app.state.service
    assert client.post(f"/api/hooks/{name}", json={"eventType": "Test"}, headers=headers).status_code == 202
    assert not service.scan_event.is_set()
    assert (
        client.post(f"/api/hooks/{name}", json={"eventType": "Download"}, headers=headers).status_code == 202
    )
    assert service.scan_event.is_set()


def test_hooks_reject_wrong_auth_and_unknown_integrations(client):
    assert client.post("/api/hooks/sonarr", json={}).status_code == 401
    assert client.post("/api/hooks/unknown", json={}, headers=auth(client)).status_code == 404


def test_pause_is_persistent(client):
    assert client.post("/api/pause", headers=auth(client)).json()["paused"]
    assert client.app.state.store.get().paused


def test_security_headers_and_request_size(client):
    response = client.get("/")
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert client.post("/api/hooks/bazarr", content="x" * 70000, headers=auth(client)).status_code == 413


def test_cpu_half_precision_is_rejected(client):
    response = client.put(
        "/api/settings", headers=auth(client), json={"device": "cpu", "compute_type": "float16"}
    )
    assert response.status_code == 422


def test_chunked_request_limit_cannot_be_bypassed(client):
    response = client.post(
        "/api/hooks/sonarr", content=iter([b"x" * 40000, b"x" * 40000]), headers=auth(client)
    )
    assert response.status_code == 413


def test_arr_mappings_and_monitoring_round_trip_without_exposing_keys(client, tmp_path):
    response = client.put(
        "/api/settings",
        headers=auth(client),
        json={
            "sonarr": {
                "url": "http://sonarr",
                "api_key": "unique-arr-test-key",
                "monitored_only": False,
                "mappings": [{"remote": "/tv", "local": str(tmp_path)}],
            }
        },
    )
    assert response.status_code == 200
    assert response.json()["sonarr"]["mappings"][0]["remote"] == "/tv"
    assert response.json()["sonarr"]["monitored_only"] is False
    assert "unique-arr-test-key" not in response.text
    status = client.get("/api/status", headers=auth(client)).json()
    assert status["discovery_mode"] == "arr"
    assert status["configured"]
