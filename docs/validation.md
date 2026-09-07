# Validation and release gates

This is a preview implementation, not a claim that every movie aligns perfectly.

## Verified during initial implementation

On macOS ARM64 with Python 3.13, a running service discovered a locally generated
English speech clip and its three-cue SRT with deliberately incorrect timestamps
starting ten minutes into the video. With Whisper `tiny`, all three authored cues
were retained, retimed, and published. The original SRT was unchanged. Whisper
`base` also completed the authored-text case.

A second clip had no SRT and an audio stream starting two seconds after the video.
The initial transcription-only run was withheld because the numeral `5` produced
a weak alignment. Adding a spoken-number normalization stage fixed that issue
without lowering the acceptance threshold. With `base`, the service generated two
cues, retained the two-second timeline offset, and passed its checks.

These are functional smoke tests using synthesized speech, not independently
annotated timing benchmarks. They do not establish a movie-library success rate.
The local browser smoke check passed login, activity rendering, settings save,
mobile overflow checks, and sign-out with no JavaScript errors. Bootstrap assets
were served locally.

Both CPU and CUDA Compose files validate syntactically. Docker Engine was not
running in the development environment, so container builds and NVIDIA execution
were not exercised there. The real-model tests used native CPU execution.
The API catalog adapters were also verified against existing Sonarr and Radarr
installations using read-only requests. Automated tests cover monitored files, path
mappings, upgrades, removals, outages, and configuration changes during a sync.
Hook payloads and Plex API calls have automated contract tests; live import-to-subtitle
processing and player subtitle selection still require a deployment test. Output uses a Crowbarr marker before the language suffix, following the
[same ordering used by Subgen](https://github.com/McCloudS/subgen/blob/main/subgen.py).

## Automated checks and release criteria

The automated tests cover subtitle parsing, phrase matching independent of old
timestamps, repeated/ambiguous phrases, missing dialogue, queue idempotence, claims,
restart recovery, source upgrades, video replacement, authentication, credential
redaction, hooks, and pipeline publication with controlled inference adapters.
Real FFmpeg media extraction is tested where FFmpeg is installed.

Controlled adapters validate orchestration; they do not validate Whisper or WhisperX
accuracy. A browser check validates real dashboard behavior, not inference quality.
Passing synthetic speech also cannot establish performance on film dialogue.

Before calling a release production-ready, evaluate a licensed or privately held
corpus covering offsets, frame-rate drift, inserted/deleted scenes, accents, music,
overlap, repeated lines, missing provider subtitles, embedded tracks, and nonzero
audio offsets. Record manually annotated start/end boundaries at beginning, middle,
end, and edit points. Report median/p95 boundary error, text retention, missing speech,
false acceptances, review rate, runtime, and peak host/GPU memory by hardware/model.

The acceptance gates currently favor withholding uncertain output. They have not
been calibrated to a target review rate. An untouched original subtitle is not
evidence that Crowbarr succeeded; only a completed job means its own checks passed.

Known resource boundaries: model packages and weights are substantial, the CPU
image must resolve CPU PyTorch wheels, and one process runs at a time. Native Windows,
Linux ARM, and all CUDA devices are not certified. Build and test a release image
on its target hardware before advertising support for that platform.

## Audit-first preview

Automated cases cover passing originals, offsets, localized timing errors, low-confidence
anchors, missing/repeated text, insufficient evidence, and candidates without improvement.
Passing and inconclusive audits skip forced alignment; unchanged input is not requeued.
The initial audit still uses full-track ASR. Thresholds are provisional and have not
been calibrated on a manually annotated movie corpus. Earlier synthetic inference
results above predate this audit gate and do not establish its acceptance rate.

A native CPU run with the real speech model exercised the new audit on the existing
three-cue synthetic clip. Only two cues had sufficient confidence, so the result
was inconclusive and no forced alignment or new repair was attempted. This run
also exposed NumPy scalar serialization in audit evidence; native scalar conversion
and a regression test now cover it. The browser check verifies actual audit details
and JSON downloads at desktop and mobile widths.
