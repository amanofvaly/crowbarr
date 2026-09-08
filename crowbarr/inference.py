"""Optional, heavyweight backends. Only the job subprocess imports these libraries."""

from __future__ import annotations

import gc
import math
import textwrap
import wave
from pathlib import Path

from .config import Settings
from .media import ReviewRequired
from .subtitles import Cue, Passage, Word, alignment_text

_resident = None
_resident_key = None
_resident_runtime = {}


def transcribe(audio: Path, settings: Settings, cache: Path) -> tuple[list[Word], list[str]]:
    from faster_whisper import WhisperModel

    global _resident, _resident_key, _resident_runtime
    backend, compute = settings.device, settings.compute_type
    model_key = (settings.model, backend, compute, settings.cpu_threads)
    fallback_reason = None
    try:
        model = (
            _resident
            if _resident_key == model_key
            else WhisperModel(
                settings.model,
                device=backend,
                compute_type=compute,
                cpu_threads=settings.cpu_threads,
                num_workers=1,
                download_root=str(cache / "whisper"),
            )
        )
    except (RuntimeError, ValueError) as error:
        if backend != "cuda" or not settings.cpu_fallback:
            raise
        fallback_reason = type(error).__name__ + ": CUDA initialization failed"
        backend, compute = "cpu", "int8"
        model = WhisperModel(
            settings.model,
            device=backend,
            compute_type=compute,
            cpu_threads=settings.cpu_threads,
            num_workers=1,
            download_root=str(cache / "whisper"),
        )
    transcribe.runtime = {
        "backend": backend,
        "compute_type": compute,
        "requested_backend": settings.device,
        "fallback_reason": fallback_reason,
    }
    if _resident_key == model_key:
        transcribe.runtime = _resident_runtime.copy()
    _resident_runtime = transcribe.runtime.copy()
    _resident, _resident_key = model, model_key
    # Automatic language detection is a guard against wrong/untagged audio; never translate for alignment.
    segments, info = model.transcribe(
        str(audio), word_timestamps=True, vad_filter=True, condition_on_previous_text=False, beam_size=5
    )
    # This, not the container tag, is what establishes the spoken language. Report the
    # measurement: "not English" is a conclusion, and the user cannot check a conclusion.
    if info.language != settings.language or info.language_probability < 0.65:
        raise ReviewRequired(
            f"Audio was heard as {info.language} with {info.language_probability:.0%} confidence, "
            f"not {settings.language}"
        )
    words, issues = [], []
    for segment in segments:
        callback = getattr(transcribe, "progress", None)
        if callback:
            callback(segment.end, info.duration)
        if segment.no_speech_prob > 0.6 or segment.avg_logprob < -1.0:
            issues.append(f"Uncertain speech near {segment.start:.1f}s")
        for word in segment.words or []:
            if word.end > word.start and word.word.strip():
                probability = (
                    0.0 if segment.no_speech_prob > 0.6 or segment.avg_logprob < -1.0 else word.probability
                )
                words.append(Word(word.start, word.end, word.word.strip(), probability))
    if not words:
        raise ReviewRequired("No speech found in the selected audio track")
    if sum(w.probability < 0.5 for w in words) / len(words) > 0.1:
        issues.append("More than 10% of recognized words have low confidence")
    del segments
    gc.collect()
    return words, issues


def align(
    audio: Path, passages: list[Passage], settings: Settings, cache: Path
) -> tuple[list[Cue], list[str]]:
    global _resident, _resident_key
    _resident, _resident_key = None, None
    gc.collect()
    import numpy as np
    import torch
    import whisperx

    torch.set_num_threads(settings.cpu_threads)
    model, metadata = whisperx.load_align_model(
        language_code=settings.language, device=settings.device, model_dir=str(cache / "alignment")
    )
    cues, issues = [], []
    with wave.open(str(audio), "rb") as stream:
        rate = stream.getframerate()
        duration = stream.getnframes() / rate
        for index, passage in enumerate(passages):
            start, end = max(0, passage.start - 0.35), min(duration, passage.end + 0.35)
            stream.setpos(int(start * rate))
            samples = np.frombuffer(stream.readframes(int((end - start) * rate)), dtype=np.int16).astype(
                np.float32
            )
            samples /= 32768.0
            result = whisperx.align(
                [{"start": 0.0, "end": len(samples) / rate, "text": alignment_text(passage.text)}],
                model,
                metadata,
                samples,
                settings.device,
                interpolate_method="ignore",
            )
            words = result.get("word_segments", [])
            scored = [w for w in words if all(k in w for k in ("start", "end", "score"))]
            # Missing timings or scores are not silently interpolated into a passing result.
            if (
                not words
                or len(scored) != len(words)
                or any(
                    not math.isfinite(w["score"]) or w["score"] < settings.min_alignment_score for w in scored
                )
            ):
                # Faster Whisper already supplied usable timestamps. WhisperX is a
                # refinement step, so a weak phoneme alignment falls back locally.
                display = (
                    passage.display
                    if passage.authored
                    else "\n".join(textwrap.wrap(passage.display, width=42))
                )
                cues.append(Cue(passage.start, passage.end, display))
                issues.append(f"Passage {index + 1}: kept Whisper timestamps after weak forced alignment")
                continue
            cue_start, cue_end = start + scored[0]["start"], start + scored[-1]["end"]
            display = (
                passage.display if passage.authored else "\n".join(textwrap.wrap(passage.display, width=42))
            )
            cues.append(Cue(cue_start, cue_end, display))
    del model
    gc.collect()
    if settings.device == "cuda":
        torch.cuda.empty_cache()
    return cues, issues


