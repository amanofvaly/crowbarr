import fcntl
import json
import logging
import sqlite3
import threading
import time
import wave
from pathlib import Path

import pytest

from crowbarr import inference, processor, service
from crowbarr.config import ConfigStore, Settings
from crowbarr.db import Database, StaleJob
from crowbarr.subtitles import Word


def test_scan_failure_is_logged_and_later_success_clears_notice(tmp_path, monkeypatch, caplog):
    store = ConfigStore(tmp_path)
    db = Database(tmp_path / "crowbarr.db")
    daemon = service.Service(store, db)
    attempts = iter([sqlite3.OperationalError("database is locked"), None])

    def scan_once(*args):
        result = next(attempts)
        if result:
            raise result
        return 12

    waits = 0

    def wait(_timeout):
        nonlocal waits
        waits += 1
        if waits == 2:
            daemon.stop_event.set()
        return False

    monkeypatch.setattr(service, "scan", scan_once)
    monkeypatch.setattr(daemon.scan_event, "wait", wait)
    with caplog.at_level(logging.ERROR, logger="crowbarr"):
        daemon.scanner()

    records = [record for record in caplog.records if "Library scan failed" in record.message]
    assert len(records) == 1
    assert records[0].exc_info[0] is sqlite3.OperationalError
    assert "database is locked" in caplog.text
    assert daemon.scan_count == 12
    assert not any(notice["name"] == "scan" for notice in db.snapshot()["notices"])


def test_repeated_crashes_exhaust_attempts_and_clear_progress(tmp_path):
    db = Database(tmp_path / "crowbarr.db")
    identifier = db.enqueue("/media/movie.mkv", "one", None, 0)
    for attempt in range(1, 4):
        job = db.claim()
        assert job["attempts"] == attempt
        db.update(identifier, progress_current=20, progress_total=30, cached=1)
        db.recover(max_attempts=3)
    recovered = db.get(identifier)
    assert recovered["state"] == "failed"
    assert recovered["attempts"] == 3
    assert all(recovered[key] is None for key in ("progress_current", "progress_total", "cached", "started"))
    assert db.claim() is None
    db.recover(max_attempts=3)
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 1


def test_restart_honours_pending_cancellation(tmp_path):
    db = Database(tmp_path / "crowbarr.db")
    identifier = db.enqueue("/media/movie.mkv", "one", None, 0)
    db.claim()
    db.cancel(identifier)
    db.recover()
    assert db.get(identifier)["state"] == "cancelled"
    assert db.claim() is None


@pytest.mark.parametrize("replace", ["revision", "directive", "recovery"])
def test_old_worker_cannot_change_replacement_request(tmp_path, replace):
    db = Database(tmp_path / "crowbarr.db")
    identifier = db.enqueue("/media/movie.mkv", "one", None, 0)
    job = db.claim()
    worker = Database(db.path)
    worker.bind_worker(job)
    worker.update(identifier, stage="Recognizing dialogue", progress_current=10, progress_total=40)
    if replace == "revision":
        db.enqueue(job["media"], "two", None, 0)
    elif replace == "directive":
        db.direct(identifier, "generate")
    else:
        db.recover()
    expected = db.get(identifier)
    for values in ({"progress_current": 30}, {"state": "superseded"}, {"state": "completed"}):
        with pytest.raises(StaleJob):
            worker.update(identifier, **values)
    with pytest.raises(StaleJob):
        worker.register_artifact(job["media"], "/media/movie.crowbarr.en.srt", "old", identifier)
    assert not db.update(identifier, expected_generation=job["generation"], state="failed")
    assert db.get(identifier) == expected
    new_job = db.claim()
    assert new_job["generation"] > job["generation"]


@pytest.mark.parametrize("outcome", ["result", "review", "error"])
def test_subprocess_completion_cannot_overwrite_new_request(tmp_path, monkeypatch, outcome):
    db = Database(tmp_path / "crowbarr.db")
    identifier = db.enqueue("/media/movie.mkv", "one", None, 0, origin="manual")
    job = db.claim()
    monkeypatch.setattr(service.os, "setsid", lambda: None)

    def process(*args):
        db.direct(identifier, "generate")
        if outcome == "review":
            raise service.ReviewRequired("Synthetic review")
        if outcome == "error":
            raise ValueError("Synthetic failure")
        return {"state": "completed", "report": "{}"}

    monkeypatch.setattr(processor, "process", process)
    service.run_job(str(tmp_path), job, Settings().model_dump())
    assert db.get(identifier)["state"] == "queued"
    assert db.get(identifier)["directive"] == "generate"
    assert not (tmp_path / "reports" / f"{identifier}.json").exists()


