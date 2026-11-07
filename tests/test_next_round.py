"""New files first, embedded-subtitle extraction on large files, and sample windows past
the end of the audio."""

import os
import time
import wave
from pathlib import Path

import pytest

from crowbarr import inference, library, media
from crowbarr.config import Settings
from crowbarr.db import Database
from crowbarr.subtitles import Word

# ----- queue order --------------------------------------------------------------------


def age(db, identifier, hours):
    with db.connect() as connection:
        connection.execute("UPDATE jobs SET created=? WHERE id=?", (time.time() - hours * 3600, identifier))


def test_manual_and_import_jobs_run_before_a_capped_backlog(tmp_path):
    db = Database(tmp_path / "crowbarr.db")
    old = db.enqueue("/old.mkv", "a", None, 0)
    age(db, old, 500)  # far past the 120-point ageing cap
    imported = db.enqueue("/new.mkv", "b", None, 0, origin="import")
    manual = db.enqueue("/asked.mkv", "c", None, 0, origin="manual")
    order = []
    for _ in range(3):
        job = db.claim()
        order.append(job["id"])
        db.update(job["id"], state="completed")
    assert order == [manual, imported, old]


def test_ageing_still_lets_long_waiting_backlog_overtake_a_fresh_retry(tmp_path):
    db = Database(tmp_path / "crowbarr.db")
    old = db.enqueue("/old.mkv", "a", None, 0)
    age(db, old, 121)
    db.enqueue("/retry.mkv", "b", None, 0, origin="retry")
    db.enqueue("/bazarr.mkv", "c", None, 0, origin="bazarr")
    assert db.claim()["id"] == old


def test_dashboard_next_up_matches_the_claim_order(tmp_path):
    db = Database(tmp_path / "crowbarr.db")
    old = db.enqueue("/old.mkv", "a", None, 0)
    age(db, old, 500)
    imported = db.enqueue("/new.mkv", "b", None, 0, origin="import")
    pending = [job for job in db.snapshot()["jobs"] if job["state"] in ("queued", "waiting", "retry")]
    assert pending[0]["id"] == imported
    assert db.claim()["id"] == imported


