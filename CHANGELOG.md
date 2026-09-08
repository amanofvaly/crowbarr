# Changelog

Crowbarr keeps two versions because they cost an installation different things. Both
are declared in `crowbarr/version.py` and both are shown in the dashboard footer.

## Audit policy

`AUDIT_POLICY_VERSION`. Every installation stores this value in its `crowbarr.db`. When
it changes, the next startup re-opens **every `review` and `failed` job** and audits
them again under the new rules. Settled verdicts are never re-opened, and neither are
results a person deliberately set aside.

Read this section before upgrading: a bump means recognition work, and it can publish
subtitles that the previous policy declined to.

### 0.3.4

- **A subtitle is no longer discarded over one bad line.** Two parser refusals rejected
  whole files that were otherwise correct, so they were never audited at all:
  - Timing lines carrying SubRip's screen-coordinate suffix (`X1:182 X2:534 Y1:466
    Y2:535`), written when a subtitle came from OCR of a DVD or VobSub stream, are now
    read. Players have always ignored these.
  - A cue that ends before it starts — a duplicated line left with zero length, or an
    uploader's credit given nonsense timestamps — is skipped rather than fatal. It
    displays nothing in a player either way. Timing broken across more than 5% of the
    file is still refused, because then there is no timing to audit.
- A subtitle that genuinely cannot be read now names the file and what the parser
  objected to. The refusal ends the job before a report exists, so "no discovered
  subtitle source could be read" was the whole of what an operator got.

### 0.3.3

- **New verdict `different_cut`.** A subtitle can match this episode's dialogue and
  still be untimeable, when it was written for a shorter cut of the recording: the
  error grows in steps at every scene the subtitle has no lines for, and a shift or a
  stretch cannot put those scenes back. Crowbarr now names that case instead of
  proposing a repair that fails its own audit, and answers it the way it answers
  `mismatched` — by generating a subtitle covering the whole runtime. The rejected file
  is left on disk untouched.
- **Structural issues no longer veto a timing verdict.** A single overlapping line, or
  an uploader's trailing advertisement that overruns the video by half a second, used
  to make `pass` unreachable however good the measured timing was — sending correctly
  synchronized subtitles to review over something no review action could fix, and that
  `pass` (which publishes nothing) could not be harmed by. Structure is now reported
  alongside the verdict. Two cases still block, because they undermine the measurement
  rather than the tidiness: a cue whose timing is impossible, and damage spread across
  more than 5% of the file.

### 0.3.2

- **New verdict `mismatched`.** When recognition is confident and almost none of the
  subtitle's text appears anywhere in the dialogue, the subtitle is not for this
  recording. That was previously reported as an inconclusive audit — Crowbarr's own
  uncertainty — when it is in fact a definite finding. It is now separated from
  genuinely unreadable audio by recognition quality, and answered by generating a fresh
  subtitle from the transcript that proved the mismatch. Controlled by
  `generate_over_mismatch` (on by default).
- Sampled audits never declare `mismatched` or `different_cut`; three windows can land
  on music or a badly chosen scene, so the sample escalates to the full file and lets
  that decide.
- A verdict that is not a repair no longer writes a review candidate. The candidate was
  the original subtitle re-rendered, so approving it published exactly what the audit
  had declined to endorse.

### 0.3.1

First version recorded under this identifier. Earlier releases derived the audit policy
from `APPLICATION_VERSION`; `0.3.1` is the value they carried, so installing the first
release with split versioning requeues nothing.

- Audio track selection became a ranking rather than a gate. A container language tag
  is frequently wrong, and a wrong tag is not a reason to refuse work: recognition
  establishes the spoken language from the audio itself, so the tag only breaks ties.
  Commentary and described-video tracks are still excluded.
- Forced subtitle tracks are skipped by title as well as by disposition flag.
- Verdicts publish the measurements behind them, so the dashboard can show what the
  evidence had to clear rather than a bare sentence.
- Repair improvement is judged in aggregate rather than by the single worst line.

## Application

`APPLICATION_VERSION`. Identifies a published release. Changing it alone never
re-audits anything.

### 0.3.4

- No application-visible change beyond the audit policy above.

### 0.3.3

- Review results can be **set aside**: `POST /api/jobs/{id}/skip` moves a `review` or
  `failed` job to the new `skipped` state, which policy upgrades do not re-open. Retry
  brings it back. Previously the only exits from review were re-running the same policy,
  which reaches the same verdict, and publishing.
- **Generate a fresh subtitle** is available from the review panel. The directive
  already existed but was reachable only from the Library page.
- The `generate_over_mismatch` setting is exposed in Settings.

### 0.3.2

- Dashboard refocused; release metadata distinguishes the two versions in the footer.

### 0.3.1

- Audio selection, processing stages, and audit reporting surfaced in the UI.
- Partial DOM updates, worker cooldown tracking, and richer status reporting.
- Dark mode, sidebar navigation, and standardized design tokens.

### 0.3.0

Initial published line: core service and background processing, durable job queue,
`*arr` discovery, Bazarr coordination, sampled audit with escalation to the full file,
chunked transcription with checkpointing, and forced-alignment fallback.

Entries before `0.3.2` are reconstructed from commit history rather than written at
release time.
