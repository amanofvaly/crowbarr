# Crowbarr

**Audio-grounded subtitle synchronization and transcription for the `*arr` ecosystem.**

Crowbarr connects to your Sonarr and Radarr libraries to keep subtitles in sync with dialogue. Rather than immediately generating new transcripts or blindly shifting files, Crowbarr uses an **audit-first** approach: it gives Bazarr time to find human-authored subtitles, checks their timing against the actual audio track using Whisper, and only adjusts timing when genuine drift or sync errors are detected. If no authored subtitle is found, it transcribes the dialogue from scratch.

All processed subtitles are published alongside your media as separate `.crowbarr.en.srt` sidecar files—your original media and provider subtitles are never overwritten or modified.

> **Visual Guide**: Check out [workflow.html](workflow.html) for an end-to-end walkthrough of the entire pipeline, file changes, and triggers.
>
> **Updating**: See [Updating](#updating). Release notes are in [CHANGELOG.md](CHANGELOG.md).

---

## Features

- **Non-destructive**: Never touches your original media or downloaded subtitles. Outputs clean `<filename>.crowbarr.en.srt` sidecars.
- **Audit-first synchronization**: Analyzes existing subtitles against audio. Subtitles that are already in sync are left alone.
- **Accurate speech alignment**: Uses `faster-whisper` for speech recognition and `WhisperX` forced alignment to lock cue timings to spoken dialogue.
- **Automatic transcription**: Generates full, properly-timed subtitle tracks when no authored subtitles exist.
- **Deep `*arr` integration**: Automatically tracks library updates from Sonarr and Radarr via API polling or instant webhooks.
- **Bazarr coordination**: Configurable grace period gives Bazarr time to fetch subtitles before transcription kicks in. A lightweight Bazarr notification script enables immediate processing on download.
- **Web dashboard**: Full-screen subtitle workspace with live queues, measured recognition progress, searchable libraries, review workflows, settings, and an external API.
- **On-demand actions**: Search any movie or episode to manually run an audit or force a fresh audio transcription.
- **Plex notifications**: Can automatically notify Plex to refresh metadata after publishing a subtitle.
- **Hardware acceleration**: Runs efficiently on CPU by default, with native NVIDIA GPU (CUDA) support for fast processing.

---

## How It Works

```
New Media (Sonarr/Radarr)
         │
         ▼
  Wait for Bazarr  ──────(Bazarr webhook speeds this up)
         │
         ▼
   Audio & Subtitle Check
         │
 ┌───────┴────────────────────────┐
 │ Authored subtitle found        │ No subtitle found
 ▼                                ▼
Audit timing against audio       Transcribe audio with Whisper
 │                                │
 ├─► Already in sync ──► Done     │
 │                                │
 └─► Out of sync ────► Re-time ───┴──► Publish .crowbarr.en.srt
```

1. **Discovery**: Crowbarr monitors your Sonarr and Radarr catalogs for new downloads, upgrades, and renames.
2. **Grace Period**: When a new video arrives without subtitles, Crowbarr waits (default: up to 30 minutes) for Bazarr to fetch human-authored subtitles. If Bazarr downloads a subtitle, processing begins immediately.
3. **Dialogue Audit**: For existing subtitles, Crowbarr extracts audio windows and compares subtitle cue start times with recognized speech.
4. **Correction or Transcription**:
   - If the existing subtitle is well-timed, Crowbarr takes no action.
   - If timing drift or offset is detected, Crowbarr realigns the cues to speech while preserving all original text and authoring.
   - If no usable subtitle exists, Crowbarr transcribes the dialogue to create a new track.
5. **Publication**: The verified subtitle is written as `<name>.crowbarr.en.srt`, and Crowbarr can trigger a Plex library scan to pick it up.

For a detailed walkthrough of the entire pipeline, see [workflow.html](workflow.html).

---

## Installation

Linux x86_64:

```sh
curl -fsSL https://github.com/amanofvaly/crowbarr/releases/latest/download/install-crowbarr.sh | sudo bash
```

Open `http://localhost:8449`. The installer adds Crowbarr as a system service and keeps
its database, settings, models, and cached transcripts in `/var/lib/crowbarr`.

For Docker and NVIDIA installations, see [Install Crowbarr with Docker](docs/docker.md).

### First run

1. Set your dashboard password.
2. Add Sonarr and Radarr under **Settings, Media managers**.
3. Add path mappings when those services report different media paths.
4. Choose the model and processing device.

---

## Updating

```sh
sudo crowbarr-update
```

The service stops while the program is replaced, then resumes with the existing data in
`/var/lib/crowbarr`. An interrupted job returns to the queue. Other queued and completed
work remains in place.

Some releases change the audit policy version shown in the dashboard footer. Crowbarr
then rechecks jobs in review or failed. Passed and skipped results remain settled. See
[CHANGELOG.md](CHANGELOG.md).

---

## Connecting Your Services

### Sonarr & Radarr

Crowbarr periodically polls Sonarr and Radarr APIs to detect imported, renamed, or deleted media. 

For instant processing without waiting for scheduled polls, add webhooks in **Sonarr/Radarr → Settings → Connect**:
- **Type**: Webhook
- **URL**: `http://YOUR-CROWBARR:8449/api/hooks/sonarr` (or `/radarr`)
- **Username**: `crowbarr`
- **Password**: Your Crowbarr API key (found in **Settings**)
- **Triggers**: On Download, On Upgrade, On Rename, On Delete

### Bazarr (Recommended Hook)

Crowbarr automatically detects subtitles downloaded by Bazarr during routine scans. However, to avoid waiting out the subtitle grace period, install the Bazarr post-processing notification script:

1. Copy [`integrations/bazarr-notify.py`](integrations/bazarr-notify.py) into your Bazarr `config/` directory.
2. In the same directory, create a `crowbarr-notify.json` configuration file:
   ```json
   {
     "url": "http://YOUR-CROWBARR:8449",
     "api_key": "YOUR_CROWBARR_API_KEY"
   }
   ```
   *(Alternatively, set `CROWBARR_URL` and `CROWBARR_API_KEY` environment variables in your Bazarr container).*
3. In **Bazarr → Settings → Subtitles → Post-processing**, enable post-processing and set the command to:
   ```sh
   python3 /config/bazarr-notify.py "{{episode}}" "{{subtitles}}" "{{provider}}" "{{score}}"
   ```

With this hook, Crowbarr triggers an audit the moment Bazarr finishes saving a subtitle.

### Plex

Under **Settings → Media servers**, provide your Plex server URL and authentication token. When Crowbarr publishes a retimed or generated subtitle, it automatically tells Plex to refresh the parent show or movie section.

---

## Web Dashboard & Manual Controls

The web UI provides real-time visibility into the queue and library:

- **Queue Management**: Pause processing, reorder jobs, cancel pending items, or retry failed ones.
- **Audit Reports**: Click any completed job to see cue coverage, measured timing offset, and before/after boundary errors.
- **Search & Manual Actions**: Use the search bar above the queue to find any movie or episode in your library:
  - **Audit**: Immediately checks the existing subtitle against audio and realigns it if it is out of sync.
  - **Generate Fresh**: Ignores existing subtitles and transcribes the dialogue from scratch. Ideal if an existing subtitle is corrupt, mismatched, or poor quality.

---

## Known Limitations

- **English Dialogue & Subtitles**: Initial releases focus on English audio and English subtitles. Multilingual transcription and translation alignment are not yet supported.
- **Text Subtitles Only**: Crowbarr reads text subtitle formats (SubRip `.srt`, `.ass`, `.ssa`, `.vtt`, and text-based MP4/MKV embedded tracks). Image/bitmap subtitles (`dvd_subtitle`, `hdmv_pgs_subtitle`) common in DVD and Blu-ray remuxes require OCR, which is not currently supported. A remux whose only English subtitle is a bitmap therefore appears to Crowbarr as having no English subtitle: it will request one from Bazarr and publish a sidecar alongside the track you already have. The job report lists any subtitle it could not read.
- **Processing Speed Varies**: Recognized audio is cached per file and reused on later checks, so a re-run finishes in seconds while a first pass must extract audio and run recognition. The dashboard states which applies to the job in progress. Do not use re-run timings to estimate how long a full library will take.
- **Single Job Worker**: To prevent server CPU and GPU memory exhaustion, jobs are processed one at a time via a local SQLite-backed queue.

---

## Development

Crowbarr requires Python 3.11+ and FFmpeg/ffprobe. See [CONTRIBUTING.md](CONTRIBUTING.md).

A change that can alter an audit result must bump `AUDIT_POLICY_VERSION` and add a [CHANGELOG.md](CHANGELOG.md) entry. The test suite checks for this.

```sh
# Set up virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install development dependencies
pip install -e '.[test]'

# Run test suite
pytest

# Lint and format check
ruff check crowbarr tests integrations

# Run development server
crowbarr --host 127.0.0.1 --port 8449
```

To process real media locally, install inference dependencies:
```sh
pip install -e '.[inference]'
```

Release images are built by GitHub Actions on a `v*` tag. To build one locally:
```sh
docker build -t crowbarr:dev .                          # CPU
docker build -f Dockerfile.cuda -t crowbarr:dev-cuda .   # NVIDIA
```

---

## License

This project is licensed under the [MIT License](LICENSE). Third-party speech models, dependencies, and Bootstrap assets are governed by their respective licenses.


### Subtitle workspace

The frontend has dedicated Dashboard, Activity, Library, Review, History, Settings,
and API & webhooks screens. Dark and light themes are available in Appearance and
saved per browser. `/` opens library search; navigation routes can be bookmarked.
Activity and Library page through the complete queue and catalog, with server-side
search. Review provides audit evidence, private candidate downloads, and explicit
publication after checking a candidate against the video.

Recognition progress measures processed audio seconds against the recognition
workload (the entire runtime or the combined sampled windows). It is a **stage
percentage**, not an estimated total-job percentage. Preparation, alignment, and
publication show the current operation. Unknown resource telemetry is shown as
unavailable. Queue pause prevents new claims; it does not interrupt an active job.

### External API

Use `X-Api-Key: <key>` or `Authorization: Bearer <key>` on `/api/*` endpoints.
Retrieve the key in **API & webhooks**. It grants administrative access and should be
stored as a secret. Dashboard authentication uses its own session cookie.

- `GET /api/openapi.json`: authenticated OpenAPI schema, with machine authentication definitions.
- `GET /api/jobs?state=queue&q=title&offset=0&limit=25`: all queued work, paginated.
  `state` also accepts `review`, `history`, `all`, or an individual state.
- `GET /api/jobs/{id}`: state, numeric progress, and full audit evidence.
- `GET /api/media?q=title&provider=all&offset=0&limit=25`: search by display title or
  file path. Provider filters are `all`, `sonarr`, `radarr`, and `folders`.
- `POST /api/process` with `{"media":"/managed/path/video.mkv"}`: audit request;
  add `"directive":"generate"` for fresh generation. Returns HTTP 202 and `job_id`.

Pagination limits are 1–100. Numeric progress fields are `progress_current` and
`progress_total` in seconds; both become null when the job enters another stage.
`started` records the start of the current processing attempt.
