from __future__ import annotations

import base64
import hmac
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
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
        if not token or not hmac.compare_digest(token.encode(), store.token.encode()):
            raise HTTPException(401, "Enter your Crowbarr API key to continue")

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
                "version": __version__,
                "paused": store.get().paused,
                "last_scan": service.last_scan,
                "media_count": service.scan_count,
                "configured": bool(store.get().media_roots()),
                "discovery_mode": "arr" if store.get().providers() else "folders",
                "providers": store.get().providers(),
                "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
            }
        )
        return data

    @app.get("/api/settings", dependencies=[Depends(authenticate)])
    def settings():
        return store.public()

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
        if payload.get("eventType") != "Test":
            service.scan_event.set()
        return {"message": "Event accepted; libraries will be reconciled"}

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
