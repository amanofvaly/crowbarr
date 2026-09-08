from __future__ import annotations

import hashlib
import json
import tempfile
import textwrap
from pathlib import Path
from statistics import median

from .audit import audit, improvement
from .config import Settings, atomic_write
from .db import Database
from .library import allowed, signature, source_subtitle, subtitle_sources
from .media import (
    ReviewRequired,
    audio_candidates,
    choose_audio,
    embedded_subtitles,
    extract_audio,
    probe,
    unreadable_subtitles,
)
from .subtitles import Cue, Word, match_passages, parse_srt, render_srt, validate_cues


def _fit_timing(points):
    # Bound the robust fit's memory for feature-length movies.
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
    intercept = median(y - scale * x for x, y in points)
    errors = sorted(abs(y - scale * x - intercept) for x, y in points)
    return {
        "scale": scale,
        "offset_seconds": intercept,
        "anchor_points": len(points),
        "median_residual_seconds": median(errors),
        "p95_residual_seconds": errors[max(0, (len(errors) * 95 + 99) // 100 - 1)],
    }


def _retime_authored(original: list[Cue], before: dict) -> tuple[list[Cue], dict]:
    evidence = sorted(before["evidence"], key=lambda e: e["subtitle_start"])
    points = [(e["subtitle_start"], e["audio_start"]) for e in evidence]
    if not points:
        return [], {"kind": "unsupported", "reason": "No anchors"}
    model = _fit_timing(points)
    model["kind"] = "robust_affine"
    if not 0.95 <= model["scale"] <= 1.05:
        return [], {**model, "reason": "Audio anchors imply an implausible timing scale"}

    def _piecewise():
        """Optional refinement. If it cannot be established, the global fit still stands."""
        residuals = [y - model["scale"] * x for x, y in points]
        cuts = []
        for index in range(8, len(points) - 8):
            left, right = residuals[index - 6 : index], residuals[index : index + 6]
            jump = abs(median(right) - median(left))
            spread = max(max(abs(v - median(side)) for v in side) for side in (left, right))
            if jump >= 2.5 and spread < 1.25 and (not cuts or index - cuts[-1] >= 8):
                cuts.append(index)
        if not cuts or len(cuts) > 6:
            return None
        boundaries = []
        for cut in cuts:
            left, right = evidence[cut - 1], evidence[cut]
            # Place a discontinuity only in a real subtitle gap bracketed by anchors.
            gaps = [
                (a.end, b.start)
                for a, b in zip(original, original[1:], strict=False)
                if a.end >= left["subtitle_end"] and b.start <= right["subtitle_start"] and b.start > a.end
            ]
            if not gaps:
                return None
            lo, hi = max(gaps, key=lambda pair: pair[1] - pair[0])
            boundaries.append((lo + hi) / 2)
        indexes = [0, *cuts, len(points)]
        built = []
        for index, (first, last) in enumerate(zip(indexes, indexes[1:], strict=False)):
            fitted = _fit_timing(points[first:last])
            if (
                last - first < 8
                or not 0.95 <= fitted["scale"] <= 1.05
                or fitted["p95_residual_seconds"] > 1.5
            ):
                return None
            built.append(
                {
                    **fitted,
                    "start": boundaries[index - 1] if index else 0,
                    "end": boundaries[index] if index < len(boundaries) else None,
                }
            )
        return built, {"kind": "piecewise_affine", "segments": built, "boundaries": boundaries}

    segments = []
    # Correctly timed subtitles scatter around the fit with a p95 near 1.5-2 s on real
    # media. Only residuals clearly above that noise floor suggest a real discontinuity.
    if model["p95_residual_seconds"] > 2.5 and len(points) >= 16:
        refined = _piecewise()
        if refined:
            segments, model = refined
    if not segments:
        # Judge the fit by its middle, not its worst points. Anchor sets are small -- at
        # 29 anchors the 95th percentile is simply the second-worst one -- so a p95 gate
        # lets two bad anchors veto a correction whose typical error is a tenth of a
        # second. Give up only when the bulk of the anchors disagree with the fit.
        if model["median_residual_seconds"] > 1.0 or model["p95_residual_seconds"] > 6.0:
            return [], {**model, "reason": "Timing residuals do not support a global correction"}
        segments = [{**model, "start": 0, "end": None}]
    result = []
    for cue in original:
        segment = next(
            (segment for segment in segments if segment["end"] is None or cue.start < segment["end"]),
            segments[-1],
        )
        if segment["end"] is not None and cue.end > segment["end"]:
            return [], {**model, "reason": "A caption crosses an uncertain discontinuity"}
        result.append(
            Cue(
                segment["scale"] * cue.start + segment["offset_seconds"],
                segment["scale"] * cue.end + segment["offset_seconds"],
                cue.text,
            )
        )
    return result, model


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
        # An English track Crowbarr cannot decode is still worth reporting: without this
        # the job looks as though the file had no English subtitle at all.
        for label in unreadable_subtitles(metadata):
            warnings.append(f"English {label} is an image subtitle and needs OCR; not used")
        provider_result = None
        if not candidates and not forced_generation and settings.bazarr.url and settings.providers():
            from .bazarr import try_alternative

            db.update(job["id"], stage="Searching Bazarr providers")
            provider_result = try_alternative(settings, db, media, directory, job["id"])
            if provider_result["state"] == "downloaded":
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

        if windows and all(candidate["audit"]["decision"] == "inconclusive" for candidate in candidates):
            full_audit_escalation = True
            recognition_stage[0] = "Sample inconclusive; auditing against full dialogue"
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
        )
        if original:
            db.update(job["id"], stage="Auditing existing subtitle")
            before = selected["audit"]
            report["audit"] = {"before": before, "after": None, "improved": False}
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
            elif before["decision"] != "repair":
                passed = before["decision"] == "pass"
                report.update(
                    issues=issues,
                    warnings=warnings,
                    output_cues=0,
                    quality="audit_passed" if passed else "review",
                    note="Measured against ASR word timestamps; these are heuristic estimates, not ground truth.",
                )
                atomic_write(directory / "reports" / f"{job['id']}.json", json.dumps(report, indent=2))
                if (
                    not passed
                    and before["matched_token_ratio"] < 0.5
                    and settings.bazarr.url
                    and settings.providers()
                ):
                    from .bazarr import reject_source, try_alternative

                    if selected["kind"] == "external":
                        report["rejected_source_hash"] = reject_source(
                            directory, str(media), selected["path"], before["reason"]
                        )
                    report["bazarr"] = try_alternative(settings, db, media, directory, job["id"])
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
                    "error": None if passed else before["reason"],
                    "report": json.dumps(report),
                }
        if original:
            if report["matched_token_ratio"] < settings.min_match_ratio:
                issues.append(
                    "Too little authored text matches this audio; the subtitle may be a different cut"
                )
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
            aligned, timing_model = _retime_authored(original, before)
            alignment_passages = before["evidence"]
            if not aligned:
                # Report why the model actually declined, not a guess about the scale.
                issues.append(timing_model.get("reason", "Audio anchors do not support a correction"))
        else:
            timing_model = None
            baseline = _generated_baseline(passages)
            if refine_generated:
                alignment_passages = _distributed_sample(eligible_passages, duration)
                try:
                    aligned, alignment_issues = aligner(audio, alignment_passages, settings, cache)
                except ImportError as error:
                    # The CUDA image ships recognition without WhisperX, which would pull
                    # a second CUDA torch stack for an off-by-default refinement. A build
                    # that cannot honour the setting must fall back to Whisper's own word
                    # timestamps and say so, not fail the job on an import.
                    warnings.append(
                        f"WhisperX refinement is not installed in this image ({error}); "
                        "used Whisper word timestamps instead"
                    )
                    alignment_passages, aligned = [], baseline
                else:
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
        for validation_issue in validate_cues(aligned, duration):
            if "unsuitable reading duration" in validation_issue:
                warnings.append(validation_issue)
            else:
                issues.append(validation_issue)
        if original:
            after = _audit_source(
                aligned, timeline_words, duration, windows, before.get("sample_cue_indices")
            )
            verdict = improvement(before, after)
            if [c.text for c in aligned] != [c.text for c in original]:
                verdict = {
                    "accepted": False,
                    "reason": "the repair did not preserve every authored line of text",
                    "checks": verdict["checks"],
                }
            report["audit"].update(after=after, improved=verdict["accepted"], improvement=verdict)
            if not verdict["accepted"]:
                issues.append(f"Repair withheld because {verdict['reason']}")
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
            candidate = directory / "candidates" / f"{job['id']}.srt"
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
