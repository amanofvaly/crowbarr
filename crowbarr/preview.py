"""Report what the proposed audit policy would decide. Reads the library and changes nothing.

Run it against a copy of ``crowbarr.db``. It writes only inside ``--out``. No job, subtitle,
or transcript cache of the running application is touched, so ``AUDIT_POLICY_VERSION``
does not change.
"""

import argparse
import json
import os
import shutil
import sqlite3
from pathlib import Path

from .audit import audit, section_coverage
from .config import Settings
from .subtitles import Word, parse_srt

PREVIEWED_STATES = ("review", "unchanged", "failed")
LANGUAGE_STOP = "Audio language"


def proposed_verdict(audit_decision: str, flagged: bool) -> str:
    """Combine the audit with the bad-section check. Repairs stay held for review."""
    if audit_decision in ("mismatched", "different_cut"):
        return audit_decision
    if flagged:
        return "damaged"
    return "repair_held" if audit_decision == "repair" else audit_decision


def listen(media: Path, stream_index: int, settings: Settings, models: Path, work: Path):
    """Recognize one audio track with the voice filter off."""
    from .inference import transcribe_bounded
    from .media import extract_audio, probe

    metadata = probe(media)
    stream = next(s for s in metadata["streams"] if s["index"] == stream_index)
    offset, duration = extract_audio(media, work / "audio.wav", metadata, stream)
    words, issues = transcribe_bounded(work / "audio.wav", settings, models, work / "chunks", speech_filter=False)
    return words, issues, offset, duration


def read_embedded(media: Path, settings: Settings, work: Path) -> str:
    from .media import embedded_subtitles, probe

    return embedded_subtitles(media, work, probe(media), settings)[0].read_text(encoding="utf-8", errors="replace")


def _subtitle_text(media: Path, report: dict, settings, work: Path, embedded) -> str:
    if report.get("selected_source_kind") == "embedded":
        return embedded(media, settings, work)
    return Path(report["selected_source"]).read_text(encoding="utf-8", errors="replace")


def _words(job_id: int, media: Path, stream: int, out: Path, hear, settings, models, work: Path):
    saved = out / "words" / f"{job_id}.json"
    if saved.exists():
        payload = json.loads(saved.read_text())
    else:
        words, issues, offset, duration = hear(media, stream, settings, models, work)
        payload = {
            "words": [vars(word) for word in words],
            "issues": issues,
            "offset": offset,
            "duration": duration,
        }
        saved.parent.mkdir(parents=True, exist_ok=True)
        partial = saved.with_suffix(".part")
        partial.write_text(json.dumps(payload))
        os.replace(partial, saved)
    return payload


def _judge(job_id: int, media: Path, report: dict, out: Path, hear, settings, models, embedded) -> dict:
    work = out / "work" / str(job_id)
    work.mkdir(parents=True, exist_ok=True)
    try:
        cues = parse_srt(_subtitle_text(media, report, settings, work, embedded))
        payload = _words(
            job_id, media, report["audio_selection"]["stream"], out, hear, settings, models, work
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    offset, duration = payload["offset"], payload["duration"]
    words = [
        Word(w["start"] + offset, w["end"] + offset, w["text"], w["probability"]) for w in payload["words"]
    ]
    verdict = audit(cues, words, duration)
    section = section_coverage(cues, words, duration)
    return {
        "proposed": proposed_verdict(verdict["decision"], section["flagged"]),
        "audit": {
            key: verdict[key]
            for key in ("decision", "reason", "matched_token_ratio", "coverage_checks", "checks", "cut_checks")
        },
        "section": section,
        "recognized_words": len(words),
    }


def run(db: Path, out: Path, listen=listen, settings=None, models=None, embedded=read_embedded) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    marks = ",".join("?" for _ in PREVIEWED_STATES)
    jobs = connection.execute(
        f"select id, media, state, error, report from jobs where state in ({marks}) order by id",  # noqa: S608
        PREVIEWED_STATES,
    ).fetchall()
    connection.close()
    results = []
    for job_id, media, state, error, stored in jobs:
        report = json.loads(stored) if stored else {}
        result = {
            "id": job_id,
            "media": Path(media).name,
            "state": state,
            "current_decision": (report.get("audit") or {}).get("before", {}).get("decision"),
            "current_match": report.get("matched_token_ratio"),
        }
        if (error or "").startswith(LANGUAGE_STOP):
            result.update(proposed="language_stop", error=error)
        elif not report.get("selected_source_kind"):
            result.update(proposed="no_subtitle", error=error)
        else:
            try:
                result.update(_judge(job_id, Path(media), report, out, listen, settings, models, embedded))
            except Exception as failure:  # noqa: BLE001
                # One unreadable file must not lose the rest of an hours-long run.
                result.update(proposed="error", error=f"{type(failure).__name__}: {failure}")
        results.append(result)
        partial = out / "preview.part"
        partial.write_text(json.dumps(results, indent=1))
        os.replace(partial, out / "preview.json")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="a copy of crowbarr.db")
    parser.add_argument("--settings", type=Path, required=True, help="settings.json")
    parser.add_argument("--models", type=Path, required=True, help="model cache folder")
    parser.add_argument("--out", type=Path, required=True, help="folder for the report")
    args = parser.parse_args()
    settings = Settings(**json.loads(args.settings.read_text()))
    for result in run(args.db, args.out, settings=settings, models=args.models):
        print(result["id"], result["proposed"], result["media"], flush=True)


if __name__ == "__main__":
    main()
