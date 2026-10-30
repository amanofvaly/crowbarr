"""Run the real app and update it from inside, against a local stand-in for GitHub."""

import hashlib
import importlib.metadata
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "posix", reason="re-executes the process in place")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_wheel(version: str, main_override: str = "") -> bytes:
    """A wheel of the current source, labelled `version`."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        for path in sorted((ROOT / "crowbarr").rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            name = path.relative_to(ROOT).as_posix()
            content = path.read_bytes()
            if name == "crowbarr/version.py":
                text = content.decode().replace(
                    'APPLICATION_VERSION = "', f'APPLICATION_VERSION = "{version}" #', 1
                )
                content = text.encode()
            if name == "crowbarr/__main__.py" and main_override:
                content = main_override.encode()
            wheel.writestr(name, content)
        wheel.writestr(f"crowbarr-{version}.dist-info/METADATA", f"Name: crowbarr\nVersion: {version}\n")
    return buffer.getvalue()


class FakeGitHub:
    def __init__(self, version: str, wheel: bytes):
        self.port = free_port()
        base = f"http://127.0.0.1:{self.port}"
        manifest = {
            "version": version,
            "wheel": f"crowbarr-{version}-py3-none-any.whl",
            "sha256": hashlib.sha256(wheel).hexdigest(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "runtime": {"cpu": [f"fastapi=={importlib.metadata.version('fastapi')}"]},
        }
        self.routes = {
            "/repos/amanofvaly/crowbarr/releases/latest": json.dumps(
                {
                    "tag_name": f"v{version}",
                    "body": "Test release",
                    "html_url": f"{base}/release",
                    "published_at": "2026-10-31T00:00:00Z",
                    "assets": [
                        {"name": "crowbarr-update.json", "browser_download_url": f"{base}/dl/manifest"},
                        {"name": manifest["wheel"], "browser_download_url": f"{base}/dl/wheel"},
                    ],
                }
            ).encode(),
            "/dl/manifest": json.dumps(manifest).encode(),
            "/dl/wheel": wheel,
        }
        routes = self.routes

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = routes.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.end_headers()
                self.wfile.write(body or b"")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class App:
    """The real service in its own process, as a container would run it."""

    def __init__(self, data: Path, github: FakeGitHub, port: int):
        self.data, self.port = data, port
        self.env = {
            **os.environ,
            "CROWBARR_DATA": str(data),
            "CROWBARR_IMAGE_VARIANT": "cpu",
            "CROWBARR_UPDATE_API": f"http://127.0.0.1:{github.port}",
            "PYTHONPATH": str(ROOT),
        }
        for name in ("CROWBARR_OTA", "CROWBARR_ORIG_PYTHONPATH"):
            self.env.pop(name, None)
        self.process = None
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5)

    def start(self):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "crowbarr", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=self.data, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return self

    def version(self, seconds=40):
        """The version the app reports once it answers, or None."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                return self.client.get("/health").json()["version"]
            except (httpx.HTTPError, ValueError, KeyError):
                if self.process.poll() is not None:
                    return None
                time.sleep(0.2)
        return None

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def sign_up(self):
        self.client.post("/api/setup", json={"username": "tester", "password": "test-password-1"}).raise_for_status()


@pytest.fixture
def stand_in(tmp_path):
    made = []

    def make(version, main_override=""):
        github = FakeGitHub(version, build_wheel(version, main_override))
        data = tmp_path / f"data-{len(made)}"
        data.mkdir()
        app = App(data, github, free_port())
        made.append((github, app))
        return github, app

    yield make
    for github, app in made:
        app.stop()
        github.close()


def test_the_app_updates_itself_in_place_and_keeps_the_update_across_restarts(stand_in):
    github, app = stand_in("9.9.9")
    base = app.start().version()
    assert base is not None and base != "9.9.9"
    app.sign_up()

    found = app.client.post("/api/update/check").json()
    assert found["available"] and found["can_install"], found
    assert app.client.post("/api/update/install").status_code == 202

    pid = app.process.pid
    deadline = time.monotonic() + 40
    version = base
    while version == base and time.monotonic() < deadline:
        time.sleep(0.5)
        version = app.version(seconds=2) or base
    assert version == "9.9.9"
    # The same process carried on, so no container restart was needed.
    assert app.process.poll() is None and app.process.pid == pid
    now = app.client.get("/api/update").json()
    assert now["current"] == "9.9.9" and now["running_update"] == "9.9.9" and not now["available"]

    # A container restart starts the image again, which switches into the update.
    app.stop()
    assert app.start().version() == "9.9.9"


def test_an_update_that_cannot_start_is_given_up_and_the_app_returns_to_the_image(stand_in):
    github, app = stand_in("9.9.8", main_override="import sys\nsys.exit(7)\n")
    base = app.start().version()
    app.sign_up()
    app.client.post("/api/update/check")
    assert app.client.post("/api/update/install").status_code == 202

    # The broken version exits straight away, as it would in a container that then restarts.
    assert app.process.wait(timeout=40) == 7
    app.process = None
    assert app.start().version(seconds=15) is None
    assert app.process.wait(timeout=40) == 7

    # After two failed starts the image runs as it did before.
    assert app.start().version() == base
    state = json.loads((app.data / "updates" / "state.json").read_text())
    assert state["active"] is None
    shown = app.client.get("/api/update").json()
    assert shown["failed"]["version"] == "9.9.8"
    assert "did not stay running" in shown["failed"]["reason"]
