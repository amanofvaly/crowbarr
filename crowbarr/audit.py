"""Conservative timing evidence against ASR anchors, independent of repair timestamps.

This is an automated heuristic, not an independently annotated timing benchmark.
Unmatched dialogue is uncertainty, never evidence that the subtitle is synchronized.
"""

import math
from statistics import median

from .subtitles import Cue, Word, match_passages, tokens, validate_cues


def _policy_signature() -> str:
    """Identify the decision policy by the released version.

    Publishing a release is what changes verdicts for people who install it, so that is
    what re-evaluates their library -- automatically, with nobody bumping a separate
    constant. Deriving it from the source instead would re-check every file on any edit,
    including ones that cannot change a decision.
    """
    from . import __version__

    return __version__


AUDIT_VERSION = _policy_signature()


def _distributed(evidence: list[dict], duration: float) -> bool:
    """Require long-form evidence to cover the timeline, not one convenient scene."""
    if duration < 120:
        return True
    buckets = {min(2, int(item["audio_start"] / duration * 3)) for item in evidence}
    return buckets == {0, 1, 2}


def _fit_start(evidence: list[dict]) -> dict:
    """Robust affine fit of authored cue starts onto recognized speech starts.

    Retiming can only shift and scale. Whatever this fit cannot remove is not a
    timing fault the app is able to repair, so the decision must not call it one.
    """
    points = sorted((e["subtitle_start"], e["audio_start"]) for e in evidence)
    sample = (
        points if len(points) <= 120 else [points[round(i * (len(points) - 1) / 119)] for i in range(120)]
    )
    slopes = [
        (by - ay) / (bx - ax)
        for i, (ax, ay) in enumerate(sample)
        for bx, by in sample[i + 1 :]
        if bx - ax >= 30
    ]
    scale = median(slopes) if slopes else 1.0
    if not 0.9 <= scale <= 1.1:
        scale = 1.0
    offset = median(y - scale * x for x, y in points)
    residuals = sorted(abs(y - scale * x - offset) for x, y in points)
    return {
        "scale": scale,
        "offset_seconds": offset,
        "residual_median_seconds": median(residuals),
        "residual_p95_seconds": residuals[math.ceil(len(residuals) * 0.95) - 1],
        # How much of the evidence disagrees, rather than how badly the worst point does.
        # A handful of mismatched anchors is normal; a substantial minority is a fault.
        "residual_outlier_ratio": sum(value > 1.5 for value in residuals) / len(residuals),
    }


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
    # required_anchors is deliberately capped at 30: that many confident, distributed
    # anchors already prove a global offset. A percentage floor on top of it silently
    # raises the bar for long content -- a feature film needs 155 anchors to clear 10%
    # -- so let a comfortable absolute count satisfy it instead.
    sufficient = (
        bool(cues)
        and len(evidence) >= required_anchors
        and (dialogue_coverage >= 0.1 or len(evidence) >= 2 * required_anchors)
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
        percentile = math.ceil(len(evidence) * 0.95) - 1
        # Synchronisation lives in the cue starts. A cue's end is a reading-time
        # decision, so lingering past speech is authoring, not timing error; only a
        # cue cut off before its speech finishes is a fault.
        start_median = median(e["start_delta_seconds"] for e in evidence)
        start_errors = sorted(abs(e["start_delta_seconds"]) for e in evidence)
        start_p95 = start_errors[percentile]
        start_outlier_ratio = sum(abs(e["start_delta_seconds"]) > 1.0 for e in evidence) / len(evidence)
        truncation = sorted(max(0.0, -e["end_delta_seconds"]) for e in evidence)[percentile]
        fit = _fit_start(evidence)
        # What a shift-and-scale repair would leave behind.
        residual_p95 = fit["residual_p95_seconds"]
        aligned = abs(start_median) <= 0.5 and start_p95 <= 1.25 and truncation <= 1.0
        # An identity transform means the cues already sit on the dialogue; the
        # remaining spread is anchor noise that retiming cannot and must not chase.
        near_identity = abs(fit["offset_seconds"]) <= 0.35 and abs(fit["scale"] - 1) * duration <= 1.5
        # Settled means the cues already sit on the dialogue and what is left is
        # irreducible: reading time on the ends, and anchor noise that a shift or
        # scale cannot chase without inventing precision the evidence lacks.
        # Measured on a real library, correctly timed subtitles still scatter around
        # the fit with a p95 near 1.5-2 s. Judge the centre robustly and leave the tail
        # room, or the noise floor itself reads as a fault.
        settled = (
            near_identity
            and fit["residual_median_seconds"] <= 0.75
            and fit["residual_outlier_ratio"] <= 0.15
        )
        if not blocking_structural and (aligned or settled):
            decision, reason = (
                ("pass", "Cue starts sit on the recognized dialogue")
                if aligned
                else ("pass", "Already aligned; the remainder is reading time and anchor noise")
            )
        elif start_p95 > 2.0 or start_outlier_ratio >= 0.2 or abs(start_median) > 0.5:
            decision, reason = "repair", "Cue starts are offset from the recognized dialogue"
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
        "start_p95_seconds": start_p95 if evidence and sufficient else None,
        "start_residual_p95_seconds": residual_p95 if evidence and sufficient else None,
        "start_fit": fit if evidence and sufficient else None,
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
    # Judge the repair on the axis it can actually move. Cue durations are preserved,
    # so reading time rides along unchanged and must not decide whether a shift worked.
    regressions = [
        abs(candidate["start_delta_seconds"]) - abs(original["start_delta_seconds"])
        for original, candidate in zip(before["evidence"], after["evidence"], strict=True)
    ]
    # Judge regressions in aggregate. A correct global shift still pushes the few
    # anchors that were mismatched further out, so any single-anchor veto rejects
    # good repairs on real media, where anchors scatter with a p95 near 1.5 s.
    count = len(regressions)
    if (
        sum(regression > 0.25 for regression in regressions) / count > 0.25
        or sum(regression > 0.75 for regression in regressions) / count > 0.1
    ):
        return False
    # A repair must never start truncating speech that the original covered.
    if max(
        max(0.0, -candidate["end_delta_seconds"]) - max(0.0, -original["end_delta_seconds"])
        for original, candidate in zip(before["evidence"], after["evidence"], strict=True)
    ) > 0.5:
        return False
    # Judge the middle, not the tail. Anchors that were mismatched stay mismatched after
    # a shift, so they dominate a p95 and make a correct repair look like no improvement.
    was = median(abs(e["start_delta_seconds"]) for e in before["evidence"])
    now = median(abs(e["start_delta_seconds"]) for e in after["evidence"])
    return now <= was * 0.8 and was - now >= 0.25
