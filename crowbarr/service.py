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

from .config import ConfigStore, Settings, atomic_write
from .db import Database, StaleJob
from .integrations import deliver_plex, plex_activity, refresh_plex
from .library import scan
from .media import ReviewRequired
from .resources import gate, quiet
from .resources import snapshot as resource_snapshot

log = logging.getLogger("crowbarr")


def run_job(directory: str, job: dict, settings_data: dict) -> None:
    os.setsid()  # Include FFmpeg descendants when a job is stopped or times out.
    from .processor import process

    settings = Settings.model_validate(settings_data)
    if job.get("origin", "backlog") not in {"manual", "import"}:
        os.nice(10)
        import subprocess

        try:
            subprocess.run(["ionice", "-c", "3", "-p", str(os.getpid())], capture_output=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            pass
    db = Database(Path(directory) / "crowbarr.db")
    db.bind_worker(job)
    try:
        result = process(job, settings, Path(directory), db)
        if settings.plex.url and result.get("output") and result["state"] == "completed":
            report = json.loads(result.get("report") or "{}")
            try:
                report["plex_delivery"] = deliver_plex(
                    settings.plex, job["media"], result["output"], settings.plex_mappings
                )
            except Exception as error:
                report["plex_delivery"] = {"state": "pending", "reason": type(error).__name__}
            result["report"] = json.dumps(report)
        if settings.plex.url and (json.loads(result.get("report") or "{}").get("retired_output")):
            db.notice("plex-refresh", "Subtitles updated; Plex refresh pending.")
        db.update(job["id"], **result)
        if result.get("report"):
            with db.connect():
                atomic_write(Path(directory) / "reports" / f"{job['id']}.json", result["report"])
    except StaleJob:
        return
    except ReviewRequired as error:
        try:
            db.update(job["id"], state="review", stage="Needs attention", error=str(error))
        except StaleJob:
            pass
    except Exception as error:
        # Avoid exception payloads that may include credentials or external request URLs.
        name = type(error).__name__
        # The stored message stays generic, but an operator needs the traceback to act
        # on a failure at all. It goes to the log, which is not rendered in the UI.
        log.error("Job %s failed (%s)", job["id"], name, exc_info=True)
        retry = job["attempts"] < settings.max_attempts
        try:
            db.update(
                job["id"],
                state="retry" if retry else "failed",
                stage="",
                error=f"{name}: processing failed. Check model availability, resources, and media access.",
                ready=time.time() + min(3600, 60 * 2 ** job["attempts"]),
                origin="retry",
                priority=40,
            )
        except StaleJob:
            pass


class Service:
    def __init__(self, store: ConfigStore, db: Database):
        self.store, self.db = store, db
        self.stop_event, self.scan_event = threading.Event(), threading.Event()
        self.threads: list[threading.Thread] = []
        self.lock_file = None
        self.last_scan = None
        self.wait_until = None
        self.wait_total = None
        self.scan_count = 0
        self.scan_in_progress = False
        self.resources = {}
        self.wait_reason = ""
        self.last_background_end = 0
        self.last_resource_check = 0
        self.plex_busy = False
        self.child_pid = None

    def start(self):
        self.lock_file = (self.store.directory / "service.lock").open("a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            raise RuntimeError("Another Crowbarr service is already using this data directory") from None
        self.db.recover(self.store.get().max_attempts)
        self.db.backfill_result_digests()
        from .audit import AUDIT_VERSION

        reopened = self.db.adopt_policy(AUDIT_VERSION)
        # Older releases persisted a one-time policy-change message indefinitely.
        # Keep the re-evaluation in the log and remove the stale dashboard notice.
        self.db.notice("policy", "")
        if reopened:
            log.info("Policy changed; %s media will be re-evaluated", reopened)
        self.db.invalidate_sync()
        shutil.rmtree(self.store.directory / "work", ignore_errors=True)
        for target in (self.scanner, self.worker):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self, timeout: float = 20):
        self.stop_event.set()
        self.scan_event.set()
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):
            child_pid = self.child_pid
            if child_pid is not None:
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            log.warning("Shutdown deadline reached; retaining data lock until background threads exit")
            # Slow filesystem/network operations cannot be cancelled in a Python thread.
            # Do not let a second service use the data while those operations still run.
            def release_when_stopped():
                for thread in self.threads:
                    thread.join()
                if self.lock_file:
                    self.lock_file.close()

            threading.Thread(target=release_when_stopped, daemon=True).start()
        elif self.lock_file:
            self.lock_file.close()

    def scanner(self):
        while not self.stop_event.is_set():
            self.scan_event.clear()
            self.scan_in_progress = True
            try:
                self.scan_count = scan(self.store.get(), self.db)
                # A manager-backed scan used to leave the last top-level failure on
                # the dashboard forever, even after later reconciliations succeeded.
                self.db.notice("scan", "")
                if self.stop_event.is_set():
                    break
                self.db.prune()
                self.last_scan = time.time()
                settings = self.store.get()
                if settings.plex.url:
                    with self.db.connect() as connection:
                        pending = [
                            dict(row)
                            for row in connection.execute(
                                "SELECT * FROM jobs WHERE state='completed' AND output IS NOT NULL AND json_extract(report,'$.plex_delivery.state')='pending' ORDER BY updated LIMIT 5"
                            )
                        ]
                    for job in pending:
                        if self.stop_event.is_set():
                            break
                        report = json.loads(job["report"])
                        attempts = report["plex_delivery"].get("attempts", 0)
                        if attempts >= 5:
                            continue
                        try:
                            delivery = deliver_plex(
                                settings.plex, job["media"], job["output"], settings.plex_mappings, timeout=10
                            )
                        except Exception as error:
                            delivery = {"state": "pending", "reason": type(error).__name__}
                        delivery["attempts"] = attempts + 1
                        report["plex_delivery"] = delivery
                        self.db.update(job["id"], report=json.dumps(report))
                        atomic_write(
                            self.store.directory / "reports" / f"{job['id']}.json", json.dumps(report)
                        )
                for notice in self.db.snapshot()["notices"]:
                    if notice["name"] == "plex-refresh" and self.store.get().plex.url:
                        try:
                            refresh_plex(self.store.get().plex)
                            self.db.clear_notice("plex-refresh", notice["updated"])
                        except Exception:
                            # Durable notice is retried by the next reconciliation, including after restarts.
                            log.warning("Plex refresh failed; retrying at the next library check")
            except Exception as error:
                # The dashboard stays deliberately terse because exception messages
                # can contain media paths or upstream details. Server logs retain the
                # traceback and concrete SQLite reason needed to diagnose a repeat.
                log.exception("Library scan failed; Crowbarr will retry")
                self.db.notice("scan", f"Library scan failed ({type(error).__name__}); Crowbarr will retry.")
            finally:
                self.scan_in_progress = False
            self.scan_event.wait(self.store.get().scan_seconds)

    def worker(self):
        context = multiprocessing.get_context("spawn")
        while not self.stop_event.wait(1):
            settings = self.store.get()
            if time.monotonic() - self.last_resource_check > 10:
                self.resources = resource_snapshot()
                self.last_resource_check = time.monotonic()
                try:
                    self.plex_busy = bool(plex_activity(settings.plex)) if settings.plex.url else False
                except Exception:
                    self.plex_busy = settings.defer_during_plex
            self.wait_reason = "Paused by user" if settings.paused else gate(settings, self.resources)
            if self.wait_reason:
                continue
            background_reason = (
                "Plex is playing; background work deferred"
                if settings.defer_during_plex and self.plex_busy
                else "Quiet hours"
                if quiet(settings)
                else "CPU is busy"
                if self.resources.get("cpu_load", 0) > settings.max_cpu_load
                else "Hourly background budget reached"
                if self.db.background_seconds() >= settings.background_budget_minutes * 60
                else "Background cooldown"
                if time.time() - self.last_background_end < settings.backlog_cooldown_seconds
                else ""
            )
            self.wait_reason = background_reason
            # Only the cooldown has a knowable end; other gates clear when the machine does.
            self.wait_until = (
                self.last_background_end + settings.backlog_cooldown_seconds
                if background_reason == "Background cooldown"
                else None
            )
            self.wait_total = settings.backlog_cooldown_seconds if self.wait_until else None
            job = self.db.claim(
                settings.providers(),
                settings.discovery_fingerprint(),
                background_allowed=not background_reason,
            )
            if not job:
                continue
            if self.stop_event.is_set():
                self.db.update(
                    job["id"], expected_generation=job["generation"], state="retry", stage="",
                    attempts=max(0, job["attempts"] - 1), ready=time.time(),
                )
                break
            child = context.Process(
                target=run_job, args=(str(self.store.directory), job, settings.model_dump())
            )
            try:
                child.start()
                self.child_pid = child.pid
            except Exception:
                self.db.update(
                    job["id"], expected_generation=job["generation"],
                    state="failed", stage="", error="Could not start the inference process"
                )
                continue
            started = time.monotonic()
            wall_started = time.time()
            self.wait_reason = ""
            interrupted = None
            while child.is_alive():
                child.join(timeout=1)
                current = self.store.get()
                latest = self.db.get(job["id"])
                if not latest or latest["generation"] != job["generation"]:
                    interrupted = "Job request replaced"
                elif latest.get("cancel_requested"):
                    interrupted = "Cancelled by user"
                elif self.stop_event.is_set():
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
                if not interrupted and time.monotonic() - self.last_resource_check > 10:
                    self.resources = resource_snapshot()
                    self.last_resource_check = time.monotonic()
                if interrupted:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        child.terminate()
                    child.join(timeout=10)
                    # The leader may exit before an FFmpeg descendant does.
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        if child.is_alive():
                            child.kill()
                    child.join(timeout=5)
                    break
            latest = self.db.get(job["id"])
            if latest and latest["generation"] == job["generation"] and latest["state"] == "processing":
                administrative = interrupted in {"Service stopping", "Processing settings changed"}
                attempts = max(0, job["attempts"] - 1) if administrative else job["attempts"]
                retry = attempts < settings.max_attempts
                self.db.update(
                    job["id"],
                    expected_generation=job["generation"],
                    state="superseded"
                    if interrupted == "Media is no longer eligible in the arr library"
                    else "skipped" if latest.get("library_blocked")
                    else "cancelled" if interrupted == "Cancelled by user"
                    else ("retry" if retry else "failed"),
                    stage="Library preference" if latest.get("library_blocked") else "",
                    error=latest.get("library_blocked") or interrupted or "Inference process exited unexpectedly; check available memory",
                    ready=time.time() + (0 if interrupted in {"Service stopping", "Processing settings changed"} else 120),
                    origin=job.get("origin", "backlog") if interrupted in {"Service stopping", "Processing settings changed"} else "retry",
                    priority=job.get("priority", 0) if interrupted in {"Service stopping", "Processing settings changed"} else 40,
                    attempts=attempts,
                    started=None,
                    cached=None,
                )
            elif latest and latest["state"] == "completed":
                self.scan_event.set()
            # A job discarded because its inputs moved on did no inference, so it must not
            # spend the background budget or start a cooldown. Otherwise a library-wide
            # re-sign leaves the worker idling between jobs that never ran.
            did_work = not (latest and latest["state"] == "superseded")
            if did_work and job.get("origin", "backlog") not in {"manual", "import"}:
                self.last_background_end = time.time()
                self.db.record_usage(wall_started, time.monotonic() - started)
            self.child_pid = None
            child.close()
            shutil.rmtree(self.store.directory / "work", ignore_errors=True)
