# Architecture and ownership

Crowbarr is a service, not a command users run for each movie. Its durable source of
work is the Sonarr/Radarr catalog when configured, with the filesystem providing
video revisions and subtitle contents. Imports and subtitle downloads accelerate
discovery; periodic API reconciliation prevents reliance on perfect event delivery.
Explicit path mappings bridge different container namespaces. Without arr connections,
Crowbarr can instead discover videos directly in authorized media folders.

## Components

- `arr.py`: read-only Sonarr/Radarr API clients, monitored-file catalogs, and path mappings.
- `library.py`: traversal, source discovery, revision identity, settle/wait policy,
  and retirement of stale Crowbarr outputs on video replacement.
- `db.py`: transactional SQLite queue, observations, and operational notices.
- `service.py`: exclusive service lock, scanner thread, single worker, bounded
  retries, child-process lifetime, and media-server notification.
- `processor.py`: orchestration and publication; inference adapters are injectable.
- `subtitles.py`: SRT parsing, normalized phrase matching, reconstruction, validation.
- `media.py`: FFprobe metadata, audio selection, extraction, timeline offsets.
- `audit.py`: versioned before/after ASR timing evidence and conservative repair gates.
- `inference.py`: faster-whisper recognition and WhisperX forced alignment.
- `app.py`: authenticated API, hooks, settings, and locally served Bootstrap UI.

## State and revisions

`waiting → processing → completed | review | retry → failed`.
Jobs can also become `superseded` when inputs change. Manual retries are an exception
tool, not part of ordinary processing. Pause stops new jobs, not an in-flight job.

Identity includes the video path/size/mtime, SRT content hash, and inference settings.
Repeated scans and duplicate hooks converge on the same job. Video hashing is a
metadata tradeoff: replacement with identical size and preserved nanosecond mtime
is not detected. A full content-hash option is a future enhancement.

Only one service instance may own a data directory. SQLite is for a local filesystem,
not NFS/SMB or active/active replicas. Libraries may be mounted network shares with
the usual availability and write-permission requirements. Concurrency is deliberately
one job; scaling to distributed GPU workers requires a separate queue backend.

## Publication contract

Provider SRTs are never rewritten. Output is `stem.crowbarr.en.srt`; the scanner
excludes that name. Updates replace only this separate sidecar, with previous content
backed up in private state. When a video changes, the prior generated sidecar is
retired only if its hash matches Crowbarr's report. Externally changed outputs produce
an attention notice rather than an automatic deletion.

Quality reports include mode, preserved cues, text-match coverage, generated-word
fraction, selected audio stream, timeline offset, and rejected quality checks.
Thresholds must be calibrated on a representative corpus. There is no confidence
number in this release that can honestly be read as a probability of correctness.

## Integration boundaries

Sonarr/Radarr own downloads and imports. Bazarr owns subtitle provider searches.
Crowbarr owns timing, fallback generation, and its own output. Plex owns library
indexing and player preferences. No API key is required for scanner-only operation.
Authenticated hooks take events, not user-specified shell commands or output paths.

Catalog replacement is transactional and occurs only after a complete successful
API read. Sonarr's episode-file ID deduplicates multi-episode releases. Movie files
must actually exist in Radarr's catalog; missing/wanted movies do not become jobs.
The queue checks catalog eligibility before claiming work. Outages preserve the
last catalog and history while holding new jobs for the failed service. Successful
removal or unmonitoring supersedes pending work and stops affected active work;
previously published subtitles are preserved. Catalogs are refreshed after restarts
before claiming API-managed jobs. Filesystem fallback is never implicit on API failure.

API contracts: [Sonarr v3](https://github.com/Sonarr/Sonarr/blob/develop/src/Sonarr.Api.V3/openapi.json)
and [Radarr v3](https://github.com/Radarr/Radarr/blob/develop/src/Radarr.Api.V3/openapi.json).

Future adapters can add Jellyfin/Emby refreshes, additional language matchers,
provider search orchestration, or alternative ASR/aligner implementations. Keep
these behind narrow interfaces; do not put media-manager-specific rules into matching.

## Initial limits

English same-language speech, text SRTs plus convertible embedded subtitles, bounded
60,000-token matching, and local single-host state. No translation alignment, OCR,
subtitle rendering/burn-in, distributed workers, or silent auto-approval of uncertain
results. Reordered scenes and standalone SDH cues may require review. Input filenames
and text are escaped by the dashboard; credentials are write-only through its API.
