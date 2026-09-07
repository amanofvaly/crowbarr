import pytest

from crowbarr.subtitles import (
    Cue,
    Word,
    alignment_text,
    match_passages,
    parse_srt,
    render_srt,
    tokens,
    validate_cues,
)


def words(text, start=10):
    return [Word(start + i * 0.4, start + i * 0.4 + 0.35, word) for i, word in enumerate(text.split())]


def test_srt_round_trip_and_bom():
    cues = [Cue(1.123, 4.456, "Hello\nworld."), Cue(100, 105, "<i>Another line.</i>")]
    assert parse_srt("\ufeff" + render_srt(cues).replace("\n", "\r\n")) == cues


@pytest.mark.parametrize(
    "text",
    ["garbage", "1\n00:00:03,000 --> 00:00:01,000\nWrong", "1\n00:90:03,000 --> 00:91:01,000\nWrong", ""],
)
def test_invalid_srt_is_rejected(text):
    with pytest.raises(ValueError):
        parse_srt(text)


def test_normalization_keeps_words_and_ignores_labels():
    assert tokens("<i>JOHN: [whispering] We’re here.</i>") == ["we", "re", "here"]


def test_completely_wrong_timestamps_are_ignored():
    audio = words("We have finally reached the old station")
    passages, report = match_passages([Cue(9000, 9005, "We have finally reached the old station")], audio)
    assert len(passages) == 1
    assert passages[0].authored
    assert passages[0].start == 10
    assert report["matched_token_ratio"] == 1
    assert report["generated_word_ratio"] == 0


def test_inserted_audio_becomes_generated_passage():
    audio = words("We have reached home This is new dialogue Please open the door")
    original = [Cue(0, 2, "We have reached home"), Cue(2, 4, "Please open the door")]
    passages, report = match_passages(original, audio)
    assert report["preserved_cues"] == 2
    assert any(not p.authored and "new dialogue" in p.text for p in passages)


def test_deleted_scene_is_not_forced_onto_audio():
    original = [Cue(0, 5, "This completely missing scene cannot be aligned")]
    passages, report = match_passages(original, words("Please open the front door"))
    assert report["preserved_cues"] == 0
    assert report["matched_token_ratio"] == 0
    assert all(not p.authored for p in passages)


def test_ambiguous_repeated_phrase_does_not_become_anchor():
    original = [Cue(0, 1, "I know you")]
    _, report = match_passages(original, words("I know you yes I know you"))
    assert report["preserved_cues"] == 0


def test_missing_subtitle_generates_readable_chunks():
    passages, report = match_passages([], words("This is a new sentence. Here is another one."))
    assert len(passages) == 2
    assert report["generated_word_ratio"] == 1
    assert not any(p.authored for p in passages)


def test_long_silence_is_not_bridged():
    passages, _ = match_passages([], [Word(1, 2, "Hello"), Word(30, 31, "again")])
    assert len(passages) == 2


@pytest.mark.parametrize(
    "cue",
    [
        Cue(-1, 2, "bad"),
        Cue(2, 1, "bad"),
        Cue(1, 99, "bad"),
        Cue(float("nan"), 5, "bad"),
        Cue(1, 1.01, "far too many words"),
    ],
)
def test_invalid_timing_fails_quality_check(cue):
    assert validate_cues([cue], 20)


def test_overlaps_are_reported():
    assert validate_cues([Cue(1, 3, "hello"), Cue(2, 4, "world")], 20)


def test_normal_timings_pass():
    assert validate_cues([Cue(1, 3, "hello"), Cue(3, 5, "world")], 20) == []


def test_generated_numbers_have_spoken_form_but_keep_display_form():
    passages, _ = match_passages([], words("We arrive in 5 minutes"))
    assert passages[0].text == "We arrive in five minutes"
    assert passages[0].display == "We arrive in 5 minutes"


def test_integer_normalization_is_bounded_and_preserves_contextual_numbers():
    assert alignment_text("Room 42 has 105 seats") == "Room forty two has one hundred five seats"
    assert alignment_text("$13.60 and 3.14") == "$13.60 and 3.14"
