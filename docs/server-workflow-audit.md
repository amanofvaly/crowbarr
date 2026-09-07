# Server workflow audit — in progress (7 September 2026)

Work branch: `codex/server-workflow-audit`. This is not a server-ready signoff.

## Observed hardware and deployment

- TrueNAS `192.168.0.99`, i3-12100F (4 cores / 8 threads), ~16 GiB RAM, no swap.
- NVIDIA T400, 2 GiB VRAM, driver 550.142 / CUDA 12.4.
- CUDA visibility verified inside the deployed `crowbarr-audit` container.
- Initial 2 GiB container RAM cap caused feature-film inference subprocess OOM kills. Removed at the user's direction; Docker now reports `HostConfig.Memory=0`. ZFS ARC is reclaimable and must be accounted for in host headroom estimates.
- A bounded five-minute inference/chunk checkpoint implementation replaces whole-movie decode/VAD allocations. TV runs use ~1 GiB RAM; definitive movie/chunk peak measurement pending.
- Original TV/movie libraries remain mounted read-only. Writable copies are in `/mnt/.ix-apps/app_mounts/plex/data/crowbarr-audit`, mounted `/test` in Crowbarr and `/data/crowbarr-audit` in Plex.
- Private state/models: `/mnt/Server-Storage/crowbarr-audit/config`.
- Dashboard: http://192.168.0.99:8449. API token remains private in `/config/admin-token`.
- Dedicated Plex library `Crowbarr Workflow Tests`, section 5. Creating the old personal-media scanner requires its actual supported language `xn`.
- Copied videos inherited mode 770; Plex could not read them. Test-copy permissions were corrected without changing original files.

## Real runs

1. S08E08, The Big Bang Theory — The Prom Equivalency: full-file `small`, CUDA `int8_float16` executed. Selected embedded track, external 161 supported cues / p95 4.311 s; embedded 239 supported cues / p95 2.097 s. First repair withheld because the improvement threshold was not met. This exposed treatment of normal authored trailing reading time as timing error. New audit policy requires tightly supported starts before accepting bounded trailing time. Re-run pending.
2. S11E22, The Big Bang Theory — The Monetary Insufficiency: CUDA generated 376 cues, no blocking issues, separate SRT published. Plex discovered and selected stream 128468 (English SRT External). Downloaded Plex stream SHA256 exactly matches Crowbarr report: `5e0805ddd47023eaff11dc98f35b9db2d8914ac2378db0ca3400f81142cb7337`. User watched and reported that subtitles are synchronized, but questioned provenance; exact stream match confirms generated output. This episode also has an embedded subtitle; “missing” means no eligible authored subtitle under the original untagged-track policy.
3. The Disaster Artist (2017), real Radarr movie 1926 / file 1322, 103:50, 23.976 fps, HEVC 1080p, AAC 7.1, English embedded tracks and external English SRT. Whole-file inference under initial artificial 2 GiB cap OOM-killed twice; queue retained job/retried. New sampled run (job 6) passed and left sources unchanged. Windows 686.73–806.73, 3055.307–3175.307, 5234.696–5354.696 seconds. External 31 anchors, p95 0.554 seconds. Full generation and controlled timing scenarios pending.
4. An actual Plex Modern Family session was observed while Crowbarr ran. Background jobs deferred. Later user played test S11E22 while another job ran; Plex reported video/audio copy (direct stream), and captions were visible in browser. Forced transcoding test pending.
5. Bazarr history for Sonarr episode 26462 identifies SuperSubtitles sidecar with provider score 94.17% and an older Bazarr 5.63-second sync adjustment. Actual provider search returned 22 alternatives (embedded, OpenSubtitles, Gestdown, others). Search/metadata adapter implemented; safe automatic alternative download/rejection workflow is incomplete. Bazarr blacklist endpoint deletes its target sidecar and initiates download, so it must not be naively called.

## Implementation / outstanding validation

Implemented or in progress: durable priority/origin migration, priority aging, single active claim, process-next/cancel controls, direct arr event lookup, resource gates/budget/cooldown/quiet hours/Plex activity, CPU fallback on model initialization, actual backend reports, optional WhisperX, private candidate download/manual approval, sampled audit with full escalation, bounded robust/piecewise timing model, five-minute full transcription checkpoints, targeted Plex discovery with exact stream-content hash fallback, ZFS ARC telemetry.

Latest code is ahead of the deployed image. Do not assume every change is deployed. Regression suite most recently passed 94 tests before subsequent small edits; rerun necessary. Real mixed queue/restart/CPU fallback/discontinuity/wrong-cut/full generation/soak acceptance is still pending. Preserve the initial failures in final findings.
