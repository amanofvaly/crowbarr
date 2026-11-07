"""Parallel GPU jobs: admission, isolation, accounting and shutdown with N workers."""

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from crowbarr import service
from crowbarr.config import ConfigStore, Settings
from crowbarr.db import Database


def runtime_report(cuda: bool):
    entry = {"available": True, "reason": "", "compute_types": ["int8"]}
    return {
        "cpu": entry,
        "cuda": entry if cuda else {"available": False, "reason": "CUDA is unavailable. Use the CUDA image.", "compute_types": []},
        "refinement": {"available": False, "reason": "missing"},
    }


def make_service(tmp_path, monkeypatch, cuda=True, **values):
    store = ConfigStore(tmp_path)
    settings = {"device": "cuda", "max_concurrent_jobs": 2, "backlog_cooldown_seconds": 0, **values}
    store.save(Settings(**settings))
    db = Database(tmp_path / "crowbarr.db")
    instance = service.Service(store, db)
    monkeypatch.setattr("crowbarr.capabilities.runtime_capabilities", lambda block=True: runtime_report(cuda))
    monkeypatch.setattr(service, "resource_snapshot", lambda: {})
    monkeypatch.setattr(service, "plex_activity", lambda *args: False)
    return store, db, instance


class FakeChildren:
    """Stand-in for spawned job processes that run until they are signalled."""

    def __init__(self):
        self.children = []
        self.signalled = []
        self.lock = threading.Lock()

    def process(self, **kwargs):
        owner = self

        class Child:
            def __init__(self):
                self.alive = True
                with owner.lock:
                    self.pid = 900000 + len(owner.children)
                    owner.children.append(self)
                self.job = kwargs["args"][1]

            def start(self):
                pass

            def is_alive(self):
                return self.alive

            def join(self, timeout=None):
                if self.alive:
                    time.sleep(min(timeout or 0.05, 0.05))

            def terminate(self):
                self.alive = False

            def kill(self):
                self.alive = False

            def close(self):
                pass

        return Child()

    def killpg(self, pid, sig):
        with self.lock:
            self.signalled.append((pid, sig))
            for child in self.children:
                if child.pid == pid:
                    child.alive = False


def start_slots(instance, count):
    threads = [threading.Thread(target=instance.worker, args=(slot,), daemon=True) for slot in range(count)]
    instance.threads = threads
    for thread in threads:
        thread.start()
    return threads


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def processing(db):
    with db.connect() as connection:
        return [row[0] for row in connection.execute("SELECT id FROM jobs WHERE state='processing'")]


# ----- settings -------------------------------------------------------------------------


def test_parallel_jobs_setting_defaults_to_one_and_is_bounded():
    from crowbarr.config import FINGERPRINT_FIELDS

    assert Settings().max_concurrent_jobs == 1
    assert Settings().one_recognition_at_a_time is True
    assert "one_recognition_at_a_time" not in FINGERPRINT_FIELDS
    assert "max_concurrent_jobs" not in FINGERPRINT_FIELDS
    assert Settings(max_concurrent_jobs=1).fingerprint() == Settings(max_concurrent_jobs=4).fingerprint()
    for value in (0, 5):
        with pytest.raises(ValidationError):
            Settings(max_concurrent_jobs=value)


def test_saved_parallel_value_applies_only_on_a_working_gpu():
    assert Settings(device="cuda", max_concurrent_jobs=2).parallel_jobs(True) == 2
    assert Settings(device="cuda", max_concurrent_jobs=2).parallel_jobs(False) == 1
    assert Settings(device="cuda", max_concurrent_jobs=2).parallel_jobs(None) == 1
    assert Settings(device="cpu", max_concurrent_jobs=2).parallel_jobs(True) == 1


# ----- claiming -------------------------------------------------------------------------


