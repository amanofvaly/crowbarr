Agent Test

## What I actually tested

I performed five layers of testing:

1. Repository validation

   - Ran the complete test suite: **85 tests passed**.
   - Ran Ruff: clean.
   - Validated CPU and CUDA Compose configurations.
   - Inspected queue recovery, path mapping, publication safety, authentication, hooks, and Plex refresh logic.

2. Live integration testing

   Using Crowbarr’s own client code against your running services:

   - Sonarr connected and returned **1,594 managed files**.
   - Radarr connected and returned **620 managed files**.
   - Bazarr connected successfully.
   - Plex connected successfully.
   - Sonarr `/tv` and Radarr `/movies` paths mapped correctly.
   - Crowbarr’s real Plex refresh function completed successfully.

3. Real authored-subtitle test

   Media:

   `The Big Bang Theory - S08E08 - The Prom Equivalency WEBDL-720p.mkv`

   Characteristics:

   - 20 minutes 10 seconds
   - English audio
   - Embedded English subtitle
   - Bazarr external English SDH subtitle
   - External subtitle: 303 cues
   - Embedded subtitle: 446 cues

   Independent comparison of the Bazarr subtitle against the embedded track measured:

   - Median start delay: **1.129 seconds**
   - Median end delay: **1.181 seconds**
   - p95 boundary error: **3.503 seconds**
   - Maximum boundary error: **5.622 seconds**

   This was therefore a genuine, measurable synchronization problem.

4. Three real Whisper audit runs

   I ran the same episode through Crowbarr with `tiny`, `base`, and the documented default `small` model.

   - `tiny`: 42/303 confident cues, 13.9% coverage, result `review`
   - `base`: 74/303 confident cues, 24.4% coverage, result `review`
   - `small`: 107/303 confident cues, 35.3% coverage, result `review`

   The `small` model independently measured:

   - Signed subtitle offset: **1.641 seconds**
   - Median boundary error: **2.245 seconds**
   - p95 boundary error: **4.055 seconds**

   Crowbarr therefore detected the same problem through audio, but produced no repair.

5. Real missing-subtitle test

   Media:

   `The Big Bang Theory - S11E22 - The Monetary Insufficiency Bluray-720p.mkv`

   Characteristics:

   - 19 minutes 12 seconds
   - No external SRT
   - Audio and embedded subtitle were untagged
   - I enabled `allow_untagged_audio` so the fallback could run

   Result:

   - Whisper transcription completed.
   - WhisperX produced **270 aligned cues**.
   - **108 passages** failed alignment validation.
   - Crowbarr discarded the complete subtitle and returned `review`.
   - No candidate SRT was made available.

## Confirmed product defects

### 1. Crowbarr chooses the external subtitle without comparing it to the embedded subtitle

The first test file contained a correctly muxed embedded English subtitle, but Crowbarr chose the newer Bazarr sidecar automatically.

The selection happens in [library.py](/Users/amankumar/Dev/subsync/crowbarr/library.py:21). It chooses the newest eligible external SRT. In [processor.py](/Users/amankumar/Dev/subsync/crowbarr/processor.py:42), embedded subtitles are considered only when no external source exists.

Effect:

- A poor Bazarr subtitle suppresses a potentially better embedded track.
- Crowbarr spends time trying to repair the worse source.
- It cannot compare source completeness, text coverage, language metadata, or timing evidence.
- In this test, the chosen external subtitle had 303 cues while the ignored embedded subtitle had 446.

Required change:

Build a source-arbitration stage that evaluates every eligible source:

- External provider subtitles
- Embedded text subtitles
- Existing Crowbarr output
- Generated transcription fallback

Choose the source using text coverage and audio-grounded timing evidence, rather than modification time.

### 2. The subtitle matcher rejects many ordinary authored cues before the audit starts

A cue is preserved only if all of these conditions pass in [subtitles.py](/Users/amankumar/Dev/subsync/crowbarr/subtitles.py:160):