def test_legacy_migration_preserves_ownership_history_and_is_idempotent(tmp_path):
    path = tmp_path / "crowbarr.db"
    db = Database(path)
    first = db.enqueue("/media/movie.mkv", "one", None, 0)
    output = "/media/movie.crowbarr.en.srt"
    # Reconstruct the pre-deduplication schema and report-only ownership.
    with db.connect() as connection:
        connection.execute("DROP INDEX jobs_media")
        connection.execute("ALTER TABLE jobs DROP COLUMN generation")
        connection.execute(
            "UPDATE jobs SET state='completed',output=?,report=? WHERE id=?",
            (output, json.dumps({"output_sha256": "legacy-digest"}), first),
        )
        connection.execute(
            "INSERT INTO jobs(media,signature,state,created,updated,ready) VALUES (?,?,?,0,1,0)",
            ("/media/movie.mkv", "two", "review"),
        )
    for _ in range(2):
        migrated = Database(path)
        assert migrated.owns_output("/media/movie.mkv", output, "legacy-digest")
        with migrated.connect() as connection:
            rows = connection.execute("SELECT * FROM jobs").fetchall()
            assert len(rows) == 1
            assert rows[0]["state"] == "review"
            assert rows[0]["generation"] == 0
            history = connection.execute("SELECT * FROM history").fetchall()
            assert len(history) == 1
            assert history[0]["state"] == "completed"
            assert history[0]["output"] == output
            assert history[0]["job_id"] is None


