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


# Verdict thresholds. Named once so the decision and the explanation shown to the
# user are measured against the same numbers rather than two copies of them.
# What the evidence itself must clear before any timing verdict means anything.
SUFFICIENT_MATCH_RATIO = 0.65
SUFFICIENT_SPEECH_WORDS = 12
SUFFICIENT_DIALOGUE_COVERAGE = 0.1
ALIGNED_START_MEDIAN = 0.5
ALIGNED_START_P95 = 1.25
ALIGNED_TRUNCATION = 1.0
# The two acceptance paths must agree on how much constant offset is tolerable. Judging
# the measured error to 0.5 s while refusing a fitted offset at 0.36 s sends correctly
# timed subtitles to review over a few milliseconds, so hold both to the same number.
SETTLED_OFFSET = ALIGNED_START_MEDIAN
SETTLED_DRIFT = 1.5
SETTLED_RESIDUAL_MEDIAN = 0.75
SETTLED_OUTLIER_RATIO = 0.15
# What a proposed repair has to demonstrate before it may replace the original.
REGRESSION_SHARE = 0.25
LARGE_REGRESSION_SHARE = 0.1
NEW_TRUNCATION = 0.5
TRUNCATION_SHARE = 0.1
SEVERE_TRUNCATION = 1.5
IMPROVEMENT_FACTOR = 0.8
IMPROVEMENT_MARGIN = 0.25


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
    low_confidence_ratio = (
        sum(word.probability < 0.5 for word in words) / len(words) if words else 0.0
    )
    # Publish what the evidence had to clear, not just whether it did. Inconclusive is
    # the verdict that most needs explaining and the one that arrived as a bare sentence.
    coverage_checks = [
        {
            "name": "Confident timing anchors",
            "measured": f"{len(evidence)}",
            "limit": f"at least {required_anchors}",
            "passed": len(evidence) >= required_anchors,
        },
        {
            "name": "Dialogue lines carrying an anchor",
            "measured": f"{dialogue_coverage:.1%}",
            "limit": (
                f"at least {SUFFICIENT_DIALOGUE_COVERAGE:.0%}, or {2 * required_anchors} anchors"
            ),
            "passed": (
                dialogue_coverage >= SUFFICIENT_DIALOGUE_COVERAGE
                or len(evidence) >= 2 * required_anchors
            ),
        },
        {
            "name": "Subtitle text matching recognized speech",
            "measured": f"{match['matched_token_ratio']:.1%}",
            "limit": f"at least {SUFFICIENT_MATCH_RATIO:.0%}",
            "passed": match["matched_token_ratio"] >= SUFFICIENT_MATCH_RATIO,
        },
        {
            "name": "Recognized speech words",
            "measured": f"{match['speech_words']}",
            "limit": f"at least {SUFFICIENT_SPEECH_WORDS}",
            "passed": match["speech_words"] >= SUFFICIENT_SPEECH_WORDS,
        },
        {
            "name": "Evidence spread across the runtime",
            "measured": "beginning, middle, and end" if distribution_ok else "one part only",
            "limit": "all three thirds",
            "passed": distribution_ok,
        },
    ]
    sufficient = bool(cues) and all(check["passed"] for check in coverage_checks)
    checks: list[dict] = []
    if not sufficient:
        short = [check for check in coverage_checks if not check["passed"]]
        decision = "inconclusive"
        reason = (
            "Not enough confident evidence to judge the timing: "
            + "; ".join(
                f"{check['name'].lower()} was {check['measured']}, needs {check['limit']}"
                for check in short
            )
            if short
            else "The subtitle contains no cues to judge"
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
        drift = abs(fit["scale"] - 1) * duration
        aligned = (
            abs(start_median) <= ALIGNED_START_MEDIAN
            and start_p95 <= ALIGNED_START_P95
            and truncation <= ALIGNED_TRUNCATION
        )
        # An identity transform means the cues already sit on the dialogue; the
        # remaining spread is anchor noise that retiming cannot and must not chase.
        near_identity = abs(fit["offset_seconds"]) <= SETTLED_OFFSET and drift <= SETTLED_DRIFT
        # Settled means the cues already sit on the dialogue and what is left is
        # irreducible: reading time on the ends, and anchor noise that a shift or
        # scale cannot chase without inventing precision the evidence lacks.
        # Measured on a real library, correctly timed subtitles still scatter around
        # the fit with a p95 near 1.5-2 s. Judge the centre robustly and leave the tail
        # room, or the noise floor itself reads as a fault.
        settled = (
            # The fit splits a lag between its offset and its scale, so both terms can sit
            # under their own limits while the cues are still most of a second off the
            # speech. Settled has to answer the measured question too, not just the shape
            # of the fit: are the cues actually on the dialogue?
            abs(start_median) <= ALIGNED_START_MEDIAN
            and near_identity
            and fit["residual_median_seconds"] <= SETTLED_RESIDUAL_MEDIAN
            and fit["residual_outlier_ratio"] <= SETTLED_OUTLIER_RATIO
        )
        # Publish the measurements, not just the conclusion. A subtitle can miss a
        # threshold by a few milliseconds, and a verdict nobody can inspect is a label.
        checks = [
            {
                "name": "Median start error",
                "measured": f"{start_median:+.3f} s",
                "limit": f"within \u00b1{ALIGNED_START_MEDIAN:.2f} s",
                "passed": abs(start_median) <= ALIGNED_START_MEDIAN,
            },
            {
                "name": "95th percentile start error",
                "measured": f"{start_p95:.3f} s",
                "limit": f"at most {ALIGNED_START_P95:.2f} s",
                "passed": start_p95 <= ALIGNED_START_P95,
            },
            {
                "name": "Speech cut off early (95th percentile)",
                "measured": f"{truncation:.3f} s",
                "limit": f"at most {ALIGNED_TRUNCATION:.2f} s",
                "passed": truncation <= ALIGNED_TRUNCATION,
            },
            {
                "name": "Constant offset in the timing fit",
                "measured": f"{fit['offset_seconds']:+.3f} s",
                "limit": f"within \u00b1{SETTLED_OFFSET:.2f} s",
                "passed": abs(fit["offset_seconds"]) <= SETTLED_OFFSET,
            },
            {
                "name": "Drift accumulated across the runtime",
                "measured": f"{drift:.3f} s",
                "limit": f"at most {SETTLED_DRIFT:.2f} s",
                "passed": drift <= SETTLED_DRIFT,
            },
            {
                "name": "Scatter around the fit (median)",
                "measured": f"{fit['residual_median_seconds']:.3f} s",
                "limit": f"at most {SETTLED_RESIDUAL_MEDIAN:.2f} s",
                "passed": fit["residual_median_seconds"] <= SETTLED_RESIDUAL_MEDIAN,
            },
            {
                "name": "Anchors more than 1 s off the fit",
                "measured": f"{fit['residual_outlier_ratio']:.0%}",
                "limit": f"at most {SETTLED_OUTLIER_RATIO:.0%}",
                "passed": fit["residual_outlier_ratio"] <= SETTLED_OUTLIER_RATIO,
            },
        ]
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
        "checks": checks,
        "coverage_checks": coverage_checks,
        "recognized_words": len(words),
        "low_confidence_ratio": low_confidence_ratio,
        "structural_issues": structural,
        "blocking_structural_issues": blocking_structural,
        "evidence": evidence,
    }


