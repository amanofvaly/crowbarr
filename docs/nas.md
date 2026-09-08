# TrueNAS and Portainer

Install the published image and configure connections in the dashboard, as with
Sonarr, Radarr or Bazarr. The host needs storage paths, a port, numeric user/group
IDs and optional GPU access. No source edits, local builds or helper scripts are
required. Use the same public release on every server.

## TrueNAS Apps

TrueNAS SCALE 24.10 and newer use Docker-based Apps. Use **Apps > Discover Apps >
Custom App** to install `ghcr.io/amanofvaly/crowbarr:latest` (or `latest-cuda` for
NVIDIA). Set container port 8449, the app UID/GID, a persistent host directory mounted
at `/config`, and your library mounted at `/media`. Grant that user write access to
the app dataset and media. Keep configuration storage private.

Alternatively, use **Install via YAML** with the [Docker Compose example](docker.md).
Crowbarr appears in the TrueNAS Apps list. A TrueNAS Custom App is its standard way
to install an app outside the catalog; it does not require a customized application.
Do not run the native Linux installer or install driver packages on the NAS host.
See [TrueNAS custom app instructions](https://apps.truenas.com/managing-apps/installing-custom-apps/).

TrueNAS offers image update checks, but notification is different from automatic
installation. For unattended Git-based release updates, use Portainer below.

## Portainer

Use a Docker Standalone environment. Portainer itself can be a TrueNAS App.
Crowbarr will appear as a Portainer stack, not as a separate TrueNAS-managed App.
Use one manager for Crowbarr's lifecycle.

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

For two separate libraries, add `compose.extra-media.yaml` and enter
`CROWBARR_EXTRA_MEDIA_DIR`. The default container mounts are `/media` and
`/media-extra`. During migration, preserve existing paths with `CROWBARR_MEDIA_MOUNT`
and `CROWBARR_EXTRA_MEDIA_MOUNT`, for example `/tv` and `/movies`. Read
[Migration and backup](migration.md) first.

App data lives outside Portainer's Git checkout. Do not use relative-path volumes
for persistent configuration. If your Portainer edition does not expose GitOps
polling, use manual pull/redeploy or an edition with that feature. Creating a stack
from Git does not by itself enable polling. See
[Portainer GitOps documentation](https://docs.portainer.io/user/docker/stacks/add#gitops-updates).

## What triggers an update?

An ordinary push does not publish a release. Changing `APPLICATION_VERSION` on
`main` starts the release pipeline. After tests and packaged-build checks succeed,
the pipeline publishes versioned artifacts and advances the `release` branch to
the new pinned image version. Failed releases must not advance that branch.

Portainer sees the branch change at its next polling interval and replaces Crowbarr
using the same storage mounts. Source commits on `main` do not trigger this stack.
An audit-policy-only change does not publish a new application version.

To pause updates, disable GitOps polling. To pin one version, set
`CROWBARR_IMAGE_TAG` to its number without `v` or `-cuda`; remove the variable to
follow releases again. Read release notes and maintain recoverable backups.
Automatic replacement is not automatic database rollback.
