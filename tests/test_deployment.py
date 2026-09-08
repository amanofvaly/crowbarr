import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def compose():
    if not shutil.which("docker"):
        pytest.skip("Docker Compose is unavailable")
    available = subprocess.run(["docker", "compose", "version"], capture_output=True)
    if available.returncode:
        pytest.skip("Docker Compose is unavailable")

    def render(values, *extra):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("CROWBARR_")}
        environment.update(values)
        command = ["docker", "compose", "--env-file", "/dev/null", "-f", str(ROOT / "compose.yaml")]
        for name in extra:
            command.extend(["-f", str(ROOT / name)])
        return subprocess.run(
            [*command, "config", "--format", "json"], env=environment,
            capture_output=True, text=True, check=False,
        )

    return render


def test_missing_persistent_storage_fails_before_deployment(compose):
    result = compose({})
    assert result.returncode != 0
    assert "CROWBARR_CONFIG_DIR" in result.stderr


def test_single_library_install_keeps_data_outside_git_checkout(compose):
    result = compose({"CROWBARR_CONFIG_DIR": "/srv/app/config", "CROWBARR_MEDIA_DIR": "/srv/media"})
    assert result.returncode == 0, result.stderr
    service = json.loads(result.stdout)["services"]["crowbarr"]
    assert service["user"] == "1000:1000"
    assert service["init"] is True
    assert "container_name" not in service
    mounts = {mount["target"]: mount for mount in service["volumes"]}
    assert mounts["/config"]["source"] == "/srv/app/config"
    assert mounts["/media"]["source"] == "/srv/media"
    assert all(not mount["bind"].get("create_host_path", False) for mount in mounts.values())


def test_gpu_migration_preserves_existing_paths_identity_and_version(compose):
    result = compose(
        {
            "CROWBARR_CONFIG_DIR": "/srv/existing/config",
            "CROWBARR_MEDIA_DIR": "/srv/television",
            "CROWBARR_MEDIA_MOUNT": "/tv",
            "CROWBARR_EXTRA_MEDIA_DIR": "/srv/movies",
            "CROWBARR_EXTRA_MEDIA_MOUNT": "/movies",
            "CROWBARR_UID": "568", "CROWBARR_GID": "568",
            "CROWBARR_IMAGE_TAG": "1.2.3", "CROWBARR_PORT": "18449",
        },
        "compose.cuda.yaml", "compose.extra-media.yaml",
    )
    assert result.returncode == 0, result.stderr
    service = json.loads(result.stdout)["services"]["crowbarr"]
    assert service["user"] == "568:568"
    assert service["image"] == "ghcr.io/amanofvaly/crowbarr:1.2.3-cuda"
    assert service["ports"][0]["published"] == "18449"
    assert {m["target"] for m in service["volumes"]} == {"/config", "/tv", "/movies"}
    assert service["deploy"]["resources"]["reservations"]["devices"][0]["capabilities"] == ["gpu"]
