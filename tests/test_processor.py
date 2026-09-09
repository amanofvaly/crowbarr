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


def test_a_single_displaced_line_is_put_back(video, tmp_path):
    """One line four seconds out of place is not a shape a shift and a stretch can
    remove, and it used to be parked for that reason. Every timestamp now comes from the
    line's own words, so a fault affecting one cue is corrected like any other."""
    from test_audit import fixture

    cues, words = fixture()
    cues[-1].start += 4
    cues[-1].end += 4
    video.with_suffix(".en.srt").write_text(render_srt(cues))
    settings, db, job = queued(video, tmp_path)
    result = process(job, settings, db.path.parent, db, lambda *args: (words, []), alignment_stub)
    assert result["state"] == "completed"
    assert json.loads(result["report"])["audit"]["improved"]
    output = parse_srt(video.with_suffix(".crowbarr.en.srt").read_text())
    assert output[-1].start == pytest.approx(words[10].start, abs=0.05)


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
def test_a_single_overlapping_cue_does_not_veto_a_whole_repair(video, tmp_path):
    """Structural damage is judged by share in the audit and must be judged the same way
    here. One overlapping line cannot overturn a repair the anchors agree on."""
    from test_audit import fixture

    cues, words = fixture(3)
    cues.insert(1, Cue(cues[0].end - 0.5, cues[0].end - 0.1, "overlaps the line before it"))
    video.with_suffix(".en.srt").write_text(render_srt(cues))
    settings, db, job = queued(video, tmp_path)
    result = process(job, settings, db.path.parent, db, lambda *args: (words, []), alignment_stub)
    report = json.loads(result["report"])
    assert any("overlapping" in w for w in report["warnings"]), "the overlap must still be reported"
    assert result["state"] == "completed", f"held by: {report['issues']}"
def test_a_sample_may_settle_that_a_file_is_fine_but_not_a_change_to_one():
    """Three two-minute windows see about a quarter of an episode. That is enough to
    leave a file alone, and not enough to rewrite every cue in it and then check the
    rewrite against the same quarter."""
    from crowbarr.processor import _needs_full_audio

    def sources(*decisions):
        return [{"audit": {"decision": decision}} for decision in decisions]

    assert not _needs_full_audio(sources("pass"))
    assert not _needs_full_audio(sources("repair", "pass"))
    assert _needs_full_audio(sources("repair")), "a repair rewrites the whole file"
    assert _needs_full_audio(sources("inconclusive"))
    assert _needs_full_audio(sources("repair", "inconclusive"))


def _transcript_fixture(count=30, wrong=lambda t: 0.0):
    """Words at known times, and authored cues whose timestamps are wrong by `wrong`."""
    from crowbarr.subtitles import Cue, Word

    words, cues, clock = [], [], 10.0
    for index in range(count):
        vocabulary = [f"word{index}{letter}" for letter in "abcdef"]
        spoken_start = clock
        for term in vocabulary:
            words.append(Word(clock, clock + 0.3, term, 0.95))
            clock += 0.4
        cues.append(
            Cue(spoken_start + wrong(spoken_start), clock - 0.1 + wrong(spoken_start), " ".join(vocabulary))
        )
        clock += 2.0
    return cues, words


def _placed(cues, words):
    from crowbarr.processor import _retime_from_transcript
    from crowbarr.subtitles import match_passages

    passages, _ = match_passages(cues, words)
    return _retime_from_transcript(cues, passages)


