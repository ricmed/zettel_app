"""CommonMark fenced-block scanning.

A leaf module on purpose: pure text in, spans out, no config, no SQLite, no
Chroma. `harvester.chunking` uses it to keep a fence atomic while splitting
(ADR-014), and anything that needs to reason about where the code blocks are can
import it without paying for chromadb — which is what `harvester/chunking.py`,
its previous home, drags in through `zettel.index`.
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
