# Crowbarr frontend

A new Operate frontend for the self-hosted arr ecosystem. The prior Bootstrap dashboard
has been replaced with a full-width application shell and seven routes: Dashboard,
Activity, Library, Review, History, Settings, and API & webhooks.

The user supplied crowbar.svg is the authoritative logo and favicon. Its forest green
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
