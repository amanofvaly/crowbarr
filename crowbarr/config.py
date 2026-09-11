from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path, PureWindowsPath
from threading import RLock

from pydantic import BaseModel, Field, field_validator


def hash_password(password: str) -> str:
    """Store a salted scrypt digest. The dashboard login must not share the API key."""
    import hashlib

    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    import hashlib
    import hmac as _hmac

    try:
        scheme, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32
    )
    return _hmac.compare_digest(candidate.hex(), digest)


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".crowbarr-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            try:
                os.fchmod(stream.fileno(), mode)
            except OSError:
                # ZFS with NFSv4 ACLs refuses chmod (TrueNAS ships aclmode=restricted)
                # and inherits permissions from the parent instead, which is what the
                # neighbouring media files already use. Accept that for published
                # sidecars, but never leave a file we meant to keep private exposed.
                if not mode & 0o077 and os.fstat(stream.fileno()).st_mode & 0o077:
                    raise
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


# Every input a verdict depends on. Changing one re-checks the library, so the UI
# reads this list rather than keeping its own copy that could drift out of step.
FINGERPRINT_FIELDS = (
    "language",
    "model",
    "device",
    "compute_type",
    "allow_untagged_audio",
    "allow_untagged_subtitles",
    "min_match_ratio",
    "min_alignment_score",
    "max_generated_ratio",
    "sampled_audit",
)


class Settings(BaseModel):
    roots: list[str] = Field(default_factory=list, max_length=32)
    language: str = "en"
    model: str = "small.en"
    device: str = "cpu"
    compute_type: str = "int8"
    cpu_threads: int = Field(default=2, ge=1, le=64)
    scan_seconds: int = Field(default=300, ge=10, le=86400)
    settle_seconds: int = Field(default=120, ge=0, le=86400)
    min_duration_minutes: int = Field(default=10, ge=0, le=600)
    subtitle_wait_minutes: int = Field(default=30, ge=0, le=10080)
    max_attempts: int = Field(default=3, ge=1, le=10)
    job_timeout_minutes: int = Field(default=240, ge=1, le=1440)
    paused: bool = False
    min_free_ram_mb: int = Field(default=1536, ge=128, le=262144)
    min_free_vram_mb: int = Field(default=1024, ge=128, le=262144)
    max_cpu_load: float = Field(default=0.75, ge=0.1, le=4)
    backlog_cooldown_seconds: int = Field(default=30, ge=0, le=86400)
    background_budget_minutes: int = Field(default=45, ge=1, le=60)
    quiet_hour_start: int = Field(default=0, ge=0, le=23)
    quiet_hour_end: int = Field(default=0, ge=0, le=23)
    defer_during_plex: bool = True
    cpu_fallback: bool = True
    refine_generated: bool = False
    sampled_audit: bool = True
    bazarr_download_alternatives: bool = False
    # A proven mismatch is the one refusal Crowbarr can answer by itself, because the
    # transcript that proved it is the transcript a fresh subtitle is built from. Left
    # available to switch off for libraries where an authored subtitle, however wrong,
    # is preferred to a generated one.
    generate_over_mismatch: bool = True
    max_provider_attempts: int = Field(default=3, ge=1, le=10)
    # Retained so existing installs keep loading and their cached transcripts stay
    # valid. Track selection no longer consults it: recognition establishes the spoken
    # language from the audio, which is a better answer than a checkbox.
    allow_untagged_audio: bool = False
    allow_untagged_subtitles: bool = False
    min_match_ratio: float = Field(default=0.75, ge=0.5, le=1)
    min_alignment_score: float = Field(default=0.4, ge=0, le=1)
    max_generated_ratio: float = Field(default=0.25, ge=0, le=1)
    sonarr: ArrConnection = Field(default_factory=ArrConnection)
    radarr: ArrConnection = Field(default_factory=ArrConnection)
    bazarr: Connection = Field(default_factory=Connection)
    plex: Connection = Field(default_factory=Connection)
    plex_mappings: list[PathMapping] = Field(default_factory=list, max_length=32)

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
        # The .en builds are English-only and better on English audio at the same size.
        # The multilingual names stay valid so existing installations keep working.
        if value not in {
            "tiny", "base", "small", "medium", "large-v3", "large-v3-turbo",
            "tiny.en", "base.en", "small.en", "medium.en",
        }:
            raise ValueError("Choose a supported Whisper model")
        return value

    @field_validator("compute_type")
    @classmethod
    def valid_compute(cls, value: str) -> str:
        if value not in {"int8", "int8_float16", "float16", "float32"}:
            raise ValueError("Unsupported compute type")
        return value

    def fingerprint(self) -> str:
        """Identify the inputs to a verdict: the media, its subtitles, and these settings.

        The release deliberately is not part of this. Upgrading must not invalidate
        verdicts a previous version already reached, or every release would re-check a
        whole library. Policy changes are handled by reopening unresolved work instead.
        """
        import hashlib

        return hashlib.sha256(
            json.dumps(
                {k: getattr(self, k) for k in FINGERPRINT_FIELDS}, sort_keys=True
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
        # Dashboard credentials are deliberately separate from the API key: the key is
        # pasted into Sonarr, Radarr and Bazarr, so it must not also be the human login.
        self.account_path = directory / "dashboard.json"
        self.account = (
            json.loads(self.account_path.read_text()) if self.account_path.exists() else {}
        )
        if not self.account.get("session_secret"):
            self.account["session_secret"] = secrets.token_urlsafe(32)
            atomic_write(self.account_path, json.dumps(self.account, indent=2))

    def has_account(self) -> bool:
        return bool(self.account.get("password"))

    def create_account(self, username: str, password: str) -> None:
        with self.lock:
            if self.has_account():
                raise ValueError("A dashboard login already exists")
            if len(password) < 8:
                raise ValueError("Choose a password of at least 8 characters")
            if not username.strip():
                raise ValueError("Choose a username")
            self.account.update({"username": username.strip(), "password": hash_password(password)})
            atomic_write(self.account_path, json.dumps(self.account, indent=2))

    def check_account(self, username: str, password: str) -> bool:
        stored = self.account
        if not stored.get("password"):
            return False
        return username.strip() == stored.get("username") and verify_password(
            password, stored["password"]
        )

    def issue_session(self, hours: int = 720) -> str:
        """A signed, expiring cookie value. No server-side session store to keep."""
        import hmac as _hmac
        import time as _time

        expires = int(_time.time() + hours * 3600)
        signature = _hmac.new(
            self.account["session_secret"].encode(), str(expires).encode(), "sha256"
        ).hexdigest()
        return f"{expires}.{signature}"

    def valid_session(self, value: str) -> bool:
        import hmac as _hmac
        import time as _time

        try:
            expires, signature = value.split(".", 1)
            if int(expires) < _time.time():
                return False
        except (ValueError, AttributeError):
            return False
        expected = _hmac.new(
            self.account["session_secret"].encode(), expires.encode(), "sha256"
        ).hexdigest()
        return _hmac.compare_digest(signature, expected)

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