def test_queue_page_lists_imports_first(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from crowbarr.app import create_app

    monkeypatch.setattr(
        "crowbarr.capabilities.runtime_capabilities",
        lambda block=True: {k: {"available": False, "reason": "", "compute_types": []} for k in ("cpu", "cuda", "refinement")},
    )
    with TestClient(create_app(tmp_path, background=False)) as client:
        db = client.app.state.db
        old = db.enqueue("/media/old.mkv", "a", None, 0)
        age(db, old, 500)
        imported = db.enqueue("/media/new.mkv", "b", None, 0, origin="import")
        rows = client.get("/api/jobs?state=queue", headers={"X-Api-Key": client.app.state.store.token}).json()
        ids = [row["id"] for row in rows["results"]]
        assert ids.index(imported) < ids.index(old)


# ----- new files ----------------------------------------------------------------------


def test_a_fresh_file_never_seen_before_is_queued_as_an_import(tmp_path):
    db = Database(tmp_path / "crowbarr.db")
    identifier = db.enqueue("/new.mkv", "a", None, 0, fresh=True)
    job = db.get(identifier)
    assert (job["origin"], job["priority"]) == ("import", 80)


def test_fresh_does_not_promote_a_file_already_in_the_queue(tmp_path):
    db = Database(tmp_path / "crowbarr.db")
    identifier = db.enqueue("/known.mkv", "a", None, 0)
    db.enqueue("/known.mkv", "a", None, 0, fresh=True)
    assert db.get(identifier)["origin"] == "backlog"
    db.enqueue("/known.mkv", "b", None, 0, fresh=True)  # changed file, still not new
    assert db.get(identifier)["origin"] == "backlog"


def test_fresh_never_lowers_a_manual_request(tmp_path):
    db = Database(tmp_path / "crowbarr.db")
    identifier = db.enqueue("/asked.mkv", "a", None, 0, origin="manual", fresh=True)
    assert db.get(identifier)["origin"] == "manual"


def scan_one(tmp_path, written_days_ago, **settings):
    folder = tmp_path / "library"
    folder.mkdir()
    video = folder / "episode.mkv"
    video.write_bytes(b"video")
    stamp = time.time() - written_days_ago * 86400
    os.utime(video, (stamp, stamp))
    db = Database(tmp_path / "state" / "crowbarr.db")
    config = Settings(roots=[str(folder)], settle_seconds=0, subtitle_wait_minutes=0, **settings)
    library.scan(config, db)
    with db.connect() as connection:
        return connection.execute("SELECT origin FROM jobs").fetchone()[0]


def test_scan_queues_a_new_download_ahead_of_the_backlog(tmp_path):
    assert scan_one(tmp_path, written_days_ago=0.1) == "import"


def test_scan_leaves_old_files_in_a_new_folder_in_the_backlog(tmp_path):
    assert scan_one(tmp_path, written_days_ago=library.NEW_FILE_DAYS + 1) == "backlog"


def test_new_files_first_can_be_turned_off(tmp_path):
    assert scan_one(tmp_path, written_days_ago=0.1, prioritize_new_files=False) == "backlog"


def test_new_files_first_defaults_on_and_is_not_a_fingerprint_field():
    from crowbarr.config import FINGERPRINT_FIELDS

    assert Settings().prioritize_new_files is True
    assert "prioritize_new_files" not in FINGERPRINT_FIELDS


# ----- embedded subtitle extraction -----------------------------------------------------


def test_extraction_timeout_scales_with_file_size(tmp_path):
    small = tmp_path / "small.mkv"
    small.write_bytes(b"x")
    large = tmp_path / "large.mkv"
    with large.open("wb") as handle:
        handle.truncate(80 * 1024**3)  # sparse; an 80 GB 2160p remux
    huge = tmp_path / "huge.mkv"
    with huge.open("wb") as handle:
        handle.truncate(400 * 1024**3)
    assert media.extraction_timeout(small) == 300
    assert media.extraction_timeout(large) == pytest.approx(80 * 1024 / 40)
    assert media.extraction_timeout(huge) == 3600
    assert media.extraction_timeout(tmp_path / "missing.mkv") == 300


def test_every_english_text_track_is_extracted_in_one_pass(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(media.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"x")
    metadata = {
        "streams": [
            {"index": 0, "codec_type": "video"},
            {"index": 3, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
            {"index": 4, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng", "title": "SDH"}},
            {"index": 5, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng", "title": "Forced"}},
            {"index": 6, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "fre"}},
        ]
    }
    paths = media.embedded_subtitles(source, tmp_path, metadata, Settings())
    assert [p.name for p in paths] == ["embedded-3.srt", "embedded-4.srt"]
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command.count("-i") == 1
    assert command.count("-map") == 2 and "0:3" in command and "0:4" in command
    assert kwargs["timeout"] == 300 and kwargs["check"] is True


def test_no_ffmpeg_call_without_a_usable_track(tmp_path, monkeypatch):
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **k: pytest.fail("ffmpeg must not run"))
    metadata = {"streams": [{"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "ger"}}]}
    assert media.embedded_subtitles(tmp_path / "movie.mkv", tmp_path, metadata, Settings()) == []


# ----- sample windows past the end of the audio ---------------------------------------


def write_audio(path: Path, seconds: int) -> None:
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\0\0" * 16000 * seconds)


def test_sample_windows_beyond_the_audio_are_skipped_not_fatal(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    write_audio(audio, 10)
    clips = []

    def recognize(clip, *args):
        with wave.open(str(clip), "rb") as handle:
            clips.append(handle.getnframes() / handle.getframerate())
        return [Word(0, 1, "hello", 0.9)], []

    recognize.progress = None
    monkeypatch.setattr(inference, "transcribe", recognize)
    words, issues = inference.transcribe_windows(audio, [(0, 2), (8, 14), (30, 40)], Settings(), tmp_path)
    # The second window is trimmed to the audio; the third starts after it ends.
    assert clips == [2, 2]
    assert [w.start for w in words] == [0, 8]
    assert len(issues) == 1 and "past the end of the audio" in issues[0] and "30s" in issues[0]


def test_all_windows_past_the_end_returns_no_words(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    write_audio(audio, 5)
    monkeypatch.setattr(inference, "transcribe", lambda *a: pytest.fail("nothing to transcribe"))
    words, issues = inference.transcribe_windows(audio, [(100, 160)], Settings(), tmp_path)
    assert words == [] and len(issues) == 1