def test_two_jobs_are_claimed_at_once_and_never_the_same_one(tmp_path):
    db = Database(tmp_path / "crowbarr.db")
    for index in range(3):
        db.enqueue(f"/media/{index}.mkv", str(index), None, 0)
    first = db.claim(max_processing=2)
    second = db.claim(max_processing=2)
    assert first and second and first["id"] != second["id"]
    assert db.claim(max_processing=2) is None
    assert sorted(processing(db)) == sorted([first["id"], second["id"]])


def test_concurrent_claimers_respect_the_limit_without_duplicates(tmp_path):
    path = tmp_path / "crowbarr.db"
    Database(path)
    seed = Database(path)
    for index in range(6):
        seed.enqueue(f"/media/{index}.mkv", str(index), None, 0)
    barrier = threading.Barrier(6)
    claimed, errors = [], []

    def claimer():
        db = Database(path)
        barrier.wait()
        try:
            job = db.claim(max_processing=2)
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)
            return
        if job:
            claimed.append(job["id"])

    threads = [threading.Thread(target=claimer) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    assert len(claimed) == 2 and len(set(claimed)) == 2
    assert sorted(processing(seed)) == sorted(claimed)


def test_a_limit_of_one_claims_exactly_as_before(tmp_path):
    db = Database(tmp_path / "crowbarr.db")
    db.enqueue("/media/one.mkv", "a", None, 0)
    db.enqueue("/media/two.mkv", "b", None, 0)
    assert db.claim()["media"] == "/media/one.mkv"
    assert db.claim() is None
    assert db.claim(max_processing=1) is None


# ----- work folder ----------------------------------------------------------------------


def test_job_cleanup_removes_only_that_jobs_leftovers(tmp_path, monkeypatch):
    _, _, instance = make_service(tmp_path, monkeypatch)
    work = tmp_path / "work"
    for name in ("1-abc", "12-xyz", "2-def"):
        (work / name).mkdir(parents=True)
        (work / name / "audio.wav").write_bytes(b"pcm")
    service.fallback_marker(tmp_path, 1).touch()
    instance._clean_job_work(1)
    assert sorted(p.name for p in work.iterdir()) == ["12-xyz", "2-def"]
    assert (work / "2-def" / "audio.wav").read_bytes() == b"pcm"
    assert (work / "12-xyz" / "audio.wav").exists()


def test_a_finished_job_does_not_wipe_a_running_jobs_audio(tmp_path, monkeypatch):
    _, db, instance = make_service(tmp_path, monkeypatch)
    first = db.enqueue("/media/one.mkv", "a", None, 0, origin="manual")
    db.enqueue("/media/two.mkv", "b", None, 0, origin="manual")
    children = FakeChildren()
    monkeypatch.setattr(service.multiprocessing, "get_context", lambda method: SimpleNamespace(Process=children.process))
    monkeypatch.setattr(service.os, "killpg", children.killpg)
    start_slots(instance, 2)
    try:
        assert wait_for(lambda: len(children.children) == 2)
        for child in children.children:
            folder = tmp_path / "work" / f"{child.job['id']}-tmp"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "audio.wav").write_bytes(b"pcm")
        finished = next(c for c in children.children if c.job["id"] == first)
        other = next(c for c in children.children if c.job["id"] != first)
        db.update(first, state="completed", stage="")
        finished.alive = False
        assert wait_for(lambda: not (tmp_path / "work" / f"{first}-tmp").exists())
        assert (tmp_path / "work" / f"{other.job['id']}-tmp" / "audio.wav").read_bytes() == b"pcm"
    finally:
        instance.close(timeout=10)


# ----- hourly budget --------------------------------------------------------------------


