"""Bazarr 1.x API adapter. Search metadata is separate from media publication."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import httpx

from .config import atomic_write

# A Bazarr provider search asks every enabled provider and routinely runs for minutes.
# A job waits this long for it; past that it hands its slot back and finds the result
# cached on its next pass instead of keeping a worker idle.
SEARCH_WAIT_SECONDS = 20
SEARCH_CACHE_SECONDS = 6 * 3600
SEARCH_FAILURE_CACHE_SECONDS = 3600
# Bazarr's Whisper "provider" transcribes the same audio Crowbarr already transcribes, and
# a download makes Bazarr run the whole transcription. It is not an authored alternative.
GENERATED_PROVIDERS = {"whisperai"}

_searches: dict[str, threading.Thread] = {}
_searches_lock = threading.Lock()


class BazarrClient:
    def __init__(self, connection):
        self.connection = connection
        self.client = httpx.Client(timeout=180, trust_env=False, headers={"X-Api-Key": connection.api_key})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def history(self, kind, item_id):
        key = "episodeid" if kind == "episodes" else "radarrid"
        response = self.client.get(
            self.connection.url + f"/api/{kind}/history", params={key: item_id, "length": 25}
        )
        response.raise_for_status()
        return response.json()["data"]

    def search(self, kind, item_id):
        key = "episodeid" if kind == "episodes" else "radarrid"
        response = self.client.get(self.connection.url + f"/api/providers/{kind}", params={key: item_id})
        response.raise_for_status()
        return response.json()["data"]

    def download(self, kind, item_id, candidate, series_id=None):
        payload = {
            "episodeid" if kind == "episodes" else "radarrid": item_id,
            "hi": str(candidate.get("hearing_impaired", "False")),
            "forced": str(candidate.get("forced", "False")),
            "original_format": str(candidate.get("original_format", "False")),
            "provider": candidate["provider"],
            "subtitle": candidate["subtitle"],
        }
        if kind == "episodes":
            payload["seriesid"] = series_id
        response = self.client.post(self.connection.url + f"/api/providers/{kind}", data=payload)
        response.raise_for_status()


def target(settings, db, media):
    with db.connect() as connection:
        record = connection.execute("SELECT * FROM managed_media WHERE path=?", (media,)).fetchone()
    if not record:
        return None
    if record["provider"] == "radarr":
        return {"kind": "movies", "item_id": record["item_id"]}
    from .arr import ArrClient

    with ArrClient("sonarr", settings.sonarr) as client:
        episodes = client.get_list("episode", seriesId=record["item_id"])
    matches = [episode for episode in episodes if episode.get("episodeFileId") == record["file_id"]]
    if not matches:
        return None
    return {"kind": "episodes", "item_id": matches[0]["id"], "series_id": record["item_id"]}


def inspect_sources(settings, db, media, directory: Path, job_id: int):
    identified = target(settings, db, media)
    if not identified:
        return {"state": "unmapped", "reason": "No managed Sonarr/Radarr item maps to this media"}
    with BazarrClient(settings.bazarr) as client:
        history = client.history(identified["kind"], identified["item_id"])
        candidates = client.search(identified["kind"], identified["item_id"])
    payload = {
        "state": "alternatives_available" if candidates else "exhausted",
        "target": identified,
        "history": history,
        "candidates": candidates,
        "searched_at": time.time(),
    }
    atomic_write(directory / "providers" / f"{job_id}.json", json.dumps(payload))
    return payload


def _search_cache(directory, key) -> Path:
    return Path(directory) / "providers" / f"search-{key}.json"


def cached_search(directory, key):
    path = _search_cache(directory, key)
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    limit = SEARCH_FAILURE_CACHE_SECONDS if payload.get("state") == "failed" else SEARCH_CACHE_SECONDS
    return payload if time.time() - payload.get("searched_at", 0) < limit else None


def _run_search(settings, db, media, directory, job_id, key):
    try:
        payload = inspect_sources(settings, db, media, directory, job_id)
    except Exception as error:  # recorded so the next attempt sees it without searching again
        payload = {"state": "failed", "error": f"{type(error).__name__}: {error}"[:300]}
    payload.setdefault("searched_at", time.time())
    atomic_write(_search_cache(directory, key), json.dumps(payload))


def search_sources(settings, db, media, directory, job_id, key, wait=None):
    """Provider search results for this media, or None while a slow search still runs."""
    payload = cached_search(directory, key)
    if payload is not None:
        return payload
    with _searches_lock:
        thread = _searches.get(key)
        if thread is None or not thread.is_alive():
            thread = threading.Thread(
                target=_run_search, args=(settings, db, media, directory, job_id, key),
                name=f"bazarr-search-{key[:8]}", daemon=True,
            )
            _searches[key] = thread
            thread.start()
    thread.join(SEARCH_WAIT_SECONDS if wait is None else wait)
    if thread.is_alive():
        return None
    return cached_search(directory, key)


def reject_source(directory, media, source, reason):
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    path = directory / "rejections" / f"{hashlib.sha256(media.encode()).hexdigest()}.json"
    record = json.loads(path.read_text()) if path.exists() else {}
    record[digest] = {"reason": reason, "source": source.name, "time": time.time()}
    atomic_write(path, json.dumps(record))
    return digest


def try_alternative(settings, db, media, directory, job_id):
    """Bound provider retries and preserve every existing provider sidecar before upgrades."""
    from .library import subtitle_sources

    key = hashlib.sha256(str(media).encode()).hexdigest()
    ledger_path = directory / "providers" / f"attempts-{key}.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else []
    payload = search_sources(settings, db, str(media), directory, job_id, key)
    if payload is None:
        return {"state": "searching", "reason": "Bazarr is still searching its providers"}
    if payload["state"] == "failed":
        raise RuntimeError(f"Bazarr provider search failed ({payload.get('error', 'unknown error')})")
    if payload["state"] == "unmapped":
        return payload
    eligible = [
        candidate
        for candidate in payload["candidates"]
        if candidate.get("language") == settings.language
        and str(candidate.get("forced", "False")).lower() == "false"
        and candidate.get("provider") not in GENERATED_PROVIDERS
    ]
    remaining = [
        candidate
        for candidate in eligible
        if hashlib.sha256(candidate["subtitle"].encode()).hexdigest() not in ledger
    ]
    if not remaining or len(ledger) >= settings.max_provider_attempts:
        return {"state": "exhausted", "attempts": len(ledger), "alternatives_seen": len(eligible)}
    if not settings.bazarr_download_alternatives:
        return {
            "state": "available",
            "alternatives": len(remaining),
            "reason": "Enable provider upgrades or select an alternative in Bazarr",
        }
    selected = max(remaining, key=lambda candidate: candidate.get("score", 0))
    # Bazarr performs the download using its configured provider permissions. It
    # can replace its own sidecar; retain original bytes privately before asking.
    before = {str(source): hashlib.sha256(source.read_bytes()).hexdigest()
              for source in subtitle_sources(media, settings)}
    for source in subtitle_sources(media, settings):
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        atomic_write(directory / "provider-backups" / key / f"{digest}-{source.name}", content)
    ledger.append(hashlib.sha256(selected["subtitle"].encode()).hexdigest())
    atomic_write(ledger_path, json.dumps(ledger))  # journal before external side effect
    identified = payload["target"]
    with BazarrClient(settings.bazarr) as client:
        client.download(identified["kind"], identified["item_id"], selected, identified.get("series_id"))
    after = {str(source): hashlib.sha256(source.read_bytes()).hexdigest()
             for source in subtitle_sources(media, settings)}
    changed = any(before.get(path) != digest for path, digest in after.items())
    return {
        "state": "downloaded" if changed else "discarded",
        "reason": "Subtitle bytes changed; awaiting audio audit" if changed else
                  "Bazarr accepted the request but left no new or changed subtitle",
        "provider": selected["provider"],
        "score": selected.get("score"),
        "release_info": selected.get("release_info"),
        "attempts": len(ledger),
    }
