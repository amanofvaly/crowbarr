"""Split a full inference lock into the image runtime and the refinement add-on.

`compile_locks.py` resolves everything once, so every shared package keeps one version.
This module then moves the packages that exist only for WhisperX refinement into their
own lock, which Crowbarr installs on request instead of shipping in the image.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOTS = frozenset({"whisperx", "torch", "torchaudio", "transformers"})
ENTRY = re.compile(r"\n(?=[A-Za-z0-9_.\-]+(?:==| @ ))")
VIA = re.compile(r"^    # via(?: (?P<one>.+))?\n(?P<rest>(?:    #   .+\n)*)", re.MULTILINE)


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def name_of(entry: str) -> str:
    return canonical(re.match(r"[A-Za-z0-9_.\-]+", entry).group(0))


def via_of(entry: str) -> list[str]:
    found = VIA.search(entry)
    if not found:
        return []
    names = [found["one"]] if found["one"] else []
    names += [line.strip()[1:].strip() for line in found["rest"].splitlines()]
    return names


def without_via(entry: str, dropped: set[str]) -> str:
    """Rewrite the `# via` comment without the packages that moved out."""
    found = VIA.search(entry)
    if not found:
        return entry
    names = [n for n in via_of(entry) if canonical(n) not in dropped]
    if not names:
        return entry
    if len(names) == 1:
        block = f"    # via {names[0]}\n"
    else:
        block = "    # via\n" + "".join(f"    #   {n}\n" for n in names)
    return entry[: found.start()] + block + entry[found.end() :]


def split(text: str, roots: frozenset[str] = ROOTS) -> tuple[str, str]:
    first = ENTRY.search(text).end()
    head, body = text[:first], text[first:]
    entries = [e.rstrip("\n") + "\n" for e in ENTRY.split(body)]
    via = {name_of(e): {canonical(v) for v in via_of(e)} for e in entries}
    moved = set(roots)
    changed = True
    while changed:
        changed = False
        for name, parents in via.items():
            if name not in moved and parents and parents <= moved and parents & moved:
                moved.add(name)
                changed = True
    core = [without_via(e, moved) for e in entries if name_of(e) not in moved]
    # The CPU and CUDA inputs differ in name only, so name them alike and the two compare equal.
    addon = [
        re.sub(r"-r packaging/inference/\w+\.in", "-r packaging/inference/*.in", e)
        for e in entries
        if name_of(e) in moved
    ]
    banner = (
        "# Optional WhisperX refinement software, installed on request into the data directory.\n"
        "# These are the exact resolutions from the image locks, so versions never drift from them.\n"
        "# Regenerate with packaging/inference/compile_locks.py.\n"
    )
    index = "--index-url https://pypi.org/simple\n\n"
    return head + "".join(core), banner + index + "".join(addon)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("full", type=Path)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--refinement", type=Path)
    args = parser.parse_args()
    core, addon = split(args.full.read_text())
    args.core.write_text(core)
    if args.refinement:
        args.refinement.write_text(addon)


if __name__ == "__main__":
    main()
