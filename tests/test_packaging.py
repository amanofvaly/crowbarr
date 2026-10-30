import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
import packaging_smoke  # noqa: E402

spec = importlib.util.spec_from_file_location("release_tools", ROOT / "packaging/release_tools.py")
release_tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_tools)


def assets(directory, extra=None):
    archive = directory / "crowbarr-linux-x86_64.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for name in ["crowbarr/crowbarr", "crowbarr/LICENSE", *([extra] if extra else [])]:
            member = tarfile.TarInfo(name)
            member.size = 1
            stream.addfile(member, io.BytesIO(b"x"))
    (directory / (archive.name + ".sha256")).write_text(
        hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n"
    )
    (directory / "install-crowbarr.sh").write_text("#!/bin/sh\n")
    return archive


def test_archive_validation_checks_checksum_and_paths(tmp_path):
    archive = assets(tmp_path)
    release_tools.verify_assets(tmp_path)
    archive.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        release_tools.verify_assets(tmp_path)
    assets(tmp_path, "crowbarr/../../outside")
    with pytest.raises(ValueError, match="Unsafe archive path"):
        release_tools.verify_assets(tmp_path)


def test_manifest_defaults_are_pinned_without_editing_sources(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name, suffix in [("compose.yaml", ""), ("compose.cuda.yaml", "-cuda")]:
        (source / name).write_text("image: ghcr.io/example/app:${CROWBARR_IMAGE_TAG:-latest}" + suffix)
    (source / "compose.extra-media.yaml").write_text("volumes: []")
    (source / ".env.example").write_text("CROWBARR_IMAGE_TAG=latest\n")
    output = tmp_path / "output"
    release_tools.manifests("1.2.3", output, source)
    assert "${CROWBARR_IMAGE_TAG:-1.2.3}-cuda" in (output / "compose.cuda.yaml").read_text()
    assert "latest" in (source / "compose.yaml").read_text()
    assert (output / ".env.example").read_text() == "# CROWBARR_IMAGE_TAG=1.2.3\n"
    assert json.loads((output / "release.json").read_text())["version"] == "1.2.3"


def test_release_instructions_keep_local_links_and_do_not_pin_updates(tmp_path):
    import re

    output = tmp_path / "release"
    release_tools.manifests("1.2.3", output, ROOT)
    for name in ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "docs/docker.md", "docs/nas.md"):
        page = output / name
        assert page.exists()
        for link in re.findall(r"\]\(([^)]+)\)", page.read_text()):
            if "://" not in link and not link.startswith("#"):
                assert (page.parent / link.split("#", 1)[0]).exists(), f"Broken link: {name}: {link}"
    assert not re.search(r"(?m)^CROWBARR_IMAGE_TAG=", (output / ".env.example").read_text())


def test_invalid_version_cannot_be_inserted_in_manifests(tmp_path):
    with pytest.raises(ValueError, match="Invalid release version"):
        release_tools.manifests("latest", tmp_path / "output")


