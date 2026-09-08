import pytest

from crowbarr.audit import ALIGNED_START_P95, audit, improved, improvement
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


def test_small_constant_offset_is_not_sent_to_review_over_noisy_anchors():
    """A tight subtitle with a few wild anchors is correct, whichever path accepts it.

    Real measurement: 30 anchors, median start error 0.27 s, three mismatched anchors
    dragging the 95th percentile to 3.6 s. The fit clears every scatter limit, so the
    file must be left alone rather than flagged for a repair that cannot help it.
    """
    words, cues, clock = [], [], 0.0
    for index in range(40):
        phrase = [f"alpha{index}", f"beta{index}", f"gamma{index}"]
        start = clock
        for token in phrase:
            words.append(Word(clock, clock + 0.4, token, 0.95))
            clock += 0.5
        bad = 3.5 if index in (11, 27, 33) else 0.0
        cues.append(Cue(start + 0.42 + bad, clock - 0.1 + 0.42 + bad, " ".join(phrase)))
        clock += 8.0
    result = audit(cues, words, clock)
    assert result["start_p95_seconds"] > ALIGNED_START_P95, "the tail is genuinely noisy"
    assert abs(result["start_fit"]["offset_seconds"]) > 0.35, "and the fit offset is over the old limit"
    assert result["decision"] == "pass"


def test_a_lag_split_between_offset_and_scale_is_still_a_lag():
    """Real measurement: cues 0.93 s late passed as aligned because the fit charged
    0.47 s to its offset and the rest to its scale, each under its own limit."""
    words, cues, clock = [], [], 0.0
    for index in range(40):
        phrase = [f"alpha{index}", f"beta{index}", f"gamma{index}"]
        start = clock
        for token in phrase:
            words.append(Word(clock, clock + 0.4, token, 0.95))
            clock += 0.5
        # A constant 0.47 s late plus a slow drift that reaches another 0.5 s by the end.
        lag = 0.47 + 0.5 * (index / 39)
        cues.append(Cue(start + lag, clock - 0.1 + lag, " ".join(phrase)))
        clock += 8.0
    result = audit(cues, words, clock)
    assert abs(result["start_fit"]["offset_seconds"]) <= 0.5, "each fit term looks small"
    assert result["signed_offset_seconds"] > 0.5, "yet the cues are measurably late"
    assert result["decision"] == "repair"


def _late_subtitle(clipped_indices):
    """A subtitle 1.4 s late, where the named lines end before their speech does."""
    words, cues, shifted, clock = [], [], [], 0.0
    for index in range(40):
        phrase = [f"alpha{index}", f"beta{index}", f"gamma{index}"]
        start = clock
        for token in phrase:
            words.append(Word(clock, clock + 0.4, token, 0.95))
            clock += 0.5
        speech_end = clock - 0.1
        end = speech_end - 0.6 if index in clipped_indices else speech_end
        cues.append(Cue(start + 1.4, end + 1.4, " ".join(phrase)))
        shifted.append(Cue(start, end, " ".join(phrase)))
        clock += 8.0
    return audit(cues, words, clock), audit(shifted, words, clock)


def test_one_clipped_line_does_not_discard_a_correct_repair():
    """Measured on the real library: a 1.19 s lag fixed to 0.15 s was thrown away
    because a single line of 43 lost 0.554 s of trailing reading time."""
    verdict = improvement(*_late_subtitle({17}))
    assert verdict["accepted"], verdict["reason"]


def test_a_shift_that_clips_many_lines_is_still_refused():
    verdict = improvement(*_late_subtitle(set(range(12))))
    assert not verdict["accepted"]
    assert "cut short" in verdict["reason"]


def test_a_refused_repair_always_says_why():
    """A verdict nobody can inspect is a label. Every refusal carries a stated reason."""
    cues, words = fixture(4)
    before = audit(cues, words, 8)
    worse, _ = fixture(6)
    verdict = improvement(before, audit(worse, words, 12))
    assert not verdict["accepted"]
    assert verdict["reason"], "a refusal without a reason cannot be shown to anyone"


def test_an_accepted_repair_publishes_the_checks_it_cleared():
    words, cues, clock, shifted = [], [], 0.0, []
    for index in range(40):
        phrase = [f"alpha{index}", f"beta{index}", f"gamma{index}"]
        start = clock
        for token in phrase:
            words.append(Word(clock, clock + 0.4, token, 0.95))
            clock += 0.5
        bad = 5.0 if index in (7, 19, 31) else 0.0
        cues.append(Cue(start + 1.4 + bad, clock - 0.1 + 1.4 + bad, " ".join(phrase)))
        shifted.append(Cue(start + bad, clock - 0.1 + bad, " ".join(phrase)))
        clock += 8.0
    verdict = improvement(audit(cues, words, clock), audit(shifted, words, clock))
    assert verdict["accepted"]
    assert len(verdict["checks"]) == 5
    for check in verdict["checks"]:
        assert {"name", "measured", "limit", "passed", "failure"} <= set(check)
        assert check["passed"]


