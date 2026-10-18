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


# A Docling table row. Indented code and pipe characters inside a fence are not rows.
_TABLE_LINE_RE = re.compile(r"^[ \t]{0,3}\|")
_TABLE_CELL_PAD_RE = re.compile(r"[ \t]{2,}")


def iter_table_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of Markdown tables that sit outside fenced blocks.

    A table is two or more consecutive lines that start with ``|``. A blank
    line ends it. Pipe lines inside a fence are the fence's content.
    """
    source = text or ""
    fences = iter_fenced_spans(source)
    spans: list[tuple[int, int]] = []
    pos = 0
    run_start: int | None = None
    run_end = 0
    rows = 0
    for line in source.splitlines(keepends=True):
        line_start = pos
        pos += len(line)
        is_row = not offset_is_fenced(line_start, fences) and bool(_TABLE_LINE_RE.match(line))
        if is_row:
            if run_start is None:
                run_start = line_start
            run_end = pos
            rows += 1
            continue
        if run_start is not None and rows >= 2:
            spans.append((run_start, run_end))
        run_start = None
        rows = 0
    if run_start is not None and rows >= 2:
        spans.append((run_start, run_end))
    return spans


def collapse_table_padding(text: str) -> str:
    """Collapse runs of spaces inside Markdown table rows, outside fences.

    Docling pads cells so the columns line up. The characters are not content,
    and they consume ``chunk_size``. ``| a | b |`` stays a table.
    """
    source = text or ""
    if "|" not in source:
        return source
    fences = iter_fenced_spans(source)
    out: list[str] = []
    pos = 0
    for line in source.splitlines(keepends=True):
        line_start = pos
        pos += len(line)
        if offset_is_fenced(line_start, fences) or not _TABLE_LINE_RE.match(line):
            out.append(line)
            continue
        ending = ""
        body = line
        if body.endswith("\r\n"):
            body, ending = body[:-2], "\r\n"
        elif body.endswith("\n"):
            body, ending = body[:-1], "\n"
        out.append(_TABLE_CELL_PAD_RE.sub(" ", body) + ending)
    return "".join(out)


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
