import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from crowbarr.config import Settings
from crowbarr.db import Database
from crowbarr.library import scan
from crowbarr.media import (
    ReviewRequired,
    audio_candidates,
    choose_audio,
    describe_audio,
    embedded_subtitles,
    extract_audio,
    probe,
)
from crowbarr.processor import _recognized_words, process
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


def test_transcript_cache_is_independent_of_subtitle_revision(video, tmp_path):
    calls = 0

    def transcriber(*args):
        nonlocal calls
        calls += 1
        return [Word(1, 2, "hello", 0.9)], ["warning"]

    settings = Settings(roots=[str(video.parent)])
    audio = tmp_path / "audio.wav"
    first = _recognized_words(video, audio, settings, tmp_path / "state", tmp_path / "models", transcriber)
    video.with_suffix(".en.srt").write_text("a provider update must not invalidate audio recognition")
    second = _recognized_words(video, audio, settings, tmp_path / "state", tmp_path / "models", transcriber)
    assert calls == 1
    assert not first[2]
    assert second[2]
    assert second[:2] == first[:2]


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


def test_generated_subtitle_falls_back_to_whisper_timestamps(video, tmp_path):
    settings, db, job = queued(video, tmp_path)

    def failed_alignment(*args):
        return [], ["No confident alignment"]

    result = process(job, settings, db.path.parent, db, inference_stub, failed_alignment)
    assert result["state"] == "completed"
    assert video.with_suffix(".crowbarr.en.srt").exists()
    assert "kept Whisper timestamps" in json.loads(result["report"])["warnings"][-1]


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


def test_forced_subtitle_track_is_skipped_even_without_the_disposition_flag(tmp_path):
    """Real file: a forced track with disposition forced=0 and the title set to "Forced"
    was extracted and audited, scoring 8% text match against 74 signage cues."""
    metadata = {
        "streams": [
            {
                "index": 2,
                "codec_type": "subtitle",
                "codec_name": "subrip",
                "disposition": {"forced": 0},
                "tags": {"language": "eng", "title": "Forced"},
            }
        ]
    }
    assert embedded_subtitles(Path("/nonexistent.mkv"), tmp_path, metadata, Settings()) == []


def test_commentary_is_never_selected():
    with pytest.raises(ReviewRequired):
        choose_audio(
            {"streams": [{"codec_type": "audio", "tags": {"language": "eng", "title": "Commentary"}}]},
            Settings(),
        )


def test_missing_audio_reports_what_the_file_holds():
    with pytest.raises(ReviewRequired, match="no audio track"):
        choose_audio({"streams": [{"codec_type": "video", "index": 0}]}, Settings())


def test_wrongly_tagged_track_is_used_and_verified_by_recognition():
    """Container tags are often wrong. Recognition establishes the language, not the tag."""
    metadata = {"streams": [{"codec_type": "audio", "index": 1, "tags": {"language": "fin"}}]}
    assert choose_audio(metadata, Settings())["index"] == 1
    assert "tagged fin" in describe_audio(metadata)


def test_english_tag_outranks_an_untagged_and_a_foreign_track():
    metadata = {
        "streams": [
            {"codec_type": "audio", "index": 1, "tags": {"language": "fra"}},
            {"codec_type": "audio", "index": 2},
            {"codec_type": "audio", "index": 3, "tags": {"language": "eng"}},
        ]
    }
    assert choose_audio(metadata, Settings())["index"] == 3
    assert [c["index"] for c in audio_candidates(metadata)] == [3, 2, 1]


def test_interchangeable_english_tracks_pick_the_fullest_mix():
    """Stereo and 5.1 of the same dialogue is not ambiguity worth refusing over."""
    metadata = {
        "streams": [
            {"codec_type": "audio", "index": 1, "channels": 2, "tags": {"language": "eng"}},
            {"codec_type": "audio", "index": 2, "channels": 6, "tags": {"language": "eng"}},
        ]
    }
    assert choose_audio(metadata, Settings())["index"] == 2
    metadata["streams"][0]["disposition"] = {"default": 1}
    assert choose_audio(metadata, Settings())["index"] == 1


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

    cues, words = fixture()
    cues[-1].start += 4
    cues[-1].end += 4
    video.with_suffix(".en.srt").write_text(render_srt(cues))
    settings, db, job = queued(video, tmp_path)
    result = process(job, settings, db.path.parent, db, lambda *args: (words, []), alignment_stub)
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


