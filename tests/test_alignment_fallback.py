import json
import logging
import math
import sys
import wave
from types import SimpleNamespace

import pytest
import test_processor

from crowbarr import inference
from crowbarr.config import Settings
from crowbarr.db import Database
from crowbarr.library import scan
from crowbarr.processor import process
from crowbarr.subtitles import Passage

video = test_processor.video
inference_stub = test_processor.inference_stub


SECRET = "https://user:private-password@example.invalid/model?token=private-token"


@pytest.fixture
def backend(monkeypatch):
    class Samples:
        def __init__(self, data):
            self.data = data

        def astype(self, dtype):
            return self

        def __itruediv__(self, divisor):
            return self

        def __len__(self):
            return len(self.data) // 2

    state = SimpleNamespace(loads=[], calls=[], failures=set(), cleanup_fails=False, weak=False,
                            cuda_torch=True)

    def load_align_model(*, language_code, device, model_dir):
        state.loads.append(device)
        if (device, "load") in state.failures:
            raise OSError(SECRET)
        return device, {"device": device}

    def align(segments, model, metadata, samples, device, **kwargs):
        assert model == metadata["device"] == device
        state.calls.append((device, segments[0]["text"], samples.data))
        count = sum(call[0] == device for call in state.calls)
        if (device, count) in state.failures:
            raise RuntimeError(SECRET)
        return {
            "word_segments": [
                {
                    "start": 0.2 if device == "cpu" else 0.1,
                    "end": 0.8,
                    "score": 0.0 if state.weak else 0.99,
                }
            ]
        }

    def empty_cache():
        if state.cleanup_fails:
            raise RuntimeError(SECRET)

    monkeypatch.setitem(
        sys.modules,
        "numpy",
        SimpleNamespace(frombuffer=lambda data, dtype: Samples(data), int16="int16", float32="float32"),
    )
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            set_num_threads=lambda count: None,
            cuda=SimpleNamespace(empty_cache=empty_cache, is_available=lambda: state.cuda_torch),
        ),
    )
    monkeypatch.setitem(
        sys.modules, "whisperx", SimpleNamespace(load_align_model=load_align_model, align=align)
    )
    monkeypatch.setattr(inference.align, "runtime", {}, raising=False)
    return state


@pytest.fixture
def alignment_input(tmp_path):
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\x00\x00" * 16000 * 8)
    passages = [Passage(i, i + 1, text, text, False) for i, text in [(1, "First"), (4, "Second")]]
    return audio, passages


@pytest.mark.parametrize("device", ["cuda", "cpu"])
def test_alignment_success_reports_actual_backend(backend, alignment_input, tmp_path, device):
    cues, issues = inference.align(*alignment_input, Settings(device=device), tmp_path)
    assert len(cues) == 2
    assert issues == []
    assert backend.loads == [device]
    assert inference.align.runtime == {
        "requested_backend": device,
        "backend": device,
        "fallback_reason": None,
        "status": "completed",
    }


@pytest.mark.parametrize("failure", ["load", 1, 2])
def test_cuda_failure_retries_all_passages_on_cpu(backend, alignment_input, tmp_path, failure):
    backend.failures.add(("cuda", failure))
    backend.cleanup_fails = True
    cues, issues = inference.align(*alignment_input, Settings(device="cuda"), tmp_path)
    assert backend.loads == ["cuda", "cpu"]
    assert [call[1] for call in backend.calls if call[0] == "cpu"] == ["First", "Second"]
    assert all(math.isclose(cue.start, start) for cue, start in zip(cues, [0.85, 3.85], strict=True))
    assert len(cues) == 2
    assert "CPU successfully" in issues[0]
    assert "CUDA" in issues[0]
    assert inference.align.runtime["backend"] == "cpu"
    assert inference.align.runtime["status"] == "completed"
    assert SECRET not in json.dumps([issues, inference.align.runtime])


def test_alignment_failure_cause_reaches_the_log(backend, alignment_input, tmp_path, caplog):
    backend.failures.add(("cuda", "load"))
    with caplog.at_level(logging.WARNING, logger="crowbarr.inference"):
        cues, issues = inference.align(*alignment_input, Settings(device="cuda"), tmp_path)
    assert cues, "the CPU retry still has to produce cues"
    # The operator needs the cause somewhere. The report and the dashboard are not it.
    assert SECRET not in json.dumps([issues, inference.align.runtime])
    logged = [record for record in caplog.records if "alignment model loading failed" in record.message]
    assert logged and all(record.exc_info for record in logged)


