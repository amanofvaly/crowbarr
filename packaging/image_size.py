"""Measure image storage and enforce a measured per-variant regression budget."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tarfile
import zlib
from pathlib import Path

INVENTORY = r'''
import importlib.metadata as metadata
import json
from pathlib import Path
import sysconfig
root = Path(sysconfig.get_paths()["purelib"])
packages = []
for dist in metadata.distributions():
    size = 0
    for file in dist.files or []:
        path = Path(dist.locate_file(file))
        if path.is_file() and not path.is_symlink():
            size += path.stat().st_size
    packages.append({"name": dist.metadata["Name"], "version": dist.version, "bytes": size})
print(json.dumps(sorted(packages, key=lambda p: p["bytes"], reverse=True)))
'''


def command(*args):
    return subprocess.check_output(args, text=True)


def measure(image: str) -> dict:
    info = json.loads(command("docker", "image", "inspect", image))[0]
    packages = json.loads(command("docker", "run", "--rm", "--network=none", "--entrypoint",
                                  "python", image, "-c", INVENTORY))
    return {
        "image": image, "id": info["Id"], "repo_digests": info.get("RepoDigests", []),
        "unpacked_bytes": info["Size"], "layers": info["RootFS"]["Layers"],
        "history": [json.loads(line) for line in command(
            "docker", "history", "--no-trunc", "--format", "{{json .}}", image
        ).splitlines()],
        "packages": packages,
    }


def compressed_layers(image: str) -> list[dict]:
    """Measure gzip-6 layer payloads, not the outer docker-save archive.

    This is a reproducible transfer estimate; registry compression can differ.
    Hash uncompressed layers so application-only update reuse is measurable.
    """
    layers = []
    process = subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                if not member.isfile() or not member.name.endswith("/layer.tar"):
                    continue
                digest = hashlib.sha256()
                compressor = zlib.compressobj(level=6, wbits=31)
                size = 0
                with archive.extractfile(member) as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(compressor.compress(chunk))
                size += len(compressor.flush())
                layers.append({"diff_id": "sha256:" + digest.hexdigest(),
                               "unpacked_bytes": member.size, "gzip_bytes": size})
        if process.wait() != 0:
            raise RuntimeError("docker save failed")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
    if not layers:
        raise ValueError("No classic Docker layer archives found; cannot estimate compression")
    return layers


def check_budget(report: dict, baseline: dict, variant: str) -> None:
    limit = baseline[variant]["unpacked_bytes"] * 105 // 100
    if report["unpacked_bytes"] > limit:
        raise ValueError(f"{variant} image exceeds its measured size + 5% budget: "
                         f'{report["unpacked_bytes"]} > {limit} bytes')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compressed", action="store_true", help="Also estimate gzip layer transfer bytes")
    parser.add_argument("--budget", type=Path)
    parser.add_argument("--variant", choices=("cpu", "cuda"))
    args = parser.parse_args()
    report = measure(args.image)
    if args.compressed:
        report["compressed_layers"] = compressed_layers(args.image)
        report["gzip_layer_bytes"] = sum(layer["gzip_bytes"] for layer in report["compressed_layers"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f'{args.image}: {report["unpacked_bytes"]:,} unpacked bytes, {len(report["packages"])} packages')
    if args.budget:
        if not args.variant:
            parser.error("--variant is required with --budget")
        check_budget(report, json.loads(args.budget.read_text()), args.variant)


if __name__ == "__main__":
    main()