def transcribe_windows(audio: Path, windows: list[tuple[float, float]], settings: Settings, cache: Path):
    import tempfile

    result, issues = [], []
    with wave.open(str(audio), "rb") as source, tempfile.TemporaryDirectory(dir=audio.parent) as work:
        rate = source.getframerate()
        total = sum(end - start for start, end in windows)
        completed = 0.0
        for index, (start, end) in enumerate(windows):
            clip = Path(work) / f"sample-{index}.wav"
            source.setpos(int(start * rate))
            with wave.open(str(clip), "wb") as target:
                target.setparams(source.getparams())
                target.writeframes(source.readframes(int((end - start) * rate)))
            callback = getattr(transcribe, "progress", None)
            transcribe.progress = (
                (
                    lambda position, duration, offset=completed, notify=callback: notify(
                        offset + position, total
                    )
                )
                if callback
                else None
            )
            try:
                words, warnings = transcribe(clip, settings, cache)
                result.extend(Word(w.start + start, w.end + start, w.text, w.probability) for w in words)
                issues.extend(f"Sample {start:.0f}s: {warning}" for warning in warnings)
            except ReviewRequired as error:
                issues.append(f"Sample near {start:.0f}s was not usable: {error}")
            finally:
                transcribe.progress = callback
                completed += end - start
                if callback:
                    callback(completed, total)
    return result, issues


def transcribe_bounded(audio: Path, settings: Settings, cache: Path, checkpoint: Path):
    """Bound decoder/VAD allocations and resume completed chunks after a restart."""
    import json
    import tempfile

    from .config import atomic_write

    checkpoint.mkdir(parents=True, exist_ok=True)
    result, issues = [], []
    uncertain_duration = 0
    with wave.open(str(audio), "rb") as source, tempfile.TemporaryDirectory(dir=audio.parent) as work:
        rate = source.getframerate()
        duration = source.getnframes() / rate
        for index in range(math.ceil(duration / 300)):
            core_start, core_end = index * 300, min(duration, (index + 1) * 300)
            start, end = max(0, core_start - 2), min(duration, core_end + 2)
            saved = checkpoint / f"{index}.json"
            if saved.exists():
                payload = json.loads(saved.read_text())
                chunk = [Word(**word) for word in payload["words"]]
                warnings = payload["issues"]
                transcribe.runtime = payload.get("runtime", {"backend": "cached"})
            else:
                clip = Path(work) / "chunk.wav"
                source.setpos(int(start * rate))
                with wave.open(str(clip), "wb") as target:
                    target.setparams(source.getparams())
                    target.writeframes(source.readframes(int((end - start) * rate)))
                callback = getattr(transcribe, "progress", None)
                transcribe.progress = (
                    (
                        lambda position, total, offset=start, notify=callback, full_duration=duration: notify(
                            position + offset, full_duration
                        )
                    )
                    if callback
                    else None
                )
                try:
                    try:
                        chunk, warnings = transcribe(clip, settings, cache)
                    except ReviewRequired as error:
                        chunk, warnings = [], [str(error)]
                finally:
                    transcribe.progress = callback
                chunk = [
                    Word(w.start + start, w.end + start, w.text, w.probability)
                    for w in chunk
                    if core_start <= w.start + start < core_end
                ]
                atomic_write(
                    saved,
                    json.dumps(
                        {
                            "words": [vars(word) for word in chunk],
                            "issues": warnings,
                            "runtime": getattr(transcribe, "runtime", {}),
                        }
                    ),
                )
            if not chunk:
                uncertain_duration += core_end - core_start
            result.extend(chunk)
            issues.extend(f"Chunk {index + 1}: {warning}" for warning in warnings)
            callback = getattr(transcribe, "progress", None)
            if callback:
                callback(core_end, duration)
    if not result or uncertain_duration > duration * 0.4:
        raise ReviewRequired("Too much of the audio lacks confidently identified English dialogue")
    # Final transcript is committed by the caller before checkpoint cleanup.
    return result, issues