def test_overlapping_jobs_spend_the_budget_once(tmp_path, monkeypatch):
    db = Database(tmp_path / "crowbarr.db")
    monkeypatch.setattr(time, "time", lambda: 10_000)
    # Two workers, each busy for 20 minutes, overlapping for 10: 30 wall-clock minutes.
    db.record_usage(7_000, 1_200)
    db.record_usage(7_600, 1_200)
    db.record_usage(9_500, 100)  # a later job on its own
    assert db.background_seconds() == pytest.approx(1_800 + 100)
    budget = db.background_budget(30)
    assert budget["used_seconds"] == pytest.approx(1_900)
    assert budget["remaining_seconds"] == 0
    # Merged spans 7000-8800 and 9500-9600 hold 1900s; dropping under 1800s needs
    # 100s of the first span to leave the rolling hour (summing would need 700s more).
    assert budget["resume_at"] == pytest.approx(10_700, abs=1)


def test_a_full_hour_cap_stays_reachable_only_after_a_full_hour_with_two_workers(tmp_path, monkeypatch):
    db = Database(tmp_path / "crowbarr.db")
    monkeypatch.setattr(time, "time", lambda: 10_000)
    # Two workers running side by side for the last 35 minutes.
    db.record_usage(7_900, 2_100)
    db.record_usage(7_900, 2_100)
    assert db.background_seconds() == pytest.approx(2_100)
    assert db.background_budget(60)["remaining_seconds"] == pytest.approx(1_500)


# ----- shutdown -------------------------------------------------------------------------


def test_shutdown_stops_both_children_and_refunds_their_attempts(tmp_path, monkeypatch):
    _, db, instance = make_service(tmp_path, monkeypatch)
    ids = [db.enqueue(f"/media/{n}.mkv", n, None, 0, origin="manual") for n in ("one", "two")]
    children = FakeChildren()
    monkeypatch.setattr(service.multiprocessing, "get_context", lambda method: SimpleNamespace(Process=children.process))
    monkeypatch.setattr(service.os, "killpg", children.killpg)
    start_slots(instance, 2)
    assert wait_for(lambda: len(processing(db)) == 2)
    instance.close(timeout=10)
    assert all(not thread.is_alive() for thread in instance.threads)
    assert {pid for pid, _ in children.signalled} == {child.pid for child in children.children}
    assert len(children.children) == 2
    assert not instance.child_pids and not instance.running
    for identifier in ids:
        job = db.get(identifier)
        assert job["state"] == "retry"
        assert job["attempts"] == 0
        assert job["error"] == "Service stopping"


