"""Integrity and release guardrails for the reduced inference distribution."""
import base64
import csv
import hashlib
import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


alignment = load("alignment_wheel", "packaging/inference/build_alignment.py")
image_size = load("image_size", "packaging/image_size.py")
inference_check = load("inference_check", "packaging/inference_check.py")


def test_alignment_rejects_unverified_source():
    with pytest.raises(ValueError, match="checksum"):
        alignment.build(b"unexpected upstream bytes")


def test_alignment_wheel_preserves_source_license_and_has_valid_record(monkeypatch):
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as wheel:
        for name in alignment.MODULES:
            wheel.writestr(f"whisperx/{name}", f"# upstream {name}\n")
        wheel.writestr("whisperx-3.8.6.dist-info/licenses/LICENSE", "BSD-2-Clause upstream licence")
        wheel.writestr("whisperx/diarize.py", "raise RuntimeError('must not ship')")
    data = source.getvalue()
    monkeypatch.setattr(alignment, "UPSTREAM_SHA256", hashlib.sha256(data).hexdigest())
    built = alignment.build(data)
    assert built == alignment.build(data)
    with zipfile.ZipFile(io.BytesIO(built)) as wheel:
        names = wheel.namelist()
        assert "whisperx/diarize.py" not in names
        for name in alignment.MODULES:
            assert wheel.read(f"whisperx/{name}") == f"# upstream {name}\n".encode()
        assert wheel.read(next(n for n in names if n.endswith("/LICENSE"))).startswith(b"BSD-2-Clause")
        metadata = wheel.read(next(n for n in names if n.endswith("/METADATA"))).decode()
        assert "Requires-Dist: torch~=2.8.0" in metadata
        assert "pyannote" not in metadata and "torchvision" not in metadata
        record = wheel.read(next(n for n in names if n.endswith("/RECORD"))).decode()
        for name, digest, size in csv.reader(io.StringIO(record)):
            if not digest:
                assert name.endswith("/RECORD")
                continue
            content = wheel.read(name)
            expected = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
            assert digest == f"sha256={expected}"
            assert int(size) == len(content)


def test_size_budget_rejects_growth_above_five_percent():
    budget = {"cpu": {"unpacked_bytes": 1000}}
    image_size.check_budget({"unpacked_bytes": 1050}, budget, "cpu")
    with pytest.raises(ValueError, match="budget"):
        image_size.check_budget({"unpacked_bytes": 1051}, budget, "cpu")


def test_inference_comparison_rejects_silent_output_changes():
    report = {
        "device": "cuda", "fixture_sha256": "fixture", "issues": [],
        "words": [{"word": "hello", "start": 1.0, "end": 1.5}],
        "aligned_words": [], "cues": [],
    }
    candidate = json.loads(json.dumps(report))
    inference_check.compare(report, candidate)
    candidate["words"][0]["start"] = 1.1
    with pytest.raises(AssertionError):
        inference_check.compare(report, candidate)
    candidate["words"][0]["start"] = 1.0
    candidate["words"][0]["word"] = "goodbye"
    with pytest.raises(AssertionError):
        inference_check.compare(report, candidate)
