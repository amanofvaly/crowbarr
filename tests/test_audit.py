import pytest

from crowbarr.audit import audit, improved
from crowbarr.subtitles import Cue, Word

TEXT = ["Please open the front door", "We should leave before sunrise", "Bring your coat and shoes"]


def fixture(offset=0):
    words = [
        Word(0.5 + i * 0.3 + (i // 5) * 0.5, 0.75 + i * 0.3 + (i // 5) * 0.5, text)
        for i, text in enumerate(" ".join(TEXT).split())
    ]
    cues = [
        Cue(words[i * 5].start + offset, words[i * 5 + 4].end + offset, text) for i, text in enumerate(TEXT)
    ]
    return cues, words


def test_correct_timing_and_normal_display_padding_pass():
    cues, words = fixture()
    for cue in cues:
        cue.start -= 0.2
        cue.end += 0.2
    result = audit(cues, words, 8)
    assert result["decision"] == "pass"
    assert result["coverage"] == 1


@pytest.mark.parametrize("offset", [3, 600, -3])
def test_large_offsets_need_repair(offset):
    cues, words = fixture(offset)
    before = audit(cues, words, 8)
    assert before["decision"] == "repair"
    assert before["signed_offset_seconds"] == pytest.approx(offset)
    corrected, _ = fixture()
    assert improved(before, audit(corrected, words, 8))


def test_localized_cut_error_and_drift_are_not_hidden():
    cues, words = fixture()
    cues[-1].start += 4
    cues[-1].end += 4
    assert audit(cues, words, 12)["decision"] == "repair"


def test_low_confidence_missing_text_and_short_track_are_inconclusive():
    cues, words = fixture()
    words[-1].probability = 0.4
    assert audit(cues, words, 8)["decision"] == "inconclusive"
    cues[-1].text = "Completely different missing dialogue"
    assert audit(cues, words, 8)["decision"] == "inconclusive"
    assert audit(cues[:1], words[:5], 8)["decision"] == "inconclusive"


def test_repair_must_pass_and_improve():
    cues, words = fixture(4)
    before = audit(cues, words, 8)
    assert not improved(before, before)
    worse, _ = fixture(6)
    assert not improved(before, audit(worse, words, 12))


def test_wrong_text_cannot_pass_just_because_timestamps_match():
    cues, words = fixture()
    for cue in cues:
        cue.text = "Unrelated repeated phrase here"
    assert audit(cues, words, 8)["decision"] == "inconclusive"


def test_backend_numpy_scalars_produce_serializable_evidence():
    import json

    np = pytest.importorskip("numpy")
    cues, words = fixture()
    for word in words:
        word.start = np.float64(word.start)
        word.end = np.float64(word.end)
        word.probability = np.float32(word.probability)
    result = json.loads(json.dumps(audit(cues, words, 8), allow_nan=False))
    assert result["decision"] == "pass"
