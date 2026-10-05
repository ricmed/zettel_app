"""CommonMark fenced-block scanning.

A leaf module on purpose: pure text in, spans out, no config, no SQLite, no
Chroma. `harvester.chunking` uses it to keep a fence atomic while splitting
(ADR-014), and anything that needs to reason about where the code blocks are can
import it without paying for chromadb — which is what `harvester/chunking.py`,
its previous home, drags in through `zettel.index`. The extraction dump, the
manual LIT parser, the skill export and the extractor's anchor check read
headings and sections through it too, so no reader mistakes a `#` comment
inside a fence for structure.
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


def headings_outside_fences(pattern: re.Pattern[str], text: str) -> list[re.Match[str]]:
    """Matches of `pattern` whose offset does not fall inside a fenced block.

    The one rule every Markdown reader shares: a `#` line inside a fence is the
    fence's content (a Python comment, an illustrative template), never document
    structure.
    """
    spans = iter_fenced_spans(text)
    if not spans:
        return list(pattern.finditer(text))
    return [m for m in pattern.finditer(text) if not offset_is_fenced(m.start(), spans)]


_H2_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def h2_section(body: str, heading: str) -> str:
    """Text under ``## heading`` up to the next ``## `` outside a fence, stripped.

    Only an H2 closes the section (H1 and H3+ are part of it). Empty when the
    heading is absent.
    """
    body = body or ""
    matches = headings_outside_fences(_H2_RE, body)
    for i, m in enumerate(matches):
        if m.group(1) == heading:
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            return body[m.end() : end].strip()
    return ""
