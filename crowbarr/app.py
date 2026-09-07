from __future__ import annotations

import base64
import hmac
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import __version__
from .config import ConfigStore, Settings
from .db import Database
from .integrations import check_connection
from .service import Service


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
    store = ConfigStore(directory or Path(os.environ.get("CROWBARR_DATA", "data")).resolve())
    db = Database(store.directory / "crowbarr.db")
    service = Service(store, db)

    @asynccontextmanager
    async def lifespan(app):
        if background:
            service.start()
        yield
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

    @app.get("/api/status", dependencies=[Depends(authenticate)])
    def status():
        data = db.snapshot()
        data.update(
            {
                "resources": service.resources,
                "wait_reason": service.wait_reason,
                "version": __version__,
                "paused": store.get().paused,
                "last_scan": service.last_scan,
                "media_count": data.pop("media_total", service.scan_count),
                "configured": bool(store.get().media_roots()),
                "discovery_mode": "arr" if store.get().providers() else "folders",
                "providers": store.get().providers(),
                "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
            }
        )
        return data

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
        return {**store.public(), "api_key": store.token}

    @app.put("/api/settings", dependencies=[Depends(authenticate)])
    def save_settings(payload: dict):
        previous = store.get()
        for name in ("sonarr", "radarr", "bazarr", "plex"):
            if name in payload and isinstance(payload[name], dict):
                value = payload[name]
                if not value.get("api_key") and value.get("url"):
                    value["api_key"] = getattr(previous, name).api_key
        try:
            settings = Settings.model_validate(payload)
        except ValidationError as error:
            details = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors()]
            raise HTTPException(422, "; ".join(details)) from None
        if settings.device == "cpu" and settings.compute_type in {"float16", "int8_float16"}:
            raise HTTPException(422, "CPU processing requires int8 or float32 compute")
        if settings.discovery_fingerprint() != previous.discovery_fingerprint():
            db.invalidate_sync()
        store.save(settings)
        service.scan_event.set()
        return store.public()

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
        if not db.retry(job_id):
            raise HTTPException(409, "Only failed or attention-needed jobs can be retried")
        return {"message": "Job queued again"}

    @app.post("/api/process", dependencies=[Depends(authenticate)], status_code=202)
    def process_now(payload: dict):
        import time

        from .library import queue_media, signature, source_subtitle

        media = Path(payload.get("media", ""))
        current = store.get()
        if not media.is_absolute() or not db.eligible(str(media), current.providers(), healthy=False):
            raise HTTPException(422, "Select an absolute path from the managed library")
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
    def media(q: str = "", limit: int = 20):
        """Search the managed library so the dashboard can request work on a title."""
        term = f"%{q.strip()}%"
        with db.connect() as connection:
            rows = connection.execute(
                "SELECT path, title FROM managed_media WHERE path LIKE ? ORDER BY path LIMIT ?",
                (term, max(1, min(limit, 50))),
            ).fetchall()
            if not rows:
                rows = connection.execute(
                    "SELECT DISTINCT media AS path, media AS title FROM jobs WHERE media LIKE ? "
                    "ORDER BY media LIMIT ?",
                    (term, max(1, min(limit, 50))),
                ).fetchall()
        return {
            "results": [
                {"path": row["path"], "title": Path(row["path"]).stem, "label": row["title"]}
                for row in rows
            ]
        }

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
        from .processor import publish_candidate
        from .subtitles import parse_srt, validate_cues

        job = db.get(job_id)
        path = store.directory / "candidates" / f"{job_id}.srt"
        if not job or job["state"] != "review" or not path.is_file() or path.is_symlink():
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
        job = db.get(job_id)
        path = store.directory / "candidates" / f"{job_id}.srt"
        if not job or not path.is_file() or path.is_symlink():
            raise HTTPException(404, "No private candidate is available")
        return FileResponse(path, media_type="application/x-subrip", filename=f"crowbarr-{job_id}.srt")

    @app.post("/api/connections/{name}/test", dependencies=[Depends(authenticate)])
    def test_connection(name: str):
        if name not in {"sonarr", "radarr", "bazarr", "plex"}:
            raise HTTPException(404, "Unknown integration")
        try:
            return check_connection(name, getattr(store.get(), name))
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
                for file in files:
                    queue_media(Path(file.path), current, db, time.time(), origin="import")
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
