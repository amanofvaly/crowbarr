"""Conservative timing evidence against ASR anchors, independent of repair timestamps.

This is an automated heuristic, not an independently annotated timing benchmark.
Unmatched dialogue is uncertainty, never evidence that the subtitle is synchronized.
"""

import math
from statistics import median

from .subtitles import Cue, Word, match_passages, tokens, validate_cues
from .version import AUDIT_POLICY_VERSION

AUDIT_VERSION = AUDIT_POLICY_VERSION


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
# Evidence can fall short in two different ways, and only one of them is the app's own
# uncertainty. When recognition is strong and the subtitle still matches almost none of
# what was heard, nothing is unclear: the subtitle is not this recording. That finding
# is the only one that may replace an authored subtitle with a generated one, so it is
# held to the quality of the recognition as well as to the size of the disagreement.
MISMATCH_MATCH_RATIO = 0.05
MISMATCH_RECOGNIZED_WORDS = 200
MISMATCH_LOW_CONFIDENCE = 0.5
# A recording that carries scenes the subtitle has no lines for leaves a staircase: the
# text is this episode, but every later cue falls further behind the dialogue and none
# comes back. Shift and scale cannot remove a staircase, so calling it a repair only
# produces one that fails its own audit.
DIFFERENT_CUT_RESIDUAL = 10.0
DIFFERENT_CUT_OUTLIERS = 0.5
DIFFERENT_CUT_DRIFT_SHARE = 0.8
# How much of a subtitle has to be structurally broken before the damage says something
# about the timing rather than about the typing.
STRUCTURAL_SHARE = 0.05
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


def blocking_structural_issues(issues: list[str], cue_count: int) -> list[str]:
    """Split structural damage into what blocks publication and what is only reported.

    Damage decides an outcome by share, never by a single item. One overlapping line in
    a file of five hundred says nothing about the timing of the other four hundred and
    ninety-nine, and no review action exists that would fix it. Two faults are exempt
    from the share rule because they leave nothing to measure rather than leaving
    something untidy: a cue whose timing is impossible, and an alignment that produced
    no dialogue at all.

    Both the audit and the repair path ask this question, so they ask it here. Keeping
    one copy is the point: the rule was already correct in the audit and applied as a
    single-item veto in the processor, which held finished repairs for one bad cue.
    """
    fatal, untidy = [], []
    for issue in issues:
        if "invalid timing" in issue or "No aligned dialogue" in issue:
            fatal.append(issue)
        elif "unsuitable reading duration" not in issue:
            untidy.append(issue)
    return fatal + (untidy if len(untidy) > max(3, cue_count * STRUCTURAL_SHARE) else [])


