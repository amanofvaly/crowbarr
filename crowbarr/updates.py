"""Find new releases, and install one over the air when the running image can use it.

The heavy runtime (Python, ffmpeg, the speech engine) rarely changes, and Crowbarr's own
code is a small wheel. A release publishes that wheel with a manifest that lists the
runtime packages it was built for. When the installed packages match, Crowbarr unpacks
the wheel into its data folder and restarts itself from there. When they do not, it
says so and shows how to update the image instead.

Trust: the wheel is fetched over HTTPS from this project's GitHub release and checked
against the hash in the release's manifest. Nothing signs it, so the trust placed in a
release is the trust placed in the GitHub account that publishes it.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path

import httpx

from .config import atomic_write

REPO = "amanofvaly/crowbarr"
IMAGE = "ghcr.io/amanofvaly/crowbarr"
MANIFEST = "crowbarr-update.json"
MAX_WHEEL_BYTES = 25 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
CONFIRM_AFTER_SECONDS = 30
CHECK_INTERVAL_SECONDS = 6 * 3600
# A new version that has not stayed up twice in a row is abandoned.
MAX_UNCONFIRMED_BOOTS = 2
CHECK_COOLDOWN_SECONDS = 10


class UpdateError(RuntimeError):
    """An update step failed. The message is safe to show on the settings page."""


def api_base() -> str:
    # The override exists so tests can point at a local server.
    return os.environ.get("CROWBARR_UPDATE_API", "https://api.github.com").rstrip("/")


def parse_version(text: str) -> tuple[int, ...]:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", str(text).strip())
    if not match:
        raise ValueError(f"Not a release version: {text!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def running_version() -> str:
    from .version import APPLICATION_VERSION

    return APPLICATION_VERSION


def install_kind() -> str:
    """image (Docker, TrueNAS, Unraid), native (the Linux package) or manual (source or pip)."""
    if getattr(sys, "frozen", False):
        return "native"
    if os.environ.get("CROWBARR_IMAGE_VARIANT") in {"cpu", "cuda"}:
        return "image"
    return "manual"


def image_tag(version: str) -> str:
    suffix = "-cuda" if os.environ.get("CROWBARR_IMAGE_VARIANT") == "cuda" else ""
    return f"{IMAGE}:{version}{suffix}"


def instructions(kind: str, version: str) -> list[str]:
    if kind == "native":
        return ["Run sudo crowbarr-update on the machine that runs Crowbarr."]
    if kind == "image":
        return [
            f"Change the image to {image_tag(version)} and save, or pull the image again "
            "and recreate the container.",
            "TrueNAS: Apps, Edit, change the image tag, Save. Unraid: edit the container and "
            "change the repository tag. Compose: change image: and run docker compose up -d.",
        ]
    return ["Update with the method you installed Crowbarr with, for example pip install --upgrade."]


def _folder(directory: Path | str) -> Path:
    return Path(directory) / "updates"


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def read_state(directory: Path | str) -> dict:
    return _read(_folder(directory) / "state.json")


def write_state(directory: Path | str, state: dict) -> None:
    _folder(directory).mkdir(parents=True, exist_ok=True)
    atomic_write(_folder(directory) / "state.json", json.dumps(state, indent=1))


# ---------------------------------------------------------------- finding a release


def _get_json(client: httpx.Client, url: str, limit: int) -> dict:
    with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
        response.raise_for_status()
        body = b""
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) > limit:
                raise UpdateError("The release information was larger than expected.")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise UpdateError("The release information was not in the expected format.")
    return value


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(10.0, read=30.0),
        follow_redirects=True,
        headers={"User-Agent": f"Crowbarr/{running_version()}"},
        trust_env=True,
    )


def fetch_latest() -> dict:
    """The newest published release, with its manifest when it has one."""
    try:
        with _client() as client:
            release = _get_json(client, f"{api_base()}/repos/{REPO}/releases/latest", 1024 * 1024)
            version = str(release["tag_name"]).lstrip("v")
            parse_version(version)
            assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
            manifest = None
            if MANIFEST in assets:
                manifest = _get_json(client, assets[MANIFEST], MAX_MANIFEST_BYTES)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise UpdateError(f"Could not read the latest release ({type(error).__name__}).") from None
    return {
        "version": version,
        "notes": str(release.get("body") or "")[:4000],
        "url": str(release.get("html_url") or ""),
        "published": str(release.get("published_at") or ""),
        "assets": assets,
        "manifest": manifest,
    }


def runtime_mismatch(manifest: dict) -> str:
    """Why this image cannot run the release's code, or an empty string when it can."""
    wanted_python = str(manifest.get("python", ""))
    if wanted_python and wanted_python != f"{sys.version_info.major}.{sys.version_info.minor}":
        return f"This release needs Python {wanted_python}."
    variant = os.environ.get("CROWBARR_IMAGE_VARIANT", "")
    packages = (manifest.get("runtime") or {}).get(variant)
    if not isinstance(packages, list) or not packages:
        return "This release does not say which packages it needs."
    for pin in packages:
        name, _, wanted = str(pin).partition("==")
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return f"This release needs {name}, which this image does not have."
        if installed != wanted:
            return f"This release needs {name} {wanted}, and this image has {installed}."
    return ""


