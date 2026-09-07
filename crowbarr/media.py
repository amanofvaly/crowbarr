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


def choose_audio(metadata: dict, settings: Settings) -> dict:
    streams = [s for s in metadata["streams"] if s["codec_type"] == "audio"]
    clean = [
        s
        for s in streams
        if not any(
            word in s.get("tags", {}).get("title", "").lower()
            for word in ("commentary", "description", "descriptive")
        )
        and not s.get("disposition", {}).get("visual_impaired")
    ]
    matches = [s for s in clean if s.get("tags", {}).get("language", "").lower() in {"en", "eng"}]
    if not matches and settings.allow_untagged_audio:
        matches = [s for s in clean if s.get("tags", {}).get("language", "und") in {"und", ""}]
    if not matches:
        raise ReviewRequired("No English dialogue track found. Check audio tags or enable untagged audio.")
    defaults = [s for s in matches if s.get("disposition", {}).get("default")]
    if len(matches) > 1 and len(defaults) != 1:
        raise ReviewRequired(
            "Multiple English audio tracks without a unique default; mark the dialogue track default."
        )
    return (defaults or matches)[0]


def extract_audio(media: Path, destination: Path, metadata: dict, stream: dict) -> tuple[float, float]:
    origin = float(metadata.get("format", {}).get("start_time", 0))
    offset = float(stream.get("start_time", origin)) - origin
    duration = float(metadata.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(offset):
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


def embedded_subtitle(media: Path, destination: Path, metadata: dict) -> Path | None:
    supported = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}
    for stream in metadata["streams"]:
        if (
            stream["codec_type"] == "subtitle"
            and stream.get("codec_name") in supported
            and stream.get("tags", {}).get("language") in {"en", "eng"}
            and not stream.get("disposition", {}).get("forced")
        ):
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
            return destination
    return None
