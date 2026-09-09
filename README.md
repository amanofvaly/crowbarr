# Crowbarr

**Audio-grounded subtitle synchronization and transcription for the `*arr` ecosystem.**

Crowbarr watches your Sonarr and Radarr libraries and keeps subtitles in sync with the
dialogue. It waits for Bazarr to find a human-written subtitle, checks that subtitle's
timing against the audio with Whisper, and corrects it only when the audio says it is
wrong. When no subtitle turns up, it transcribes one.

Results are written as `<filename>.crowbarr.en.srt` next to the video. Your media and
your existing subtitles are never modified.

[workflow.html](workflow.html) walks through the pipeline end to end. Release notes are
in [CHANGELOG.md](CHANGELOG.md).

---

## Features

- Audits existing subtitles against the audio and leaves correct ones alone
- Corrects drift and sync errors instead of replacing the subtitle
- Transcribes from audio when no subtitle is available
- Sonarr and Radarr integration by API polling or webhook
- Configurable grace period for Bazarr, with a notification script for instant handling
- Web dashboard with queues, progress, library search, review and settings
- On-demand audit or fresh transcription for any movie or episode
- Plex refresh after publishing
- CPU by default, NVIDIA CUDA supported

---

## Installation

Two images are published per release for `linux/amd64`:

- `ghcr.io/amanofvaly/crowbarr:latest` for CPU
- `ghcr.io/amanofvaly/crowbarr:latest-cuda` for NVIDIA

Pick your platform:

- [Docker and Docker Compose](docs/docker.md)
- [TrueNAS and Portainer](docs/nas.md)
- [Native Linux, no Docker](docs/linux.md)

Replacing an existing install? Read [Migration and backup](docs/migration.md) first.

Without Docker, on Linux x86_64 with systemd:

```sh
curl -fsSL https://github.com/amanofvaly/crowbarr/releases/latest/download/install-crowbarr.sh | sudo bash
```

Open `http://YOUR-IP-ADDRESS:8449`. Data lives in `/var/lib/crowbarr`.

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

This command updates manually. Container auto-updates use the managed
[release-only Portainer setup](docs/nas.md#portainer). Ordinary source pushes do not
update installations; a new application release must pass the release pipeline.

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

1. Crowbarr watches Sonarr and Radarr for new, upgraded and renamed files.
2. A new video without a subtitle waits up to 30 minutes for Bazarr. A Bazarr
   webhook skips the wait.
3. Crowbarr extracts audio and compares the subtitle's cue times against recognised
   speech.
4. A well-timed subtitle is left alone. A drifting one is re-timed, keeping the
   original text. If there is no usable subtitle, Crowbarr transcribes one.
5. The result is written as `<name>.crowbarr.en.srt`, and Plex can be told to rescan.

For a detailed walkthrough of the entire pipeline, see [workflow.html](workflow.html).

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
