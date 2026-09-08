"""Exercise installed/frozen imports and real job processing in a spawned child.

No models or user media are needed. This validates packaging, not model accuracy
or GPU execution. Missing alignment imports must fail instead of being softened
into the processor's optional-refinement warning.
"""

import multiprocessing
import subprocess
import tempfile
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


def process_fixture(directory, check_inference=True):
    if check_inference:
        check_imports()
    from crowbarr.config import Settings
    from crowbarr.db import Database
    from crowbarr.library import scan
    from crowbarr.service import run_job

    root = Path(directory)
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
