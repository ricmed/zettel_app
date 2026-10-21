"""Property: an upsert never changes a character outside the block it targets (#321).

Notes are generated as free text interleaved with well-formed managed blocks;
the malformed variants add one stray marker of the target block. Free text and
block bodies never contain ``<``, so they cannot spell a marker by accident.
"""

from __future__ import annotations

import re

from hypothesis import given, settings
from hypothesis import strategies as st
from zettel.vault import MalformedManagedBlock, malformed_managed_blocks, upsert_managed_block

NAMES = ("auto-backlinks", "auto-connections", "auto-moc-backrefs")
MARKER_RE = re.compile(r"<!-- zettel:[\w-]+:(?:start|end) -->")
BLOCK_RE = re.compile(r"<!-- zettel:([\w-]+):start -->.*?<!-- zettel:\1:end -->", re.DOTALL)
SETTINGS = settings(max_examples=1000, deadline=None)

text = st.text(alphabet=st.characters(blacklist_characters="<", codec="utf-8"), max_size=40)


def _block(name: str, inner: str) -> str:
    return f"<!-- zettel:{name}:start -->\n{inner}\n<!-- zettel:{name}:end -->"


@st.composite
def notes(draw) -> str:
    """Free text with each of a few block names present at most once."""
    names = draw(st.lists(st.sampled_from(NAMES), unique=True))
    parts = [draw(text)]
    for name in names:
        parts += [_block(name, draw(text)), draw(text)]
    return "".join(parts)


@st.composite
def broken_notes(draw) -> tuple[str, str]:
    """(note, block name) where that block has one stray start or end marker."""
    note = draw(notes())
    name = draw(st.sampled_from(NAMES))
    marker = f"<!-- zettel:{name}:{draw(st.sampled_from(['start', 'end']))} -->"
    inside_marker = {i for m in MARKER_RE.finditer(note) for i in range(m.start() + 1, m.end())}
    at = draw(st.sampled_from([i for i in range(len(note) + 1) if i not in inside_marker]))
    return note[:at] + marker + note[at:], name


def _outside_blocks(content: str) -> str:
    return BLOCK_RE.sub("", content)


@SETTINGS
@given(notes(), st.sampled_from(NAMES), text)
def test_text_outside_the_blocks_survives_an_upsert(note, name, inner):
    result = upsert_managed_block(note, name, inner)
    if f"zettel:{name}:start" in note:
        assert _outside_blocks(result) == _outside_blocks(note)
    else:
        assert result.startswith(note)
        assert result[len(note) :].strip() == _block(name, inner)


@SETTINGS
@given(notes(), st.sampled_from(NAMES), text)
def test_upsert_is_idempotent(note, name, inner):
    once = upsert_managed_block(note, name, inner)
    assert upsert_managed_block(once, name, inner) == once


@SETTINGS
@given(notes())
def test_a_generated_note_has_no_malformed_block(note):
    assert malformed_managed_blocks(note) == []


@SETTINGS
@given(broken_notes(), text)
def test_a_stray_marker_is_refused_and_reported(broken, inner):
    note, name = broken
    assert name in malformed_managed_blocks(note)
    try:
        upsert_managed_block(note, name, inner)
    except MalformedManagedBlock as e:
        assert e.block_name == name
    else:
        raise AssertionError("upsert wrote a note whose block markers are malformed")
