from __future__ import annotations

import contextlib
import xml.etree.ElementTree as ET

import httpx

from .config import Connection


def _library_paths(name: str, connection, settings) -> str:
    """Say whether the media manager's own library paths reach Crowbarr's folders.

    An empty library is the usual symptom of two containers mounting the same media
    under different names, and it looks identical to having nothing to do. Asking the
    manager where its libraries are turns that into a specific instruction.
    """
    from .arr import map_path
    from .library import allowed

    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
        response = client.get(
            connection.url + "/api/v3/rootfolder", headers={"X-Api-Key": connection.api_key}
        )
        response.raise_for_status()
        folders = [str(item["path"]) for item in response.json() if item.get("path")]
    unreachable = []
    for folder in folders:
        try:
            local = map_path(folder, connection)
            if local.is_dir() and allowed(local, settings):
                continue
        except (ValueError, OSError):
            pass
        unreachable.append(folder)
    if not folders or not unreachable:
        return ""
    listed = ", ".join(unreachable)
    return (
        f" Crowbarr cannot use its library folders: {listed}. "
        "Open Settings → Media & discovery and use Find my media folders, then Test folders. "
        f"Current Crowbarr folders: {', '.join(settings.roots) or 'none'}. "
        "If the same folder has a different path in Crowbarr, add a path mapping in "
        f"Settings → Connections → {name.title()} → Path mappings."
    )



def check_connection(name: str, connection: Connection, settings=None) -> dict:
    paths = {
        "sonarr": "/api/v3/system/status",
        "radarr": "/api/v3/system/status",
        "bazarr": "/api/system/status",
        "plex": "/identity",
    }
    if name not in paths or not connection.url:
        raise ValueError("Configure a service URL first")
    headers = {"X-Plex-Token" if name == "plex" else "X-Api-Key": connection.api_key}
    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
        response = client.get(connection.url + paths[name], headers=headers)
        response.raise_for_status()
        if name == "plex":
            ET.fromstring(response.text)
        else:
            response.json()
    detail = ""
    if name in ("sonarr", "radarr") and settings is not None:
        with contextlib.suppress(Exception):
            detail = _library_paths(name, connection, settings)
    return {"ok": not detail, "message": f"Connected to {name.title()}.{detail}"}


def refresh_plex(connection: Connection) -> None:
    """Refresh movie/TV sections without assuming Crowbarr and Plex share container paths."""
    if not connection.url:
        return
    with httpx.Client(
        timeout=20, follow_redirects=False, trust_env=False, headers={"X-Plex-Token": connection.api_key}
    ) as client:
        response = client.get(connection.url + "/library/sections")
        response.raise_for_status()
        for section in ET.fromstring(response.text).findall("Directory"):
            if section.get("type") in {"movie", "show"}:
                key = section.get("key", "")
                if not key.isdigit():
                    continue
                client.get(connection.url + f"/library/sections/{key}/refresh").raise_for_status()


def plex_activity(connection: Connection) -> list[dict]:
    if not connection.url:
        return []
    with httpx.Client(timeout=5, trust_env=False, headers={"X-Plex-Token": connection.api_key}) as client:
        response = client.get(connection.url + "/status/sessions")
        response.raise_for_status()
        return [
            {"title": v.get("title"), "transcoding": v.find("TranscodeSession") is not None}
            for v in ET.fromstring(response.text).findall("Video")
        ]


def deliver_plex(
    connection: Connection, media: str, output: str, mappings: list, timeout: float = 30
) -> dict:
    """Find one exact media part, refresh it, and verify the sidecar stream metadata."""
    import hashlib
    import time
    from pathlib import Path, PurePosixPath

    mapped = media
    for mapping in sorted(mappings, key=lambda m: len(m.remote), reverse=True):
        if media.startswith(mapping.remote.rstrip("/") + "/"):
            mapped = mapping.local.rstrip("/") + media[len(mapping.remote) :]
            break
    expected_hash = hashlib.sha256(Path(output).read_bytes()).hexdigest()
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=10, trust_env=False, headers={"X-Plex-Token": connection.api_key}) as client:
        response = client.get(connection.url + "/library/sections")
        response.raise_for_status()
        item_key = None
        for section in ET.fromstring(response.text).findall("Directory"):
            if section.get("type") not in {"movie", "show"}:
                continue
            key = section.get("key", "")
            if not key.isdigit():
                continue
            catalog = client.get(
                connection.url + f"/library/sections/{key}/all",
                params={"type": 4 if section.get("type") == "show" else 1, "includeMedia": 1},
            )
            catalog.raise_for_status()
            for video in ET.fromstring(catalog.text).findall("Video"):
                if any(part.get("file") == mapped for part in video.findall("./Media/Part")):
                    item_key = video.get("ratingKey")
                    break
            if item_key:
                break
        if not item_key or not item_key.isdigit():
            return {
                "state": "pending",
                "reason": "Exact media path is not in Plex; check Plex path mappings",
                "media_path": mapped,
            }
        client.put(connection.url + f"/library/metadata/{item_key}/refresh").raise_for_status()
        while time.monotonic() < deadline:
            response = client.get(
                connection.url + f"/library/metadata/{item_key}", params={"includeMedia": 1}
            )
            response.raise_for_status()
            for part in ET.fromstring(response.text).findall("./Video/Media/Part"):
                if part.get("file") != mapped:
                    continue
                for stream in part.findall("Stream"):
                    if stream.get("streamType") != "3":
                        continue
                    stream_file = stream.get("file", "")
                    title = stream.get("title", "")
                    matches_output = (
                        PurePosixPath(stream_file).name == PurePosixPath(output).name
                        or "crowbarr" in title.lower()
                    )
                    stream_key = stream.get("key", "")
                    if (
                        not matches_output
                        and stream_key.startswith("/library/streams/")
                        and stream_key.removeprefix("/library/streams/").isdigit()
                    ):
                        content = client.get(connection.url + stream_key)
                        content.raise_for_status()
                        matches_output = hashlib.sha256(content.content).hexdigest() == expected_hash
                    if matches_output:
                        attrs = {
                            name: stream.get(name)
                            for name in (
                                "id",
                                "language",
                                "languageCode",
                                "title",
                                "forced",
                                "hearingImpaired",
                                "selected",
                            )
                        }
                        valid = (
                            attrs["languageCode"] in {"eng", "en"}
                            and attrs["forced"] in {None, "0"}
                            and attrs["hearingImpaired"] in {None, "0"}
                        )
                        return {
                            "state": "discovered" if valid else "review",
                            "rating_key": item_key,
                            "stream": attrs,
                        }
            time.sleep(2)
        return {
            "state": "pending",
            "rating_key": item_key,
            "reason": "Published on disk; Plex has not exposed the subtitle yet",
        }
