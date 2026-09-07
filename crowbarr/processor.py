from __future__ import annotations

import hashlib
import json
import tempfile
import textwrap
from pathlib import Path
from statistics import median

from .audit import audit, improved
from .config import Settings, atomic_write
from .db import Database
from .library import allowed, signature, source_subtitle, subtitle_sources
from .media import ReviewRequired, choose_audio, embedded_subtitles, extract_audio, probe
from .subtitles import Cue, Word, match_passages, parse_srt, render_srt, validate_cues


def _retime_authored(original: list[Cue], before: dict) -> tuple[list[Cue], dict]:
    """Fit a robust offset + drift transform from distributed audio anchors."""
    points = []
    for item in before["evidence"]:
        points.extend(
            [
                (item["subtitle_start"], item["audio_start"]),
                (item["subtitle_end"], item["audio_end"]),
            ]
        )
    slopes = [
        (right_y - left_y) / (right_x - left_x)
        for index, (left_x, left_y) in enumerate(points)
        for right_x, right_y in points[index + 1 :]
        if right_x - left_x >= 30
    ]
    scale = median(slopes) if slopes else 1.0
    intercept = median(audio - scale * subtitle for subtitle, audio in points)
    residuals = sorted(abs(audio - (scale * subtitle + intercept)) for subtitle, audio in points)
    p95 = residuals[max(0, (len(residuals) * 95 + 99) // 100 - 1)]
    model = {
        "kind": "robust_affine",
        "scale": scale,
        "offset_seconds": intercept,
        "anchor_points": len(points),
        "median_residual_seconds": median(residuals),
        "p95_residual_seconds": p95,
    }
    if not 0.95 <= scale <= 1.05:
        return [], model
    return [Cue(scale * cue.start + intercept, scale * cue.end + intercept, cue.text) for cue in original], model


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
        indexes = {
            round(index * (len(region) - 1) / (per_region - 1)) for index in range(per_region)
        }
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


def _recognized_words(
    media: Path, audio: Path, settings: Settings, directory: Path, model_cache: Path, transcriber
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
                return words, list(payload.get("issues", [])), True
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
    words, issues = transcriber(audio, settings, model_cache)
    atomic_write(
        transcript_path,
        json.dumps(
            {
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
    transcript_files = sorted(
        transcript_path.parent.glob("*.json"), key=lambda path: path.stat().st_mtime_ns
    )
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
    from .inference import align, transcribe

    cache_transcript = transcriber is None
    refine_generated = aligner is not None or settings.device == "cuda"
    transcriber, aligner = transcriber or transcribe, aligner or align
    if not current(job, settings):
        return {"state": "superseded", "error": "Media, subtitle, or processing settings changed"}
    media = Path(job["media"])
    metadata = probe(media)
    audio_stream = choose_audio(metadata, settings)
    cache = directory / "models"
    cache.mkdir(exist_ok=True)
    work = directory / "work"
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{job['id']}-", dir=work) as temporary:
        audio = Path(temporary) / "audio.wav"
        offset, duration = extract_audio(media, audio, metadata, audio_stream)
        issues = []
        warnings = []
        candidate_paths = [
            ("external", path, str(path)) for path in subtitle_sources(media, settings)
        ]
        candidate_paths.extend(
            ("embedded", path, f"embedded stream {path.stem.removeprefix('embedded-')}")
            for path in embedded_subtitles(media, Path(temporary), metadata, settings)
        )
        candidates = []
        for kind, source, label in candidate_paths:
            try:
                candidates.append(
                    {"kind": kind, "path": source, "label": label, "cues": parse_srt(source.read_text(encoding="utf-8-sig"))}
                )
            except (ValueError, UnicodeError) as error:
                warnings.append(f"Ignored unreadable {label}: {error}")
        if candidate_paths and not candidates:
            raise ReviewRequired("No discovered subtitle source could be read")
        db.update(job["id"], stage="Recognizing dialogue")
        if cache_transcript:
            words, transcription_issues, transcript_cache_hit = _recognized_words(
                media, audio, settings, directory, cache, transcriber
            )
        else:
            words, transcription_issues = transcriber(audio, settings, cache)
            transcript_cache_hit = False
        # ASR uncertainty is local evidence quality, not a global publication veto.
        warnings.extend(transcription_issues)
        timeline_words = [type(w)(w.start + offset, w.end + offset, w.text, w.probability) for w in words]
        arbitration = []
        for candidate in candidates:
            candidate_audit = audit(candidate["cues"], timeline_words, duration)
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
            decision = {"inconclusive": 0, "repair": 1, "pass": 2}[candidate_audit["decision"]]
            p95 = candidate_audit["p95_error_seconds"]
            return (
                decision,
                candidate_audit["matched_token_ratio"] >= settings.min_match_ratio,
                int(candidate_audit["matched_token_ratio"] * 20),
                -(p95 if p95 is not None else float("inf")),
                candidate_audit["matched_token_ratio"],
                candidate_audit["dialogue_coverage"],
            )

        selected = max(candidates, key=candidate_rank) if candidates else None
        original = selected["cues"] if selected else []
        db.update(job["id"], stage="Matching authored subtitles")
        passages, report = match_passages(original, words, settings.min_match_ratio)
        report.update(
            mode="authored_timing" if original else "generated",
            model=settings.model,
            transcript_cache_hit=transcript_cache_hit,
            source_arbitration=arbitration,
            selected_source=selected["label"] if selected else None,
            selected_source_kind=selected["kind"] if selected else None,
        )
        if original:
            db.update(job["id"], stage="Auditing existing subtitle")
            before = selected["audit"]
            report["audit"] = {"before": before, "after": None, "improved": False}
            if before["decision"] != "repair":
                passed = before["decision"] == "pass"
                report.update(
                    issues=issues,
                    warnings=warnings,
                    output_cues=0,
                    quality="audit_passed" if passed else "review",
                    note="Measured against ASR word timestamps; these are heuristic estimates, not ground truth.",
                )
                atomic_write(directory / "reports" / f"{job['id']}.json", json.dumps(report, indent=2))
                if not current(job, settings):
                    return {
                        "state": "superseded",
                        "error": "Inputs changed during audit",
                        "report": json.dumps(report),
                    }
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
        db.update(job["id"], stage="Aligning words to audio")
        eligible_passages = [passage for passage in passages if passage.authored] if original else passages
        if original:
            aligned, timing_model = _retime_authored(original, before)
            alignment_passages = before["evidence"]
            if not aligned:
                issues.append("Audio anchors imply an implausible timing scale")
        else:
            timing_model = None
            baseline = _generated_baseline(passages)
            if refine_generated:
                alignment_passages = _distributed_sample(eligible_passages, duration)
                aligned, alignment_issues = aligner(audio, alignment_passages, settings, cache)
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
                warnings.append("Skipped optional WhisperX refinement on CPU to limit memory use")
            aligned, adjusted_overlaps = _stabilize_generated(aligned, baseline)
            if adjusted_overlaps:
                warnings.append(
                    f"Adjusted {adjusted_overlaps} generated cue boundaries to prevent overlap"
                )
        aligned = [Cue(c.start + offset, c.end + offset, c.text) for c in aligned]
        for validation_issue in validate_cues(aligned, duration):
            if "unsuitable reading duration" in validation_issue:
                warnings.append(validation_issue)
            else:
                issues.append(validation_issue)
        if original:
            after = audit(aligned, timeline_words, duration)
            better = [c.text for c in aligned] == [c.text for c in original] and improved(before, after)
            report["audit"].update(after=after, improved=better)
            if not better:
                issues.append(
                    "Repair did not demonstrate sufficient timing improvement while preserving every authored cue"
                )
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