- At least 75% of its tokens match.
- Its first token matches.
- Its final token matches.
- The matched words are not spread too far apart.
- The matched duration is at most 15 seconds.
- None of the recognized words were assigned to another cue.
- The match must originate from a sufficiently large block containing a unique three-token phrase.

This excludes many normal subtitle cases:

- Short cues such as “Yeah.”
- Repeated dialogue.
- A cue where Whisper misses the first or final word.
- Speaker labels and SDH markup.
- Slight text differences such as contractions.
- Combined or split dialogue lines.
- Proper-name recognition errors.
- Lyrics and non-speech captions.

This is why even `small`, which matched 91.9% of the subtitle’s tokens overall, supported only 107 of 303 cues.

Required change:

Separate global text compatibility from per-cue boundary evidence. First establish that the subtitle belongs to the same episode/cut, then accept partial high-quality anchors without requiring every individual cue to satisfy exact boundary matching.

### 3. The audit requires 100% cue coverage

The decisive condition is:

```python
len(evidence) == len(cues)
```

in [audit.py](/Users/amankumar/Dev/subsync/crowbarr/audit.py:48).

That means one unsupported cue makes the entire subtitle inconclusive.

This is not just “conservative.” It makes normal subtitles nearly impossible to approve because:

- SDH captions do not have spoken anchors.
- Music captions do not have spoken anchors.
- Very short cues lack unique phrases.
- Whisper will not recognize every word correctly.
- Repeated phrases deliberately fail the unique-anchor logic.
- Some words will fall below the 0.8 confidence threshold.

The test proved this across three model sizes. Increasing the model from `tiny` to `small` improved coverage from 13.9% to 35.3%, but never approached 100%.

Required change:

Use an evidence threshold such as:

- Minimum absolute number of anchors
- Minimum percentage of dialogue cues, excluding non-speech cues
- Anchors distributed across beginning, middle, and end
- Maximum unexplained timing variance
- Separate handling for global offset, linear drift, and discontinuities

For example, 30–50 reliable anchors distributed across a 20-minute episode can be more than sufficient to prove a global offset.

### 4. One low-confidence word invalidates an otherwise useful cue

After matching a cue, the audit collects all Whisper words inside its passage. If any word has probability below 0.8, the entire cue is discarded from the evidence set:

[audit.py](/Users/amankumar/Dev/subsync/crowbarr/audit.py:25)

This is harsher than necessary. A ten-word cue with nine high-confidence words and one 0.79 word provides useful timing evidence, but Crowbarr throws it away.

Required change:

Use aggregate confidence:

- Median or trimmed-mean word confidence
- Confidence on the first and last reliable anchor
- Minimum count of confident words
- Ignore isolated low-confidence internal words

### 5. Any uncertain ASR segment vetoes the complete repair

The transcriber records an issue whenever a segment has:

- `no_speech_prob > 0.6`, or
- `avg_logprob < -1.0`

See [inference.py](/Users/amankumar/Dev/subsync/crowbarr/inference.py:33).

The processor then does this:

```python
if issues:
    before["decision"] = "inconclusive"
```

in [processor.py](/Users/amankumar/Dev/subsync/crowbarr/processor.py:65).

In the `small` run, uncertainty around approximately 801–829 seconds invalidated timing evidence gathered from the rest of the episode.

This is a second independent veto. Even if the 100% coverage requirement were removed, these unrelated uncertain segments would still stop the repair.

Required change:

ASR uncertainty must remain local:

- Exclude the uncertain segment from evidence.
- Reduce confidence around that time range.
- Require sufficient evidence elsewhere.
- Only reject the whole file if uncertainty covers too much of the runtime or prevents cut identification.

### 6. The tested subtitle would fail another threshold even after those two fixes

The `small` model reported:

```text
generated_word_ratio = 0.25217
```

The default maximum is:

```text
max_generated_ratio = 0.25
```

In [processor.py](/Users/amankumar/Dev/subsync/crowbarr/processor.py:114), exceeding this threshold adds another fatal issue.

