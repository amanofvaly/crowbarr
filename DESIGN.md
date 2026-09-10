# Crowbarr frontend

A new Operate frontend for the self-hosted arr ecosystem. The prior Bootstrap dashboard
has been replaced with a full-width application shell and seven routes: Dashboard,
Activity, Library, Review, History, Settings, and API & webhooks.

The user supplied icon.svg is the authoritative logo and favicon. Its forest green
(#00520b), jade (#268353), and olive (#adb34d) are accents, not a surface tint.
The user rejected a generic card dashboard. The reference is their actual Radarr UI:
neutral light workspace, charcoal navigation, compact toolbar, flat sections,
and dense full-width tables. Bootstrap is bundled locally for standard controls.
Dark mode is optional and also uses neutral grays. The dashboard starts with a compact
status line, live worker strip, and actionable queue; avoid oversized metric tiles,
rounded-card grids, marketing copy, and decorative spacing. Mobile rows stack.

No sample jobs are shipped. Dashboard summaries come from /api/status. Full queue and
library views use paginated endpoints. Numeric progress is audio seconds processed
within recognition, including accumulated audit windows; it is not overall job percent.
Stages without measurable denominators show named activity. Respect reduced motion.
Settings have dedicated categories, persistent labels, retained drafts, and explicit
saves. API documentation and schema are authenticated; keys are concealed by default.

## Library surface

The Library extends this neutral arr workspace with the user's Sonarr-style hierarchy:
separate Shows and Movies controls, compact search and eligibility filtering, and shows
that expand into season bands and inline episode tables. Movies use file rows. Preserve
the existing shell, flat borders, restrained green actions, and dense working layout;
this surface is not a poster gallery or a separate episode-detail screen.

Posters are small recognition aids (40 × 60 px), fetched from the configured Sonarr or
Radarr manager and omitted when unavailable. No generated raster assets ship with this
extension. Titles, episode numbers, audio language, subtitle status, processing reasons,
and actions carry the information; paths are disclosed on demand. At widths up to
900 px, file rows become labeled two-column blocks with full-width processing and
actions. At widths up to 540 px, search and filtering stack. Both themes inherit the
existing neutral palette.

Processing controls expose persistent Skip, Always include, and inherited rules at
show/movie, season, and episode-file scope. The visible reason explains the effective
decision: file overrides season, season overrides title, and title overrides the audio
policy. Known non-English audio is skipped by default; unknown languages remain
eligible. Scope rules apply to future imports, and multi-episode files remain one
physical processing unit. Skipped rows disable Audit and Generate until included.

Each file row offers Download subtitle when a result is available, Download candidate
for an available private review result, or an explicit unavailable state. These labels
distinguish published or accepted subtitles from candidates. The queue note states the
actual boundary of ordering: background work follows title → season → episode within
the same priority and age, with ready files first and manual requests or new imports
able to take precedence.