# ------------------------------------------------------------------- checking


def _check_file(directory: Path | str) -> Path:
    return _folder(directory) / "check.json"


def check(directory: Path | str, force: bool = False) -> dict:
    """Ask GitHub for the newest release and remember the answer."""
    previous = _read(_check_file(directory))
    if not force and time.time() - float(previous.get("checked_at", 0)) < CHECK_COOLDOWN_SECONDS:
        return previous
    result = {"checked_at": time.time(), "error": "", "latest": None}
    try:
        release = fetch_latest()
    except UpdateError as error:
        result.update(error=str(error), latest=previous.get("latest"))
    else:
        manifest = release.pop("manifest")
        release.pop("assets")
        release["manifest"] = manifest
        result["latest"] = release
    _folder(directory).mkdir(parents=True, exist_ok=True)
    atomic_write(_check_file(directory), json.dumps(result, indent=1))
    return result


# ------------------------------------------------------------ installing a release

_job_lock = threading.Lock()
_job: dict = {"status": "idle", "reason": ""}


def job() -> dict:
    with _job_lock:
        return dict(_job)


def _set_job(status: str, reason: str = "") -> None:
    with _job_lock:
        _job.update(status=status, reason=reason)


def _extract(wheel: Path, destination: Path, version: str) -> None:
    allowed = ("crowbarr/", f"crowbarr-{version}.dist-info/")
    with zipfile.ZipFile(wheel) as archive:
        members = archive.infolist()
        for member in members:
            name = member.filename
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts or not name.startswith(allowed):
                raise UpdateError("The update contained files outside Crowbarr's own package.")
        if sum(m.file_size for m in members) > 200 * 1024 * 1024:
            raise UpdateError("The update unpacks to more than expected.")
        archive.extractall(destination)
    declared = (destination / "crowbarr" / "version.py").read_text()
    found = re.search(r'^APPLICATION_VERSION\s*=\s*"([^"]+)"', declared, re.MULTILINE)
    if not found or found.group(1) != version:
        raise UpdateError("The update's version does not match its manifest.")