So the subtitle exceeds the limit by about 0.22 percentage points. Even if the coverage and global-ASR vetoes were fixed, it could still be rejected later.

There is also a conceptual problem: a repair operation should not generate replacement dialogue merely because individual authored cues could not be aligned. It should generally preserve the authored text and adjust only timings.

Required change:

Separate modes clearly:

- Timing-only repair: never invent or replace authored dialogue.
- Hybrid completion: optional, separately configured behavior.
- Full generation: used when no acceptable authored source exists.

### 7. Missing-subtitle generation performs a fragile second alignment pass

Faster Whisper already returns word timestamps. Crowbarr groups those timestamped words into generated passages, then sends every passage through WhisperX again.

The forced alignment logic rejects a passage when:

- No word segments are returned.
- Any aligned word lacks start/end/score.
- Any score is non-finite.
- Any word score is below 0.4.

See [inference.py](/Users/amankumar/Dev/subsync/crowbarr/inference.py:79).

The real generation test produced 270 good cues but rejected 108 passages.

Required change:

For generated subtitles:

- Use Faster Whisper timestamps as the baseline.
- Use WhisperX only to refine boundaries where it succeeds.
- Fall back to original Whisper timestamps when WhisperX fails.
- Do not delete an otherwise valid cue because one phoneme-alignment score is weak.

WhisperX should improve generated timestamps, not become a mandatory second point of failure.

### 8. Any alignment issue blocks the entire output

All alignment warnings are appended to the global `issues` list. The publication code then applies:

```python
if issues:
    return {"state": "review", ...}
```

in [processor.py](/Users/amankumar/Dev/subsync/crowbarr/processor.py:152).

Consequences:

- 270 valid cues plus one weak cue produces no subtitle.
- A harmless reading-speed warning has the same publication effect as catastrophic corruption.
- Issues have no severity levels.
- There is no acceptable-error budget.
- There is no distinction between recoverable and fatal failures.

Required change:

Introduce issue severity:

- Fatal: invalid timestamps, wrong language, wrong episode/cut, zero usable dialogue.
- Recoverable: fall back to previous timing for one cue.
- Warning: low confidence that should be visible in the report.
- Informational: metric only.

Publication should depend on fatal issues and quality aggregates, not `bool(issues)`.

### 9. Rejected candidates are not preserved for inspection

Crowbarr rendered the 270-cue generated subtitle in memory, computed its hash, and wrote a JSON report. It did not save the SRT because issues existed.

That means developers and users cannot inspect the candidate that was almost produced.

Required change:

Save rejected candidates privately, for example:

```text
/config/candidates/<job-id>.srt
```

The dashboard should allow:

- Download candidate
- Compare with source
- Inspect problematic cues
- Approve manually
- Retry with another model or threshold

It must remain outside the media library until approved.

### 10. Bazarr integration is not a workflow integration yet

Crowbarr can:

- Test Bazarr connectivity
- Notice new SRT files during reconciliation
- Receive an optional Bazarr notification hook

Crowbarr cannot:

- Request a Bazarr subtitle search
- Read Bazarr’s selected provider/release metadata
- Inspect the subtitle score
- Ask Bazarr for alternative candidates
- Blacklist a demonstrably wrong subtitle
- Trigger an upgrade search after rejecting a candidate

This is explicitly visible in [README.md](/Users/amankumar/Dev/subsync/README.md:105): the Bazarr connection is currently only a connectivity test.

Required change:

Implement an actual Bazarr adapter. When a subtitle is rejected, Crowbarr should be able to:

1. Record why it failed.
2. Ask Bazarr for another candidate.
3. Avoid retrying the same subtitle.
4. Compare multiple candidates.
5. Fall back to generation only after provider options are exhausted.

### 11. Plex integration is coarse-grained

The Plex adapter refreshes every movie and TV section after publication:

[integrations.py](/Users/amankumar/Dev/subsync/crowbarr/integrations.py:30)

It does not:

