"""The bad-section check for everyone, and High Accuracy turning the voice filter off."""

import json
import shutil
import subprocess

import pytest
from test_processor import alignment_stub, queued
from test_section_coverage import film, without

from crowbarr import inference
from crowbarr.audit import (
    SECTION_RUN_MINUTES,
    SECTION_RUN_SHARE,
    sample_missing_speech,
    section_coverage,
    with_section,
)
from crowbarr.config import FINGERPRINT_FIELDS, Settings
from crowbarr.db import Database
from crowbarr.library import scan
from crowbarr.processor import _recognized_words, _spaced_windows, process
from crowbarr.subtitles import Cue, Word, render_srt

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


def test_with_it_off_a_damaged_subtitle_is_replaced_from_one_hearing(feature, tmp_path):
    words = damaged_subtitle(feature)
    original = feature.with_suffix(".en.srt").read_text()
    transcriber, calls = hearing(words)
    settings, db, job = queued(feature, tmp_path)
    result = process(job, settings, db.path.parent, db, transcriber, alignment_stub)
    report = json.loads(result["report"])
    assert report["audit"]["before"]["decision"] == "damaged"
    assert report["audit"]["before"]["section_checks"]
    assert result["state"] == "completed"
    assert report["mode"] == "generated"
    assert feature.with_suffix(".en.srt").read_text() == original
    assert len(calls) == 1


def test_with_it_off_a_retimed_subtitle_is_still_published(feature, tmp_path):
    cues, words = film(MINUTES)
    late = [Cue(c.start + 5, c.end + 5, c.text) for c in cues]
    feature.with_suffix(".en.srt").write_text(render_srt(late))
    transcriber, _ = hearing(words)
    settings, db, job = queued(feature, tmp_path)
    result = process(job, settings, db.path.parent, db, transcriber, alignment_stub)
    assert result["state"] != "review"
    assert not any("held for review" in issue for issue in json.loads(result["report"])["issues"])


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


def covered_by_a_sample(windows, first, last):
    return any(first <= start and end <= last for start, end in windows)


@pytest.mark.parametrize("minutes", [45, 60, 90, 120, 180])
def test_a_long_bad_section_cannot_fit_between_samples(minutes):
    duration = minutes * 60.0
    windows = _spaced_windows(duration)
    limit = min(SECTION_RUN_MINUTES * 60, SECTION_RUN_SHARE * duration)
    for first in range(0, int(duration - limit), 30):
        assert covered_by_a_sample(windows, first, first + limit), f"{limit}s bad run at {first}s was missed"


def test_samples_listen_to_much_less_than_the_film():
    assert len(_spaced_windows(90 * 60.0)) == 8
    assert len(_spaced_windows(120 * 60.0)) == 9
    assert len(_spaced_windows(45 * 60.0)) <= 10


def test_a_recording_too_short_to_sample_safely_is_heard_whole():
    assert _spaced_windows(15 * 60.0) == []


def test_a_sample_with_clear_speech_and_no_subtitle_lines_is_bad():
    cues, words = film(60)
    checks = sample_missing_speech(without(cues, 60, 0.4, 0.6), words, [(300, 420), (1800, 1920)])
    assert [check["bad"] for check in checks] == [False, True]


def test_a_sample_of_music_or_silence_is_not_bad():
    cues, words = film(60)
    spoken_elsewhere = [word for word in words if not 1800 <= word.start < 1920]
    checks = sample_missing_speech(without(cues, 60, 0.4, 0.6), spoken_elsewhere, [(1800, 1920)])
    assert checks[0]["bad"] is False


def test_a_sample_of_unclear_speech_is_not_bad():
    cues, words = film(60)
    unsure = [Word(w.start, w.end, w.text, 0.3) for w in words]
    assert sample_missing_speech(without(cues, 60, 0.4, 0.6), unsure, [(1800, 1920)])[0]["bad"] is False


def sampled_hearing(monkeypatch, words):
    """Real sampling path: samples hear only their windows, a full hearing hears everything."""
    sampled, whole = [], []

    def windowed(audio, windows, settings, cache):
        sampled.append(list(windows))
        return [w for w in words if any(a <= w.start < b for a, b in windows)], []

    def bounded(audio, settings, cache, checkpoint, speech_filter=True):
        whole.append(speech_filter)
        return words, []

    monkeypatch.setattr(inference, "verify_audio_language", lambda *args: {"language": "en"})
    monkeypatch.setattr(inference, "transcribe_windows", windowed)
    monkeypatch.setattr(inference, "transcribe_bounded", bounded)
    return sampled, whole


def test_with_it_on_samples_find_a_hole_and_the_subtitle_is_replaced(feature, tmp_path, monkeypatch):
    """Shrek passed three samples with 22 minutes deleted, so nothing was replaced."""
    words = damaged_subtitle(feature)
    sampled, whole = sampled_hearing(monkeypatch, words)
    settings, db, job = queued_on(feature, tmp_path)
    result = process(job, settings, db.path.parent, db, aligner=alignment_stub)
    report = json.loads(result["report"])
    assert len(sampled[0]) > 3
    assert whole[0] is False
    assert report["full_audit_escalation"] is True
    assert report["audit"]["before"]["decision"] == "damaged"
    assert result["state"] == "completed"
    assert report["mode"] == "generated"


def test_with_it_on_a_complete_subtitle_is_settled_by_samples(feature, tmp_path, monkeypatch):
    cues, words = film(MINUTES)
    feature.with_suffix(".en.srt").write_text(render_srt(cues))
    sampled, whole = sampled_hearing(monkeypatch, words)
    settings, db, job = queued_on(feature, tmp_path)
    result = process(job, settings, db.path.parent, db, aligner=alignment_stub)
    assert sampled and whole == []
    assert result["state"] == "unchanged"


def test_with_it_off_samples_find_a_hole_and_keep_the_voice_filter(feature, tmp_path, monkeypatch):
    words = damaged_subtitle(feature)
    sampled, whole = sampled_hearing(monkeypatch, words)
    settings, db, job = queued(feature, tmp_path)
    result = process(job, settings, db.path.parent, db, aligner=alignment_stub)
    report = json.loads(result["report"])
    assert len(sampled[0]) > 3
    assert whole == [True]
    assert report["full_audit_escalation"] is True
    assert report["audit"]["before"]["decision"] == "damaged"
    assert result["state"] == "completed"


def test_with_it_off_a_complete_subtitle_is_settled_by_samples(feature, tmp_path, monkeypatch):
    cues, words = film(MINUTES)
    feature.with_suffix(".en.srt").write_text(render_srt(cues))
    sampled, whole = sampled_hearing(monkeypatch, words)
    settings, db, job = queued(feature, tmp_path)
    result = process(job, settings, db.path.parent, db, aligner=alignment_stub)
    assert sampled and whole == []
    assert result["state"] == "unchanged"
