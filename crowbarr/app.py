from __future__ import annotations

import base64
import hmac
import os
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import __version__
from .capabilities import SPEECH_MODELS, capabilities_for, model_inventory, start_download, warm_up
from .config import ConfigStore, Settings
from .db import Database
from .integrations import check_connection
from .service import Service
from .version import AUDIT_POLICY_VERSION


class BodyLimitMiddleware:
    """Bound request bodies even when a client uses chunked transfer encoding."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body.extend(event.get("body", b""))
            if len(body) > 65536:
                return await JSONResponse({"detail": "Request too large"}, status_code=413)(
                    scope, receive, send
                )
            if not event.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(directory: Path | None = None, background: bool = True) -> FastAPI:
    store = ConfigStore((directory or Path(os.environ.get("CROWBARR_DATA", "data"))).resolve())
    db = Database(store.directory / "crowbarr.db")
    service = Service(store, db)

    @asynccontextmanager
    async def lifespan(app):
        if background:
            service.start()
            warm_up()
        try:
            yield
        finally:
            if background:
                service.close()

    app = FastAPI(
        title="Crowbarr",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store, app.state.db, app.state.service = store, db, service
    app.add_middleware(BodyLimitMiddleware)

    def authenticate(request: Request):
        token = request.headers.get("x-api-key", "")
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:]
        elif authorization.lower().startswith("basic "):
            try:
                username, token = base64.b64decode(authorization[6:], validate=True).decode().split(":", 1)
                if username != "crowbarr":
                    token = ""
            except (ValueError, UnicodeError):
                token = ""
        # A signed session cookie is how a person signs in. The API key stays valid for
        # Sonarr, Radarr, Bazarr and scripts, which cannot hold a cookie.
        session = request.cookies.get("crowbarr_session", "")
        if session and store.valid_session(session):
            return
        if not token or not hmac.compare_digest(token.encode(), store.token.encode()):
            raise HTTPException(401, "Sign in to continue")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        try:
            length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            return JSONResponse({"detail": "Invalid content length"}, status_code=400)
        if length < 0 or length > 65536:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api") else "no-cache"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Pydantic's default response includes submitted values, including service API keys.
        return JSONResponse({"detail": "Invalid request. Check the submitted fields."}, status_code=422)

    @app.get("/health")
    def health():
        if background and service.threads and not all(thread.is_alive() for thread in service.threads):
            raise HTTPException(503, "A background service stopped")
        return {"status": "ok", "version": __version__}

    def build_status():
        data = db.snapshot()
        data.update(
            {
                "resources": service.resources,
                "wait_reason": service.wait_reason,
                "wait_until": service.wait_until,
                "wait_total": service.wait_total,
                "background_budget": db.background_budget(store.get().background_budget_minutes),
                "version": __version__,
                "audit_policy_version": AUDIT_POLICY_VERSION,
                "paused": store.get().paused,
                "last_scan": service.last_scan,
                "scan_in_progress": service.scan_in_progress,
                "media_count": data.pop("media_total", service.scan_count),
                "configured": bool(store.get().media_roots()),
                "discovery_mode": "arr" if store.get().providers() else "folders",
                "providers": store.get().providers(),
                "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
            }
        )
        return data

    @app.get("/api/status", dependencies=[Depends(authenticate)])
    def status():
        return build_status()

    @app.get("/api/events", dependencies=[Depends(authenticate)])
    def events():
        """Push state to the browser when it changes, instead of being asked on a timer.

        The interface should never poll: a shell that redraws on a clock cannot help but
        feel like it is buffering. This watches the snapshot server-side and emits only
        when something actually differs, so an idle library costs one heartbeat a minute.
        """
        import asyncio
        import json as _json

        from fastapi.responses import StreamingResponse

        async def stream():
            previous, beat = None, 0.0
            while not service.stop_event.is_set():
                payload = await asyncio.to_thread(build_status)
                encoded = _json.dumps(payload, default=str)
                now = time.monotonic()
                if encoded != previous:
                    previous = encoded
                    beat = now
                    yield f"event: status\ndata: {encoded}\n\n"
                elif now - beat > 20:
                    beat = now
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/openapi.json", dependencies=[Depends(authenticate)], include_in_schema=False)
    def api_schema():
        schema = app.openapi()
        schema.setdefault("components", {})["securitySchemes"] = {
            "ApiKey": {"type": "apiKey", "in": "header", "name": "X-Api-Key"},
            "Bearer": {"type": "http", "scheme": "bearer"},
        }
        for path, operations in schema["paths"].items():
            if path.startswith("/api/") and path not in {"/api/session", "/api/setup"}:
                for operation in operations.values():
                    if isinstance(operation, dict):
                        operation["security"] = [{"ApiKey": []}, {"Bearer": []}]
        return schema

    @app.get("/api/jobs", dependencies=[Depends(authenticate)])
    def jobs(
        state: str = "queue", q: str = "", offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)
    ):
        import time

        groups = {
            "queue": ["processing", "queued", "waiting", "retry"],
            "review": ["review", "failed"],
            "history": ["completed", "unchanged", "superseded", "cancelled", "failed", "review"],
        }
        valid = set(sum(groups.values(), []))
        if state not in groups and state not in valid and state != "all":
            raise HTTPException(422, "Unknown job state")
        if state == "history":
            # What happened, newest first -- not the current state of each file.
            where, args = [], []
            for word in q.split():
                where.append("media LIKE ? ESCAPE '!'")
                args.append("%" + word.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%")
            condition = " AND ".join(where) or "1"
            with db.connect() as connection:
                total = connection.execute(
                    f"SELECT COUNT(*) FROM history WHERE {condition}", args
                ).fetchone()[0]
                rows = connection.execute(
                    f"SELECT * FROM history WHERE {condition} ORDER BY finished DESC, id DESC LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                ).fetchall()
            return {
                "results": [
                    {
                        **dict(row),
                        # Details opens the job this outcome belongs to, not the history row.
                        "id": row["job_id"] or row["id"],
                        "history_id": row["id"],
                        "title": Path(row["media"]).stem,
                        "updated": row["finished"],
                        "created": row["finished"],
                        "has_report": bool(row["job_id"]),
                    }
                    for row in rows
                ],
                "total": total,
                "offset": offset,
                "limit": limit,
            }
        selected = groups.get(state, [state])
        where, args = [], []
        if state != "all":
            where.append("state IN (" + ",".join("?" for _ in selected) + ")")
            args.extend(selected)
        for word in q.split():
            where.append("media LIKE ? ESCAPE '!'")
            args.append("%" + word.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%")
        condition = " AND ".join(where) or "1"
        order = (
            "CASE WHEN state='processing' THEN 0 ELSE 1 END, "
            f"(priority + MIN(120, CAST(({time.time()}-created)/3600 AS INTEGER))) DESC,"
            "CASE WHEN origin='backlog' THEN library_order ELSE '' END,created,id"
            if state == "queue"
            else "updated DESC,id DESC"
        )
        with db.connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM jobs WHERE {condition}", args).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE {condition} ORDER BY {order} LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        result = []
        for row in rows:
            job = dict(row)
            # Same reason as the status payload: the list shows none of it.
            job.update(title=Path(job["media"]).stem, has_report=bool(job.pop("report", None)))
            result.append(job)
        return {"results": result, "total": total, "offset": offset, "limit": limit}

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(authenticate)])
    def job_detail(job_id: int):
        import json

        job = db.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return {**job, "title": Path(job["media"]).stem, "report": json.loads(job["report"] or "null")}

    @app.get("/api/session")
    def session_state():
        """Public: lets the sign-in page know whether a login has been created yet."""
        return {"configured": store.has_account()}

    @app.post("/api/setup", status_code=201)
    def setup(payload: dict, response: Response):
        try:
            store.create_account(payload.get("username", ""), payload.get("password", ""))
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        response.set_cookie(
            "crowbarr_session", store.issue_session(), httponly=True, samesite="lax", path="/"
        )
        return {"message": "Dashboard login created"}

    @app.post("/api/session")
    def sign_in(payload: dict, response: Response):
        if not store.check_account(payload.get("username", ""), payload.get("password", "")):
            raise HTTPException(401, "That username and password do not match")
        response.set_cookie(
            "crowbarr_session", store.issue_session(), httponly=True, samesite="lax", path="/"
        )
        return {"message": "Signed in"}

    @app.delete("/api/session")
    def sign_out(response: Response):
        response.delete_cookie("crowbarr_session", path="/")
        return {"message": "Signed out"}

    @app.get("/api/settings", dependencies=[Depends(authenticate)])
    def settings():
        # Shown so it can be copied into Sonarr/Radarr/Bazarr; it is no longer the login.
        return settings_payload()

    def settings_payload():
        # No hardware probe here: this answer gates the first paint after sign-in, and
        # the probe imports the inference stack in a child process, which takes seconds.
        from .config import FINGERPRINT_FIELDS

        return {**store.public(), "api_key": store.token, "recheck_fields": list(FINGERPRINT_FIELDS)}

    @app.get("/api/capabilities", dependencies=[Depends(authenticate)])
    def capabilities():
        # Answers at once with the cached probe, or with status "checking" while the
        # first probe runs; the settings page asks again until it settles.
        return capabilities_for(store.directory, block=False)

    @app.post("/api/capabilities/alignment", dependencies=[Depends(authenticate)], status_code=202)
    def fetch_alignment_model():
        runtime = capabilities_for(store.directory)
        if not runtime["refinement"]["available"]:
            raise HTTPException(409, runtime["refinement"]["reason"])
        if runtime["models"]["alignment"]["present"]:
            return runtime
        start_download(store.directory, "alignment")
        return capabilities_for(store.directory, block=False)

    @app.post("/api/capabilities/model/{name}", dependencies=[Depends(authenticate)], status_code=202)
    def fetch_speech_model(name: str):
        if name not in SPEECH_MODELS:
            raise HTTPException(404, "Unknown speech model")
        # A download needs the model inventory, not the hardware answer.
        runtime = capabilities_for(store.directory, block=False)
        if name in runtime["models"]["speech"]:
            return runtime
        start_download(store.directory, name)
        return capabilities_for(store.directory, block=False)

    @app.put("/api/settings", dependencies=[Depends(authenticate)])
    def save_settings(payload: dict):
        previous = store.get()
        for name in ("sonarr", "radarr", "bazarr", "plex"):
            if name in payload and isinstance(payload[name], dict):
                value = payload[name]
                if not value.get("api_key") and value.get("url"):
                    value["api_key"] = getattr(previous, name).api_key
        carry_forward = bool(payload.pop("carry_forward", False))
        try:
            settings = Settings.model_validate({**previous.model_dump(), **payload})
        except ValidationError as error:
            details = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors()]
            raise HTTPException(422, "; ".join(details)) from None
        if settings.device == "cpu" and settings.compute_type in {"float16", "int8_float16"}:
            raise HTTPException(422, "CPU processing requires int8 or float32 compute")
        # Only turning CUDA on needs the hardware answer, so only that change waits for
        # the probe. Saving anything else must not import the inference stack.
        if settings.device == "cuda" and previous.device != "cuda":
            runtime = capabilities_for(store.directory)
            if not runtime["cuda"]["available"]:
                raise HTTPException(422, runtime["cuda"]["reason"])
        # A model is chosen after it is downloaded, never before. Otherwise the first
        # job to run pays for the download and looks like it has stalled.
        if settings.model != previous.model and settings.model not in model_inventory(store.directory)["speech"]:
            raise HTTPException(422, f"Download the {settings.model} model before selecting it")
        if settings.discovery_fingerprint() != previous.discovery_fingerprint():
            db.invalidate_sync()
        # Changing the model re-checks the whole library. Carrying the old fingerprint
        # lets the next scan keep settled verdicts instead of re-running every file.
        if settings.fingerprint() != previous.fingerprint():
            if carry_forward:
                db.request_carry_forward(previous.fingerprint(), settings.fingerprint())
            else:
                db.cancel_carry_forward()
        store.save(settings)
        from .library import skip_short_pending

        skip_short_pending(settings, db)
        service.scan_event.set()
        return settings_payload()

    @app.get("/api/media-folders", dependencies=[Depends(authenticate)])
    def media_folders():
        from .library import suggested_roots

        return suggested_roots(store.get())

    @app.post("/api/media-folders/test", dependencies=[Depends(authenticate)])
    def test_media_folders(payload: dict):
        from .library import check_media_folder

        roots = payload.get("roots")
        if (not isinstance(roots, list) or not roots or len(roots) > 100
                or any(not isinstance(root, str) or len(root) > 4096 for root in roots)):
            raise HTTPException(422, "Enter between 1 and 100 folder paths to test")
        return {"results": [check_media_folder(root.strip()) for root in dict.fromkeys(roots)]}

    @app.post("/api/scan", dependencies=[Depends(authenticate)], status_code=202)
    def trigger_scan():
        service.scan_event.set()
        return {"message": "Library check requested"}

    @app.post("/api/pause", dependencies=[Depends(authenticate)])
    def toggle_pause():
        settings = store.get()
        settings.paused = not settings.paused
        store.save(settings)
        return {"paused": settings.paused}

    @app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(authenticate)])
    def retry(job_id: int):
        db.refresh_library_policy()
        job = db.get(job_id)
        if job and job.get("library_blocked"):
            raise HTTPException(409, "This file is skipped. Choose Always include in Library before retrying.")
        if not db.retry(job_id):
            raise HTTPException(409, "Only failed, attention-needed, or set-aside jobs can be retried")
        return {"message": "Job queued again"}

    @app.post("/api/jobs/{job_id}/skip", dependencies=[Depends(authenticate)])
    def skip(job_id: int):
        if not db.skip(job_id):
            raise HTTPException(409, "Only failed or attention-needed jobs can be set aside")
        return {"message": "Set aside; it will not come back on a policy upgrade"}

    @app.post("/api/process", dependencies=[Depends(authenticate)], status_code=202)
    def process_now(payload: dict):
        import time

        from .library import queue_media, signature, source_subtitle

        media = Path(payload.get("media", ""))
        current = store.get()
        if not media.is_absolute() or not db.eligible(str(media), current.providers(), healthy=False):
            raise HTTPException(422, "Select an absolute path from the managed library")
        from .catalog import decision, preferences, rows

        with db.connect() as connection:
            record = next((r for r in rows(connection) if r["path"] == str(media)), None)
            if record and decision(record, *preferences(connection))["skipped"]:
                raise HTTPException(409, "This file is skipped. Choose Always include in Library to process it.")
        try:
            queue_media(media, current, db, time.time(), origin="manual")
            source = source_subtitle(media, current)
            identifier = db.enqueue(
                str(media),
                signature(media, source, current),
                str(source) if source else None,
                time.time(),
                origin="manual",
            )
            if payload.get("directive") == "generate":
                # Re-open regardless of a previous verdict: the request is the point.
                db.direct(identifier, "generate")
                return {"message": "Fresh subtitle generation requested", "job_id": identifier}
            db.direct(identifier, "")
            return {"message": "Manual processing requested", "job_id": identifier}
        except (OSError, ValueError):
            raise HTTPException(422, "Media must be readable within configured media roots") from None

    @app.get("/api/media", dependencies=[Depends(authenticate)])
    def media(
        q: str = "", provider: str = "all", offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)
    ):
        if provider not in {"all", "sonarr", "radarr", "folders"}:
            raise HTTPException(422, "Unknown library source")
        conditions, args = [], []
        if provider != "all":
            conditions.append("provider=?")
            args.append(provider)
        for word in q.split():
            conditions.append("(path LIKE ? ESCAPE '!' OR title LIKE ? ESCAPE '!')")
            term = "%" + word.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
            args.extend([term, term])
        where = " AND ".join(conditions) or "1"
        catalog = (
            "WITH catalog AS (SELECT path,title,provider FROM managed_media UNION ALL "
            "SELECT DISTINCT media AS path,media AS title,'folders' AS provider FROM jobs "
            "WHERE media NOT IN (SELECT path FROM managed_media)) "
        )
        with db.connect() as connection:
            total = connection.execute(
                catalog + f"SELECT COUNT(*) FROM catalog WHERE {where}", args
            ).fetchone()[0]
            rows = connection.execute(
                catalog + f"SELECT * FROM catalog WHERE {where} ORDER BY title,path LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        return {
            "results": [
                {
                    "path": row["path"],
                    "title": Path(row["path"]).stem,
                    "label": row["title"],
                    "provider": row["provider"],
                }
                for row in rows
            ],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    @app.get("/api/library", dependencies=[Depends(authenticate)])
    def browse_library(kind: str = "shows", q: str = "", status: str = "all",
                       offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
        from .catalog import library

        if kind not in {"shows", "movies"} or status not in {"all", "skipped", "eligible"}:
            raise HTTPException(422, "Unknown library view")
        with db.connect() as connection:
            return library(connection, kind, q, offset, limit, status)

    @app.put("/api/library/preferences", dependencies=[Depends(authenticate)])
    def library_preference(payload: dict):
        from .catalog import file_targets, refresh_policy, rows

        scope, target, choice = payload.get("scope"), payload.get("target"), payload.get("decision")
        with db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if scope == "audio":
                if not isinstance(payload.get("enabled"), bool):
                    raise HTTPException(422, "Choose whether to skip other audio languages")
                connection.execute("INSERT OR REPLACE INTO meta VALUES ('skip_other_audio',?)",
                                   ("true" if payload["enabled"] else "false",))
            else:
                if scope not in {"title", "season", "file"} or choice not in {"skip", "include", "auto"}:
                    raise HTTPException(422, "Choose a title, season, or file preference")
                records = rows(connection)
                targets = {r["path"] if scope == "file" else r["key"] if scope == "title" else
                           f"{r['key']}:{r['season']}" for r in records if scope != "season" or r["season"] is not None}
                if not isinstance(target, str) or target not in targets:
                    raise HTTPException(404, "Library item is no longer available; sync and try again")
                affected = file_targets(next(r for r in records if r["path"] == target)) if scope == "file" else [target]
                for rule_target in affected:
                    if choice == "auto":
                        connection.execute("DELETE FROM library_rules WHERE scope=? AND target=?", (scope, rule_target))
                    else:
                        connection.execute("INSERT OR REPLACE INTO library_rules VALUES (?,?,?)", (scope, rule_target, choice))
            refresh_policy(connection)
        return {"message": "Library preference saved. More specific file or season overrides still apply."}

    @app.get("/api/library/artwork/{provider}/{item_id}", dependencies=[Depends(authenticate)])
    def library_artwork(provider: str, item_id: int):
        import httpx

        if provider not in {"sonarr", "radarr"} or item_id <= 0:
            raise HTTPException(404, "No artwork")
        with db.connect() as connection:
            if not connection.execute("SELECT 1 FROM managed_media WHERE provider=? AND item_id=?",
                                      (provider, item_id)).fetchone():
                raise HTTPException(404, "No artwork")
        current = getattr(store.get(), provider)
        if not current.url or not current.api_key:
            raise HTTPException(404, "No artwork")
        # Fixed manager endpoint: no client-supplied URL, redirects, or exposed API key.
        try:
            with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
                with client.stream("GET", current.url + f"/api/v3/mediacover/{item_id}/poster.jpg",
                                   headers={"X-Api-Key": current.api_key}) as upstream:
                    upstream.raise_for_status()
                    mime = upstream.headers.get("content-type", "").split(";")[0]
                    if mime not in {"image/jpeg", "image/png", "image/webp"}:
                        raise ValueError("Not an image")
                    content = bytearray()
                    for chunk in upstream.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 4 * 1024 * 1024:
                            raise ValueError("Image too large")
            return Response(bytes(content), media_type=mime)
        except (httpx.HTTPError, ValueError):
            raise HTTPException(404, "Artwork unavailable") from None

    @app.get("/api/jobs/{job_id}/subtitle", dependencies=[Depends(authenticate)])
    def download_subtitle(job_id: int):
        import hashlib
        import json
        from urllib.parse import quote

        from .library import allowed

        job = db.get(job_id)
        if not job:
            raise HTTPException(404, "No published subtitle is available for this file")
        report = json.loads(job["report"] or "{}")
        if not job.get("output") and job["state"] == "unchanged":
            try:
                if report.get("audited_subtitle"):
                    path = Path(report["audited_subtitle"])
                    if path.is_symlink() or path.resolve().parent != (store.directory / "subtitles").resolve():
                        raise ValueError("Invalid saved subtitle")
                    if path.stat().st_size > 8 * 1024 * 1024:
                        raise ValueError("Subtitle too large")
                    content = path.read_bytes()
                    if hashlib.sha256(content).hexdigest() != report.get("audited_subtitle_sha256"):
                        raise ValueError("Saved subtitle changed")
                elif report.get("selected_source_kind") == "external":
                    from .library import subtitle_sources
                    from .processor import current

                    path = Path(report.get("selected_source", ""))
                    if (not allowed(Path(job["media"]), store.get()) or
                            path not in subtitle_sources(Path(job["media"]), store.get()) or
                            path.stat().st_size > 8 * 1024 * 1024 or not current(job, store.get())):
                        raise ValueError("Authored subtitle changed")
                    content = path.read_bytes()
                elif report.get("selected_source_kind") == "embedded":
                    import re
                    import subprocess

                    from .processor import current

                    match = re.fullmatch(r"embedded stream (\d+)", report.get("selected_source", ""))
                    if not match or not allowed(Path(job["media"]), store.get()) or not current(job, store.get()):
                        raise ValueError("Audited video changed")
                    # Legacy unchanged audits predate saved downloads. Extract just
                    # their selected text track; no audio decoding or inference.
                    try:
                        result = subprocess.run(
                            ["ffmpeg", "-nostdin", "-v", "error", "-i", job["media"], "-map", f"0:{match[1]}",
                             "-f", "srt", "-fs", str(8 * 1024 * 1024), "pipe:1"],
                            capture_output=True, timeout=60, check=True,
                        )
                    except (OSError, subprocess.SubprocessError):
                        raise ValueError("Cannot extract audited subtitle") from None
                    content = result.stdout
                    if not content or len(content) >= 8 * 1024 * 1024 or not current(job, store.get()):
                        raise ValueError("Audited subtitle unavailable")
                else:
                    raise ValueError("No downloadable subtitle")
                filename = Path(job["media"]).stem + ".audited.en.srt"
                return Response(content, media_type="application/x-subrip", headers={
                    "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)})
            except (OSError, ValueError):
                raise HTTPException(404, "Audited subtitle is unavailable or changed; audit the file again") from None
        if not job.get("output"):
            raise HTTPException(404, "No published subtitle is available for this file")
        path = Path(job["output"])
        media_path = Path(job["media"])
        try:
            if (not allowed(media_path, store.get()) or path.is_symlink() or not path.is_file()
                    or path.resolve().parent != media_path.resolve().parent or path.suffix.lower() != ".srt"
                    or path.stat().st_size > 8 * 1024 * 1024):
                raise ValueError("Invalid output")
            content = path.read_bytes()
            if not db.owns_output(str(media_path), str(path), hashlib.sha256(content).hexdigest()):
                raise ValueError("Output changed")
        except (OSError, ValueError):
            raise HTTPException(404, "Published subtitle is missing or changed; audit the file again") from None
        return Response(content, media_type="application/x-subrip", headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(path.name),
        })

    @app.post("/api/jobs/{job_id}/promote", dependencies=[Depends(authenticate)])
    def promote(job_id: int):
        if not db.promote(job_id):
            raise HTTPException(409, "Only pending jobs can move to the front")
        return {"message": "Prioritized for the next available slot"}

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(authenticate)])
    def cancel(job_id: int):
        if not db.cancel(job_id):
            raise HTTPException(409, "This job has already finished")
        return {"message": "Cancellation requested"}

    @app.post("/api/jobs/{job_id}/providers", dependencies=[Depends(authenticate)])
    def providers(job_id: int):
        from .bazarr import inspect_sources

        job = db.get(job_id)
        if not job or not store.get().bazarr.url:
            raise HTTPException(404, "Configure Bazarr and select a managed job")
        try:
            result = inspect_sources(store.get(), db, job["media"], store.directory, job_id)
            # Opaque download handles remain private; the UI receives metadata only.
            return {
                **result,
                "candidates": [
                    {k: v for k, v in c.items() if k != "subtitle"} for c in result.get("candidates", [])
                ],
            }
        except Exception as error:
            raise HTTPException(502, f"Bazarr search failed ({type(error).__name__})") from None

    @app.post("/api/jobs/{job_id}/approve", dependencies=[Depends(authenticate)])
    def approve(job_id: int):
        import hashlib
        import json

        from .media import probe
        from .processor import publish_candidate, review_candidate_path
        from .subtitles import parse_srt, validate_cues

        job = db.get(job_id)
        if not job or job["state"] != "review":
            raise HTTPException(409, "Only a saved review candidate can be approved")
        try:
            path = review_candidate_path(store.directory, job)
        except ValueError:
            raise HTTPException(409, "Only a saved review candidate can be approved") from None
        if not path.is_file() or path.is_symlink():
            raise HTTPException(409, "Only a saved review candidate can be approved")
        rendered = path.read_text()
        report = json.loads(job["report"] or "{}")
        if hashlib.sha256(rendered.encode()).hexdigest() != report.get("output_sha256"):
            raise HTTPException(409, "Candidate changed; retry processing before approval")
        try:
            cues = parse_srt(rendered)
            duration = float(probe(Path(job["media"]))["format"]["duration"])
            errors = [
                issue for issue in validate_cues(cues, duration) if "unsuitable reading duration" not in issue
            ]
            if errors:
                raise HTTPException(409, "Candidate has invalid timestamps; repair before approval")
            report["manual_approval"] = True
            result = publish_candidate(job, store.get(), store.directory, db, rendered, report)
            db.update(job_id, **result)
            return {
                "message": "Candidate published"
                if result["state"] == "completed"
                else result.get("error", "Publication did not complete")
            }
        except (ValueError, OSError):
            raise HTTPException(409, "Candidate or media is unavailable") from None

    @app.get("/api/jobs/{job_id}/candidate", dependencies=[Depends(authenticate)])
    def candidate(job_id: int):
        from .processor import review_candidate_path

        job = db.get(job_id)
        if not job:
            raise HTTPException(404, "No private candidate is available")
        try:
            path = review_candidate_path(store.directory, job)
        except ValueError:
            raise HTTPException(404, "No private candidate is available") from None
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, "No private candidate is available")
        return FileResponse(path, media_type="application/x-subrip", filename=f"crowbarr-{job_id}.srt")

    @app.post("/api/connections/{name}/test", dependencies=[Depends(authenticate)])
    def test_connection(name: str):
        if name not in {"sonarr", "radarr", "bazarr", "plex"}:
            raise HTTPException(404, "Unknown integration")
        try:
            return check_connection(name, getattr(store.get(), name), store.get())
        except Exception as error:
            # Do not reflect URLs or upstream response bodies containing secrets.
            raise HTTPException(
                400,
                f"Could not connect to {name.title()} ({type(error).__name__}). "
                "Check the saved URL, API key, and network access.",
            ) from None

    @app.post("/api/hooks/{name}", dependencies=[Depends(authenticate)], status_code=202)
    def hook(name: str, payload: dict):
        if name not in {"sonarr", "radarr", "bazarr"}:
            raise HTTPException(404, "Unknown integration")
        if payload.get("eventType") == "Test":
            return {"message": "Connection test accepted"}
        import time

        from .arr import ArrClient
        from .library import queue_media

        current = store.get()
        try:
            if name in {"sonarr", "radarr"} and payload.get(
                "series" if name == "sonarr" else "movie", {}
            ).get("id"):
                with ArrClient(name, getattr(current, name)) as client:
                    files = client.event_files(payload)
                with db.connect() as connection:
                    for file in files:
                        connection.execute(
                            "INSERT OR REPLACE INTO managed_media VALUES (?,?,?,?,?,?)",
                            (
                                file.provider,
                                file.file_id,
                                file.item_id,
                                file.path,
                                file.remote_path,
                                file.title,
                            ),
                        )
                    db.save_library_metadata(connection, [file.record() for file in files])
                for file in files:
                    queue_media(Path(file.path), current, db, time.time(), origin="import")
                db.refresh_library_policy()
                return {"message": f"Queued {len(files)} imported files"}
            path = payload.get("path") or payload.get("video_path")
            if path:
                with db.connect() as connection:
                    rows = connection.execute(
                        "SELECT path FROM managed_media WHERE remote_path=? OR path=?", (path, path)
                    ).fetchall()
                for row in rows:
                    queue_media(Path(row[0]), current, db, time.time(), origin="bazarr")
                if rows:
                    return {"message": "Subtitle upgrade queued"}
        except (ValueError, KeyError, OSError):
            raise HTTPException(422, "Event could not be mapped to a readable managed media file") from None
        service.scan_event.set()
        return {"message": "Event lacked a target; library reconciliation requested"}

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
