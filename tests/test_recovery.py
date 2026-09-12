"""Recovery must advance without turning weak evidence into publication permission."""
import sys
import wave
from types import SimpleNamespace

import pytest

from crowbarr import bazarr, inference
from crowbarr.config import Settings
from crowbarr.media import ReviewRequired, choose_audio


def test_provider_retries_discarded_bytes_and_stops_at_budget(tmp_path, monkeypatch):
    media = tmp_path / "film.mkv"
    media.touch()
    sidecar = media.with_suffix(".en.srt")
    sidecar.write_text("original")
    settings = Settings(bazarr_download_alternatives=True, max_provider_attempts=2)
    candidates = [{"subtitle": str(i), "provider": "test", "language": "en", "score": 100 - i}
                  for i in range(3)]
    monkeypatch.setattr(bazarr, "inspect_sources", lambda *a: {
        "state": "alternatives_available", "target": {"kind": "movies", "item_id": 1},
        "candidates": candidates,
    })
    calls = []

    class Client:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def download(self, kind, item, candidate, series):
            calls.append(candidate["subtitle"])

    monkeypatch.setattr(bazarr, "BazarrClient", Client)
    assert bazarr.try_alternative(settings, None, media, tmp_path, 1)["state"] == "discarded"
    assert bazarr.try_alternative(settings, None, media, tmp_path, 1)["state"] == "discarded"
    assert bazarr.try_alternative(settings, None, media, tmp_path, 1)["state"] == "exhausted"
    assert calls == ["0", "1"]
    assert sidecar.read_text() == "original"
    assert list((tmp_path / "provider-backups").rglob("*.srt"))[0].read_text() == "original"


def test_provider_changed_bytes_require_audit(tmp_path, monkeypatch):
    media = tmp_path / "film.mkv"
    media.touch()
    sidecar = media.with_suffix(".en.srt")
    original = b"\xef\xbb\xbforiginal\r\n"
    sidecar.write_bytes(original)
    monkeypatch.setattr(bazarr, "inspect_sources", lambda *a: {
        "state": "alternatives_available", "target": {"kind": "movies", "item_id": 1},
        "candidates": [{"subtitle": "new", "provider": "test", "language": "en"}],
    })

    class Client:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def download(self, *args): sidecar.write_text("replacement")

    monkeypatch.setattr(bazarr, "BazarrClient", Client)
    result = bazarr.try_alternative(Settings(bazarr_download_alternatives=True), None, media, tmp_path, 1)
    assert result["state"] == "downloaded"
    assert "awaiting audio audit" in result["reason"]
    assert list((tmp_path / "provider-backups").rglob("*.srt"))[0].read_bytes() == original


def sample(language="en", probability=0.95, words=12):
    return {"start": 60, "language": language, "probability": probability, "speech_tokens": words}


def test_language_requires_repeated_speech_not_silent_or_conflicting_votes():
    assert inference.language_verdict([sample(), sample(), sample(words=0)], "en")["language"] == "en"
    for samples in ([sample(words=0)] * 3, [sample(), sample("ja"), sample("ja")],
                    [sample(), sample(probability=0.4), sample(words=0)]):
        with pytest.raises(ReviewRequired, match="uncertain or mixed"):
            inference.language_verdict(samples, "en")
    with pytest.raises(ReviewRequired, match="Audio language is ja"):
        inference.language_verdict([sample("ja"), sample("ja"), sample(words=0)], "en")


def test_untagged_audio_setting_controls_selection():
    metadata = {"streams": [{"codec_type": "audio", "index": 1, "tags": {"language": "und"}}]}
    assert choose_audio(metadata, Settings())["index"] == 1
    assert Settings.model_validate({"allow_untagged_audio": False}).allow_untagged_audio is False
    with pytest.raises(ReviewRequired, match="Enable untagged audio"):
        choose_audio(metadata, Settings(allow_untagged_audio=False))
    assert choose_audio(metadata, Settings(allow_untagged_audio=True))["index"] == 1


def test_accepted_speech_preserves_word_confidence(monkeypatch, tmp_path):
    segments = [SimpleNamespace(start=i, end=i + 1, no_speech_prob=0.99, avg_logprob=logprob,
                               words=[SimpleNamespace(start=i, end=i + 1, word="dialogue", probability=p)])
                for i, (logprob, p) in enumerate([(-0.2, 0.97), (-0.2, 0.3), (-1.2, 0.97)])]

    class Model:
        def __init__(self, *args, **kwargs): pass
        def transcribe(self, *args, **kwargs):
            return iter(segments), SimpleNamespace(language="en", language_probability=1, duration=3)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr(inference, "_resident_key", None)
    monkeypatch.setattr(inference, "_resident", None)
    monkeypatch.setattr(inference.transcribe, "progress", None, raising=False)
    words, warnings = inference.transcribe(tmp_path / "unused.wav", Settings(), tmp_path)
    assert [w.probability for w in words] == [0.97, 0.3, 0]
    assert "Uncertain speech near 2.0s" in warnings


def test_speech_download_prepares_language_detector(monkeypatch, tmp_path):
    from crowbarr.capabilities import _speech_worker

    calls = []
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", SimpleNamespace(
        download_model=lambda name, **kwargs: calls.append(name),
    ))
    _speech_worker(str(tmp_path), "small.en")
    assert calls == ["small.en", "small"]


def test_language_probe_counts_japanese_speech_without_spaces(monkeypatch, tmp_path):
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * 48000)

    class Samples:
        def astype(self, *args): return self
        def __truediv__(self, value): return self

    class Model:
        hf_tokenizer = SimpleNamespace(encode=lambda *a, **kw: SimpleNamespace(ids=list(range(12))))
        def __init__(self, *args, **kwargs): pass
        def transcribe(self, *args, **kwargs):
            return iter([SimpleNamespace(text="これは日本語の会話です")]), SimpleNamespace(
                language="ja", language_probability=0.95,
            )

    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(
        frombuffer=lambda *a, **kw: Samples(), int16="int16", float32="float32",
    ))
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    with pytest.raises(ReviewRequired, match="Audio language is ja"):
        inference.verify_audio_language(audio, Settings(), tmp_path)
