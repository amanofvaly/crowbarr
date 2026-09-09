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

### 0.3.5

- **Timing is now rebuilt from the transcript instead of corrected.** Crowbarr used to
  measure how far an authored subtitle had drifted, fit a shift and a stretch to that
  measurement, and move every cue by the result. That only works when the whole file is
  wrong in one describable way. A subtitle that is right for ten minutes and two seconds
  late afterwards, or wrong by a different amount in each act, has no single shift that
  fixes it, so the repair was withheld and the file was parked for review.

  Recognition already produces a timestamp for every word spoken in the file. Each cue's
  own words are now located in that transcript, and the cue is placed where they were
  said. Nothing measures the old timing, because none of it survives. A constant offset,
  accumulating drift, a missing scene, and a file wrong in a different way every minute
  are the same job now, and none of them has to be recognised as a shape first.
  - Cues with nothing to match -- a sound caption, on-screen text, a line the recognizer
    missed -- keep their position relative to the matched cues either side of them.
  - Cue length is left alone. How long a line stays on screen is reading time, an
    authoring decision, not synchronisation.
  - A file is retimed when at least 3 of its cues, and a quarter of them, were matched
    to their own speech. Below that there is too little to place from and it is parked.
- **`inconclusive` no longer parks a file that can be retimed.** That verdict means the
  audit could not gather enough confident, evenly spread anchors to grade the timing. It
  says nothing about how many lines can be found in the transcript, which is a looser
  question and the only one placement depends on. Of 24 such files in one library, 20
  had a quarter or more of their cues matched to their own speech and were parked
  anyway; one had three quarters. Whether a file is retimed is now decided by how much
  of it was found in the audio, not by which verdict the audit reached.
- **A subtitle is no longer rewritten from a sample of the audio.** On files over ten
  minutes Crowbarr recognises three two-minute windows rather than the whole runtime,
  about a quarter of an episode. A sample that found the timing already correct still
  ends the job, because the answer there is to write nothing. A sample that found it
  wrong used to go straight to rewriting every cue, including those outside the windows
  that were never matched to any audio, and then check the result against the same
  windows it was fitted to. Any verdict other than `pass` now recognises the full audio
  before the file is touched. Where roughly four files in five pass, this recognises the
  whole of the remaining fifth; the transcript is cached per file, so it is paid once.
  - Files already rewritten from a sample are worth re-checking. In one library, 11 of
    120 settled jobs, and 67 of the 102 authored repairs sitting in review.
- **One overlapping line no longer holds back a finished subtitle.** The audit judges
  structural damage by share; the publication path treated any single item as blocking,
  so a result every anchor agreed with was parked over one overlapping cue in a file of
  five hundred. Both now apply the same rule: damage blocks when it is spread across
  more than 5% of the file, and a cue whose timing is impossible still blocks on its
  own. Anything below that share is reported alongside the published result.
- Withheld results are no longer judged against the subtitle they replace. That
  comparison assumed the old timestamps were being adjusted. A retimed subtitle is now
  judged on whether it measures as wrong and on how much of it was placed from speech
  rather than filled in between.

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

### 0.4.0

- The NVIDIA image is 2.45 GB, down from 6.98 GB. Recognition still runs on the GPU.
  TrueNAS kills an app update after twenty minutes, which the old image could not
  beat, so updates failed with "Timed out waiting for response".
- Refinement runs on the CPU in both images, and no longer tries CUDA first.
- Runtime detection no longer requires cuDNN, which reported CUDA as unavailable on
  systems where it worked.

### 0.3.9

- Dashboard system information is now a compact, wrapping strip with GPU, memory,
  speech model, service status and settings links. Removed the large System readiness
  section and unnecessary dashboard borders while keeping the existing arr-style UI.
- Page actions use the desktop header alongside library search; on mobile they stay
  beside the page title. Removed the duplicate dashboard search action and the box
  around the sidebar theme toggle.
- Text selection has stronger contrast in both themes, and keyboard focus is clearer
  on the dark header and sidebar. Fixed the Activity page's Sync libraries button
  losing its green background in light mode.
- Dashboard outcome counters now say “subtitles written” and “subtitles unchanged”
  instead of implying that only unchanged subtitles were checked.
- Quiet hours use whole-hour, 24-hour time dropdowns with guidance on the server's
  timezone, overnight ranges and disabling the schedule. The mobile Save bar no longer
  covers settings fields. Fixed CPU-load validation rejecting the default `0.75` and
  preventing Resource settings from saving.
- Speech processing marks cached speech models as downloaded and explains the first-use
  download delay. WhisperX refinement shows alignment-model download status and size,
  provides download and retry actions, and keeps its toggle disabled until the model
  is present. Downloads started from the page are checked for completion and reported
  in the UI.

### 0.3.8

- WhisperX alignment failures record their cause in the service log. On an NVIDIA
  install where CUDA alignment fails and refinement retries on the CPU, the report
  said only that loading failed, which left no way to find out why. Reports and the
  dashboard still carry no exception detail.

### 0.3.7

- Settings identifies unavailable CUDA and WhisperX runtimes before new selections
  are saved. Existing device preferences survive an upgrade or a temporary GPU
  outage; switching to CPU selects a supported precision.
- WhisperX refinement retries on CPU after CUDA model loading or alignment fails
  when CPU fallback is enabled. Reports identify the backend used and the fallback;
  failed retries do not publish subtitles.
- Native packages include SciPy's dynamically imported compatibility modules, which
  are required by the alignment runtime. Clean-runtime verification caught the
  missing module before 0.3.6 could publish; that version was not released.
- Selecting an older native binary retains the current updater instead of installing
  an older script that can forget the service account and data directory on its next
  run. Existing data and the audit policy are unchanged.
- Native installations store the speech, alignment and tokenizer caches under
  `models` in the data directory, matching the published images. WhisperX downloads
  its sentence tokenizer the first time refinement runs, which previously had no
  writable location on a native install.

### 0.3.6

- Published containers include the optional WhisperX refinement dependencies on CPU
  and NVIDIA. Refinement remains off by default; the audit policy is unchanged.
- Portainer can follow the public `release` branch for unattended, versioned
  upgrades. Ordinary source pushes do not redeploy installations. Release channels
  advance only after the test suite and packaged runtime checks pass.
- Installs use persistent host storage with configurable UID/GID, ports and media
  mounts. Missing directories fail before deployment. Migration instructions retain
  the existing database, credentials, reports, models and cached transcription work.
- Restarting with an open dashboard now drains within the service stop deadline.
  Administrative interruptions refund their attempt and resume queued work; repeated
  crashes stop at the configured retry limit. Invalid cached chunks are recomputed.
- A replaced job request cannot be overwritten by an older worker. Legacy duplicate
  job migration retains subtitle ownership and historical outcomes.
- Native Linux upgrades retain installation paths and service identity, default new
  installs to an unprivileged service account, and retain the prior binary for failed
  start recovery. Daily updates are opt-in. Database rollback still requires a backup.
- Native packages verify multiprocessing and processing imports in a clean runtime,
  rather than testing only the dashboard health endpoint.

### 0.3.5

- Linux x86_64 releases now include a runnable Crowbarr package and installer. The
  installer registers a system service, keeps application data in `/var/lib/crowbarr`,
  and installs `crowbarr-update` for later upgrades. Docker remains available as a
  separate installation method.

### 0.3.4

- Dashboard is made cleaner by removing redundant edit candidates.
- Added skip review action.


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
