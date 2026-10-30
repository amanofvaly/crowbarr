import hashlib
import importlib.metadata
import io
import json
import sys
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from crowbarr import updates
from crowbarr.app import create_app
from crowbarr.config import Settings


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(updates, "_job", {"status": "idle", "reason": ""})
    monkeypatch.setattr(updates.RESTART, "requested", False)
    monkeypatch.setattr(updates.RESTART, "server", None)
    for name in ("CROWBARR_OTA", "CROWBARR_ORIG_PYTHONPATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CROWBARR_IMAGE_VARIANT", "cpu")


def manifest_for(version="9.9.9", sha="0" * 64, **extra):
    installed = importlib.metadata.version("fastapi")
    return {
        "version": version,
        "wheel": f"crowbarr-{version}-py3-none-any.whl",
        "sha256": sha,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "runtime": {"cpu": [f"fastapi=={installed}"], "cuda": [f"fastapi=={installed}"]},
        **extra,
    }


def make_wheel(version="9.9.9", extra=None, version_text=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        wheel.writestr("crowbarr/__init__.py", "")
        wheel.writestr("crowbarr/version.py", version_text or f'APPLICATION_VERSION = "{version}"\n')
        wheel.writestr(f"crowbarr-{version}.dist-info/METADATA", "Name: crowbarr\n")
        for name, content in (extra or {}).items():
            wheel.writestr(name, content)
    return buffer.getvalue()


def serve(monkeypatch, routes):
    """Answer the update module's web requests from `routes`, a path -> bytes mapping."""

    def handler(request):
        body = routes.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        updates, "_client", lambda: httpx.Client(transport=transport, follow_redirects=True)
    )


def release_routes(wheel=None, manifest=None, version="9.9.9"):
    base = "https://github.test"
    manifest = manifest or manifest_for(version, hashlib.sha256(wheel or b"").hexdigest())
    return {
        "/repos/amanofvaly/crowbarr/releases/latest": json.dumps(
            {
                "tag_name": f"v{version}",
                "body": "Notes for the release",
                "html_url": f"{base}/release",
                "published_at": "2026-10-31T00:00:00Z",
                "assets": [
                    {"name": updates.MANIFEST, "browser_download_url": f"{base}/dl/manifest"},
                    {"name": manifest["wheel"], "browser_download_url": f"{base}/dl/wheel"},
                ],
            }
        ).encode(),
        "/dl/manifest": json.dumps(manifest).encode(),
        "/dl/wheel": wheel or b"",
    }


def test_versions_compare_as_numbers_not_text():
    assert updates.parse_version("v0.10.0") > updates.parse_version("0.9.9")
    assert updates.parse_version("1.0") == (1, 0)
    with pytest.raises(ValueError):
        updates.parse_version("main")


def test_the_latest_release_is_read_with_its_manifest(monkeypatch):
    serve(monkeypatch, release_routes(make_wheel()))

    release = updates.fetch_latest()

    assert release["version"] == "9.9.9"
    assert release["notes"] == "Notes for the release"
    assert release["manifest"]["version"] == "9.9.9"
    assert updates.MANIFEST in release["assets"]


def test_a_failed_lookup_says_so_without_raising_to_the_caller(tmp_path, monkeypatch):
    serve(monkeypatch, {})

    result = updates.check(tmp_path, force=True)

    assert "Could not read the latest release" in result["error"]
    assert result["latest"] is None


def test_a_matching_runtime_is_accepted_and_a_different_one_is_explained():
    assert updates.runtime_mismatch(manifest_for()) == ""

    other = manifest_for()
    other["runtime"]["cpu"] = ["fastapi==0.0.1"]
    assert "needs fastapi 0.0.1" in updates.runtime_mismatch(other)

    other["runtime"]["cpu"] = ["not-a-real-package-xyz==1"]
    assert "does not have" in updates.runtime_mismatch(other)

    other = manifest_for()
    other["python"] = "2.7"
    assert "Python 2.7" in updates.runtime_mismatch(other)

    assert "does not say" in updates.runtime_mismatch({"runtime": {}})


