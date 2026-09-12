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

### 0.3.7

- Verify spoken language using distributed multilingual speech samples before recognition
  or provider recovery. Untagged audio is enabled by default and verified by speech;
  an explicitly saved disabled setting is preserved.
- Preserve word confidence when accepted speech has a high segment no-speech score;
  invalidate old recognition caches and resumable chunks containing zeroed probabilities.
- Try bounded authored alternatives before generation for proven mismatches, and retry
  provider requests that leave no changed subtitle. Uncertain audits do not reject sources.
- Timing sufficiency, full-audio escalation, and publication checks remain conservative.
  A repaired subtitle that passes its full audit is no longer vetoed by the separate
  configurable text-match gate; inconclusive repairs retain that gate.
  This policy reopens unresolved work and may generate after authored recovery is exhausted.

### 0.3.6

- Repairs no longer publish cues that overlap each other.
- Jobs held for overlapping dialogue are re-opened; most now publish.
- Fixed: a structural fault was reported as borderline timing.

### 0.3.5

- Timing is rebuilt from the transcript instead of corrected by a fitted shift, so
  constant offsets, accumulating drift and missing scenes are one case.
- Cue length is preserved. Unmatched cues keep their position between matched ones.
- A file is retimed when at least 3 cues, and a quarter of them, match their own speech.
- `inconclusive` no longer parks a file that can be retimed.
- Any verdict other than `pass` recognises the full audio before the file is changed.
  Re-check files previously rewritten from a sample.
- Structural damage blocks publication only when spread across more than 5% of a file.
- Withheld results are judged on their own measurement, not against the old subtitle.

### 0.3.4

- Subtitles carrying SubRip screen-coordinate suffixes are read rather than rejected.
- A cue that ends before it starts is skipped. Timing broken across more than 5% of a
  file is still refused.
- An unreadable subtitle names the file and what the parser objected to.

### 0.3.3

- New verdict `different_cut`: the subtitle is this episode but written for a shorter
  cut. Answered by generating a fresh subtitle; the original is left on disk.
- Structure is reported alongside the verdict instead of vetoing it. Impossible cue
  timing, and damage across more than 5% of a file, still block.

### 0.3.2

- New verdict `mismatched`: recognition is confident and the subtitle's text is not in
  the dialogue. Answered by generating a fresh subtitle. Controlled by
  `generate_over_mismatch`, on by default.
- Sampled audits escalate to the full file before declaring `mismatched` or
  `different_cut`.
- A verdict that is not a repair no longer writes a review candidate.

### 0.3.1

First version recorded under this identifier. Earlier releases derived the audit policy
from `APPLICATION_VERSION`; `0.3.1` is the value they carried, so installing the first
release with split versioning requeues nothing.

- Audio track selection ranks tracks rather than gating on the language tag. Commentary
  and described-video tracks are still excluded.
- Forced subtitle tracks are skipped by title as well as by disposition flag.
- Verdicts publish the measurements behind them.
- Repair improvement is judged in aggregate rather than by the worst line.

## Application

`APPLICATION_VERSION`. Identifies a published release. Changing it alone never
re-audits anything.

### 0.4.10

- Sign-in and page load no longer wait for hardware detection. The Settings page shows
  "Checking hardware…" until the report arrives.
- Saving a setting other than the processing device no longer runs hardware detection.

### 0.4.9

- Integrated audio-language, recognition-confidence, and authored-provider recovery fixes
  (audit policy 0.3.7). Provider outcomes distinguish requests from changed subtitle bytes.

### 0.4.8

- Videos shorter than set length are skipped. Useful for trailers.

### 0.4.7

- Media library: UI improvements

### 0.4.6

- Library browsing now separates Shows and Movies. Shows expand into season and episode
  tables, with compact artwork supplied by Sonarr or Radarr.
- Processing preferences can skip or include a movie, show, season, or episode. Known
  non-English audio is skipped by default, unknown language remains eligible, and more
  specific choices override inherited rules. Title and episode rules continue to apply
  after future imports and file upgrades.
- Fixed: a recovered library scan could leave its old failure notice on the dashboard.
  Scan failures now retain their underlying exception and traceback in server logs.

### 0.4.5

- Fixed: Release packaging retries transient Hugging Face failures while downloading the
  small speech model used to verify the installed inference dependencies.
- The dashboard shows background processing used in the rolling hour and estimates when
  work can resume after reaching the limit. Budget-only saves no longer claim that the
  library will be rechecked.

### 0.4.4

- Fixed: Model changes now keep their chosen queue behavior even when an older library scan
  is still running. Keeping finished results is stored as a versioned database request.
