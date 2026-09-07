import hashlib
import json
import shutil
import subprocess

import pytest

from crowbarr.config import Settings
from crowbarr.db import Database
from crowbarr.library import scan
from crowbarr.media import ReviewRequired, choose_audio, extract_audio, probe
from crowbarr.processor import process
from crowbarr.subtitles import Cue, Word, parse_srt, render_srt


@pytest.fixture
def video(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is required for media integration tests")
    path = tmp_path / "library" / "sample.mkv"
    path.parent.mkdir()
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:d=8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=8",
            "-c:v",
            "mpeg4",
            "-c:a",
            "pcm_s16le",
            "-metadata:s:a:0",
            "language=eng",
            "-shortest",
            str(path),
        ],
        check=True,
    )
    return path


def inference_stub(audio, settings, cache):
    return [
        Word(1 + i * 0.6, 1.5 + i * 0.6, word) for i, word in enumerate("Please open the front door".split())
    ], []


def alignment_stub(audio, passages, settings, cache):
    return [Cue(p.start, p.end, p.display) for p in passages], []


def queued(video, tmp_path):
    db = Database(tmp_path / "state" / "crowbarr.db")
    settings = Settings(roots=[str(video.parent)], settle_seconds=0, subtitle_wait_minutes=0)
    scan(settings, db)
    return settings, db, db.claim()


def test_automatic_fallback_publishes_separate_sidecar(video, tmp_path):
    settings, db, job = queued(video, tmp_path)
    result = process(job, settings, db.path.parent, db, inference_stub, alignment_stub)
    assert result["state"] == "completed"
    assert video.with_suffix(".crowbarr.en.srt").exists()
    assert json.loads(result["report"])["mode"] == "generated"


def test_wrong_cut_timing_repaired_without_changing_original(video, tmp_path):
    source = video.with_suffix(".en.srt")
    from test_audit import fixture

    cues, words = fixture(600)
    original = render_srt(cues)
    source.write_text(original)
    settings, db, job = queued(video, tmp_path)
    result = process(job, settings, db.path.parent, db, lambda *args: (words, []), alignment_stub)
    assert result["state"] == "completed"
    assert json.loads(result["report"])["audit"]["improved"]
    assert source.read_text() == original
    assert parse_srt(video.with_suffix(".crowbarr.en.srt").read_text())[0].start == 0.5


def test_low_quality_never_publishes(video, tmp_path):
    settings, db, job = queued(video, tmp_path)

    def failed_alignment(*args):
        return [], ["No confident alignment"]

    result = process(job, settings, db.path.parent, db, inference_stub, failed_alignment)
    assert result["state"] == "review"
    assert not video.with_suffix(".crowbarr.en.srt").exists()


def test_source_changes_during_processing_cannot_publish(video, tmp_path):
    settings, db, job = queued(video, tmp_path)

    def change_input(*args):
        video.with_suffix(".en.srt").write_text("new source arrived")
        return alignment_stub(*args)

    result = process(job, settings, db.path.parent, db, inference_stub, change_input)
    assert result["state"] == "superseded"
    assert not video.with_suffix(".crowbarr.en.srt").exists()


def test_ffmpeg_selects_track_and_preserves_offset(video, tmp_path):
    settings = Settings()
    metadata = probe(video)
    track = choose_audio(metadata, settings)
    offset, duration = extract_audio(video, tmp_path / "audio.wav", metadata, track)
    assert offset == 0
    assert duration == pytest.approx(8, abs=0.1)
    assert (tmp_path / "audio.wav").stat().st_size > 200000


def test_commentary_and_wrong_language_are_not_selected():
    with pytest.raises(ReviewRequired):
        choose_audio(
            {"streams": [{"codec_type": "audio", "tags": {"language": "eng", "title": "Commentary"}}]},
            Settings(),
        )
    with pytest.raises(ReviewRequired):
        choose_audio({"streams": [{"codec_type": "audio", "tags": {"language": "fra"}}]}, Settings())


def test_ambiguous_audio_is_reviewed():
    metadata = {"streams": [{"codec_type": "audio", "tags": {"language": "eng"}, "index": i} for i in (1, 2)]}
    with pytest.raises(ReviewRequired):
        choose_audio(metadata, Settings())
    metadata["streams"][1]["disposition"] = {"default": 1}
    assert choose_audio(metadata, Settings())["index"] == 2


