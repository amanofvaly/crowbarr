# Install Crowbarr with Docker

Linux x86_64 (`linux/amd64`) only. No ARM64 images, no Windows or macOS packages.

## Choose an image

- `ghcr.io/amanofvaly/crowbarr:latest` for CPU.
- `ghcr.io/amanofvaly/crowbarr:latest-cuda` for an NVIDIA card the container can
  reach. It adds cuBLAS, about 600 MB more.

The GPU is used for speech recognition, which is the slow part. Subtitle timing
refinement runs on the CPU in both images.

Speech models are not in either image. They download on first use into `/config`.

With the CUDA image, install the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html),
add the device reservation shown below, and select CUDA in Settings after the first
start. Crowbarr does not switch to the GPU on its own.

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

On a NAS, create the storage and set access through the dataset permissions screen.
Do not change media ownership recursively or grant access to everyone.

See [TrueNAS and Portainer](nas.md) for managed installs. Replacing an existing
install? Read [Migration and backup](migration.md) first.

## NVIDIA

Change the image to `ghcr.io/amanofvaly/crowbarr:latest-cuda` and add the device
reservation to the same service:

```yaml
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

Set the processing device to CUDA in Settings. Until you do, it runs on the CPU.
On TrueNAS, use its own GPU support instead of installing drivers on the host.

## First run

Open `http://localhost:8449`, or the server's IP from another machine. Create your
account, then add Sonarr and Radarr in Settings. Their URLs must be reachable from
inside the container, so `localhost` will not work. Map their media paths to the
container paths.

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

Run the container as the uid and gid that own your video files, since subtitles are
written beside them. Keep `/config` persistent: it holds the database, settings,
credentials, API key, reports, models and cached transcripts.

Allow about 115 MB free per hour of audio during extraction, plus space for models
and transcripts. The first job downloads a model and needs outbound network access.

## Update

```sh
docker compose pull
docker compose up -d
```

Nothing updates on its own. `latest` and `restart: unless-stopped` do not schedule
anything. For unattended updates, use [Portainer GitOps](nas.md#portainer).

A running job returns to the queue and resumes from its last checkpoint. Queued and
finished work is unaffected. A new version alone does not re-audit settled jobs.

Read [rollback requirements](migration.md#rollback) before pinning an older image.
Going back does not undo database migrations or published subtitles.

## Repository Compose file

The `compose.yaml` in the repository is for Portainer, which supplies host paths
through stack fields. It will not start without them. Use the standalone example
above instead, or copy `.env.example` to `.env` and set the paths there. Leave
`CROWBARR_IMAGE_TAG` commented out to follow the release branch.

## Troubleshooting

```sh
docker compose ps
docker compose logs --tail=100 crowbarr
docker compose exec crowbarr id
docker compose exec crowbarr sh -c 'test -w /config && echo "Config is writable"'
```

Test the media mount the same way. For permission failures check the UID/GID,
directory traversal, and NAS ACLs. Run a real job to confirm it works; a healthy
endpoint does not prove downloads, GPU inference or publishing do.

If a migrated install asks you to create an account, stop and check what is mounted
at `/config`. Do not reset the data.

Keep the dashboard on a trusted network, or behind a reverse proxy with TLS.
