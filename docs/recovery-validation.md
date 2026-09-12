# Recovery validation — 0.4.9 / audit policy 0.3.7

## Production evidence (12 September 2026)

Read-only inspection of the TrueNAS Crowbarr database, provider ledger, sidecar hashes,
and Bazarr logs established the following for Dragon job 10602:

- The existing sidecar opens with introductions by the producer and directors; it is commentary.
- Bazarr reported a download from OpenSubtitles, then logged that the downloaded subtitle
  was not valid for the media. The sidecar still matched the rejected SHA-256 digest
  `dcc98a5b0137c3609416ee84ca1890bfbc23f1b00b4267ea31f789387e40472b`.
- The ledger had consumed one of three allowed attempts, with 155 further candidates
  passing the application's filters in the saved search. These are not 155 validated subtitles.
- Jobs 10591 (Howl), 10619 (Children of Heaven), and 10746 (E.T.) showed the same
  combination of rejection log, unchanged rejected bytes, and a single attempt.
- The database contained 232 review jobs: 39 recorded a download, three exhaustion,
  and 190 no provider outcome. Those totals do not prove that all reviews share one cause.

## Full Dragon replay

Ran corrected recognition code in an isolated CPU-only diagnostic in the running
container. Used temporary audio and chunk checkpoints; did not call provider APIs,
publish subtitles, alter settings, or deploy application code.

- Multilingual small detected English in three distributed speech samples with
  probabilities 0.9264, 0.9276, and 0.9284.
- Full recognition produced 4,456 words. Low-confidence share was 7.09%, versus 65.40%
  in the stored production audit. These are separate runs, not a claim of identical decoding.
- The commentary sidecar matched 0.1%; the unchanged audit thresholds returned `mismatched`.
- The generation baseline contained 759 cues, with 19 reading-duration warnings and
  no other structural findings. This is evidence of generation eligibility and structural
  checks, not an independently measured transcription accuracy score.
- The replay took 395 seconds. Actual recovery must still try remaining authored
  alternatives and audit any replacement before falling back to generation.

The final language check was replayed on Dragon and Howl after replacing whitespace
word counting with tokenizer-based speech evidence. Dragon again passed with the three
probabilities above. Howl was refused as Japanese: samples measured ja at 87%, 57%, and
100%; the two confident Japanese samples established the non-English result. E.T.
passed the English control at 97%, 99%, and 95%. These diagnostics made no provider requests.

## Policy boundaries

Language is checked before recognition and provider actions, after basic source validation.
Repeated multilingual speech samples are required; silence and conflicting confident
languages do not establish English. Untagged audio is enabled by default; an explicitly
saved disabled setting is preserved. It is enabled on the inspected server.
Downloading a speech model also prepares multilingual small.

Accepted high-no-speech segments retain word probabilities; low average log probability
still suppresses unreliable words. Recognition policy 2 separates both transcript caches
and resumable chunk directories from older lossy probabilities. Successful language
checks are cached by media revision, selected stream, target language and recognition policy.

Provider recovery precedes generation for definite mismatches. Requests leaving no changed
subtitle return a retry outcome, consume the existing bounded ledger and cannot be
mistaken for audited replacements. Inconclusive evidence no longer records an authored
source as definitively rejected. Exhaustion never turns uncertain speech into permission
to generate over an authored subtitle.
Provider backups preserve exact bytes, including byte-order marks and CRLF line endings.

A repaired subtitle passing its full audit is no longer vetoed by the additional global
match requirement. Inconclusive repairs retain that safeguard. The 65% audit sufficiency
threshold and escalation of non-passing samples remain unchanged: anchors alone do not
prove the correctness of unmatched scenes. The current data does not justify lowering
those safeguards merely to reduce the review count.

Regression coverage includes discarded and unchanged provider responses, budget exhaustion,
changed-file re-audits, generation after recovery, disabled automatic recovery, language
ambiguity and foreign speech, untagged selection, confidence retention, cache versioning,
and the contradictory repair gate. Desktop and mobile browser checks cover settings,
recorded recovery details, expanded diagnostics, and visible action controls.

Validation completed: full suite 324 passed; the subsequently added Japanese-tokenization
regression also passed. The final targeted processor, recovery, packaging and API checks
passed (104 tests), followed by the seven recovery tests after that additional regression.
JavaScript syntax, Ruff, diff whitespace checks, and desktop/mobile browser checks passed.

Deployment remains a separate operation. The policy bump reopens unresolved work;
it does not blanket-reprocess settled successful outcomes or reset provider ledgers.
