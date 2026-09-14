# Platform coverage

Tracks how close each platform is to a click-and-install experience. Update the table
and the platform section together when something ships.

## What counts as done

A platform is `shipped` when all of these hold:

- Installed from the platform's own app store, catalog or installer, with a form for
  paths, port and user. No terminal, no YAML editing.
- The platform offers updates itself, or the installer keeps itself updated.
- The Bazarr, Sonarr and Radarr connections are entered in the dashboard, with nothing
  copied into another app's directory.
- The GPU works after choosing it in Settings, with no extra image or driver steps
  beyond what the platform asks for its own GPU apps.

Status values:

- `shipped`: meets every point above.
- `manual`: works today, but needs a terminal or a hand-edited file.
- `planned`: agreed and sequenced, no work started.
- `not started`: listed for completeness, no decision taken.
- `declined`: will not be done, with the reason.

## Status

| Platform | Delivery | Status | Next step |
| --- | --- | --- | --- |
| Docker Compose (Linux x86_64) | `ghcr.io/amanofvaly/crowbarr` image, standalone compose in `docs/docker.md` | `manual` | Add `PUID`/`PGID` so templates match arr convention |
| Portainer | Git stack from the `release` branch with GitOps polling | `manual` | Publish a Portainer app template JSON |
| TrueNAS SCALE 24.10+ | Custom App, pasted YAML | `manual` | Submit to the TrueNAS community catalog |
| Unraid | Community Applications templates in `amanofvaly/unraid-templates` | `manual` | Awaiting Community Applications review; `shipped` once listed in Apps |
| Synology DSM 7 | none | `not started` | Container Manager project file, then check the Synology community package route |
| QNAP | none | `not started` | Container Station application template |
| CasaOS | none | `not started` | App store manifest |
| Runtipi | none | `not started` | App store manifest |
| Umbrel | none | `not started` | App store manifest |
| Home Assistant OS | none | `not started` | Decide whether an add-on repository is worth the image size |
| Native Linux x86_64 (systemd) | `install-crowbarr.sh`, PyInstaller archive, `crowbarr-update` timer | `manual` | `.deb` and `.rpm` packages with an apt/dnf repository, then AUR |
| Windows 10/11 | none | `planned` | PyInstaller build, Windows service, installer, then winget |
| macOS 13+ | none | `planned` | PyInstaller build, launchd agent, `.pkg`, then Homebrew |
| Linux arm64 image | none | `planned` | arm64 lock files and a second build matrix entry |
| Native Linux arm64 | none | `not started` | Depends on the arm64 image work |
| NVIDIA CUDA | `latest-cuda` image, device reservation, CUDA chosen in Settings | `manual` | Detect the device reservation and default to CUDA on first start |
| AMD ROCm | none | `not started` | Check CTranslate2 support before planning |
| Intel Arc / oneAPI | none | `not started` | Check CTranslate2 support before planning |
| Apple Metal | none | `not started` | Depends on the macOS build; CTranslate2 has no Metal backend, so CPU only at first |

Two items apply to every platform and block `shipped` everywhere:

| Item | Status | Next step |
| --- | --- | --- |
| `PUID`/`PGID` environment variables | `not started` | Accept them in the entrypoint alongside the current `user:` field |
| Bazarr integration without a copied script | `not started` | Accept Bazarr's Apprise JSON notification so no file goes into Bazarr's config directory |

## Platforms

### Docker Compose

Works. The user edits image tag, `user:`, host paths and the GPU block, and creates
the config directory with matching ownership.

- Add `PUID`/`PGID` handling so catalog templates can use the arr convention.
- Publish `linux/arm64` alongside `linux/amd64` (see Linux arm64 image).

### Portainer

Works through a Git stack with stack fields. Portainer also supports app templates,
which present a form instead of stack fields.

- Write `packaging/portainer-template.json` pointing at the published image.
- Host it at a stable raw URL and document adding it under Settings, App Templates.

### TrueNAS SCALE

Works as a Custom App with pasted YAML. The catalog route gives a form with dataset
pickers, a GPU toggle and update notices.

- Write a catalog app (train, `app.yaml`, `questions.yaml`, compose template).
- Submit to the TrueNAS community train and track the review.
- Keep the Custom App YAML in `docs/nas.md` until the catalog entry is accepted.