def test_local_asr_warning_does_not_veto_a_supported_audit(video, tmp_path):
    from test_audit import fixture

    cues, words = fixture()
    video.with_suffix(".en.srt").write_text(render_srt(cues))
    settings, db, job = queued(video, tmp_path)
    result = process(
        job, settings, db.path.parent, db, lambda *args: (words, ["Uncertain speech"]), alignment_stub
    )
    assert result["state"] == "unchanged"
    report = json.loads(result["report"])
    assert report["audit"]["before"]["decision"] == "pass"
    assert report["warnings"] == ["Uncertain speech"]


def test_partial_authored_repair_preserves_all_original_text(video, tmp_path):
    source = video.with_suffix(".en.srt")
    from test_audit import fixture

    cues, words = fixture(3)
    cues.insert(1, Cue(5.05, 5.4, "[door closes]"))
    source.write_text(render_srt(cues))
    settings, db, job = queued(video, tmp_path)
    result = process(job, settings, db.path.parent, db, lambda *args: (words, []), alignment_stub)
    assert result["state"] == "completed"
    output = parse_srt(video.with_suffix(".crowbarr.en.srt").read_text())
    assert [cue.text for cue in output] == [cue.text for cue in cues]
    assert len(output) == len(cues)


def test_embedded_source_beats_bad_external_sidecar_by_audio_evidence(video, tmp_path):
    from test_audit import fixture

    embedded_cues, words = fixture()
    subtitle = tmp_path / "embedded.srt"
    subtitle.write_text(render_srt(embedded_cues))
    media = tmp_path / "embedded-library" / "with-embedded.mkv"
    media.parent.mkdir()
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(video),
            "-i",
            str(subtitle),
            "-map",
            "0",
            "-map",
            "1",
            "-c",
            "copy",
            "-metadata:s:s:0",
            "language=eng",
            str(media),
        ],
        check=True,
    )
    bad_external, _ = fixture(3)
    media.with_suffix(".en.srt").write_text(render_srt(bad_external))
    settings, db, job = queued(media, tmp_path)

    def forbidden(*args):
        pytest.fail("The passing embedded source should not be repaired")

    result = process(job, settings, db.path.parent, db, lambda *args: (words, []), forbidden)
    report = json.loads(result["report"])
    assert result["state"] == "completed"
    assert Path(result["output"]).read_text() == render_srt(embedded_cues)
    assert media.with_suffix(".en.srt").read_text() == render_srt(bad_external)
    assert report["selected_source_kind"] == "embedded"
    assert {item["decision"] for item in report["source_arbitration"]} == {"pass", "repair"}


def test_failed_output_is_saved_as_private_candidate(video, tmp_path):
    settings, db, job = queued(video, tmp_path)

    def invalid_alignment(*args):
        return [Cue(-1, 2, "bad")], ["Used fallback"]

    result = process(job, settings, db.path.parent, db, inference_stub, invalid_alignment)
    assert result["state"] == "review"
    report = json.loads(result["report"])
    assert Path(report["candidate"]).read_text().endswith("bad\n")
    assert not video.with_suffix(".crowbarr.en.srt").exists()


def test_a_provider_download_that_bazarr_discards_retries_instead_of_parking(video, tmp_path, monkeypatch):
    """Bazarr validates downloads itself and may write nothing; the episode must not stall."""
    from crowbarr import processor

    attempts = []

    def fake_try_alternative(settings, db, media, directory, job_id):
        attempts.append(job_id)
        return {"state": "downloaded", "provider": "opensubtitlescom", "attempts": len(attempts)}

    monkeypatch.setattr("crowbarr.bazarr.try_alternative", fake_try_alternative)
    settings = Settings(
        roots=[str(video.parent)],
        bazarr={"url": "http://bazarr", "api_key": "k"},
        sonarr={"url": "http://sonarr", "api_key": "k"},
        bazarr_download_alternatives=True,
    )
    db = Database(tmp_path / "queue.db")
    db.replace_catalog("sonarr", [{"file_id": 1, "item_id": 1, "path": str(video), "remote_path": str(video), "title": "t"}])
    from crowbarr.library import signature

    db.enqueue(str(video), signature(video, None, settings), None, 0)
    result = processor.process(db.claim(), settings, tmp_path, db)
    assert result["state"] == "retry"
    assert "next candidate" in result["error"]


