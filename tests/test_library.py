import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from crowbarr.app import create_app
from crowbarr.catalog import identity, language_codes
from crowbarr.db import Database, StaleJob
from crowbarr.library import queue_media


@pytest.fixture
def library_client(tmp_path):
    app = create_app(tmp_path / "config", background=False)
    settings = app.state.store.get()
    settings.roots = [str(tmp_path)]
    settings.settle_seconds = 0
    settings.subtitle_wait_minutes = 0
    app.state.store.save(settings)
    records = []
    for season, episode in [(2, 10), (1, 2), (1, 1), (2, 1)]:
        path = tmp_path / f"Family Man S{season:02}E{episode:02}.mkv"
        path.write_bytes(b"test")
        records.append(dict(provider="sonarr", file_id=season * 100 + episode, item_id=1,
                            path=str(path), remote_path=str(path), title="The Family Man",
                            season=season, episodes=[episode], episode_title=f"Episode {episode}",
                            audio_languages=["hi"]))
    app.state.db.replace_catalog("sonarr", records)
    for r in records:
        app.state.db.enqueue(r["path"], "sig", None, 0)
    app.state.db.refresh_library_policy()
    with TestClient(app, headers={"X-Api-Key": app.state.store.token}) as client:
        yield client


def pref(client, scope, target, decision):
    response = client.put("/api/library/preferences", json=dict(scope=scope, target=target, decision=decision))
    assert response.status_code == 200, response.text


def test_hierarchy_and_language_defaults(library_client):
    result = library_client.get("/api/library").json()
    assert result["total"] == 1
    show = result["results"][0]
    assert show["title"] == "The Family Man"
    assert [(f["season"], f["episodes"]) for f in show["files"]] == [(1, [1]), (1, [2]), (2, [1]), (2, [10])]
    assert all(f["skipped"] and f["decision_scope"] == "audio" for f in show["files"])
    assert library_client.app.state.db.claim() is None
    assert library_client.get("/api/library?kind=movies").json()["total"] == 0


def test_specific_overrides_persist_and_cover_future_imports(library_client, tmp_path):
    c = library_client
    pref(c, "title", "sonarr:1", "skip")
    pref(c, "season", "sonarr:1:1", "include")
    show = c.get("/api/library").json()["results"][0]
    first = show["files"][0]
    pref(c, "file", first["path"], "skip")
    files = c.get("/api/library").json()["results"][0]["files"]
    assert [f["skipped"] for f in files] == [True, False, True, True]
    assert c.app.state.db.claim()["media"] == files[1]["path"]
    restarted = Database(c.app.state.db.path)
    future = tmp_path / "Family Man S03E01.mkv"
    future.write_bytes(b"test")
    with restarted.connect() as connection:
        connection.execute("INSERT INTO managed_media VALUES ('sonarr',301,1,?,?,?)",
                           (str(future), str(future), "The Family Man"))
    queue_media(future, c.app.state.store.get(), restarted, time.time())
    restarted.refresh_library_policy()
    with restarted.connect() as connection:
        assert connection.execute("SELECT state FROM jobs WHERE media=?", (str(future),)).fetchone()[0] == "skipped"
    pref(c, "file", first["path"], "auto")
    assert not c.get("/api/library").json()["results"][0]["files"][0]["skipped"]


def test_audio_toggle_and_manual_requests_respect_rules(library_client):
    c = library_client
    file = c.get("/api/library").json()["results"][0]["files"][0]
    assert c.post("/api/process", json={"media": file["path"]}).status_code == 409
    assert c.post(f"/api/jobs/{file['job_id']}/retry").status_code == 409
    c.put("/api/library/preferences", json={"scope": "audio", "enabled": False}).raise_for_status()
    assert not c.get("/api/library").json()["results"][0]["files"][0]["skipped"]
    pref(c, "title", "sonarr:1", "skip")
    assert c.app.state.db.claim() is None
    pref(c, "file", file["path"], "include")
    assert c.post("/api/process", json={"media": file["path"]}).status_code == 202


def test_active_skip_blocks_worker_publication(library_client):
    c = library_client
    pref(c, "title", "sonarr:1", "include")
    job = c.app.state.db.claim()
    worker = Database(c.app.state.db.path)
    worker.bind_worker(job)
    pref(c, "title", "sonarr:1", "skip")
    assert c.app.state.db.get(job["id"])["cancel_requested"] == 1
    with pytest.raises(StaleJob):
        worker.update(job["id"], state="completed")
    c.app.state.db.recover()
    assert c.app.state.db.get(job["id"])["state"] == "skipped"
    pref(c, "title", "sonarr:1", "include")
    assert c.app.state.db.get(job["id"])["state"] == "queued"


