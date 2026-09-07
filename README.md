# Crowbarr

**Automatic audio-grounded subtitles for the arr ecosystem.**

Crowbarr follows your Sonarr and Radarr libraries, gives Bazarr time to find authored subtitles,
audits their timing against recognized dialogue, and repairs only demonstrated timing problems.
When no authored subtitle exists, it transcribes the selected audio track. There is
no file-upload step and no routine manual submission.

**Status: 0.3 preview.** The workflow and quality checks are implemented, but this is
not a claim of perfect synchronization or a benchmarked production release. Start
with a test library. See [validation and limitations](docs/validation.md).

## What it does

- Automatically discovers imports, video replacements, and new or upgraded SRTs.
- Reads Sonarr/Radarr's v3 APIs, respects monitored items, and maps container paths.
- Combines scheduled reconciliation with optional Sonarr/Radarr/Bazarr hooks.
- Uses a persistent SQLite queue with retry backoff and restart recovery.
- Looks for English sidecars and embedded text subtitles before generating text.
- Uses faster-whisper for phrase locations and WhisperX for forced word alignment.
- Audits existing subtitles first; passing originals are left unchanged.
- Requires every authored cue to survive and measured timing improvement before publishing a repair.
- Publishes a separate `Movie.crowbarr.en.srt` only when heuristic checks pass.
- Keeps original subtitles, prior Crowbarr outputs, and per-job quality reports.
- Retires a verified Crowbarr subtitle when its video is replaced.
- Offers a local Bootstrap dashboard for settings, activity, reports, and retries.
- Can request Plex library refreshes after publication.

English audio and English subtitles are supported initially. Translations, image
subtitle OCR, forced-only tracks, overlapping dialogue, and reordered scenes are
not silently treated as solved. Jellyfin/Emby can consume the sidecars through their
normal library scans; dedicated refresh adapters are not implemented yet.

## Install with Docker Compose

Requirements: Docker Engine with Compose v2, a writable media library, and disk space
for model downloads and temporary mono audio (about 115 MB per audio hour). The CPU
build needs no GPU. Inference dependencies make the image substantial even with a
small speech model. Initial model downloads require internet access.

```sh
cp .env.example .env
# Edit .env: set MEDIA_PATH, CONFIG_PATH, PUID and PGID for your server.
mkdir -p data
# Ensure CONFIG_PATH and MEDIA_PATH are writable by the configured PUID:PGID.
docker compose up -d --build
docker compose exec crowbarr cat /config/admin-token
```

Open **http://localhost:8449**, sign in with the generated key, and connect Sonarr
and/or Radarr under **Settings → Media managers**. Enter their URLs and API keys.
If paths differ, add mappings such as `/tv => /media/tv` and `/movies => /media/movies`.
Mapping destinations authorize media access and need not be repeated under Media
folders. With identical paths, authorize those paths under Media folders instead.
Do not mount Docker's socket or run privileged containers.

Without Sonarr or Radarr, configure Media folders for standalone filesystem discovery.
When either arr service is configured, its API catalog determines what gets processed;
Crowbarr does not also scan unrelated videos in the configured folders. A disconnected
arr service holds its new jobs until the next successful sync, instead of bypassing
monitoring preferences with a filesystem fallback.

The default port binds to localhost. Set `CROWBARR_BIND` to your server's LAN address
when accessing from another machine or using arr hooks. Treat the key as an admin
credential; use HTTPS through your own reverse proxy beyond a trusted local network.
Persistent configuration contains service credentials and must not be published.

Crowbarr waits 30 minutes for Bazarr by default. An available sidecar bypasses that
wait after the file-stability window. The existing library follows the same policy.
Reduce the wait in Settings if desired. No configured media folders or mappings leaves it idle.

### NVIDIA GPU

Install NVIDIA Container Toolkit on your host, then:

```sh
docker compose -f compose.yaml -f compose.cuda.yaml up -d --build
```

Select **NVIDIA GPU (CUDA)** and an appropriate precision in Settings. A 2 GB GPU is
not assumed sufficient for every model; CPU INT8 is the conservative default.
Crowbarr runs recognition and alignment sequentially in one isolated job process,
and releases that process after each job. Choose a larger memory limit if needed.
The provided GPU image targets CUDA 12.8. Linux x86-64 is the initial container target;
ARM containers and hardware-specific throughput have not been certified.

## Connect your media workflow

**Sonarr/Radarr are the primary library integrations.** Crowbarr fetches imported
movie files from Radarr, and series, episode files, and episode monitoring states
from Sonarr. Wanted items without a downloaded file are not queued. Multi-episode
files are processed once. Only monitored series/episodes/movies are included by
default; disable that option per service to include downloaded unmonitored items.
Periodic API syncs detect imports, upgrades, renames, removals, and monitoring changes.