def test_a_few_bad_anchors_do_not_veto_an_otherwise_excellent_fit():
    """At small anchor counts the 95th percentile is just the second-worst point."""
    from crowbarr.processor import _retime_authored
    from crowbarr.subtitles import Cue

    # 29 anchors: 27 sitting almost exactly on a 0.64 s offset, 2 badly mismatched.
    evidence = []
    for index in range(27):
        start = 10 + index * 40
        evidence.append({"cue": index + 1, "subtitle_start": start, "subtitle_end": start + 2,
                         "audio_start": start - 0.64, "audio_end": start + 1.36,
                         "start_delta_seconds": 0.64, "end_delta_seconds": 0.64,
                         "error_seconds": 0.64, "within_tolerance": False})
    for index, start in enumerate((450, 890)):
        evidence.append({"cue": 100 + index, "subtitle_start": start, "subtitle_end": start + 2,
                         "audio_start": start - 4.2, "audio_end": start - 2.2,
                         "start_delta_seconds": 4.2, "end_delta_seconds": 4.2,
                         "error_seconds": 4.2, "within_tolerance": False})
    original = [Cue(item["subtitle_start"], item["subtitle_end"], f"line {n}")
                for n, item in enumerate(evidence)]
    aligned, model = _retime_authored(original, {"evidence": evidence})
    assert model["median_residual_seconds"] < 0.5
    assert model["p95_residual_seconds"] > 3.0
    assert aligned, "two outliers must not discard a fit the other 27 anchors agree on"


def test_a_fit_the_anchors_broadly_disagree_with_is_still_refused():
    from crowbarr.processor import _retime_authored
    from crowbarr.subtitles import Cue

    evidence = []
    for index in range(20):
        start = 10 + index * 40
        drift = 3.0 if index % 2 else -3.0   # no consistent offset exists
        evidence.append({"cue": index + 1, "subtitle_start": start, "subtitle_end": start + 2,
                         "audio_start": start + drift, "audio_end": start + drift + 2,
                         "start_delta_seconds": -drift, "end_delta_seconds": -drift,
                         "error_seconds": abs(drift), "within_tolerance": False})
    original = [Cue(item["subtitle_start"], item["subtitle_end"], f"line {n}")
                for n, item in enumerate(evidence)]
    aligned, model = _retime_authored(original, {"evidence": evidence})
    assert not aligned
    assert "do not support" in model["reason"]


def test_an_english_image_subtitle_is_reported_rather_than_ignored():
    """A remux can carry a good English subtitle as bitmaps; the job must say so."""
    from crowbarr.media import unreadable_subtitles

    metadata = {"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 2, "codec_type": "subtitle", "codec_name": "dvd_subtitle", "tags": {"language": "eng"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "dvd_subtitle", "tags": {"language": "fre"}},
        {"index": 4, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
    ]}
    reported = unreadable_subtitles(metadata)
    assert reported == ["embedded stream 2 (dvd_subtitle)"]


@pytest.fixture
def long_video(tmp_path):
    """Long enough to hold a plausible amount of dialogue.

    Proving a mismatch needs a substantial transcript, and a substantial transcript
    inside eight seconds would be hundreds of words a second -- a shape no real file
    has, and one that fails the reading-duration checks for reasons of its own.
    """
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is required for media integration tests")
    path = tmp_path / "library" / "feature.mkv"
    path.parent.mkdir()
    subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-f", "lavfi", "-i", "color=c=black:s=160x90:d=180",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=180",
            "-c:v", "mpeg4", "-c:a", "pcm_s16le",
            "-metadata:s:a:0", "language=eng", "-shortest", str(path),
        ],
        check=True,
    )
    return path


def wrong_episode(video):
    """A sidecar whose text appears nowhere in the recognized dialogue."""
    from test_audit import unrelated

    cues, words, _ = unrelated(word_count=250, duration=180.0, cue_count=30)
    video.with_suffix(".en.srt").write_text(render_srt(cues))
    return lambda *args: (words, [])


