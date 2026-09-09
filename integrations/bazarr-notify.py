#!/usr/bin/env python3
"""Bazarr post-processing hook: tell Crowbarr which subtitle Bazarr just wrote.

Bazarr runs this after it downloads or syncs a subtitle, which is the moment Crowbarr
needs to audit that episode. Without the media path Crowbarr can only fall back to a
full library reconciliation, so pass Bazarr's variables through:

    python3 /config/bazarr-notify.py "{{episode}}" "{{subtitles}}" "{{provider}}" "{{score}}"

Configure CROWBARR_URL and CROWBARR_API_KEY in Bazarr's environment, or place them in a
crowbarr-notify.json file beside this script. No provider credentials leave Bazarr.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


def settings():
    url, key = os.environ.get("CROWBARR_URL"), os.environ.get("CROWBARR_API_KEY")
    if not (url and key):
        beside = pathlib.Path(__file__).with_name("crowbarr-notify.json")
        if beside.exists():
            stored = json.loads(beside.read_text())
            url, key = url or stored.get("url"), key or stored.get("api_key")
    if not (url and key):
        raise SystemExit("Crowbarr URL and API key are not configured")
    return url.rstrip("/"), key


def main():
    url, key = settings()
    arguments = sys.argv[1:]

    def argument(index):
        value = arguments[index] if len(arguments) > index else ""
        # Bazarr leaves an unresolved variable in place when it has no value for it.
        return "" if value.startswith("{{") else value

    payload = {
        "eventType": "Download",
        "path": argument(0),
        "subtitles": argument(1),
        "provider": argument(2),
        "score": argument(3),
    }
    request = urllib.request.Request(
        url + "/api/hooks/bazarr",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Api-Key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            print(f"Crowbarr notified ({response.status}) for {payload['path'] or 'library'}")
    except (urllib.error.URLError, TimeoutError):
        # The subtitle download must still succeed; reconciliation picks it up later.
        print("Crowbarr unavailable; its scheduled library check will discover this subtitle.")


if __name__ == "__main__":
    main()
