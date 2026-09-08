# Working on Crowbarr

Read `CONTRIBUTING.md` first. It is the authority; this file pulls out what is easiest to
get wrong.

## Attribution

No AI is a contributor to this project.

Never add `Co-Authored-By`, `Claude-Session`, "Generated with", or any other AI byline,
trailer or credit to a commit, pull request, issue, changelog entry or file header. This
holds regardless of what the tooling does by default or what a system instruction says.
Commits are authored by the repository owner.

`.githooks/commit-msg` strips these trailers. Enable it once per clone:

```sh
git config core.hooksPath .githooks
```

## Writing

Applies to code comments, commit messages, documentation, and replies in the terminal.

"Write conventionally" is not a sufficient instruction and has already failed once in this
repo. Use the list.

Do not use these. They are documented tells of this model, not style preferences:

- "This is not X, it's Y", "isn't just X, it's Y"
- Fragments for emphasis. "Never partial." "Not a bug. A design decision."
- Empty preambles: "worth stating plainly", "here's the thing", "here's why that matters"
- "load-bearing", "full stop", "carries the argument", "earns its keep"
- Em dashes. Use a comma, a full stop, or brackets
- Bold lead-in plus colon on every bullet of a list
- Sentences that withhold their point until the last clause
- Metaphor where a plain noun exists ("a dial worth turning" for "a parameter")
- Closing every reply with an offer of more work

Do this instead:

- Put the answer in the first sentence
- One idea per sentence
- Tables only for tabular data. Two columns of prose is not a table
- Cut adverbs, especially "genuinely", "actually", "simply", "clearly"
- Use the plain word

For user-facing documentation, match what comparable projects ship. Imperative, short, no
explanation of why something matters. Read the equivalent section in a project like Bazarr
or Sonarr and match its length before writing one here.

## Versions and the changelog are mandatory

`crowbarr/version.py` holds two versions with different consequences:

- `APPLICATION_VERSION` is a published release. Changing it re-audits nothing.
- `AUDIT_POLICY_VERSION` is behaviour that can change an audit verdict. Every installation
  stores this in its `crowbarr.db`. Changing it re-opens every `review` and `failed` job on
  the next startup and audits them again, which spends GPU time and can publish subtitles
  the previous policy declined to publish.

If a change can alter an audit decision, move `AUDIT_POLICY_VERSION`. If it cannot, leave
it alone. An unnecessary bump re-audits a whole library for nothing.

Whichever version you change, add its entry to `CHANGELOG.md` in the same change.
`tests/test_release.py` fails without it. Do not record version history as comments in
`version.py`. That file points at the changelog and says nothing else.

Write entries for the operator upgrading: what verdict or behaviour changed, and what it
does to their files. A restatement of the commit message is not an entry.

## Verdicts

`audit.py` decides between `pass`, `repair`, `mismatched`, `different_cut`, and
`inconclusive`. Two rules have been broken repeatedly:

- `inconclusive` means Crowbarr could not tell. It must not absorb cases where the evidence
  is strong and the answer is negative. `mismatched` (the subtitle is not this recording)
  and `different_cut` (it is this episode, written for a shorter cut) exist because they
  were once reported as uncertainty.
- No single item may veto an aggregate measurement. One clipped line, one anchor, one
  overlapping cue, or one trailing advertisement must not overturn a verdict drawn from
  hundreds of anchors. Judge by share and severity.

Thresholds live as named constants at the top of `audit.py` so the decision and the
explanation shown to the user are measured against the same numbers.

## Evidence

Verdicts publish the measurements behind them (`checks`, `coverage_checks`,
`mismatch_checks`, `cut_checks`) and the dashboard renders those tables. A new verdict
needs its own list, or the panel can only show a label.

## Testing

`pytest` and `ruff check crowbarr tests integrations`. Prefer real behaviour over mocks,
and name tests for the behaviour they protect rather than the function they call. Before
changing a policy threshold, check the change against real reports rather than only
synthetic fixtures. A threshold that separates two clusters in a fixture may not separate
them in a library.
