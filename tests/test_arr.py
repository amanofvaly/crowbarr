from pathlib import Path

import httpx
import pytest

from crowbarr.arr import ArrClient, ManagedFile, map_path
from crowbarr.config import ArrConnection, PathMapping, Settings
from crowbarr.db import Database
from crowbarr.library import scan_arr


def client(provider, routes, monitored=True, mappings=None):
    def handler(request):
        assert request.method == "GET"
        assert request.headers["X-Api-Key"] == "private"
        key = request.url.path + ("?" + request.url.query.decode() if request.url.query else "")
        return httpx.Response(200, json=routes[key])

    return ArrClient(
        provider,
        ArrConnection(url="http://arr", api_key="private", monitored_only=monitored, mappings=mappings or []),
        transport=httpx.MockTransport(handler),
    )


def test_radarr_imported_monitored_movies_only():
    routes = {
        "/api/v3/movie": [
            {
                "id": 1,
                "title": "Imported",
                "monitored": True,
                "hasFile": True,
                "movieFile": {"id": 10, "path": "/movies/Imported/movie.mkv"},
            },
            {"id": 2, "monitored": True, "hasFile": False},
            {
                "id": 3,
                "title": "Unmonitored",
                "monitored": False,
                "hasFile": True,
                "path": "/movies/Unmonitored",
                "movieFile": {"id": 30, "relativePath": "movie.mkv"},
            },
        ]
    }
    with client("radarr", routes) as adapter:
        files = adapter.catalog()
        assert [f.file_id for f in files] == [10]
    with client("radarr", routes, monitored=False) as adapter:
        files = adapter.catalog()
        assert [f.file_id for f in files] == [10, 30]
        assert files[1].remote_path == "/movies/Unmonitored/movie.mkv"


def test_sonarr_monitored_episodes_and_multi_episode_dedup():
    routes = {
        "/api/v3/series": [
            {"id": 1, "monitored": True, "path": "/tv/Show", "title": "Show"},
            {"id": 2, "monitored": False},
        ],
        "/api/v3/episodefile?seriesId=1": [
            {"id": 10, "relativePath": "Season 1/double.mkv"},
            {"id": 11, "relativePath": "Season 1/unmonitored.mkv"},
        ],
        "/api/v3/episode?seriesId=1": [
            {"episodeFileId": 10, "monitored": True, "hasFile": True},
            {"episodeFileId": 10, "monitored": True, "hasFile": True},
            {"episodeFileId": 11, "monitored": False, "hasFile": True},
            {"episodeFileId": 0, "monitored": True, "hasFile": False},
        ],
    }
    with client("sonarr", routes) as adapter:
        files = adapter.catalog()
    assert len(files) == 1
    assert files[0].file_id == 10
    assert files[0].path == "/tv/Show/Season 1/double.mkv"


def test_mapping_uses_longest_directory_prefix(tmp_path):
    connection = ArrConnection(
        mappings=[
            PathMapping(remote="/tv", local=str(tmp_path / "tv")),
            PathMapping(remote="/tv/anime", local=str(tmp_path / "anime")),
        ]
    )
    assert map_path("/tv/anime/Show/video.mkv", connection) == tmp_path / "anime/Show/video.mkv"
    assert map_path("/tv-old/Show/video.mkv", connection) == Path("/tv-old/Show/video.mkv")


def test_windows_mapping_is_case_insensitive_and_traversal_rejected(tmp_path):
    connection = ArrConnection(mappings=[PathMapping(remote=r"D:\TV", local=str(tmp_path))])
    assert map_path(r"d:\tv\Show\video.mkv", connection) == tmp_path / "Show/video.mkv"
    with pytest.raises(ValueError):
        map_path(r"D:\TV\..\private\video.mkv", connection)
    with pytest.raises(ValueError):
        map_path(r"E:\unmapped\video.mkv", connection)


def test_mapping_symlink_cannot_escape(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path)
    connection = ArrConnection(mappings=[PathMapping(remote="/tv", local=str(root))])
    with pytest.raises(ValueError):
        map_path("/tv/escape/private.mkv", connection)


def test_api_failure_is_not_an_empty_catalog():
    transport = httpx.MockTransport(lambda _: httpx.Response(503))
    with ArrClient("radarr", ArrConnection(url="http://arr", api_key="private"), transport) as adapter:
        with pytest.raises(httpx.HTTPStatusError):
            adapter.catalog()


