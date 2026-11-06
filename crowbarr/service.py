from __future__ import annotations

import fcntl
import json
import logging
import multiprocessing
import os
import shutil
import signal
import sqlite3
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

# One supervisor thread per possible slot (Settings.max_concurrent_jobs allows up to 4);
# slots above the effective limit idle, so the limit can change without a restart.
WORKER_SLOTS = 4
# A job's GPU memory appears in nvidia-smi only once its model loads, after the CPU
# stages. Until then admission reserves this much for it, so a reading taken between
# a start and its model load cannot admit a job the card will not have room for.
STARTING_JOB_VRAM_MB = 1200
STARTING_JOB_SECONDS = 180


def fallback_marker(directory: Path, job_id: int) -> Path:
    """Flag file a job drops when recognition fell back to the CPU.

    It carries the job's work prefix, so the job's own cleanup removes it.
    """
    return directory / "work" / f"{job_id}-cpu-fallback"


def run_job(directory: str, job: dict, settings_data: dict) -> None:
    os.setsid()  # Include FFmpeg descendants when a job is stopped or times out.
    from . import inference
    from .processor import process

    marker = fallback_marker(Path(directory), job["id"])

    def report_cpu_fallback() -> None:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()

    inference.cpu_fallback_hook = report_cpu_fallback

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
        # Most recent background job end across all slots; each slot cools down on its own.
        self.last_background_end = 0
        self.slot_background_end: dict[int, float] = {}
        self.last_resource_check = 0
        self.plex_busy = False
        self.plex_unreachable = False
        self.child_pids: set[int] = set()
        # Admission (gates, claim, bookkeeping) runs under this lock so two slots never
        # judge free memory from the same reading. Supervision runs outside it.
        self.state_lock = threading.Lock()
        self.resource_lock = threading.Lock()
        # job id -> {"slot", "started" (monotonic), "cpu_fallback"}
        self.running: dict[int, dict] = {}
        # slot -> (reason, wait_until, wait_total) for idle slots that may claim
        self.slot_waits: dict[int, tuple[str, float | None, float | None]] = {}
        self.cuda_available: bool | None = None
        self.cuda_checked = float("-inf")

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
        # No job runs yet, so everything left in the work folder is from a previous run.
        shutil.rmtree(self.store.directory / "work", ignore_errors=True)
        targets = [(self.scanner, ())] + [(self.worker, (slot,)) for slot in range(WORKER_SLOTS)]
        for target, args in targets:
            thread = threading.Thread(target=target, args=args, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self, timeout: float = 20):
        self.stop_event.set()
        self.scan_event.set()
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):
            with self.state_lock:
                children = list(self.child_pids)
            for child_pid in children:
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
                try:
                    self.db.notice("scan", f"Library scan failed ({type(error).__name__}); Crowbarr will retry.")
                except Exception:
                    # Recording the notice can hit the same lock that failed the scan.
                    # Losing the notice is acceptable; losing the scanner thread is not.
                    log.warning("Could not record the scan failure notice; Crowbarr will retry")
            finally:
                self.scan_in_progress = False
            self.scan_event.wait(self.store.get().scan_seconds)

    def plex_reason(self) -> str:
        if self.plex_unreachable:
            return "Cannot reach Plex; background work deferred"
        return "Plex is playing; background work deferred"

    # ----- parallel admission -------------------------------------------------------

    def parallel_limit(self, settings: Settings) -> int:
        """How many jobs may run now: the saved value only on a GPU that works."""
        if settings.device != "cuda" or settings.max_concurrent_jobs <= 1:
            return 1
        # The probe spawns a process that loads the CUDA runtime, so a definite answer
        # is kept for an hour; an unknown one ("checking") is asked again in a minute.
        age = time.monotonic() - self.cuda_checked
        if age > (3600 if isinstance(self.cuda_available, bool) else 60):
            from .capabilities import runtime_capabilities

            try:
                self.cuda_available = runtime_capabilities(block=False).get("cuda", {}).get("available")
            except Exception:
                self.cuda_available = None
            self.cuda_checked = time.monotonic()
        return settings.parallel_jobs(self.cuda_available)

    def refresh_resources(self, settings: Settings | None = None, max_age: float = 10) -> None:
        """Re-read host telemetry (and Plex, when given settings) if older than max_age."""
        if time.monotonic() - self.last_resource_check <= max_age:
            return
        with self.resource_lock:
            if time.monotonic() - self.last_resource_check <= max_age:
                return
            self.resources = resource_snapshot()
            if settings is not None:
                try:
                    self.plex_busy = bool(plex_activity(settings.plex)) if settings.plex.url else False
                    self.plex_unreachable = False
                except Exception:
                    # Unknown is treated as busy, so a Plex outage never slows playback.
                    self.plex_busy = settings.defer_during_plex
                    self.plex_unreachable = True
            self.last_resource_check = time.monotonic()

    def _admission_resources(self) -> dict:
        """Telemetry with memory set aside for running jobs whose model has not loaded."""
        metrics = dict(self.resources)
        free = metrics.get("vram_free_mb")
        if free is not None:
            now = time.monotonic()
            starting = sum(
                1
                for info in self.running.values()
                if not info["cpu_fallback"] and now - info["started"] < STARTING_JOB_SECONDS
            )
            metrics["vram_free_mb"] = free - starting * STARTING_JOB_VRAM_MB
        metrics.setdefault("ram_available_mb", None)
        metrics.setdefault("vram_free_mb", None)
        return metrics

    def _note_cpu_fallbacks(self) -> bool:
        for job_id, info in self.running.items():
            if not info["cpu_fallback"] and fallback_marker(self.store.directory, job_id).exists():
                info["cpu_fallback"] = True
                log.info("Job %s fell back to CPU recognition; no new job starts until it ends", job_id)
        return any(info["cpu_fallback"] for info in self.running.values())

    def _publish_wait(self, slot: int, wait: tuple[str, float | None, float | None] | None) -> None:
        """Show the dashboard the wait of the first idle slot, or nothing when none waits."""
        if wait is None:
            self.slot_waits.pop(slot, None)
        else:
            self.slot_waits[slot] = wait
        if self.slot_waits:
            self.wait_reason, self.wait_until, self.wait_total = self.slot_waits[min(self.slot_waits)]
        else:
            self.wait_reason, self.wait_until, self.wait_total = "", None, None

    def _admit(self, slot: int, settings: Settings, limit: int) -> dict | None:
        """Apply every gate and claim a job for this slot. Call with state_lock held."""
        if settings.paused:
            self._publish_wait(slot, ("Paused by user", None, None))
            return None
        if self.running:
            # Another job is running, so the card's free memory may have moved since the
            # last periodic reading; judge the second job by a current one.
            self.refresh_resources(max_age=3)
        reason = gate(settings, self._admission_resources())
        if not reason and self.running and self._note_cpu_fallbacks():
            reason = "A job fell back to CPU processing; the next job waits for it to finish"
        if reason:
            self._publish_wait(slot, (reason, None, None))
            return None
        last_end = self.slot_background_end.get(slot, 0)
        background_reason = (
            self.plex_reason()
            if settings.defer_during_plex and self.plex_busy
            else "Quiet hours"
            if quiet(settings)
            else "CPU is busy"
            if self.resources.get("cpu_load", 0) > settings.max_cpu_load
            else "Hourly background budget reached"
            if self.db.background_seconds() >= settings.background_budget_minutes * 60
            else "Background cooldown"
            if time.time() - last_end < settings.backlog_cooldown_seconds
            else ""
        )
        # Only the cooldown has a knowable end; other gates clear when the machine does.
        until = last_end + settings.backlog_cooldown_seconds if background_reason == "Background cooldown" else None
        self._publish_wait(
            slot, (background_reason, until, settings.backlog_cooldown_seconds if until else None)
        )
        job = self.db.claim(
            settings.providers(),
            settings.discovery_fingerprint(),
            background_allowed=not background_reason,
            max_processing=limit,
        )
        if job:
            self.running[job["id"]] = {"slot": slot, "started": time.monotonic(), "cpu_fallback": False}
            self._publish_wait(slot, None)
            # Free memory changes once this job loads its model; force a fresh reading.
            self.last_resource_check = 0
        return job

    # ----- supervision --------------------------------------------------------------

    def _clean_job_work(self, job_id: int) -> None:
        """Remove only this job's leftovers; another job may be using the work folder."""
        work = self.store.directory / "work"
        prefix = f"{job_id}-"
        try:
            entries = [entry for entry in work.iterdir() if entry.name.startswith(prefix)]
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
            except OSError:
                pass

    def worker(self, slot: int = 0):
        context = multiprocessing.get_context("spawn")
        while not self.stop_event.wait(1):
            try:
                settings = self.store.get()
                limit = self.parallel_limit(settings)
                if slot >= limit:
                    with self.state_lock:
                        self._publish_wait(slot, None)
                    continue
                self.refresh_resources(settings)
                with self.state_lock:
                    job = self._admit(slot, settings, limit)
                if not job:
                    continue
                try:
                    if self.stop_event.is_set():
                        try:
                            self.db.update(
                                job["id"], expected_generation=job["generation"], state="retry", stage="",
                                attempts=max(0, job["attempts"] - 1), ready=time.time(),
                            )
                        except Exception:
                            pass
                        break
                    self.supervise(slot, job, settings, context)
                finally:
                    with self.state_lock:
                        self.running.pop(job["id"], None)
                    self._clean_job_work(job["id"])
            except sqlite3.OperationalError as error:
                log.warning("Database is busy (%s); worker will retry", error)
            except Exception:
                log.exception("Worker encountered unexpected error; will retry")
        with self.state_lock:
            self._publish_wait(slot, None)

    def supervise(self, slot: int, job: dict, settings: Settings, context) -> None:
        """Run one claimed job in a child process and settle its row when it ends."""
        child = context.Process(
            target=run_job, args=(str(self.store.directory), job, settings.model_dump())
        )
        try:
            child.start()
        except Exception:
            try:
                self.db.update(
                    job["id"], expected_generation=job["generation"],
                    state="failed", stage="", error="Could not start the inference process"
                )
            except Exception:
                pass
            return
        with self.state_lock:
            self.child_pids.add(child.pid)
        started = time.monotonic()
        wall_started = time.time()
        interrupted = None
        try:
            while child.is_alive():
                child.join(timeout=1)
                current = self.store.get()
                try:
                    latest = self.db.get(job["id"])
                except sqlite3.OperationalError:
                    latest = None
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
                else:
                    try:
                        eligible = self.db.eligible(job["media"], current.providers(), healthy=False)
                    except sqlite3.OperationalError:
                        eligible = True
                    if not eligible:
                        interrupted = "Media is no longer eligible in the arr library"
                    elif time.monotonic() - started > settings.job_timeout_minutes * 60:
                        interrupted = "Job exceeded its configured time limit"
                if not interrupted:
                    self.refresh_resources()
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
            try:
                latest = self.db.get(job["id"])
            except sqlite3.OperationalError:
                latest = None
            if latest and latest["generation"] == job["generation"] and latest["state"] == "processing":
                administrative = interrupted in {"Service stopping", "Processing settings changed"}
                attempts = max(0, job["attempts"] - 1) if administrative else job["attempts"]
                retry = attempts < settings.max_attempts
                try:
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
                        ready=time.time() + (0 if administrative else 120),
                        origin=job.get("origin", "backlog") if administrative else "retry",
                        priority=job.get("priority", 0) if administrative else 40,
                        attempts=attempts,
                        started=None,
                        cached=None,
                    )
                except Exception:
                    log.exception("Failed to update job %s after completion", job["id"])
            elif latest and latest["state"] == "completed":
                self.scan_event.set()
            # A job discarded because its inputs moved on did no inference, so it must not
            # spend the background budget or start a cooldown. Otherwise a library-wide
            # re-sign leaves the worker idling between jobs that never ran.
            did_work = not (latest and latest["state"] == "superseded")
            if did_work and job.get("origin", "backlog") not in {"manual", "import"}:
                ended = time.time()
                with self.state_lock:
                    self.slot_background_end[slot] = ended
                    self.last_background_end = max(self.last_background_end, ended)
                try:
                    self.db.record_usage(wall_started, time.monotonic() - started)
                except Exception:
                    pass
        finally:
            if child.is_alive():
                # Only reached when supervision itself failed; never leave the job orphaned.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    child.kill()
                child.join(timeout=5)
            with self.state_lock:
                self.child_pids.discard(child.pid)
            try:
                child.close()
            except ValueError:
                pass
