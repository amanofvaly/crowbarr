# Crowbarr

**Audio-grounded subtitle synchronization and transcription for the `*arr` ecosystem.**

Crowbarr monitors Sonarr and Radarr, or your custom media libraries to keep subtitles in sync with spoken dialogue. It waits for Bazarr to find human-authored subtitles, verifies their timing against the audio with Whisper, and retimes them when sync drifts. If no subtitle is found, it transcribes one from scratch.


See [workflow.html](workflow.html) for a complete pipeline walkthrough and [CHANGELOG.md](CHANGELOG.md) for release notes.

---

## Features

- Audits subtitle timing against audio and preserves accurate files
- Retimes drift and offset errors while keeping human-authored text
- Transcribes speech with Whisper when no subtitle is available
- Integrates with Sonarr & Radarr via scheduled polling or instant webhooks
- On-demand audit or fresh transcription for any title
- CPU by default, NVIDIA CUDA acceleration supported

---

## Installation

Docker images published per release for `linux/amd64`:
- `ghcr.io/amanofvaly/crowbarr:latest` (CPU)
- `ghcr.io/amanofvaly/crowbarr:latest-cuda` (NVIDIA)

Platform guides:
- [Docker and Docker Compose](docs/docker.md)
- [TrueNAS and Portainer](docs/nas.md)
- [Unraid](docs/unraid.md)
- [Native Linux, no Docker](docs/linux.md)

Replacing an existing install? Read [Migration and backup](docs/migration.md) first.

### Quick Start (Linux x86_64 systemd)

```sh
curl -fsSL https://github.com/amanofvaly/crowbarr/releases/latest/download/install-crowbarr.sh | sudo bash
```

Open `http://YOUR-IP-ADDRESS:8449`. Data is stored in `/var/lib/crowbarr`.

### First Run Setup

1. Set your dashboard password.
2. Add Sonarr and Radarr under **Settings → Media managers** (add path mappings if paths differ).
3. Select your Whisper model and processing device under **Settings → Processing**.

---

## Updating

- **Native Linux**: Run `sudo crowbarr-update`.
- **Docker / Containers**: Pull the latest image and recreate the container (or use the [release-only Portainer setup](docs/nas.md#portainer)).

Data and queues in `/var/lib/crowbarr` are preserved across updates; interrupted jobs resume automatically. When an update changes the audit policy, `review` and `failed` jobs are automatically re-evaluated.

---

## Connecting Your Services

### Sonarr & Radarr

Crowbarr periodically polls Sonarr and Radarr APIs. For instant triggers on import, upgrade, or rename, add a webhook in **Sonarr/Radarr → Settings → Connect**:

- **Type**: Webhook
- **URL**: `http://YOUR-CROWBARR:8449/api/hooks/sonarr` (or `/radarr`)
- **Username**: `crowbarr`
- **Password**: Your Crowbarr API key (from **Settings**)
- **Triggers**: On Download, On Upgrade, On Rename, On Delete

### Bazarr (Recommended Hook)

To skip the 30-minute subtitle grace period when Bazarr finishes downloading:

1. Copy [`integrations/bazarr-notify.py`](integrations/bazarr-notify.py) into your Bazarr `config/` directory.
2. In that directory, create `crowbarr-notify.json` (or set `CROWBARR_URL` and `CROWBARR_API_KEY` env vars):
   ```json
   {
     "url": "http://YOUR-CROWBARR:8449",
     "api_key": "YOUR_CROWBARR_API_KEY"
   }
   ```
3. In **Bazarr → Settings → Subtitles → Post-processing**, enable post-processing and set:
   ```sh
   python3 /config/bazarr-notify.py "{{episode}}" "{{subtitles}}" "{{provider}}" "{{score}}"
   ```

### Plex

Under **Settings → Media servers**, supply your Plex URL and token. Crowbarr will refresh the parent movie or show section whenever subtitles are published.

---

## External API

Authenticate via `X-Api-Key: <key>` or `Authorization: Bearer <key>` (generated under **API & webhooks**).

- `GET /api/openapi.json` — Authenticated OpenAPI 3.0 schema.
- `GET /api/jobs?state={queue|review|history|all}&q={query}&offset=0&limit=25` — Paginated jobs list (limit: 1–100).
- `GET /api/jobs/{id}` — Status, progress (`progress_current` / `progress_total` in seconds), and audit evidence.
- `GET /api/media?q={query}&provider={all|sonarr|radarr|folders}&offset=0&limit=25` — Search library by title or path.
- `POST /api/process` — Request audit (`{"media": "/path/video.mkv"}`) or transcription (`{"media": "...", "directive": "generate"}`). Returns HTTP 202 with `job_id`.

---

## How It Works

```
New Media (Sonarr/Radarr)
         │
         ▼
  Wait for Bazarr  ──────(Bazarr webhook skips 30m wait)
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
 └─► Out of sync ────► Re-time ───┴──► Publish .crowbarr.en.srt (refresh Plex)
```

See [workflow.html](workflow.html) for a detailed walk-through of each pipeline step.

---

## Known Limitations

- **English dialogue & subtitles**: Initial focus is English audio and subtitles. Multilingual transcription and cross-lingual alignment are planned.
- **Text subtitles only**: Supports `.srt`, `.ass`, `.ssa`, `.vtt`, and embedded text tracks. Bitmap formats (`pgs`, `dvd_subtitle`) require OCR (currently unsupported); they are treated as missing subtitles, prompting a Bazarr request or sidecar generation.
- **Single job worker**: Jobs run sequentially through an SQLite-backed queue to avoid CPU and GPU memory exhaustion.
- **Processing speed & caching**: Audio features are cached per file. First passes require audio extraction and recognition, while re-checks complete in seconds.

---

## Development

Requires Python 3.11+ and FFmpeg. See [CONTRIBUTING.md](CONTRIBUTING.md) for versioning and changelog rules.

```sh
# Setup and testing
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[test]'
pytest
ruff check crowbarr tests integrations

# Run local development server
crowbarr --host 127.0.0.1 --port 8449

# Optional: real media processing locally
pip install -e '.[inference]'
```

To build container images locally:
```sh
docker build -t crowbarr:dev .                          # CPU
docker build -f Dockerfile.cuda -t crowbarr:dev-cuda .   # NVIDIA
```

---

## License

Licensed under the [MIT License](LICENSE). Speech models and third-party assets are governed by their respective licenses.
