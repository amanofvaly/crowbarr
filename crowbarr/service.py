from __future__ import annotations

import fcntl
import json
import logging
import multiprocessing
import os
import shutil
import signal
import threading
import time
from pathlib import Path

from .config import ConfigStore, Settings
from .db import Database
from .integrations import refresh_plex
from .library import scan
from .media import ReviewRequired

log = logging.getLogger("crowbarr")


def run_job(directory: str, job: dict, settings_data: dict) -> None:
    os.setsid()  # Include FFmpeg descendants when a job is stopped or times out.
    from .processor import process

    settings = Settings.model_validate(settings_data)
    db = Database(Path(directory) / "crowbarr.db")
    try:
        result = process(job, settings, Path(directory), db)
        if settings.plex.url and (
            result["state"] == "completed" or json.loads(result.get("report") or "{}").get("retired_output")
        ):
            db.notice("plex-refresh", "Subtitles updated; Plex refresh pending.")
        db.update(job["id"], **result)
    except ReviewRequired as error:
        db.update(job["id"], state="review", stage="Needs attention", error=str(error))
    except Exception as error:
        # Avoid exception payloads that may include credentials or external request URLs.
        name = type(error).__name__
        log.error("Job %s failed (%s)", job["id"], name)
        retry = job["attempts"] < settings.max_attempts
        db.update(
            job["id"],
            state="retry" if retry else "failed",
            stage="",
            error=f"{name}: processing failed. Check model availability, resources, and media access.",
            ready=time.time() + min(3600, 60 * 2 ** job["attempts"]),
        )


class Service:
    def __init__(self, store: ConfigStore, db: Database):
        self.store, self.db = store, db
        self.stop_event, self.scan_event = threading.Event(), threading.Event()
        self.threads: list[threading.Thread] = []
        self.lock_file = None
        self.last_scan = None
        self.scan_count = 0

    def start(self):
        self.lock_file = (self.store.directory / "service.lock").open("a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            raise RuntimeError("Another Crowbarr service is already using this data directory") from None
        self.db.recover()
        self.db.invalidate_sync()
        shutil.rmtree(self.store.directory / "work", ignore_errors=True)
        for target in (self.scanner, self.worker):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stop_event.set()
        self.scan_event.set()
        for thread in self.threads:
            thread.join(timeout=25)
        if self.lock_file:
            self.lock_file.close()

    def scanner(self):
        while not self.stop_event.is_set():
            self.scan_event.clear()
            try:
                self.scan_count = scan(self.store.get(), self.db)
                self.last_scan = time.time()
                for notice in self.db.snapshot()["notices"]:
                    if notice["name"] == "plex-refresh" and self.store.get().plex.url:
                        try:
                            refresh_plex(self.store.get().plex)
                            self.db.clear_notice("plex-refresh", notice["updated"])
                        except Exception:
                            # Durable notice is retried by the next reconciliation, including after restarts.
                            log.warning("Plex refresh failed; retrying at the next library check")
            except Exception as error:
                self.db.notice("scan", f"Library scan failed ({type(error).__name__}); Crowbarr will retry.")
            self.scan_event.wait(self.store.get().scan_seconds)

    def worker(self):
        context = multiprocessing.get_context("spawn")
        while not self.stop_event.wait(1):
            settings = self.store.get()
            if settings.paused:
                continue
            job = self.db.claim(settings.providers(), settings.discovery_fingerprint())
            if not job:
                continue
            child = context.Process(
                target=run_job, args=(str(self.store.directory), job, settings.model_dump())
            )
            try:
                child.start()
            except Exception:
                self.db.update(
                    job["id"], state="failed", stage="", error="Could not start the inference process"
                )
                continue
            started = time.monotonic()
            interrupted = None
            while child.is_alive():
                child.join(timeout=1)
                current = self.store.get()
                if self.stop_event.is_set():
                    interrupted = "Service stopping"
                elif (
                    current.fingerprint() != settings.fingerprint()
                    or current.discovery_fingerprint() != settings.discovery_fingerprint()
                ):
                    interrupted = "Processing settings changed"
                elif not self.db.eligible(job["media"], current.providers(), healthy=False):
                    interrupted = "Media is no longer eligible in the arr library"
                elif time.monotonic() - started > settings.job_timeout_minutes * 60:
                    interrupted = "Job exceeded its configured time limit"
                if interrupted:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        child.terminate()
                    child.join(timeout=10)
                    if child.is_alive():
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            child.kill()
                        child.join(timeout=5)
                    break
            latest = self.db.get(job["id"])
            if latest and latest["state"] == "processing":
                retry = job["attempts"] < settings.max_attempts
                self.db.update(
                    job["id"],
                    state="superseded"
                    if interrupted == "Media is no longer eligible in the arr library"
                    else ("retry" if retry else "failed"),
                    stage="",
                    error=interrupted or "Inference process exited unexpectedly; check available memory",
                    ready=time.time() + 120,
                )
            elif latest and latest["state"] == "completed":
                self.scan_event.set()
            child.close()
            shutil.rmtree(self.store.directory / "work", ignore_errors=True)
