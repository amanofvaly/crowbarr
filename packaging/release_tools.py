"""Release validation and deploy-manifest generation. No server access."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

UPDATE_MANIFEST = "crowbarr-update.json"
UPDATE_WHEEL = re.compile(r"crowbarr-[0-9]+\.[0-9]+\.[0-9]+-py3-none-any\.whl")


def version_tuple(value):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise ValueError(f"Invalid release version: {value!r}")
    return tuple(map(int, value.split(".")))


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def guard():
    # Fetch failure is fatal: never treat unavailable remote state as permission.
    command("git", "fetch", "origin", "main")
    source = command("git", "show", "origin/main:crowbarr/version.py")
    current = Path("crowbarr/version.py").read_text()
    pattern = r'^APPLICATION_VERSION\s*=\s*[\'"]([^\'"]+)[\'"]'
    expected = re.search(pattern, current, re.MULTILINE).group(1)
    actual = re.search(pattern, source, re.MULTILINE).group(1)
    version_tuple(expected)
    if actual != expected:
        raise ValueError(f"Stale release {expected}; main now selects {actual}")
    subprocess.run(["git", "merge-base", "--is-ancestor", os.environ["GITHUB_SHA"], "origin/main"], check=True)
    releases = json.loads(command(
        "gh", "api", "--paginate", "--slurp", f"repos/{os.environ['GITHUB_REPOSITORY']}/releases"
    ))
    for page in releases:
        for release in page:
            tag = release["tag_name"].removeprefix("v")
            if not release["draft"] and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", tag):
                if version_tuple(tag) > version_tuple(expected):
                    raise ValueError(f"Refusing to promote {expected} after published {tag}")


def verify_assets(directory):
    directory = Path(directory)
    name = "crowbarr-linux-x86_64.tar.gz"
    expected = {name, name + ".sha256", "install-crowbarr.sh"}
    names = {p.name for p in directory.iterdir()}
    # The in-app update files are added once the archive has been checked, so they are optional.
    update = {n for n in names if n == UPDATE_MANIFEST or UPDATE_WHEEL.fullmatch(n)}
    if names - update != expected:
        raise ValueError("Unexpected or missing release assets")
    if update:
        verify_update_files(directory, update)
    checksum = (directory / (name + ".sha256")).read_text().split()
    with (directory / name).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if checksum != [digest, name]:
        raise ValueError("Native archive checksum mismatch")
    with tarfile.open(directory / name) as archive:
        members = archive.getmembers()
        names = {member.name for member in members}
        if not {"crowbarr/crowbarr", "crowbarr/LICENSE"} <= names:
            raise ValueError("Archive lacks executable or license")
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or path.parts[0] != "crowbarr":
                raise ValueError(f"Unsafe archive path: {member.name}")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError(f"Unexpected archive member: {member.name}")
            if member.issym() or member.islnk():
                target = PurePosixPath(member.linkname)
                if target.is_absolute() or ".." in target.parts:
                    raise ValueError(f"Unsafe archive link: {member.name}")


def verify_digests(directory):
    directory = Path(directory)
    expected = {"cpu-amd64", "cuda-amd64"}
    if {p.name for p in directory.iterdir()} != expected:
        raise ValueError("Missing or unexpected image verification results")
    image = "ghcr.io/" + os.environ["GITHUB_REPOSITORY"].lower()
    for name in expected:
        ref = (directory / name).read_text().strip()
        if not re.fullmatch(re.escape(image) + r"@sha256:[a-f0-9]{64}", ref):
            raise ValueError(f"Invalid verified image digest: {name}")
        command("docker", "buildx", "imagetools", "inspect", ref)


def version_image(tag, *digest_files):
    refs = [Path(path).read_text().strip() for path in digest_files]
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", tag, "--raw"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        manifest = json.loads(result.stdout)
        actual = {item["digest"] for item in manifest.get("manifests", [])}
        expected = {ref.split("@", 1)[1] for ref in refs}
        if actual != expected:
            raise ValueError(f"Refusing to overwrite existing immutable version {tag}")
        return
    # Auth/network errors must never be mistaken for an absent immutable tag.
    if "not found" not in result.stderr.lower() and "manifest unknown" not in result.stderr.lower():
        raise RuntimeError(result.stderr)
    subprocess.run(["docker", "buildx", "imagetools", "create", "-t", tag, *refs], check=True)


def lock_pins(path):
    """`name==version` for every pinned entry of an image lock. Direct URLs are not pins."""
    return sorted(
        f"{name}=={version}"
        for name, version in re.findall(r"(?m)^([A-Za-z0-9_.\-]+)==([^\s\\;]+)", Path(path).read_text())
    )


def update_manifest(version, wheel, output, source=Path(".")):
    """Describe the in-app update: which wheel, its hash, and the runtime it was built for."""
    version_tuple(version)
    wheel, output = Path(wheel), Path(output)
    if wheel.name != f"crowbarr-{version}-py3-none-any.whl":
        raise ValueError(f"Expected crowbarr-{version}-py3-none-any.whl, found {wheel.name}")
    locks = Path(source) / "packaging" / "inference"
    manifest = {
        "version": version,
        "wheel": wheel.name,
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "python": "3.12",
        "runtime": {variant: lock_pins(locks / f"{variant}.lock") for variant in ("cpu", "cuda")},
    }
    (output / UPDATE_MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")


def verify_update_files(directory, names):
    directory = Path(directory)
    wheels = sorted(n for n in names if n != UPDATE_MANIFEST)
    if UPDATE_MANIFEST not in names or len(wheels) != 1:
        raise ValueError("The update files must be one wheel and its manifest")
    manifest = json.loads((directory / UPDATE_MANIFEST).read_text())
    wheel = directory / wheels[0]
    version = manifest.get("version", "")
    version_tuple(version)
    if manifest.get("wheel") != wheel.name or wheel.name != f"crowbarr-{version}-py3-none-any.whl":
        raise ValueError("The update manifest names a different wheel")
    if manifest.get("sha256") != hashlib.sha256(wheel.read_bytes()).hexdigest():
        raise ValueError("The update manifest checksum does not match its wheel")
    if not all(manifest.get("runtime", {}).get(variant) for variant in ("cpu", "cuda")):
        raise ValueError("The update manifest lists no runtime packages")
    allowed = ("crowbarr/", f"crowbarr-{version}.dist-info/")
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.namelist():
            if member.startswith("/") or ".." in PurePosixPath(member).parts or not member.startswith(allowed):
                raise ValueError(f"Unexpected file in the update wheel: {member}")
        declared = archive.read("crowbarr/version.py").decode()
    if f'APPLICATION_VERSION = "{version}"' not in declared:
        raise ValueError("The update wheel does not carry the manifest's version")


def manifests(version, output, source=Path(".")):
    version_tuple(version)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    for name in ("compose.yaml", "compose.cuda.yaml", "compose.extra-media.yaml"):
        text = (source / name).read_text()
        if name != "compose.extra-media.yaml":
            if text.count("${CROWBARR_IMAGE_TAG:-latest}") != 1:
                raise ValueError(f"Expected exactly one image version default in {name}")
            text = text.replace("${CROWBARR_IMAGE_TAG:-latest}", "${CROWBARR_IMAGE_TAG:-" + version + "}")
        (output / name).write_text(text)
    for name in (
        "README.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE", ".env.example",
        "workflow.html", "docs/docker.md", "docs/nas.md", "docs/unraid.md", "docs/migration.md",
        "docs/linux.md", "integrations/bazarr-notify.py",
    ):
        path = source / name
        if path.exists():
            text = path.read_text()
            if name == ".env.example":
                text = re.sub(r"(?m)^(#\s*)?CROWBARR_IMAGE_TAG=.*$", "# CROWBARR_IMAGE_TAG=" + version, text)
            (output / name).parent.mkdir(parents=True, exist_ok=True)
            (output / name).write_text(text)
    (output / "release.json").write_text(json.dumps({
        "version": version, "commit": os.environ.get("GITHUB_SHA", ""),
    }, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["guard", "verify-assets", "verify-digests", "manifests", "version-image", "update-manifest"],
    )
    parser.add_argument("arguments", nargs="*")
    args = parser.parse_args()
    {"guard": guard, "verify-assets": verify_assets, "verify-digests": verify_digests,
     "manifests": manifests, "version-image": version_image,
     "update-manifest": update_manifest}[args.action](*args.arguments)


if __name__ == "__main__":
    main()
