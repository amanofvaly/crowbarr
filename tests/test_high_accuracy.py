"""High Accuracy: off changes nothing, on hears everything and replaces damaged subtitles."""

import json
import shutil
import subprocess

import pytest
from test_processor import alignment_stub, queued
from test_section_coverage import film, without

from crowbarr import inference
from crowbarr.audit import section_coverage, with_section
from crowbarr.config import FINGERPRINT_FIELDS, Settings
from crowbarr.db import Database
from crowbarr.library import scan
from crowbarr.processor import _recognized_words, process
from crowbarr.subtitles import Cue, render_srt

MINUTES = 40


@pytest.fixture
def feature(tmp_path):
    """Long enough for the five-minute buckets the bad-section check is judged by."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is required for media integration tests")
    path = tmp_path / "library" / "feature.mkv"
    path.parent.mkdir()
    seconds = MINUTES * 60
    subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-f", "lavfi", "-i", f"color=c=black:s=64x36:r=1:d={seconds}",
            "-f", "lavfi", "-i", f"anullsrc=r=8000:cl=mono:d={seconds}",
            "-c:v", "mpeg4", "-c:a", "pcm_s16le",
            "-metadata:s:a:0", "language=eng", "-shortest", str(path),
        ],
        check=True,
    )
    return path


def on(settings):
    return settings.model_copy(update={"high_accuracy": True})


def queued_on(video, tmp_path):
    db = Database(tmp_path / "state" / "crowbarr.db")
    settings = Settings(
        roots=[str(video.parent)], settle_seconds=0, subtitle_wait_minutes=0, min_duration_minutes=0,
        high_accuracy=True,
    )
    scan(settings, db)
    return settings, db, db.claim()


def hearing(words):
    calls = []

    def transcriber(*args):
        calls.append(args)
        return words, []

    return transcriber, calls


def damaged_subtitle(feature):
    cues, words = film(MINUTES)
    feature.with_suffix(".en.srt").write_text(render_srt(without(cues, MINUTES, 0.375, 0.625)))
    return words


def test_the_setting_is_off_by_default():
    assert Settings().high_accuracy is False


def test_upgrading_with_it_off_does_not_recheck_the_library():
    """A fingerprint change re-checks every file, so the off state must hash as before."""
    import hashlib

    old = {k: getattr(Settings(), k) for k in FINGERPRINT_FIELDS if k != "high_accuracy"}
    expected = hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest()
    assert Settings().fingerprint() == expected


def test_turning_it_on_rechecks_the_library():
    assert on(Settings()).fingerprint() != Settings().fingerprint()
    assert "high_accuracy" in FINGERPRINT_FIELDS


def test_a_filter_off_transcript_is_cached_apart_from_the_filtered_one(feature, tmp_path, monkeypatch):
    seen = []

    def bounded(audio, settings, cache, checkpoint, speech_filter=True):
        seen.append(speech_filter)
        return film(1)[1], []

    monkeypatch.setattr(inference, "transcribe_bounded", bounded)
    settings = Settings(roots=[str(feature.parent)])
    state = tmp_path / "state"
    for speech_filter in (True, False, True, False):
        _recognized_words(
            feature, tmp_path / "a.wav", settings, state, tmp_path, inference.transcribe,
            speech_filter=speech_filter,
        )
    assert seen == [True, False]


def test_section_verdict_leaves_a_clean_subtitle_alone():
    cues, words = film(60)
    verdict = with_section({"decision": "pass", "reason": "fine"}, section_coverage(cues, words, 3600))
    assert verdict["decision"] == "pass"
    assert verdict["section_checks"]


def test_section_verdict_does_not_overrule_a_stronger_finding():
    cues, words = film(60)
    damaged = section_coverage(without(cues, 60, 0.2, 0.6), words, 3600)
    assert with_section({"decision": "mismatched", "reason": "x"}, damaged)["decision"] == "mismatched"
    assert with_section({"decision": "pass", "reason": "x"}, damaged)["decision"] == "damaged"


def test_with_it_off_a_damaged_subtitle_gets_todays_verdict(feature, tmp_path):
    words = damaged_subtitle(feature)
    transcriber, calls = hearing(words)
    settings, db, job = queued(feature, tmp_path)
    result = process(job, settings, db.path.parent, db, transcriber, alignment_stub)
    before = json.loads(result["report"])["audit"]["before"]
    assert before["decision"] != "damaged"
    assert "section_checks" not in before
    assert len(calls) == 1


def test_with_it_on_a_damaged_subtitle_is_replaced_not_sent_to_review(feature, tmp_path):
    words = damaged_subtitle(feature)
    original = feature.with_suffix(".en.srt").read_text()
    transcriber, calls = hearing(words)
    settings, db, job = queued_on(feature, tmp_path)
    result = process(job, settings, db.path.parent, db, transcriber, alignment_stub)
    report = json.loads(result["report"])
    assert report["audit"]["before"]["decision"] == "damaged"
    assert report["audit"]["before"]["section_checks"]
    assert result["state"] == "completed"
    assert report["mode"] == "generated"
    assert feature.with_suffix(".crowbarr.en.srt").exists()
    assert feature.with_suffix(".en.srt").read_text() == original


def test_a_generated_subtitle_is_built_from_a_second_filtered_hearing(feature, tmp_path):
    """Filter-off text is untested for writing subtitles, so generation keeps the filter."""
    words = damaged_subtitle(feature)
    transcriber, calls = hearing(words)
    settings, db, job = queued_on(feature, tmp_path)
    process(job, settings, db.path.parent, db, transcriber, alignment_stub)
    assert len(calls) == 2


def test_with_it_on_a_retimed_subtitle_waits_for_review(feature, tmp_path):
    cues, words = film(MINUTES)
    late = [Cue(c.start + 5, c.end + 5, c.text) for c in cues]
    feature.with_suffix(".en.srt").write_text(render_srt(late))
    transcriber, _ = hearing(words)
    settings, db, job = queued_on(feature, tmp_path)
    result = process(job, settings, db.path.parent, db, transcriber, alignment_stub)
    assert result["state"] == "review"
    assert any("held for review" in issue for issue in json.loads(result["report"])["issues"])
    assert not feature.with_suffix(".crowbarr.en.srt").exists()