### Unraid

Templates live in `amanofvaly/unraid-templates` (`crowbarr.xml` and `crowbarr-cuda.xml`),
documented in `docs/unraid.md`. Submitted through ca.unraid.net. Until accepted, users add
the repository URL under Template repositories, which counts as `manual`.

Unraid creates a missing appdata path as `nobody:users`, so the template runs the
container with `--user 99:100` and needs no `PUID`/`PGID` support in the image.

- Mark `shipped` when the listing appears in Apps.
- Verified locally with the template's exact `docker run` flags; not yet tested on an
  Unraid host.

### Synology DSM

Not published. Container Manager on DSM 7.2 imports compose projects; arm64 models need
the arm64 image.

- Provide a compose project file with DSM path defaults.
- Document the Container Manager import steps.
- Depends on Linux arm64 image for ARM models.

### QNAP

Not published.

- Provide a Container Station application template.
- Depends on Linux arm64 image for ARM models.

### CasaOS, Runtipi, Umbrel

Not published. Each has a community app store that takes a compose file plus a manifest.

- Write one manifest per store and submit it.
- Depends on Linux arm64 image, since many of these hosts are ARM boards.

### Home Assistant OS

Not started. An add-on is a Docker image plus `config.yaml`. The inference runtime is
large for the typical Home Assistant host, so decide first whether the audience exists.

### Native Linux x86_64

Works through the installer script. It needs a terminal, `sudo` and systemd.

- Build `.deb` and `.rpm` from the PyInstaller archive and publish an apt and dnf
  repository from the release pipeline.
- Add an AUR package.
- Keep the script as the fallback for other distributions.

### Windows

Not started. Release assets are Linux only and `install-crowbarr.sh` refuses other systems.

- Add a Windows PyInstaller build to `release.yml` with CPU inference and a bundled
  `ffmpeg`.
- Run as a Windows service (WinSW or a `pywin32` service) so it starts at boot without
  a signed-in user.
- Build an installer (Inno Setup or MSI) that asks for the data directory and port,
  registers the service, and adds a Start menu link to `http://localhost:8449`.
- Code-sign the installer; unsigned installers trigger SmartScreen.
- Publish a winget manifest.
- CUDA on Windows needs `cublas64_12.dll` shipped or found; decide after the CPU build.

### macOS

Not started. Same starting point as Windows.

- Add a macOS PyInstaller build (arm64 first, x86_64 if there is demand) with a bundled
  `ffmpeg`.
- Run as a launchd agent so it starts at login.
- Build a `.pkg` that installs the binary, writes the launchd plist and creates the data
  directory under `~/Library/Application Support/Crowbarr`.
- Sign and notarize; unsigned packages are blocked by Gatekeeper.
- Publish a Homebrew formula or cask.
- Inference is CPU only. CTranslate2 has no Metal backend.

### Linux arm64 image

Not started. Needed by Raspberry Pi, ARM NAS models, Apple Silicon Docker Desktop and
most of the app-store hosts above.

- Compile `packaging/inference/cpu.lock` for `aarch64` (torch, torchaudio and
  CTranslate2 all publish aarch64 wheels).
- Add `linux/arm64` to the build matrix and publish a multi-arch manifest.
- Run `packaging/inference_check.py` on an arm64 runner before promotion.
- No CUDA variant for arm64 unless Jetson demand appears.

### GPU backends

CUDA works when the user picks the CUDA image, adds the device reservation and selects
CUDA in Settings.

- Default to CUDA when the container can see a GPU on first start.
- ROCm and Intel: check CTranslate2 support before planning anything.
- Apple Metal: no CTranslate2 backend, so the macOS build is CPU only until that changes.

### Cross-platform items

- `PUID`/`PGID`: catalog templates on Unraid, TrueNAS and Synology default to these
  names. Accept them in the container entrypoint and drop privileges to the requested
  IDs, while keeping `user:` working for existing installs.
- Bazarr integration: replace the copied `bazarr-notify.py` with an endpoint that
  accepts Bazarr's Apprise JSON notification, so the user only pastes a URL into
  Bazarr's notification settings. Keep the script working for existing installs.
