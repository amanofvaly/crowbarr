# Unraid

Install from the Apps tab. Search for **Crowbarr**. Choose **Crowbarr** for CPU processing
or **Crowbarr-CUDA** for an NVIDIA GPU.

Until the Community Applications listing is live, add the template repository by hand:
open **Docker**, **Add Container**, **Template repositories**, and enter
`https://github.com/amanofvaly/unraid-templates`. Save, then pick the template from the
**Template** list.

## Fill in the form

- **WebUI**: host port, 8449 by default.
- **Path: /tv** and **Path: /movies**: the same shares Sonarr and Radarr use. Matching the
  container paths means no path mappings to configure later. Leave one empty if you do
  not have that library.
- **Path: /data** (advanced): use instead when Sonarr and Radarr see a single `/data` share.
- **Appdata** (advanced): `/mnt/user/appdata/crowbarr`. Unraid creates it.

The container runs as `nobody:users` (99:100), the Unraid default for shares. Crowbarr
writes subtitles next to your videos, so read-only shares will not work. The user is set
in **Extra Parameters** under the advanced view; change it there if your shares use a
different owner.

Press **Apply**. Open the WebUI, create your account, then add Sonarr, Radarr and Plex
under Settings. Speech models download into appdata on first use.

## NVIDIA

Install the **Nvidia Driver** plugin from Apps first. In the Crowbarr-CUDA template, set
**NVIDIA_VISIBLE_DEVICES** to the GPU UUID shown on the plugin page, or leave `all`.
After the first start, select CUDA under **Settings**. Until you do, it runs on the CPU.

## Update

Unraid checks the image tag on its schedule and shows **update ready** on the Docker
page. Press it. Appdata, paths and settings are untouched.

## Troubleshooting

Open the container log from the Docker page. For permission errors, check that
`nobody:users` can write to the share, and that the appdata folder is not owned by root.
