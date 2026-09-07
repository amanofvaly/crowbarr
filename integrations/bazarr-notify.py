#!/usr/bin/env python3
"""Optional Bazarr post-processing hook. The periodic scanner also discovers subtitle downloads.

Set CROWBARR_URL and CROWBARR_API_KEY in Bazarr's container environment, then set its
post-processing command to: python3 /hooks/bazarr-notify.py
No media paths or provider credentials leave Bazarr.
"""

import json
import os
import urllib.error
import urllib.request


def main():
    url = os.environ["CROWBARR_URL"].rstrip("/")
    request = urllib.request.Request(
        url + "/api/hooks/bazarr",
        data=json.dumps({"eventType": "Download"}).encode(),
        headers={"Content-Type": "application/json", "X-Api-Key": os.environ["CROWBARR_API_KEY"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            print(f"Crowbarr notified ({response.status})")
    except (urllib.error.URLError, TimeoutError):
        # The subtitle download should still succeed; reconciliation picks it up later.
        print("Crowbarr unavailable; its scheduled library check will discover this subtitle.")


if __name__ == "__main__":
    main()
