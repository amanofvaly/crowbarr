# Contributing

## Release versions

Crowbarr maintains two independent versions in `crowbarr/version.py`:

- `APPLICATION_VERSION` identifies every published application release. Change it for
  each release.
- `AUDIT_POLICY_VERSION` identifies behavior that can change an audit verdict. Change
  it only when existing `review` and `failed` jobs should be reconsidered.

On startup, each installation compares `AUDIT_POLICY_VERSION` with the policy stored in
its persistent `crowbarr.db`. A change requeues only `review` and `failed` jobs. It does
not requeue settled verdicts. UI changes and other application-only releases must leave
the audit policy version alone.

Keep Crowbarr useful as an unattended media workflow. New functionality should work
through discovery/events and durable jobs, not require per-file user submissions.

Run `pytest` and `ruff check crowbarr tests integrations`. Add behavioral tests for
queue transitions, publication safety, integration changes, and alignment failure
cases. Keep model downloads out of ordinary unit tests. Record real-model evaluations
separately with hardware, model version, and dataset provenance.

Do not commit credentials, server inventories, private conversation exports, user
media, generated subtitles from copyrighted works, or model caches. The supplied
ignore files exclude local context, and Docker uses an explicit source allowlist.
Inspect the release archive as well as the Git diff before publishing.

Bootstrap is the dashboard component system. Prefer accessible standard controls
over new custom widgets. All user-controlled text must be rendered as text, not HTML.
