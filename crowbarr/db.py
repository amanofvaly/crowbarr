from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class StaleJob(RuntimeError):
    """The worker's request has been replaced since it was claimed."""


class Database:
    """A durable single-host queue. Transactions serialize claims across threads/processes."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.worker_job = None
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
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            for name, definition in {
                "origin": "TEXT NOT NULL DEFAULT 'backlog'",
                "priority": "INTEGER NOT NULL DEFAULT 0",
                "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
                "directive": "TEXT NOT NULL DEFAULT ''",
                "progress_current": "REAL",
                "progress_total": "REAL",
                "cached": "INTEGER",
                "started": "REAL",
                "generation": "INTEGER NOT NULL DEFAULT 0",
                "library_order": "TEXT NOT NULL DEFAULT ''",
                "library_blocked": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            db.execute(
                "CREATE TABLE IF NOT EXISTS resource_usage (started REAL NOT NULL, seconds REAL NOT NULL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS library_metadata (path TEXT PRIMARY KEY, metadata TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS audio_metadata (path TEXT PRIMARY KEY, revision TEXT NOT NULL, languages TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS library_rules (scope TEXT NOT NULL, target TEXT NOT NULL, decision TEXT NOT NULL, PRIMARY KEY(scope,target))")
            db.execute(
                "CREATE TABLE IF NOT EXISTS results ("
                " media TEXT NOT NULL, signature TEXT NOT NULL, state TEXT NOT NULL,"
                " stage TEXT NOT NULL DEFAULT '', error TEXT, report TEXT, output TEXT,"
                " output_sha256 TEXT, finished REAL NOT NULL,"
                " PRIMARY KEY(media,signature))"
            )
            result_columns = {row[1] for row in db.execute("PRAGMA table_info(results)")}
            if "output_sha256" not in result_columns:
                db.execute("ALTER TABLE results ADD COLUMN output_sha256 TEXT")
            # `jobs` holds what is true now; `history` holds what happened. Keeping both
            # in one table is what made a re-queued file erase its own past.
            db.execute(
                "CREATE TABLE IF NOT EXISTS history ("
                " id INTEGER PRIMARY KEY, job_id INTEGER, media TEXT NOT NULL, state TEXT NOT NULL,"
                " error TEXT, output TEXT, finished REAL NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS history_finished ON history(finished DESC)")
            if "job_id" not in {row[1] for row in db.execute("PRAGMA table_info(history)")}:
                db.execute("ALTER TABLE history ADD COLUMN job_id INTEGER")
            # One row per media file, holding its current state -- not one row per
            # (file, inputs) pair. A row per revision turns a state question into an
            # ever-growing log: files get counted twice, finished verdicts are buried
            # under re-queued duplicates, and ids climb without bound.
            obsolete = db.execute(
                "SELECT * FROM jobs WHERE id NOT IN (SELECT MAX(id) FROM jobs GROUP BY media)"
            ).fetchall()
            for row in obsolete:
                try:
                    report = json.loads(row["report"] or "{}")
                    digest = report.get("output_sha256") if isinstance(report, dict) else None
                except (ValueError, TypeError):
                    digest = None
                if row["output"] and isinstance(digest, str) and digest:
                    db.execute(
                        "INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?)",
                        (row["media"], row["output"], digest, row["id"], row["updated"]),
                    )
                if row["state"] in {"completed", "unchanged", "review", "failed", "skipped"}:
                    db.execute(
                        "INSERT INTO history(job_id,media,state,error,output,finished) "
                        "SELECT ?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM history WHERE job_id=?)",
                        (row["id"], row["media"], row["state"], row["error"], row["output"],
                         row["updated"], row["id"]),
                    )
                # A removed revision cannot supply a current detail report.
                db.execute("UPDATE history SET job_id=NULL WHERE job_id=?", (row["id"],))
                db.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_media ON jobs(media)")
            db.execute(
                "INSERT OR IGNORE INTO results(media,signature,state,stage,error,report,output,finished) "
                "SELECT media,signature,state,stage,error,report,output,updated FROM jobs "
                "WHERE state IN ('completed','unchanged','review','skipped') AND stage<>'Library preference'"
            )
            if "generation" not in {row[1] for row in db.execute("PRAGMA table_info(provider_sync)")}:
                db.execute("ALTER TABLE provider_sync ADD COLUMN generation TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                if self.worker_job is not None:
                    db.execute("BEGIN IMMEDIATE")
                    if not db.execute(
                        "SELECT 1 FROM jobs WHERE id=? AND generation=? AND library_blocked=''", self.worker_job
                    ).fetchone():
                        raise StaleJob("Job request was replaced")
                yield db
        finally:
            db.close()

    def bind_worker(self, job: dict) -> None:
        self.worker_job = (job["id"], job["generation"])

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

    def enqueue(
        self, media: str, signature: str, source: str | None, ready: float, origin: str = "backlog"
    ) -> int:
        """Record what this file needs now. One row per file, updated in place."""
        priority = {"manual": 100, "import": 80, "bazarr": 60, "retry": 40, "backlog": 0}[origin]
        pending = ("waiting", "queued", "retry", "processing")
        now = time.time()
        state = "waiting" if ready > now else "queued"
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE media=?", (media,)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO jobs(media,signature,source,state,created,updated,ready,origin,priority)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (media, signature, source, state, now, now, ready, origin, priority),
                )
                return db.execute("SELECT id FROM jobs WHERE media=?", (media,)).fetchone()[0]

            if row["signature"] == signature:
                # Nothing about this file changed. A settled verdict stands, but work
                # that was abandoned rather than decided is revived by asking again.
                if row["state"] in ("superseded", "cancelled"):
                    db.execute(
                        "UPDATE jobs SET state=?,ready=?,updated=?,stage='',attempts=0,error=NULL,"
                        "cancel_requested=0,origin=?,priority=? WHERE id=?",
                        (state, ready, now, origin, priority, row["id"]),
                    )
                    return row["id"]
                if row["state"] in pending and priority > row["priority"]:
                    db.execute(
                        "UPDATE jobs SET priority=?,origin=?,updated=? WHERE id=?",
                        (priority, origin, now, row["id"]),
                    )
                return row["id"]

            # The media or its subtitles changed, so the previous verdict no longer
            # applies. Keep a waiting user's urgency, and revisit unresolved work sooner
            # than untouched backlog.
            if row["state"] in pending and row["priority"] > priority:
                origin, priority = row["origin"], row["priority"]
            elif row["state"] in ("review", "failed") and priority < 40:
                origin, priority = "retry", 40
            # Its inputs changed, so this is a new request: restart the clock. Ageing and
            # the "requested" column both read created, and a row that lives forever would
            # otherwise report the day it was first seen and accrue endless priority.
            db.execute(
                "UPDATE jobs SET signature=?,source=?,state=?,ready=?,created=?,updated=?,origin=?,priority=?,"
                "stage='',attempts=0,error=NULL,cancel_requested=0,directive='',"
                "progress_current=NULL,progress_total=NULL,started=NULL,cached=NULL,"
                "generation=generation+1 WHERE id=?",
                (signature, source, state, ready, now, now, origin, priority, row["id"]),
            )
            return row["id"]

    def restore_results(self, candidates: list[tuple[str, str, str | None]]) -> set[str]:
        """Restore exact prior verdicts together before a scan changes queue totals."""
        restored: set[str] = set()
        now = time.time()
        matches = []
        with self.connect() as db:
            for media, signature, source in candidates:
                saved = db.execute(
                    "SELECT * FROM results WHERE media=? AND signature=?", (media, signature)
                ).fetchone()
                current_row = db.execute("SELECT * FROM jobs WHERE media=?", (media,)).fetchone()
                if not saved or not current_row:
                    continue
                if (
                    current_row["signature"] == signature
                    or current_row["state"] == "processing"
                    or current_row["origin"] == "manual"
                    or current_row["directive"]
                ):
                    continue
                if saved["state"] == "completed":
                    try:
                        output = Path(saved["output"] or "")
                        if (
                            not output.is_file()
                            or not saved["output_sha256"]
                            or hashlib.sha256(output.read_bytes()).hexdigest()
                            != saved["output_sha256"]
                        ):
                            continue
                    except (OSError, ValueError, TypeError):
                        continue
                if saved["state"] == "review":
                    try:
                        report = json.loads(saved["report"] or "{}")
                        candidate = Path(report.get("candidate", ""))
                        if (
                            not candidate.is_file()
                            or hashlib.sha256(candidate.read_bytes()).hexdigest()
                            != report.get("output_sha256")
                        ):
                            continue
                    except (OSError, ValueError, TypeError):
                        continue
                matches.append((dict(saved), dict(current_row), signature, source))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for saved, current_row, signature, source in matches:
                result = db.execute(
                    "UPDATE jobs SET signature=?,source=?,state=?,stage=?,error=?,report=?,output=?,"
                    "updated=?,attempts=0,cancel_requested=0,directive='',progress_current=NULL,"
                    "progress_total=NULL,started=NULL,cached=NULL,generation=generation+1 "
                    "WHERE id=? AND generation=? AND state<>'processing' AND origin<>'manual' AND directive=''",
                    (signature, source, saved["state"], saved["stage"], saved["error"], saved["report"],
                     saved["output"], now, current_row["id"], current_row["generation"]),
                )
                if result.rowcount:
                    restored.add(current_row["media"])
        return restored

    def restamp(self, media: str, previous: str, signature: str) -> bool:
        """Carry a settled verdict forward under new settings instead of re-running it.

        Only the row that still carries the old signature is touched, so a file whose
        media or subtitles actually changed is left for the normal re-queue.
        """
        with self.connect() as db:
            return bool(
                db.execute(
                    "UPDATE jobs SET signature=?,updated=? WHERE media=? AND signature=?",
                    (signature, time.time(), media, previous),
                ).rowcount
            )

    def request_carry_forward(self, previous: str, target: str) -> None:
        """Persist a settings transition until the matching scan completes."""
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='carry_forward'").fetchone()
            source = previous
            if row:
                try:
                    pending = json.loads(row[0])
                    if pending.get("target") == previous:
                        source = pending["source"]
                except (ValueError, TypeError, KeyError):
                    pass
            value = json.dumps({"source": source, "target": target}, sort_keys=True)
            db.execute(
                "INSERT INTO meta(key,value) VALUES('carry_forward',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (value,),
            )

    def cancel_carry_forward(self) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM meta WHERE key='carry_forward'")

    def carried_fingerprint(self, target: str) -> tuple[str, str]:
        """Return (source, token) only for the scan the request belongs to."""
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='carry_forward'").fetchone()
        if not row:
            return "", ""
        try:
            pending = json.loads(row[0])
            if pending.get("target") == target and isinstance(pending.get("source"), str):
                return pending["source"], row[0]
        except (ValueError, TypeError):
            pass
        return "", ""

    def finish_carry_forward(self, token: str) -> bool:
        """Consume only the request this scan read, never a newer replacement."""
        if not token:
            return False
        with self.connect() as db:
            return bool(
                db.execute(
                    "DELETE FROM meta WHERE key='carry_forward' AND value=?", (token,)
                ).rowcount
            )

    def backfill_result_digests(self) -> int:
        """Index pre-upgrade outputs once, without holding a write lock during I/O."""
        with self.connect() as db:
            if db.execute(
                "SELECT 1 FROM meta WHERE key='result_digest_backfill_v1'"
            ).fetchone():
                return 0
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT media,signature,report,output FROM results "
                    "WHERE state='completed' AND output_sha256 IS NULL AND output IS NOT NULL"
                )
            ]
        updates = []
        for row in rows:
            try:
                expected = json.loads(row["report"] or "{}").get("output_sha256")
                content = Path(row["output"]).read_bytes()
                if expected and hashlib.sha256(content).hexdigest() == expected:
                    updates.append((expected, row["media"], row["signature"]))
            except (OSError, ValueError, TypeError):
                continue
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "UPDATE results SET output_sha256=? WHERE media=? AND signature=?", updates
            )
            db.execute(
                "INSERT INTO meta(key,value) VALUES('result_digest_backfill_v1',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(time.time()),),
            )
        return len(updates)

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
            self.save_library_metadata(db, records)
            db.execute(
                "INSERT OR REPLACE INTO provider_sync (provider,last_attempt,last_success,healthy,file_count,error,generation) VALUES (?,?,?,?,?,NULL,?)",
                (provider, now, now, 1, len(records), generation),
            )

    @staticmethod
    def save_library_metadata(connection, records: list[dict]) -> None:
        fields = ("season", "episodes", "episode_title", "audio_languages", "poster", "year")
        connection.executemany(
            "INSERT OR REPLACE INTO library_metadata VALUES (?,?)",
            [(r["path"], json.dumps({k: r[k] for k in fields if k in r})) for r in records],
        )

    def refresh_library_policy(self) -> None:
        from .catalog import refresh_policy

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            refresh_policy(connection)

    def cache_audio(self, media: str, metadata: dict) -> dict:
        from .catalog import decision, identity, language_codes, preferences
        from .media import audio_candidates

        stat = Path(media).stat()
        languages = language_codes([c["language"] for c in audio_candidates(metadata)])
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO audio_metadata VALUES (?,?,?)",
                               (media, f"{stat.st_size}:{stat.st_mtime_ns}", json.dumps(languages)))
            row = connection.execute("SELECT * FROM managed_media WHERE path=? LIMIT 1", (media,)).fetchone()
            saved = connection.execute("SELECT metadata FROM library_metadata WHERE path=?", (media,)).fetchone()
            record = identity({**(dict(row) if row else {"path": media}),
                               **(json.loads(saved[0]) if saved else {})})
            record["audio_languages"] = languages
            return decision(record, *preferences(connection))

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

    def claim(
        self,
        providers: list[str] | None = None,
        generation: str | None = None,
        background_allowed: bool = True,
    ) -> dict | None:
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
        if not background_allowed:
            scope += "AND origin IN ('manual','import') "
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            from .catalog import refresh_policy

            refresh_policy(db)
            if db.execute("SELECT 1 FROM jobs WHERE state='processing'").fetchone():
                return None
            row = db.execute(
                "SELECT * FROM jobs WHERE state IN ('waiting','queued','retry') AND ready<=? "
                + scope
                # `ready` gates eligibility only. Ordering by it would let the Bazarr
                # wait permanently demote media that arrived without a subtitle behind
                # every file that already had one, so order by arrival instead.
                + f"ORDER BY (priority + MIN(120, CAST(({now}-created)/3600 AS INTEGER))) DESC,"
                "CASE WHEN origin='backlog' THEN library_order ELSE '' END,created,id LIMIT 1",
                parameters,
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE jobs SET state='processing',stage='Preparing audio',attempts=attempts+1,"
                "updated=?,started=?,progress_current=NULL,progress_total=NULL,cached=NULL,"
                "generation=generation+1,error=NULL WHERE id=?",
                (now, now, row["id"]),
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

    def update(self, job_id: int, *, expected_generation: int | None = None, **values) -> bool:
        allowed = {
            "state",
            "stage",
            "ready",
            "error",
            "report",
            "output",
            "attempts",
            "origin",
            "priority",
            "cancel_requested",
            "directive",
            "progress_current",
            "progress_total",
            "started",
            "cached",
        }
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid job update")
        if "stage" in values and "progress_current" not in values:
            values.update(progress_current=None, progress_total=None)
        values["updated"] = time.time()
        stale_candidates: list[Path] = []
        with self.connect() as db:
            result = db.execute(
                "UPDATE jobs SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?"
                + (" AND generation=?" if expected_generation is not None else ""),
                (*values.values(), job_id, *((expected_generation,) if expected_generation is not None else ())),
            )
            if not result.rowcount:
                return False
            if values.get("state") in ("completed", "unchanged", "review", "failed"):
                row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row:
                    db.execute(
                        "INSERT INTO history(job_id,media,state,error,output,finished)"
                        " VALUES (?,?,?,?,?,?)",
                        (job_id, row["media"], row["state"], row["error"], row["output"], values["updated"]),
                    )
                    if row["state"] in ("completed", "unchanged", "review"):
                        output_sha256 = None
                        if row["state"] == "completed" and row["output"]:
                            try:
                                report = json.loads(row["report"] or "{}")
                                content = Path(row["output"]).read_bytes()
                                digest = hashlib.sha256(content).hexdigest()
                                if digest == report.get("output_sha256"):
                                    output_sha256 = digest
                            except (OSError, ValueError, TypeError):
                                pass
                        db.execute(
                            "INSERT OR REPLACE INTO results"
                            "(media,signature,state,stage,error,report,output,output_sha256,"
                            "finished) VALUES (?,?,?,?,?,?,?,?,?)",
                            (row["media"], row["signature"], row["state"], row["stage"],
                             row["error"], row["report"], row["output"], output_sha256,
                             values["updated"]),
                        )
                        pruned = db.execute(
                            "SELECT signature,report FROM results WHERE media=? ORDER BY finished DESC "
                            "LIMIT -1 OFFSET 5",
                            (row["media"],),
                        ).fetchall()
                        candidate_directory = self.path.parent / "candidates"
                        for old in pruned:
                            try:
                                candidate = Path(json.loads(old["report"] or "{}").get("candidate", ""))
                                expected = candidate_directory / f"{job_id}-{old['signature']}.srt"
                                if candidate == expected:
                                    stale_candidates.append(candidate)
                            except (TypeError, ValueError):
                                pass
                        db.execute(
                            "DELETE FROM results WHERE media=? AND signature NOT IN "
                            "(SELECT signature FROM results WHERE media=? ORDER BY finished DESC LIMIT 5)",
                            (row["media"], row["media"]),
                        )
                    db.execute(
                        "DELETE FROM history WHERE id NOT IN "
                        "(SELECT id FROM history ORDER BY finished DESC LIMIT 5000)"
                    )
        for candidate in stale_candidates:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
        return True

    def recover(self, max_attempts: int = 3) -> None:
        # Called only after the service acquires the exclusive process lock.
        with self.connect() as db:
            db.execute(
                "INSERT INTO history(job_id,media,state,error,output,finished) "
                "SELECT id,media,'failed','Service restarted; attempt limit reached',output,? "
                "FROM jobs WHERE state='processing' AND attempts>=? AND cancel_requested=0",
                (time.time(), max_attempts),
            )
            db.execute(
                # A graceful stop already moved its job out of 'processing' and refunded the
                # attempt. Anything still 'processing' died with the service, so it must keep
                # the attempt: a job that reliably kills the container has to fail out instead
                # of restart-looping forever.
                "UPDATE jobs SET state=CASE WHEN library_blocked<>'' THEN 'skipped' WHEN cancel_requested=1 THEN 'cancelled' "
                "WHEN attempts>=? THEN 'failed' ELSE 'retry' END,"
                "stage=CASE WHEN library_blocked<>'' THEN 'Library preference' ELSE '' END,ready=?,updated=?,progress_current=NULL,progress_total=NULL,started=NULL,cached=NULL,"
                "generation=generation+1,error=CASE WHEN cancel_requested=1 THEN 'Cancelled by user' "
                "WHEN attempts>=? THEN 'Service restarted; attempt limit reached' "
                "ELSE 'Service restarted; job will resume' END WHERE state='processing'",
                (max_attempts, time.time(), time.time(), max_attempts),
            )

    def get(self, job_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def pending(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM jobs WHERE state IN ('waiting','queued','retry') ORDER BY id"
                )
            ]

    def skip_short(self, job_id: int, error: str) -> bool:
        with self.connect() as db:
            return bool(
                db.execute(
                    "UPDATE jobs SET state='skipped',stage='Short video',error=?,updated=?,"
                    "generation=generation+1,progress_current=NULL,progress_total=NULL,started=NULL "
                    "WHERE id=? AND state IN ('waiting','queued','retry')",
                    (error, time.time(), job_id),
                ).rowcount
            )

    def retry(self, job_id: int) -> bool:
        with self.connect() as db:
            result = db.execute(
                "UPDATE jobs SET state='queued',origin='manual',priority=100,cancel_requested=0,ready=?,updated=?,attempts=0,error=NULL "
                "WHERE id=? AND state IN ('failed','review','skipped')",
                (time.time(), time.time(), job_id),
            )
            return bool(result.rowcount)

    def skip(self, job_id: int) -> bool:
        """Record that a person looked at an unresolved result and chose to leave it.

        Without this the only exits from review are running the same policy again,
        which reaches the same verdict, and publishing, which endorses a subtitle the
        audit refused. A deliberate decision is a third outcome and has to be storable,
        or every upgrade asks the same settled question again.
        """
        with self.connect() as db:
            result = db.execute(
                "UPDATE jobs SET state='skipped',stage='Set aside',cancel_requested=0,updated=? "
                "WHERE id=? AND state IN ('failed','review')",
                (time.time(), job_id),
            )
            if result.rowcount:
                row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                db.execute(
                    "INSERT INTO history(job_id,media,state,error,output,finished) VALUES (?,?,?,?,?,?)",
                    (job_id, row["media"], "skipped", row["error"], row["output"], time.time()),
                )
                db.execute(
                    "INSERT OR REPLACE INTO results"
                    "(media,signature,state,stage,error,report,output,output_sha256,"
                    "finished) VALUES (?,?,?,?,?,?,?,?,?)",
                    (row["media"], row["signature"], "skipped", row["stage"], row["error"],
                     row["report"], row["output"], None, time.time()),
                )
            return bool(result.rowcount)

    def direct(self, job_id: int, directive: str) -> bool:
        """Re-open a job with an explicit instruction, whatever its previous verdict.

        A forced regeneration must run even when nothing about the media or its
        subtitles changed, so this bypasses the signature-based convergence.
        """
        with self.connect() as db:
            return bool(
                db.execute(
                    "UPDATE jobs SET directive=?,state='queued',stage='',origin='manual',priority=100,"
                    "cancel_requested=0,ready=?,updated=?,attempts=0,error=NULL,"
                    "generation=generation+1,progress_current=NULL,progress_total=NULL,started=NULL,cached=NULL WHERE id=?",
                    (directive, time.time(), time.time(), job_id),
                ).rowcount
            )

    def promote(self, job_id: int) -> bool:
        with self.connect() as db:
            return bool(
                db.execute(
                    "UPDATE jobs SET priority=100,origin='manual',updated=? WHERE id=? AND state IN ('waiting','queued','retry')",
                    (time.time(), job_id),
                ).rowcount
            )

    def cancel(self, job_id: int) -> bool:
        with self.connect() as db:
            return bool(
                db.execute(
                    "UPDATE jobs SET cancel_requested=1,state=CASE WHEN state='processing' THEN state ELSE 'cancelled' END,updated=? WHERE id=? AND state IN ('waiting','queued','retry','processing')",
                    (time.time(), job_id),
                ).rowcount
            )

    def record_usage(self, started: float, seconds: float) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO resource_usage VALUES (?,?)", (started, seconds))
            db.execute("DELETE FROM resource_usage WHERE started + seconds < ?", (time.time() - 7200,))

    def background_seconds(self) -> float:
        now = time.time()
        with self.connect() as db:
            return db.execute(
                "SELECT COALESCE(SUM(MAX(0, MIN(started+seconds, ?) - MAX(started, ?))),0) FROM resource_usage",
                (now, now - 3600),
            ).fetchone()[0]

    def background_budget(self, minutes: int) -> dict:
        """Describe rolling-hour background use and when a reached limit clears."""
        now = time.time()
        with self.connect() as db:
            intervals = [
                (row["started"], row["started"] + row["seconds"])
                for row in db.execute(
                    "SELECT started,seconds FROM resource_usage WHERE started+seconds>?",
                    (now - 3600,),
                )
            ]

        def used(at: float) -> float:
            window_start = at - 3600
            return sum(max(0, min(end, at) - max(start, window_start)) for start, end in intervals)

        limit = minutes * 60
        consumed = used(now)
        resume_at = None
        if consumed >= limit and intervals:
            low, high = now, now + 3601
            for _ in range(32):
                middle = (low + high) / 2
                if used(middle) >= limit:
                    low = middle
                else:
                    high = middle
            resume_at = max(now + 1, high)
        return {
            "used_seconds": consumed,
            "limit_seconds": limit,
            "remaining_seconds": max(0, limit - consumed),
            "resume_at": resume_at,
        }

    def adopt_policy(self, policy: str) -> int:
        """Re-open unresolved work when the installed decision policy changes.

        An upgrade should benefit an existing library without anyone re-queueing by
        hand. Jobs that could not be decided are exactly the ones a policy change is
        likely to resolve, so they come back; settled verdicts are left alone rather
        than re-running the whole library on every release.
        """
        with self.connect() as db:
            previous = db.execute("SELECT value FROM meta WHERE key='policy'").fetchone()
            if previous and previous[0] == policy:
                return 0
            db.execute("INSERT OR REPLACE INTO meta VALUES ('policy',?)", (policy,))
            if not previous:
                return 0  # first run: nothing to reconsider
            # Settled verdicts survive an upgrade; only work the previous version
            # could not resolve is worth reconsidering under new rules.
            return db.execute(
                "UPDATE jobs SET state='queued',stage='',origin='retry',priority=40,"
                "attempts=0,error=NULL,ready=?,updated=? WHERE state IN ('review','failed')",
                (time.time(), time.time()),
            ).rowcount

    def prune(self, keep: int = 200) -> int:
        """Superseded rows are bookkeeping. Policy changes can create one per file per
        revision, so retain a recent window instead of growing the table forever."""
        with self.connect() as db:
            return db.execute(
                "DELETE FROM jobs WHERE state='superseded' AND id NOT IN "
                "(SELECT id FROM jobs WHERE state='superseded' ORDER BY updated DESC LIMIT ?)",
                (keep,),
            ).rowcount

    def snapshot(self) -> dict:
        # A real library keeps thousands of jobs pending. Showing one flat page of
        # them buries every result, so return the active job, the few that are next,
        # and the recent outcomes -- which is what the dashboard is actually for.
        select = (
            "SELECT jobs.*, (SELECT provider FROM managed_media "
            "WHERE path=jobs.media LIMIT 1) AS manager FROM jobs "
        )
        pending = "('queued','waiting','retry')"
        now = time.time()
        with self.connect() as db:
            jobs = [
                dict(row) for row in db.execute(select + "WHERE state='processing' ORDER BY updated DESC")
            ]
            jobs += [
                dict(row)
                for row in db.execute(
                    # Identical ordering to claim(), including the ageing term, or the
                    # dashboard shows a different "next up" than the queue will run.
                    select
                    + f"WHERE state IN {pending} "
                    + f"ORDER BY (priority + MIN(120, CAST(({now}-created)/3600 AS INTEGER))) DESC,"
                    "CASE WHEN origin='backlog' THEN library_order ELSE '' END,created,id "
                    + "LIMIT 25"
                )
            ]
            jobs += [
                dict(row)
                for row in db.execute(
                    select + f"WHERE state NOT IN {pending} AND state<>'processing' "
                    "ORDER BY updated DESC LIMIT 75"
                )
            ]
            # A row is a file, so this is simply how many files are in each state.
            counts = dict(db.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state"))
            media_total = (
                db.execute("SELECT COUNT(*) FROM managed_media").fetchone()[0]
                or db.execute("SELECT COUNT(DISTINCT media) FROM jobs").fetchone()[0]
            )
            notices = [dict(row) for row in db.execute("SELECT * FROM notices ORDER BY name")]
            integrations = [
                dict(row)
                for row in db.execute(
                    "SELECT provider,last_attempt,last_success,healthy,file_count,error FROM provider_sync ORDER BY provider"
                )
            ]
        for job in jobs:
            job["title"] = Path(job["media"]).stem
            # Audit reports carry per-cue evidence and run to six figures of JSON. No
            # list renders them, and shipping a hundred of them on every update is what
            # made the interface feel like it was buffering. The detail view fetches the
            # one report it needs from /api/jobs/{id}.
            job["has_report"] = bool(job.pop("report", None))
        return {
            "jobs": jobs,
            "counts": counts,
            "media_total": media_total,
            "notices": notices,
            "integrations": integrations,
        }