def test_audit_publishes_every_threshold_it_judged():
    cues, words = fixture()
    result = audit(cues, words, 8)
    assert result["checks"], "a sufficient audit must show what it measured"
    for check in result["checks"]:
        assert {"name", "measured", "limit", "passed"} <= set(check)
    # A decision to leave the subtitle alone means nothing was over its limit.
    assert result["decision"] != "pass" or all(check["passed"] for check in result["checks"])


def test_inconclusive_names_the_measurement_that_fell_short():
    """Measured on a real film: 57.7% text match against a 65% floor arrived as the
    bare sentence "Insufficient confident coverage", with no number anywhere."""
    cues, words = fixture()
    for cue in cues:
        cue.text = "Unrelated repeated phrase here"
    result = audit(cues, words, 8)
    assert result["decision"] == "inconclusive"
    assert result["coverage_checks"], "an inconclusive verdict must still show its evidence"
    short = [check for check in result["coverage_checks"] if not check["passed"]]
    assert short, "something must have fallen short to reach this verdict"
    for check in short:
        assert check["measured"] in result["reason"]
        assert check["limit"] in result["reason"]
    assert "recognized_words" in result and "low_confidence_ratio" in result


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


def test_scattered_bad_anchors_do_not_turn_a_correct_subtitle_into_a_repair():
    """47 good anchors and a few wild ones is a correct subtitle, not a timing fault."""
    words, cues, clock = [], [], 0.0
    for index in range(40):
        phrase = [f"alpha{index}", f"beta{index}", f"gamma{index}"]
        start = clock
        for token in phrase:
            words.append(Word(clock, clock + 0.4, token, 0.95))
            clock += 0.5
        # Three cues sit far from their audio; the rest are correct.
        drift = 5.0 if index in (7, 19, 31) else 0.0
        cues.append(Cue(start + drift, clock - 0.1 + drift, " ".join(phrase)))
        clock += 8.0
    result = audit(cues, words, clock)
    assert result["start_fit"]["residual_median_seconds"] < 0.5
    assert result["start_fit"]["residual_p95_seconds"] > 2.5
    assert result["decision"] == "pass"


def test_a_correct_shift_counts_as_improvement_despite_unmovable_outliers():
    """Mismatched anchors stay wrong after a shift; they must not mask a real repair."""
    words, cues, clock, shifted = [], [], 0.0, []
    for index in range(40):
        phrase = [f"alpha{index}", f"beta{index}", f"gamma{index}"]
        start = clock
        for token in phrase:
            words.append(Word(clock, clock + 0.4, token, 0.95))
            clock += 0.5
        # The whole subtitle is 1.4 s late; three cues are simply mismatched.
        bad = 5.0 if index in (7, 19, 31) else 0.0
        cues.append(Cue(start + 1.4 + bad, clock - 0.1 + 1.4 + bad, " ".join(phrase)))
        shifted.append(Cue(start + bad, clock - 0.1 + bad, " ".join(phrase)))
        clock += 8.0
    before = audit(cues, words, clock)
    after = audit(shifted, words, clock)
    assert before["decision"] == "repair"
    assert after["decision"] == "pass"
    assert improved(before, after), "removing a real 1.4 s lag is an improvement"


def unrelated(word_count=400, probability=0.95, duration=600.0, cue_count=60):
    """Recognized dialogue and an authored subtitle that share no vocabulary.

    This is the shape of a subtitle downloaded for the wrong episode: the audio is
    heard perfectly well, and almost none of what was heard appears in the text.
    """
    step = duration / word_count
    words = [
        Word(i * step, i * step + step * 0.6, f"alpha{i}", probability) for i in range(word_count)
    ]
    cues = [
        Cue(i * duration / cue_count, i * duration / cue_count + 2.0, f"bravo{i} charlie{i} delta{i}")
        for i in range(cue_count)
    ]
    return cues, words, duration


def test_a_subtitle_for_different_content_is_named_a_mismatch():
    cues, words, duration = unrelated()
    result = audit(cues, words, duration)
    assert result["decision"] == "mismatched"
    assert result["matched_token_ratio"] == 0


def test_a_mismatch_says_what_it_measured_rather_than_a_label():
    cues, words, duration = unrelated()
    result = audit(cues, words, duration)
    assert "400" in result["reason"] and "0.0%" in result["reason"]
    assert [check["passed"] for check in result["mismatch_checks"]] == [True, True, True]


def test_audio_the_recognizer_could_not_hear_is_inconclusive_not_a_mismatch():
    # Every word is a guess. Nothing matching it proves nothing about the subtitle,
    # so this must never license replacing an authored subtitle with a generated one.
    cues, words, duration = unrelated(probability=0.2)
    result = audit(cues, words, duration)
    assert result["decision"] == "inconclusive"