def test_status_offers_the_update_only_where_the_image_can_run_it(tmp_path, monkeypatch):
    serve(monkeypatch, release_routes(make_wheel()))
    updates.check(tmp_path, force=True)
    settings = Settings()

    state = updates.status(tmp_path, settings)
    assert state["available"] and state["can_install"] and state["kind"] == "image"
    assert state["image_tag"] == "ghcr.io/amanofvaly/crowbarr:9.9.9"

    monkeypatch.setenv("CROWBARR_IMAGE_VARIANT", "cuda")
    assert updates.status(tmp_path, settings)["image_tag"].endswith(":9.9.9-cuda")

    monkeypatch.delenv("CROWBARR_IMAGE_VARIANT")
    state = updates.status(tmp_path, settings)
    assert state["available"] and not state["can_install"] and state["kind"] == "manual"

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    state = updates.status(tmp_path, settings)
    assert state["kind"] == "native"
    assert "sudo crowbarr-update" in state["instructions"][0]


def test_an_older_or_equal_release_is_not_an_update(tmp_path, monkeypatch):
    serve(monkeypatch, release_routes(make_wheel("0.0.1"), version="0.0.1"))
    updates.check(tmp_path, force=True)

    assert not updates.status(tmp_path, Settings())["available"]


def test_a_release_whose_packages_differ_cannot_install_from_inside(tmp_path, monkeypatch):
    changed = manifest_for()
    changed["runtime"]["cpu"] = ["fastapi==0.0.1"]
    serve(monkeypatch, release_routes(make_wheel(), manifest=changed))
    updates.check(tmp_path, force=True)

    state = updates.status(tmp_path, Settings())

    assert state["available"] and not state["can_install"]
    assert "needs fastapi" in state["reason"]
    assert state["instructions"]


def unpack(tmp_path, monkeypatch, wheel, manifest=None):
    routes = release_routes(wheel, manifest=manifest)
    serve(monkeypatch, routes)
    release = updates.fetch_latest()
    release["wheel_url"] = release["assets"][release["manifest"]["wheel"]]
    return updates.download_and_unpack(tmp_path, release)


def test_a_verified_wheel_is_unpacked_into_a_versioned_folder(tmp_path, monkeypatch):
    version = unpack(tmp_path, monkeypatch, make_wheel())

    assert version == "9.9.9"
    assert (tmp_path / "updates" / "9.9.9" / "crowbarr" / "version.py").is_file()
    assert [p.name for p in (tmp_path / "updates").iterdir()] == ["9.9.9"]


def test_a_download_that_does_not_match_its_checksum_is_discarded(tmp_path, monkeypatch):
    wheel = make_wheel()
    wrong = manifest_for(sha="f" * 64)

    with pytest.raises(updates.UpdateError, match="checksum"):
        unpack(tmp_path, monkeypatch, wheel, manifest=wrong)

    assert list((tmp_path / "updates").iterdir()) == []


@pytest.mark.parametrize(
    "extra",
    [
        {"../escape.py": "x"},
        {"crowbarr/../../escape.py": "x"},
        {"/absolute.py": "x"},
        {"other_package/__init__.py": "x"},
    ],
)
def test_files_outside_crowbarrs_own_package_are_refused(tmp_path, monkeypatch, extra):
    with pytest.raises(updates.UpdateError, match="outside"):
        unpack(tmp_path, monkeypatch, make_wheel(extra=extra))

    assert not (tmp_path / "escape.py").exists()
    assert [p for p in (tmp_path / "updates").iterdir() if p.is_dir()] == []


def test_a_wheel_that_reports_another_version_is_refused(tmp_path, monkeypatch):
    with pytest.raises(updates.UpdateError, match="does not match"):
        unpack(tmp_path, monkeypatch, make_wheel(version_text='APPLICATION_VERSION = "1.2.3"\n'))


def test_an_incompatible_release_is_refused_before_anything_downloads(tmp_path, monkeypatch):
    wheel = make_wheel()
    changed = manifest_for(sha=hashlib.sha256(wheel).hexdigest())
    changed["runtime"]["cpu"] = ["fastapi==0.0.1"]

    with pytest.raises(updates.UpdateError, match="needs fastapi"):
        unpack(tmp_path, monkeypatch, wheel, manifest=changed)