class Catalog:
    files = []
    fail = False

    def __init__(self, *args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def catalog(self):
        if self.fail:
            raise ConnectionError("not reachable")
        return self.files


@pytest.fixture
def catalog_library(tmp_path):
    video = tmp_path / "library" / "movie.mkv"
    video.parent.mkdir()
    video.write_bytes(b"media")
    settings = Settings(
        radarr=ArrConnection(
            url="http://arr",
            api_key="private",
            mappings=[PathMapping(remote="/movies", local=str(video.parent))],
        ),
        settle_seconds=0,
        subtitle_wait_minutes=0,
    )
    db = Database(tmp_path / "state" / "queue.db")

    class Factory(Catalog):
        files = [ManagedFile("radarr", 10, 1, str(video), "/movies/movie.mkv", "Movie")]

    return video, settings, db, Factory


def test_catalog_enqueues_automatically_and_provider_is_visible(catalog_library):
    video, settings, db, factory = catalog_library
    assert settings.roots == []  # Mapping destinations also authorize media access.
    assert scan_arr(settings, db, factory) == 1
    assert scan_arr(settings, db, factory) == 1
    assert len(db.snapshot()["jobs"]) == 1
    assert db.snapshot()["jobs"][0]["manager"] == "radarr"
    assert db.claim(settings.providers())["media"] == str(video)


def test_outage_holds_new_jobs_without_erasing_catalog(catalog_library):
    video, settings, db, factory = catalog_library
    scan_arr(settings, db, factory)
    factory.fail = True
    assert scan_arr(settings, db, factory) == 0
    assert db.claim(settings.providers()) is None
    assert db.eligible(str(video), settings.providers(), healthy=False)
    assert db.snapshot()["integrations"][0]["file_count"] == 1
    assert db.snapshot()["jobs"][0]["state"] == "queued"
    factory.fail = False
    scan_arr(settings, db, factory)
    assert db.claim(settings.providers())


def test_successful_removal_cancels_pending_job(catalog_library):
    _, settings, db, factory = catalog_library
    scan_arr(settings, db, factory)
    factory.files = []
    scan_arr(settings, db, factory)
    assert db.snapshot()["jobs"][0]["state"] == "superseded"
    assert db.claim(settings.providers()) is None


def test_other_folder_jobs_cannot_bypass_arr_scope(catalog_library):
    _, settings, db, factory = catalog_library
    other = db.enqueue("/unmanaged/movie.mkv", "one", None, 0)
    scan_arr(settings, db, factory)
    assert db.get(other)["state"] == "superseded"
    assert db.claim(settings.providers())["id"] != other


def test_upgrade_replaces_pending_revision(catalog_library):
    video, settings, db, factory = catalog_library
    scan_arr(settings, db, factory)
    video.write_bytes(b"upgraded-release-with-a-different-cut")
    factory.files = [ManagedFile("radarr", 11, 1, str(video), "/movies/movie.mkv", "Movie")]
    scan_arr(settings, db, factory)
    assert [j["state"] for j in db.snapshot()["jobs"]] == ["queued"]


def test_invisible_import_is_reported_not_queued(catalog_library):
    video, settings, db, factory = catalog_library
    video.unlink()
    assert scan_arr(settings, db, factory) == 0
    assert "not visible" in db.snapshot()["notices"][0]["message"]
    assert not db.snapshot()["jobs"]


def test_old_sync_cannot_authorize_jobs_after_settings_change(catalog_library):
    video, settings, db, factory = catalog_library
    old_settings = settings.model_copy(deep=True)
    settings.radarr.api_key = "replacement-key"
    db.invalidate_sync()
    # An already running request finishes after the configuration was saved.
    scan_arr(old_settings, db, factory)
    assert db.claim(settings.providers(), settings.discovery_fingerprint()) is None
    scan_arr(settings, db, factory)
    assert db.claim(settings.providers(), settings.discovery_fingerprint())["media"] == str(video)


def test_existing_catalog_schema_migrates(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE provider_sync (provider TEXT PRIMARY KEY, last_attempt REAL NOT NULL, "
            "last_success REAL, healthy INTEGER NOT NULL, file_count INTEGER NOT NULL, error TEXT)"
        )
        connection.execute("INSERT INTO provider_sync VALUES ('radarr',1,1,1,0,NULL)")
    db = Database(path)
    db.sync_failed("radarr", "offline")
    assert db.snapshot()["integrations"][0]["last_success"] == 1
    db.replace_catalog("radarr", [], "current")
    assert db.snapshot()["integrations"][0]["healthy"] == 1


def test_connection_test_names_the_mapping_a_library_needs(tmp_path):
    """An empty library and a correct one look identical. Asking the manager where its
    libraries are turns silence into an instruction."""
    import httpx

    from crowbarr.config import Settings
    from crowbarr.integrations import check_connection

    def handler(request):
        if request.url.path.endswith("/system/status"):
            return httpx.Response(200, json={"version": "4.0"})
        return httpx.Response(200, json=[{"path": "/data/tv"}])

    transport = httpx.MockTransport(handler)
    original = httpx.Client

    class Client(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    httpx.Client = Client
    try:
        settings = Settings(roots=[str(tmp_path / "tv")], sonarr={"url": "http://arr", "api_key": "k"})
        result = check_connection("sonarr", settings.sonarr, settings)
    finally:
        httpx.Client = original

    assert result["ok"] is False
    assert "/data/tv" in result["message"]
    assert "path mapping" in result["message"]
    assert str(tmp_path / "tv") in result["message"]
