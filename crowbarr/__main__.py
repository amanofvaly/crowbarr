from __future__ import annotations

import argparse
import multiprocessing
import os


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Crowbarr automated subtitle service")
    parser.add_argument("--host", default=os.environ.get("CROWBARR_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CROWBARR_PORT", "8449")))
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(
        "crowbarr.app:create_app", factory=True, host=args.host, port=args.port,
        workers=1, timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