def test_episode_skip_survives_upgrade_and_shared_file(library_client, tmp_path):
    c = library_client
    pref(c, "title", "sonarr:1", "include")
    first = c.get("/api/library").json()["results"][0]["files"][0]
    pref(c, "file", first["path"], "skip")
    replacement = tmp_path / "Upgraded S01E01E02.mkv"
    replacement.write_bytes(b"replacement")
    c.app.state.db.replace_catalog("sonarr", [dict(provider="sonarr", file_id=999, item_id=1,
        path=str(replacement), remote_path=str(replacement), title="The Family Man", season=1, episodes=[1, 2])])
    queue_media(replacement, c.app.state.store.get(), c.app.state.db, time.time())
    c.app.state.db.refresh_library_policy()
    show = next(s for s in c.get("/api/library").json()["results"] if s["key"] == "sonarr:1")
    assert show["files"][0]["skipped"]
    assert "shared episode file" in show["files"][0]["reason"]


def test_background_episode_order_and_manual_priority(library_client):
    c = library_client
    pref(c, "title", "sonarr:1", "include")
    db = c.app.state.db
    show = c.get("/api/library").json()["results"][0]
    files = show["files"]
    db.promote(files[-1]["job_id"])
    expected = [files[-1], *files[:-1]]
    assert [j["id"] for j in c.get("/api/jobs?state=queue").json()["results"]] == [f["job_id"] for f in expected]
    assert [j["id"] for j in db.snapshot()["jobs"]][:4] == [f["job_id"] for f in expected]
    for file in expected:
        job = db.claim()
        assert job["media"] == file["path"]
        db.update(job["id"], state="unchanged")


def test_audio_probe_before_expensive_work_and_override(library_client, monkeypatch):
    from crowbarr import processor

    c = library_client
    db = c.app.state.db
    file = c.get("/api/library").json()["results"][0]["files"][0]
    with db.connect() as connection:
        connection.execute("DELETE FROM library_metadata")
    db.refresh_library_policy()
    job = db.claim()
    monkeypatch.setattr(processor, "current", lambda *args: True)
    monkeypatch.setattr(processor, "probe", lambda *args: {"streams": [
        {"codec_type": "audio", "index": 1, "tags": {"language": "hin"}}]})
    monkeypatch.setattr(processor, "extract_audio", lambda *args: pytest.fail("Must skip before decoding"))
    result = processor.process(job, c.app.state.store.get(), c.app.state.store.directory, db)
    assert result["state"] == "skipped"
    db.update(job["id"], **result)
    pref(c, "file", file["path"], "include")
    assert not db.cache_audio(file["path"], {"streams": [
        {"codec_type": "audio", "index": 1, "tags": {"language": "hin"}}]})["skipped"]


@pytest.mark.parametrize("languages,skipped", [(["hin"], True), (["hin", "eng"], False),
                                              (["hin", "und"], False), ([], False)])
def test_multiaudio_and_unknown_are_conservative(library_client, languages, skipped):
    c = library_client
    file = c.get("/api/library").json()["results"][0]["files"][0]
    metadata = {"streams": [{"codec_type": "audio", "index": i, "tags": {"language": lang}}
                             for i, lang in enumerate(languages)]}
    assert c.app.state.db.cache_audio(file["path"], metadata)["skipped"] == skipped


def test_subtitle_download_auth_integrity_and_missing(library_client, tmp_path):
    c = library_client
    file = c.get("/api/library").json()["results"][0]["files"][0]
    path = tmp_path / "Family Man S01E01.crowbarr.en.srt"
    content = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"
    path.write_bytes(content)
    db = c.app.state.db
    db.update(file["job_id"], state="completed", output=str(path),
              report=json.dumps({"output_sha256": hashlib.sha256(content).hexdigest()}))
    url = f"/api/jobs/{file['job_id']}/subtitle"
    assert c.get(url).content == content
    assert "attachment" in c.get(url).headers["content-disposition"]
    assert c.get(url, headers={"X-Api-Key": "wrong"}).status_code == 401
    path.write_text("externally changed")
    assert c.get(url).status_code == 404
    path.unlink()
    assert c.get(url).status_code == 404


