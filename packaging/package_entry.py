"""Frozen entry point, including packaged inference validation."""

import multiprocessing
import sys

from packaging_smoke import smoke

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if sys.argv[1:] == ["--packaging-smoke"]:
        smoke()
    else:
        from crowbarr.__main__ import main

        main()
