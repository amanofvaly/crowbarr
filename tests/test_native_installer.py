"""Run the real installer control flow without root, Linux, network or systemd.

Only fixed system paths/EUID are redirected in a temporary copy. Host-specific
commands are mocked; actual files, symlink switches and archive checks are exercised.
"""

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "packaging/install-crowbarr.sh"
MOCK = r'''
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile

name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ["HARNESS_ROOT"])
with (root / "commands").open("a") as log:
    log.write(json.dumps([name, *args]) + "\n")
state_file = root / "state.json"
state = json.loads(state_file.read_text())

def save():
    state_file.write_text(json.dumps(state))

if name == "uname":
    print("Linux" if args == ["-s"] else "x86_64")
elif name == "stat":
    if args[1] == "%u":
        print("1000" if os.environ.get("UNSAFE_OWNER") == args[-1] else "0")
    else:
        # Real leaf permissions; virtual root ancestors belong to the test runner.
        path = Path(args[-1])
        print(oct(path.stat().st_mode & 0o777)[2:] if path.is_relative_to(root) else "755")
elif name == "flock":
    sys.exit(int(os.environ.get("LOCKED", "0")))
elif name == "id":
    user = args[-1]
    if user not in state["users"]:
        sys.exit(1)
    print(user if args[0] == "-gn" else "0" if user == "root" else "999")
elif name == "getent":
    sys.exit(0 if args[-1] in state["groups"] else 2)
elif name in ("useradd", "groupadd"):
    state["users" if name == "useradd" else "groups"].append(args[-1])
    save()
elif name == "systemctl":
    action = args[0]
    unit = args[-1]
    if action == "show":
        sys.exit(int(os.environ.get("NO_SYSTEMD", "0")))
    active = state.setdefault("active", [])
    enabled = state.setdefault("enabled", [])
    if action in ("is-active", "is-enabled"):
        sys.exit(0 if unit in (active if action == "is-active" else enabled) else 1)
    if action == "enable":
        if "--now" in args and os.environ.get("FAIL_START") and not state.get("failed_once"):
            state["failed_once"] = True
            save()
            sys.exit(1)
        if unit not in enabled:
            enabled.append(unit)
    if action in ("start", "restart") or (action == "enable" and "--now" in args):
        if unit not in active:
            active.append(unit)
    if action == "stop" or (action == "disable" and "--now" in args):
        if unit in active:
            active.remove(unit)
    if action == "disable" and unit in enabled:
        enabled.remove(unit)
    save()
elif name == "curl":
    url = next(arg for arg in args if arg.startswith("http"))
    if url.endswith("/health"):
        sys.exit(int(os.environ.get("BAD_HEALTH", "0")))
    if os.environ.get("FAIL_DOWNLOAD") and url.endswith(os.environ["FAIL_DOWNLOAD"]):
        sys.exit(22)
    dest = Path(args[args.index("-o") + 1])
    if url.endswith(".sha256"):
        digest = hashlib.sha256((root / "package.tar.gz").read_bytes()).hexdigest()
        dest.write_text(("0" * 64 if os.environ.get("BAD_CHECKSUM") else digest) + "  crowbarr-linux-x86_64.tar.gz\n")
    else:
        shutil.copyfile(root / ("installer.sh" if url.endswith(".sh") else "package.tar.gz"), dest)
elif name == "sha256sum":
    expected, path = sys.stdin.read().split()
    sys.exit(0 if hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected else 1)
elif name == "tar":
    with tarfile.open(args[args.index("--file") + 1]) as archive:
        archive.extractall(args[args.index("--directory") + 1], filter="data")
elif name == "install":
    mode = 0o755
    paths = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-o", "-g", "-m"):
            if arg == "-m":
                mode = int(args[index + 1], 8)
            index += 2
        else:
            if not arg.startswith("-"):
                paths.append(Path(arg))
            index += 1
    if "-d" in args:
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(mode)
    else:
        shutil.copyfile(*paths)
        paths[-1].chmod(mode)
elif name == "mv":
    if args[0] == "-Tf":
        os.replace(args[1], args[2])
    else:
        shutil.move(*args)
elif name == "readlink":
    print(Path(args[-1]).resolve())
elif name not in ("chown", "sleep", "ffmpeg", "ffprobe"):
    raise RuntimeError(f"Unexpected mock: {name}")
'''


