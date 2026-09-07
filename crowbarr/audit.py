"""Conservative timing evidence against ASR anchors, independent of repair timestamps.

This is an automated heuristic, not an independently annotated timing benchmark.
Unmatched dialogue is uncertainty, never evidence that the subtitle is synchronized.
"""

import math
from statistics import median

from .subtitles import Cue, Word, match_passages, tokens, validate_cues

AUDIT_VERSION = 2


def _distributed(evidence: list[dict], duration: float) -> bool:
    """Require long-form evidence to cover the timeline, not one convenient scene."""
    if duration < 120:
        return True
    buckets = {min(2, int(item["audio_start"] / duration * 3)) for item in evidence}
    return buckets == {0, 1, 2}


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
        probabilities = [w.probability for w in anchor_words if math.isfinite(w.probability)]
        confident = [probability for probability in probabilities if probability >= 0.8]
        # Isolated weak internal words are normal ASR noise. Boundary confidence and
        # an aggregate majority are what make this passage useful as a timing anchor.
        if (
            len(probabilities) != len(anchor_words)
            or not probabilities
            or probabilities[0] < 0.65
            or probabilities[-1] < 0.65
            or len(confident) < max(2, math.ceil(len(probabilities) * 0.6))
            or median(probabilities) < 0.8
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
    blocking_structural = [issue for issue in structural if "unsuitable reading duration" not in issue]
    dialogue_cues = sum(bool(tokens(cue.text)) for cue in cues)
    required_anchors = min(30, max(3, math.ceil(dialogue_cues * 0.1)))
    dialogue_coverage = len(evidence) / max(1, dialogue_cues)
    distribution_ok = _distributed(evidence, duration)
    sufficient = (
        bool(cues)
        and len(evidence) >= required_anchors
        and dialogue_coverage >= 0.1
        and match["matched_token_ratio"] >= 0.65
        and match["speech_words"] >= 12
        and distribution_ok
    )
    if not sufficient:
        decision, reason = (
            "inconclusive",
            "Insufficient confident coverage of subtitle cues or recognized dialogue",
        )
    else:
        outlier_ratio = sum(e["error_seconds"] > 1.0 for e in evidence) / len(evidence)
        if not blocking_structural and p95 <= 1.75 and outlier_ratio < 0.2:
            decision, reason = "pass", "Distributed audio anchors are within the timing error budget"
        elif p95 > 2.0 or outlier_ratio >= 0.2:
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
        "dialogue_cues": dialogue_cues,
        "dialogue_coverage": dialogue_coverage,
        "required_anchors": required_anchors,
        "distributed_across_timeline": distribution_ok,
        "matched_token_ratio": match["matched_token_ratio"],
        "median_error_seconds": median(errors) if errors else None,
        "p95_error_seconds": p95,
        "signed_offset_seconds": median(e["start_delta_seconds"] for e in evidence) if evidence else None,
        "tolerances": {"start_seconds": 0.75, "end_seconds": 1.0},
        "structural_issues": structural,
        "blocking_structural_issues": blocking_structural,
        "evidence": evidence,
    }
def improved(before: dict, after: dict) -> bool:
    if before["decision"] != "repair" or after["decision"] != "pass":
        return False
    # Compare the same authored cues; replacement text cannot game the score.
    if [e["cue"] for e in before["evidence"]] != [e["cue"] for e in after["evidence"]]:
        return False
    regressions = [
        candidate["error_seconds"] - original["error_seconds"]
        for original, candidate in zip(before["evidence"], after["evidence"], strict=True)
    ]
    # Noisy ASR boundaries can move slightly in either direction. Reject broad or
    # severe regressions, while allowing a small outlier budget when aggregates improve.
    if (
        sum(regression > 0.25 for regression in regressions) / len(regressions) > 0.1
        or max(regressions) > 0.75
    ):
        return False
    return (
        after["p95_error_seconds"] <= before["p95_error_seconds"] * 0.8
        and before["p95_error_seconds"] - after["p95_error_seconds"] >= 0.5
    )