def test_failed_legacy_migration_rolls_back_schema_and_preserved_records(tmp_path):
    path = tmp_path / "crowbarr.db"
    db = Database(path)
    db.enqueue("/media/movie.mkv", "one", None, 0)
    with db.connect() as connection:
        connection.execute("DROP INDEX jobs_media")
        connection.execute("ALTER TABLE jobs DROP COLUMN generation")
        connection.execute(
            "UPDATE jobs SET state='completed',output='/media/movie.crowbarr.en.srt',report=?",
            (json.dumps({"output_sha256": "legacy-digest"}),),
        )
        connection.execute(
            "INSERT INTO jobs(media,signature,state,created,updated,ready) VALUES (?,?,?,0,1,0)",
            ("/media/movie.mkv", "two", "review"),
        )
        connection.execute(
            "CREATE TRIGGER stop_migration BEFORE DELETE ON jobs BEGIN "
            "SELECT RAISE(ABORT, 'Synthetic migration failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        Database(path)
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0
        assert "generation" not in {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}


@pytest.mark.parametrize(
    "damaged",
    [
        "{",
        '{"words":[{}],"issues":[]}',
        '{"words":[{"start":"bad","end":1,"text":"hello","probability":1}],"issues":[]}',
    ],
)
def test_corrupt_chunk_is_rebuilt_without_repeating_valid_chunks(tmp_path, monkeypatch, damaged):
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as output:
        output.setparams((1, 2, 10, 0, "NONE", "not compressed"))
        output.writeframes(b"\0\0" * 10 * 600)
    checkpoint = tmp_path / "transcript.parts"
    checkpoint.mkdir()
    valid = json.dumps({"words": [vars(Word(1, 2, "saved", 0.9))], "issues": []})
    (checkpoint / "0.json").write_text(valid)
    (checkpoint / "1.json").write_text(damaged)
    calls, progress = [], []

    def recognize(*args):
        calls.append(True)
        return [Word(3, 4, "rebuilt", 0.9)], []

    recognize.progress = lambda current, total: progress.append((current, total))
    monkeypatch.setattr(inference, "transcribe", recognize)
    words, issues = inference.transcribe_bounded(audio, Settings(), tmp_path, checkpoint)
    assert not issues
    assert [w.text for w in words] == ["saved", "rebuilt"]
    assert len(calls) == 1
    assert progress[-1] == (600, 600)
    assert (checkpoint / "0.json").read_text() == valid
    inference.transcribe_bounded(audio, Settings(), tmp_path, checkpoint)
    assert len(calls) == 1


def test_shutdown_uses_one_deadline_and_keeps_lock_for_live_threads(tmp_path):
    store = ConfigStore(tmp_path)
    instance = service.Service(store, Database(tmp_path / "crowbarr.db"))
    instance.lock_file = (tmp_path / "service.lock").open("a")
    fcntl.flock(instance.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    release = threading.Event()
    instance.threads = [threading.Thread(target=release.wait, daemon=True) for _ in range(2)]
    for thread in instance.threads:
        thread.start()
    try:
        started = time.monotonic()
        instance.close(timeout=0.05)
        assert time.monotonic() - started < 0.2
        assert instance.stop_event.is_set()
        with (tmp_path / "service.lock").open("a") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        release.set()
        for thread in instance.threads:
            thread.join(timeout=1)
    deadline = time.monotonic() + 1
    while not instance.lock_file.closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert instance.lock_file.closed


def test_main_initializes_frozen_workers_before_argument_parsing(monkeypatch):
    import sys

    import uvicorn

    from crowbarr import __main__

    events = []
    monkeypatch.setattr(__main__.multiprocessing, "freeze_support", lambda: events.append("freeze"))
    monkeypatch.setattr(sys, "argv", ["crowbarr", "--port", "8450"])
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: events.append(kwargs))
    __main__.main()
    assert events[0] == "freeze"
    assert events[1]["port"] == 8450
    assert events[1]["timeout_graceful_shutdown"] == 5


@pytest.mark.parametrize("interruption", ["stop", "settings", "replacement", "cancel"])
def test_active_worker_interruption_preserves_retry_and_new_request(tmp_path, monkeypatch, interruption):
    from types import SimpleNamespace

    store = ConfigStore(tmp_path)
    store.save(Settings(max_attempts=1))
    db = Database(tmp_path / "crowbarr.db")
    identifier = db.enqueue("/media/movie.mkv", "one", None, 0, origin="manual")
    instance = service.Service(store, db)

    class ImmediateEvent(threading.Event):
        def wait(self, timeout=None):
            return self.is_set()

    instance.stop_event = ImmediateEvent()

    class Child:
        pid = 123456
        alive = True
        interrupted = False

        def start(self):
            pass

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            if self.interrupted:
                return
            self.interrupted = True
            if interruption == "settings":
                store.save(Settings(model="base", max_attempts=1))
            elif interruption == "replacement":
                db.direct(identifier, "generate")
            elif interruption == "cancel":
                db.cancel(identifier)
            else:
                instance.stop_event.set()

        def terminate(self):
            self.alive = False

        def close(self):
            instance.stop_event.set()

    child = Child()
    signals = []

    def stop_group(pid, sig):
        signals.append(sig)
        child.terminate()

    monkeypatch.setattr(
        service.multiprocessing, "get_context", lambda method: SimpleNamespace(Process=lambda **kw: child)
    )
    monkeypatch.setattr(service, "resource_snapshot", lambda: {})
    monkeypatch.setattr(service, "gate", lambda *args: "")
    monkeypatch.setattr(service.os, "killpg", stop_group)
    instance.worker()
    job = db.get(identifier)
    assert not child.alive
    assert signals == [service.signal.SIGTERM, service.signal.SIGKILL]
    assert instance.child_pid is None
    if interruption == "replacement":
        assert job["state"] == "queued"
        assert job["directive"] == "generate"
        assert job["attempts"] == 0
    elif interruption == "cancel":
        assert job["state"] == "cancelled"
    else:
        assert job["state"] == "retry"
        assert job["attempts"] == 0
        assert job["origin"] == "manual"
        assert job["priority"] == 100
        assert db.claim()["attempts"] == 1


def test_open_event_stream_does_not_prevent_server_shutdown(tmp_path, monkeypatch):
    import asyncio
    import socket

    import httpx
    import uvicorn

    from crowbarr.app import create_app

    app = create_app(tmp_path)
    instance = app.state.service
    closed = threading.Event()
    monkeypatch.setattr(instance, "start", lambda: None)
    monkeypatch.setattr(instance, "close", closed.set)

    async def exercise():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    timeout_graceful_shutdown=0.1,
                    log_level="critical",
                    lifespan="on",
                )
            )
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        await asyncio.sleep(0.01)
                    async with httpx.AsyncClient(trust_env=False) as client:
                        url = f"http://127.0.0.1:{listener.getsockname()[1]}/api/events"
                        async with client.stream(
                            "GET", url, headers={"X-Api-Key": app.state.store.token}
                        ) as response:
                            assert response.status_code == 200
                            async for line in response.aiter_lines():
                                if line.startswith("event: status"):
                                    break
                            server.should_exit = True
                            await task
                            assert closed.is_set()
            finally:
                server.should_exit = True
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


def test_relative_app_data_path_is_fixed_before_working_directory_changes(tmp_path, monkeypatch):
    from crowbarr.app import create_app

    monkeypatch.chdir(tmp_path)
    app = create_app(Path("state"), background=False)
    monkeypatch.chdir(tmp_path.parent)
    assert app.state.store.directory == tmp_path / "state"
    app.state.store.save(Settings(paused=True))
    assert ConfigStore(tmp_path / "state").get().paused
    identifier = app.state.db.enqueue("/media/movie.mkv", "one", None, 0)
    assert Database(tmp_path / "state" / "crowbarr.db").get(identifier)
