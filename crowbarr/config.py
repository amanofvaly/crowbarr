from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path, PureWindowsPath
from threading import RLock

from pydantic import BaseModel, Field, field_validator


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".crowbarr-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Connection(BaseModel):
    url: str = ""
    api_key: str = ""

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Use an http:// or https:// service URL")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("Put credentials in the API key field, not the URL")
        return value.rstrip("/")


class PathMapping(BaseModel):
    remote: str
    local: str

    @field_validator("remote")
    @classmethod
    def remote_absolute(cls, value: str) -> str:
        if not value.startswith("/") and not PureWindowsPath(value).is_absolute():
            raise ValueError("Remote path must be absolute in Sonarr/Radarr")
        return value.replace("\\", "/").rstrip("/") or "/"

    @field_validator("local")
    @classmethod
    def local_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute() or Path(value) == Path("/"):
            raise ValueError("Local mapping must be an absolute media directory, not /")
        return str(Path(value).resolve())


class ArrConnection(Connection):
    mappings: list[PathMapping] = Field(default_factory=list, max_length=32)
    monitored_only: bool = True


class Settings(BaseModel):
    roots: list[str] = Field(default_factory=list, max_length=32)
    language: str = "en"
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    cpu_threads: int = Field(default=2, ge=1, le=64)
    scan_seconds: int = Field(default=300, ge=10, le=86400)
    settle_seconds: int = Field(default=120, ge=0, le=86400)
    subtitle_wait_minutes: int = Field(default=30, ge=0, le=10080)
    max_attempts: int = Field(default=3, ge=1, le=10)
    job_timeout_minutes: int = Field(default=240, ge=1, le=1440)
    paused: bool = False
    allow_untagged_audio: bool = False
    allow_untagged_subtitles: bool = False
    min_match_ratio: float = Field(default=0.75, ge=0.5, le=1)
    min_alignment_score: float = Field(default=0.4, ge=0, le=1)
    max_generated_ratio: float = Field(default=0.25, ge=0, le=1)
    sonarr: ArrConnection = Field(default_factory=ArrConnection)
    radarr: ArrConnection = Field(default_factory=ArrConnection)
    bazarr: Connection = Field(default_factory=Connection)
    plex: Connection = Field(default_factory=Connection)

    def providers(self) -> list[str]:
        return [name for name in ("sonarr", "radarr") if getattr(self, name).url]

    def media_roots(self) -> list[str]:
        roots = list(self.roots)
        for name in self.providers():
            roots.extend(mapping.local for mapping in getattr(self, name).mappings)
        return list(dict.fromkeys(roots))

    def discovery_fingerprint(self) -> str:
        import hashlib

        return hashlib.sha256(
            json.dumps(
                {"roots": self.roots, "sonarr": self.sonarr.model_dump(), "radarr": self.radarr.model_dump()},
                sort_keys=True,
            ).encode()
        ).hexdigest()

    @field_validator("roots")
    @classmethod
    def valid_roots(cls, values: list[str]) -> list[str]:
        result = []
        for value in values:
            path = Path(value)
            if not path.is_absolute() or path == Path("/"):
                raise ValueError("Media roots must be absolute directories, not /")
            normalized = str(path.resolve())
            if normalized not in result:
                result.append(normalized)
        return result

    @field_validator("language")
    @classmethod
    def valid_language(cls, value: str) -> str:
        # Scope is deliberate: text matching and speech normalization need language-specific evaluation.
        if value != "en":
            raise ValueError("This release supports English audio and English subtitles only")
        return value

    @field_validator("device")
    @classmethod
    def valid_device(cls, value: str) -> str:
        if value not in {"cpu", "cuda"}:
            raise ValueError("Choose cpu or cuda")
        return value

    @field_validator("model")
    @classmethod
    def valid_model(cls, value: str) -> str:
        if value not in {"tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"}:
            raise ValueError("Choose a supported Whisper model")
        return value

    @field_validator("compute_type")
    @classmethod
    def valid_compute(cls, value: str) -> str:
        if value not in {"int8", "int8_float16", "float16", "float32"}:
            raise ValueError("Unsupported compute type")
        return value

    def fingerprint(self) -> str:
        import hashlib

        from .audit import AUDIT_VERSION

        keys = (
            "language",
            "model",
            "device",
            "compute_type",
            "allow_untagged_audio",
            "allow_untagged_subtitles",
            "min_match_ratio",
            "min_alignment_score",
            "max_generated_ratio",
        )
        return hashlib.sha256(
            json.dumps(
                {**{k: getattr(self, k) for k in keys}, "audit_version": AUDIT_VERSION}, sort_keys=True
            ).encode()
        ).hexdigest()


class ConfigStore:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "settings.json"
        self.lock = RLock()
        self.settings = (
            Settings.model_validate_json(self.path.read_text()) if self.path.exists() else Settings()
        )
        token_path = directory / "admin-token"
        self.token = os.environ.get("CROWBARR_API_KEY", "") or (
            token_path.read_text().strip() if token_path.exists() else secrets.token_urlsafe(32)
        )
        if not token_path.exists():
            atomic_write(token_path, self.token + "\n")

    def get(self) -> Settings:
        with self.lock:
            return self.settings.model_copy(deep=True)

    def save(self, settings: Settings) -> None:
        with self.lock:
            atomic_write(self.path, settings.model_dump_json(indent=2))
            self.settings = settings.model_copy(deep=True)

    def public(self) -> dict:
        data = self.get().model_dump()
        for key in ("sonarr", "radarr", "bazarr", "plex"):
            data[key]["has_api_key"] = bool(data[key].pop("api_key"))
        return data
