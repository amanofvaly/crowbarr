from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

from .config import Settings


class ReviewRequired(Exception):
    """An input/quality issue that retries cannot safely repair."""


def probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(result.stdout)


def duration_seconds(metadata: dict) -> float:
    duration = float(metadata.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ReviewRequired("Video has an invalid or unknown timeline")
    return duration


COMMENTARY_WORDS = ("commentary", "description", "descriptive")


def describe_audio(metadata: dict) -> str:
    """Say what the file actually contains, so a refusal can be checked against it."""
    streams = [s for s in metadata["streams"] if s["codec_type"] == "audio"]
    if not streams:
        return "The file contains no audio track at all."
    described = []
    for stream in streams:
        tags = stream.get("tags", {})
        title = tags.get("title", "").strip()
        described.append(
            "stream {index} {codec} {channels}ch tagged {language}{default}{title}".format(
                index=stream.get("index", "?"),
                codec=stream.get("codec_name", "unknown"),
                channels=stream.get("channels", "?"),
                language=tags.get("language") or "nothing",
                default=", default" if stream.get("disposition", {}).get("default") else "",
                title=f', titled "{title}"' if title else "",
            )
        )
    return "Audio tracks present: " + "; ".join(described) + "."


def audio_candidates(metadata: dict) -> list[dict]:
    """Rank the tracks that could carry dialogue, preferring but never requiring a tag.

    Container language tags are frequently wrong, and a wrong tag is not a reason to
    refuse work: recognition detects the spoken language from the audio itself, so the
    tag only has to break ties. Commentary and described-video tracks are still excluded,
    because those carry speech that is genuinely not the dialogue being subtitled.
    """
    candidates = []
    for stream in metadata["streams"]:
        if stream["codec_type"] != "audio":
            continue
        tags = stream.get("tags", {})
        if any(word in tags.get("title", "").lower() for word in COMMENTARY_WORDS):
            continue
        if stream.get("disposition", {}).get("visual_impaired"):
            continue
        language = (tags.get("language") or "").lower()
        candidates.append(
            {
                "stream": stream,
                "index": stream.get("index"),
                "language": language or "untagged",
                "channels": stream.get("channels") or 0,
                "default": bool(stream.get("disposition", {}).get("default")),
                # 0 tagged English, 1 tagged nothing, 2 tagged another language.
                "tier": 0 if language in {"en", "eng"} else 1 if language in {"und", ""} else 2,
            }
        )
    # Within a tier the default track wins, then the fullest mix -- a 5.1 track carries
    # dialogue in its own centre channel, which survives the downmix more cleanly than
    # a stereo fold-down -- and finally the earliest stream, so the choice is stable.
    candidates.sort(key=lambda c: (c["tier"], not c["default"], -c["channels"], c["index"]))
    return candidates


def choose_audio(metadata: dict, settings: Settings) -> dict:
    candidates = audio_candidates(metadata)
    if not candidates:
        raise ReviewRequired(f"No track could carry dialogue. {describe_audio(metadata)}")
    return candidates[0]["stream"]


def extract_audio(media: Path, destination: Path, metadata: dict, stream: dict) -> tuple[float, float]:
    origin = float(metadata.get("format", {}).get("start_time", 0))
    offset = float(stream.get("start_time", origin)) - origin
    duration = duration_seconds(metadata)
    if not math.isfinite(offset):
        raise ReviewRequired("Video has an invalid or unknown timeline")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(media),
            "-map",
            f"0:{stream['index']}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        check=True,
        capture_output=True,
        timeout=3600,
    )
    return offset, duration


def unreadable_subtitles(metadata: dict) -> list[str]:
    """Name English subtitle tracks that exist but cannot be read as text.

    Blu-ray and DVD remuxes usually store subtitles as bitmaps, so a file can carry a
    perfectly good English subtitle that Crowbarr cannot use without OCR. Skipping it
    silently makes the job look like an episode with no subtitle at all.
    """
    return [
        f"embedded stream {stream['index']} ({stream.get('codec_name')})"
        for stream in metadata["streams"]
        if stream["codec_type"] == "subtitle"
        and stream.get("codec_name") not in EMBEDDED_TEXT_CODECS
        and stream.get("tags", {}).get("language", "und").lower() in {"en", "eng"}
    ]


EMBEDDED_TEXT_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}


def embedded_subtitles(media: Path, directory: Path, metadata: dict, settings: Settings) -> list[Path]:
    supported = EMBEDDED_TEXT_CODECS
    results = []
    for stream in metadata["streams"]:
        language = stream.get("tags", {}).get("language", "und").lower()
        if (
            stream["codec_type"] == "subtitle"
            and stream.get("codec_name") in supported
            and (language in {"en", "eng"} or (settings.allow_untagged_subtitles and language in {"", "und"}))
            # A forced track carries only translated signage and foreign lines, so it is
            # never the subtitle being audited. Muxers often leave the disposition flag
            # clear and say so in the title instead, exactly as commentary audio does.
            and not stream.get("disposition", {}).get("forced")
            and "forced" not in stream.get("tags", {}).get("title", "").lower()
        ):
            destination = directory / f"embedded-{stream['index']}.srt"
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(media),
                    "-map",
                    f"0:{stream['index']}",
                    str(destination),
                ],
                check=True,
                capture_output=True,
                timeout=300,
            )
            results.append(destination)
    return results


def embedded_subtitle(
    media: Path, destination: Path, metadata: dict, settings: Settings | None = None
) -> Path | None:
    """Compatibility wrapper for callers that need only one embedded subtitle."""
    candidates = embedded_subtitles(media, destination.parent, metadata, settings or Settings())
    if not candidates:
        return None
    if candidates[0] != destination:
        candidates[0].replace(destination)
    return destination