def download_and_unpack(directory: Path | str, release: dict) -> str:
    """Fetch the release's wheel, verify it, and unpack it. Returns the version."""
    manifest = release.get("manifest") or {}
    version = release["version"]
    if manifest.get("version") != version:
        raise UpdateError("The release manifest does not match the release.")
    reason = runtime_mismatch(manifest)
    if reason:
        raise UpdateError(reason)
    name = str(manifest.get("wheel", ""))
    if not re.fullmatch(r"crowbarr-[0-9.]+-py3-none-any\.whl", name):
        raise UpdateError("The release names an unexpected file.")
    url = release.get("wheel_url") or ""
    folder = _folder(directory)
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / f"{version}.whl.part"
    staging = folder / f"{version}.partial"
    final = folder / version
    shutil.rmtree(staging, ignore_errors=True)
    try:
        digest = hashlib.sha256()
        size = 0
        try:
            with _client() as client, client.stream("GET", url) as response:
                response.raise_for_status()
                with temporary.open("wb") as out:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_WHEEL_BYTES:
                            raise UpdateError("The update was larger than expected.")
                        digest.update(chunk)
                        out.write(chunk)
        except httpx.HTTPError as error:
            raise UpdateError(f"The download failed ({type(error).__name__}).") from None
        if digest.hexdigest() != str(manifest.get("sha256", "")).lower():
            raise UpdateError("The download did not match its checksum, so it was discarded.")
        _set_job("installing")
        staging.mkdir()
        try:
            _extract(temporary, staging, version)
        except (zipfile.BadZipFile, OSError) as error:
            raise UpdateError(f"The update could not be unpacked ({type(error).__name__}).") from None
        shutil.rmtree(final, ignore_errors=True)
        staging.rename(final)
    except OSError as error:
        raise UpdateError(f"Crowbarr could not write the update ({type(error).__name__}).") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        temporary.unlink(missing_ok=True)
    return version


def activate(directory: Path | str, version: str) -> None:
    """Record that the next start should run `version`, keeping the one in use as a fallback."""
    state = read_state(directory)
    previous = state.get("active")
    write_state(
        directory,
        {"active": version, "previous": previous, "boots": 0, "confirmed": False, "failed": None},
    )
    keep = {version, previous}
    for entry in _folder(directory).iterdir():
        if entry.is_dir() and entry.name not in keep and re.fullmatch(r"[0-9.]+", entry.name):
            shutil.rmtree(entry, ignore_errors=True)


def install(directory: Path | str, release: dict, restart: bool = True) -> None:
    """Download, unpack and activate a release, then restart into it."""
    _set_job("downloading")
    try:
        version = download_and_unpack(directory, release)
        activate(directory, version)
    except UpdateError as error:
        _set_job("failed", str(error))
        return
    except Exception as error:
        _set_job("failed", f"The update failed ({type(error).__name__}).")
        return
    _set_job("restarting")
    if restart:
        request_restart()


def start_install(directory: Path | str, restart: bool = True) -> bool:
    """Install the release the last check found. False when one is already running."""
    with _job_lock:
        if _job["status"] in {"downloading", "installing", "restarting"}:
            return False
        _job.update(status="downloading", reason="")
    found = _read(_check_file(directory)).get("latest")
    if not found:
        _set_job("failed", "Check for updates first.")
        return True
    # The wheel is looked up again, so a stale link is never used.
    def run():
        try:
            release = fetch_latest()
        except UpdateError as error:
            _set_job("failed", str(error))
            return
        name = str((release.get("manifest") or {}).get("wheel", ""))
        release["wheel_url"] = release["assets"].get(name, "")
        install(directory, release, restart)

    threading.Thread(target=run, daemon=True).start()
    return True


# --------------------------------------------------------- what the page shows


def status(directory: Path | str, settings) -> dict:
    kind = install_kind()
    saved = _read(_check_file(directory))
    latest = saved.get("latest") or {}
    current = running_version()
    available = False
    if latest:
        try:
            available = parse_version(latest["version"]) > parse_version(current)
        except (ValueError, KeyError):
            available = False
    reason = ""
    if available and kind == "image":
        reason = runtime_mismatch(latest.get("manifest") or {}) if latest.get("manifest") else (
            "This release does not support updating from inside Crowbarr."
        )
    elif available:
        reason = "Updating from inside Crowbarr is only available in the Docker images."
    state = read_state(directory)
    return {
        "current": current,
        "latest": latest.get("version", ""),
        "available": available,
        "checked_at": saved.get("checked_at"),
        "error": saved.get("error", ""),
        "notes": latest.get("notes", ""),
        "url": latest.get("url", ""),
        "kind": kind,
        "can_install": available and kind == "image" and not reason,
        "reason": reason,
        "instructions": instructions(kind, latest["version"]) if available else [],
        "image_tag": image_tag(latest["version"]) if available and kind == "image" else "",
        "job": job(),
        "running_update": os.environ.get("CROWBARR_OTA", ""),
        "failed": state.get("failed"),
        "check_enabled": bool(settings.update_check),
        "auto_enabled": bool(settings.auto_update),
    }