- Refresh only the affected item or directory.
- Confirm the new subtitle appeared.
- Verify its language/forced/SDH flags.
- Confirm which subtitle Plex selects.
- Detect naming incompatibilities.
- Report a failed Plex pickup back to the job.

The refresh endpoint worked in live testing, but that only proves the request was accepted.

Required change:

After publication:

1. Refresh the affected library path or metadata item.
2. Poll Plex metadata for a bounded period.
3. Confirm the Crowbarr stream appears.
4. Record the stream language and title.
5. Mark the job complete only after filesystem publication; show Plex discovery as a separate delivery state.

## Why the full workflow did not complete

The integration chain worked up to processing:

```text
Sonarr catalog
→ path mapping
→ media discovery
→ external subtitle discovery
→ audio extraction
→ Whisper transcription
→ timing problem detected
```

It stopped here:

```text
audit policy
→ inconclusive
→ no repair
→ no sidecar publication
```

The generation workflow stopped here:

```text
Whisper transcription
→ WhisperX alignment
→ 270 good cues + 108 failed passages
→ global issues list
→ no publication
```

Because no real subtitle was published, I could not truthfully claim that the final sequence succeeded:

```text
publication → Plex discovery → Plex playback selection
```

I tested the Plex connection and real refresh call, but I did not fake the missing middle step.

## Development priority

The immediate implementation order should be:

1. Add embedded-versus-external source arbitration.
2. Replace 100% audit coverage with distributed statistical evidence.
3. Stop turning local ASR uncertainty into a global veto.
4. Separate timing repair from dialogue generation.
5. Add Whisper-timestamp fallback when WhisperX fails.
6. Add issue severity instead of rejecting on any issue.
7. Persist review candidates.
8. Integrate Bazarr searches and alternative-candidate selection.
9. Verify Plex discovery after publication.
10. Build a real regression corpus from your own problematic media.

That is what the testing established. The product does not have merely two problems; it has several interacting policies that currently make both primary workflows unlikely to publish on ordinary media.

phase 2 audit:
Next work, in order
1. Server hardware and runtime audit
   - CPU model, cores and instruction support.
   - Total/free RAM and swap.
   - NVIDIA GPU presence.
   - nvidia-smi, driver and CUDA compatibility.
   - GPU visibility inside containers.
   - VRAM capacity and utilization.
   - Storage throughput and available model/cache space.
   - Existing load from Plex, Sonarr, Radarr and Bazarr.
2. Deploy the current Crowbarr build on the server
   - Use the CPU image if no compatible NVIDIA GPU exists.
   - Use the CUDA image only after proving container GPU access.
   - Mount media read-only initially.
   - Give Crowbarr a separate writable test library and private state directory.
   - Verify the health endpoint, queue recovery, model downloads and filesystem permissions.
   - Confirm the report states the backend actually used—CPU or CUDA.
3. Implement proper queue priorities
   Proposed order:
   1. Manual “Process now”
   2. New Sonarr/Radarr imports
   3. New or upgraded Bazarr subtitles
   4. Automatic retries
   5. Historical backlog
   Also add:
   - Priority and origin fields in SQLite.
   - Priority aging to prevent permanent backlog starvation.
   - Direct event targeting instead of rescanning the complete catalog for every hook.
   - Manual processing endpoint and dashboard button.
   - Move-to-top and cancel controls.
   - Do not normally interrupt the active job; priority applies to the next job.
   - Optional explicit “stop current job and run this” action.
4. Implement lazy resource scheduling
   - Continue allowing only one inference job by default.
   - Minimum-free-RAM and minimum-free-VRAM gates.
   - CPU load threshold.
   - Backlog cooldown between jobs.
   - Manual/import jobs bypass the cooldown.
   - Optional quiet hours.
   - Reduced OS CPU and disk priority for background work.
   - Defer background generation while Plex is transcoding or actively playing.
   - Configurable hourly background-processing budget.
   - Display current RAM, VRAM, backend and reason a job is waiting.
   - Retain transcript-cache size limits and model reuse.
