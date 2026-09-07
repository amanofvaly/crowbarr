"""Read-only v3 API catalog adapters for Sonarr and Radarr."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path, PureWindowsPath

import httpx

from .config import ArrConnection


@dataclass(frozen=True)
class ManagedFile:
    provider: str
    file_id: int
    item_id: int
    path: str
    remote_path: str
    title: str

    def record(self) -> dict:
        return asdict(self)


def map_path(remote: str, connection: ArrConnection) -> Path:
    normalized = remote.replace("\\", "/")
    if ".." in normalized.split("/"):
        raise ValueError("Remote media path contains parent traversal")
    windows = bool(PureWindowsPath(remote).drive)
    comparison = normalized.casefold() if windows else normalized
    for mapping in sorted(connection.mappings, key=lambda m: len(m.remote), reverse=True):
        prefix = mapping.remote.casefold() if windows else mapping.remote
        boundary = prefix.rstrip("/") + "/"
        if comparison.startswith(boundary):
            relative = normalized[len(boundary) :]
            result = (Path(mapping.local) / relative).resolve()
            if not result.is_relative_to(Path(mapping.local).resolve()):
                raise ValueError("Mapped media escapes its configured directory")
            return result
    if windows:
        raise ValueError("Windows media paths need a path mapping")
    if not Path(remote).is_absolute():
        raise ValueError("API returned a relative media path without a library directory")
    return Path(remote).resolve()


def _remote_path(item: dict, file: dict) -> str:
    if file.get("path"):
        return file["path"]
    if item.get("path") and file.get("relativePath"):
        return item["path"].replace("\\", "/").rstrip("/") + "/" + file["relativePath"].replace("\\", "/")
    raise ValueError("Imported media has no file path in the API response")


class ArrClient:
    def __init__(self, provider: str, connection: ArrConnection, transport=None):
        if provider not in {"sonarr", "radarr"}:
            raise ValueError("Unknown arr service")
        if not connection.url or not connection.api_key:
            raise ValueError("Both service URL and API key are required for library sync")
        self.provider, self.connection = provider, connection
        self.client = httpx.Client(
            timeout=20,
            follow_redirects=False,
            trust_env=False,
            headers={"X-Api-Key": connection.api_key},
            transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def get_list(self, endpoint: str, **params) -> list[dict]:
        response = self.client.get(self.connection.url + "/api/v3/" + endpoint, params=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("Unexpected arr API response; expected a list of records")
        return data

    def catalog(self) -> list[ManagedFile]:
        return self._sonarr() if self.provider == "sonarr" else self._radarr()

    def _file(self, item: dict, file: dict) -> ManagedFile:
        remote = _remote_path(item, file)
        return ManagedFile(
            self.provider,
            int(file["id"]),
            int(item["id"]),
            str(map_path(remote, self.connection)),
            remote,
            item.get("title", ""),
        )

    def _radarr(self) -> list[ManagedFile]:
        result = []
        for movie in self.get_list("movie"):
            if self.connection.monitored_only and not movie.get("monitored", False):
                continue
            if not movie.get("hasFile", False):
                continue
            file = movie.get("movieFile")
            if not isinstance(file, dict) or not file.get("id"):
                raise ValueError("Radarr reports an imported movie without movieFile metadata")
            result.append(self._file(movie, file))
        return result

    def _sonarr(self) -> list[ManagedFile]:
        result = []
        for series in self.get_list("series"):
            if self.connection.monitored_only and not series.get("monitored", False):
                continue
            files = self.get_list("episodefile", seriesId=series["id"])
            eligible_ids = None
            if self.connection.monitored_only and files:
                episodes = self.get_list("episode", seriesId=series["id"])
                eligible_ids = {
                    e["episodeFileId"]
                    for e in episodes
                    if e.get("monitored", False) and e.get("hasFile", False) and e.get("episodeFileId")
                }
            # Multi-episode releases are represented once by episode-file ID.
            seen = set()
            for file in files:
                file_id = file["id"]
                if file_id in seen or (eligible_ids is not None and file_id not in eligible_ids):
                    continue
                seen.add(file_id)
                result.append(self._file(series, file))
        return result
