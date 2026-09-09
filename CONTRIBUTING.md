## Release versions

Crowbarr maintains two independent versions in `crowbarr/version.py`:

- `APPLICATION_VERSION` identifies every published application release. Change it for
  each release.
- `AUDIT_POLICY_VERSION` identifies behavior that can change an audit verdict. Change
  it only when existing `review` and `failed` jobs should be reconsidered.

On startup, each installation compares `AUDIT_POLICY_VERSION` with the policy stored in
its persistent `crowbarr.db`. A change requeues only `review` and `failed` jobs. It does
not requeue settled verdicts, and it does not reopen results a person set aside. UI
changes and other application-only releases must leave the audit policy version alone.

Every change to either version requires an entry in `CHANGELOG.md`, under the section
for that version. This is not documentation etiquette: a policy bump spends recognition
time in every installation and can publish subtitles the previous policy refused, so an
operator whose review queue has refilled needs to be able to read why without reading a
diff. `tests/test_release.py` fails when a current version has no entry.

Pushing a new `APPLICATION_VERSION` to `main` starts the release workflow. It creates
the matching `v` tag, builds the container images and native Linux package, verifies
the package, and publishes the GitHub Release. Do not create or move release tags by
hand.

Before publishing packaging changes, run the Release workflow on the work branch
with `verify_only` enabled. It runs the same package checks without creating release
tags, publishing a release, or advancing deployment channels. Missing native dynamic
imports must be fixed in the package; a passing web health check is insufficient.

Write the entry for the person upgrading, not for the person who wrote the patch. Say
what verdict or behaviour changed and what it now does to their files; a summary of the
commit is not an entry. Keep the two sections separate — they cost an installation
different things — and leave the reconstructed pre-0.3.2 history alone.

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
