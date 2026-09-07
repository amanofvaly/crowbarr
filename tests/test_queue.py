import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

from crowbarr.config import Settings
from crowbarr.db import Database
from crowbarr.library import scan, source_subtitle


def test_duplicate_jobs_converge_and_claim_once(tmp_path):
    db = Database(tmp_path / "state" / "queue.db")
    first = db.enqueue("movie.mkv", "revision", None, 0)
    assert db.enqueue("movie.mkv", "revision", None, 0) == first
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: db.claim(), range(4)))
    assert sum(result is not None for result in results) == 1


def test_future_job_waits_and_restart_recovers(tmp_path):
    db = Database(tmp_path / "queue.db")
    job = db.enqueue("movie.mkv", "one", None, time.time() + 999)
    assert db.claim() is None
    db.update(job, ready=0)
    assert db.claim()["id"] == job
    Database(db.path).recover()
    assert db.get(job)["state"] == "retry"
    assert db.claim()["attempts"] == 2


def test_subtitle_upgrade_supersedes_waiting_fallback(tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"video")
    db = Database(tmp_path / "state" / "queue.db")
    settings = Settings(roots=[str(tmp_path)], settle_seconds=0)
    scan(settings, db)
    assert db.snapshot()["jobs"][0]["state"] == "waiting"
    media.with_suffix(".en.srt").write_text("authored")
    scan(settings, db)
    jobs = db.snapshot()["jobs"]
    assert len(jobs) == 2
    assert sorted(job["state"] for job in jobs) == ["queued", "superseded"]
    scan(settings, db)
    assert len(db.snapshot()["jobs"]) == 2


def test_own_output_and_forced_subtitles_are_not_sources(tmp_path):
    media = tmp_path / "movie.mkv"
    media.touch()
    media.with_suffix(".crowbarr.en.srt").write_text("generated")
    media.with_suffix(".en.forced.srt").write_text("forced")
    settings = Settings(roots=[str(tmp_path)])
    assert source_subtitle(media, settings) is None
    authored = media.with_suffix(".eng.srt")
    authored.write_text("authored")
    assert source_subtitle(media, settings) == authored


def test_symlink_escape_is_not_discovered(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    (tmp_path / "outside.mkv").write_text("outside")
    (root / "escape.mkv").symlink_to(tmp_path / "outside.mkv")
    db = Database(tmp_path / "state" / "queue.db")
    assert scan(Settings(roots=[str(root)]), db) == 0


def test_video_replacement_retires_only_verified_own_output(tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"old-video")
    db = Database(tmp_path / "state" / "queue.db")
    settings = Settings(roots=[str(tmp_path)], settle_seconds=0, subtitle_wait_minutes=0)
    scan(settings, db)
    job = db.claim()
    output = media.with_suffix(".crowbarr.en.srt")
    output.write_text("old subtitle")
    db.update(
        job["id"],
        state="completed",
        output=str(output),
        report=json.dumps({"output_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}),
    )
    media.write_bytes(b"new-video-with-another-cut")
    scan(settings, db)
    assert not output.exists()
    assert (db.path.parent / "backups" / f"retired-{job['id']}.srt").read_text() == "old subtitle"
    assert db.get(job["id"])["state"] == "superseded"


def test_externally_edited_output_is_not_deleted(tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"video")
    db = Database(tmp_path / "state" / "queue.db")
    settings = Settings(roots=[str(tmp_path)], settle_seconds=0, subtitle_wait_minutes=0)
    scan(settings, db)
    job = db.claim()
    output = media.with_suffix(".crowbarr.en.srt")
    output.write_text("user changed this")
    db.update(job["id"], state="completed", output=str(output), report='{"output_sha256":"different"}')
    media.write_bytes(b"new video")
    scan(settings, db)
    assert output.exists()
    assert db.snapshot()["notices"]


def test_only_attention_jobs_can_retry(tmp_path):
    db = Database(tmp_path / "queue.db")
    job = db.enqueue("movie", "v1", None, 0)
    assert not db.retry(job)
    db.update(job, state="review")
    assert db.retry(job)
    assert db.get(job)["attempts"] == 0


def test_missing_published_subtitle_is_regenerated(tmp_path):
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"video")
    db = Database(tmp_path / "state" / "queue.db")
    settings = Settings(roots=[str(tmp_path)], settle_seconds=0, subtitle_wait_minutes=0)
    scan(settings, db)
    job = db.claim()
    db.update(job["id"], state="completed", output=str(media.with_suffix(".crowbarr.en.srt")))
    scan(settings, db)
    assert db.get(job["id"])["state"] == "queued"


def test_returning_revision_can_be_requeued(tmp_path):
    db = Database(tmp_path / "queue.db")
    job = db.enqueue("movie", "v1", None, 0)
    db.update(job, state="superseded")
    assert db.enqueue("movie", "v1", None, 0) == job
    assert db.get(job)["state"] == "queued"
