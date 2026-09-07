from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Database:
    """A durable single-host queue. Transactions serialize claims across threads/processes."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, media TEXT NOT NULL, signature TEXT NOT NULL,
                    source TEXT, state TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, updated REAL NOT NULL, ready REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, error TEXT, report TEXT, output TEXT,
                    UNIQUE(media, signature)
                );
                CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(state, ready);
                CREATE TABLE IF NOT EXISTS observations (
                    media TEXT PRIMARY KEY, signature TEXT NOT NULL, first_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notices (
                    name TEXT PRIMARY KEY, message TEXT NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS videos (
                    media TEXT PRIMARY KEY, revision TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    media TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
                    job_id INTEGER NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(path, sha256)
                );
                CREATE TABLE IF NOT EXISTS managed_media (
                    provider TEXT NOT NULL, file_id INTEGER NOT NULL, item_id INTEGER NOT NULL,
                    path TEXT NOT NULL, remote_path TEXT NOT NULL, title TEXT NOT NULL,
                    PRIMARY KEY(provider, file_id)
                );
                CREATE INDEX IF NOT EXISTS managed_path ON managed_media(path);
                CREATE TABLE IF NOT EXISTS provider_sync (
                    provider TEXT PRIMARY KEY, last_attempt REAL NOT NULL, last_success REAL,
                    healthy INTEGER NOT NULL, file_count INTEGER NOT NULL, error TEXT,
                    generation TEXT NOT NULL DEFAULT ''
                );
            """)
            if "generation" not in {row[1] for row in db.execute("PRAGMA table_info(provider_sync)")}:
                db.execute("ALTER TABLE provider_sync ADD COLUMN generation TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def notice(self, name: str, message: str) -> None:
        with self.connect() as db:
            if message:
                db.execute("INSERT OR REPLACE INTO notices VALUES (?,?,?)", (name, message, time.time()))
            else:
                db.execute("DELETE FROM notices WHERE name=?", (name,))

    def clear_notice(self, name: str, updated: float) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM notices WHERE name=? AND updated=?", (name, updated))

    def observe(self, media: str, signature: str, now: float) -> float:
        with self.connect() as db:
            db.execute(
                "INSERT INTO observations VALUES (?,?,?) ON CONFLICT(media) DO UPDATE SET "
                "signature=excluded.signature, first_seen=excluded.first_seen "
                "WHERE observations.signature != excluded.signature",
                (media, signature, now),
            )
            return db.execute("SELECT first_seen FROM observations WHERE media=?", (media,)).fetchone()[0]

    def video_revision(self, media: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT revision FROM videos WHERE media=?", (media,)).fetchone()
            return row[0] if row else None

    def set_video_revision(self, media: str, revision: str) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO videos VALUES (?,?)", (media, revision))

    def published(self, media: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE media=? AND output IS NOT NULL AND report IS NOT NULL ORDER BY updated DESC LIMIT 1",
                (media,),
            ).fetchone()
            return dict(row) if row else None

    def enqueue(self, media: str, signature: str, source: str | None, ready: float) -> int:
        now = time.time()
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET state='superseded',updated=? WHERE media=? AND signature!=? "
                "AND state IN ('waiting','queued','retry')",
                (now, media, signature),
            )
            db.execute(
                "INSERT OR IGNORE INTO jobs(media,signature,source,state,created,updated,ready) "
                "VALUES (?,?,?,?,?,?,?)",
                (media, signature, source, "waiting" if ready > now else "queued", now, now, ready),
            )
            db.execute(
                "UPDATE jobs SET state=?,ready=?,updated=?,attempts=0,error=NULL "
                "WHERE media=? AND signature=? AND state='superseded'",
                ("waiting" if ready > now else "queued", ready, now, media, signature),
            )
            return db.execute(
                "SELECT id FROM jobs WHERE media=? AND signature=?", (media, signature)
            ).fetchone()[0]

    def replace_catalog(self, provider: str, records: list[dict], generation: str = "") -> None:
        now = time.time()
        with self.connect() as db:
            db.execute("DELETE FROM managed_media WHERE provider=?", (provider,))
            db.executemany(
                "INSERT INTO managed_media VALUES (?,?,?,?,?,?)",
                [
                    (provider, r["file_id"], r["item_id"], r["path"], r["remote_path"], r["title"])
                    for r in records
                ],
            )
            db.execute(
                "INSERT OR REPLACE INTO provider_sync (provider,last_attempt,last_success,healthy,file_count,error,generation) VALUES (?,?,?,?,?,NULL,?)",
                (provider, now, now, 1, len(records), generation),
            )

    def sync_failed(self, provider: str, error: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO provider_sync (provider,last_attempt,last_success,healthy,file_count,error) VALUES (?,?,NULL,0,0,?) ON CONFLICT(provider) DO UPDATE SET "
                "last_attempt=excluded.last_attempt,healthy=0,error=excluded.error",
                (provider, time.time(), error),
            )

    def invalidate_sync(self) -> None:
        with self.connect() as db:
            db.execute("UPDATE provider_sync SET healthy=0")

    def eligible(self, media: str, providers: list[str], healthy: bool = True) -> bool:
        if not providers:
            return True
        marks = ",".join("?" for _ in providers)
        with self.connect() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM managed_media m JOIN provider_sync s ON m.provider=s.provider "
                    f"WHERE m.path=? AND m.provider IN ({marks}) "
                    + ("AND s.healthy=1 " if healthy else "")
                    + "LIMIT 1",
                    (media, *providers),
                ).fetchone()
            )

    def discard_unmanaged(self, providers: list[str]) -> None:
        if not providers:
            return
        marks = ",".join("?" for _ in providers)
        with self.connect() as db:
            count = db.execute(
                f"SELECT COUNT(*) FROM provider_sync WHERE provider IN ({marks}) AND healthy=1", providers
            ).fetchone()[0]
            if count != len(providers):
                return  # A failed or incomplete catalog never erases existing eligibility/history.
            db.execute(
                "UPDATE jobs SET state='superseded',stage='',updated=?,error='No longer eligible in the arr library' "
                "WHERE state IN ('waiting','queued','retry') AND NOT EXISTS "
                f"(SELECT 1 FROM managed_media m WHERE m.path=jobs.media AND m.provider IN ({marks}))",
                (time.time(), *providers),
            )

    def claim(self, providers: list[str] | None = None, generation: str | None = None) -> dict | None:
        now = time.time()
        scope, parameters = "", [now]
        if providers:
            marks = ",".join("?" for _ in providers)
            scope = (
                "AND EXISTS (SELECT 1 FROM managed_media m JOIN provider_sync s ON m.provider=s.provider "
                f"WHERE m.path=jobs.media AND m.provider IN ({marks}) AND s.healthy=1 "
                + ("AND s.generation=? " if generation is not None else "")
                + ") "
            )
            parameters.extend(providers)
            if generation is not None:
                parameters.append(generation)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE state IN ('waiting','queued','retry') AND ready<=? "
                + scope
                + "ORDER BY ready,id LIMIT 1",
                parameters,
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE jobs SET state='processing',stage='Preparing audio',attempts=attempts+1,"
                "updated=?,error=NULL WHERE id=?",
                (now, row["id"]),
            )
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def register_artifact(self, media: str, path: str, digest: str, job_id: int) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?,?)",
                (media, path, digest, job_id, time.time()),
            )

    def owns_output(self, media: str, path: str, digest: str) -> bool:
        with self.connect() as db:
            if db.execute(
                "SELECT 1 FROM artifacts WHERE media=? AND path=? AND sha256=?", (media, path, digest)
            ).fetchone():
                return True
            return any(
                json.loads(row[0] or "{}").get("output_sha256") == digest
                for row in db.execute("SELECT report FROM jobs WHERE media=? AND output=?", (media, path))
            )

    def update(self, job_id: int, **values) -> None:
        allowed = {"state", "stage", "ready", "error", "report", "output", "attempts"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid job update")
        values["updated"] = time.time()
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                (*values.values(), job_id),
            )

    def recover(self) -> None:
        # Called only after the service acquires the exclusive process lock.
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET state='retry',stage='',ready=?,updated=?,"
                "error='Service restarted; job will resume' WHERE state='processing'",
                (time.time(), time.time()),
            )

    def get(self, job_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def retry(self, job_id: int) -> bool:
        with self.connect() as db:
            result = db.execute(
                "UPDATE jobs SET state='queued',ready=?,updated=?,attempts=0,error=NULL "
                "WHERE id=? AND state IN ('failed','review')",
                (time.time(), time.time(), job_id),
            )
            return bool(result.rowcount)

    def snapshot(self) -> dict:
        with self.connect() as db:
            jobs = [
                dict(row)
                for row in db.execute(
                    "SELECT jobs.*, (SELECT provider FROM managed_media "
                    "WHERE path=jobs.media LIMIT 1) AS manager FROM jobs ORDER BY updated DESC LIMIT 100"
                )
            ]
            counts = dict(db.execute("SELECT state,COUNT(*) FROM jobs GROUP BY state"))
            notices = [dict(row) for row in db.execute("SELECT * FROM notices ORDER BY name")]
            integrations = [
                dict(row)
                for row in db.execute(
                    "SELECT provider,last_attempt,last_success,healthy,file_count,error FROM provider_sync ORDER BY provider"
                )
            ]
        for job in jobs:
            job["title"] = Path(job["media"]).stem
            job["report"] = json.loads(job["report"]) if job["report"] else None
        return {"jobs": jobs, "counts": counts, "notices": notices, "integrations": integrations}
