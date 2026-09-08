# Working on Crowbarr

Read `CONTRIBUTING.md` first; it is the authority and this file only pulls out what is
easiest to get wrong.

## Versions and the changelog are mandatory

`crowbarr/version.py` holds two versions with different consequences:

- `APPLICATION_VERSION` — a published release. Changing it re-audits nothing.
- `AUDIT_POLICY_VERSION` — behaviour that can change an audit verdict. Every
  installation stores this in its `crowbarr.db`. Changing it re-opens **every `review`
  and `failed` job** on the next startup and audits them again, which spends GPU time
  and can publish subtitles the previous policy declined to publish.

If a change can alter an audit decision, move `AUDIT_POLICY_VERSION`. If it cannot,
leave it alone — an unnecessary bump re-audits a whole library for nothing.

**Either version you change, add its entry to `CHANGELOG.md` in the same change.**
`tests/test_release.py` fails without it. Do not record version history as comments in
`version.py`; that file points at the changelog and says nothing else.

Write entries for the operator upgrading: what verdict or behaviour changed, and what it
now does to their files. A restatement of the commit message is not an entry.

## Verdicts

`audit.py` decides between `pass`, `repair`, `mismatched`, `different_cut`, and
`inconclusive`. Two rules have been broken repeatedly and are worth stating:

- **`inconclusive` means Crowbarr could not tell.** It must not absorb cases where the
  evidence is strong and the answer is negative. `mismatched` (the subtitle is not this
  recording) and `different_cut` (it is this episode, written for a shorter cut) exist
  because they were once reported as uncertainty.
- **No single item may veto an aggregate measurement.** One clipped line, one anchor, one
  overlapping cue, or one trailing advertisement must not overturn a verdict drawn from
  hundreds of anchors. Judge by share and severity.

Thresholds live as named constants at the top of `audit.py` so the decision and the
explanation shown to the user are measured against the same numbers.

## Evidence

Verdicts publish the measurements behind them (`checks`, `coverage_checks`,
`mismatch_checks`, `cut_checks`) and the dashboard renders those tables. A new verdict
needs its own list, or the panel can only show a label.

## Testing

`pytest` and `ruff check crowbarr tests integrations`. Prefer real behaviour over
mocks, and name tests for the behaviour they protect rather than the function they call.
Before changing a policy threshold, check the change against real reports rather than
only synthetic fixtures — a threshold that separates two clusters in a fixture may not
separate them in a library.