def test_cpu_torch_aligns_on_cpu_without_attempting_cuda(backend, alignment_input, tmp_path):
    """Torch is the CPU build in every image, so asking it for CUDA only fails twice."""
    backend.cuda_torch = False
    backend.failures.add(("cuda", "load"))
    cues, issues = inference.align(*alignment_input, Settings(device="cuda"), tmp_path)
    assert backend.loads == ["cpu"]
    assert len(cues) == 2
    assert inference.align.runtime["backend"] == "cpu"
    assert inference.align.runtime["fallback_reason"] is None
    assert not any("failed" in issue for issue in issues)


@pytest.mark.parametrize("failure", ["load", 1, 2])
def test_disabled_fallback_propagates_safe_failure(backend, alignment_input, tmp_path, failure):
    backend.failures.add(("cuda", failure))
    with pytest.raises(RuntimeError, match="CUDA alignment") as error:
        inference.align(*alignment_input, Settings(device="cuda", cpu_fallback=False), tmp_path)
    assert SECRET not in str(error.value)
    assert backend.loads == ["cuda"]
    assert inference.align.runtime["status"] == "failed"
    assert inference.align.runtime["backend"] is None


@pytest.mark.parametrize("failure", ["load", 1, 2])
def test_cpu_retry_failure_is_not_skipped(backend, alignment_input, tmp_path, failure):
    backend.failures.update({("cuda", "load"), ("cpu", failure)})
    with pytest.raises(RuntimeError, match="CPU alignment") as error:
        inference.align(*alignment_input, Settings(device="cuda"), tmp_path)
    assert SECRET not in str(error.value)
    assert backend.loads == ["cuda", "cpu"]
    assert inference.align.runtime["status"] == "failed"


def test_cpu_failure_does_not_retry(backend, alignment_input, tmp_path):
    backend.failures.add(("cpu", 1))
    with pytest.raises(RuntimeError, match="CPU alignment"):
        inference.align(*alignment_input, Settings(device="cpu"), tmp_path)
    assert backend.loads == ["cpu"]
    assert inference.align.runtime["fallback_reason"] is None


def test_weak_alignment_keeps_existing_timestamp_policy(backend, alignment_input, tmp_path):
    backend.weak = True
    cues, issues = inference.align(*alignment_input, Settings(device="cuda"), tmp_path)
    assert backend.loads == ["cuda"]
    assert [cue.start for cue in cues] == [1, 4]
    assert len(issues) == 2
    assert all("weak forced alignment" in issue for issue in issues)


def test_runtime_is_reset_between_calls(backend, alignment_input, tmp_path):
    backend.failures.add(("cuda", "load"))
    inference.align(*alignment_input, Settings(device="cuda"), tmp_path)
    backend.failures.clear()
    _, issues = inference.align(*alignment_input, Settings(device="cuda"), tmp_path)
    assert inference.align.runtime["backend"] == "cuda"
    assert inference.align.runtime["fallback_reason"] is None
    assert issues == []


@pytest.mark.parametrize("failure", [None, "load", 1])
def test_processor_reports_refinement_backend(backend, video, tmp_path, failure):
    settings, db, job = queued_refinement(video, tmp_path)
    if failure is not None:
        backend.failures.add(("cuda", failure))
    result = process(job, settings, db.path.parent, db, inference_stub)
    report = json.loads(result["report"])
    runtime = report["alignment_runtime"]
    assert runtime["backend"] == ("cpu" if failure is not None else "cuda")
    assert runtime["status"] == "completed"
    assert bool(runtime["fallback_reason"]) == (failure is not None)
    assert any("CPU successfully" in warning for warning in report["warnings"]) == (failure is not None)
    assert report["alignment_anchor_cues"] > 0
    assert SECRET not in result["report"]


def test_processor_does_not_publish_on_disabled_fallback(backend, video, tmp_path):
    settings, db, job = queued_refinement(video, tmp_path, cpu_fallback=False)
    backend.failures.add(("cuda", 1))
    with pytest.raises(RuntimeError, match="CUDA alignment"):
        process(job, settings, db.path.parent, db, inference_stub)
    assert not video.with_suffix(".crowbarr.en.srt").exists()


def test_missing_dependency_warning_does_not_expose_exception(video, tmp_path):
    settings, db, job = queued_refinement(video, tmp_path)

    def unavailable(*args):
        raise ImportError(SECRET)

    result = process(job, settings, db.path.parent, db, inference_stub, unavailable)
    report = json.loads(result["report"])
    assert report["alignment_runtime"]["backend"] is None
    assert report["alignment_runtime"]["status"] == "unavailable"
    assert any("not installed" in warning for warning in report["warnings"])
    assert SECRET not in result["report"]


def queued_refinement(video, tmp_path, cpu_fallback=True):
    settings = Settings(
        roots=[str(video.parent)],
        settle_seconds=0,
        subtitle_wait_minutes=0,
        device="cuda",
        refine_generated=True,
        cpu_fallback=cpu_fallback,
    )
    db = Database(tmp_path / "state" / "crowbarr.db")
    scan(settings, db)
    return settings, db, db.claim()