def _one_way_drift(evidence: list[dict]) -> tuple[float, float]:
    """How much of the disagreement runs one way only, and how far it travels.

    Missing scenes push every later cue further from its speech and never pull one
    back. Ordinary anchor noise scatters in both directions, so the share of steps
    heading the same way separates a cut difference from a badly matched subtitle.
    """
    ordered = sorted(evidence, key=lambda item: item["subtitle_start"])
    deltas = [item["start_delta_seconds"] for item in ordered]
    if len(deltas) < 2:
        return 0.0, 0.0
    steps = [later - earlier for earlier, later in zip(deltas, deltas[1:], strict=False)]
    forward = max(sum(step <= 0.5 for step in steps), sum(step >= -0.5 for step in steps))
    return forward / len(steps), max(deltas) - min(deltas)


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
    # A pass publishes nothing; it leaves the original exactly where it is. So a
    # structural nit in a file Crowbarr is not touching cannot make correct timing
    # wrong, and no review action exists that would fix one -- an uploader's trailing
    # advertisement that overruns the video by half a second, or two lines overlapping
    # by 0.4 s, are reported and no more. Two things still block, because they
    # undermine the measurement rather than the tidiness: a cue whose timing is
    # impossible, and damage spread across the whole file.
    blocking_structural = blocking_structural_issues(structural, len(cues))
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
    # Measured whatever the verdict, so a report can show why a mismatch was not
    # declared as readily as why it was.
    mismatch_checks = [
        {
            "name": "Subtitle text matching recognized speech",
            "measured": f"{match['matched_token_ratio']:.1%}",
            "limit": f"at most {MISMATCH_MATCH_RATIO:.0%}",
            "passed": match["matched_token_ratio"] <= MISMATCH_MATCH_RATIO,
        },
        {
            "name": "Recognized speech words",
            "measured": f"{len(words)}",
            "limit": f"at least {MISMATCH_RECOGNIZED_WORDS}",
            "passed": len(words) >= MISMATCH_RECOGNIZED_WORDS,
        },
        {
            "name": "Words the recognizer was unsure of",
            "measured": f"{low_confidence_ratio:.1%}",
            "limit": f"at most {MISMATCH_LOW_CONFIDENCE:.0%}",
            "passed": low_confidence_ratio <= MISMATCH_LOW_CONFIDENCE,
        },
    ]
    mismatched = bool(cues) and not sufficient and all(check["passed"] for check in mismatch_checks)
    checks: list[dict] = []
    cut_checks: list[dict] = []
    if mismatched:
        decision = "mismatched"
        reason = (
            f"Recognized {len(words)} words of dialogue clearly, and only "
            f"{match['matched_token_ratio']:.1%} of the subtitle text appears anywhere in them; "
            "this subtitle was not written for this recording"
        )
    elif not sufficient:
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
        if match["matched_token_ratio"] <= MISMATCH_MATCH_RATIO:
            failed_mismatch = [check for check in mismatch_checks if not check["passed"]]
            if failed_mismatch:
                reason += ". Content mismatch cannot be established: " + "; ".join(
                    f"{check['name'].lower()} was {check['measured']}, needs {check['limit']}"
                    for check in failed_mismatch
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
        drift_share, drift_span = _one_way_drift(evidence)
        cut_checks = [
            {
                "name": "Scatter the fit cannot remove",
                "measured": f"{fit['residual_median_seconds']:.1f} s",
                "limit": f"more than {DIFFERENT_CUT_RESIDUAL:.0f} s",
                "passed": fit["residual_median_seconds"] > DIFFERENT_CUT_RESIDUAL,
            },
            {
                "name": "Anchors the fit cannot place",
                "measured": f"{fit['residual_outlier_ratio']:.0%}",
                "limit": f"more than {DIFFERENT_CUT_OUTLIERS:.0%}",
                "passed": fit["residual_outlier_ratio"] > DIFFERENT_CUT_OUTLIERS,
            },
            {
                "name": "Disagreement that only ever grows",
                "measured": f"{drift_share:.0%}",
                "limit": f"at least {DIFFERENT_CUT_DRIFT_SHARE:.0%}",
                "passed": drift_share >= DIFFERENT_CUT_DRIFT_SHARE,
            },
        ]
        different_cut = all(check["passed"] for check in cut_checks)
        if not blocking_structural and (aligned or settled):
            decision, reason = (
                ("pass", "Cue starts sit on the recognized dialogue")
                if aligned
                else ("pass", "Already aligned; the remainder is reading time and anchor noise")
            )
        elif different_cut:
            decision, reason = (
                "different_cut",
                f"The subtitle is this episode -- {match['matched_token_ratio']:.0%} of its text was "
                f"recognized in the audio -- but it falls {drift_span:.0f} s further behind the "
                "dialogue by the end than at the start, in steps. The recording carries scenes the "
                "subtitle has no lines for, and no shift or stretch can line the two up",
            )
        elif start_p95 > 2.0 or start_outlier_ratio >= 0.2 or abs(start_median) > 0.5:
            decision, reason = "repair", "Cue starts are offset from the recognized dialogue"
        elif blocking_structural and (aligned or settled):
            # Timing this good is not uncertainty. Saying "borderline" here sent a file
            # whose cues sat exactly on the dialogue to review for a structural fault.
            decision, reason = (
                "inconclusive",
                f"Cue timing matches the recognized dialogue, but {len(blocking_structural)} of "
                f"{len(cues)} cues overlap each other or run too long to read",
            )
        else:
            decision, reason = (
                "inconclusive",
                "Timing differences are borderline",
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
        "mismatch_checks": mismatch_checks,
        "cut_checks": cut_checks,
        "recognized_words": len(words),
        "low_confidence_ratio": low_confidence_ratio,
        "structural_issues": structural,
        "blocking_structural_issues": blocking_structural,
        "evidence": evidence,
    }
