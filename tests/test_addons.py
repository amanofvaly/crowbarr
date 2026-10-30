import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from crowbarr import addons, capabilities
from crowbarr.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def entries(path: Path) -> dict[str, str]:
    """Package name -> its whole lock entry."""
    text = path.read_text()
    first = re.search(r"^[A-Za-z0-9_.\-]+(?:==| @ )", text, re.MULTILINE).start()
    chunks = re.split(r"\n(?=[A-Za-z0-9_.\-]+(?:==| @ ))", text[first:])
    return {re.match(r"[A-Za-z0-9_.\-]+", c).group(0).lower().replace("_", "-"): c for c in chunks}


@pytest.fixture(autouse=True)
def isolated_download_state(monkeypatch):
    """Download progress is module state, so give each test its own."""
    monkeypatch.setattr(capabilities, "_downloads", {})


@pytest.fixture
def fake_pip(monkeypatch):
    """Stand in for pip, recording the command and creating what a real install would."""
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        target = Path(command[command.index("--target") + 1])
        (target / "whisperx").mkdir(parents=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(addons, "supported", lambda: (True, ""))
    monkeypatch.setattr(addons.subprocess, "run", run)
    return calls


def test_install_uses_the_hash_locked_package_list_and_marks_the_result(tmp_path, fake_pip):
    addons.install(tmp_path)

    command = fake_pip[0]
    assert command[:4] == [sys.executable, "-m", "pip", "install"]
    for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:", "--no-cache-dir"):
        assert flag in command
    assert command[command.index("-r") + 1] == str(addons.LOCK)
    assert addons.installed(tmp_path)
    assert (tmp_path / "addons" / "refinement" / "site-packages" / "whisperx").is_dir()
    assert not (tmp_path / "addons" / "refinement.installing").exists()


def test_a_failed_install_keeps_the_previous_one_and_leaves_no_partial_files(tmp_path, monkeypatch, fake_pip):
    addons.install(tmp_path)
    marker = (tmp_path / "addons" / "refinement" / "installed").read_text()

    def fail(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "line one\nERROR: Hash mismatch\n")

    monkeypatch.setattr(addons.subprocess, "run", fail)
    with pytest.raises(addons.InstallError, match="Hash mismatch"):
        addons.install(tmp_path)

    assert (tmp_path / "addons" / "refinement" / "installed").read_text() == marker
    assert not (tmp_path / "addons" / "refinement.installing").exists()


def test_an_install_from_an_older_package_list_does_not_count(tmp_path, fake_pip):
    addons.install(tmp_path)
    (tmp_path / "addons" / "refinement" / "installed").write_text("an older lock")

    assert not addons.installed(tmp_path)


def test_activate_makes_installed_packages_importable_after_the_image_packages(tmp_path, monkeypatch, fake_pip):
    monkeypatch.setenv("CROWBARR_ADDONS", str(tmp_path / "addons"))
    path = str(tmp_path / "addons" / "refinement" / "site-packages")
    monkeypatch.setattr(sys, "path", list(sys.path))

    addons.activate()
    assert path not in sys.path

    addons.install(tmp_path)
    addons.activate()
    assert sys.path[-1] == path
    addons.activate()
    assert sys.path.count(path) == 1


@pytest.mark.parametrize(
    "change, expected",
    [
        (lambda m: setattr(sys, "frozen", True), "already includes"),
        (lambda m: setattr(sys, "platform", "darwin"), "Linux x86_64"),
        (lambda m: setattr(sys, "version_info", (3, 13, 0, "final", 0)), "Python 3.12"),
    ],
)
def test_unsupported_installs_say_why(monkeypatch, change, expected):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(addons.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(sys, "version_info", (3, 12, 0, "final", 0))
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    change(monkeypatch)

    ok, reason = addons.supported()

    assert not ok
    assert expected in reason


def test_software_install_reports_ready_and_makes_the_probe_look_again(tmp_path, monkeypatch):
    monkeypatch.setattr(addons, "install", lambda directory: None)
    monkeypatch.setattr(capabilities, "_cached", {"stale": True})
    monkeypatch.setattr(capabilities, "_expires", time.monotonic() + 300)

    assert capabilities.start_software_install(tmp_path)
    deadline = time.monotonic() + 5
    while capabilities.download_state("software")["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.01)

    assert capabilities.download_state("software") == {"status": "ready", "reason": ""}
    assert capabilities._cached is None


def test_software_install_reports_the_reason_it_failed(tmp_path, monkeypatch):
    def fail(directory):
        raise addons.InstallError("The install failed: no network")

    monkeypatch.setattr(addons, "install", fail)
    capabilities.start_software_install(tmp_path)
    deadline = time.monotonic() + 5
    while capabilities.download_state("software")["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.01)

    assert capabilities.download_state("software") == {
        "status": "failed", "reason": "The install failed: no network",
    }


def test_the_inventory_reports_whether_the_software_can_be_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(addons, "supported", lambda: (False, "Linux x86_64 only."))

    software = capabilities.model_inventory(tmp_path)["software"]

    assert software["present"] is False
    assert software["installable"] is False
    assert software["unsupported_reason"] == "Linux x86_64 only."


def test_the_install_button_is_refused_where_the_software_cannot_install(tmp_path, monkeypatch):
    monkeypatch.setattr(addons, "supported", lambda: (False, "This install already includes WhisperX refinement."))
    with TestClient(create_app(tmp_path, background=False)) as client:
        headers = {"X-Api-Key": client.app.state.store.token}
        response = client.post("/api/capabilities/software", headers=headers)

    assert response.status_code == 409
    assert "already includes" in response.json()["detail"]


def test_the_install_button_starts_an_install(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(addons, "supported", lambda: (True, ""))
    monkeypatch.setattr("crowbarr.app.start_software_install", lambda directory: started.append(directory) or True)
    with TestClient(create_app(tmp_path, background=False)) as client:
        headers = {"X-Api-Key": client.app.state.store.token}
        response = client.post("/api/capabilities/software", headers=headers)

    assert response.status_code == 202
    assert started == [client.app.state.store.directory]


def test_the_refinement_lock_pins_every_package_by_hash():
    packages = entries(addons.LOCK)

    assert {"torch", "torchaudio", "transformers", "whisperx"} <= set(packages)
    for name, entry in packages.items():
        assert "--hash=sha256:" in entry or "#sha256=" in entry, f"{name} is not pinned by hash"


def test_the_image_locks_leave_the_refinement_packages_out_and_cover_the_rest():
    refinement = set(entries(addons.LOCK))
    for variant in ("cpu", "cuda"):
        image = set(entries(ROOT / "packaging" / "inference" / f"{variant}.lock"))
        assert not image & refinement, f"{variant} lock repeats {sorted(image & refinement)}"
        assert {"faster-whisper", "ctranslate2", "fastapi", "numpy"} <= image


def test_splitting_the_joined_locks_gives_the_same_two_package_sets():
    import importlib.util

    spec = importlib.util.spec_from_file_location("split_locks", ROOT / "packaging" / "inference" / "split_locks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    image_file = ROOT / "packaging" / "inference" / "cpu.lock"
    joined = image_file.read_text() + "\n" + "\n".join(entries(addons.LOCK).values()) + "\n"

    core, addon = module.split(joined)

    def names(text):
        return set(re.findall(r"^([A-Za-z0-9_.\-]+)(?:==| @ )", text, re.MULTILINE))

    assert names(core) == names(image_file.read_text())
    assert names(addon) == names(addons.LOCK.read_text())
