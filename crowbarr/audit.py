"""Conservative timing evidence against ASR anchors, independent of repair timestamps.

This is an automated heuristic, not an independently annotated timing benchmark.
Unmatched dialogue is uncertainty, never evidence that the subtitle is synchronized.
"""

import math
from statistics import median

from .subtitles import Cue, Word, match_passages, validate_cues

AUDIT_VERSION = 1


def audit(cues: list[Cue], words: list[Word], duration: float) -> dict:
    # Backends may return NumPy scalars; reports must contain native JSON values.
    cues = [Cue(float(c.start), float(c.end), c.text) for c in cues]
    words = [Word(float(w.start), float(w.end), w.text, float(w.probability)) for w in words]
    passages, match = match_passages(cues, words)
    evidence = []
    for passage in passages:
        if passage.source_index is None:
            continue
        cue = cues[passage.source_index]
        anchor_words = [w for w in words if w.start >= passage.start and w.end <= passage.end]
        if not anchor_words or any(
            not math.isfinite(w.probability) or w.probability < 0.8 for w in anchor_words
        ):
            continue
        start_delta = cue.start - passage.start
        end_delta = cue.end - passage.end
        evidence.append(
            {
                "cue": passage.source_index + 1,
                "subtitle_start": cue.start,
                "subtitle_end": cue.end,
                "audio_start": passage.start,
                "audio_end": passage.end,
                "start_delta_seconds": start_delta,
                "end_delta_seconds": end_delta,
                "error_seconds": max(abs(start_delta), abs(end_delta)),
                "within_tolerance": abs(start_delta) <= 0.75 and abs(end_delta) <= 1.0,
            }
        )
    errors = sorted(e["error_seconds"] for e in evidence)
    p95 = errors[math.ceil(len(errors) * 0.95) - 1] if errors else None
    structural = validate_cues(cues, duration)
    sufficient = bool(cues) and len(evidence) == len(cues) and match["generated_word_ratio"] <= 0.1
    # Very short tracks cannot establish reliable global timing from one phrase.
    sufficient = sufficient and len(evidence) >= 3 and match["speech_words"] >= 12
    if not sufficient:
        decision, reason = (
            "inconclusive",
            "Insufficient confident coverage of subtitle cues or recognized dialogue",
        )
    elif not structural and all(e["within_tolerance"] for e in evidence):
        decision, reason = "pass", "All supported cue boundaries are within timing tolerance"
    elif p95 > 2.0 or sum(e["error_seconds"] > 1.0 for e in evidence) / len(evidence) >= 0.2:
        decision, reason = "repair", "Confident audio anchors show substantial timing errors"
    else:
        decision, reason = (
            "inconclusive",
            "Timing differences are borderline or subtitle structure needs review",
        )
    return {
        "version": AUDIT_VERSION,
        "decision": decision,
        "reason": reason,
        "supported_cues": len(evidence),
        "total_cues": len(cues),
        "coverage": len(evidence) / max(1, len(cues)),
        "median_error_seconds": median(errors) if errors else None,
        "p95_error_seconds": p95,
        "signed_offset_seconds": median(e["start_delta_seconds"] for e in evidence) if evidence else None,
        "tolerances": {"start_seconds": 0.75, "end_seconds": 1.0},
        "structural_issues": structural,
        "evidence": evidence,
    }


def improved(before: dict, after: dict) -> bool:
    if before["decision"] != "repair" or after["decision"] != "pass":
        return False
    # Compare the same authored cues; replacement text cannot game the score.
    if [e["cue"] for e in before["evidence"]] != [e["cue"] for e in after["evidence"]]:
        return False
    if any(
        b["error_seconds"] > a["error_seconds"] + 0.25
        for a, b in zip(before["evidence"], after["evidence"], strict=True)
    ):
        return False
    return (
        after["p95_error_seconds"] <= before["p95_error_seconds"] * 0.5
        and before["p95_error_seconds"] - after["p95_error_seconds"] >= 0.5
    )
