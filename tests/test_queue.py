import hashlib
import importlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

from crowbarr.config import Settings
from crowbarr.db import Database
from crowbarr.library import scan, source_subtitle


def test_application_release_does_not_change_audit_policy(monkeypatch):
    import crowbarr
    import crowbarr.audit as audit

    policy = audit.AUDIT_VERSION
    monkeypatch.setattr(crowbarr, "__version__", "99.0.0")

    assert importlib.reload(audit).AUDIT_VERSION == policy


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
    # One file is one row: the arriving subtitle replaces the wait in place rather
    # than leaving a superseded duplicate behind.
    assert len(jobs) == 1
    assert jobs[0]["state"] == "queued"
    assert jobs[0]["source"].endswith(".en.srt")
    scan(settings, db)
    assert len(db.snapshot()["jobs"]) == 1


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
    # The retired output is gone and the file is queued again for the new video.
    assert db.get(job["id"])["state"] == "queued"


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
    assert (
        db.get(revisit)["priority"]
        > db.get(db.enqueue("never-looked-at.mkv", "policy-v2", None, 0))["priority"]
    )
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


def test_progress_is_numeric_and_resets_when_stage_changes(tmp_path):
    from crowbarr.db import Database

    db = Database(tmp_path / "progress.db")
    identifier = db.enqueue("/media/test.mkv", "sig", None, 0)
    job = db.claim()
    assert job["started"] and job["progress_current"] is None
    db.update(identifier, stage="Recognizing dialogue", progress_current=10, progress_total=40)
    assert db.get(identifier)["progress_current"] == 10
    db.update(identifier, stage="Aligning words to audio")
    assert db.get(identifier)["progress_current"] is None
    assert db.get(identifier)["progress_total"] is None


def test_a_policy_change_reopens_unresolved_work_without_manual_intervention(tmp_path):
    """Installing an update must revisit what it could not decide, on its own."""
    db = Database(tmp_path / "queue.db")
    stuck = db.enqueue("unclear.mkv", "sig", None, 0)
    db.update(stuck, state="review", stage="Needs attention", error="could not decide")
    settled = db.enqueue("fine.mkv", "sig", None, 0)
    db.update(settled, state="unchanged", stage="Audit passed")

    assert db.adopt_policy("policy-one") == 0          # first run reconsiders nothing
    assert db.adopt_policy("policy-one") == 0          # unchanged policy is a no-op
    assert db.adopt_policy("policy-two") == 1          # only the unresolved one comes back

    assert db.get(stuck)["state"] == "queued"          # reconsidered under the new rules
    assert db.get(settled)["state"] == "unchanged"     # an upgrade does not undo this


def test_counts_describe_files_not_job_rows(tmp_path):
    """A re-queued file must not be counted as both finished and pending."""
    db = Database(tmp_path / "queue.db")
    first = db.enqueue("episode.mkv", "policy-one", None, 0)
    db.update(first, state="unchanged", stage="Audit passed")
    db.enqueue("episode.mkv", "policy-two", None, 0)      # an update re-queues it
    db.enqueue("other.mkv", "policy-two", None, 0)

    counts = db.snapshot()["counts"]
    assert sum(counts.values()) == 2, "two files, two counts"
    assert counts.get("unchanged") is None, "its old verdict is no longer current"
    assert counts["queued"] == 2


def test_a_changed_file_is_a_new_request_but_an_unchanged_one_keeps_waiting(tmp_path):
    """`created` drives ageing and the "requested" column, so it must mean something."""
    db = Database(tmp_path / "queue.db")
    job = db.enqueue("movie.mkv", "inputs-one", None, 0)
    db.update(job, state="unchanged")
    first = db.get(job)["created"]

    db.enqueue("movie.mkv", "inputs-one", None, 0)          # nothing changed
    assert db.get(job)["created"] == first

    time.sleep(0.01)
    db.enqueue("movie.mkv", "inputs-two", None, 0)          # its subtitle changed
    assert db.get(job)["created"] > first, "a changed file is a fresh request"


def test_keeping_results_across_a_model_change_does_not_requeue(tmp_path):
    """Changing the model re-checks everything. A user who declines that keeps their
    verdicts, and only files whose media actually changed come back."""
    from crowbarr.config import Settings
    from crowbarr.db import Database
    from crowbarr.library import queue_media, signature

    media = tmp_path / "library" / "show.mkv"
    media.parent.mkdir()
    media.write_bytes(b"video")
    db = Database(tmp_path / "state" / "crowbarr.db")
    before = Settings(roots=[str(media.parent)], settle_seconds=0, subtitle_wait_minutes=0)
    assert queue_media(media, before, db, 0.0)
    job = db.get(db.claim()["id"])
    db.update(job["id"], state="unchanged", stage="")

    after = before.model_copy(update={"model": "medium"})
    assert after.fingerprint() != before.fingerprint()

    # Without the carried fingerprint the settled verdict is thrown away.
    assert queue_media(media, after, db, 1.0)
    assert db.get(job["id"])["state"] == "queued"

    db.update(job["id"], state="unchanged", stage="")
    later = after.model_copy(update={"model": "large-v3"})
    assert queue_media(media, later, db, 2.0, carried=after.fingerprint())
    kept = db.get(job["id"])
    assert kept["state"] == "unchanged", "the verdict was carried forward"
    assert kept["signature"] == signature(media, None, later)