def test_every_cue_is_placed_where_its_own_words_were_spoken():
    """No model of the old timing is fitted, so its shape does not have to be recognised."""
    import random
    from statistics import median

    shapes = {
        "constant offset": lambda t: 3.0,
        "drift": lambda t: t * 0.02,
        "one step": lambda t: 0.0 if t < 100 else 4.0,
        "three steps": lambda t: (0.0 if t < 80 else 2.5 if t < 160 else -1.5 if t < 240 else 6.0),
        "no pattern at all": lambda t: random.Random(int(t)).uniform(-8, 8),
    }
    for name, wrong in shapes.items():
        cues, words = _transcript_fixture(wrong=wrong)
        truth = {index: word.start for index, word in enumerate(words[::6])}
        aligned, model = _placed(cues, words)
        assert aligned, name
        error = median(abs(cue.start - truth[index]) for index, cue in enumerate(aligned))
        assert error < 0.05, f"{name}: cues landed {error:.3f} s from their speech"
        assert model["kind"] == "transcript_placement", name


def test_a_cue_with_nothing_to_match_keeps_its_place_between_its_neighbours():
    """A sound caption has no speech of its own, so its neighbours decide where it goes."""
    from crowbarr.subtitles import Cue

    cues, words = _transcript_fixture(wrong=lambda t: 5.0)
    caption = Cue(cues[3].end + 0.4, cues[3].end + 1.0, "[a door closes]")
    cues.insert(4, caption)
    aligned, model = _placed(cues, words)
    assert model["interpolated_cues"] == 1
    assert aligned[3].end <= aligned[4].start <= aligned[5].start
    assert aligned[4].text == "[a door closes]"


def test_authored_reading_time_survives_being_retimed():
    """Cue length is an authoring decision. Placement moves a line; it does not reshape it."""
    cues, words = _transcript_fixture(wrong=lambda t: -2.0)
    aligned, _ = _placed(cues, words)
    for before, after in zip(cues, aligned, strict=True):
        assert after.end - after.start == pytest.approx(before.end - before.start)


def test_a_retimed_subtitle_is_judged_on_its_own_result_not_on_the_old_timing():
    """Placement does not correct the old timestamps, so "did it improve on them" is not
    a question about the new file. What matters is whether the new file is right, and
    whether enough of it was placed rather than interpolated."""
    from crowbarr.processor import placement_verdict

    good = {"decision": "pass"}
    assert placement_verdict(good, {"placed_cues": 90, "interpolated_cues": 10})["accepted"]
    assert placement_verdict({"decision": "inconclusive"}, {"placed_cues": 90, "interpolated_cues": 10})[
        "accepted"
    ], "a file the audit cannot grade is not a file that was placed wrongly"
    assert not placement_verdict({"decision": "repair"}, {"placed_cues": 90, "interpolated_cues": 10})[
        "accepted"
    ], "a result that fails its own audit is not published"
    thin = placement_verdict(good, {"placed_cues": 5, "interpolated_cues": 95})
    assert not thin["accepted"], "a file that is mostly guesswork is not published"
    assert "5" in thin["reason"]


def test_an_unjudgeable_subtitle_is_still_retimed_when_its_lines_match(video, tmp_path):
    """`inconclusive` says the audit could not assemble enough confident, spread-out
    anchors to judge the timing. It does not say the lines cannot be found in the audio.
    A file with the whole transcript in hand and most of its cues matched used to be
    parked on that distinction."""
    from test_audit import fixture

    cues, words = fixture()
    # One weak word at the end of the last cue: enough to drop that anchor from the
    # audit's evidence and leave the runtime unevenly covered, not enough to stop the
    # line being found in the transcript.
    words[-1].probability = 0.3
    for cue in cues:
        cue.start += 2.5
        cue.end += 2.5
    video.with_suffix(".en.srt").write_text(render_srt(cues))
    settings, db, job = queued(video, tmp_path)
    result = process(job, settings, db.path.parent, db, lambda *args: (words, []), alignment_stub)
    report = json.loads(result["report"])
    assert report["audit"]["before"]["decision"] == "inconclusive"
    assert result["state"] == "completed", f"held by: {report['issues']}"
    output = parse_srt(video.with_suffix(".crowbarr.en.srt").read_text())
    assert output[0].start == pytest.approx(words[0].start, abs=0.05)
