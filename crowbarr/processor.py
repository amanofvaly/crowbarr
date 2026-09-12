from __future__ import annotations

import hashlib
import json
import tempfile
import textwrap
from bisect import bisect_left
from pathlib import Path

from .audit import audit, blocking_structural_issues
from .config import Settings, atomic_write
from .db import Database
from .library import allowed, signature, source_subtitle, subtitle_sources
from .media import (
    ReviewRequired,
    audio_candidates,
    choose_audio,
    duration_seconds,
    embedded_subtitles,
    extract_audio,
    probe,
    unreadable_subtitles,
)
from .subtitles import Cue, Word, match_passages, parse_srt, render_srt, validate_cues
from .version import RECOGNITION_POLICY_VERSION

# How much of a retimed file has to come from its own matched speech. Cues with nothing
# to match are placed between their neighbours, which is sound for a sound caption in a
# well matched file and guesswork in a file where almost nothing matched.
PLACED_CUE_SHARE = 0.25
PLACED_CUE_FLOOR = 3


def _worth_retiming(report: dict) -> bool:
    """Whether enough of this subtitle was found in the audio to place it from."""
    total = report.get("source_cues") or 0
    placed = report.get("preserved_cues") or 0
    return bool(total) and placed >= PLACED_CUE_FLOOR and placed / total >= PLACED_CUE_SHARE


def placement_verdict(after: dict, timing_model: dict) -> dict:
    """Judge a retimed subtitle on what it is, not on what it replaced.

    Placement reads every timestamp from the transcript, so the old timing is gone
    rather than adjusted, and asking whether the new file improves on it compares two
    unrelated things. Two questions remain worth asking: does the result pass its own
    audit, and was enough of it read from speech rather than filled in between.
    """
    placed = timing_model.get("placed_cues", 0)
    total = placed + timing_model.get("interpolated_cues", 0)
    share = placed / total if total else 0.0
    checks = [
        {
            # `inconclusive` is not a negative finding. A file the audit could not grade
            # before retiming cannot be graded after it either, for the same reason, so
            # treating that as a failure would refuse every placement made on a file
            # whose anchors are thin -- which is the case placement exists to serve.
            # What must not appear is a verdict that measured the result and disagreed
            # with it, because after placement that means lines went to the wrong words.
            "name": "The retimed subtitle does not measure as wrong",
            "measured": after.get("decision", "unknown"),
            "limit": "pass or inconclusive",
            "passed": after.get("decision") in ("pass", "inconclusive"),
            "failure": f"the retimed subtitle audits as {after.get('decision')}",
        },
        {
            "name": "Lines placed from their own speech",
            "measured": f"{placed} of {total}",
            "limit": f"at least {PLACED_CUE_FLOOR} and {PLACED_CUE_SHARE:.0%}",
            "passed": placed >= PLACED_CUE_FLOOR and share >= PLACED_CUE_SHARE,
            "failure": f"only {placed} of {total} lines could be matched to the audio",
        },
    ]
    failed = [check for check in checks if not check["passed"]]
    return {
        "accepted": not failed,
        "reason": (
            "; ".join(check["failure"] for check in failed)
            if failed
            else f"{placed} of {total} lines were placed on their own speech"
        ),
        "checks": checks,
    }


def _retime_from_transcript(original: list[Cue], passages: list) -> tuple[list[Cue], dict]:
    """Place every cue where its own words were spoken.

    Timestamps are read from the transcript rather than corrected. A cue whose words
    were matched takes the time of those words. A cue with nothing to match -- a sound
    caption, on-screen text, a line the recognizer missed -- keeps its position relative
    to the matched cues either side of it.

    Nothing here models the old timing, so nothing has to recognise its shape first. A
    constant offset, accumulating drift, a cut the recording does not share, and all
    three at once in the same file are the same problem to this function, because none
    of the old timestamps survive it.
    """
    matched = {
        passage.source_index: passage
        for passage in passages
        if passage.authored and passage.source_index is not None
    }
    if not matched:
        return [], {"kind": "unsupported", "reason": "No authored line could be matched to the audio"}
    anchors = sorted(matched)
    placed = {index: matched[index].start for index in anchors}
    result = []
    for index, cue in enumerate(original):
        if index in placed:
            start = placed[index]
        else:
            position = bisect_left(anchors, index)
            earlier = anchors[position - 1] if position else None
            later = anchors[position] if position < len(anchors) else None
            if earlier is not None and later is not None:
                # Keep the gap it sat in, proportionally. Authored order is information:
                # a caption a third of the way between two lines belongs a third of the
                # way between where those lines turned out to be.
                span = original[later].start - original[earlier].start
                share = (cue.start - original[earlier].start) / span if span > 0 else 0.0
                share = min(1.0, max(0.0, share))
                start = placed[earlier] + share * (placed[later] - placed[earlier])
            else:
                # Before the first match or after the last one, there is nothing to
                # interpolate between, so carry the nearest match's correction outward.
                neighbour = earlier if earlier is not None else later
                start = placed[neighbour] + (cue.start - original[neighbour].start)
        # Cue length is an authoring decision -- reading time, not synchronisation -- so
        # placement moves a line without reshaping it.
        result.append(Cue(start, start + max(0.0, cue.end - cue.start), cue.text))
    return result, {
        "kind": "transcript_placement",
        "anchor_points": len(matched),
        "placed_cues": len(matched),
        "interpolated_cues": len(original) - len(matched),
    }


