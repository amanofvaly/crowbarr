from __future__ import annotations

import html
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher


@dataclass
class Cue:
    start: float
    end: float
    text: str


@dataclass
class Word:
    start: float
    end: float
    text: str
    probability: float = 1.0


@dataclass
class Passage:
    start: float
    end: float
    text: str
    display: str
    authored: bool
    source_index: int | None = None


TIMING = re.compile(
    r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{3})"
    # SubRip writes the original bitmap's on-screen box after the timestamps when a
    # subtitle was produced by OCR of a DVD or VobSub stream. Players ignore it, and
    # refusing it throws away an entire correct file over a coordinate. Accepted
    # explicitly rather than by loosening the match, so genuine junk is still junk.
    r"(?:\s+X1:\d+\s+X2:\d+\s+Y1:\d+\s+Y2:\d+)?\s*"
)


def english_integer(value: int) -> str:
    small = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
    tens = "zero ten twenty thirty forty fifty sixty seventy eighty ninety".split()
    if value < 20:
        return small[value]
    if value < 100:
        return tens[value // 10] + (" " + small[value % 10] if value % 10 else "")
    if value < 1000:
        return small[value // 100] + " hundred" + (" " + english_integer(value % 100) if value % 100 else "")
    return (
        english_integer(value // 1000)
        + " thousand"
        + (" " + english_integer(value % 1000) if value % 1000 else "")
    )


def alignment_text(text: str) -> str:
    """Expand ordinary English integers for the phoneme model; retain display text separately.

    Dates, currency and decimal pronunciation need context, so they remain conservative review cases.
    """

    def replace(match):
        value = match.group()
        if len(value) > 1 and value.startswith("0"):
            return " ".join(english_integer(int(digit)) for digit in value)
        return english_integer(int(value))

    return re.sub(r"(?<![\w.,$£€])\d{1,6}(?![\w.,]|\d)", replace, text)


def spoken(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]*>", "", text))
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", text)
    text = re.sub(r"(?m)^\s*[A-Z][A-Z\s]{1,30}:\s*", "", text)
    return " ".join(text.replace("♪", "").split()).strip("- ")


def tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", spoken(text)).casefold().replace("’", "'"))


def parse_srt(text: str) -> list[Cue]:
    cues = []
    impossible = 0
    blocks = re.split(r"\n\s*\n", text.lstrip("\ufeff").replace("\r\n", "\n").strip())
    for block in blocks:
        lines = block.splitlines()
        timing_index = next((i for i, line in enumerate(lines[:2]) if "-->" in line), None)
        if timing_index is None:
            raise ValueError("Invalid SRT block: no timing line")
        match = TIMING.fullmatch(lines[timing_index].strip())
        if not match:
            raise ValueError("Invalid SRT timestamp")
        values = list(map(int, match.groups()))
        if any(values[i] >= 60 for i in (1, 2, 5, 6)):
            raise ValueError("Invalid SRT minutes or seconds")
        start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
        end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
        body = "\n".join(lines[timing_index + 1 :]).strip()
        if end <= start:
            # A cue ending before it starts displays nothing, exactly like the empty
            # block below: a duplicated line left with a zero length, or an uploader's
            # credit given nonsense times. One is an artefact, and refusing the file
            # over it discards every good cue around it and audits none of them.
            impossible += 1
            continue
        # Provider files can contain an empty timed block (e.g. a removed advert).
        # It displays nothing and must not invalidate the remaining dialogue.
        if not body:
            continue
        cues.append(Cue(start, end, body))
    # Timing broken throughout is a different matter: there is no timing to audit, and
    # measuring whatever survived would report a verdict about a file that has none.
    if impossible > max(3, len(blocks) * 0.05):
        raise ValueError(f"{impossible} of {len(blocks)} cues end before they start")
    if not cues:
        raise ValueError("Subtitle has no cues")
    return cues


def timestamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


def render_srt(cues: list[Cue]) -> str:
    return (
        "\n\n".join(
            f"{i}\n{timestamp(c.start)} --> {timestamp(c.end)}\n{c.text}" for i, c in enumerate(cues, 1)
        )
        + "\n"
    )


def match_passages(cues: list[Cue], words: list[Word], minimum: float = 0.75) -> tuple[list[Passage], dict]:
    """Match unique phrase anchors, then preserve supported authored cues and fill gaps from ASR.

    Old SRT timestamps are deliberately ignored. A monotone matcher cannot resolve reordered scenes;
    the quality report makes that a review case instead of disguising it with interpolated times.
    """
    source_tokens, spans = [], []
    for cue in cues:
        start = len(source_tokens)
        source_tokens.extend(tokens(cue.text))
        spans.append((start, len(source_tokens)))
    audio_tokens, owners = [], []
    for index, word in enumerate(words):
        for token in tokens(word.text):
            audio_tokens.append(token)
            owners.append(index)
    if len(source_tokens) > 60000 or len(audio_tokens) > 60000:
        raise ValueError("Transcript exceeds the 60,000-token matching limit")
    n = 3
    source_grams = Counter(tuple(source_tokens[i : i + n]) for i in range(len(source_tokens) - n + 1))
    audio_grams = Counter(tuple(audio_tokens[i : i + n]) for i in range(len(audio_tokens) - n + 1))
    matches = {}
    for block in SequenceMatcher(None, source_tokens, audio_tokens, autojunk=False).get_matching_blocks():
        if block.size < n:
            continue
        unique = any(
            source_grams[tuple(source_tokens[i : i + n])] == 1
            and audio_grams[tuple(source_tokens[i : i + n])] == 1
            for i in range(block.a, block.a + block.size - n + 1)
        )
        if unique:
            matches.update((block.a + j, owners[block.b + j]) for j in range(block.size))
    passages, used, preserved = [], set(), 0
    unsupported_cues = 0
    for source_index, (cue, (start, end)) in enumerate(zip(cues, spans, strict=True)):
        matched = [matches[i] for i in range(start, end) if i in matches]
        ratio = sum(i in matches for i in range(start, end)) / max(1, end - start)
        # Both boundaries need support; do not force missing leading/trailing words onto another scene.
        if (
            not matched
            or ratio < minimum
            or start not in matches
            or end - 1 not in matches
            or matched[-1] - matched[0] > (end - start) * 2
            or words[matched[-1]].end - words[matched[0]].start > 15
        ):
            unsupported_cues += 1
            continue
        first, last = matched[0], matched[-1]
        if any(i in used for i in range(first, last + 1)):
            unsupported_cues += 1
            continue
        passages.append(
            Passage(words[first].start, words[last].end, spoken(cue.text), cue.text, True, source_index)
        )
        used.update(range(first, last + 1))
        preserved += 1
    # Group replacement speech into readable chunks; never bridge a long silence.
    pending = []

    def flush():
        if pending:
            text = " ".join(w.text.strip() for w in pending)
            passages.append(Passage(pending[0].start, pending[-1].end, alignment_text(text), text, False))
            pending.clear()

    for index, word in enumerate(words):
        if index in used:
            flush()
            continue
        if pending and (
            word.start - pending[-1].end > 0.8
            or word.end - pending[0].start > 6
            or sum(len(w.text.strip()) + 1 for w in pending) + len(word.text) > 80
        ):
            flush()
        pending.append(word)
        if word.text.rstrip().endswith((".", "?", "!")):
            flush()
    flush()
    passages.sort(key=lambda p: p.start)
    report = {
        "source_cues": len(cues),
        "preserved_cues": preserved,
        "unsupported_cues": unsupported_cues,
        "matched_token_ratio": len(matches) / max(1, len(source_tokens)),
        "generated_word_ratio": (len(words) - len(used)) / max(1, len(words)),
        "speech_words": len(words),
    }
    return passages, report


def validate_cues(cues: list[Cue], duration: float) -> list[str]:
    issues = []
    if not cues:
        return ["No aligned dialogue was produced"]
    previous_end = 0.0
    for index, cue in enumerate(cues, 1):
        length = cue.end - cue.start
        if not all(math.isfinite(v) for v in (cue.start, cue.end)) or cue.start < 0 or length <= 0:
            issues.append(f"Cue {index}: invalid timing")
        elif cue.end > duration + 0.1:
            issues.append(f"Cue {index}: ends after the video")
        elif cue.start < previous_end - 0.08:
            issues.append(f"Cue {index}: overlapping dialogue needs review")
        elif length > 15 or len(spoken(cue.text)) / max(length, 0.1) > 35:
            issues.append(f"Cue {index}: unsuitable reading duration")
        previous_end = cue.end
    return issues
