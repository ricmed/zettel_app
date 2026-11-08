"""The Mesa body grammar (#383): lossless round trip and edits that keep every other line."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from zettel import project_file as pf

LINES = [
    "",
    "## Seção A",
    "## Seção B",
    "# Título",
    "### Sub",
    "- [[ZTL - 01AAAAAAAAAAAAAAAAAAAAAAAA - a]]",
    "- [[ZTL - 01BBBBBBBBBBBBBBBBBBBBBBBB|alias]]",
    "- [[MOC - 01CCCCCCCCCCCCCCCCCCCCCCCC - m]]",
    "- item comum",
    "  anotação",
    "\tcom tab",
    "Texto livre do autor.",
    "```",
    "~~~python",
    "~~~",
    "$$x^2$$",
]


@given(st.lists(st.sampled_from(LINES), max_size=40))
def test_render_of_parse_is_the_identity(lines):
    text = "\n".join(lines)
    assert pf.render_body(pf.parse_body(text)) == text


@given(st.lists(st.sampled_from(LINES), max_size=30))
def test_adding_cards_keeps_every_line_in_order(lines):
    text = "\n".join(lines)
    body = pf.parse_body(text)
    pf.add_to_inbox(body, [("01DDDDDDDDDDDDDDDDDDDDDDDD", "[[ZTL - 01DDDDDDDDDDDDDDDDDDDDDDDD]]")])
    after = pf.render_body(body).split("\n")
    remaining = iter(after)
    assert all(any(line == other for other in remaining) for line in lines)
    assert "- [[ZTL - 01DDDDDDDDDDDDDDDDDDDDDDDD]]" in after


SAMPLE = """Intro do autor.

## Modelos

Ponte escrita.
- [[ZTL - 01AAAAAAAAAAAAAAAAAAAAAAAA - a]]
  contraponto da seção 3
- [[ZTL - 01BBBBBBBBBBBBBBBBBBBBBBBB]]

```
## dentro da fence
- [[ZTL - 01CCCCCCCCCCCCCCCCCCCCCCCC]]
```

## Resultados
- [[ZTL - 01DDDDDDDDDDDDDDDDDDDDDDDD - d|D]]
"""


def test_sections_cards_annotations_and_fences():
    body = pf.parse_body(SAMPLE)
    assert [s.title for s in body.sections] == ["", "Modelos", "Resultados"]
    assert body.note_ids() == [
        "01AAAAAAAAAAAAAAAAAAAAAAAA",
        "01BBBBBBBBBBBBBBBBBBBBBBBB",
        "01DDDDDDDDDDDDDDDDDDDDDDDD",
    ]
    card = body.sections[1].cards[0]
    assert card.annotation_text == "contraponto da seção 3"


def test_add_skips_notes_already_on_the_desk_and_creates_the_inbox():
    body = pf.parse_body(SAMPLE)
    added = pf.add_to_inbox(
        body,
        [
            ("01AAAAAAAAAAAAAAAAAAAAAAAA", "[[ZTL - 01AAAAAAAAAAAAAAAAAAAAAAAA - a]]"),
            ("01EEEEEEEEEEEEEEEEEEEEEEEE", "[[ZTL - 01EEEEEEEEEEEEEEEEEEEEEEEE - e]]"),
        ],
    )
    assert added == 1
    inbox = pf.find_section(body, pf.INBOX_SECTION)
    assert body.sections[inbox].cards[0].note_id == "01EEEEEEEEEEEEEEEEEEEEEEEE"
    assert pf.render_body(body).startswith(SAMPLE.rstrip("\n"))


def test_moves_cross_sections_and_keep_the_annotation():
    body = pf.parse_body(SAMPLE)
    # The first card of "Resultados" goes up: to the end of "Modelos".
    section, block = pf.move_block(body, 2, 0, "up")
    assert section == 1
    assert body.sections[1].blocks[block].note_id == "01DDDDDDDDDDDDDDDDDDDDDDDD"
    # A drop before the first card of "Modelos", annotation and all.
    first = body.sections[1].blocks.index(body.sections[1].cards[0])
    pf.place_block(body, 1, block, 1, first)
    assert body.sections[1].cards[0].note_id == "01DDDDDDDDDDDDDDDDDDDDDDDD"
    assert body.sections[1].cards[1].annotation_text == "contraponto da seção 3"
    rendered = pf.render_body(body)
    assert "## dentro da fence" in rendered and rendered.count("## Resultados") == 1


def test_text_blocks_annotations_and_sections_are_editable():
    body = pf.parse_body(SAMPLE)
    pf.set_annotation(body, 1, 2, "nova anotação\n\nsegunda linha")
    assert body.sections[1].blocks[2].annotation == ["  nova anotação", "  segunda linha"]
    index = pf.add_text(body, 2, "Fecho da seção.")
    assert body.sections[2].blocks[index].content == "Fecho da seção."
    pf.set_text(body, 2, index, "")
    assert all(b.content != "Fecho da seção." for b in body.sections[2].blocks if hasattr(b, "raw"))
    pf.rename_section(body, 2, "  Achados   finais ")
    assert body.sections[2].heading == "## Achados finais"
    assert pf.move_section(body, 2, "up") == 1
    assert [s.title for s in body.sections] == ["", "Achados finais", "Modelos"]
    pf.delete_section(body, 1)
    # Its card joined the section above: nothing is lost.
    assert "01DDDDDDDDDDDDDDDDDDDDDDDD" in body.note_ids()


def test_invalid_addresses_are_refused():
    body = pf.parse_body(SAMPLE)
    for edit in (
        lambda: pf.remove_block(body, 9, 0),
        lambda: pf.set_annotation(body, 1, 0, "x"),
        lambda: pf.delete_section(body, 0),
        lambda: pf.add_section(body, "   "),
    ):
        try:
            edit()
        except pf.ProjectEditError:
            continue
        raise AssertionError("edit accepted")
