from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .arr import ArrClient
from .config import Settings, atomic_write
from .db import Database

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".webm"}


def allowed(path: Path, settings: Settings) -> bool:
    resolved = path.resolve()
    return any(resolved.is_relative_to(Path(root).resolve()) for root in settings.media_roots())


def subtitle_sources(media: Path, settings: Settings) -> list[Path]:
    candidates = []
    for path in media.parent.iterdir():
        if path.suffix.lower() != ".srt" or not path.name.startswith(media.stem + "."):
            continue
        suffix = path.name[len(media.stem) + 1 : -4].lower().split(".")
        if any(tag in suffix for tag in ("crowbarr", "forced")) or path.is_symlink():
            continue
        tagged = any(tag in suffix for tag in ("en", "eng"))
        if tagged:
            candidates.append(path)
    plain = media.with_suffix(".srt")
    if settings.allow_untagged_subtitles and plain.exists() and not plain.is_symlink():
        candidates.append(plain)
    return sorted(set(candidates), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)


def source_subtitle(media: Path, settings: Settings) -> Path | None:
    """Return the newest sidecar as a queue hint; processing arbitrates every source."""
    return next(iter(subtitle_sources(media, settings)), None)


def signature(
    media: Path,
    source: Path | None,
    settings: Settings,
    fingerprint: str = "",
    sources: list[Path] | None = None,
) -> str:
    stat = media.stat()
    data = [str(media.resolve()), stat.st_size, stat.st_mtime_ns, fingerprint or settings.fingerprint()]
    sources = list(sources) if sources is not None else subtitle_sources(media, settings)
    if source and source not in sources:
        sources.append(source)
    for candidate in sources:
        if candidate.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Subtitle exceeds 8 MiB limit")
        data.extend([str(candidate), hashlib.sha256(candidate.read_bytes()).hexdigest()])
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def retire_stale_output(media: Path, db: Database) -> None:
    """Remove only a verified Crowbarr-owned output when the actual video revision changes."""
    stat = media.stat()
    revision = f"{stat.st_size}:{stat.st_mtime_ns}"
    previous = db.video_revision(str(media))
    if previous and previous != revision:
        published = db.published(str(media))
        if published and published["output"]:
            output = Path(published["output"])
            if output.exists() and not output.is_symlink():
                content = output.read_bytes()
                if not db.owns_output(str(media), str(output), hashlib.sha256(content).hexdigest()):
                    raise ValueError(
                        "Video changed, but its Crowbarr subtitle was externally edited. Remove it before retrying."
                    )
                backup = db.path.parent / "backups" / f"retired-{published['id']}.srt"
                atomic_write(backup, content.decode("utf-8"))
                output.unlink()
                db.update(
                    published["id"],
                    state="superseded",
                    stage="",
                    error="Video replaced; previous subtitle retired",
                )
    db.set_video_revision(str(media), revision)


def suggested_roots(settings) -> dict:
    """Return manager paths with their local mappings and visible installation folders."""
    from .arr import map_path

    paths, folders, errors = [], [], []
    for provider in settings.providers():
        connection = getattr(settings, provider)
        try:
            with ArrClient(provider, connection) as client:
                roots = client.get_list("rootfolder")
            for item in roots:
                if not item.get("path"):
                    continue
                remote = str(item["path"])
                try:
                    local = str(map_path(remote, connection))
                except ValueError:
                    local = ""
                accessible = bool(local) and Path(local).is_dir()
                folders.append(dict(provider=provider, remote=remote, path=local, accessible=accessible))
                if accessible:
                    paths.append(local)
        except Exception:
            errors.append(f"Could not read {provider.title()} folders. Check Settings → Connections → "
                          f"{provider.title()}, then test the connection.")
    system = ("/proc", "/sys", "/dev", "/etc", "/run", "/tmp", "/var", "/usr", "/app", "/config", "/opt")
    mounts = []
    try:
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            point = parts[4]
            for encoded, decoded in ((r"\040", " "), (r"\011", "\t"), (r"\134", "\\")):
                point = point.replace(encoded, decoded)
            if point == "/" or any(point == prefix or point.startswith(prefix + "/") for prefix in system):
                continue
            if Path(point).is_dir():
                mounts.append(point)
    except OSError:
        pass
    return {"source": "managers" if folders else "mounts" if mounts else "none",
            "paths": sorted(set(paths)), "folders": folders, "mounts": sorted(set(mounts)), "errors": errors}


