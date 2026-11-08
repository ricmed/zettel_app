"""A Mesa as a manuscript (ADR-067): the author's order, the notes' own words, no LLM.

Each card becomes its thesis (with the note's citation), its definition and,
on request, its literal passage and its limits. The author's text stays where
the author wrote it; where the outline has a gap, a ``[Complementar]`` marker
says what is missing. Figures are numbered in reading order and copied once.
``## Referências`` closes the text: every source behind an exported card, once,
in alphabetical order (``bibliography.reference_entries``).

Wikilinks never reach the manuscript: it is meant to leave the vault.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zettel.project_file import Card, Text

PLACEHOLDER_STYLE = "Complementar"
FIGURES_DIR = "figuras"

_WIKILINK = re.compile(r"!?\[\[(?P<target>[^\]|#]+)(?:#[^\]|]*)?(?:\|(?P<alias>[^\]]+))?\]\]")
_PERMANENT_TARGET = re.compile(r"^(?:ZTL\s*-\s*)+(?P<id>[0-9A-HJKMNP-TV-Z]{26})")
_TRAILING_PUNCT = re.compile(r"[.!?…]+$")


@dataclass
class Figure:
    number: int
    vault_path: Path
    name: str
    caption: str


@dataclass
class Manuscript:
    title: str
    style: str
    markdown: str
    figures: list[Figure] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Source:
    row: dict
    authors: list[str]

    @property
    def light_mention(self) -> str:
        from zettel.bibliography import display_author_natural

        author = display_author_natural(self.authors) or "o autor"
        year = self.row.get("year")
        title = self.row.get("title") or "a obra"
        return f"{author}, *{title}*" + (f" ({year})" if year else "")

    @property
    def in_text(self) -> str:
        from zettel.bibliography import format_abnt_in_text

        return format_abnt_in_text(self.authors, self.row.get("year"))


def placeholder(text: str) -> str:
    """A gap the author fills; a Pandoc div so the DOCX gives it its own style."""
    return f'::: {{custom-style="{PLACEHOLDER_STYLE}"}}\n[Complementar: {text}]\n:::'


def plain_links(text: str, titles: dict[str, str]) -> str:
    """Replace wikilinks with readable text: the alias, a note's title, or the target."""

    def repl(match: re.Match[str]) -> str:
        if match.group(0).startswith("!"):
            return ""
        if match.group("alias"):
            return match.group("alias").strip()
        target = match.group("target").strip()
        permanent = _PERMANENT_TARGET.match(target)
        if permanent and permanent.group("id") in titles:
            return titles[permanent.group("id")]
        return target.split("/")[-1]

    return _WIKILINK.sub(repl, text)


def _with_citation(thesis: str, cite: str) -> str:
    """``Tese (AUTOR, ano, p. X).`` — the citation goes before the final period."""
    sentence = thesis.strip()
    if not cite:
        return sentence
    end = _TRAILING_PUNCT.search(sentence)
    if end:
        return f"{sentence[: end.start()]} {cite}{end.group(0)}"
    return f"{sentence} {cite}."


def _caption_text(description: str) -> str:
    """The first paragraph of an image description, on one line, safe inside ``![...]``.

    A stored description can carry a second "Conceitos: ..." paragraph and
    brackets; either would break the Markdown image the caption lives in.
    """
    paragraphs = [p for p in re.split(r"\n\s*\n", description.strip()) if p.strip()]
    first = next((p for p in paragraphs if not p.lstrip().startswith("Conceitos:")), "")
    return " ".join(first.split()).replace("[", "(").replace("]", ")")


def _figure_descriptions(body: str) -> dict[str, str]:
    """``90_Assets`` path -> the paragraph right after its embed in ``## Figuras``."""
    from zettel.markdown_fences import h2_section

    section = h2_section(body, "Figuras")
    found: dict[str, str] = {}
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", section) if p.strip()]
    for index, paragraph in enumerate(paragraphs):
        embed = _WIKILINK.fullmatch(paragraph)
        if not embed or not paragraph.startswith("!"):
            continue
        following = paragraphs[index + 1] if index + 1 < len(paragraphs) else ""
        if following and not following.startswith(("!", "Conceitos:")):
            found[embed.group("target").strip()] = following
    return found


