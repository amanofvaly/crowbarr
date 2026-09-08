# Install Crowbarr with Docker

## Docker Compose

Save this as `compose.yaml`, change the two marked lines, then run
`docker compose up -d`.

```yaml
services:
  crowbarr:
    image: ghcr.io/amanofvaly/crowbarr:latest
    container_name: crowbarr
    restart: unless-stopped
    init: true
    user: "1000:1000"            # the user and group that own your media
    ports:
      - "8449:8449"
    volumes:
      - ./config:/config
      - /path/to/media:/media    # your library
    stop_grace_period: 45s
```

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

## Docker Run

```sh
docker run -d \
  --name crowbarr \
  --restart unless-stopped \
  --user 1000:1000 \
  -p 8449:8449 \
  -v ./config:/config \
  -v /path/to/media:/media \
  ghcr.io/amanofvaly/crowbarr:latest
```

Crowbarr writes subtitles beside the video files. Run the container with the uid and
gid that own those files. Keep `/config` persistent because it contains the database,
settings, downloaded models, and cached transcripts.

Allow roughly 115 MB of free disk space per hour of audio during extraction.

## Update

```sh
docker compose pull
docker compose up -d
```

An interrupted job returns to the queue when the replacement container starts. Other
queued and completed work remains in `/config`.

To roll back, pin an earlier image tag and run `docker compose up -d` again. Rolling
back the application does not reverse an audit policy change already stored in the
database.
