# TrueNAS and Portainer

Install the published image and configure connections in the dashboard, as with
Sonarr, Radarr or Bazarr. The host needs storage paths, a port, numeric user/group
IDs and optional GPU access. No source edits, local builds or helper scripts are
required. Use the same public release on every server.

## TrueNAS Apps

TrueNAS SCALE 24.10 and newer run apps on Docker. Use **Apps > Discover Apps >
Custom App > Install via YAML** and paste the following, changing the image, the
host paths and the port to suit your system.

```yaml
services:
  crowbarr:
    image: ghcr.io/amanofvaly/crowbarr:latest-cuda
    restart: unless-stopped
    init: true
    user: "568:568"
    ports:
      - "8449:8449"
    volumes:
      - type: bind
        source: /mnt/pool/apps/crowbarr/config
        target: /config
        bind:
          create_host_path: false
      - type: bind
        source: /mnt/pool/media/TV-Shows
        target: /tv
        bind:
          create_host_path: false
      - type: bind
        source: /mnt/pool/media/Movies
        target: /movies
        bind:
          create_host_path: false
    stop_grace_period: 45s
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

`568:568` is the `apps` account TrueNAS runs containers as. Use it unless you have
deliberately chosen another, and give that account write access to the datasets
through the dataset permissions screen. Crowbarr writes subtitles next to your
videos, so read-only media will not work.

Create the config dataset before installing. `create_host_path: false` makes a
missing path fail the install rather than silently create empty, root-owned storage.

Mount each library separately and name the container paths to match what Sonarr and
Radarr report, commonly `/tv` and `/movies`. Matching them means no path mappings to
configure later. Add or remove volume entries for as many libraries as you have.

Use `latest` instead of `latest-cuda` and delete the whole `deploy:` block if you
have no NVIDIA card. See [Choose an image](docker.md#choose-an-image) for the
difference. With the CUDA image, enable GPU support for the app in TrueNAS and
select CUDA in Crowbarr's Settings after the first start.

Crowbarr then appears in the TrueNAS Apps list like any other app. Do not run the
native Linux installer or install driver packages on the NAS host. See
[TrueNAS custom app instructions](https://apps.truenas.com/managing-apps/installing-custom-apps/).

### Updating a TrueNAS app

TrueNAS offers **Update** when the image behind your tag changes in the registry. It
can take up to a day to notice. Restarting the Apps service checks immediately.

Updating replaces the image only. Your YAML, storage, port and settings are untouched.

Keep a moving tag. Pin a version instead of `latest` and no update is ever reported.

For updates that install themselves with nobody clicking, use Portainer below.

## Portainer

Use a Docker Standalone environment. Portainer itself can be a TrueNAS App.
Crowbarr then appears as a Portainer stack, not in the TrueNAS Apps list. Manage it
from one place or the other, not both.

1. Open **Stacks > Add stack**, name it `crowbarr`, and select **Git repository**.
2. Enter `https://github.com/amanofvaly/crowbarr`, reference `refs/heads/release`,
   and Compose path `compose.yaml`. The public repository needs no GitHub credentials.
3. For NVIDIA, add `compose.cuda.yaml` under additional paths.
4. In the stack's environment fields, enter `CROWBARR_CONFIG_DIR` and
   `CROWBARR_MEDIA_DIR` as existing absolute host directories, plus `CROWBARR_UID`
   and `CROWBARR_GID` as the user/group that can write to them. Set `CROWBARR_PORT`
   only if you want a port other than 8449. No `.env` file is required.
5. Enable **GitOps updates**, choose **Polling** and an interval such as five minutes.
   Enable **Re-pull image** and leave **Force redeployment** disabled.
   Leave `CROWBARR_IMAGE_TAG` unset so the release branch selects the version.
6. Deploy. Check container health, open `http://SERVER-IP:8449`, and configure
   services in the dashboard. Select CUDA in Settings if using the NVIDIA image.

For a second library, add `compose.extra-media.yaml` and set
`CROWBARR_EXTRA_MEDIA_DIR`. Container mounts default to `/media` and `/media-extra`.
Set `CROWBARR_MEDIA_MOUNT` and `CROWBARR_EXTRA_MEDIA_MOUNT` to keep existing paths
such as `/tv` and `/movies`.

Point config storage at a path outside Portainer's Git checkout, never a relative
one. Adding a stack from Git does not enable polling on its own. See the
[Portainer GitOps documentation](https://docs.portainer.io/user/docker/stacks/add#gitops-updates).

## What triggers an update?

Only a version change publishes a release. Pushing code to `main` does not. When
`APPLICATION_VERSION` changes, the pipeline runs the tests and the packaged build
checks, and advances the `release` branch only if they all pass.

Portainer picks up that branch change at its next poll and redeploys with the same
storage. To pause updates, turn off GitOps polling. To stay on one version, set
`CROWBARR_IMAGE_TAG` to its number, without `v` or `-cuda`, and remove it to follow
releases again.

Keep backups. An automatic redeploy is not an automatic way back.
