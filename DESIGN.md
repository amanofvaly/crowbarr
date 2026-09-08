# Crowbarr frontend

A new Operate frontend for the self-hosted arr ecosystem. The prior Bootstrap dashboard
has been replaced with a full-width application shell and seven routes: Dashboard,
Activity, Library, Review, History, Settings, and API & webhooks.

The user supplied crowbar.svg is the authoritative logo and favicon. Its forest green
(#00520b), jade (#268353), and olive (#adb34d) define the brand. Dark forest is the
initial theme for a media-server workspace; a light sage theme is saved per browser.
System sans typography, compact controls, 224px persistent navigation, dense readable
queues, restrained borders, and green active states keep it familiar beside Sonarr,
Radarr, and Bazarr. Mobile navigation opens on demand and table rows become stacked.

No sample jobs are shipped. Dashboard summaries come from /api/status. Full queue and
library views use paginated endpoints. Numeric progress is audio seconds processed
within recognition, including accumulated audit windows; it is not overall job percent.
Stages without measurable denominators show named activity. Respect reduced motion.
Settings have dedicated categories, persistent labels, retained drafts, and explicit
saves. API documentation and schema are authenticated; keys are concealed by default.
