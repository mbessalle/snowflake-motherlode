"""A document as an outline of sections, so a reader can go to the right part instead of reading all of it.

Retrieval in this project is hierarchical, not embedding-based:

    level 0  the deal table        which announcement is relevant? (structured, queried with SQL)
    level 1  the document outline  which section of it? (headings, first sentences, sizes)
    level 2  one section           the text itself

The agent walks down the levels with tools, the way a person uses a table of contents. Nothing is embedded
and nothing is ranked by vector similarity: every step is an exact lookup that can be inspected.

This module builds level 1 and 2 with code only. A press release has little markup, so a heading is a short
line without a full stop, and long runs of text are cut at paragraph boundaries. When the Cortex functions
are available, the first-sentence summaries can be replaced by model-written ones; the structure stays.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TARGET_CHARS = 1500      # a section is closed once it reaches about this size
MIN_CHARS = 400          # a section smaller than this is joined to its neighbour
HEADING_MAX_CHARS = 90
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9“\"(])")


@dataclass(frozen=True)
class Section:
    number: int
    heading: str
    text: str

    @property
    def first_sentence(self) -> str:
        return _SENTENCE_END.split(" ".join(self.text.split()), maxsplit=1)[0][:300]


def is_heading(line: str) -> bool:
    """A short line that does not end like a sentence and is not a table row or a list item."""
    line = line.strip()
    if not line or len(line) > HEADING_MAX_CHARS or line[-1] in ".,;:" or "|" in line or line[0] in "-•*":
        return False
    words = line.split()
    return len(words) <= 12 and any(c.isalpha() for c in line) and not line[0].islower()


def _label(text: str) -> str:
    """A heading for a section that has none: its first words."""
    words = text.split()
    return " ".join(words[:8]) + ("…" if len(words) > 8 else "")


def _pieces(line: str) -> list[str]:
    """A line as it is, or, when it is one very long paragraph, cut at sentence ends into runs of about TARGET_CHARS."""
    if len(line) <= TARGET_CHARS:
        return [line]
    out, current = [], ""
    for sentence in _SENTENCE_END.split(line):
        if current and len(current) + len(sentence) > TARGET_CHARS:
            out.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    return out + ([current] if current else [])


def sections(text: str) -> list[Section]:
    """Split a document into numbered sections. Every non-empty line ends up in exactly one of them.

    First pass: a heading starts a section and a section closes at about TARGET_CHARS. Second pass: a
    section that is too small to be worth a separate read (a lone heading, a contact line) is joined to
    the next one, so the outline stays short enough to be read in one go."""
    raw: list[tuple[str | None, str]] = []      # (heading or None, body)
    heading, body = None, []

    def close() -> None:
        nonlocal heading, body
        if body or heading:
            raw.append((heading, "\n".join(body)))
        heading, body = None, []

    for line in (candidate.strip() for candidate in text.splitlines()):
        if not line:
            continue
        if is_heading(line):
            close()
            heading = line
            continue
        for piece in _pieces(line):
            body.append(piece)
            if sum(len(part) for part in body) >= TARGET_CHARS:
                close()
    close()

    merged: list[tuple[str | None, str]] = []
    for head, content in raw:
        if merged and len(merged[-1][1]) < MIN_CHARS:
            previous_head, previous = merged[-1]
            joined = "\n".join(part for part in (previous, head, content) if part)
            merged[-1] = (previous_head, joined)
        else:
            merged.append((head, content))
    if len(merged) > 1 and len(merged[-1][1]) < MIN_CHARS:      # a small tail joins the section before it
        head, content = merged.pop()
        previous_head, previous = merged[-1]
        merged[-1] = (previous_head, "\n".join(part for part in (previous, head, content) if part))
    return [Section(i, head or _label(content), content or (head or "")) for i, (head, content) in enumerate(merged, 1)]


def outline(parts: list[Section]) -> str:
    """Level 1 as text: one line per section with its number, heading, size and first sentence."""
    return "\n".join(f"{s.number}. {s.heading} ({len(s.text)} chars): {s.first_sentence}" for s in parts)
