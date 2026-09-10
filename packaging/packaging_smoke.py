"""Exercise packaged inference and real job processing in a spawned child.

Release checks download the tiny speech model and decode generated audio on CPU.
This validates packaging, not recognition accuracy or GPU execution. Missing
alignment imports fail instead of becoming an optional-refinement warning.
"""

import multiprocessing
import os
import subprocess
import tempfile
import time
from pathlib import Path


def check_imports():
    import ctranslate2
    import faster_whisper
    import torch
    import torchaudio
    from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor
    from whisperx.alignment import align, load_align_model

    assert all((ctranslate2, faster_whisper, torchaudio, Wav2Vec2ForCTC, Wav2Vec2Processor))
    assert callable(align) and callable(load_align_model)
    assert torch.ones(2).sum().item() == 2


def load_smoke_model(model_class, root, sleep=time.sleep):
    """Download the small test model with retries for transient Hub failures."""
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "60")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
    error = None
    for delay in (0, 3, 10):
        if delay:
            sleep(delay)
        try:
            return model_class(
                "tiny",
                device="cpu",
                compute_type="int8",
                cpu_threads=2,
                download_root=str(root / "models"),
            )
        except Exception as caught:
            error = caught
    raise RuntimeError("Could not download the tiny model after three attempts") from error


def process_fixture(directory, check_inference=True):
    if check_inference:
        check_imports()
    from crowbarr.config import Settings
    from crowbarr.db import Database
    from crowbarr.library import scan
    from crowbarr.service import run_job

    root = Path(directory)
    if check_inference:
        import numpy as np
        from faster_whisper import WhisperModel

        model = load_smoke_model(WhisperModel, root)
        audio = (0.1 * np.sin(2 * np.pi * 440 * np.arange(16000) / 16000)).astype(np.float32)
        segments, _ = model.transcribe(audio, language="en", beam_size=1, vad_filter=False)
        # Exhaust the lazy iterator to execute the encoder and decoder.
        list(segments)
        del model
    media = root / "library" / "fixture.mkv"
    media.parent.mkdir()
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=160x90:d=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "mpeg4",
         "-c:a", "pcm_s16le", "-metadata:s:a:0", "language=eng", "-shortest", str(media)],
        check=True, timeout=30,
    )
    media.with_suffix(".en.srt").write_text("not a valid subtitle", encoding="utf-8")
    settings = Settings(roots=[str(media.parent)], settle_seconds=0, subtitle_wait_minutes=0)
    state = root / "state"
    db = Database(state / "crowbarr.db")
    scan(settings, db)
    job = db.claim()
    assert job, "Fixture was not discovered and queued"
    run_job(str(state), job, settings.model_dump())
    result = db.get(job["id"])
    assert result["state"] == "review", result
    assert "No discovered subtitle source could be read" in result["error"], result
    assert not media.with_suffix(".crowbarr.en.srt").exists()


def smoke(check_inference=True):
    if check_inference:
        # Import directly first. The capability probe reports a category, so a packaging
        # gap must raise its own traceback before that category can hide it.
        check_imports()
        from crowbarr.capabilities import runtime_capabilities

        runtime = runtime_capabilities()
        assert runtime["cpu"]["available"], runtime["cpu"]["reason"]
        assert runtime["refinement"]["available"], runtime["refinement"]["reason"]
    with tempfile.TemporaryDirectory(prefix="crowbarr-package-") as directory:
        child = multiprocessing.get_context("spawn").Process(
            target=process_fixture, args=(directory, check_inference)
        )
        child.start()
        child.join(timeout=180)
        if child.is_alive():
            child.kill()
            child.join()
            raise RuntimeError("Packaging child timed out")
        assert child.exitcode == 0, f"Packaging child exited {child.exitcode}"
        child.close()
    print("Packaging imports and spawned job processing passed", flush=True)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    smoke()
