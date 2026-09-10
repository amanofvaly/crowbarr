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


def test_switching_back_to_an_exact_previous_signature_restores_all_results_together(tmp_path):
    from crowbarr.config import Settings
    from crowbarr.library import prepare_and_restore, queue_media

    db = Database(tmp_path / "state" / "crowbarr.db")
    first = Settings(
        roots=[str(tmp_path)], model="small.en", settle_seconds=0, subtitle_wait_minutes=0
    )
    second = first.model_copy(update={"model": "medium.en"})
    media = []
    jobs = {}
    for name in ("one.mkv", "two.mkv"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        media.append(path)
        queue_media(path, first, db, 0)
        with db.connect() as connection:
            jobs[path] = connection.execute(
                "SELECT id FROM jobs WHERE media=?", (str(path),)
            ).fetchone()[0]
        db.update(jobs[path], state="unchanged", stage="Audit passed")

    for path in media:
        queue_media(path, second, db, 1)
        assert db.get(jobs[path])["state"] == "queued"

    assert len(prepare_and_restore(media, first, db, 2)[2]) == 2
    assert {db.get(jobs[path])["state"] for path in media} == {"unchanged"}
    assert {db.get(jobs[path])["cached"] for path in media} == {None}


def test_carry_forward_belongs_to_the_target_scan_and_survives_older_scan_completion(tmp_path):
    db = Database(tmp_path / "queue.db")
    db.request_carry_forward("small", "medium")
    assert db.carried_fingerprint("small") == ("", "")
    assert not db.finish_carry_forward("")
    source, token = db.carried_fingerprint("medium")
    assert source == "small"
    assert db.finish_carry_forward(token)
    assert db.carried_fingerprint("medium") == ("", "")


def test_rapid_kept_changes_retain_the_original_result_fingerprint(tmp_path):
    db = Database(tmp_path / "queue.db")
    db.request_carry_forward("small", "medium")
    db.request_carry_forward("medium", "large")
    source, _ = db.carried_fingerprint("large")
    assert source == "small"


def test_failed_scan_does_not_discard_a_pending_keep_results_request(tmp_path):
    from crowbarr.config import Settings
    from crowbarr.library import scan

    db = Database(tmp_path / "state" / "crowbarr.db")
    settings = Settings(roots=[str(tmp_path / "missing")])
    db.request_carry_forward("old", settings.fingerprint())

    assert scan(settings, db) == 0
    source, _ = db.carried_fingerprint(settings.fingerprint())
    assert source == "old"


def test_successful_scan_consumes_only_its_keep_results_request(tmp_path):
    from crowbarr.config import Settings
    from crowbarr.library import queue_media, scan

    media = tmp_path / "show.mkv"
    media.write_bytes(b"video")
    before = Settings(roots=[str(tmp_path)], model="small.en", settle_seconds=0, subtitle_wait_minutes=0)
    after = before.model_copy(update={"model": "medium.en"})
    db = Database(tmp_path / "state" / "crowbarr.db")
    queue_media(media, before, db, 0)
    with db.connect() as connection:
        job = connection.execute("SELECT id FROM jobs WHERE media=?", (str(media),)).fetchone()[0]
    db.update(job, state="unchanged")
    db.request_carry_forward(before.fingerprint(), after.fingerprint())

    assert scan(after, db) == 1
    assert db.get(job)["state"] == "unchanged"
    assert db.carried_fingerprint(after.fingerprint()) == ("", "")


def test_exact_result_restore_does_not_replace_an_explicit_manual_run(tmp_path):
    from crowbarr.config import Settings
    from crowbarr.library import prepare_and_restore, queue_media

    media = tmp_path / "show.mkv"
    media.write_bytes(b"video")
    settings = Settings(roots=[str(tmp_path)], settle_seconds=0, subtitle_wait_minutes=0)
    db = Database(tmp_path / "state" / "crowbarr.db")
    queue_media(media, settings, db, 0)
    with db.connect() as connection:
        job = connection.execute("SELECT id FROM jobs WHERE media=?", (str(media),)).fetchone()[0]
    db.update(job, state="unchanged")
    assert db.direct(job, "")

    assert len(prepare_and_restore([media], settings, db, 1)[2]) == 0
    assert db.get(job)["state"] == "queued"
    assert db.get(job)["origin"] == "manual"


def test_published_result_is_reused_only_while_its_output_still_matches(tmp_path):
    import hashlib
    import json

    from crowbarr.config import Settings
    from crowbarr.library import prepare_and_restore, queue_media

    media = tmp_path / "movie.mkv"
    media.write_bytes(b"video")
    output = tmp_path / "movie.crowbarr.en.srt"
    output.write_bytes(b"original subtitle")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    small = Settings(
        roots=[str(tmp_path)], model="small.en", settle_seconds=0, subtitle_wait_minutes=0
    )
    medium = small.model_copy(update={"model": "medium.en"})
    db = Database(tmp_path / "state" / "crowbarr.db")
    queue_media(media, small, db, 0)
    with db.connect() as connection:
        job = connection.execute("SELECT id FROM jobs WHERE media=?", (str(media),)).fetchone()[0]
    db.update(job, state="completed", output=str(output), report=json.dumps({"output_sha256": digest}))

    queue_media(media, medium, db, 1)
    assert len(prepare_and_restore([media], small, db, 2)[2]) == 1
    assert db.get(job)["state"] == "completed"

    queue_media(media, medium, db, 2)
    output.unlink()
    assert len(prepare_and_restore([media], small, db, 3)[2]) == 0
    assert not output.exists()
    assert db.get(job)["state"] == "queued"

    output.write_bytes(b"edited outside Crowbarr")
    assert len(prepare_and_restore([media], small, db, 4)[2]) == 0
    assert db.get(job)["state"] == "queued"


def test_exact_result_restore_does_not_replace_another_output(tmp_path):
    import hashlib
    import json

    from crowbarr.config import Settings
    from crowbarr.library import prepare_and_restore, queue_media

    media = tmp_path / "movie.mkv"
    media.write_bytes(b"video")
    output = tmp_path / "movie.crowbarr.en.srt"
    output.write_bytes(b"small result")
    small_digest = hashlib.sha256(output.read_bytes()).hexdigest()
    small = Settings(roots=[str(tmp_path)], model="small.en", settle_seconds=0, subtitle_wait_minutes=0)
    medium = small.model_copy(update={"model": "medium.en"})
    db = Database(tmp_path / "state" / "crowbarr.db")
    queue_media(media, small, db, 0)
    with db.connect() as connection:
        job = connection.execute("SELECT id FROM jobs WHERE media=?", (str(media),)).fetchone()[0]
    db.update(
        job, state="completed", output=str(output),
        report=json.dumps({"output_sha256": small_digest}),
    )

    queue_media(media, medium, db, 1)
    output.write_bytes(b"medium result")
    medium_digest = hashlib.sha256(output.read_bytes()).hexdigest()
    db.register_artifact(str(media), str(output), medium_digest, job)

    assert len(prepare_and_restore([media], small, db, 2)[2]) == 0
    assert output.read_bytes() == b"medium result"
    assert db.get(job)["state"] == "queued"


def test_exact_review_is_reused_only_while_its_candidate_still_matches(tmp_path):
    import hashlib
    import json

    from crowbarr.config import Settings
    from crowbarr.library import prepare_and_restore, queue_media
    from crowbarr.processor import new_review_candidate_path

    media = tmp_path / "show.mkv"
    media.write_bytes(b"video")
    small = Settings(roots=[str(tmp_path)], model="small.en", settle_seconds=0, subtitle_wait_minutes=0)
    medium = small.model_copy(update={"model": "medium.en"})
    db = Database(tmp_path / "state" / "crowbarr.db")
    queue_media(media, small, db, 0)
    with db.connect() as connection:
        job = connection.execute("SELECT id FROM jobs WHERE media=?", (str(media),)).fetchone()[0]
    small_job = db.get(job)
    candidate = new_review_candidate_path(tmp_path, small_job)
    candidate.parent.mkdir()
    candidate.write_bytes(b"small review candidate")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    db.update(job, state="review", report=json.dumps({"candidate": str(candidate), "output_sha256": digest}))

    queue_media(media, medium, db, 1)
    medium_job = db.get(job)
    medium_candidate = new_review_candidate_path(tmp_path, medium_job)
    medium_candidate.write_bytes(b"medium review candidate")
    medium_digest = hashlib.sha256(medium_candidate.read_bytes()).hexdigest()
    db.update(
        job,
        state="review",
        report=json.dumps({"candidate": str(medium_candidate), "output_sha256": medium_digest}),
    )

    assert candidate != medium_candidate
    assert candidate.read_bytes() == b"small review candidate"
    assert len(prepare_and_restore([media], small, db, 2)[2]) == 1
    assert db.get(job)["state"] == "review"

    queue_media(media, medium, db, 2)
    candidate.write_bytes(b"changed candidate")
    assert len(prepare_and_restore([media], small, db, 3)[2]) == 0
    assert db.get(job)["state"] == "queued"


def test_new_review_does_not_overwrite_a_legacy_candidate(tmp_path):
    from crowbarr.processor import new_review_candidate_path, review_candidate_path

    directory = tmp_path / "state"
    legacy = directory / "candidates" / "7.srt"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy candidate")
    job = {
        "id": 7,
        "signature": "a" * 64,
        "report": json.dumps({"candidate": str(legacy)}),
    }

    assert review_candidate_path(directory, job) == legacy
    write_path = new_review_candidate_path(directory, job)
    assert write_path != legacy
    write_path.write_text("new candidate")
    assert legacy.read_text() == "legacy candidate"


def test_pruning_a_saved_review_removes_its_signature_candidate(tmp_path):
    from crowbarr.processor import new_review_candidate_path

    db = Database(tmp_path / "state" / "crowbarr.db")
    job_id = db.enqueue("show.mkv", "0" * 64, None, 0)
    first_candidate = None
    for index in range(6):
        signature = f"{index:064x}"
        with db.connect() as connection:
            connection.execute("UPDATE jobs SET signature=? WHERE id=?", (signature, job_id))
        job = db.get(job_id)
        candidate = new_review_candidate_path(db.path.parent, job)
        candidate.parent.mkdir(exist_ok=True)
        candidate.write_text(f"candidate {index}")
        if index == 0:
            first_candidate = candidate
        db.update(
            job_id,
            state="review",
            report=json.dumps({"candidate": str(candidate), "output_sha256": "unused"}),
        )
        time.sleep(0.001)

    assert first_candidate is not None and not first_candidate.exists()
    assert len(list((db.path.parent / "candidates").iterdir())) == 5


def test_result_digest_backfill_runs_only_once(tmp_path):
    import hashlib
    import json

    media = tmp_path / "movie.mkv"
    media.write_bytes(b"video")
    output = tmp_path / "movie.crowbarr.en.srt"
    output.write_bytes(b"subtitle")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    db = Database(tmp_path / "state" / "crowbarr.db")
    job = db.enqueue(str(media), "signature", None, 0)
    db.update(job, state="completed", output=str(output), report=json.dumps({"output_sha256": digest}))
    with db.connect() as connection:
        connection.execute("UPDATE results SET output_sha256=NULL")
        connection.execute("DELETE FROM meta WHERE key='result_digest_backfill_v1'")

    assert db.backfill_result_digests() == 1
    output.unlink()
    assert db.backfill_result_digests() == 0


def test_scan_hashes_each_signature_once(tmp_path, monkeypatch):
    from crowbarr import library
    from crowbarr.config import Settings

    media = tmp_path / "show.mkv"
    media.write_bytes(b"video")
    db = Database(tmp_path / "state" / "crowbarr.db")
    settings = Settings(roots=[str(tmp_path)], settle_seconds=0, subtitle_wait_minutes=0)
    calls = 0
    original = library.signature

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(library, "signature", counted)
    assert library.scan(settings, db) == 1
    assert calls == 1


def test_one_unreadable_file_does_not_leave_keep_results_pending(tmp_path, monkeypatch):
    from crowbarr import library
    from crowbarr.config import Settings

    media = tmp_path / "show.mkv"
    media.write_bytes(b"video")
    settings = Settings(roots=[str(tmp_path)])
    db = Database(tmp_path / "state" / "crowbarr.db")
    db.request_carry_forward("old", settings.fingerprint())
    monkeypatch.setattr(
        library, "prepare_media", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError())
    )

    assert library.scan(settings, db) == 0
    assert db.carried_fingerprint(settings.fingerprint()) == ("", "")