def test_too_little_recognized_speech_is_inconclusive_not_a_mismatch():
    cues, words, duration = unrelated(word_count=40)
    assert audit(cues, words, duration)["decision"] == "inconclusive"


def test_a_partial_text_match_is_inconclusive_not_a_mismatch():
    # A subtitle that agrees on a third of its text is a hard case for a human, not a
    # proven wrong episode. Only a floored match may be called one.
    cues, words = fixture()
    cues.append(Cue(20.0, 22.0, "zulu yankee xray whisky victor uniform"))
    result = audit(cues + [Cue(30.0, 32.0, "tango sierra romeo")], words, 40)
    assert result["decision"] != "mismatched"


def test_a_mismatch_publishes_no_timing_measurements_it_did_not_take():
    cues, words, duration = unrelated()
    result = audit(cues, words, duration)
    assert result["start_fit"] is None and result["start_p95_seconds"] is None


def cut_fixture(cue_count=60, duration=1800.0, missing=()):
    """A subtitle written for a shorter cut of the recording it is paired with.

    ``missing`` gives (subtitle_second, scene_length) pairs: scenes the recording holds
    and the subtitle has no lines for. The subtitle's own timeline stays ordered and
    evenly paced -- it is a valid file -- while the speech it describes sits further and
    further later in the video. With no missing scenes this is simply a correct subtitle.
    """
    words, cues = [], []
    absent = sum(length for _, length in missing)
    for i in range(cue_count):
        text = f"line{i} alpha{i} bravo{i}"
        written = 10.0 + i * (duration - absent - 20.0) / cue_count
        lag = sum(length for at, length in missing if written > at)
        moment = written + lag
        for token in text.split():
            words.append(Word(moment, moment + 0.4, token, 0.95))
            moment += 0.5
        cues.append(Cue(written, written + (moment - written - lag), text))
    return cues, words, duration


CASINO_NIGHT = (
    (60.0, 5.3), (90.0, 33.6), (160.0, 60.4), (180.0, 90.2), (360.0, 21.0),
    (440.0, 27.5), (460.0, 10.0), (530.0, 47.4), (880.0, 40.6),
)


def test_a_subtitle_for_a_shorter_cut_is_named_as_one():
    cues, words, duration = cut_fixture(missing=CASINO_NIGHT)
    result = audit(cues, words, duration)
    assert result["decision"] == "different_cut"
    assert result["matched_token_ratio"] == 1.0, "the text is this episode; only the cut differs"


def test_a_different_cut_says_how_far_apart_the_two_run():
    cues, words, duration = cut_fixture(missing=CASINO_NIGHT)
    result = audit(cues, words, duration)
    assert "336" in result["reason"]
    assert [check["passed"] for check in result["cut_checks"]] == [True, True, True]


def test_a_plain_offset_is_a_repair_not_a_different_cut():
    """One gap before everything shifts every cue equally; that is what repair is for."""
    cues, words, duration = cut_fixture(missing=((5.0, 30.0),))
    assert audit(cues, words, duration)["decision"] == "repair"


def test_scattered_disagreement_is_not_a_different_cut():
    """Missing scenes only ever push later cues further behind. Noise goes both ways."""
    cues, words, duration = cut_fixture(missing=CASINO_NIGHT)
    for index, cue in enumerate(cues):
        nudge = 90.0 if index % 2 else -90.0
        cue.start += nudge
        cue.end += nudge
    assert audit(cues, words, duration)["decision"] != "different_cut"


def test_one_overlapping_line_does_not_veto_a_correctly_timed_subtitle():
    """Two people talking over each other is authoring, not a timing fault -- and a pass
    publishes nothing, so a nit in a file being left alone cannot change the answer."""
    cues, words, duration = cut_fixture()
    cues[10].end = cues[11].start + 0.5
    result = audit(cues, words, duration)
    assert result["decision"] == "pass"
    assert any("overlapping" in issue for issue in result["structural_issues"])


def test_a_trailing_advertisement_does_not_veto_a_correctly_timed_subtitle():
    cues, words, duration = cut_fixture()
    cues.append(Cue(duration - 2.0, duration + 0.6, "Download subtitles from example dot invalid"))
    result = audit(cues, words, duration)
    assert result["decision"] == "pass"
    assert any("ends after the video" in issue for issue in result["structural_issues"])


def test_structural_damage_across_the_whole_file_still_blocks():
    """A handful of nits is authoring; a fifth of the file is a broken subtitle."""
    cues, words, duration = cut_fixture()
    for cue in cues[::3]:
        cue.end = duration + 5.0
    assert audit(cues, words, duration)["decision"] != "pass"


def test_an_impossible_cue_still_blocks():
    """Zero-length and reversed cues make the measurement itself untrustworthy."""
    cues, words, duration = cut_fixture()
    cues[7].end = cues[7].start
    assert audit(cues, words, duration)["decision"] != "pass"
