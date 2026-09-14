# Contributing to Crowbarr

## Versioning

Crowbarr maintains two independent versions in `crowbarr/version.py`:

- **`APPLICATION_VERSION`**: Bump for each published release.
- **`AUDIT_POLICY_VERSION`**: Bump **only** when audit verdict behavior changes and existing `review`/`failed` jobs must be reconsidered. Do not bump for UI or app-only changes.

## Changelog Guidelines

Every version bump requires an entry in `CHANGELOG.md` under the corresponding version section (enforced by `tests/test_release.py`).

- **Focus on the user**: State what behavior changed and how it affects files, not commit summaries or code internals.
- **Be concise**: One line per behavior change. Avoid rationale and implementation details.
- **Keep sections separate**: Keep application changes distinct from audit policy changes.

### Example

- **Good**: `Untagged audio tracks are refused unless "Process audio without a language tag" is enabled.`
- **Bad**: `Verify spoken language using distributed multilingual speech samples before recognition or provider recovery.`

## Development & Testing

- **Lint & Test**: Run `ruff check crowbarr tests integrations` and `pytest`.
- **Test coverage**: Include behavioral tests for queue transitions, publication safety, and integrations. Do not download models in unit tests.
- **Automation first**: Features should operate via background discovery, events, and durable queues—not manual per-file triggers.

## Releases & Packaging

- Pushing a new `APPLICATION_VERSION` to `main` triggers the release workflow (builds native packages/containers and creates tags/releases). Do not create release tags manually.
- For packaging changes, run the Release workflow with `verify_only` on your branch to validate native dependencies before release.

## Security & UI

- **Sensitive data**: Never commit credentials, server inventories, chat exports, media files, generated subtitles, or model caches.
- **Frontend**: Use standard, accessible Bootstrap controls. Always render user-controlled content as plain text, never raw HTML.