def test_audited_embedded_download_and_tamper(library_client):
    c = library_client
    file = c.get("/api/library").json()["results"][0]["files"][0]
    path = c.app.state.store.directory / "subtitles" / "audited.srt"
    path.parent.mkdir()
    content = b"1\n00:00:01,000 --> 00:00:02,000\nAudited dialogue\n"
    path.write_bytes(content)
    report = dict(audited_subtitle=str(path), audited_subtitle_sha256=hashlib.sha256(content).hexdigest(),
                  selected_source_kind="embedded")
    c.app.state.db.update(file["job_id"], state="unchanged", report=json.dumps(report))
    item = c.get("/api/library").json()["results"][0]["files"][0]
    assert item["download"]
    assert c.get(item["download"]).content == content
    path.write_text("changed")
    assert c.get(item["download"]).status_code == 404


def test_legacy_authored_download_requires_unchanged_inputs(library_client):
    from pathlib import Path

    from crowbarr.library import signature

    c = library_client
    file = c.get("/api/library").json()["results"][0]["files"][0]
    media = Path(file["path"])
    source = media.with_suffix(".en.srt")
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nOriginal dialogue\n")
    settings = c.app.state.store.get()
    job_id = c.app.state.db.enqueue(str(media), signature(media, source, settings), str(source), 0)
    c.app.state.db.update(job_id, state="unchanged", output=None,
                         report=json.dumps({"selected_source_kind": "external", "selected_source": str(source)}))
    url = f"/api/jobs/{job_id}/subtitle"
    assert c.get(url).content == source.read_bytes()
    source.write_text("changed")
    assert c.get(url).status_code == 404


def test_legacy_embedded_download_extracts_only_selected_track(library_client, monkeypatch):
    import subprocess

    from crowbarr import processor

    c = library_client
    file = c.get("/api/library").json()["results"][0]["files"][0]
    c.app.state.db.update(file["job_id"], state="unchanged", output=None,
                         report=json.dumps({"selected_source_kind": "embedded", "selected_source": "embedded stream 4"}))
    monkeypatch.setattr(processor, "current", lambda *args: True)
    content = b"1\n00:00:01,000 --> 00:00:02,000\nEmbedded dialogue\n"
    def extract(command, **kwargs):
        assert command[0] == "ffmpeg" and command[command.index("-map") + 1] == "0:4"
        assert kwargs["timeout"] == 60 and "-fs" in command
        return subprocess.CompletedProcess(command, 0, stdout=content)
    monkeypatch.setattr(subprocess, "run", extract)
    assert c.get(f"/api/jobs/{file['job_id']}/subtitle").content == content


def test_artwork_is_authenticated_and_uses_only_saved_manager(library_client, monkeypatch):
    import httpx

    c = library_client
    settings = c.app.state.store.get()
    settings.sonarr.url = "http://sonarr:8989"
    settings.sonarr.api_key = "private-manager-key"
    c.app.state.store.save(settings)
    original = httpx.Client
    def upstream(request):
        assert str(request.url) == "http://sonarr:8989/api/v3/mediacover/1/poster.jpg"
        assert request.headers["X-Api-Key"] == "private-manager-key"
        return httpx.Response(200, content=b"poster", headers={"Content-Type": "image/jpeg"})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs))
    response = c.get("/api/library/artwork/sonarr/1")
    assert response.status_code == 200 and response.content == b"poster"
    assert "private-manager-key" not in str(response.headers)
    assert c.get("/api/library/artwork/sonarr/999").status_code == 404
    assert c.get("/api/library/artwork/plex/1").status_code == 404
    assert c.get("/api/library/artwork/sonarr/1", headers={"X-Api-Key": "bad"}).status_code == 401


def test_validation_search_and_folder_identity(library_client):
    c = library_client
    assert c.get("/api/library?q=Family%20S01E02").json()["total"] == 1
    assert c.get("/api/library?q=%25").json()["total"] == 0
    assert c.get("/api/library?kind=bad").status_code == 422
    assert c.put("/api/library/preferences", json={"scope": "file", "target": "/etc/passwd", "decision": "skip"}).status_code == 404
    a = identity({"path": "/tv/Family Man/Season 1/Family.Man.S01E02.mkv"})
    b = identity({"path": "/tv/Family Man/Season 2/Family.Man.S02E01E02.mkv"})
    assert a["key"] == b["key"]
    assert b["episodes"] == [1, 2]
    assert language_codes("Hindi / English") == ["en", "hi"]
