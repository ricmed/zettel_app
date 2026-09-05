"""CommonMark fenced-block scanning.

A leaf module on purpose: pure text in, spans out, no config, no SQLite, no
Chroma. Two subsystems need this fact about a document and they must not each
grow their own scanner — `harvester.chunking` uses it to keep a fence atomic
while splitting (ADR-014), and `extractor`/`preflight` use it to recognise a
chunk that is mostly code before spending an LLM call on it (issue #154).

It used to live in `harvester/chunking.py`, which reaches `zettel.index` and so
loads chromadb. Importing that from `preflight` — documented as pure functions
that read only SQLite and config — cost 1.6s and 651 extra modules for a
character count.
"""

from __future__ import annotations

import re

# Opening/closing fence line: up to 3 spaces of indent, 3+ backticks or tildes,
# optional info string. Indented code, tables and HTML are out of scope.
_FENCE_LINE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def iter_fenced_spans(text: str) -> list[tuple[int, int]]:
    """Return character spans of CommonMark fenced code blocks.

    A fence closes only with the same marker family (backtick never closes tilde),
    a marker at least as long as the opening one and no info string. An unclosed
    fence spans to EOF. Spans are returned in order and never overlap.
    """
    spans: list[tuple[int, int]] = []
    open_char = ""
    open_len = 0
    start = 0
    pos = 0

    for line in (text or "").splitlines(keepends=True):
        line_start = pos
        pos += len(line)
        m = _FENCE_LINE_RE.match(line.rstrip("\r\n"))
        if not m:
            continue
        marker, info = m.group(1), m.group(2)

        if open_char:
            # Closing fence: same family, at least as long, no info string.
            if marker[0] == open_char and len(marker) >= open_len and not info.strip():
                spans.append((start, pos))
                open_char = ""
            continue

        # Backtick fences cannot carry a backtick in the info string.
        if marker[0] == "`" and "`" in info:
            continue
        open_char = marker[0]
        open_len = len(marker)
        start = line_start

    if open_char:
        spans.append((start, len(text or "")))
    return spans


def offset_is_fenced(offset: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= offset < end for start, end in spans)


def fenced_char_ratio(text: str) -> float:
    """Fraction of `text` that sits inside fenced blocks, in [0, 1].

    The signal behind the pre-LLM gate: a chunk that is mostly a listing is read
    by the extract model as an illustration rather than a concept, and rejected.
    Empty text has no code, so it scores 0.0 and is never gated on this account.
    """
    if not text:
        return 0.0
    fenced = sum(end - start for start, end in iter_fenced_spans(text))
    return fenced / len(text)
