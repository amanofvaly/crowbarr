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


def test_mixed_priority_queue_and_single_claim(tmp_path):
    import time

    from crowbarr.db import Database

    db = Database(tmp_path / "queue.db")
    identifiers = {}
    for origin in ["backlog", "retry", "bazarr", "import", "manual"]:
        identifiers[origin] = db.enqueue("/" + origin, origin, None, time.time() - 1, origin=origin)
    for origin in ["manual", "import", "bazarr", "retry", "backlog"]:
        job = db.claim()
        assert job["id"] == identifiers[origin]
        assert db.claim() is None
        db.update(job["id"], state="completed")


def test_resource_deferral_leaves_background_pending(tmp_path):
    import time

    from crowbarr.db import Database

    db = Database(tmp_path / "queue.db")
    background = db.enqueue("/background", "a", None, time.time() - 1)
    assert db.claim(background_allowed=False) is None
    assert db.promote(background)
    assert db.claim(background_allowed=False)["id"] == background
    assert db.cancel(background)
    assert db.get(background)["cancel_requested"] == 1


def test_aged_backlog_eventually_runs(tmp_path):
    import time

    from crowbarr.db import Database

    db = Database(tmp_path / "queue.db")
    old = db.enqueue("/old", "a", None, 0)
    with db.connect() as connection:
        connection.execute("UPDATE jobs SET created=? WHERE id=?", (time.time() - 121 * 3600, old))
    db.enqueue("/manual", "b", None, 0, origin="manual")
    assert db.claim()["id"] == old


def test_snapshot_shows_results_behind_a_large_backlog(tmp_path):
    db = Database(tmp_path / "queue.db")
    finished = db.enqueue("done.mkv", "sig", None, 0)
    db.update(finished, state="completed", stage="Ready to watch")
    for index in range(200):
        db.enqueue(f"pending{index}.mkv", "sig", None, 0)
    jobs = db.snapshot()["jobs"]
    # The one outcome must remain visible rather than being buried by the backlog.
    assert finished in [job["id"] for job in jobs]
    assert len(jobs) < 200
    assert db.snapshot()["counts"]["queued"] == 200


def test_superseded_bookkeeping_is_pruned(tmp_path):
    db = Database(tmp_path / "queue.db")
    kept = []
    for index in range(30):
        job = db.enqueue(f"movie{index}.mkv", "sig", None, 0)
        db.update(job, state="superseded")
        kept.append(job)
    live = db.enqueue("current.mkv", "sig", None, 0)
    assert db.prune(keep=10) == 20
    remaining = {job["id"] for job in db.snapshot()["jobs"]}
    assert live in remaining
    assert db.snapshot()["counts"]["superseded"] == 10


def test_previously_unresolved_media_is_revisited_before_untouched_backlog(tmp_path):
    db = Database(tmp_path / "queue.db")
    stuck = db.enqueue("needs-attention.mkv", "policy-v1", None, 0)
    db.update(stuck, state="review", stage="Needs attention", error="could not decide")
    db.enqueue("never-looked-at.mkv", "policy-v2", None, 0)
    # A policy change gives every file a new signature.
    revisit = db.enqueue("needs-attention.mkv", "policy-v2", None, 0)
    assert db.get(revisit)["priority"] > db.get(db.enqueue("never-looked-at.mkv", "policy-v2", None, 0))["priority"]
    assert db.claim()["id"] == revisit


def test_waiting_for_bazarr_does_not_permanently_demote_media(tmp_path):
    """The subtitle wait must delay a job, not push it behind everything forever."""
    db = Database(tmp_path / "queue.db")
    # Media with no subtitle is enqueued first, but held for the Bazarr window.
    no_subtitle = db.enqueue("a-no-subtitle.mkv", "sig", None, time.time() + 60)
    db.enqueue("b-has-subtitle.mkv", "sig", "b.en.srt", 0)
    assert db.claim()["media"] == "b-has-subtitle.mkv"
    db.update(db.get(no_subtitle)["id"], state="waiting", ready=0)
    db.update(2, state="completed")
    # Once its wait expires it takes its place by arrival, not last.
    assert db.claim()["id"] == no_subtitle
