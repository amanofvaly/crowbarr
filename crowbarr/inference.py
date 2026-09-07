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


def transcribe(audio: Path, settings: Settings, cache: Path) -> tuple[list[Word], list[str]]:
    from faster_whisper import WhisperModel

    model = WhisperModel(
        settings.model,
        device=settings.device,
        compute_type=settings.compute_type,
        cpu_threads=settings.cpu_threads,
        num_workers=1,
        download_root=str(cache / "whisper"),
    )
    # Automatic language detection is a guard against wrong/untagged audio; never translate for alignment.
    segments, info = model.transcribe(
        str(audio), word_timestamps=True, vad_filter=True, condition_on_previous_text=False, beam_size=5
    )
    if info.language != settings.language or info.language_probability < 0.65:
        raise ReviewRequired("Audio language detection does not confidently match English")
    words, issues = [], []
    for segment in segments:
        if segment.no_speech_prob > 0.6 or segment.avg_logprob < -1.0:
            issues.append(f"Uncertain speech near {segment.start:.1f}s")
        for word in segment.words or []:
            if word.end > word.start and word.word.strip():
                words.append(Word(word.start, word.end, word.word.strip(), word.probability))
    if not words:
        raise ReviewRequired("No speech found in the selected audio track")
    if sum(w.probability < 0.5 for w in words) / len(words) > 0.1:
        issues.append("More than 10% of recognized words have low confidence")
    del segments, model
    gc.collect()
    return words, issues


def align(
    audio: Path, passages: list[Passage], settings: Settings, cache: Path
) -> tuple[list[Cue], list[str]]:
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
                issues.append(
                    f"Passage {index + 1}: kept Whisper timestamps after weak forced alignment"
                )
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