def _distributed_sample(passages: list, duration: float, per_region: int = 30) -> list:
    """Bound forced-alignment cost while retaining beginning/middle/end evidence."""
    regions = [[], [], []]
    for passage in passages:
        region = min(2, int(passage.start / max(duration, 1e-9) * 3))
        regions[region].append(passage)
    selected = []
    for region in regions:
        if len(region) <= per_region:
            selected.extend(region)
            continue
        indexes = {round(index * (len(region) - 1) / (per_region - 1)) for index in range(per_region)}
        selected.extend(region[index] for index in sorted(indexes))
    return sorted(selected, key=lambda passage: passage.start)


def _generated_baseline(passages: list) -> list[Cue]:
    return [
        Cue(passage.start, passage.end, "\n".join(textwrap.wrap(passage.display, width=42)))
        for passage in passages
    ]


def _stabilize_generated(cues: list[Cue], baseline: list[Cue]) -> tuple[list[Cue], int]:
    """Resolve small refinement overlaps without changing generated text or order."""
    result = [Cue(cue.start, cue.end, cue.text) for cue in cues]
    adjusted = 0
    for index, (left, right) in enumerate(zip(result, result[1:], strict=False)):
        if right.start >= left.end:
            continue
        # Prefer the original Whisper interval when a refinement crosses its neighbor.
        left.start, left.end = baseline[index].start, baseline[index].end
        right.start, right.end = baseline[index + 1].start, baseline[index + 1].end
        adjusted += 1
    for left, right in zip(result, result[1:], strict=False):
        if right.start >= left.end:
            continue
        boundary = min(
            max((right.start + left.end) / 2, left.start + 0.05),
            right.end - 0.05,
        )
        left.end = boundary
        right.start = boundary
        adjusted += 1
    return result, adjusted


def _stabilize_authored(cues: list[Cue], minimum: float = 0.04) -> tuple[list[Cue], int]:
    """Separate repaired cues that landed on top of each other.

    Retiming moves every cue onto recognised speech, which can push neighbours into
    each other even when the authored file had no overlap. Only the shared boundary
    moves: returning a cue to its unrepaired position would undo the repair.
    """
    result = [Cue(cue.start, cue.end, cue.text) for cue in cues]
    adjusted = 0
    for left, right in zip(result, result[1:], strict=False):
        if right.start >= left.end or right.end - left.start <= 2 * minimum:
            continue
        boundary = min(
            max((right.start + left.end) / 2, left.start + minimum),
            right.end - minimum,
        )
        left.end, right.start = boundary, boundary
        adjusted += 1
    return result, adjusted


def _sample_windows(cues: list[Cue], duration: float) -> list[tuple[float, float]]:
    from .subtitles import tokens

    windows = []
    for fraction in (0.12, 0.5, 0.85):
        dialogue = [
            cue
            for cue in cues
            if len(tokens(cue.text)) >= 4 and abs(cue.start - duration * fraction) < duration / 6
        ]
        if not dialogue:
            continue
        center = min(dialogue, key=lambda cue: abs(cue.start - duration * fraction)).start
        start, end = max(0, center - 60), min(duration, center + 60)
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((start, end))
    return windows


# Verdicts that leave nothing for a repair to correct, and that the full transcript
# already in hand can answer by writing a subtitle of its own.
UNUSABLE = ("mismatched", "different_cut")


def _needs_full_audio(candidates) -> bool:
    """Whether a sampled verdict is enough to act on, or only enough to stand down.

    Three two-minute windows see about a quarter of an episode. A sample that says the
    subtitle is already correct settles the job, because the answer is to write nothing
    and the rest of the runtime cannot make "leave it alone" harmful. Every other answer
    ends in a file being rewritten, and the cues outside the windows were never matched
    to any audio: the model that moves them is fitted to the sample, and the check that
    the move worked re-measures the same sample. Nothing ever looks at the remainder.
    """
    return not any(candidate["audit"]["decision"] == "pass" for candidate in candidates)


