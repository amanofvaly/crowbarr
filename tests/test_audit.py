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


def test_one_weak_internal_word_does_not_discard_a_good_anchor():
    cues, words = fixture()
    words[2].probability = 0.4
    result = audit(cues, words, 8)
    assert result["decision"] == "pass"
    assert result["supported_cues"] == 3


def test_partial_distributed_evidence_can_prove_a_global_offset():
    phrases = [f"unique phrase number {i} continues clearly" for i in range(30)]
    words = []
    cues = []
    for i, phrase in enumerate(phrases):
        audio_start = 10 + i * 20
        phrase_words = phrase.split()
        cue_words = [Word(audio_start + j * 0.25, audio_start + j * 0.25 + 0.2, word) for j, word in enumerate(phrase_words)]
        words.extend(cue_words)
        # Only every third cue belongs to the audio transcript; the others model
        # captions/phrasing that cannot provide a unique speech anchor.
        cues.append(Cue(audio_start + 2, cue_words[-1].end + 2, phrase))
        cues.extend(
            [Cue(audio_start + 5, audio_start + 6, "[music]"), Cue(audio_start + 7, audio_start + 8, "[applause]")]
        )
    result = audit(cues, words, 620)
    assert result["decision"] == "repair"
    assert result["coverage"] < 0.5
    assert result["distributed_across_timeline"]


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


def test_a_correct_shift_is_accepted_despite_a_few_mismatched_anchors():
    """Real anchors scatter; a handful always regress when a true offset is removed."""
    cues, words = fixture(4)
    before = audit(cues, words, 8)
    assert before["decision"] == "repair"
    corrected, _ = fixture()
    after = audit(corrected, words, 8)
    assert improved(before, after)


def test_a_shift_that_broadly_worsens_timing_is_still_rejected():
    cues, words = fixture(4)
    before = audit(cues, words, 8)
    # "Repairing" by shifting the wrong way must not be accepted.
    worse, _ = fixture(8)
    assert not improved(before, audit(worse, words, 8))


def test_a_feature_length_subtitle_is_not_rejected_for_being_long():
    """A percentage floor must not demand more evidence just because a film has more cues.

    Mirrors a real feature film: most caption text is present in the audio, but short
    repeated lines cannot form unique anchors, so coverage stays low while the token
    ratio stays high.
    """
    words, cues, clock = [], [], 0.0
    for index in range(60):  # anchorable phrases spread across the runtime
        phrase = [f"alpha{index}", f"beta{index}", f"gamma{index}"]
        start = clock
        for token in phrase:
            words.append(Word(clock, clock + 0.4, token, 0.95))
            clock += 0.5
        cues.append(Cue(start, clock - 0.1, " ".join(phrase)))
        for _ in range(10):  # short repeated interjections between the phrases
            words.append(Word(clock, clock + 0.3, "yeah", 0.95))
            cues.append(Cue(clock, clock + 0.3, "Yeah."))
            clock += 0.9

    result = audit(cues, words, clock)
    assert result["matched_token_ratio"] > 0.65
    assert result["supported_cues"] >= 2 * result["required_anchors"]
    assert result["dialogue_coverage"] < 0.1
    # Plenty of distributed anchors: the percentage floor must not veto it.
    assert result["decision"] != "inconclusive"