class NativeInstaller:
    def __init__(self, root):
        self.root = root.resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.unit = self.root / "etc/systemd/system"
        self.unit.mkdir(parents=True)
        (self.root / "run/systemd/system").mkdir(parents=True)
        (self.root / "run/lock").mkdir()
        self.config = self.root / "etc/crowbarr/installer.env"
        self.install = self.root / "opt/crowbarr"
        self.data = self.root / "var/lib/crowbarr"
        self.updater = self.root / "usr/local/sbin/crowbarr-update"
        source = INSTALLER.read_text().replace('"$EUID"', '"0"')
        for prefix in ("/etc/crowbarr", "/etc/systemd/system", "/usr/local/sbin", "/run/", "/opt/", "/var/lib/"):
            source = source.replace(prefix, str(self.root) + prefix)
        self.script = self.root / "installer.sh"
        self.script.write_text(source)
        self.state_file = self.root / "state.json"
        self.state_file.write_text(json.dumps({"users": ["root", "media"], "groups": ["root", "media", "video"]}))
        for command in (
            "uname", "stat", "flock", "id", "getent", "useradd", "groupadd", "systemctl",
            "curl", "sha256sum", "tar", "install", "mv", "readlink", "chown", "sleep", "ffmpeg", "ffprobe",
        ):
            path = self.bin / command
            path.write_text(f"#!{sys.executable}\n" + MOCK)
            path.chmod(0o755)
        self.package("first")

    def package(self, version):
        with tarfile.open(self.root / "package.tar.gz", "w:gz") as archive:
            data = f"#!/bin/sh\necho {version}\n".encode()
            entry = tarfile.TarInfo("crowbarr/crowbarr")
            entry.mode = 0o755
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))

    def run(self, *, updater=False, **settings):
        env = {key: value for key, value in os.environ.items() if not key.startswith("CROWBARR_")}
        env.update(HARNESS_ROOT=str(self.root), PATH=f"{self.bin}:{os.environ['PATH']}", SUDO_USER="root")
        env.update({key: str(value) for key, value in settings.items()})
        return subprocess.run(
            ["bash", str(self.updater if updater else self.script)], env=env,
            capture_output=True, text=True, timeout=60,
        )

    @property
    def state(self):
        return json.loads(self.state_file.read_text())

    @property
    def commands(self):
        return [json.loads(line) for line in (self.root / "commands").read_text().splitlines()]


@pytest.fixture
def native(tmp_path):
    return NativeInstaller(tmp_path)


def succeeded(result):
    assert result.returncode == 0, result.stdout + result.stderr


def test_shell_syntax():
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)


def test_default_account_and_repeat_install(native):
    succeeded(native.run())
    assert native.install.is_symlink()
    assert "User=crowbarr\nGroup=crowbarr" in (native.unit / "crowbarr.service").read_text()
    assert native.config.stat().st_mode & 0o777 == 0o600
    assert "CROWBARR_AUTO_UPDATE=false" in native.config.read_text()
    assert "crowbarr-update.timer" not in native.state["enabled"]
    target = native.install.resolve()
    before = len(native.commands)
    succeeded(native.run(updater=True))
    subsequent = native.commands[before:]
    assert ["systemctl", "stop", "crowbarr.service"] not in subsequent
    assert not any(cmd[0] == "curl" and any(arg.endswith(".tar.gz") for arg in cmd) for cmd in subsequent)
    assert native.install.resolve() == target
    assert native.state["users"].count("crowbarr") == 1
    assert len(list(native.install.with_suffix(".versions").iterdir())) == 1


def test_custom_settings_survive_updater_and_timer_opt_out(native):
    install = native.root / "custom/app"
    data = native.root / "custom/data"
    succeeded(native.run(CROWBARR_USER="media", CROWBARR_GROUP="video", CROWBARR_INSTALL_DIR=install,
                         CROWBARR_DATA_DIR=data, CROWBARR_AUTO_UPDATE="true"))
    (data / "crowbarr.db").write_text("persistent data")
    previous = install.resolve()
    native.package("second")
    succeeded(native.run(updater=True))
    assert install.resolve() != previous
    assert (previous / "crowbarr").is_file()
    assert (data / "crowbarr.db").read_text() == "persistent data"
    assert "User=media\nGroup=video" in (native.unit / "crowbarr.service").read_text()
    assert "crowbarr-update.timer" in native.state["enabled"]
    succeeded(native.run(updater=True, CROWBARR_AUTO_UPDATE="false"))
    assert "crowbarr-update.timer" not in native.state["active"]
    assert "CROWBARR_AUTO_UPDATE=false" in native.config.read_text()