def test_untracked_output_is_never_overwritten(video, tmp_path):
    output = video.with_suffix(".crowbarr.en.srt")
    output.write_text("user supplied content")
    settings, db, job = queued(video, tmp_path)
    with pytest.raises(ReviewRequired):
        process(job, settings, db.path.parent, db, inference_stub, alignment_stub)
    assert output.read_text() == "user supplied content"


def test_nonzero_audio_timeline_offset(video, tmp_path):
    delayed = video.with_name("delayed.mkv")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(video),
            "-itsoffset",
            "2",
            "-i",
            str(video),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c",
            "copy",
            str(delayed),
        ],
        check=True,
    )
    metadata = probe(delayed)
    offset, _ = extract_audio(delayed, tmp_path / "delayed.wav", metadata, choose_audio(metadata, Settings()))
    assert offset == pytest.approx(2, abs=0.01)


def test_publication_journal_recovers_write_before_completion(video, tmp_path):
    settings, db, job = queued(video, tmp_path)
    output = video.with_suffix(".crowbarr.en.srt")
    output.write_text("prior interrupted publication")
    db.register_artifact(str(video), str(output), hashlib.sha256(output.read_bytes()).hexdigest(), job["id"])
    result = process(job, settings, db.path.parent, db, inference_stub, alignment_stub)
    assert result["state"] == "completed"
    assert output.read_text() != "prior interrupted publication"


def test_passing_audit_skips_alignment_publication_and_repeat_work(video, tmp_path):
    from test_audit import fixture

    cues, words = fixture()
    source = video.with_suffix(".en.srt")
    source.write_text(render_srt(cues))
    settings, db, job = queued(video, tmp_path)

    def forbidden(*args):
        pytest.fail("A passing audit must not run forced alignment")

    result = process(job, settings, db.path.parent, db, lambda *args: (words, []), forbidden)
    assert result["state"] == "unchanged"
    assert not video.with_suffix(".crowbarr.en.srt").exists()
    db.update(job["id"], **result)
    scan(settings, db)
    assert db.claim() is None


def test_inconclusive_audit_skips_alignment(video, tmp_path):
    video.with_suffix(".en.srt").write_text("1\n00:00:01,000 --> 00:00:04,000\nPlease open the front door\n")
    settings, db, job = queued(video, tmp_path)

    def forbidden(*args):
        pytest.fail("Insufficient audit evidence must not trigger a repair")

    result = process(job, settings, db.path.parent, db, inference_stub, forbidden)
    assert result["state"] == "review"
    assert json.loads(result["report"])["audit"]["before"]["decision"] == "inconclusive"


def test_repair_that_does_not_improve_is_withheld(video, tmp_path):
    from test_audit import fixture

    cues, words = fixture(3)
    video.with_suffix(".en.srt").write_text(render_srt(cues))
    settings, db, job = queued(video, tmp_path)
    result = process(job, settings, db.path.parent, db, lambda *args: (words, []), lambda *args: (cues, []))
    assert result["state"] == "review"
    assert not json.loads(result["report"])["audit"]["improved"]
    assert not video.with_suffix(".crowbarr.en.srt").exists()


def test_passing_original_retires_only_owned_previous_output(video, tmp_path):
    from test_audit import fixture

    cues, words = fixture()
    video.with_suffix(".en.srt").write_text(render_srt(cues))
    settings, db, job = queued(video, tmp_path)
    output = video.with_suffix(".crowbarr.en.srt")
    output.write_text("older repair")
    db.register_artifact(str(video), str(output), hashlib.sha256(output.read_bytes()).hexdigest(), job["id"])
    result = process(job, settings, db.path.parent, db, lambda *args: (words, []), alignment_stub)
    assert result["state"] == "unchanged"
    assert json.loads(result["report"])["retired_output"]
    assert not output.exists()
    assert (db.path.parent / "backups" / f"audit-retired-{job['id']}.srt").read_text() == "older repair"


def test_asr_warning_cannot_pass_audit(video, tmp_path):
    from test_audit import fixture

    cues, words = fixture()
    video.with_suffix(".en.srt").write_text(render_srt(cues))
    settings, db, job = queued(video, tmp_path)
    result = process(
        job, settings, db.path.parent, db, lambda *args: (words, ["Uncertain speech"]), alignment_stub
    )
    assert result["state"] == "review"
    assert json.loads(result["report"])["audit"]["before"]["decision"] == "inconclusive"
