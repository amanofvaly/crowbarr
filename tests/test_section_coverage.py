"""The bad-section check, on the damage the 65% text check was shown to miss."""

import itertools
import string

from crowbarr.audit import section_coverage
from crowbarr.subtitles import Cue, Word

LETTERS = string.ascii_lowercase


def vocabulary(seed: str):
    """Endless distinct spoken words, so every trigram in a fixture is unique."""
    for first, second, third in itertools.product(LETTERS, repeat=3):
        yield f"{seed}{first}{second}{third}"


def film(minutes: int, seed: str = "q", per_minute: int = 60) -> tuple[list[Cue], list[Word]]:
    """A recording with steady dialogue and the subtitle written for it."""
    source = vocabulary(seed)
    words, cues = [], []
    for second in range(0, minutes * 60, 4):
        spoken = [next(source) for _ in range(per_minute // 15)]
        for index, text in enumerate(spoken):
            start = second + index * 0.8
            words.append(Word(start, start + 0.7, text, 0.95))
        cues.append(Cue(second, second + 3.5, " ".join(spoken)))
    return cues, words


def without(cues: list[Cue], minutes: int, first: float, last: float) -> list[Cue]:
    return [cue for cue in cues if not first * minutes * 60 <= cue.start < last * minutes * 60]


def test_subtitle_written_for_the_recording_is_not_flagged():
    cues, words = film(120)
    assert section_coverage(cues, words, 120 * 60)["flagged"] is False


def test_subtitle_from_another_film_is_flagged():
    _, words = film(120)
    other, _ = film(120, seed="z")
    assert section_coverage(other, words, 120 * 60)["flagged"] is True


def test_quarter_of_a_film_deleted_is_flagged():
    cues, words = film(120)
    report = section_coverage(without(cues, 120, 0.375, 0.625), words, 120 * 60)
    assert report["flagged"] is True
    assert report["longest_bad_minutes"] >= 15


def test_quarter_of_a_short_episode_deleted_is_flagged():
    """Dark Matter S02E01, E03 and E06 passed a fixed 15 minute limit with this damage."""
    cues, words = film(50)
    report = section_coverage(without(cues, 50, 0.375, 0.625), words, 50 * 60)
    assert report["flagged"] is True
    assert report["longest_bad_minutes"] < 15


def test_one_bad_five_minutes_does_not_veto_a_film():
    cues, words = film(120)
    report = section_coverage(without(cues, 120, 0.5, 0.55), words, 120 * 60)
    assert report["flagged"] is False
    assert report["longest_bad_minutes"] <= 5


def test_words_the_recognizer_was_unsure_of_are_not_evidence():
    cues, words = film(120)
    damaged = without(cues, 120, 0.375, 0.625)
    unsure = [
        Word(w.start, w.end, w.text, 0.3) if 0.375 * 7200 <= w.start < 0.625 * 7200 else w for w in words
    ]
    assert section_coverage(damaged, unsure, 120 * 60)["flagged"] is False


def test_recording_with_almost_no_speech_cannot_be_judged():
    """Could not tell is not a negative finding, so it must not flag."""
    cues, words = film(120)
    report = section_coverage(cues, words[:10], 120 * 60)
    assert report["flagged"] is False
    assert report["measured"] is False


def test_measurements_are_published_with_their_limits():
    cues, words = film(50)
    report = section_coverage(without(cues, 50, 0.375, 0.625), words, 50 * 60)
    assert [check["name"] for check in report["section_checks"]] == [
        "Clearly heard speech found in the subtitle",
        "Longest run with the subtitle missing",
    ]
    assert report["section_checks"][1]["passed"] is False
    assert report["section_checks"][1]["limit"] == "under 7.5 min"
    assert report["bad_sections"]