def _audit_source(cues, words, duration, windows, indices=None):
    if not windows:
        return audit(cues, words, duration)
    selected = [
        (index, cue)
        for index, cue in enumerate(cues)
        if (
            index in indices
            if indices is not None
            else any(cue.start >= start + 5 and cue.end <= end - 5 for start, end in windows)
        )
    ]
    result = audit([cue for _, cue in selected], words, duration)
    if result["decision"] in UNUSABLE:
        # Three windows can land on music, a silent stretch, or the one scene the
        # sampler chose badly, and they cannot show a staircase that only appears across
        # the whole runtime. Declaring either verdict from a sample is not a finding, it
        # is a guess -- so hand it back as inconclusive and let the escalation to the
        # full file decide. Sampling only ever defers the verdict; it never makes one.
        result["decision"] = "inconclusive"
        result["reason"] = "The sample could not settle this subtitle; auditing the full file"
    for item in result["evidence"]:
        item["cue"] = selected[item["cue"] - 1][0] + 1
    result["sample_cue_indices"] = [index for index, _ in selected]
    result["sample_windows"] = windows
    result["full_source_cues"] = len(cues)
    return result


def _recognized_words(
    media: Path,
    audio: Path,
    settings: Settings,
    directory: Path,
    model_cache: Path,
    transcriber,
    windows=None,
) -> tuple[list[Word], list[str], bool]:
    stat = media.stat()
    transcript_key = hashlib.sha256(
        json.dumps(
            {
                "path": str(media.resolve()),
                "recognition_policy": RECOGNITION_POLICY_VERSION,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "language": settings.language,
                "model": settings.model,
                "device": settings.device,
                "compute_type": settings.compute_type,
                "allow_untagged_audio": settings.allow_untagged_audio,
                "windows": windows,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    transcript_path = directory / "transcripts" / f"{transcript_key}.json"
    if transcript_path.exists():
        try:
            payload = json.loads(transcript_path.read_text(encoding="utf-8"))
            words = [Word(**word) for word in payload["words"]]
            if words:
                transcriber.runtime = {
                    **payload.get("runtime", {"backend": "unknown (legacy cache)"}),
                    "cache_hit": True,
                }
                return words, list(payload.get("issues", [])), True
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
    if windows:
        from .inference import transcribe_windows

        words, issues = transcribe_windows(audio, windows, settings, model_cache)
    else:
        from .inference import transcribe, transcribe_bounded

        if transcriber is transcribe:
            words, issues = transcribe_bounded(
                audio, settings, model_cache, transcript_path.with_suffix(".parts")
            )
        else:
            words, issues = transcriber(audio, settings, model_cache)
    atomic_write(
        transcript_path,
        json.dumps(
            {
                "runtime": getattr(transcriber, "runtime", {}),
                "words": [
                    {
                        "start": word.start,
                        "end": word.end,
                        "text": word.text,
                        "probability": word.probability,
                    }
                    for word in words
                ],
                "issues": issues,
            }
        ),
    )
    import shutil

    shutil.rmtree(transcript_path.with_suffix(".parts"), ignore_errors=True)
    transcript_files = sorted(transcript_path.parent.glob("*.json"), key=lambda path: path.stat().st_mtime_ns)
    total = sum(path.stat().st_size for path in transcript_files)
    for stale in transcript_files:
        if total <= 2 * 1024**3:
            break
        size = stale.stat().st_size
        stale.unlink(missing_ok=True)
        total -= size
    return words, issues, False


def current(job: dict, settings: Settings) -> bool:
    media = Path(job["media"])
    if not allowed(media, settings) or media.is_symlink() or not media.exists():
        return False
    source = source_subtitle(media, settings)
    return signature(media, source, settings) == job["signature"]


def process(
    job: dict, settings: Settings, directory: Path, db: Database, transcriber=None, aligner=None
) -> dict:
    import time

    from .inference import align, transcribe

    progress_updated = [0.0]
    recognition_stage = ["Recognizing dialogue"]

    def progress(position, total):
        if time.monotonic() - progress_updated[0] > 2 or position >= total:
            db.update(
                job["id"],
                stage=recognition_stage[0],
                progress_current=max(0, min(position, total)),
                progress_total=total,
            )
            progress_updated[0] = time.monotonic()

    if transcriber is None:
        transcribe.progress = progress
    cache_transcript = transcriber is None
    refine_generated = aligner is not None or settings.refine_generated
    transcriber, aligner = transcriber or transcribe, aligner or align
    if not current(job, settings):
        return {"state": "superseded", "error": "Media, subtitle, or processing settings changed"}
    media = Path(job["media"])
    metadata = probe(media)
    duration = duration_seconds(metadata)
    minimum = settings.min_duration_minutes * 60
    if minimum and duration < minimum:
        return {
            "state": "skipped",
            "stage": "Short video",
            "error": (
                f"Video is {duration / 60:.1f} minutes long; "
                f"minimum is {settings.min_duration_minutes} minutes"
            ),
        }
    preference = db.cache_audio(str(media), metadata)
    if preference["skipped"]:
        return {"state": "skipped", "stage": "Library preference", "error": preference["reason"]}
    audio_stream = choose_audio(metadata, settings)
    audio_selection = _describe_audio_choice(audio_candidates(metadata), audio_stream)
    cache = directory / "models"
    cache.mkdir(exist_ok=True)
    work = directory / "work"
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{job['id']}-", dir=work) as temporary:
        audio = Path(temporary) / "audio.wav"
        offset, duration = extract_audio(media, audio, metadata, audio_stream)
        issues = []
        warnings = []
        if audio_selection["note"]:
            warnings.append(audio_selection["note"])
        # An explicit "generate" request means the user has already judged whatever
        # exists to be unusable, so discovered sources and providers are both skipped.
        forced_generation = job.get("directive") == "generate"
        candidate_paths = (
            []
            if forced_generation
            else [("external", path, str(path)) for path in subtitle_sources(media, settings)]
        )
        if not forced_generation:
            candidate_paths.extend(
                ("embedded", path, f"embedded stream {path.stem.removeprefix('embedded-')}")
                for path in embedded_subtitles(media, Path(temporary), metadata, settings)
            )
        candidates = []
        unreadable = []
        for kind, source, label in candidate_paths:
            try:
                candidates.append(
                    {
                        "kind": kind,
                        "path": source,
                        "label": label,
                        "cues": parse_srt(source.read_text(encoding="utf-8-sig")),
                    }
                )
            except (ValueError, UnicodeError) as error:
                unreadable.append(f"{Path(label).name}: {error}")
                warnings.append(f"Ignored unreadable {label}: {error}")
        if candidate_paths and not candidates:
            # This refusal ends the job before a report is written, so the reason has to
            # travel in the message. Saying only that nothing could be read leaves the
            # one fact that would explain it -- what the parser actually objected to --
            # in a warnings list that is then discarded.
            raise ReviewRequired("No discovered subtitle source could be read. " + "; ".join(unreadable))
        language_evidence = None
        if cache_transcript:
            from .inference import verify_audio_language

            db.update(job["id"], stage="Verifying spoken audio language")
            stat = media.stat()
            language_key = hashlib.sha256(json.dumps([
                str(media.resolve()), stat.st_size, stat.st_mtime_ns, audio_stream["index"],
                settings.language, RECOGNITION_POLICY_VERSION,
            ]).encode()).hexdigest()
            language_path = directory / "language" / f"{language_key}.json"
            if language_path.exists():
                try:
                    language_evidence = json.loads(language_path.read_text())
                    if language_evidence.get("language") != settings.language:
                        language_evidence = None
                except (ValueError, AttributeError):
                    language_evidence = None
            if language_evidence is None:
                language_evidence = verify_audio_language(audio, settings, cache)
                atomic_write(language_path, json.dumps(language_evidence))
        # An English track Crowbarr cannot decode is still worth reporting: without this
        # the job looks as though the file had no English subtitle at all.
        for label in unreadable_subtitles(metadata):
            warnings.append(f"English {label} is an image subtitle and needs OCR; not used")
        provider_result = None
        if not candidates and not forced_generation and settings.bazarr.url and settings.providers():
            from .bazarr import try_alternative

            db.update(job["id"], stage="Searching Bazarr providers")
            provider_result = try_alternative(settings, db, media, directory, job["id"])
            if provider_result["state"] in ("downloaded", "discarded"):
                if not current(job, settings):
                    return {
                        "state": "superseded",
                        "stage": "Provider subtitle arrived",
                        "report": json.dumps({"bazarr": provider_result}),
                    }
                # Bazarr validates a download itself and discards one it judges wrong for
                # this file, so a request can succeed and leave nothing behind. The ledger
                # has already advanced, so retry to reach the next candidate rather than
                # parking the episode; once the provider budget is spent try_alternative
                # reports "exhausted" and processing falls through to generation.
                return {
                    "state": "retry",
                    "stage": "",
                    "error": "Bazarr discarded that provider subtitle; trying the next candidate",
                    "ready": time.time() + 60,
                    "report": json.dumps({"bazarr": provider_result}),
                }
            if provider_result["state"] == "available":
                raise ReviewRequired(
                    "Bazarr has authored alternatives. Enable provider upgrades or choose one in Bazarr before generation."
                )
        windows = (
            _sample_windows(max(candidates, key=lambda c: len(c["cues"]))["cues"], duration)
            if candidates and settings.sampled_audit and cache_transcript and duration > 600
            else []
        )
        initial_sampling_windows = list(windows)
        full_audit_escalation = False
        if windows:
            recognition_stage[0] = "Sampling existing subtitle across the runtime"
        elif forced_generation:
            recognition_stage[0] = "Generating a fresh subtitle from full audio"
        elif candidates:
            recognition_stage[0] = "Auditing existing subtitle against full dialogue"
        else:
            recognition_stage[0] = "No usable subtitle found; recognizing full audio"
        db.update(job["id"], stage=recognition_stage[0])
        if cache_transcript:
            words, transcription_issues, transcript_cache_hit = _recognized_words(
                media, audio, settings, directory, cache, transcriber, windows=windows
            )
            # Say so plainly: a reused transcript finishes in seconds, and that speed
            # must not be mistaken for what a file takes the first time.
            db.update(job["id"], cached=1 if transcript_cache_hit else 0)
        else:
            words, transcription_issues = transcriber(audio, settings, cache)
            transcript_cache_hit = False
        # ASR uncertainty is local evidence quality, not a global publication veto.
        warnings.extend(transcription_issues)
        timeline_words = [type(w)(w.start + offset, w.end + offset, w.text, w.probability) for w in words]
        arbitration = []
        for candidate in candidates:
            candidate_audit = _audit_source(candidate["cues"], timeline_words, duration, windows)
            candidate["audit"] = candidate_audit
            arbitration.append(
                {
                    "source": candidate["label"],
                    "kind": candidate["kind"],
                    "decision": candidate_audit["decision"],
                    "matched_token_ratio": candidate_audit["matched_token_ratio"],
                    "supported_cues": candidate_audit["supported_cues"],
                    "dialogue_coverage": candidate_audit["dialogue_coverage"],
                    "p95_error_seconds": candidate_audit["p95_error_seconds"],
                }
            )

        def candidate_rank(candidate):
            candidate_audit = candidate["audit"]
            decision = {"mismatched": 0, "different_cut": 1, "inconclusive": 2, "repair": 3, "pass": 4}[
                candidate_audit["decision"]
            ]
            p95 = candidate_audit["p95_error_seconds"]
            return (
                decision,
                candidate_audit["matched_token_ratio"] >= settings.min_match_ratio,
                int(candidate_audit["matched_token_ratio"] * 20),
                -(p95 if p95 is not None else float("inf")),
                candidate_audit["matched_token_ratio"],
                candidate_audit["dialogue_coverage"],
            )

        if windows and _needs_full_audio(candidates):
            full_audit_escalation = True
            recognition_stage[0] = "Sample cannot settle a change; auditing against full dialogue"
            db.update(job["id"], stage=recognition_stage[0])
            words, transcription_issues, transcript_cache_hit = _recognized_words(
                media, audio, settings, directory, cache, transcriber
            )
            warnings.extend(transcription_issues)
            windows = []
            timeline_words = [type(w)(w.start + offset, w.end + offset, w.text, w.probability) for w in words]
            for candidate in candidates:
                candidate["audit"] = audit(candidate["cues"], timeline_words, duration)
            arbitration = [
                {
                    "source": c["label"],
                    "kind": c["kind"],
                    **{
                        k: c["audit"][k]
                        for k in (
                            "decision",
                            "matched_token_ratio",
                            "supported_cues",
                            "dialogue_coverage",
                            "p95_error_seconds",
                        )
                    },
                }
                for c in candidates
            ]
        selected = max(candidates, key=candidate_rank) if candidates else None
        original = selected["cues"] if selected else []
        db.update(job["id"], stage="Matching authored subtitles")
        passages, report = match_passages(original, words, settings.min_match_ratio)
        if windows and selected:
            report["matched_token_ratio"] = selected["audit"]["matched_token_ratio"]
        report.update(
            bazarr=provider_result,
            sampling_windows=windows,
            initial_sampling_windows=initial_sampling_windows,
            full_audit_escalation=full_audit_escalation,
            mode="authored_timing" if original else "generated",
            model=settings.model,
            transcript_cache_hit=transcript_cache_hit,
            runtime=getattr(
                transcriber, "runtime", {"backend": "cached" if transcript_cache_hit else "custom"}
            ),
            source_arbitration=arbitration,
            selected_source=selected["label"] if selected else None,
            selected_source_kind=selected["kind"] if selected else None,
            audio_selection=audio_selection,
            language_evidence=language_evidence,
        )
        if original:
            db.update(job["id"], stage="Auditing existing subtitle")
            before = selected["audit"]
            report["audit"] = {"before": before, "after": None, "improved": False}
            # An authored replacement is the first recovery option, including for a
            # proven mismatch. A successful HTTP request is not a usable subtitle.
            needs_alternative = before["decision"] in UNUSABLE or (
                before["decision"] != "pass"
                and not _worth_retiming(report)
                and before["matched_token_ratio"] < 0.5
            )
            if needs_alternative and settings.bazarr.url and settings.providers():
                from .bazarr import try_alternative

                db.update(job["id"], stage="Searching Bazarr alternatives before generation")
                report["bazarr"] = try_alternative(settings, db, media, directory, job["id"])
                atomic_write(directory / "reports" / f"{job['id']}.json", json.dumps(report, indent=2))
                if not current(job, settings):
                    return {"state": "superseded", "error": "Provider subtitle arrived; needs a new audit",
                            "report": json.dumps(report)}
                if report["bazarr"]["state"] in ("downloaded", "discarded"):
                    return {"state": "retry", "stage": "Awaiting next Bazarr candidate",
                            "error": "Bazarr left no changed subtitle; trying the next candidate",
                            "ready": time.time() + 60, "report": json.dumps(report)}
                if report["bazarr"]["state"] == "available":
                    return {"state": "review", "stage": "Authored alternatives need permission",
                            "error": report["bazarr"]["reason"], "report": json.dumps(report)}
            if before["decision"] in UNUSABLE and settings.generate_over_mismatch:
                # The audit did not fail to reach a verdict here; it reached a definite
                # one, and neither verdict leaves anything a repair could act on: the
                # subtitle is for other content, or for a cut this recording is not.
                # Recognition has already produced every word this file speaks, so a
                # fresh subtitle costs nothing beyond what proving that spent, and
                # parking the episode would throw the transcript away. The rejected
                # sidecar is left on disk exactly as it was; Crowbarr publishes beside it.
                if selected["kind"] == "external" and settings.bazarr.url and settings.providers():
                    from .bazarr import reject_source

                    report["rejected_source_hash"] = reject_source(
                        directory, str(media), selected["path"], before["reason"]
                    )
                report["replaced_source"] = selected["label"]
                db.update(job["id"], stage="Generating a fresh subtitle for mismatched content")
                original, selected = [], None
                passages, rewritten = match_passages(original, words, settings.min_match_ratio)
                report.update(rewritten, mode="generated")
            elif before["decision"] == "pass" or not _worth_retiming(report):
                # A verdict decides whether the timing needs replacing, not whether it
                # can be. `inconclusive` means the audit could not assemble enough
                # confident, evenly spread anchors to judge the file; it says nothing
                # about how many lines can be found in the transcript, which is a looser
                # question and the only one placement depends on. Park a file when its
                # lines cannot be located, not when its timing could not be graded.
                passed = before["decision"] == "pass"
                report.update(
                    issues=issues,
                    warnings=warnings,
                    output_cues=0,
                    quality="audit_passed" if passed else "review",
                    note="Measured against ASR word timestamps; these are heuristic estimates, not ground truth.",
                )
                atomic_write(directory / "reports" / f"{job['id']}.json", json.dumps(report, indent=2))
                # No candidate is written for a verdict that is not a repair. The only
                # thing there was to offer was the original subtitle re-rendered, so
                # "publish reviewed candidate" published the subtitle the audit had just
                # declined to endorse -- and left it owned by Crowbarr afterwards.
                if not current(job, settings):
                    return {
                        "state": "superseded",
                        "error": "Inputs changed during audit",
                        "report": json.dumps(report),
                    }
                if (
                    passed
                    and selected["kind"] == "embedded"
                    and any(c["kind"] == "external" and c["audit"]["decision"] != "pass" for c in candidates)
                ):
                    rendered = render_srt(original)
                    report.update(
                        output_cues=len(original),
                        output_sha256=hashlib.sha256(rendered.encode()).hexdigest(),
                        publication_reason="Embedded source passed audio audit; external source did not",
                    )
                    atomic_write(directory / "reports" / f"{job['id']}.json", json.dumps(report, indent=2))
                    return publish_candidate(job, settings, directory, db, rendered, report)
                if passed:
                    # Preserve the audited authored track for download, including an
                    # embedded track that otherwise disappears with the work folder.
                    rendered = render_srt(original)
                    download_path = directory / "subtitles" / f"{job['id']}-{job['signature']}.srt"
                    atomic_write(download_path, rendered)
                    report["audited_subtitle"] = str(download_path)
                    report["audited_subtitle_sha256"] = hashlib.sha256(rendered.encode()).hexdigest()
                    output = media.with_name(f"{media.stem}.crowbarr.{settings.language}.srt")
                    if output.is_symlink():
                        raise ReviewRequired(
                            "Existing Crowbarr output is a symbolic link; cannot retire it safely"
                        )
                    if output.exists():
                        content = output.read_bytes()
                        if not db.owns_output(str(media), str(output), hashlib.sha256(content).hexdigest()):
                            raise ReviewRequired(
                                "Original passed audit, but an untracked or edited Crowbarr sidecar needs review"
                            )
                        atomic_write(
                            directory / "backups" / f"audit-retired-{job['id']}.srt", content.decode("utf-8")
                        )
                        output.unlink()
                        report["retired_output"] = True
                        atomic_write(
                            directory / "reports" / f"{job['id']}.json", json.dumps(report, indent=2)
                        )
                return {
                    "state": "unchanged" if passed else "review",
                    "stage": "Audit passed — unchanged" if passed else "Audit inconclusive",
                    "error": None if passed else before["reason"] + (
                        f". Bazarr recovery: {report['bazarr']['state']}; "
                        "generation requires reliable evidence that the authored subtitle is unusable"
                        if report.get("bazarr") else ""
                    ),
                    "report": json.dumps(report),
                    "output": None if passed else job.get("output"),
                }
        if original:
            if report["generated_word_ratio"] > settings.max_generated_ratio:
                warnings.append(
                    "Some authored dialogue lacks direct anchors; its timing will be interpolated"
                )
            if any(not p.text for p in passages):
                warnings.append("Non-dialogue captions will use interpolated timing")
            from .subtitles import spoken

            if any(not spoken(c.text) for c in original):
                warnings.append("Standalone sound captions use timing interpolated from nearby speech")
        db.update(
            job["id"],
            stage=(
                "Repairing existing subtitle timing"
                if original
                else "Refining fresh subtitle timing"
                if refine_generated
                else "Preparing fresh subtitle file"
            ),
        )
        eligible_passages = [passage for passage in passages if passage.authored] if original else passages
        if original:
            aligned, timing_model = _retime_from_transcript(original, eligible_passages)
            alignment_passages = eligible_passages
            if not aligned:
                issues.append(timing_model.get("reason", "No authored line could be matched to the audio"))
            else:
                aligned, adjusted_overlaps = _stabilize_authored(aligned)
                if adjusted_overlaps:
                    warnings.append(
                        f"Adjusted {adjusted_overlaps} repaired cue boundaries to prevent overlap"
                    )
        else:
            timing_model = None
            baseline = _generated_baseline(passages)
            if refine_generated:
                alignment_passages = _distributed_sample(eligible_passages, duration)
                try:
                    aligned, alignment_issues = aligner(audio, alignment_passages, settings, cache)
                except ImportError:
                    # Minimal source installs can omit optional refinement dependencies.
                    # Keep usable Whisper timings and report the missing capability.
                    warnings.append(
                        "WhisperX refinement is not installed in this image; "
                        "used Whisper word timestamps instead"
                    )
                    report["alignment_runtime"] = {
                        "requested_backend": settings.device,
                        "backend": None,
                        "status": "unavailable",
                    }
                    alignment_passages, aligned = [], baseline
                else:
                    report["alignment_runtime"] = dict(
                        getattr(aligner, "runtime", {"backend": "custom", "status": "completed"})
                    )
                    warnings.extend(alignment_issues)
                    if len(aligned) == len(alignment_passages):
                        refined = {
                            id(passage): cue
                            for passage, cue in zip(alignment_passages, aligned, strict=True)
                        }
                        aligned = [
                            refined.get(id(passage), cue)
                            for passage, cue in zip(passages, baseline, strict=True)
                        ]
                    else:
                        warnings.append("Forced aligner returned incomplete output; kept Whisper timestamps")
                        aligned = baseline
            else:
                alignment_passages = []
                aligned = baseline
                warnings.append("Optional WhisperX refinement disabled; using Whisper word timestamps")
            aligned, adjusted_overlaps = _stabilize_generated(aligned, baseline)
            if adjusted_overlaps:
                warnings.append(f"Adjusted {adjusted_overlaps} generated cue boundaries to prevent overlap")
        if not original:
            aligned = [Cue(c.start + offset, c.end + offset, c.text) for c in aligned]
        structural = validate_cues(aligned, duration)
        blocking = blocking_structural_issues(structural, len(aligned))
        issues.extend(blocking)
        warnings.extend(issue for issue in structural if issue not in blocking)
        if original:
            after = _audit_source(
                aligned, timeline_words, duration, windows, before.get("sample_cue_indices")
            )
            verdict = placement_verdict(after, timing_model)
            if [c.text for c in aligned] != [c.text for c in original]:
                verdict = {
                    "accepted": False,
                    "reason": "retiming did not preserve every authored line of text",
                    "checks": verdict["checks"],
                }
            report["audit"].update(after=after, improved=verdict["accepted"], improvement=verdict)
            # A complete passing audit settles the repair. The configurable match
            # minimum still protects a repair whose own audit remains inconclusive.
            if after["decision"] != "pass" and report["matched_token_ratio"] < settings.min_match_ratio:
                issues.append(
                    f"Repair remains unverified: authored text match is {report['matched_token_ratio']:.1%}, "
                    f"below the {settings.min_match_ratio:.0%} repair requirement, and its audit did not pass"
                )
            if not verdict["accepted"]:
                issues.append(f"Retimed subtitle withheld because {verdict['reason']}")
        report.update(
            {
                "mode": "authored_timing" if original else "generated",
                "audio_stream": audio_stream["index"],
                "audio_offset_seconds": offset,
                "alignment_strategy": (
                    "robust_affine_from_distributed_whisper_anchors"
                    if original
                    else "distributed_sample_with_whisper_fallback"
                ),
                "alignment_candidate_cues": len(eligible_passages),
                "alignment_anchor_cues": len(alignment_passages),
                "timing_model": timing_model,
                "issues": issues[:100],
                "issue_count": len(issues),
                "warnings": warnings[:100],
                "warning_count": len(warnings),
                "output_cues": len(aligned),
                "model": settings.model,
                "quality": "review" if issues else "passed_heuristics",
                "note": "Heuristic checks are not a guarantee of transcription or timing accuracy.",
            }
        )
        rendered = render_srt(aligned)
        report["output_sha256"] = hashlib.sha256(rendered.encode()).hexdigest()
        report_path = directory / "reports" / f"{job['id']}.json"
        atomic_write(report_path, json.dumps(report, indent=2))
        if issues:
            candidate = new_review_candidate_path(directory, job)
            if aligned:
                atomic_write(candidate, rendered)
                report["candidate"] = str(candidate)
                atomic_write(report_path, json.dumps(report, indent=2))
            return {
                "state": "review",
                "stage": "Needs attention",
                "error": issues[0],
                "report": json.dumps(report),
            }
        if db.get(job["id"]).get("cancel_requested"):
            return {
                "state": "cancelled",
                "stage": "",
                "error": "Cancelled before publication",
                "report": json.dumps(report),
            }
        return publish_candidate(job, settings, directory, db, rendered, report)


def _describe_audio_choice(tracks: list[dict], chosen: dict) -> dict:
    """Explain the track choice, including when a language tag had to be overruled."""
    picked = next(
        (track for track in tracks if track["stream"] is chosen),
        {"index": chosen.get("index"), "language": "unknown", "channels": 0, "tier": 0},
    )
    peers = [track for track in tracks if track["tier"] == picked["tier"] and track is not picked]
    note = None
    if picked["tier"] and peers:
        note = (
            f"No audio track is tagged English. Using stream {picked['index']} tagged "
            f"{picked['language']} of {len(peers) + 1} untagged or differently tagged tracks; "
            "the spoken language is verified from the audio itself."
        )
    elif picked["tier"]:
        note = (
            f"The only dialogue track is tagged {picked['language']}, not English. Using it anyway "
            "and verifying the spoken language from the audio itself."
        )
    elif peers:
        note = (
            f"{len(peers) + 1} interchangeable English tracks; using stream {picked['index']} "
            f"({picked['channels']} channels) because it carries the fullest mix."
        )
    return {
        "stream": picked["index"],
        "language_tag": picked["language"],
        "channels": picked["channels"],
        "tag_matched_english": picked["tier"] == 0,
        "alternates": [
            {"stream": track["index"], "language_tag": track["language"], "channels": track["channels"]}
            for track in tracks
            if track is not picked
        ],
        "note": note,
    }


def new_review_candidate_path(directory: Path, job: dict) -> Path:
    """Return the write path for this exact input signature."""
    signature = str(job.get("signature", ""))
    if len(signature) != 64 or any(character not in "0123456789abcdef" for character in signature):
        raise ValueError("Job has an invalid input signature")
    return directory / "candidates" / f"{job['id']}-{signature}.srt"


def review_candidate_path(directory: Path, job: dict) -> Path:
    """Find the saved candidate for reading, including the pre-0.4.4 name."""
    exact = new_review_candidate_path(directory, job)
    # Candidates created before 0.4.4 used only the job id. Keep the current review
    # usable after upgrade, while every newly written candidate gets an exact name.
    legacy = directory / "candidates" / f"{job['id']}.srt"
    try:
        report_candidate = Path(json.loads(job.get("report") or "{}").get("candidate", ""))
    except (TypeError, ValueError):
        report_candidate = Path()
    if not exact.exists() and report_candidate == legacy:
        return legacy
    return exact


def publish_candidate(job, settings, directory, db, rendered, report):
    media = Path(job["media"])
    # Recheck the input immediately before publishing. Never overwrite the provider's subtitle.
    if not current(job, settings):
        return {
            "state": "superseded",
            "error": "Inputs changed while processing",
            "report": json.dumps(report),
        }
    output = media.with_name(f"{media.stem}.crowbarr.{settings.language}.srt")
    if output.is_symlink():
        raise ReviewRequired("Output path is a symbolic link; refusing to replace it")
    if output.exists():
        if not db.owns_output(str(media), str(output), hashlib.sha256(output.read_bytes()).hexdigest()):
            raise ReviewRequired(
                "Existing Crowbarr output is untracked or externally edited; refusing to overwrite it"
            )
        # Preserve our preceding output too. Private state holds rollback copies, not the library scanner.
        backup = directory / "backups" / f"{job['id']}.srt"
        atomic_write(backup, output.read_text(encoding="utf-8"))
    # Journal intended content before the atomic rename so a crash cannot orphan a valid output.
    db.register_artifact(str(media), str(output), report["output_sha256"], job["id"])
    db.update(job["id"], stage="Publishing subtitles", output=str(output), report=json.dumps(report))
    # Serialize the publication boundary with library skip decisions. The worker
    # guard rechecks its generation and preference while holding this transaction.
    with db.connect() as connection:
        if db.worker_job is None:
            connection.execute("BEGIN IMMEDIATE")
        latest = connection.execute("SELECT library_blocked FROM jobs WHERE id=?", (job["id"],)).fetchone()
        if latest and latest[0]:
            from .db import StaleJob

            raise StaleJob("Library preference changed before publication")
        atomic_write(output, rendered, mode=0o644)
    if not current(job, settings):
        if output.exists() and hashlib.sha256(output.read_bytes()).hexdigest() == report["output_sha256"]:
            output.unlink()
        return {
            "state": "superseded",
            "error": "Inputs changed during publication",
            "report": json.dumps(report),
        }
    return {
        "state": "completed",
        "stage": "Ready to watch",
        "output": str(output),
        "report": json.dumps(report),
        "error": None,
    }