def test_activating_keeps_only_the_new_and_previous_versions(tmp_path):
    for version in ("1.0.0", "1.1.0"):
        (tmp_path / "updates" / version).mkdir(parents=True)
    updates.activate(tmp_path, "1.1.0")
    (tmp_path / "updates" / "1.2.0").mkdir()
    updates.activate(tmp_path, "1.2.0")

    assert sorted(p.name for p in (tmp_path / "updates").iterdir() if p.is_dir()) == ["1.1.0", "1.2.0"]
    state = updates.read_state(tmp_path)
    assert state["active"] == "1.2.0" and state["previous"] == "1.1.0" and state["boots"] == 0


@pytest.fixture
def capture_exec(monkeypatch):
    calls = []
    monkeypatch.setattr(updates.os, "execve", lambda path, argv, env: calls.append((path, argv, env)))
    return calls


def installed_update(tmp_path, version="9.9.9", **state):
    (tmp_path / "updates" / version).mkdir(parents=True)
    updates.write_state(tmp_path, {"active": version, "boots": 0, "confirmed": False, **state})


def test_start_up_switches_into_an_installed_update(tmp_path, capture_exec, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/existing")
    installed_update(tmp_path)

    updates.boot(tmp_path, ["--port", "9000"])

    path, argv, env = capture_exec[0]
    assert argv == [sys.executable, "-m", "crowbarr", "--port", "9000"]
    assert env["CROWBARR_OTA"] == "9.9.9"
    assert env["PYTHONPATH"].split(":")[0] == str(tmp_path / "updates" / "9.9.9")
    assert env["CROWBARR_ORIG_PYTHONPATH"] == "/existing"
    assert updates.read_state(tmp_path)["boots"] == 1


def test_start_up_does_nothing_without_an_update_or_when_already_switched(tmp_path, capture_exec, monkeypatch):
    updates.boot(tmp_path, [])
    installed_update(tmp_path)
    monkeypatch.setenv("CROWBARR_OTA", "9.9.9")
    updates.boot(tmp_path, [])
    monkeypatch.delenv("CROWBARR_OTA")
    monkeypatch.delenv("CROWBARR_IMAGE_VARIANT")
    updates.boot(tmp_path, [])

    assert capture_exec == []


def test_an_update_that_never_stays_up_is_dropped_and_the_reason_kept(tmp_path, capture_exec):
    installed_update(tmp_path, boots=updates.MAX_UNCONFIRMED_BOOTS)

    updates.boot(tmp_path, [])

    assert capture_exec == []
    state = updates.read_state(tmp_path)
    assert state["active"] is None
    assert "did not stay running" in state["failed"]["reason"]
    assert state["failed"]["version"] == "9.9.9"


def test_a_confirmed_update_keeps_starting_however_often_the_app_restarts(tmp_path, capture_exec):
    installed_update(tmp_path, boots=0, confirmed=True)
    for _ in range(5):
        updates.boot(tmp_path, [])
        state = updates.read_state(tmp_path)
        state["confirmed"] = True
        updates.write_state(tmp_path, state)

    assert len(capture_exec) == 5


def test_an_image_that_has_caught_up_drops_the_installed_copy(tmp_path, capture_exec):
    installed_update(tmp_path, version="0.0.1")

    updates.boot(tmp_path, [])

    assert capture_exec == []
    assert updates.read_state(tmp_path)["active"] is None


def test_staying_up_confirms_the_update(tmp_path, monkeypatch):
    installed_update(tmp_path)
    monkeypatch.setenv("CROWBARR_OTA", "9.9.9")

    class Immediate:
        def __init__(self, seconds, function):
            self.function, self.daemon = function, False

        def start(self):
            self.function()

    monkeypatch.setattr(updates.threading, "Timer", Immediate)
    updates.confirm_boot(tmp_path)

    assert updates.read_state(tmp_path)["confirmed"] is True


def test_restarting_goes_back_through_the_images_own_start_up(monkeypatch, capture_exec):
    monkeypatch.setenv("CROWBARR_OTA", "9.9.9")
    monkeypatch.setenv("CROWBARR_ORIG_PYTHONPATH", "/existing")
    monkeypatch.setenv("PYTHONPATH", "/updates/9.9.9:/existing")

    updates.restart_into(["--port", "1"])

    env = capture_exec[0][2]
    assert "CROWBARR_OTA" not in env and "CROWBARR_ORIG_PYTHONPATH" not in env
    assert env["PYTHONPATH"] == "/existing"


def test_a_restart_request_stops_the_server(monkeypatch):
    class Server:
        should_exit = False

    server = Server()
    monkeypatch.setattr(updates.RESTART, "server", server)

    updates.request_restart()

    assert server.should_exit and updates.RESTART.requested


def test_installing_records_the_version_and_asks_for_a_restart(tmp_path, monkeypatch):
    wheel = make_wheel()
    serve(monkeypatch, release_routes(wheel))
    release = updates.fetch_latest()
    release["wheel_url"] = release["assets"][release["manifest"]["wheel"]]

    updates.install(tmp_path, release)

    assert updates.job()["status"] == "restarting"
    assert updates.read_state(tmp_path)["active"] == "9.9.9"
    assert updates.RESTART.requested


def test_a_failed_install_reports_why_and_changes_nothing(tmp_path, monkeypatch):
    serve(monkeypatch, release_routes(make_wheel(), manifest=manifest_for(sha="f" * 64)))
    release = updates.fetch_latest()
    release["wheel_url"] = release["assets"][release["manifest"]["wheel"]]

    updates.install(tmp_path, release)

    assert updates.job()["status"] == "failed"
    assert "checksum" in updates.job()["reason"]
    assert updates.read_state(tmp_path) == {}
    assert not updates.RESTART.requested


def wait_for(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_the_install_button_downloads_and_restarts(tmp_path, monkeypatch):
    serve(monkeypatch, release_routes(make_wheel()))
    with TestClient(create_app(tmp_path, background=False)) as client:
        headers = {"X-Api-Key": client.app.state.store.token}
        assert client.post("/api/update/check", headers=headers).json()["available"]

        response = client.post("/api/update/install", headers=headers)
        assert response.status_code == 202
        assert wait_for(lambda: updates.job()["status"] == "restarting")

    assert updates.read_state(tmp_path)["active"] == "9.9.9"


def test_the_install_button_is_refused_when_nothing_can_install(tmp_path, monkeypatch):
    serve(monkeypatch, release_routes(make_wheel(), version="0.0.1"))
    with TestClient(create_app(tmp_path, background=False)) as client:
        headers = {"X-Api-Key": client.app.state.store.token}
        client.post("/api/update/check", headers=headers)
        response = client.post("/api/update/install", headers=headers)

    assert response.status_code == 409


def test_update_settings_default_to_checking_but_not_installing():
    settings = Settings()

    assert settings.update_check is True
    assert settings.auto_update is False


def test_the_background_check_installs_only_when_asked_to_and_the_worker_is_idle(tmp_path, monkeypatch):
    serve(monkeypatch, release_routes(make_wheel()))
    started = []
    monkeypatch.setattr(updates, "start_install", lambda directory: started.append(directory) or True)
    monkeypatch.setattr(updates, "CHECK_INTERVAL_SECONDS", 0.01)

    class Store:
        def __init__(self, **values):
            self.settings = Settings(**values)

        def get(self):
            return self.settings

    class Db:
        busy = False

        def connect(self):
            raise AssertionError("not used when the fake below is installed")

    class OnePass:
        """Lets the loop run its body once: the opening pause, one pass, then stop."""

        waits = 0

        def wait(self, timeout=None):
            self.waits += 1

        def is_set(self):
            return self.waits >= 2

    def run(store, busy):
        monkeypatch.setattr(updates, "_processing", lambda db: busy)
        updates.watch(tmp_path, store, Db(), OnePass())

    run(Store(auto_update=False), busy=False)
    assert started == []
    run(Store(auto_update=True), busy=True)
    assert started == []
    run(Store(auto_update=True), busy=False)
    assert started == [tmp_path]
    started.clear()
    run(Store(update_check=False, auto_update=True), busy=False)
    assert started == []