def test_shutdown_deadline_kills_every_child(tmp_path, monkeypatch):
    _, _, instance = make_service(tmp_path, monkeypatch)
    release = threading.Event()
    instance.threads = [threading.Thread(target=release.wait, daemon=True)]
    instance.threads[0].start()
    instance.child_pids = {111, 222}
    killed = []
    monkeypatch.setattr(service.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    try:
        instance.close(timeout=0.05)
    finally:
        release.set()
    assert sorted(killed) == [(111, service.signal.SIGKILL), (222, service.signal.SIGKILL)]


# ----- device rules ---------------------------------------------------------------------


@pytest.mark.parametrize("device,cuda", [("cpu", True), ("cuda", False)])
def test_worker_runs_one_job_without_a_working_gpu_even_with_two_saved(tmp_path, monkeypatch, device, cuda):
    _, db, instance = make_service(tmp_path, monkeypatch, cuda=cuda, device=device, cpu_fallback=True)
    for n in ("one", "two"):
        db.enqueue(f"/media/{n}.mkv", n, None, 0, origin="manual")
    children = FakeChildren()
    monkeypatch.setattr(service.multiprocessing, "get_context", lambda method: SimpleNamespace(Process=children.process))
    monkeypatch.setattr(service.os, "killpg", children.killpg)
    start_slots(instance, service.WORKER_SLOTS)
    try:
        assert wait_for(lambda: len(processing(db)) == 1)
        time.sleep(2.5)  # several admission rounds for every idle slot
        assert len(processing(db)) == 1
        assert len(children.children) == 1
    finally:
        instance.close(timeout=10)


def test_with_a_working_gpu_two_jobs_run_and_the_rest_wait(tmp_path, monkeypatch):
    _, db, instance = make_service(tmp_path, monkeypatch)
    for n in ("one", "two", "three"):
        db.enqueue(f"/media/{n}.mkv", n, None, 0, origin="manual")
    children = FakeChildren()
    monkeypatch.setattr(service.multiprocessing, "get_context", lambda method: SimpleNamespace(Process=children.process))
    monkeypatch.setattr(service.os, "killpg", children.killpg)
    start_slots(instance, service.WORKER_SLOTS)
    try:
        assert wait_for(lambda: len(processing(db)) == 2)
        time.sleep(2.5)
        assert len(processing(db)) == 2
        assert len({child.job["id"] for child in children.children}) == 2
    finally:
        instance.close(timeout=10)


def admit_settings(**values):
    return Settings(device="cuda", max_concurrent_jobs=2, backlog_cooldown_seconds=0, **values)


def test_a_cpu_fallback_job_holds_off_the_next_job(tmp_path, monkeypatch):
    _, db, instance = make_service(tmp_path, monkeypatch)
    for n in ("one", "two"):
        db.enqueue(f"/media/{n}.mkv", n, None, 0, origin="manual")
    instance.resources = {"ram_available_mb": None, "vram_free_mb": None}
    instance.last_resource_check = time.monotonic()
    settings = admit_settings()
    first = instance._admit(0, settings, 2)
    assert first
    service.fallback_marker(tmp_path, first["id"]).parent.mkdir(parents=True, exist_ok=True)
    service.fallback_marker(tmp_path, first["id"]).touch()
    assert instance._admit(1, settings, 2) is None
    assert "fell back to CPU" in instance.wait_reason
    assert len(processing(db)) == 1
    # Once the fallback job ends, the slot claims again.
    instance.running.pop(first["id"])
    instance._clean_job_work(first["id"])
    db.update(first["id"], state="completed", stage="")
    assert instance._admit(1, settings, 2)


def test_free_vram_reserves_room_for_a_job_that_has_not_loaded_its_model(tmp_path, monkeypatch):
    _, db, instance = make_service(tmp_path, monkeypatch)
    for n in ("one", "two"):
        db.enqueue(f"/media/{n}.mkv", n, None, 0, origin="manual")
    monkeypatch.setattr(
        service, "resource_snapshot", lambda: {"ram_available_mb": None, "vram_free_mb": 3000, "cpu_load": 0}
    )
    settings = admit_settings(min_free_vram_mb=2048)
    assert instance._admit(0, settings, 2)
    # 3000 MB free, minus 1200 MB held back for the job still loading, is under 2048.
    assert instance._admit(1, settings, 2) is None
    assert "free GPU memory" in instance.wait_reason
    # Once the first job's model is resident, the reading is taken at face value.
    for info in instance.running.values():
        info["started"] -= service.STARTING_JOB_SECONDS + 1
    assert instance._admit(1, settings, 2)


def test_each_slot_has_its_own_cooldown(tmp_path, monkeypatch):
    _, db, instance = make_service(tmp_path, monkeypatch, backlog_cooldown_seconds=600)
    for n in ("one", "two"):
        db.enqueue(f"/media/{n}.mkv", n, None, 0)
    instance.resources = {"ram_available_mb": None, "vram_free_mb": None, "cpu_load": 0}
    instance.last_resource_check = time.monotonic()
    settings = Settings(device="cuda", max_concurrent_jobs=2, backlog_cooldown_seconds=600)
    instance.slot_background_end[0] = time.time()
    assert instance._admit(0, settings, 2) is None
    assert instance.wait_reason == "Background cooldown"
    assert instance._admit(1, settings, 2)


# ----- API ------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    from crowbarr.app import create_app

    monkeypatch.setattr("crowbarr.capabilities.runtime_capabilities", lambda block=True: runtime_report(True))
    with TestClient(create_app(tmp_path, background=False)) as client:
        client.headers["X-Api-Key"] = client.app.state.store.token
        yield client


def test_api_refuses_parallel_jobs_on_cpu(client):
    response = client.put("/api/settings", json={"device": "cpu", "max_concurrent_jobs": 2})
    assert response.status_code == 422
    assert "CUDA" in response.json()["detail"]
    assert client.app.state.store.get().max_concurrent_jobs == 1


def test_api_accepts_parallel_jobs_on_cuda(client):
    response = client.put("/api/settings", json={"device": "cuda", "max_concurrent_jobs": 2})
    assert response.status_code == 200
    assert response.json()["max_concurrent_jobs"] == 2
    assert client.put("/api/settings", json={"max_concurrent_jobs": 5}).status_code == 422


@pytest.mark.parametrize("full_payload", [False, True])
def test_switching_to_cpu_resets_parallel_jobs(client, full_payload):
    assert client.put("/api/settings", json={"device": "cuda", "max_concurrent_jobs": 2}).status_code == 200
    payload = client.get("/api/settings").json() if full_payload else {}
    payload["device"] = "cpu"
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200
    assert response.json()["device"] == "cpu"
    assert client.app.state.store.get().max_concurrent_jobs == 1


def test_settings_page_offers_parallel_jobs_only_for_the_gpu(client):
    script = client.get("/static/app.js").text
    assert "Files in progress at once" in script
    assert 'name="max_concurrent_jobs"' in script
    assert "parallel.disabled = cpu" in script
    assert "Wait for GPU processing (one speech recognition at a time)" in script
    assert '"one_recognition_at_a_time"' in script
    # The dashboard lists every processing job, not just the first.
    assert 'filter((j) => j.state === "processing")' in script


# ----- one recognition at a time --------------------------------------------------------


def test_recognition_slot_admits_one_holder_and_reports_the_wait(tmp_path):
    from crowbarr.resources import recognition_slot

    events = []
    holding, release = threading.Event(), threading.Event()

    def first():
        with recognition_slot(tmp_path, waiting=lambda: events.append("first waited")):
            events.append("first recognizing")
            holding.set()
            release.wait(5)
            events.append("first done")

    def second():
        holding.wait(5)
        with recognition_slot(
            tmp_path, waiting=lambda: events.append("second waiting"), acquired=lambda: events.append("second acquired")
        ):
            events.append("second recognizing")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    assert wait_for(lambda: "second waiting" in events, timeout=5)
    time.sleep(0.2)
    assert "second recognizing" not in events
    release.set()
    for thread in threads:
        thread.join(timeout=5)
    assert events == [
        "first recognizing", "second waiting", "first done", "second acquired", "second recognizing"
    ]


def test_a_killed_holder_frees_the_recognition_slot(tmp_path):
    import multiprocessing

    from crowbarr.resources import recognition_slot

    context = multiprocessing.get_context("spawn")
    child = context.Process(target=_hold_recognition_slot, args=(str(tmp_path),))
    child.start()
    assert wait_for(lambda: (tmp_path / "held").exists(), timeout=20)
    waited = []
    child.kill()
    child.join(5)
    with recognition_slot(tmp_path, waiting=lambda: waited.append(True)):
        pass
    assert child.exitcode is not None


def _hold_recognition_slot(directory):
    from pathlib import Path

    from crowbarr.resources import recognition_slot

    with recognition_slot(Path(directory)):
        (Path(directory) / "held").touch()
        time.sleep(60)


@pytest.mark.parametrize("windows", [None, [(0, 2)]])
def test_recognition_holds_the_slot_and_releases_the_model(tmp_path, monkeypatch, windows):
    import fcntl

    from crowbarr import inference, processor
    from crowbarr.resources import recognition_slot
    from crowbarr.subtitles import Word

    media = tmp_path / "movie.mkv"
    media.write_bytes(b"video")
    state = tmp_path / "state"
    observed, released = [], []

    def recognize(*args, **kwargs):
        with (state / "recognition.lock").open("a") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed.append("free")
                fcntl.flock(probe, fcntl.LOCK_UN)
            except BlockingIOError:
                observed.append("held")
        return [Word(1, 2, "hello", 0.9)], []

    monkeypatch.setattr(inference, "transcribe_bounded", recognize)
    monkeypatch.setattr(inference, "transcribe_windows", recognize)
    monkeypatch.setattr(inference, "release_model", lambda: released.append(True))
    settings = Settings(device="cuda")
    lock = lambda: recognition_slot(state)  # noqa: E731
    first = processor._recognized_words(
        media, tmp_path / "audio.wav", settings, state, tmp_path / "models", inference.transcribe, windows,
        recognition_lock=lock,
    )
    assert observed == ["held"] and released == [True] and not first[2]
    # A cached transcript is read without waiting for the GPU.
    with recognition_slot(state):
        again = processor._recognized_words(
            media, tmp_path / "audio.wav", settings, state, tmp_path / "models", inference.transcribe, windows,
            recognition_lock=lambda: pytest.fail("a cache hit must not take the recognition slot"),
        )
    assert again[2] and observed == ["held"]


@pytest.fixture
def video(tmp_path):
    import shutil
    import subprocess

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is required for media integration tests")
    path = tmp_path / "library" / "sample.mkv"
    path.parent.mkdir()
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x90:d=8", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=8", "-c:v", "mpeg4", "-c:a", "pcm_s16le",
         "-metadata:s:a:0", "language=eng", "-shortest", str(path)],
        check=True,
    )
    return path


