# Native Linux installation

Use the native package on Linux x86_64 with systemd and glibc 2.35 or newer
(Ubuntu 22.04+, Debian 12+, or a compatible distribution). ARM and Alpine/musl
native packages are not currently published. On TrueNAS, use [Apps or Portainer](nas.md)
instead of installing software into the NAS operating system.

```sh
curl -fsSL https://github.com/amanofvaly/crowbarr/releases/latest/download/install-crowbarr.sh | sudo bash
```

The installer creates an unprivileged `crowbarr` service account, installs the
application under `/opt/crowbarr`, and keeps data under `/var/lib/crowbarr`.
Grant that account access to traverse and write to your media directories using
their existing group or ACL. Do not recursively change media ownership.

Open `http://SERVER-IP:8449`, create your dashboard account and configure services
in Settings. Use CPU processing with the native package. For supported NVIDIA
installation, use the [CUDA container](docker.md#nvidia).

## Updates

```sh
sudo crowbarr-update
```

Installation settings persist in `/etc/crowbarr/installer.env`. Upgrades retain
the service account and data paths, including supported older installer settings.
Application binaries and data must live in separate directories.

To enable unattended daily release updates:

```sh
sudo env CROWBARR_AUTO_UPDATE=true crowbarr-update
systemctl list-timers crowbarr-update.timer
```

To disable them:

```sh
sudo env CROWBARR_AUTO_UPDATE=false crowbarr-update
```

The timer follows published GitHub releases, not source pushes. It runs daily with
up to an hour of randomized delay. Keep recoverable backups and read release notes.

For existing media permissions, an installer invocation may explicitly select an
existing account/group using `CROWBARR_USER` and `CROWBARR_GROUP`. Other supported
options are `CROWBARR_DATA_DIR`, `CROWBARR_INSTALL_DIR`, and `CROWBARR_VERSION`.
Run these through `sudo env NAME=value ...` so sudo passes them to the installer.
These options are not required for a normal install.

## Status and recovery

```sh
systemctl status crowbarr
journalctl -u crowbarr -n 100 --no-pager
journalctl -u crowbarr-update -n 100 --no-pager
```

If the replacement does not start, the installer restores the prior binary and
service settings. That does not reverse database migrations or subtitle writes.
Follow [Migration and backup](migration.md#rollback) for full rollback. Old binaries
remain in `/opt/crowbarr.versions`; retain the version matching your backup.