- Fixed: Finished results are retained by complete input signature. Returning to the same
  model, device, precision and audit settings restores unchanged files together without
  running inference again. Externally changed or missing subtitle outputs are rechecked.
  Crowbarr retains up to five audit records per file without storing subtitle bodies in
  the database.
- Settings show how many files a recheck affects and distinguish library synchronization
  from an idle worker while queue totals are being reconciled.
- Review candidates are retained per input signature, so returning to a previous model can
  restore its review result. Existing review candidates remain available after upgrade.
- Symbolic links and non-video extras reported by Sonarr or Radarr are ignored without
  creating a persistent connection warning.

### 0.4.3

- Fixed: choosing a speech model saved the previous one, and never offered to re-check
  the library or keep existing results.
- Each model has one button: Download, Select or Active. A model larger than the
  graphics card is refused.

### 0.4.2

- Media folder setup reads paths from Sonarr and Radarr, applies saved mappings, and
  reports folders Crowbarr cannot reach.
- Test folders checks unsaved paths for read and write access.
- Setup instructions cover Docker, TrueNAS, native installs and path mappings.

### 0.4.1

- Audit policy 0.3.6. Review and failed jobs are re-audited on the first startup.

### 0.4.0

- The NVIDIA image is 2.45 GB, down from 6.98 GB. Recognition still runs on the GPU.
- Fixed: TrueNAS app updates timed out on the NVIDIA image.
- Refinement runs on the CPU in both images.
- Fixed: CUDA was reported as unavailable on systems where it worked.

### 0.3.9

- Dashboard system information is a compact strip; the System readiness section is gone.
- Page actions moved to the desktop header, beside the page title on mobile.
- Stronger text selection contrast and clearer keyboard focus in both themes.
- Outcome counters read "subtitles written" and "subtitles unchanged".
- Quiet hours use 24-hour dropdowns. The mobile save bar no longer covers fields.
- Fixed: the CPU load default of `0.75` was rejected, blocking Resource settings from
  saving.
- Speech processing marks downloaded models, and refinement shows the alignment model's
  size, download status and retry action.

### 0.3.8

- WhisperX alignment failures record their cause in the service log. Reports and the
  dashboard still carry no exception detail.

### 0.3.7

- Settings reports unavailable CUDA and WhisperX runtimes before a selection is saved.
  Existing device preferences survive an upgrade or a temporary GPU outage.
- Refinement retries on the CPU when CUDA fails and fallback is enabled. Reports name
  the backend used. Failed retries publish nothing.
- Native packages include SciPy's dynamically imported modules, required by alignment.
- Selecting an older native binary keeps the current updater.
- Native installations store model caches under `models` in the data directory,
  matching the images.

### 0.3.6

- Published containers include the WhisperX refinement dependencies. Refinement remains
  off by default.
- Portainer can follow the public `release` branch for unattended upgrades. Ordinary
  source pushes do not redeploy. Channels advance only after tests and runtime checks
  pass.
- Installs use persistent host storage with configurable UID/GID, ports and mounts.
  Missing directories fail before deployment.
- Restarting drains within the service stop deadline. Administrative interruptions
  refund their attempt; repeated crashes stop at the retry limit.
- A replaced job cannot be overwritten by an older worker.
- Native upgrades keep installation paths and service identity, default to an
  unprivileged account, and keep the prior binary for failed starts. Daily updates are
  opt-in.
- Native packages verify processing in a clean runtime, not just the health endpoint.

### 0.3.5

- Linux x86_64 releases include a runnable package and installer. The installer
  registers a system service, keeps data in `/var/lib/crowbarr`, and installs
  `crowbarr-update`.

### 0.3.4

- Removed redundant edit candidates from the dashboard.
- Added the skip review action.

### 0.3.3

- Review results can be set aside: `POST /api/jobs/{id}/skip` moves a `review` or
  `failed` job to `skipped`, which policy upgrades do not re-open. Retry brings it back.
- Generate a fresh subtitle is available from the review panel.
- `generate_over_mismatch` is exposed in Settings.

### 0.3.2

- Dashboard refocused; the footer distinguishes the two versions.

### 0.3.1

- Audio selection, processing stages and audit reporting surfaced in the UI.
- Partial DOM updates, worker cooldown tracking, richer status reporting.
- Dark mode, sidebar navigation, standardised design tokens.

### 0.3.0

Initial published line: core service and background processing, durable job queue,
`*arr` discovery, Bazarr coordination, sampled audit with escalation to the full file,
chunked transcription with checkpointing, and forced-alignment fallback.

Entries before `0.3.2` are reconstructed from commit history rather than written at
release time.
