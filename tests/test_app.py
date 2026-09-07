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


def test_publishing_survives_a_filesystem_that_refuses_chmod(tmp_path, monkeypatch):
    """TrueNAS ships ZFS datasets with NFSv4 ACLs, where chmod raises EPERM."""
    import os as _os

    from crowbarr.config import atomic_write

    def refuse(*_args, **_kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(_os, "fchmod", refuse)
    target = tmp_path / "Show" / "episode.crowbarr.en.srt"
    atomic_write(target, "1\n00:00:01,000 --> 00:00:02,000\nhello\n", mode=0o644)
    assert target.read_text().endswith("hello\n")


def test_private_files_are_never_left_exposed_when_chmod_is_refused(tmp_path, monkeypatch):
    import os as _os

    from crowbarr.config import atomic_write

    real_fstat = _os.fstat

    def refuse(*_args, **_kwargs):
        raise PermissionError(1, "Operation not permitted")

    class Exposed:
        st_mode = 0o100770

    monkeypatch.setattr(_os, "fchmod", refuse)
    monkeypatch.setattr(_os, "fstat", lambda fd: Exposed() if fd else real_fstat(fd))
    with pytest.raises(PermissionError):
        atomic_write(tmp_path / "settings.json", "{}", mode=0o600)


def test_library_search_and_manual_requests_are_available_from_the_dashboard(client, tmp_path):
    """The spec's manual request must be reachable in Crowbarr, not only by curl."""
    media = tmp_path / "Show" / "Show - S01E01 - Pilot.mkv"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"video")
    client.put("/api/settings", headers=auth(client), json={"roots": [str(tmp_path)]})
    db = client.app.state.db
    db.enqueue(str(media), "sig", None, 0)

    found = client.get("/api/media", headers=auth(client), params={"q": "Pilot"}).json()["results"]
    assert [item["path"] for item in found] == [str(media)]

    audit = client.post("/api/process", headers=auth(client), json={"media": str(media)})
    assert audit.status_code == 202
    assert db.get(audit.json()["job_id"])["priority"] == 100

    fresh = client.post(
        "/api/process", headers=auth(client), json={"media": str(media), "directive": "generate"}
    )
    assert fresh.status_code == 202
    job = db.get(fresh.json()["job_id"])
    assert job["directive"] == "generate"
    assert job["state"] == "queued"


def test_generate_directive_ignores_an_existing_subtitle(client, tmp_path):
    """A forced regeneration must not be short-circuited by the subtitle already there."""
    media = tmp_path / "Show" / "Show - S01E02 - Next.mkv"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"video")
    media.with_suffix(".en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n")
    client.put("/api/settings", headers=auth(client), json={"roots": [str(tmp_path)]})
    db = client.app.state.db
    response = client.post(
        "/api/process", headers=auth(client), json={"media": str(media), "directive": "generate"}
    )
    job = db.get(response.json()["job_id"])
    # The job still records the authored source, but the directive tells the processor
    # to skip every discovered candidate and transcribe instead.
    assert job["directive"] == "generate"
    assert job["state"] == "queued"


def test_first_run_creates_a_login_instead_of_demanding_the_api_key(client):
    """The API key is pasted into Sonarr/Radarr/Bazarr; it must not be the human login."""
    assert client.get("/api/session").json()["configured"] is False
    assert client.get("/api/status").status_code == 401

    created = client.post("/api/setup", json={"username": "aman", "password": "correct-horse"})
    assert created.status_code == 201
    assert client.get("/api/session").json()["configured"] is True
    # The cookie alone now authenticates; no key was ever typed in.
    assert client.get("/api/status").status_code == 200

    assert client.post("/api/setup", json={"username": "x", "password": "another-one"}).status_code == 400


def test_sign_in_rejects_a_wrong_password_and_the_api_key_still_works_for_machines(client):
    client.post("/api/setup", json={"username": "aman", "password": "correct-horse"})
    client.delete("/api/session")
    assert client.get("/api/status").status_code == 401

    assert client.post("/api/session", json={"username": "aman", "password": "wrong"}).status_code == 401
    assert client.post("/api/session", json={"username": "aman", "password": "correct-horse"}).status_code == 200
    assert client.get("/api/status").status_code == 200

    client.delete("/api/session")
    # Sonarr, Radarr and Bazarr cannot hold a cookie, so the key must still authenticate.
    assert client.get("/api/status", headers=auth(client)).status_code == 200
    assert client.get("/api/settings", headers=auth(client)).json()["api_key"]


def test_a_short_password_is_refused(client):
    response = client.post("/api/setup", json={"username": "aman", "password": "short"})
    assert response.status_code == 400
    assert "8 characters" in response.json()["detail"]
    assert client.get("/api/session").json()["configured"] is False
