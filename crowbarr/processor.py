from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from .audit import audit, improved
from .config import Settings, atomic_write
from .db import Database
from .library import allowed, signature, source_subtitle
from .media import ReviewRequired, choose_audio, embedded_subtitle, extract_audio, probe
from .subtitles import Cue, match_passages, parse_srt, render_srt, validate_cues


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
        source = (
            Path(job["source"])
            if job["source"]
            else embedded_subtitle(media, Path(temporary) / "embedded.srt", metadata)
        )
        original = []
        issues = []
        if source:
            try:
                original = parse_srt(source.read_text(encoding="utf-8-sig"))
            except (ValueError, UnicodeError) as error:
                raise ReviewRequired(f"Source subtitle cannot be read: {error}") from error
        db.update(job["id"], stage="Recognizing dialogue")
        words, transcription_issues = transcriber(audio, settings, cache)
        issues.extend(transcription_issues)
        db.update(job["id"], stage="Matching authored subtitles")
        passages, report = match_passages(original, words, settings.min_match_ratio)
        report.update(mode="hybrid" if original else "generated", model=settings.model)
        timeline_words = [type(w)(w.start + offset, w.end + offset, w.text, w.probability) for w in words]
        if original:
            db.update(job["id"], stage="Auditing existing subtitle")
            before = audit(original, timeline_words, duration)
            report["audit"] = {"before": before, "after": None, "improved": False}
            if issues:
                before["decision"] = "inconclusive"
                before["reason"] = "Speech recognition reported uncertainty"
            if before["decision"] != "repair":
                passed = before["decision"] == "pass"
                report.update(
                    issues=issues,
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
                issues.append("Too much dialogue would be replaced by generated text")
            if any(not p.text for p in passages):
                issues.append("Non-dialogue captions could not be aligned")
            from .subtitles import spoken

            if any(not spoken(c.text) for c in original):
                issues.append("Standalone sound captions need review; speech alignment cannot place them")
        db.update(job["id"], stage="Aligning words to audio")
        aligned, alignment_issues = aligner(audio, passages, settings, cache)
        issues.extend(alignment_issues)
        aligned = [Cue(c.start + offset, c.end + offset, c.text) for c in aligned]
        issues.extend(validate_cues(aligned, duration))
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
                "mode": "hybrid" if original else "generated",
                "audio_stream": audio_stream["index"],
                "audio_offset_seconds": offset,
                "issues": issues[:100],
                "issue_count": len(issues),
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