def test_missing_verified_architecture_blocks_release(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/app")
    (tmp_path / "cpu-amd64").write_text("ghcr.io/example/app@sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="Missing or unexpected"):
        release_tools.verify_digests(tmp_path)


def test_existing_version_is_never_overwritten(tmp_path, monkeypatch):
    digest = tmp_path / "cpu-amd64"
    digest.write_text("ghcr.io/example/app@sha256:" + "a" * 64)
    monkeypatch.setattr(release_tools.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a, 0, json.dumps({"manifests": [{"digest": "sha256:" + "b" * 64}]}), ""
    ))
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        release_tools.version_image("ghcr.io/example/app:1.2.3", digest)


def test_release_guard_rejects_newer_published_release(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "crowbarr").mkdir()
    (tmp_path / "crowbarr/version.py").write_text('APPLICATION_VERSION = "1.2.3"\n')
    monkeypatch.setenv("GITHUB_SHA", "abc")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/app")

    def command(*args):
        if args[:2] == ("git", "show"):
            return 'APPLICATION_VERSION = "1.2.3"\n'
        if args[0] == "gh":
            return json.dumps([[{"tag_name": "v1.2.4", "draft": False}]])
        return ""

    monkeypatch.setattr(release_tools, "command", command)
    monkeypatch.setattr(release_tools.subprocess, "run", lambda *a, **kw: None)
    with pytest.raises(ValueError, match="after published"):
        release_tools.guard()


def test_packaging_fixture_uses_real_spawned_processing():
    packaging_smoke.smoke(check_inference=False)


def test_packaging_model_download_retries_transient_failures(tmp_path):
    attempts = []

    def model(*args, **kwargs):
        attempts.append((args, kwargs))
        if len(attempts) < 3:
            raise OSError("temporary Hub failure")
        return "loaded"

    delays = []
    assert packaging_smoke.load_smoke_model(model, tmp_path, delays.append) == "loaded"
    assert delays == [3, 10]
    assert len(attempts) == 3


def update_wheel(directory, version="1.2.3", extra=None, declared=None):
    wheel = directory / f"crowbarr-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("crowbarr/version.py", declared or f'APPLICATION_VERSION = "{version}"\n')
        archive.writestr(f"crowbarr-{version}.dist-info/METADATA", "Name: crowbarr\n")
        for name in extra or []:
            archive.writestr(name, "x")
    return wheel


def test_update_files_carry_the_wheel_hash_and_the_runtime_it_needs(tmp_path):
    assets(tmp_path)
    wheel = update_wheel(tmp_path)

    release_tools.update_manifest("1.2.3", wheel, tmp_path, ROOT)

    release_tools.verify_assets(tmp_path)
    manifest = json.loads((tmp_path / "crowbarr-update.json").read_text())
    assert manifest["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert manifest["wheel"] == wheel.name and manifest["python"] == "3.12"
    assert any(pin.startswith("faster-whisper==") for pin in manifest["runtime"]["cpu"])
    assert any(pin.startswith("nvidia-cublas") for pin in manifest["runtime"]["cuda"])
    assert not any(pin.startswith("nvidia-cublas") for pin in manifest["runtime"]["cpu"])
    assert not any(pin.split("==")[0] in {"torch", "whisperx"} for pin in manifest["runtime"]["cpu"])


def test_a_changed_update_wheel_fails_verification(tmp_path):
    assets(tmp_path)
    wheel = update_wheel(tmp_path)
    release_tools.update_manifest("1.2.3", wheel, tmp_path, ROOT)

    update_wheel(tmp_path, extra=["crowbarr/changed.py"])

    with pytest.raises(ValueError, match="checksum"):
        release_tools.verify_assets(tmp_path)


def test_an_update_wheel_may_only_hold_crowbarrs_own_package(tmp_path):
    assets(tmp_path)
    wheel = update_wheel(tmp_path, extra=["other_package/__init__.py"])
    release_tools.update_manifest("1.2.3", wheel, tmp_path, ROOT)

    with pytest.raises(ValueError, match="Unexpected file"):
        release_tools.verify_assets(tmp_path)


def test_an_update_wheel_must_carry_the_version_it_claims(tmp_path):
    assets(tmp_path)
    wheel = update_wheel(tmp_path, declared='APPLICATION_VERSION = "9.9.9"\n')
    release_tools.update_manifest("1.2.3", wheel, tmp_path, ROOT)

    with pytest.raises(ValueError, match="version"):
        release_tools.verify_assets(tmp_path)


def test_a_wheel_without_its_manifest_is_not_a_release_asset(tmp_path):
    assets(tmp_path)
    update_wheel(tmp_path)

    with pytest.raises(ValueError, match="one wheel and its manifest"):
        release_tools.verify_assets(tmp_path)


def test_the_update_manifest_needs_the_matching_wheel_name(tmp_path):
    wheel = update_wheel(tmp_path, version="1.2.3")

    with pytest.raises(ValueError, match="Expected"):
        release_tools.update_manifest("1.2.4", wheel, tmp_path, ROOT)