@pytest.mark.parametrize("failure", ["BAD_HEALTH", "FAIL_START"])
def test_failed_upgrade_restores_binary_settings_and_timer(native, failure):
    succeeded(native.run(CROWBARR_AUTO_UPDATE="true"))
    previous = native.install.resolve()
    files = [native.config, native.updater, *native.unit.iterdir()]
    originals = {path: path.read_bytes() for path in files}
    (native.data / "crowbarr.db").write_text("keep me")
    native.package("broken")
    result = native.run(updater=True, CROWBARR_AUTO_UPDATE="false", **{failure: "1"})
    assert result.returncode != 0
    assert "restoring" in result.stderr
    assert native.install.resolve() == previous
    assert {path: path.read_bytes() for path in files} == originals
    assert (native.data / "crowbarr.db").read_text() == "keep me"
    assert "crowbarr.service" in native.state["active"]
    assert "crowbarr-update.timer" in native.state["active"]
    assert len(list(native.install.with_suffix(".versions").iterdir())) == 1


@pytest.mark.parametrize("settings", [{"BAD_CHECKSUM": "1"}, {"FAIL_DOWNLOAD": ".sh"}])
def test_download_failure_does_not_stop_old_service(native, settings):
    succeeded(native.run())
    native.package("second")
    before = len(native.commands)
    previous = native.install.resolve()
    result = native.run(updater=True, **settings)
    assert result.returncode != 0
    assert native.install.resolve() == previous
    assert ["systemctl", "stop", "crowbarr.service"] not in native.commands[before:]


def test_selecting_older_binary_keeps_current_updater(native):
    succeeded(native.run(CROWBARR_VERSION="0.3.5"))
    urls = [arg for command in native.commands if command[0] == "curl"
            for arg in command if arg.startswith("https://")]
    assert any("/download/v0.3.5/" in url and url.endswith(".tar.gz") for url in urls)
    assert next(url for url in urls if url.endswith("install-crowbarr.sh")).endswith(
        "/releases/latest/download/install-crowbarr.sh"
    )


def test_legacy_unit_migration(native):
    native.install.mkdir(parents=True)
    binary = native.install / "crowbarr"
    binary.write_text("legacy")
    binary.chmod(0o755)
    native.data.mkdir(parents=True)
    (native.data / "crowbarr.db").write_text("old database")
    (native.unit / "crowbarr.service").write_text(
        f"[Service]\nUser=media\nGroup=video\nEnvironment=CROWBARR_DATA={native.data}\n"
        f"ExecStart={native.install}/crowbarr --host 0.0.0.0 --port 8449\n"
    )
    succeeded(native.run())
    assert "CROWBARR_USER=media" in native.config.read_text()
    assert "CROWBARR_GROUP=video" in native.config.read_text()
    assert (native.data / "crowbarr.db").read_text() == "old database"
    assert next(native.install.with_suffix(".versions").glob("legacy.*/crowbarr")).read_text() == "legacy"


@pytest.mark.parametrize("settings", [{"NO_SYSTEMD": "1"}, {"LOCKED": "1"}, {"CROWBARR_USER": "missing"},
                                     {"CROWBARR_AUTO_UPDATE": "yes"}, {"CROWBARR_DATA_DIR": "/tmp/a%h"}])
def test_preflight_failure_does_not_download(native, settings):
    result = native.run(**settings)
    assert result.returncode != 0
    assert not any(command[0] == "curl" for command in native.commands)


def test_config_is_not_executable_and_must_be_root_owned(native):
    succeeded(native.run())
    result = native.run(UNSAFE_OWNER=native.config)
    assert result.returncode != 0
    assert "root-owned" in result.stderr
    marker = native.root / "injected"
    native.config.write_text(f"CROWBARR_USER=$(touch {marker})\n")
    result = native.run()
    assert result.returncode != 0
    assert not marker.exists()


def test_first_install_health_failure_preserves_data_and_removes_service(native):
    result = native.run(BAD_HEALTH="1")
    assert result.returncode != 0
    assert native.data.is_dir()
    assert not native.install.exists()
    assert not native.config.exists()
    assert not (native.unit / "crowbarr.service").exists()
    assert "crowbarr.service" not in native.state["active"]
