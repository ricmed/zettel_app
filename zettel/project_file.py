"""The body of a writing project (Mesa, ADR-067): sections of cards and the author's text.

A project is a vault note (``50_Projetos/PRJ - <ULID> - <slug>.md``). Its body
is read line by line, and every line belongs to exactly one block, so
``render_body(parse_body(text)) == text`` for any text: a project edited by
hand in Obsidian never loses a character when the web rewrites it.

- A level-2 heading outside a fence opens a section.
- A card is a list item whose only content is a permanent-note link
  (``- [[ZTL - <id> - slug]]``, ``vault.ZTL_LINK_RE``); the indented,
  non-blank lines right under it are its annotation.
- Every other line is text, kept verbatim. Consecutive text lines form one
  block: the author's bridge between cards.

Blocks are addressed by ``(section, block)`` indexes. Section 0 is what comes
before the first heading (``heading is None``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from zettel.markdown_fences import iter_fenced_spans
from zettel.vault import ZTL_LINK_RE

_H2 = re.compile(r"^##[ \t]+(?P<title>.+?)[ \t#]*$")
_CARD = re.compile(r"^-[ \t]+(?P<link>\[\[[^\]]+\]\])[ \t]*$")
_INDENTED = re.compile(r"^[ \t]+\S")
ANNOTATION_INDENT = "  "
# Where cards added from another page land until the author places them.
INBOX_SECTION = "Para organizar"


@dataclass
class Card:
    note_id: str
    line: str
    annotation: list[str] = field(default_factory=list)

    @property
    def lines(self) -> list[str]:
        return [self.line, *self.annotation]

    @property
    def annotation_text(self) -> str:
        return "\n".join(line.strip() for line in self.annotation)


@dataclass
class Text:
    raw: list[str]

    @property
    def lines(self) -> list[str]:
        return self.raw

    @property
    def is_blank(self) -> bool:
        return not any(line.strip() for line in self.raw)

    @property
    def content(self) -> str:
        return "\n".join(self.raw).strip("\n")


Block = Card | Text


@dataclass
class Section:
    heading: str | None
    blocks: list[Block] = field(default_factory=list)

    @property
    def title(self) -> str:
        if self.heading is None:
            return ""
        match = _H2.match(self.heading)
        return match.group("title") if match else self.heading

    @property
    def cards(self) -> list[Card]:
        return [b for b in self.blocks if isinstance(b, Card)]


@dataclass
class ProjectBody:
    sections: list[Section]

    def note_ids(self) -> list[str]:
        return [card.note_id for section in self.sections for card in section.cards]


class ProjectEditError(ValueError):
    """An edit that names a section or block the body does not have."""


def _card(line: str) -> Card | None:
    match = _CARD.match(line)
    if not match:
        return None
    link = ZTL_LINK_RE.fullmatch(match.group("link"))
    return Card(note_id=link.group(1), line=line) if link else None


def _fenced_lines(text: str, lines: list[str]) -> set[int]:
    spans = iter_fenced_spans(text)
    if not spans:
        return set()
    fenced: set[int] = set()
    offset = 0
    for number, line in enumerate(lines):
        if any(start <= offset < end for start, end in spans):
            fenced.add(number)
        offset += len(line) + 1
    return fenced


def parse_body(text: str) -> ProjectBody:
    """Split a project body into sections and blocks; nothing is dropped."""
    lines = text.split("\n")
    fenced = _fenced_lines(text, lines)
    sections = [Section(heading=None)]
    # The card whose annotation the next indented line would continue.
    open_card: Card | None = None
    for number, line in enumerate(lines):
        blocks = sections[-1].blocks
        if number not in fenced:
            if open_card is not None and _INDENTED.match(line):
                open_card.annotation.append(line)
                continue
            if _H2.match(line):
                sections.append(Section(heading=line))
                open_card = None
                continue
            card = _card(line)
            if card:
                blocks.append(card)
                open_card = card
                continue
        open_card = None
        if blocks and isinstance(blocks[-1], Text):
            blocks[-1].raw.append(line)
        else:
            blocks.append(Text(raw=[line]))
    return ProjectBody(sections=sections)


def render_body(body: ProjectBody) -> str:
    lines: list[str] = []
    for section in body.sections:
        if section.heading is not None:
            lines.append(section.heading)
        for block in section.blocks:
            lines.extend(block.lines)
    return "\n".join(lines)


# ── Edits (each one keeps every line it does not touch) ────────────────


def _section(body: ProjectBody, index: int) -> Section:
    if not 0 <= index < len(body.sections):
        raise ProjectEditError(f"Seção {index} não existe.")
    return body.sections[index]


def _block(body: ProjectBody, section: int, block: int) -> Block:
    blocks = _section(body, section).blocks
    if not 0 <= block < len(blocks):
        raise ProjectEditError(f"Item {section}.{block} não existe.")
    return blocks[block]


def _spaced(lines: list[str]) -> list[str]:
    """``lines`` as a paragraph of its own: one blank line before and after."""
    return ["", *lines, ""]


def _insert_before_trailing_blanks(section: Section, blocks: list[Block]) -> None:
    position = len(section.blocks)
    while position and isinstance(section.blocks[position - 1], Text):
        if not section.blocks[position - 1].is_blank:
            break
        position -= 1
    section.blocks[position:position] = blocks


def card_line(link: str) -> str:
    return f"- {link}"


def find_section(body: ProjectBody, title: str) -> int | None:
    for index, section in enumerate(body.sections):
        if section.heading is not None and section.title == title:
            return index
    return None


def add_section(body: ProjectBody, title: str) -> int:
    clean = " ".join(title.split())
    if not clean:
        raise ProjectEditError("A seção precisa de um título.")
    last = body.sections[-1]
    if last.blocks and not (isinstance(last.blocks[-1], Text) and last.blocks[-1].raw[-1] == ""):
        last.blocks.append(Text(raw=[""]))
    body.sections.append(Section(heading=f"## {clean}", blocks=[Text(raw=[""])]))
    return len(body.sections) - 1


def add_cards(body: ProjectBody, section: int, links: list[tuple[str, str]]) -> int:
    """Append ``(note_id, link)`` cards to a section, skipping notes already on the desk."""
    present = set(body.note_ids())
    target = _section(body, section)
    new: list[Block] = []
    for note_id, link in links:
        if note_id in present:
            continue
        present.add(note_id)
        new.append(Card(note_id=note_id, line=card_line(link)))
    if new:
        last = target.blocks[-1] if target.blocks else None
        if isinstance(last, Text) and not last.is_blank:
            target.blocks.append(Text(raw=[""]))
        _insert_before_trailing_blanks(target, new)
    return len(new)


def add_to_inbox(body: ProjectBody, links: list[tuple[str, str]]) -> int:
    index = find_section(body, INBOX_SECTION)
    if index is None:
        index = add_section(body, INBOX_SECTION)
    return add_cards(body, index, links)


def _movable(section: Section) -> list[int]:
    return [i for i, b in enumerate(section.blocks) if not (isinstance(b, Text) and b.is_blank)]


def move_block(body: ProjectBody, section: int, block: int, direction: str) -> tuple[int, int]:
    """Swap a block with its neighbour (blank lines do not count); return its new address.

    Up from the first block of a section moves it to the end of the previous
    section; down from the last moves it to the start of the next one.
    """
    item = _block(body, section, block)
    blocks = body.sections[section].blocks
    order = _movable(body.sections[section])
    position = order.index(block)
    if direction == "up" and position > 0:
        other = order[position - 1]
        blocks[block], blocks[other] = blocks[other], item
        return section, other
    if direction == "down" and position < len(order) - 1:
        other = order[position + 1]
        blocks[block], blocks[other] = blocks[other], item
        return section, other
    if direction == "up" and section > 0:
        return move_to_section(body, section, block, section - 1)
    if direction == "down" and section < len(body.sections) - 1:
        return move_to_section(body, section, block, section + 1, first=True)
    return section, block


def move_to_section(
    body: ProjectBody, section: int, block: int, target: int, *, first: bool = False
) -> tuple[int, int]:
    """Move a block into another section (at its end, or its start with ``first``)."""
    item = _block(body, section, block)
    destination = _section(body, target)
    if target == section:
        return section, block
    del body.sections[section].blocks[block]
    if first:
        position = 0
        while position < len(destination.blocks):
            candidate = destination.blocks[position]
            if not (isinstance(candidate, Text) and candidate.is_blank):
                break
            position += 1
        destination.blocks.insert(position, item)
    else:
        last = destination.blocks[-1] if destination.blocks else None
        if isinstance(last, Text) and not last.is_blank and isinstance(item, Card):
            destination.blocks.append(Text(raw=[""]))
        _insert_before_trailing_blanks(destination, [item])
        position = destination.blocks.index(item)
    if isinstance(item, Text):
        item.raw = _spaced(item.content.split("\n"))
    return target, position


def place_block(
    body: ProjectBody, section: int, block: int, target: int, before: int | None = None
) -> tuple[int, int]:
    """Move a block before block ``before`` of section ``target`` (``None``: at its end).

    This is what a drag and drop asks for; the buttons use ``move_block``.
    """
    if before is None:
        _block(body, section, block)
        if target == section:
            return _to_section_end(body, section, block)
        return move_to_section(body, section, block, target)
    item = _block(body, section, block)
    _block(body, target, before)
    if (target, before) == (section, block):
        return section, block
    del body.sections[section].blocks[block]
    if target == section and before > block:
        before -= 1
    body.sections[target].blocks.insert(before, item)
    if isinstance(item, Text):
        item.raw = _spaced(item.content.split("\n"))
    return target, before


def _to_section_end(body: ProjectBody, section: int, block: int) -> tuple[int, int]:
    item = body.sections[section].blocks.pop(block)
    _insert_before_trailing_blanks(body.sections[section], [item])
    return section, body.sections[section].blocks.index(item)


def remove_block(body: ProjectBody, section: int, block: int) -> Block:
    _block(body, section, block)
    return body.sections[section].blocks.pop(block)


def set_annotation(body: ProjectBody, section: int, block: int, text: str) -> None:
    item = _block(body, section, block)
    if not isinstance(item, Card):
        raise ProjectEditError("Só um cartão tem anotação.")
    item.annotation = [
        ANNOTATION_INDENT + line.strip() for line in text.splitlines() if line.strip()
    ]


def set_text(body: ProjectBody, section: int, block: int, text: str) -> None:
    """Replace a text block; an empty text removes it."""
    item = _block(body, section, block)
    if not isinstance(item, Text):
        raise ProjectEditError("Esse item é um cartão, não um texto.")
    content = text.replace("\r\n", "\n").strip("\n")
    if not content.strip():
        remove_block(body, section, block)
        return
    item.raw = _spaced(content.split("\n"))


def add_text(body: ProjectBody, section: int, text: str, *, after: int | None = None) -> int:
    """Insert the author's text after block ``after`` (or at the section's end)."""
    target = _section(body, section)
    content = text.replace("\r\n", "\n").strip("\n")
    if not content.strip():
        raise ProjectEditError("O texto está vazio.")
    block = Text(raw=_spaced(content.split("\n")))
    if after is None:
        _insert_before_trailing_blanks(target, [block])
    else:
        _block(body, section, after)
        target.blocks.insert(after + 1, block)
    return target.blocks.index(block)


def rename_section(body: ProjectBody, section: int, title: str) -> None:
    target = _section(body, section)
    clean = " ".join(title.split())
    if target.heading is None or not clean:
        raise ProjectEditError("Informe o novo título da seção.")
    target.heading = f"## {clean}"


def move_section(body: ProjectBody, section: int, direction: str) -> int:
    target = _section(body, section)
    if target.heading is None:
        raise ProjectEditError("A introdução fica sempre no topo.")
    other = section - 1 if direction == "up" else section + 1
    if other < 1 or other >= len(body.sections):
        return section
    _ensure_trailing_blank(body.sections[other])
    _ensure_trailing_blank(target)
    body.sections[section], body.sections[other] = body.sections[other], target
    return other


def _ensure_trailing_blank(section: Section) -> None:
    last = section.blocks[-1] if section.blocks else None
    if not (isinstance(last, Text) and last.raw[-1] == ""):
        section.blocks.append(Text(raw=[""]))


def delete_section(body: ProjectBody, section: int) -> None:
    """Drop a heading; its cards and text join the section above, so nothing is lost."""
    target = _section(body, section)
    if target.heading is None:
        raise ProjectEditError("A introdução não pode ser excluída.")
    above = body.sections[section - 1]
    _ensure_trailing_blank(above)
    above.blocks.extend(target.blocks)
    del body.sections[section]
