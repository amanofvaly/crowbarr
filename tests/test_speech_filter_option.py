"""Audits can hear audio the voice filter would remove. Everything else keeps the filter."""

import sys
import types
import wave
from types import SimpleNamespace

import pytest

from crowbarr import inference
from crowbarr.config import Settings


@pytest.fixture
def recognizer(monkeypatch):
    calls = []

    class Model:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, audio, **options):
            calls.append(options)
            word = SimpleNamespace(start=1.0, end=1.5, word="hello", probability=0.9)
            segment = SimpleNamespace(start=1.0, end=1.5, avg_logprob=-0.2, words=[word])
            return iter([segment]), SimpleNamespace(language="en", language_probability=0.99, duration=2.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr(inference, "_resident", None)
    monkeypatch.setattr(inference, "_resident_key", None)
    return calls


def silent_wav(path, seconds=10):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 16000 * seconds)
    return path


def settings():
    return Settings(device="cpu", compute_type="int8", cpu_threads=1, model="small.en", language="en")


def test_recognition_keeps_the_voice_filter_by_default(recognizer, tmp_path):
    inference.transcribe_bounded(silent_wav(tmp_path / "a.wav"), settings(), tmp_path, tmp_path / "ck")
    assert recognizer[0]["vad_filter"] is True


def test_an_audit_can_hear_the_audio_the_filter_would_remove(recognizer, tmp_path):
    words, _ = inference.transcribe_bounded(
        silent_wav(tmp_path / "a.wav"), settings(), tmp_path, tmp_path / "ck", speech_filter=False
    )
    assert recognizer[0]["vad_filter"] is False
    assert words[0].text == "hello"