class _Builder:
    def __init__(self, cfg: Any, db: Any, project: Any, *, style: str, options: dict[str, bool]):
        self.cfg = cfg
        self.db = db
        self.project = project
        self.style = style
        self.options = options
        self.lines: list[str] = []
        self.figures: list[Figure] = []
        self.figure_by_path: dict[str, Figure] = {}
        self.sources: dict[str, _Source | None] = {}
        self.warnings: list[str] = []
        ids = project.body.note_ids()
        self.rows = db.get_notes_by_ids(ids)
        self.titles = {nid: row.get("title") or nid for nid, row in self.rows.items()}

    # ── pieces ─────────────────────────────────────────────────────────

    def emit(self, *blocks: str) -> None:
        for block in blocks:
            if block.strip():
                self.lines.extend([block.strip("\n"), ""])

    def gap(self, text: str) -> None:
        if self.options["placeholders"]:
            self.emit(placeholder(text))

    def source(self, source_id: str | None) -> _Source | None:
        from zettel.citation import stored_authors

        if not source_id:
            return None
        if source_id not in self.sources:
            row = self.db.get_source(source_id)
            self.sources[source_id] = (
                _Source(row, stored_authors(row.get("authors"))) if row else None
            )
        return self.sources[source_id]

    def cite(self, row: dict) -> str:
        from zettel.citation import load_provenance

        source = self.source(row.get("source_id"))
        if self.style == "blog":
            return f"(segundo {source.light_mention})" if source else ""
        cite = str(load_provenance(row).get("citation") or "").strip()
        return cite or (source.in_text if source else "")

    def credit(self, row: dict, cite: str) -> str:
        """Who the figure is adapted from: the citation, or the source named in full."""
        if self.style == "academic":
            return cite
        source = self.source(row.get("source_id"))
        return source.light_mention if source else ""

    def figure(self, path: str, description: str, credit: str) -> Figure | None:
        vault_file = Path(self.cfg.vault_path) / path
        if not vault_file.is_file():
            self.warnings.append(f"Figura ausente no vault: {path}")
            return None
        number = len(self.figures) + 1
        caption = f"Figura {number} — {_caption_text(description) or 'Sem descrição'}"
        caption = caption.rstrip(".") + "."
        if credit:
            caption += f" Fonte: adaptado de {credit}."
        figure = Figure(number, vault_file, f"fig-{number:02d}{vault_file.suffix.lower()}", caption)
        self.figures.append(figure)
        self.figure_by_path[path] = figure
        return figure

    def card(self, card: Card) -> None:
        from zettel.assets import embedded_asset_paths
        from zettel.citation import load_provenance
        from zettel.note_content import note_sections

        row = self.rows.get(card.note_id)
        if row is None:
            self.warnings.append(f"Nota ausente no acervo: {card.note_id}")
            self.gap(f"a nota {card.note_id} não existe mais no acervo")
            return
        if not row.get("source_id"):
            self.warnings.append(f"Nota sem fonte bibliográfica: {self.titles[card.note_id]}")
        body = row.get("body") or ""
        fields = note_sections(row.get("title") or "", body)
        cite = self.cite(row)
        self.emit(plain_links(_with_citation(fields["thesis"], cite), self.titles))
        self.emit(plain_links(fields["definition"], self.titles))
        if self.options["limits"] and fields["limits"]:
            self.emit("Ressalva: " + plain_links(fields["limits"], self.titles))
        anchor = str(load_provenance(row).get("anchor_quote") or "").strip()
        if self.options["quotes"] and anchor:
            self.emit(f'> "{anchor}" {cite}'.rstrip())
        descriptions = _figure_descriptions(body)
        assets: dict[str, dict] = {}
        if row.get("source_id"):
            assets = {a["path"]: a for a in self.db.get_assets_for_source(row["source_id"])}
        for path in embedded_asset_paths(Path(self.cfg.vault_path), body):
            if path in self.figure_by_path:
                continue  # already shown, numbered, under an earlier card
            description = (assets.get(path) or {}).get("description") or descriptions.get(path, "")
            figure = self.figure(path, description, self.credit(row, cite))
            if figure:
                self.emit(f"![{figure.caption}]({FIGURES_DIR}/{figure.name})")

    def section(self, title: str | None, blocks: list) -> None:
        if title is not None:
            self.emit(f"## {title}")
        visible = [b for b in blocks if not (isinstance(b, Text) and b.is_blank)]
        if title is not None and (not visible or not isinstance(visible[0], Text)):
            self.gap("abertura da seção: o que ela mostra e por quê")
        previous: Card | None = None
        for block in visible:
            if isinstance(block, Text):
                self.emit(plain_links(block.content, self.titles))
                previous = None
                continue
            if previous is not None:
                a = self.titles.get(previous.note_id, previous.note_id)
                b = self.titles.get(block.note_id, block.note_id)
                self.gap(f'transição de "{a}" para "{b}"')
            self.card(block)
            previous = block

    def references(self) -> list[str]:
        from zettel.bibliography import reference_entries

        cited = list(
            dict.fromkeys(row["source_id"] for row in self.rows.values() if row.get("source_id"))
        )
        sources = [s.row for s in (self.source(sid) for sid in cited) if s]
        entries, bare = reference_entries(sources)
        for source_id in bare:
            self.warnings.append(f"Referência incompleta (sem dados ABNT): {source_id}")
        self.emit("## Referências")
        self.emit(*(entries or ["(Nenhuma fonte bibliográfica.)"]))
        return cited

    def appendix(self) -> None:
        from zettel.citation import load_provenance
        from zettel.note_content import note_sections

        self.emit("## Apêndice: notas de origem")
        for note_id in self.project.body.note_ids():
            row = self.rows.get(note_id)
            if not row:
                continue
            fields = note_sections(row.get("title") or "", row.get("body") or "")
            provenance = load_provenance(row)
            self.emit(f"### {self.titles[note_id]}")
            self.emit(f"**Tese.** {plain_links(fields['thesis'], self.titles)}")
            self.emit(plain_links(fields["definition"], self.titles))
            if fields["limits"]:
                self.emit(f"**Limites.** {plain_links(fields['limits'], self.titles)}")
            cite = str(provenance.get("citation") or "")
            anchor = str(provenance.get("anchor_quote") or "")
            if anchor:
                self.emit(f'> "{anchor}" {cite}'.rstrip())
            elif cite:
                self.emit(cite)

    def build(self) -> Manuscript:
        self.emit(f"# {self.project.title}")
        sections = self.project.body.sections
        intro = sections[0]
        if any(not (isinstance(b, Text) and b.is_blank) for b in intro.blocks):
            self.section(None, intro.blocks)
        else:
            self.gap("introdução: o tema, a pergunta e o caminho do texto")
        for section in sections[1:]:
            self.section(section.title, section.blocks)
        self.gap("conclusão: o que o percurso mostrou")
        cited = self.references()
        if self.options["appendix"]:
            self.appendix()
        return Manuscript(
            title=self.project.title,
            style=self.style,
            markdown="\n".join(self.lines).rstrip() + "\n",
            figures=self.figures,
            source_ids=cited,
            warnings=self.warnings,
        )


def build_manuscript(
    cfg: Any,
    db: Any,
    project: Any,
    *,
    style: str | None = None,
    placeholders: bool = True,
    quotes: bool = False,
    limits: bool = False,
    appendix: bool = False,
) -> Manuscript:
    """The Markdown of ``project`` and the figures it references. Reads only."""
    chosen = style if style in ("academic", "blog") else project.style
    options = {
        "placeholders": placeholders,
        "quotes": quotes,
        "limits": limits,
        "appendix": appendix,
    }
    return _Builder(cfg, db, project, style=chosen, options=options).build()
