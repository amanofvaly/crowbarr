"""The preview reports what a new policy would decide and changes nothing it reads."""

import hashlib
import json
import sqlite3

import pytest
from test_section_coverage import film, without

from crowbarr import preview
from crowbarr.subtitles import Word, render_srt


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("audit_decision", "flagged", "proposed"),
    [
        ("pass", False, "pass"),
        ("pass", True, "damaged"),
        ("inconclusive", True, "damaged"),
        ("inconclusive", False, "inconclusive"),
        ("repair", False, "repair_held"),
        ("repair", True, "damaged"),
        ("mismatched", True, "mismatched"),
        ("different_cut", True, "different_cut"),
    ],
)
def test_proposed_verdict(audit_decision, flagged, proposed):
    assert preview.proposed_verdict(audit_decision, flagged) == proposed


@pytest.fixture
def library(tmp_path):
    """A small installation: one good job, one with a quarter deleted, one language stop."""
    cues, words = film(120)
    good = tmp_path / "good.srt"
    good.write_text(render_srt(cues), encoding="utf-8")
    cut = tmp_path / "cut.srt"
    cut.write_text(render_srt(without(cues, 120, 0.375, 0.625)), encoding="utf-8")
    db = tmp_path / "crowbarr.db"
    connection = sqlite3.connect(db)
    connection.execute("create table jobs (id integer, media text, state text, error text, report text)")
    for job, media, state, source in (
        (1, "good.mkv", "review", good),
        (2, "cut.mkv", "unchanged", cut),
    ):
        report = {
            "selected_source": str(source),
            "selected_source_kind": "external",
            "matched_token_ratio": 0.6,
            "audit": {"before": {"decision": "inconclusive"}},
            "audio_selection": {"stream": 1},
        }
        connection.execute(
            "insert into jobs values (?,?,?,?,?)", (job, str(tmp_path / media), state, None, json.dumps(report))
        )
    connection.execute(
        "insert into jobs values (3, ?, 'review', 'Audio language is ja, not en', null)",
        (str(tmp_path / "ja.mkv"),),
    )
    connection.commit()
    connection.close()
    return tmp_path, db, good, cut, words


def fake_listen(words):
    def listen(media, stream, settings, models, work):
        return words, [], 0.0, 120 * 60.0

    return listen


def test_preview_reports_each_job_without_touching_the_library(library):
    root, db, good, cut, words = library
    before = (digest(db), digest(good), digest(cut))
    out = root / "preview"

    results = preview.run(db, out, listen=fake_listen(words), settings=None, models=root)

    assert (digest(db), digest(good), digest(cut)) == before
    by_job = {result["id"]: result for result in results}
    assert by_job[1]["proposed"] == "pass"
    assert by_job[2]["proposed"] == "damaged"
    assert by_job[2]["section"]["flagged"] is True
    assert by_job[3]["proposed"] == "language_stop"
    assert json.loads((out / "preview.json").read_text())[0]["id"] == 1


def test_preview_keeps_finished_transcripts_so_it_can_resume(library):
    root, db, _, _, words = library
    out = root / "preview"
    preview.run(db, out, listen=fake_listen(words), settings=None, models=root)

    def must_not_listen(*args):
        raise AssertionError("a saved transcript should be used")

    results = preview.run(db, out, listen=must_not_listen, settings=None, models=root)
    assert {result["id"]: result["proposed"] for result in results}[1] == "pass"


def test_a_job_that_cannot_be_heard_is_recorded_and_the_run_continues(library):
    root, db, _, _, words = library

    def listen(media, stream, settings, models, work):
        if "good" in str(media):
            raise RuntimeError("ffmpeg failed")
        return words, [], 0.0, 120 * 60.0

    results = preview.run(db, root / "preview", listen=listen, settings=None, models=root)
    by_job = {result["id"]: result for result in results}
    assert by_job[1]["proposed"] == "error"
    assert "ffmpeg failed" in by_job[1]["error"]
    assert by_job[2]["proposed"] == "damaged"


def test_saved_words_keep_their_confidence(library):
    root, db, _, _, _ = library
    words = [Word(1.0, 1.5, "hello", 0.42)]
    out = root / "preview"
    preview.run(db, out, listen=fake_listen(words), settings=None, models=root)
    assert json.loads((out / "words" / "1.json").read_text())["words"][0]["probability"] == 0.42


def test_embedded_subtitle_is_read_from_the_recording(library):
    root, db, _, _, words = library
    cues, _ = film(120)
    connection = sqlite3.connect(db)
    connection.execute(
        "insert into jobs values (4, ?, 'review', null, ?)",
        (
            str(root / "embedded.mkv"),
            json.dumps({"selected_source_kind": "embedded", "audio_selection": {"stream": 1}}),
        ),
    )
    connection.commit()
    connection.close()

    def embedded(media, settings, work):
        return render_srt(cues)

    results = preview.run(
        db, root / "preview", listen=fake_listen(words), settings=None, models=root, embedded=embedded
    )
    assert {result["id"]: result["proposed"] for result in results}[4] == "pass"
