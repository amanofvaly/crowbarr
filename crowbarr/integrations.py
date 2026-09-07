from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from .config import Connection


def check_connection(name: str, connection: Connection) -> dict:
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
    return {"ok": True, "message": f"Connected to {name.title()}"}


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