def check_media_folder(raw: str) -> dict:
    """Probe the chosen directory without touching existing media."""
    import tempfile

    path = Path(raw)
    result = {"path": raw, "ok": False}
    if not raw or not path.is_absolute():
        return {**result, "message": "Enter a full folder path, starting with / on Linux."}
    try:
        if not path.exists():
            return {**result, "message": "Folder not found. Use ‘Where do I find this path?’ below to "
                    "check the folder shared with Crowbarr."}
        if not path.is_dir():
            return {**result, "message": "This is a file. Enter the folder containing your videos."}
        with os.scandir(path) as entries:
            next(entries, None)
    except OSError:
        return {**result, "message": "Cannot read this folder. Give the Crowbarr user read and browse permission."}
    try:
        with tempfile.TemporaryFile(prefix=".crowbarr-folder-test-", dir=path) as probe:
            probe.write(b"Crowbarr folder test")
            probe.flush()
    except OSError:
        return {**result, "message": "Cannot write here. Enable write access for Crowbarr in the folder’s "
                "permissions and turn off Read Only in its storage settings."}
    return {**result, "ok": True, "message": "Can read this folder and create a file here. "
            "Subfolders may have different permissions."}


def prepare_media(media: Path, settings: Settings, db: Database, now: float) -> tuple:
    if not allowed(media, settings):
        raise ValueError("Path is outside Crowbarr's media folders; configure a path mapping")
    if not media.is_file():
        raise ValueError("Imported file is not visible to Crowbarr; check its mount and path mapping")
    if media.is_symlink() or media.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError("Unsupported media file")
    retire_stale_output(media, db)
    sources = subtitle_sources(media, settings)
    source = next(iter(sources), None)
    latest_write = max([media.stat().st_mtime, *(candidate.stat().st_mtime for candidate in sources)])
    sig = signature(media, source, settings, sources=sources)
    first_seen = db.observe(str(media), sig, now)
    stable_at = max(first_seen, latest_write) + settings.settle_seconds
    ready = max(stable_at, first_seen + (0 if source else settings.subtitle_wait_minutes * 60))
    return source, sources, sig, ready


def queue_media(
    media: Path,
    settings: Settings,
    db: Database,
    now: float,
    origin: str = "backlog",
    carried: str = "",
    prepared: tuple | None = None,
) -> bool:
    if media.is_symlink() or media.suffix.lower() not in VIDEO_EXTENSIONS:
        return False
    source, sources, sig, ready = prepared or prepare_media(media, settings, db, now)
    if carried:
        # Only the settings changed, and the user asked to keep what is already decided.
        db.restamp(str(media), signature(media, source, settings, carried, sources), sig)
    job_id = db.enqueue(str(media), sig, str(source) if source else None, ready, origin=origin)
    job = db.get(job_id)
    if job["state"] == "completed" and job["output"] and not Path(job["output"]).exists():
        db.update(job_id, state="queued", ready=now, attempts=0, error="Published subtitle is missing")
    return True


def prepare_and_restore(
    paths: list[Path], settings: Settings, db: Database, now: float
) -> tuple[dict[Path, tuple], dict[Path, str], set[str]]:
    """Prepare scan inputs once and restore every exact prior result in one commit."""
    prepared: dict[Path, tuple] = {}
    errors: dict[Path, str] = {}
    for media in paths:
        # Sonarr and Radarr can legitimately report links or non-video extras. They are
        # outside Crowbarr's work set, just as they are during direct folder discovery.
        if media.is_symlink() or media.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        try:
            prepared[media] = prepare_media(media, settings, db, now)
        except (OSError, ValueError) as error:
            errors[media] = str(error)
    restored = db.restore_results(
        [(str(path), item[2], str(item[0]) if item[0] else None) for path, item in prepared.items()]
    )
    return prepared, errors, restored


