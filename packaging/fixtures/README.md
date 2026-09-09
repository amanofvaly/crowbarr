`speech.flac` is synthetic English speech generated for Crowbarr packaging checks
using the macOS Samantha system voice at 150 words/minute, then converted to mono
16 kHz FLAC with FFmpeg. Its text is in `packaging/inference_check.py`.

It contains no library media or user information. This fixed input tests changes
in runtime packaging; it is not an accuracy benchmark for natural dialogue.
