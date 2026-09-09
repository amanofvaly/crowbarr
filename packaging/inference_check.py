"""Real speech and alignment regression check; suitable for an offline cached rerun.

Run the same command in the baseline and candidate, then compare their JSON results.
The fixture is generated speech; it tests runtime equivalence, not model accuracy.
"""
from __future__ import annotations

import argparse
import dataclasses
import gc
import hashlib
import json
import os
import subprocess
import tempfile
import time
import wave
from pathlib import Path

TEXT = (
    "The library contains movies and television shows. Crowbarr checks the subtitles "
    "and matches each line to the spoken dialogue. This recording is used to test "
    "speech recognition and subtitle timing."
)


def run(args):
    import torch
    from faster_whisper import WhisperModel

    from crowbarr import inference
    from crowbarr.config import Settings
    from crowbarr.subtitles import Passage

    args.cache.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(2)
    if args.device == "cuda":
        assert torch.cuda.is_available(), "CUDA test requires a real GPU"
    with tempfile.TemporaryDirectory() as temp:
        audio = Path(temp) / "speech.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(args.audio), "-ar", "16000",
                        "-ac", "1", "-c:a", "pcm_s16le", str(audio)], check=True)
        with wave.open(str(audio)) as stream:
            duration = stream.getnframes() / stream.getframerate()
        model = WhisperModel("tiny", device=args.device,
                             compute_type="int8" if args.device == "cpu" else "int8_float32",
                             cpu_threads=2, download_root=str(args.cache / "speech"),
                             local_files_only=args.offline)
        recognition = []
        for _ in range(3):
            started = time.perf_counter()
            segments, _ = model.transcribe(str(audio), language="en", word_timestamps=True,
                                            beam_size=5, vad_filter=True)
            words = [dataclasses.asdict(word) for segment in segments for word in segment.words or []]
            recognition.append(time.perf_counter() - started)
        assert len(words) >= 15, f"Speech recognition returned too few words: {words}"
        del model
        gc.collect()
        if args.device == "cuda":
            torch.cuda.empty_cache()
        settings = Settings(device=args.device, cpu_fallback=False, cpu_threads=2, refine_generated=True)
        passages = [Passage(0, duration, TEXT, TEXT, False)]
        alignment = []
        for _ in range(3):
            started = time.perf_counter()
            cues, issues = inference.align(audio, passages, settings, args.cache)
            alignment.append(time.perf_counter() - started)
        assert inference.align.runtime["backend"] == args.device, inference.align.runtime
        assert inference.align.runtime["status"] == "completed", inference.align.runtime
        assert cues and all(cue.end > cue.start for cue in cues)
        # Check raw word alignment too, so conservative cue fallback cannot mask a broken model.
        from whisperx import align, load_align_model

        model, metadata = load_align_model("en", args.device, model_dir=str(args.cache / "alignment"))
        result = align([{"start": 0, "end": duration, "text": TEXT}], model, metadata,
                       str(audio), args.device, interpolate_method="ignore")
        aligned = result["word_segments"]
        assert sum("start" in word and "end" in word for word in aligned) >= 15, aligned
        del model
        gc.collect()
        fallback = None
        if args.device == "cpu":
            # Execute the real CPU retry after a controlled GPU-initialization failure.
            original = inference._align_attempt

            def attempt(audio, passages, settings, cache, backend, np, whisperx):
                if backend == "cuda":
                    raise inference._AlignmentFailure("Packaging test: CUDA initialization failed")
                return original(audio, passages, settings, cache, backend, np, whisperx)

            inference._align_attempt = attempt
            try:
                fallback_cues, _ = inference.align(
                    audio, passages, settings.model_copy(update={"device": "cuda", "cpu_fallback": True}),
                    args.cache,
                )
                assert fallback_cues == cues
                assert inference.align.runtime["backend"] == "cpu"
                assert inference.align.runtime["fallback_reason"]
                fallback = dict(inference.align.runtime)
            finally:
                inference._align_attempt = original
        return {
            "device": args.device, "offline": args.offline,
            "fixture_sha256": hashlib.sha256(args.audio.read_bytes()).hexdigest(),
            "words": words, "aligned_words": aligned,
            "cues": [dataclasses.asdict(cue) for cue in cues], "issues": issues,
            "recognition_seconds": recognition, "alignment_seconds": alignment,
            "fallback": fallback,
        }


def compare(before, after):
    assert before["device"] == after["device"]
    assert before["fixture_sha256"] == after["fixture_sha256"]
    for field in ("words", "aligned_words", "cues"):
        assert len(before[field]) == len(after[field]), field
        for old, new in zip(before[field], after[field], strict=True):
            assert old.keys() == new.keys(), (field, old, new)
            for key in old:
                if isinstance(old[key], (float, int)):
                    # Ten milliseconds is below the audit timing tolerances. Allow
                    # small floating-point differences in probability/score values too.
                    assert abs(old[key] - new[key]) <= 0.01, (field, key, old, new)
                else:
                    assert old[key] == new[key], (field, key, old, new)
    assert before["issues"] == after["issues"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, default=Path(__file__).parent / "fixtures/speech.flac")
    parser.add_argument("--cache", type=Path, default=Path("/config/models"))
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    report = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    if args.compare:
        compare(json.loads(args.compare.read_text()), report)
    print(f"{args.device} recognition, alignment and output checks passed", flush=True)


if __name__ == "__main__":
    main()