# ----------------------------------------------------------- start up and restart


def boot(directory: Path | str, args: list[str]) -> None:
    """Run first. Re-executes into an installed update, or drops a broken one."""
    if os.environ.get("CROWBARR_OTA") or install_kind() != "image":
        return
    state = read_state(directory)
    active = state.get("active")
    if not active:
        return
    folder = _folder(directory) / str(active)
    try:
        newer = parse_version(active) > parse_version(running_version())
    except ValueError:
        newer = False
    if not folder.is_dir() or not newer:
        # The image caught up, or the files are gone.
        write_state(directory, {**state, "active": None, "boots": 0, "confirmed": False})
        return
    if state.get("boots", 0) >= MAX_UNCONFIRMED_BOOTS and not state.get("confirmed"):
        write_state(
            directory,
            {
                "active": None, "previous": None, "boots": 0, "confirmed": False,
                "failed": {
                    "version": active,
                    "reason": f"Version {active} did not stay running, so Crowbarr went back to "
                    f"{running_version()}.",
                },
            },
        )
        return
    # Each start counts until the new version has stayed up long enough to be trusted.
    boots = 1 if state.get("confirmed") else int(state.get("boots", 0)) + 1
    write_state(directory, {**state, "boots": boots, "confirmed": False})
    environment = dict(os.environ)
    environment["CROWBARR_OTA"] = str(active)
    environment["CROWBARR_ORIG_PYTHONPATH"] = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(folder), environment.get("PYTHONPATH", "")) if part
    )
    os.execve(sys.executable, [sys.executable, "-m", "crowbarr", *args], environment)


def confirm_boot(directory: Path | str) -> None:
    """After a new version has stayed up, stop counting its starts as unconfirmed."""
    if not os.environ.get("CROWBARR_OTA"):
        return

    def mark():
        state = read_state(directory)
        if state.get("active") == os.environ.get("CROWBARR_OTA"):
            write_state(directory, {**state, "boots": 0, "confirmed": True})

    timer = threading.Timer(CONFIRM_AFTER_SECONDS, mark)
    timer.daemon = True
    timer.start()


class _Restart:
    server = None
    requested = False


RESTART = _Restart()


def request_restart() -> None:
    """Stop serving, so `restart_into` can start the new code in this same process."""
    RESTART.requested = True
    if RESTART.server is not None:
        RESTART.server.should_exit = True


def restart_into(args: list[str]) -> None:
    environment = dict(os.environ)
    environment.pop("CROWBARR_OTA", None)
    original = environment.pop("CROWBARR_ORIG_PYTHONPATH", None)
    if original is None:
        pass
    elif original:
        environment["PYTHONPATH"] = original
    else:
        environment.pop("PYTHONPATH", None)
    os.execve(sys.executable, [sys.executable, "-m", "crowbarr", *args], environment)


# ------------------------------------------------------------ background checking


def watch(directory: Path | str, store, db, stop: threading.Event) -> None:
    """Check for updates now and then, and install them when asked to and the worker is idle."""
    stop.wait(60)
    while not stop.is_set():
        settings = store.get()
        if settings.update_check:
            check(directory, force=True)
            if settings.auto_update and install_kind() == "image":
                state = status(directory, settings)
                if state["can_install"] and not _processing(db):
                    start_install(directory)
        stop.wait(CHECK_INTERVAL_SECONDS)


def _processing(db) -> bool:
    try:
        with db.connect() as connection:
            return bool(connection.execute("SELECT 1 FROM jobs WHERE state='processing'").fetchone())
    except Exception:
        return True
