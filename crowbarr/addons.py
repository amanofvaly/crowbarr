"""Optional software installed into the data directory on request.

The speech engine ships in every image. WhisperX refinement needs PyTorch and about
twenty more packages, so images leave them out and Crowbarr installs them here when the
user asks, the way it downloads a speech model. The packages come from
`refinement.lock`, which pins every file by hash, so what runs matches what was tested.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

NAME = "refinement"
LOCK = Path(__file__).with_name("refinement.lock")
# Where an image keeps the wheels that PyPI does not host.
DEFAULT_WHEELS = "/opt/crowbarr/wheels"
INSTALL_TIMEOUT = 1800


class InstallError(RuntimeError):
    """The install failed. The message is safe to show on the settings page."""


def _root() -> Path | None:
    value = os.environ.get("CROWBARR_ADDONS")
    return Path(value) if value else None


def _digest() -> str:
    return hashlib.sha256(LOCK.read_bytes()).hexdigest()


def location(directory: Path | str | None = None) -> Path | None:
    base = Path(directory) / "addons" if directory else _root()
    return base / NAME if base else None


def installed(directory: Path | str | None = None) -> bool:
    """True when the packages on disk were installed from this release's lock."""
    folder = location(directory)
    marker = folder / "installed" if folder else None
    try:
        return bool(marker and (folder / "site-packages").is_dir() and marker.read_text() == _digest())
    except OSError:
        return False


def activate() -> None:
    """Make installed add-on packages importable. Call before importing them."""
    folder = location()
    if folder and installed():
        path = str(folder / "site-packages")
        # Last, so the packages an image ships always win over an add-on's copy.
        if path not in sys.path:
            sys.path.append(path)


def supported() -> tuple[bool, str]:
    """Whether this process can install the add-on, and why not when it cannot."""
    if getattr(sys, "frozen", False):
        return False, "This install already includes WhisperX refinement."
    if not (sys.platform.startswith("linux") and platform.machine() in {"x86_64", "AMD64"}):
        return False, "WhisperX refinement installs on Linux x86_64 only."
    if sys.version_info[:2] != (3, 12):
        return False, "WhisperX refinement installs on Python 3.12 only."
    if importlib.util.find_spec("pip") is None:
        return False, "pip is not available in this install."
    if not LOCK.is_file():
        return False, "The refinement package list is missing from this install."
    return True, ""


def _wheels() -> Path:
    return Path(os.environ.get("CROWBARR_WHEELS", DEFAULT_WHEELS))


def install(directory: Path | str) -> None:
    """Install the add-on into `directory`/addons. Replaces an older copy in one step."""
    ok, reason = supported()
    if not ok:
        raise InstallError(reason)
    final = location(directory)
    staging = final.with_name(NAME + ".installing")
    shutil.rmtree(staging, ignore_errors=True)
    (staging / "site-packages").mkdir(parents=True)
    command = [
        sys.executable, "-m", "pip", "install",
        "--no-input", "--disable-pip-version-check", "--no-cache-dir", "--no-compile",
        "--no-deps", "--require-hashes", "--only-binary=:all:",
        "--target", str(staging / "site-packages"),
        "--find-links", str(_wheels()),
        "-r", str(LOCK),
    ]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=INSTALL_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        shutil.rmtree(staging, ignore_errors=True)
        raise InstallError("The install timed out after 30 minutes.") from None
    except OSError as error:
        shutil.rmtree(staging, ignore_errors=True)
        raise InstallError(f"The install could not start ({type(error).__name__}).") from None
    if done.returncode:
        shutil.rmtree(staging, ignore_errors=True)
        tail = (done.stderr.strip().splitlines() or ["no details"])[-1][:200]
        raise InstallError(f"The install failed: {tail}")
    (staging / "installed").write_text(_digest())
    shutil.rmtree(final, ignore_errors=True)
    staging.rename(final)