5. Implement sampled authored-subtitle auditing
   For existing subtitles, do not transcribe an entire movie immediately:
   - Sample dialogue windows from the first, middle and final portions.
   - Establish a constant offset or linear drift when regions agree.
   - Escalate to additional windows when they disagree.
   - Run full-file transcription only for ambiguous cuts or generation.
   - Avoid sampling literal credits; select windows containing authored dialogue.
6. Add ad, recap and discontinuity handling
   - Compute local offset estimates across multiple regions.
   - Detect persistent offset jumps.
   - Distinguish linear frame-rate drift from inserted/deleted scenes.
   - Fit piecewise timing segments around ads or alternate recaps.
   - Require anchors on both sides of every discontinuity.
   - Refuse repair when segment boundaries are not sufficiently supported.
   - Preserve non-dialogue captions using neighboring segment transforms.
7. Complete Bazarr integration
   Crowbarr currently only notices sidecars. It needs to:
   - Read provider/release/score information.
   - Request a subtitle search.
   - Reject or blacklist a demonstrably wrong candidate.
   - Request another candidate.
   - Avoid retrying the same subtitle hash.
   - Generate subtitles only after provider alternatives are exhausted.
8. Complete Plex verification
   - Refresh only the affected item/path.
   - Poll Plex metadata afterward.
   - Confirm the new sidecar appears.
   - Confirm language, forced and hearing-impaired attributes.
   - Record Plex delivery separately from filesystem publication.
   - Test while Plex is playing and transcoding.
Required real-world server tests
All inference must run inside the deployed server container while monitoring RAM, VRAM, CPU, I/O and runtime.
Television cases
- The real S08E08 authored-subtitle problem.
- The real S11E22 missing-subtitle case.
- Correct embedded subtitle versus bad Bazarr sidecar.
- Untagged audio and embedded subtitles.
- Episode with recap differences.
- Episode containing credits dialogue.
- Episode with an inserted advertisement or discontinuity.
Full feature-length movie
At least one 90–150 minute movie from the real Radarr library, preferably with:
- English multichannel audio.
- Embedded English subtitle.
- Bazarr external subtitle.
- Common release metadata.
- Enough dialogue throughout the runtime.
Run these movie scenarios:
- Existing correct subtitle: Crowbarr must leave it unchanged.
- Known constant shift: apply a controlled shift to a test copy and recover it.
- Known frame-rate drift: test 23.976/24/25 fps timing behavior.
- Beginning trailer/ad insertion.
- Middle insertion that requires piecewise correction.
- Wrong-release subtitle: must reject it.
- Missing subtitle: generate the complete movie subtitle.
- Restart Crowbarr halfway through processing.
- Run during Plex direct play.
- Run during Plex transcoding.
- Retry using the transcript cache.
Queue/load acceptance test
Create a realistic server queue containing:
- Historical backlog jobs.
- A newly imported episode.
- A newly imported movie.
- A Bazarr subtitle upgrade.
- An automatic retry.
- A manual request.
Expected order:
currently running job finishes
→ manual request
→ new imports
→ Bazarr upgrade
→ retry
→ backlog resumes lazily
Verify that:
- Only one model is resident.
- Manual and new-import jobs jump ahead of backlog.
- The service does not continuously saturate the server.
- Plex remains usable.
- Restarting does not duplicate or lose jobs.
- Changed media supersedes stale work.
- No original subtitle or media file is overwritten.
- Cache growth remains bounded.
Completion criteria
Crowbarr should not be called server-ready until:
- CPU or CUDA execution is proven on the actual server.
- A complete feature-length movie passes.
- Both real TV workflows publish usable results.
- Queue priorities work under a real mixed backlog.
- RAM and VRAM remain inside hardware-specific limits.
- Plex playback remains stable during background processing.
- Bazarr alternatives and Plex discovery are verified end-to-end.
- A multi-hour server soak finishes without OOM, stuck jobs or uncontrolled load.