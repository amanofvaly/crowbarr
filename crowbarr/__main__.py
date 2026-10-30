from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
from pathlib import Path


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Crowbarr automated subtitle service")
    parser.add_argument("--host", default=os.environ.get("CROWBARR_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CROWBARR_PORT", "8449")))
    args = parser.parse_args()
    from . import updates

    # An update installed from inside the app takes over here, before anything is served.
    updates.boot(Path(os.environ.get("CROWBARR_DATA", "data")).resolve(), sys.argv[1:])
    import uvicorn

    config = uvicorn.Config(
        "crowbarr.app:create_app", factory=True, host=args.host, port=args.port,
        workers=1, timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    updates.RESTART.server = server
    server.run()
    if updates.RESTART.requested:
        updates.restart_into(sys.argv[1:])
    if not server.started:
        sys.exit(3)


if __name__ == "__main__":
    main()