class StopAtRecognition(Exception):
    pass


@pytest.mark.parametrize("device,take_turns", [("cpu", True), ("cuda", True), ("cuda", False)])
def test_a_job_waits_for_the_gpu_only_on_cuda_with_the_toggle_on(video, tmp_path, monkeypatch, device, take_turns):
    from crowbarr import processor
    from crowbarr.library import scan
    from crowbarr.resources import recognition_slot

    state = tmp_path / "state"
    db = Database(state / "crowbarr.db")
    settings = Settings(
        roots=[str(video.parent)], settle_seconds=0, subtitle_wait_minutes=0, min_duration_minutes=0,
        device=device, one_recognition_at_a_time=take_turns,
    )
    scan(settings, db)
    job = db.claim()
    stages = []
    original_update = db.update

    def update(job_id, **values):
        if "stage" in values:
            stages.append(values["stage"])
        return original_update(job_id, **values)

    monkeypatch.setattr(db, "update", update)
    entered = []

    def recognized(*args, recognition_lock, **kwargs):
        with recognition_lock():
            entered.append(stages[-1])
        raise StopAtRecognition

    monkeypatch.setattr(processor, "_recognized_words", recognized)
    # The language check runs first and would download a model; it is not under test.
    from crowbarr import inference

    monkeypatch.setattr(
        inference, "verify_audio_language", lambda *args: {"language": "en", "samples": [], "model": "stub"}
    )
    release = threading.Event()
    holding = threading.Event()

    def other_job():
        with recognition_slot(state):
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=other_job)
    holder.start()
    holding.wait(5)
    worker = threading.Thread(target=lambda: pytest.raises(StopAtRecognition, processor.process, job, settings, state, db))
    worker.start()
    try:
        if device == "cuda" and take_turns:
            assert wait_for(lambda: "Waiting for the GPU; another job is recognizing speech" in stages)
            assert not entered
        else:
            assert wait_for(lambda: entered)
    finally:
        release.set()
        holder.join(5)
        worker.join(30)
    assert entered and entered[0] != "Waiting for the GPU; another job is recognizing speech"
    if not (device == "cuda" and take_turns):
        assert "Waiting for the GPU; another job is recognizing speech" not in stages
