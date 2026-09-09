"""Build a deterministic, alignment-only WhisperX wheel from verified upstream bytes.

No alignment algorithms are modified. The local version distinguishes this distribution
from upstream; it intentionally exposes only the alignment API used by Crowbarr.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import urllib.request
import zipfile
from pathlib import Path

UPSTREAM_URL = (
    "https://files.pythonhosted.org/packages/00/53/"
    "d533db85704e64b1709db695fba2dd3b747cb7e979a18bd18dfbf1e44b91/"
    "whisperx-3.8.6-py3-none-any.whl"
)
UPSTREAM_SHA256 = "cb6d4fcd3fb6c42305cb8b222a33a0b78f6b657e9db3b714345fe43dc0a69c1f"
VERSION = "3.8.6+crowbarr.1"
FILENAME = f"whisperx-{VERSION}-py3-none-any.whl"
MODULES = ("alignment.py", "audio.py", "utils.py", "schema.py", "log_utils.py")
DEPENDENCIES = (
    "torch~=2.8.0", "torchaudio~=2.8.0", "transformers>=4.48.0,<5",
    "numpy>=2.1.0", "pandas>=2.2.3", "nltk>=3.9.1", "huggingface-hub<1.0.0",
)
INIT = '''"""WhisperX alignment API, packaged for Crowbarr. See distribution provenance."""
import importlib


def load_align_model(*args, **kwargs):
    return importlib.import_module("whisperx.alignment").load_align_model(*args, **kwargs)


def align(*args, **kwargs):
    return importlib.import_module("whisperx.alignment").align(*args, **kwargs)


def load_audio(*args, **kwargs):
    return importlib.import_module("whisperx.audio").load_audio(*args, **kwargs)
'''


def build(source: bytes) -> bytes:
    if hashlib.sha256(source).hexdigest() != UPSTREAM_SHA256:
        raise ValueError("Upstream WhisperX checksum mismatch")
    info = f"whisperx-{VERSION}.dist-info"
    with zipfile.ZipFile(io.BytesIO(source)) as wheel:
        files = {f"whisperx/{name}": wheel.read(f"whisperx/{name}") for name in MODULES}
        for name in wheel.namelist():
            if name.startswith("whisperx/assets/") and not name.endswith("/"):
                files[name] = wheel.read(name)
        licenses = [name for name in wheel.namelist() if name.endswith("/LICENSE")]
        if len(licenses) != 1:
            raise ValueError("Expected exactly one upstream license")
        files[f"{info}/licenses/LICENSE"] = wheel.read(licenses[0])
    files["whisperx/__init__.py"] = INIT.encode()
    files[f"{info}/METADATA"] = (
        f"Metadata-Version: 2.4\nName: whisperx\nVersion: {VERSION}\n"
        "Summary: Unmodified WhisperX alignment, packaged for Crowbarr\n"
        "Requires-Python: >=3.10,<3.14\nLicense-Expression: BSD-2-Clause\n"
        "License-File: licenses/LICENSE\n"
        + "".join(f"Requires-Dist: {dep}\n" for dep in DEPENDENCIES)
        + "\nAlignment-only distribution. See PROVENANCE.md.\n"
    ).encode()
    files[f"{info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: crowbarr-alignment\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files[f"{info}/PROVENANCE.md"] = (
        f"Source: {UPSTREAM_URL}\nSHA256: {UPSTREAM_SHA256}\n\n"
        "Retained modules and assets are byte-for-byte upstream copies.\n"
        "Replaced package initialization and dependency metadata; removed the CLI,\n"
        "transcription, VAD and diarization modules. Crowbarr performs speech recognition\n"
        "through faster-whisper directly. This is not the upstream WhisperX distribution.\n"
    ).encode()
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, content in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        writer.writerow((name, f"sha256={digest}", len(content)))
    writer.writerow((f"{info}/RECORD", "", ""))
    files[f"{info}/RECORD"] = record.getvalue().encode()
    output = io.BytesIO()
    # Stored entries avoid zlib-version-dependent wheel hashes; this wheel is small.
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as wheel:
        for name, content in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            wheel.writestr(entry, content)
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Previously downloaded upstream wheel")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.source:
        source = args.source.read_bytes()
    else:
        with urllib.request.urlopen(UPSTREAM_URL, timeout=60) as response:
            source = response.read()
    result = build(source)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / FILENAME).write_bytes(result)
    print(f"{FILENAME} sha256:{hashlib.sha256(result).hexdigest()}")


if __name__ == "__main__":
    main()