def scan_arr(settings: Settings, db: Database, client_factory=ArrClient) -> int:
    total, seen = 0, set()
    carried, carry_token = db.carried_fingerprint(settings.fingerprint())
    catalogs = []
    # Validate every provider before walking thousands of files. Otherwise a slow
    # Sonarr filesystem pass holds fresh Radarr imports behind an unhealthy gate.
    for provider in settings.providers():
        try:
            with client_factory(provider, getattr(settings, provider)) as client:
                files = client.catalog()
            db.replace_catalog(provider, [file.record() for file in files], settings.discovery_fingerprint())
        except Exception as error:
            message = f"{provider.title()} library sync failed ({type(error).__name__}). Check its URL, API key, and path mappings."
            db.sync_failed(provider, message)
            db.notice(provider, message)
            continue
        catalogs.append((provider, files))
    paths = list(dict.fromkeys(Path(file.path) for _, files in catalogs for file in files))
    prepared, prepare_errors, restored = prepare_and_restore(paths, settings, db, time.time())
    for provider, files in catalogs:
        errors = []
        for file in files:
            if file.path in seen:
                continue
            try:
                path = Path(file.path)
                if path.is_symlink() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                    seen.add(file.path)
                    continue
                item = prepared.get(path)
                if item is None:
                    raise ValueError(prepare_errors.get(path, "Crowbarr could not inspect this file"))
                if queue_media(
                    Path(file.path), settings, db, time.time(), carried=carried, prepared=item
                ):
                    seen.add(file.path)
                    total += 1
            except (OSError, ValueError) as error:
                errors.append(f"{file.remote_path}: {error}")
        db.notice(provider, "; ".join(errors[:5]))
    db.discard_unmanaged(settings.providers())
    if len(catalogs) == len(settings.providers()):
        db.finish_carry_forward(carry_token)
    if restored:
        db.notice("results", f"Restored {len(restored)} exact previous results without processing.")
    else:
        db.notice("results", "")
    return total


def scan(settings: Settings, db: Database) -> int:
    if settings.providers():
        return scan_arr(settings, db)
    total = 0
    now = time.time()
    carried, carry_token = db.carried_fingerprint(settings.fingerprint())
    discovered: list[Path] = []
    root_errors: dict[str, list[str]] = {}
    incomplete_roots: set[str] = set()
    complete = True
    for root in settings.roots:
        if not Path(root).is_dir():
            db.notice(root, f"Library is unavailable: {root}. Check the media mount.")
            complete = False
            continue
        errors = root_errors.setdefault(root, [])

        def on_error(error, errors=errors, root=root):
            errors.append(str(error))
            incomplete_roots.add(root)

        for directory, dirs, files in os.walk(root, followlinks=False, onerror=on_error):
            dirs[:] = [d for d in dirs if not d.startswith(".") and not Path(directory, d).is_symlink()]
            for name in files:
                media = Path(directory, name)
                if media.suffix.lower() not in VIDEO_EXTENSIONS or media.is_symlink() or name.startswith("."):
                    continue
                discovered.append(media)
    prepared, prepare_errors, restored = prepare_and_restore(discovered, settings, db, now)
    for media, error in prepare_errors.items():
        root = next((item for item in settings.roots if media.is_relative_to(item)), settings.roots[0])
        root_errors.setdefault(root, []).append(f"{media.name}: {error}")
    for media in discovered:
        try:
            item = prepared.get(media)
            if item is not None:
                total += int(queue_media(media, settings, db, now, carried=carried, prepared=item))
        except (OSError, ValueError) as error:
            root = next((item for item in settings.roots if media.is_relative_to(item)), settings.roots[0])
            root_errors.setdefault(root, []).append(f"{media.name}: {error}")
    for root, errors in root_errors.items():
        db.notice(root, "; ".join(errors[:5]))
    complete = complete and not incomplete_roots
    db.notice("scan", "")
    if complete:
        db.finish_carry_forward(carry_token)
    if restored:
        db.notice("results", f"Restored {len(restored)} exact previous results without processing.")
    else:
        db.notice("results", "")
    return total
