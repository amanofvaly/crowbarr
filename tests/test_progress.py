import wave

import pytest

from crowbarr import inference
from crowbarr.config import Settings
from crowbarr.subtitles import Word


def test_sample_progress_accumulates_windows_and_restores_callback(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\0\0" * 16000 * 8)
    events = []

    def notify(current, total):
        events.append((current, total))

    def recognize(*_args):
        recognize.progress(1, 2)
        return [Word(0, 1, "hello", 0.9)], []

    recognize.progress = notify
    monkeypatch.setattr(inference, "transcribe", recognize)
    words, issues = inference.transcribe_windows(audio, [(0, 2), (6, 8)], Settings(), tmp_path)
    assert [w.start for w in words] == [0, 6]
    assert not issues
    assert events == [(1, 4), (2, 4), (3, 4), (4, 4)]
    assert recognize.progress is notify


def test_sample_progress_restores_callback_after_decoder_failure(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\0\0" * 16000 * 2)

    def notify(*_args):
        pass

    def recognize(*_args):
        raise RuntimeError("decoder stopped")

    recognize.progress = notify
    monkeypatch.setattr(inference, "transcribe", recognize)
    with pytest.raises(RuntimeError):
        inference.transcribe_windows(audio, [(0, 2)], Settings(), tmp_path)
    assert recognize.progress is notify