def improvement(before: dict, after: dict) -> dict:
    """Judge a repair and publish the measurement behind the verdict.

    A bare boolean cannot be explained. The dashboard has to say why a repair was
    withheld, and re-deriving the policy in the browser would keep these limits in two
    languages, so each one is measured here once and handed over for display.
    """
    if before["decision"] != "repair" or after["decision"] != "pass":
        return {
            "accepted": False,
            "reason": f"the retimed subtitle still did not pass its own audit ({after['reason'].lower()})",
            "checks": [],
        }
    # Compare the same authored cues; replacement text cannot game the score.
    if [e["cue"] for e in before["evidence"]] != [e["cue"] for e in after["evidence"]]:
        return {
            "accepted": False,
            "reason": "the repair did not leave every authored cue in place, so the two audits do not compare",
            "checks": [],
        }
    # Judge the repair on the axis it can actually move. Cue durations are preserved,
    # so reading time rides along unchanged and must not decide whether a shift worked.
    regressions = [
        abs(candidate["start_delta_seconds"]) - abs(original["start_delta_seconds"])
        for original, candidate in zip(before["evidence"], after["evidence"], strict=True)
    ]
    count = len(regressions)
    moved = [sum(regression > bound for regression in regressions) for bound in (0.25, 0.75)]
    # Shifting cues earlier moves their ends earlier too, so a correct repair still eats
    # into the trailing reading time of whichever line had the least of it to spare.
    # Judge that in aggregate, like the regressions above: one clipped line out of forty
    # is anchor noise, while a shift that clips many of them is genuinely cutting speech.
    truncations = [
        max(0.0, -candidate["end_delta_seconds"]) - max(0.0, -original["end_delta_seconds"])
        for original, candidate in zip(before["evidence"], after["evidence"], strict=True)
    ]
    clipped = sum(value > NEW_TRUNCATION for value in truncations)
    worst_clip = max(truncations)
    # Judge the middle, not the tail. Anchors that were mismatched stay mismatched after
    # a shift, so they dominate a p95 and make a correct repair look like no improvement.
    was = median(abs(e["start_delta_seconds"]) for e in before["evidence"])
    now = median(abs(e["start_delta_seconds"]) for e in after["evidence"])
    checks = [
        # Judge regressions in aggregate. A correct global shift still pushes the few
        # anchors that were mismatched further out, so any single-anchor veto rejects
        # good repairs on real media, where anchors scatter with a p95 near 1.5 s.
        {
            "name": "Anchors pushed over 0.25 s further from speech",
            "measured": f"{moved[0]} of {count}",
            "limit": f"at most {math.floor(count * REGRESSION_SHARE):d}",
            "passed": moved[0] / count <= REGRESSION_SHARE,
            "failure": f"it pushed {moved[0]} of {count} anchors further from the dialogue",
        },
        {
            "name": "Anchors pushed over 0.75 s further from speech",
            "measured": f"{moved[1]} of {count}",
            "limit": f"at most {math.floor(count * LARGE_REGRESSION_SHARE):d}",
            "passed": moved[1] / count <= LARGE_REGRESSION_SHARE,
            "failure": f"it pushed {moved[1]} of {count} anchors far further from the dialogue",
        },
        {
            "name": "Lines newly cut off before their speech ends",
            "measured": f"{clipped} of {count}",
            "limit": f"at most {math.floor(count * TRUNCATION_SHARE):d}",
            "passed": clipped / count <= TRUNCATION_SHARE,
            "failure": f"it would cut short {clipped} of {count} spoken lines",
        },
        {
            # No single line may lose a meaningful part of its speech, however good the rest.
            "name": "Worst single line cut short",
            "measured": f"{worst_clip:.3f} s",
            "limit": f"at most {SEVERE_TRUNCATION:.2f} s",
            "passed": worst_clip <= SEVERE_TRUNCATION,
            "failure": f"it would cut {worst_clip:.2f} s off the end of a spoken line",
        },
        {
            "name": "Median start error",
            "measured": f"{was:.3f} s to {now:.3f} s",
            "limit": f"at most {was * IMPROVEMENT_FACTOR:.3f} s, improving by {IMPROVEMENT_MARGIN:.2f} s or more",
            "passed": now <= was * IMPROVEMENT_FACTOR and was - now >= IMPROVEMENT_MARGIN,
            "failure": (
                f"the median start error rose from {was:.3f} s to {now:.3f} s"
                if now > was
                else f"the median start error only improved from {was:.3f} s to {now:.3f} s"
            ),
        },
    ]
    failed = [check for check in checks if not check["passed"]]
    return {
        "accepted": not failed,
        "reason": (
            "; ".join(check["failure"] for check in failed)
            if failed
            else f"the median start error improved from {was:.3f} s to {now:.3f} s"
        ),
        "checks": checks,
    }


def improved(before: dict, after: dict) -> bool:
    return improvement(before, after)["accepted"]
