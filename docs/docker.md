# Install Crowbarr with Docker

Published images currently support Linux x86_64 (`linux/amd64`). ARM64 images are
not yet published. Use Docker Engine with its Compose plugin or a Docker-based NAS
app manager. Native Windows/macOS packages are not provided.

## Docker Compose

Save this as `compose.yaml`. Set the host storage paths and the numeric user/group
that can write to your media, then run `docker compose up -d`. No `.env` is required.

```yaml
services:
  crowbarr:
    image: ghcr.io/amanofvaly/crowbarr:latest
    restart: unless-stopped
    init: true
    user: "1000:1000"            # the user and group that own your media
    ports:
      - "8449:8449"
    volumes:
      - type: bind
        source: /srv/crowbarr/config   # existing private app directory
        target: /config
        bind:
          create_host_path: false
      - type: bind
        source: /path/to/media        # existing library directory
        target: /media
        bind:
          create_host_path: false
    stop_grace_period: 45s
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
```

Create the configuration directory first. For UID/GID 1000:

```sh
sudo install -d -m 0700 -o 1000 -g 1000 /srv/crowbarr/config
```

Use the paths and IDs chosen above. On a NAS, create app storage and grant access
through the dataset permissions screen. Do not recursively change media ownership
or grant access to everyone. Missing directories are rejected instead of silently
creating empty, root-owned storage.

For managed installs and unattended updates, see [TrueNAS and Portainer](nas.md).
Before replacing an existing installation, read [Migration and backup](migration.md).

## NVIDIA

For an NVIDIA GPU, install the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html),
use `ghcr.io/amanofvaly/crowbarr:latest-cuda`, and add the device reservation:

```yaml
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

Open `http://localhost:8449` after the container starts.
Use the server's IP address when opening it from another computer. Create your
dashboard account and configure Sonarr/Radarr in Settings. Map their media paths
to paths visible inside Crowbarr, such as `/media`. Service URLs must be reachable
from the container: `localhost` refers to Crowbarr itself.

Select CUDA in Settings when using the NVIDIA image. The image does not override
your saved processing settings. On TrueNAS, configure its GPU support rather than
installing driver packages on the host.

## Docker Run

```sh
docker run -d \
  --name crowbarr \
  --restart unless-stopped \
  --user 1000:1000 \
  -p 8449:8449 \
  --init \
  --stop-timeout 45 \
  --mount type=bind,src=/srv/crowbarr/config,dst=/config \
  --mount type=bind,src=/path/to/media,dst=/media \
  ghcr.io/amanofvaly/crowbarr:latest
```

Crowbarr writes subtitles beside the video files. Run the container with the uid and
gid that own those files. Keep `/config` persistent because it contains the database,
settings, login credentials, API key, reports, downloaded models, and cached
transcripts. Published subtitles remain alongside the media.

Allow roughly 115 MB of free disk space per hour of audio during extraction.
Model and transcript caches require additional space. The first inference downloads
models and requires outbound network access.

## Update

```sh
docker compose pull
docker compose up -d
```

These commands update manually. `latest` and `restart: unless-stopped` do not schedule
updates. For unattended release updates, use [Portainer GitOps](nas.md#portainer).

An administrative interruption returns the job to the queue. Valid recognition
checkpoints can resume; the current unfinished chunk may repeat. Other queued and
completed work remains in `/config`. An application version change alone does not
re-audit settled jobs.

Read [rollback requirements](migration.md#rollback) before selecting an older image.
Changing the image does not reverse database migrations, audit-policy adoption, or
subtitle publications.

## Troubleshooting

```sh
docker compose ps
docker compose logs --tail=100 crowbarr
docker compose exec crowbarr id
docker compose exec crowbarr sh -c 'test -w /config && echo "Config is writable"'
```

Test your media mount in the same way, replacing `/config` with its container path.
For permission failures, check UID/GID, parent directory traversal and NAS ACLs.
Crowbarr does not change host ownership automatically. Verify a real job: HTTP health
alone does not prove that downloads, GPU inference, or subtitle publication work.

If a migrated installation asks you to create an account, stop and check the host
directory mounted at `/config`. Do not delete or reset the existing data.

Keep the dashboard on your trusted network or use authenticated TLS access through
a reverse proxy. Remove keys, cookies and private media names from shared logs.