In Sonarr/Radarr's Settings → Connect, optionally add a Webhook for imports,
upgrades, renames, and deletions. Use `http://YOUR-CROWBARR:8449/api/hooks/sonarr` or
`/api/hooks/radarr`, with username `crowbarr` and password equal to Crowbarr's API key.
Events request an immediate catalog reconciliation; periodic API syncs cover missed
events. Test events are accepted without scheduling media work. Windows remote paths
are supported through explicit mappings; matching uses directory boundaries and the
longest matching prefix. API credentials are sent as headers, not URL parameters.

**Bazarr:** keep subtitle providers and automatic searches enabled. Crowbarr discovers
downloaded SRTs directly from the library. Disable Bazarr's automatic synchronization
if Crowbarr is to own timing. Do not configure another tool to rewrite Crowbarr's
output files. Optional service URLs/API keys in Crowbarr can test connectivity;
Crowbarr does not currently invoke Bazarr searches or configure Bazarr for you.

**Faster Bazarr notifications (optional):** mount the supplied
[`integrations/bazarr-notify.py`](integrations/bazarr-notify.py) inside Bazarr, set its
`CROWBARR_URL` and `CROWBARR_API_KEY` environment variables, and use
`python3 /hooks/bazarr-notify.py` as the custom post-processing command. You do not
need this hook for automation; the periodic scanner discovers the same downloads.

**Plex:** save a Plex URL and token in Crowbarr Settings to request library refreshes
after publishing. It refreshes movie/TV sections rather than guessing container path
mappings. Normal Plex subtitle language/track preferences still control selection;
Crowbarr does not force a particular subtitle track in the player.

Bazarr may consider a generated sidecar sufficient to satisfy a missing language.
Configure its upgrade policy if you want later provider downloads. Crowbarr handles
those downloads when they arrive; it does not promise Bazarr will keep searching.

## Audit-first decisions

The first audit still transcribes the full selected audio track with faster-whisper.
It is not yet a cheap sampled-audio audit. Unchanged input revisions are remembered;
passing or inconclusive audits do not run WhisperX. Missing subtitles still use the
full generation pipeline.

Each audit reports cue coverage, signed timing offset, median/p95 boundary differences,
and per-cue evidence. The dashboard shows before/after metrics and offers a JSON download.
Positive signed differences mean the subtitle is later than recognized speech.

Version 1 requires confident anchors for **every cue**, at least three cues and twelve
recognized words, and no more than 10% unmatched recognized dialogue. Anchor words
must each have probability at least 0.8. Passing requires start differences within
0.75 seconds, end differences within 1 second, and valid subtitle structure.
Repair is justified when p95 boundary difference exceeds 2 seconds or at least 20%
of supported cues differ by more than 1 second. Other cases are inconclusive.

A repair must preserve all authored cues, pass the same audit, reduce p95 difference
by at least 50% and 0.5 seconds, and avoid worsening any cue by more than 0.25 seconds.
All existing publication checks also apply. Passing originals retire only verified,
Crowbarr-owned sidecars, with a backup, so an earlier repair does not remain beside
the now-correct original. Untracked or edited outputs require attention.

These thresholds are provisional heuristics, not measured accuracy guarantees.
The before/after comparison uses ASR timestamps as its reference; it is independent
of the candidate's timestamps, but is not an independent human or acoustic benchmark.
ASR errors can affect both audit and repair. Conservative coverage means short cues,
sound captions, and recognition disagreements will often need attention.
Audit policy changes invalidate prior input signatures and trigger fresh evaluation.

## How alignment works

1. Discover a stable video revision and wait for provider subtitles.
2. Select an English dialogue track, excluding known commentary/descriptive tracks.
3. Extract mono audio and retain its offset on the video's timeline.
4. Recognize word locations with the configured Whisper model.
5. Match unique phrase anchors against the SRT, ignoring its old timestamps.
6. Preserve supported authored cues; generate text for uncovered speech.
7. Audit existing cue timing. Stop if it passes or is inconclusive; otherwise force-align the supported passages.
8. Re-audit the candidate for improvement and check coverage, alignment scores, cue durations, overlaps, and bounds.
9. Atomically publish a separate sidecar, or retain a report under Needs attention.

A successful model call is not a quality verdict. The gates are heuristics, not
calibrated probabilities. Repeated phrases, deleted scenes, recognition mistakes,
and sound captions can require review. Crowbarr does not manufacture timings to
make an incomplete alignment appear successful.

## Develop

Python 3.11–3.13 and FFmpeg/ffprobe are required. Native development is supported
on Linux/macOS; the process lock and process-group cleanup use Unix APIs.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
pytest
ruff check crowbarr tests integrations
crowbarr --host 127.0.0.1
```

Install `pip install -e '.[inference]'` to process real media. The application and
deterministic pipeline tests do not require downloading models. Set `CROWBARR_DATA`
to change the local data directory. Optional `CROWBARR_API_KEY` overrides the generated
key. Never check `.env`, `data/`, model caches, or real library samples into a repository.

See [architecture](docs/architecture.md), [validation](docs/validation.md), and
[contributing](CONTRIBUTING.md). Licensed under MIT; model weights and bundled
dependencies retain their own licenses. Bootstrap's license is bundled with its CSS.