def test_a_subtitle_for_other_content_is_replaced_rather_than_parked(long_video, tmp_path):
    """The transcript that proved the subtitle wrong is the transcript a fresh one needs."""
    transcriber = wrong_episode(long_video)
    settings, db, job = queued(long_video, tmp_path)
    result = process(job, settings, db.path.parent, db, transcriber, alignment_stub)
    report = json.loads(result["report"])
    assert report["audit"]["before"]["decision"] == "mismatched"
    assert result["state"] == "completed"
    assert report["mode"] == "generated"
    assert report["replaced_source"]
    output = long_video.with_suffix(".crowbarr.en.srt")
    assert output.exists()
    assert "alpha0" in output.read_text()


def test_the_rejected_subtitle_is_left_on_disk_untouched(long_video, tmp_path):
    transcriber = wrong_episode(long_video)
    original = long_video.with_suffix(".en.srt").read_text()
    settings, db, job = queued(long_video, tmp_path)
    process(job, settings, db.path.parent, db, transcriber, alignment_stub)
    assert long_video.with_suffix(".en.srt").read_text() == original


def test_generation_over_a_mismatch_can_be_refused(long_video, tmp_path):
    transcriber = wrong_episode(long_video)
    settings, db, job = queued(long_video, tmp_path)
    settings = settings.model_copy(update={"generate_over_mismatch": False})
    result = process(job, settings, db.path.parent, db, transcriber, alignment_stub)
    assert result["state"] == "review"
    assert json.loads(result["report"])["audit"]["before"]["decision"] == "mismatched"


def test_an_inconclusive_audit_offers_no_candidate_to_publish(video, tmp_path):
    """The candidate written for an inconclusive verdict was the original subtitle, so
    approving it published exactly what the audit had refused to endorse."""
    video.with_suffix(".en.srt").write_text("1\n00:00:01,000 --> 00:00:04,000\nPlease open the front door\n")
    settings, db, job = queued(video, tmp_path)
    result = process(job, settings, db.path.parent, db, inference_stub, alignment_stub)
    assert result["state"] == "review"
    assert "candidate" not in json.loads(result["report"])


def test_a_subtitle_for_a_shorter_cut_is_replaced_rather_than_parked(long_video, tmp_path):
    """The recording holds scenes the subtitle never covers; a shift cannot add them."""
    from test_audit import cut_fixture

    cues, words, _ = cut_fixture(cue_count=40, duration=180.0, missing=((40, 15), (70, 20), (100, 25)))
    long_video.with_suffix(".en.srt").write_text(render_srt(cues))
    settings, db, job = queued(long_video, tmp_path)
    result = process(job, settings, db.path.parent, db, lambda *a: (words, []), alignment_stub)
    report = json.loads(result["report"])
    assert report["audit"]["before"]["decision"] == "different_cut"
    assert result["state"] == "completed"
    assert report["mode"] == "generated" and report["replaced_source"]


def test_an_unreadable_subtitle_says_what_was_wrong_with_it(video, tmp_path):
    """This refusal ends the job before a report exists, so if the parser's objection is
    not in the message it is nowhere, and the operator is told only that it failed."""
    video.with_suffix(".en.srt").write_text("1\n00:00:01,000 --> 00:00:04,000 nonsense\nLine\n")
    settings, db, job = queued(video, tmp_path)
    with pytest.raises(ReviewRequired) as refusal:
        process(job, settings, db.path.parent, db, inference_stub, alignment_stub)
    assert "Invalid SRT timestamp" in str(refusal.value)
    assert "sample.en.srt" in str(refusal.value)


def test_a_missing_alignment_package_does_not_fail_the_job(video, tmp_path):
    """The CUDA image ships recognition without WhisperX. A setting that is on in a build
    which cannot honour it must fall back to Whisper timestamps and say so."""
    def absent(*args):
        raise ImportError("No module named 'whisperx'")

    settings, db, job = queued(video, tmp_path)
    settings = settings.model_copy(update={"refine_generated": True})
    result = process(job, settings, db.path.parent, db, inference_stub, absent)
    assert result["state"] == "completed"
    report = json.loads(result["report"])
    assert any("whisperx" in w.lower() for w in report["warnings"])
    assert video.with_suffix(".crowbarr.en.srt").exists()
