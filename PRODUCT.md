# Crowbarr

Crowbarr is a self-hosted companion to the arr ecosystem. Sonarr and Radarr are the
primary sources of library membership and monitoring preferences. It follows their APIs,
uses authored subtitles from Bazarr when available, rebuilds timing against dialogue,
and generates subtitles when no usable authored track arrives. Routine processing must
not require manual file submissions or user-authored glue scripts.

The audience runs Sonarr/Radarr, Bazarr, and Plex or another sidecar-capable player on
a home server. Installation and language/resource preferences are one-time tasks.
Originals must be preserved, missed events reconciled, and uncertainty made visible.

The first release supports English same-language dialogue, Docker on Linux x86-64,
CPU or NVIDIA inference, and a single worker. It must not promise perfect timing.
Other languages and translated subtitles require separately evaluated matching strategies.

UI direction: a completely new frontend, ready for people to use, fitting alongside
Sonarr, Radarr, and Bazarr. Use the supplied crowbar.svg as logo, icon, and color
reference. Provide full-screen navigation, real activity queues and measured animated
progress, library search, dedicated settings, and an API interface for external apps.
