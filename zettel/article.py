"""The `article` command — structured long-form writing from the vault.

Domain helpers (catalog, merge, draft, assemble, personality, judge) live here.
Orchestration is a LangGraph StateGraph in ``article_graph.py``: query enrich ->
incremental hybrid search -> context HITL -> outline HITL -> draft -> assemble ->
personality -> judge loop -> verify/save.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Optional

from .bibliography import display_author_natural, format_abnt_in_text
from .config import llm_phase
from .hashing import compute_llm_call_checksum, normalize_text_for_hash, sha256_hex
from .llm import call_llm, clip_text, extract_json, fill_template, get_llm, load_prompt_parts
from .retrieval import RetrievedNote
from .schemas import ArticleOutline, ArticleOutlineSection
from .vault import _slug, permanent_wikilink, render_frontmatter

if TYPE_CHECKING:
    from .config import AppConfig
    from .index import VectorIndex
    from .state import StateDB

logger = logging.getLogger(__name__)

ArticleStyle = Literal["blog", "academic"]
OutlineDecision = Literal["approve", "regenerate", "abort"]

_NO_EVIDENCE = (
    "Nao encontrei evidencia suficiente no vault para escrever um artigo "
    "sobre esse tema."
)

_ZTL_WIKILINK = re.compile(r"\[\[ZTL - ([0-9A-HJKMNP-TV-Z]{26})")
_FIG_EMBED = re.compile(r"!\[\[(90_Assets/[^\]]+)\]\]")
_CITES_COMMENT = re.compile(
    r"<!--\s*cites:\s*([^\n>]*?)\s*-->", re.IGNORECASE
)
_WIKI_EMBED_ANY = re.compile(r"!\[\[([^\]]+)\]\]")


# ── Data structures ────────────────────────────────────────────────────


@dataclass
class CatalogAsset:
    asset_id: str
    path: str
    description: str = ""
    source_id: Optional[str] = None


@dataclass
class CatalogSource:
    source_id: str
    citekey: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: Optional[int] = None
    abnt_reference: str = ""
    document_type: Optional[str] = None

    @property
    def in_text_cite(self) -> str:
        return format_abnt_in_text(self.authors, self.year)

    @property
    def author_natural(self) -> str:
        return display_author_natural(self.authors)

    @property
    def light_mention(self) -> str:
        author = self.author_natural or "o autor"
        title = self.title or "a obra"
        return f"{author} em *{title}*"


@dataclass
class CatalogNote:
    note_id: str
    title: str
    body: str
    wiki_link: str
    source_id: Optional[str] = None
    score: float = 0.0
    hop: int = 0
    origin: str = "busca"
    assets: list[CatalogAsset] = field(default_factory=list)
    summary: str = ""


@dataclass
class ArticleCatalog:
    topic: str
    style: ArticleStyle
    notes: dict[str, CatalogNote] = field(default_factory=dict)
    sources: dict[str, CatalogSource] = field(default_factory=dict)
    assets: dict[str, CatalogAsset] = field(default_factory=dict)
    moc_ids: list[str] = field(default_factory=list)
    candidates: list[RetrievedNote] = field(default_factory=list)
    retrieval_params: dict = field(default_factory=dict)


@dataclass
class ArticleResult:
    topic: str
    style: ArticleStyle
    title: str
    body: str
    frontmatter: dict = field(default_factory=dict)
    outline: Optional[ArticleOutline] = None
    warnings: list[str] = field(default_factory=list)
    llm_called: bool = False
    llm_model: str = ""
    note_ids: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    answer: str = ""  # alias of assembled body for CLI convenience
    no_evidence: bool = False
    aborted: bool = False


ApproveOutlineFn = Callable[
    [ArticleOutline], tuple[OutlineDecision, Optional[str]]
]


# ── Public API ─────────────────────────────────────────────────────────


def run_article(
    cfg: "AppConfig",
    db: "StateDB",
    idx: "VectorIndex",
    topic: str,
    style: ArticleStyle = "blog",
    topk: Optional[int] = None,
    use_graph: Optional[bool] = None,
    mode: Optional[str] = None,
    outline_only: bool = False,
    approve_outline: Optional[ApproveOutlineFn] = None,
    personality: Optional[str] = None,
    custom_style_notes: Optional[str] = None,
    skip_context_review: bool = False,
    skip_judge: bool = False,
    max_judge_iterations: Optional[int] = None,
    context_callback: Optional[Callable] = None,
) -> ArticleResult:
    """Generate a structured article via the LangGraph pipeline.

    ``approve_outline`` maps to an outline HITL callback (tests/CLI helpers).
    When both outline and context callbacks are absent and skips are false,
    the graph uses LangGraph ``interrupt()`` for human-in-the-loop.
    """
    from .article_graph import run_article_graph

    return run_article_graph(
        cfg, db, idx, topic,
        style=style,
        topk=topk,
        use_graph=use_graph,
        mode=mode,
        outline_only=outline_only,
        approve_outline=approve_outline,
        personality=personality,
        custom_style_notes=custom_style_notes,
        skip_context_review=skip_context_review,
        skip_judge=skip_judge,
        max_judge_iterations=max_judge_iterations,
        context_callback=context_callback,
    )


def generate_outline(
    cfg: "AppConfig",
    db: "StateDB",
    catalog: ArticleCatalog,
    feedback: Optional[str] = None,
) -> tuple[ArticleOutline, bool]:
    """Call the LLM to produce an ArticleOutline. Returns (outline, llm_called)."""
    art_cfg = cfg.retrieval.article
    prompt_parts = load_prompt_parts(cfg.prompts_path / "article_outline.md")
    notes_block = _format_notes_for_outline(catalog)
    mapping = {
        "language": cfg.language,
        "topic": catalog.topic,
        "style": catalog.style,
        "max_sections": str(art_cfg.max_sections),
        "notes_catalog": notes_block,
        "feedback": feedback.strip() if feedback else "(nenhum)",
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    user = fill_template(prompt_parts.user_template, mapping)

    raw, llm_called = _cached_llm(
        cfg, db, prompt_parts.full_template, system=system, user=user,
        label=f"outline | tema={clip_text(catalog.topic)}",
    )
    data = json.loads(extract_json(raw))
    outline = ArticleOutline.model_validate(data)
    outline = _sanitize_outline(outline, catalog, art_cfg.max_sections)
    return outline, llm_called


def draft_sections(
    cfg: "AppConfig",
    db: "StateDB",
    catalog: ArticleCatalog,
    outline: ArticleOutline,
    judge_feedback: str = "",
) -> tuple[list[str], list[str], bool]:
    """Draft all sections. Returns (bodies, note_ids, llm_called)."""
    art_cfg = cfg.retrieval.article
    prompt_name = (
        "article_section_blog.md"
        if catalog.style == "blog"
        else "article_section_academic.md"
    )
    prompt_parts = load_prompt_parts(cfg.prompts_path / prompt_name)
    anti_path = cfg.prompts_path / "article_anti_ai.md"
    anti_ai = anti_path.read_text(encoding="utf-8") if anti_path.exists() else ""
    feedback_block = judge_feedback.strip() if judge_feedback else "(nenhum)"

    writer_temp = art_cfg.writer_temperature
    section_bodies: list[str] = []
    used_note_ids: list[str] = []
    llm_called = False

    for i, section in enumerate(outline.sections, 1):
        packed = _pack_section(catalog, section)
        used_note_ids.extend(packed["note_ids"])
        mapping = {
            "language": cfg.language,
            "topic": catalog.topic,
            "article_title": outline.title,
            "thesis": outline.thesis,
            "style_notes": outline.style_notes or "",
            "heading": section.heading,
            "goal": section.goal,
            "target_chars": str(art_cfg.chars_per_section_draft),
            "evidence": packed["evidence"],
            "figures": packed["figures"],
            "sources": packed["sources"],
            "anti_ai": anti_ai,
            "judge_feedback": feedback_block,
        }
        system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
        user = fill_template(prompt_parts.user_template, mapping)
        note_preview = ", ".join(packed["note_ids"][:2])
        if len(packed["note_ids"]) > 2:
            note_preview += f" (+{len(packed['note_ids']) - 2})"
        raw, called = _cached_llm(
            cfg, db, prompt_parts.full_template, system=system, user=user,
            temperature=writer_temp,
            label=(
                f"secao {i}/{len(outline.sections)} | "
                f"{clip_text(section.heading, 40)} | notas={note_preview or '-'}"
            ),
            step=i,
            total=len(outline.sections),
        )
        llm_called = llm_called or called
        section_bodies.append(raw.strip())

    return section_bodies, used_note_ids, llm_called


def assemble_article(
    outline: ArticleOutline,
    section_bodies: list[str],
    catalog: ArticleCatalog,
    vault_path: Path | None = None,
) -> tuple[dict, str, list[str], list[str]]:
    """Merge section drafts into final Markdown. Returns meta, body, cited source_ids, warnings."""
    warnings: list[str] = []
    cleaned_sections: list[str] = []
    cited_source_ids: list[str] = []
    figure_counter = 0
    seen_figures: set[str] = set()

    citekey_to_source = {
        (s.citekey or s.source_id.lstrip("@")): s
        for s in catalog.sources.values()
    }
    # Also index by full source_id (@citekey)
    for sid, src in catalog.sources.items():
        citekey_to_source[sid] = src
        citekey_to_source[sid.lstrip("@")] = src

    for i, raw in enumerate(section_bodies):
        text = raw.strip()
        cite_ids = _extract_cites_comment(text)
        text = _CITES_COMMENT.sub("", text).strip()

        for cid in cite_ids:
            src = citekey_to_source.get(cid) or citekey_to_source.get(cid.lstrip("@"))
            if src and src.source_id not in cited_source_ids:
                cited_source_ids.append(src.source_id)
            elif src is None:
                warnings.append(f"Citekey desconhecida na secao {i + 1}: {cid}")

        # Academic: also harvest parenthetical citations against catalog surnames
        if catalog.style == "academic":
            for sid in _match_parenthetical_sources(text, catalog):
                if sid not in cited_source_ids:
                    cited_source_ids.append(sid)

        # Renumber figures that appear as embeds
        def _renumber_fig(m: re.Match) -> str:
            nonlocal figure_counter
            path = m.group(1)
            if path in seen_figures:
                return m.group(0)
            seen_figures.add(path)
            figure_counter += 1
            asset = _asset_by_path(catalog, path)
            desc = (asset.description if asset else "") or ""
            lines = [f"![[{path}]]", ""]
            if catalog.style == "academic":
                lines.append(f"**Figura {figure_counter}** — {desc}".rstrip(" —"))
                if asset and asset.source_id and asset.source_id in catalog.sources:
                    src = catalog.sources[asset.source_id]
                    label = src.title or src.source_id
                    lines.append(f"Fonte: adaptado de {label}.")
            else:
                if desc:
                    lines.append(f"*Figura: {desc}*")
            return "\n".join(lines)

        text = _WIKI_EMBED_ANY.sub(_renumber_fig, text)
        if not text.strip():
            warnings.append(f"Secao vazia: {outline.sections[i].heading if i < len(outline.sections) else i}")
        cleaned_sections.append(text)

    lines: list[str] = [f"# {outline.title}", ""]
    if outline.thesis.strip():
        if catalog.style == "academic":
            # thesis is woven into intro; keep as blockquote lead-in
            lines.append(f"> {outline.thesis.strip()}")
            lines.append("")
        else:
            lines.append(outline.thesis.strip())
            lines.append("")

    lines.extend(cleaned_sections)
    lines.append("")

    if catalog.style == "blog":
        lines.append("## Para saber mais")
        lines.append("")
        if cited_source_ids:
            for sid in cited_source_ids:
                src = catalog.sources.get(sid)
                if not src:
                    continue
                author = src.author_natural or "Autor desconhecido"
                title = src.title or sid
                year = f" ({src.year})" if src.year else ""
                lines.append(f"- {author}. *{title}*{year}.")
        else:
            # fallback: all sources in catalog notes
            for src in _unique_sources_from_notes(catalog):
                author = src.author_natural or "Autor desconhecido"
                title = src.title or src.source_id
                year = f" ({src.year})" if src.year else ""
                lines.append(f"- {author}. *{title}*{year}.")
                cited_source_ids.append(src.source_id)
        lines.append("")
    else:
        lines.append("## Referencias")
        lines.append("")
        refs = []
        for sid in cited_source_ids:
            src = catalog.sources.get(sid)
            if not src:
                continue
            ref = (src.abnt_reference or "").strip()
            if not ref:
                # fallback minimal
                author = src.author_natural or "Autor desconhecido"
                title = src.title or sid
                year = src.year or "s.d."
                ref = f"{author}. {title}. {year}."
            refs.append(ref)
        refs.sort(key=lambda r: r.upper())
        if refs:
            for ref in refs:
                lines.append(ref)
                lines.append("")
        else:
            lines.append("(Nenhuma referencia citada.)")
            lines.append("")
            warnings.append("Nenhuma referencia ABNT resolvida para o artigo academico.")

    # Vault provenance appendix
    lines.append("## Origem no vault")
    lines.append("")
    for note in catalog.notes.values():
        lines.append(f"- {note.wiki_link} — {note.title or 'Sem titulo'}")
    lines.append("")

    if vault_path is not None:
        for path in seen_figures:
            full = Path(vault_path) / path
            if not full.exists():
                warnings.append(f"Figura ausente no vault: {path}")

    meta = {
        "created_at": datetime.now().isoformat(),
        "title": outline.title,
    }
    return meta, "\n".join(lines).rstrip() + "\n", cited_source_ids, warnings


def verify_article(
    body: str,
    catalog: ArticleCatalog,
    vault_path: Path | None = None,
) -> list[str]:
    """Deterministic checks; returns warning strings (never raises)."""
    warnings: list[str] = []
    if not body.strip() or body.strip() == _NO_EVIDENCE:
        warnings.append("Corpo do artigo vazio ou sem evidencia.")
        return warnings

    for m in _WIKI_EMBED_ANY.finditer(body):
        path = m.group(1)
        if vault_path is not None:
            full = Path(vault_path) / path
            if not full.exists():
                warnings.append(f"Embed inexistente: {path}")

    if catalog.style == "academic":
        # Parenthetical citations should map to at least one catalog source surname
        for m in re.finditer(r"\(([A-ZÀ-Ú][^)]*?,\s*(?:s\.d\.|\d{4}))", body):
            snippet = m.group(1)
            if not _parenthetical_matches_catalog(snippet, catalog):
                warnings.append(f"Citacao possivelmente orfa: ({snippet}...)")

    return warnings


def build_article_note(result: ArticleResult) -> tuple[dict, str]:
    meta = dict(result.frontmatter)
    meta.setdefault("type", "article")
    meta.setdefault("origin", "article")
    meta.setdefault("topic", result.topic)
    meta.setdefault("style", result.style)
    meta.setdefault("title", result.title)
    meta.setdefault("llm_model", result.llm_model)
    meta.setdefault("created_at", datetime.now().isoformat())
    return meta, result.body


def save_article_note(
    result: ArticleResult, vault_path: Path, dest: Optional[Path] = None
) -> Path:
    """Persist the article as Markdown under ``00_Inbox/`` by default."""
    meta, body = build_article_note(result)
    if dest is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = _slug(result.title or result.topic) or "artigo"
        filename = f"ART - {ts} - {slug}.md"
        dest = Path(vault_path) / "00_Inbox" / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = render_frontmatter(meta) + "\n" + body
    if not content.endswith("\n"):
        content += "\n"
    dest.write_text(content, encoding="utf-8")
    logger.info("Artigo salvo em: %s", dest)
    return dest


def format_outline_for_display(outline: ArticleOutline) -> str:
    return _format_outline_preview(outline)


# ── Internals ──────────────────────────────────────────────────────────


def _wiki_link(db: "StateDB", note_id: str, title: str, path: str | None = None) -> str:
    if path is None:
        row = db.get_note(note_id)
        path = row.get("path") if row else None
    return permanent_wikilink(note_id, title, path=path)


def _origin_label(hit: RetrievedNote) -> str:
    if hit.hop == 0 or not hit.via:
        return "busca"
    step = hit.via[-1]
    rel = step.get("relation_type", "related")
    anchor = step.get("from", "")
    return f"conexao {rel} a partir de [[ZTL - {anchor}]]"


def _merge_moc_notes(
    db: "StateDB", hits: list[RetrievedNote], moc: dict
) -> list[RetrievedNote]:
    """Add notes linked from a matching MOC body as high-confidence seeds."""
    by_id = {h.note_id: h for h in hits}
    body = moc.get("body") or ""
    for nid in _ZTL_WIKILINK.findall(body):
        if nid in by_id:
            by_id[nid].score = max(by_id[nid].score, 1.0)
            continue
        row = db.get_note(nid)
        if not row:
            continue
        rn = RetrievedNote(
            note_id=nid,
            score=1.0,
            title=row.get("title") or "",
            document=row.get("body") or "",
            metadata={
                "source_id": row.get("source_id"),
                "path": row.get("path"),
                "moc_boost": True,
            },
            hop=0,
            passed_floor=True,
            floor_reason="nota linkada no MOC do tema",
        )
        by_id[nid] = rn
    return sorted(by_id.values(), key=lambda r: r.score, reverse=True)


def _populate_catalog(
    db: "StateDB",
    catalog: ArticleCatalog,
    hits: list[RetrievedNote],
    max_figures: int,
    max_chars: int,
) -> None:
    asset_freq: dict[str, int] = {}

    for hit in hits:
        row = db.get_note(hit.note_id)
        source_id = hit.metadata.get("source_id")
        body = hit.document or ""
        title = hit.title or ""
        if row:
            source_id = source_id or row.get("source_id")
            body = body or row.get("body") or ""
            title = title or row.get("title") or ""

        note_assets = _assets_from_note_body(db, body, source_id)
        for a in note_assets:
            catalog.assets[a.asset_id] = a
            asset_freq[a.asset_id] = asset_freq.get(a.asset_id, 0) + 1

        if source_id and source_id not in catalog.sources:
            src_row = db.get_source(source_id)
            if src_row:
                authors = src_row.get("authors") or "[]"
                if isinstance(authors, str):
                    try:
                        authors = json.loads(authors)
                    except json.JSONDecodeError:
                        authors = []
                catalog.sources[source_id] = CatalogSource(
                    source_id=source_id,
                    citekey=src_row.get("citekey") or source_id.lstrip("@"),
                    title=src_row.get("title") or "",
                    authors=list(authors or []),
                    year=src_row.get("year"),
                    abnt_reference=src_row.get("abnt_reference") or "",
                    document_type=src_row.get("document_type"),
                )

        summary = (body or "").strip()
        # Strip managed/figures blocks for summary
        summary = re.sub(
            r"## Figuras\n.*?(?=\n## |\Z)", "", summary, flags=re.DOTALL
        ).strip()
        if len(summary) > 200:
            summary = summary[:200].rstrip() + "..."

        body_trunc = (body or "").strip()
        if len(body_trunc) > max_chars:
            body_trunc = body_trunc[:max_chars].rstrip() + "..."

        catalog.notes[hit.note_id] = CatalogNote(
            note_id=hit.note_id,
            title=title,
            body=body_trunc,
            wiki_link=_wiki_link(
                db, hit.note_id, title, path=row.get("path") if row else None,
            ),
            source_id=source_id,
            score=hit.score,
            hop=hit.hop,
            origin=_origin_label(hit),
            assets=note_assets,
            summary=summary,
        )

    # Keep top max_figures assets by frequency across notes
    if len(catalog.assets) > max_figures:
        ranked = sorted(
            catalog.assets.values(),
            key=lambda a: asset_freq.get(a.asset_id, 0),
            reverse=True,
        )[:max_figures]
        keep = {a.asset_id for a in ranked}
        catalog.assets = {k: v for k, v in catalog.assets.items() if k in keep}


def _assets_from_note_body(
    db: "StateDB", body: str, source_id: Optional[str]
) -> list[CatalogAsset]:
    paths = _FIG_EMBED.findall(body or "")
    if not paths:
        return []
    by_path: dict[str, dict] = {}
    if source_id:
        for row in db.get_assets_for_source(source_id):
            by_path[row["path"]] = row
    # Also try exact path lookup across known paths if source missing
    out: list[CatalogAsset] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        row = by_path.get(path)
        if row is None and source_id is None:
            # best-effort: skip unresolved
            out.append(
                CatalogAsset(asset_id=f"path::{path}", path=path, description="")
            )
            continue
        if row is None:
            out.append(
                CatalogAsset(
                    asset_id=f"path::{path}",
                    path=path,
                    description="",
                    source_id=source_id,
                )
            )
            continue
        out.append(
            CatalogAsset(
                asset_id=row["asset_id"],
                path=row["path"],
                description=row.get("description") or "",
                source_id=row.get("source_id") or source_id,
            )
        )
    return out


def _format_notes_for_outline(catalog: ArticleCatalog) -> str:
    parts: list[str] = []
    for i, note in enumerate(catalog.notes.values(), 1):
        src_line = note.source_id or "(sem fonte)"
        if note.source_id and note.source_id in catalog.sources:
            src = catalog.sources[note.source_id]
            src_line = (
                f"{src.source_id} | {src.author_natural} | "
                f"{src.title} | cite: {src.in_text_cite or 'n/d'}"
            )
        figs = ", ".join(a.asset_id for a in note.assets) or "(nenhuma)"
        parts.append(
            f"### Nota {i}\n"
            f"- note_id: {note.note_id}\n"
            f"- titulo: {note.title}\n"
            f"- fonte: {src_line}\n"
            f"- figuras: {figs}\n"
            f"- resumo: {note.summary}\n"
        )
    # Asset catalog
    if catalog.assets:
        parts.append("### Assets disponiveis\n")
        for a in catalog.assets.values():
            parts.append(
                f"- asset_id: {a.asset_id} | path: {a.path} | "
                f"desc: {(a.description or '')[:120]}\n"
            )
    return "\n".join(parts)


def _sanitize_outline(
    outline: ArticleOutline, catalog: ArticleCatalog, max_sections: int
) -> ArticleOutline:
    known = set(catalog.notes.keys())
    known_assets = set(catalog.assets.keys())
    sections: list[ArticleOutlineSection] = []
    for sec in outline.sections[:max_sections]:
        note_ids = [n for n in sec.note_ids if n in known]
        if not note_ids:
            # fallback: assign top-scoring notes
            note_ids = list(catalog.notes.keys())[:3]
        fig_ids = [a for a in sec.figure_asset_ids if a in known_assets][:2]
        sections.append(
            ArticleOutlineSection(
                heading=sec.heading.strip() or "Secao",
                goal=sec.goal.strip() or "",
                note_ids=note_ids,
                figure_asset_ids=fig_ids,
            )
        )
    if not sections:
        sections = [
            ArticleOutlineSection(
                heading="Desenvolvimento",
                goal="Sintetizar as ideias principais do acervo sobre o tema.",
                note_ids=list(catalog.notes.keys())[:5],
                figure_asset_ids=[],
            )
        ]
    return ArticleOutline(
        title=outline.title.strip() or catalog.topic,
        thesis=outline.thesis.strip(),
        sections=sections,
        style_notes=outline.style_notes or "",
    )


def _pack_section(
    catalog: ArticleCatalog,
    section: ArticleOutlineSection,
) -> dict:
    note_ids = [nid for nid in section.note_ids if nid in catalog.notes]
    if not note_ids:
        # Redistribute: mini search by heading against catalog pool order
        note_ids = list(catalog.notes.keys())[:3]

    evidence_parts: list[str] = []
    source_parts: list[str] = []
    seen_sources: set[str] = set()
    for nid in note_ids:
        note = catalog.notes[nid]
        evidence_parts.append(
            f"#### {note.title}\n"
            f"- note_id: {note.note_id}\n"
            f"- wikilink: {note.wiki_link}\n"
            f"- origem: {note.origin}\n\n"
            f"{note.body}\n"
        )
        if note.source_id and note.source_id not in seen_sources:
            seen_sources.add(note.source_id)
            src = catalog.sources.get(note.source_id)
            if src:
                source_parts.append(
                    f"- source_id: {src.source_id}\n"
                    f"  citekey: {src.citekey}\n"
                    f"  mencao_leve: {src.light_mention}\n"
                    f"  citacao_abnt: {src.in_text_cite or '(indisponivel)'}\n"
                    f"  referencia_abnt: {src.abnt_reference or '(indisponivel)'}\n"
                    f"  autor_natural: {src.author_natural}\n"
                    f"  titulo: {src.title}\n"
                    f"  ano: {src.year or 's.d.'}\n"
                )

    fig_parts: list[str] = []
    fig_ids = list(section.figure_asset_ids)
    if not fig_ids:
        # pick assets from section notes
        for nid in note_ids:
            for a in catalog.notes[nid].assets:
                if a.asset_id not in fig_ids:
                    fig_ids.append(a.asset_id)
                if len(fig_ids) >= 2:
                    break
            if len(fig_ids) >= 2:
                break

    for aid in fig_ids[:2]:
        asset = catalog.assets.get(aid)
        if not asset:
            continue
        fig_parts.append(
            f"- asset_id: {asset.asset_id}\n"
            f"  path: {asset.path}\n"
            f"  embed: ![[{asset.path}]]\n"
            f"  description: {asset.description or '(sem descricao)'}\n"
        )

    return {
        "note_ids": note_ids,
        "evidence": "\n".join(evidence_parts) or "(sem evidencias)",
        "sources": "\n".join(source_parts) or "(sem fontes bibliograficas)",
        "figures": "\n".join(fig_parts) or "(nenhuma figura sugerida)",
    }


def _cached_llm(
    cfg: "AppConfig",
    db: "StateDB",
    prompt_template: str,
    filled: str = "",
    temperature: float | None = None,
    *,
    system: str = "",
    user: str = "",
    label: str | None = None,
    step: int | None = None,
    total: int | None = None,
) -> tuple[str, bool]:
    spec = llm_phase(cfg, "article")
    temp = cfg.llm.temperature if temperature is None else temperature
    user_text = user or filled
    system_text = system or ""
    filled_for_hash = f"{system_text}\n{user_text}" if system_text else user_text
    prompt_hash = sha256_hex(prompt_template)
    filled_hash = sha256_hex(normalize_text_for_hash(filled_for_hash))
    call_checksum = compute_llm_call_checksum(
        prompt_hash, filled_hash, spec.model, temp, cfg.language,
    )
    cached = db.get_cached_llm_response(call_checksum)
    if cached is not None:
        if label:
            if step is not None and total is not None:
                logger.info("LLM cache [%d/%d] %s", step, total, label)
            else:
                logger.info("LLM cache %s", label)
        else:
            logger.debug("Cache hit (article)")
        from zettel.usage import record_cache_hit
        record_cache_hit(label=label or "article", model=spec.model)
        return cached, False
    llm = get_llm(cfg, "article", temperature=temp)
    answer = call_llm(
        llm,
        user_text,
        system=system_text or None,
        label=label,
        step=step,
        total=total,
        provider=spec.provider,
        prompt_cache=cfg.llm.prompt_cache,
    )
    db.cache_llm_response(
        call_checksum,
        json.dumps({"system": system_text, "user": user_text}, ensure_ascii=False),
        answer,
    )
    return answer, True


def _format_outline_preview(outline: ArticleOutline) -> str:
    lines = [
        f"# {outline.title}",
        "",
        f"**Tese:** {outline.thesis}",
        "",
    ]
    if outline.style_notes:
        lines.append(f"*Tom:* {outline.style_notes}")
        lines.append("")
    for i, sec in enumerate(outline.sections, 1):
        lines.append(f"{i}. **{sec.heading}**")
        lines.append(f"   - Objetivo: {sec.goal}")
        lines.append(f"   - Notas: {len(sec.note_ids)} | Figuras: {len(sec.figure_asset_ids)}")
    return "\n".join(lines)


def _extract_cites_comment(text: str) -> list[str]:
    m = _CITES_COMMENT.search(text)
    if not m:
        return []
    raw = m.group(1).strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _asset_by_path(catalog: ArticleCatalog, path: str) -> Optional[CatalogAsset]:
    for a in catalog.assets.values():
        if a.path == path:
            return a
    return None


def _unique_sources_from_notes(catalog: ArticleCatalog) -> list[CatalogSource]:
    seen: set[str] = set()
    out: list[CatalogSource] = []
    for note in catalog.notes.values():
        if note.source_id and note.source_id not in seen:
            src = catalog.sources.get(note.source_id)
            if src:
                seen.add(note.source_id)
                out.append(src)
    return out


def _match_parenthetical_sources(text: str, catalog: ArticleCatalog) -> list[str]:
    found: list[str] = []
    for sid, src in catalog.sources.items():
        cite = src.in_text_cite
        if cite and cite in text and sid not in found:
            found.append(sid)
            continue
        # surname + year loose match
        if not src.authors or not src.year:
            continue
        surname = src.authors[0].strip().split()[-1].upper()
        pattern = re.compile(
            rf"\({re.escape(surname)}(?:\s+et\s+al\.)?[^)]*{src.year}"
        )
        if pattern.search(text) and sid not in found:
            found.append(sid)
    return found


def _parenthetical_matches_catalog(snippet: str, catalog: ArticleCatalog) -> bool:
    upper = snippet.upper()
    for src in catalog.sources.values():
        if not src.authors:
            continue
        surname = src.authors[0].strip().split()[-1].upper()
        if surname in upper:
            return True
    return False


# ── Research merge / enrich / personality / judge ──────────────────────


def retrieved_note_to_dict(hit: RetrievedNote) -> dict:
    """Serialize a RetrievedNote for graph state (JSON-friendly)."""
    return {
        "note_id": hit.note_id,
        "score": hit.score,
        "title": hit.title or "",
        "document": hit.document or "",
        "metadata": dict(hit.metadata or {}),
        "hop": hit.hop,
        "via": list(hit.via or []),
        "passed_floor": hit.passed_floor,
        "floor_reason": hit.floor_reason or "",
        "vector_distance": hit.vector_distance,
        "bm25_rank": hit.bm25_rank,
    }


def dict_to_retrieved_note(d: dict) -> RetrievedNote:
    return RetrievedNote(
        note_id=d["note_id"],
        score=float(d.get("score") or 0.0),
        title=d.get("title") or "",
        document=d.get("document") or "",
        metadata=dict(d.get("metadata") or {}),
        hop=int(d.get("hop") or 0),
        via=list(d.get("via") or []),
        passed_floor=bool(d.get("passed_floor", True)),
        floor_reason=d.get("floor_reason") or "",
        vector_distance=d.get("vector_distance"),
        bm25_rank=d.get("bm25_rank"),
    )


def merge_retrieved_notes(
    existing: list[dict],
    new_hits: list[RetrievedNote],
    max_notes: int,
) -> list[dict]:
    """Merge retrieval hits by note_id, keeping the best score; cap by score."""
    by_id: dict[str, dict] = {d["note_id"]: dict(d) for d in existing}
    for hit in new_hits:
        d = retrieved_note_to_dict(hit)
        prev = by_id.get(hit.note_id)
        if prev is None or d["score"] >= float(prev.get("score") or 0.0):
            by_id[hit.note_id] = d
    merged = sorted(
        by_id.values(), key=lambda x: float(x.get("score") or 0), reverse=True
    )
    return merged[:max_notes]


def parse_extra_queries(raw: str) -> list[str]:
    """Split user extra queries by newlines or semicolons."""
    if not raw or not raw.strip():
        return []
    parts: list[str] = []
    for line in raw.replace(";", "\n").splitlines():
        q = line.strip()
        if q:
            parts.append(q)
    return parts


def enrich_search_queries(
    cfg: "AppConfig",
    db: "StateDB",
    topic: str,
    style: ArticleStyle,
    extra_queries: Optional[list[str]] = None,
    count: Optional[int] = None,
) -> tuple[list[str], bool]:
    """LLM-expand topic into search queries. Returns (queries, llm_called)."""
    art_cfg = cfg.retrieval.article
    count = count if count is not None else art_cfg.enrich_query_count
    extras = [q.strip() for q in (extra_queries or []) if q and q.strip()]
    extras_block = "\n".join(f"- {q}" for q in extras) if extras else "(nenhuma)"

    prompt_parts = load_prompt_parts(cfg.prompts_path / "article_query_enrich.md")
    mapping = {
        "language": cfg.language,
        "topic": topic,
        "style": style,
        "count": str(count),
        "extra_queries": extras_block,
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    user = fill_template(prompt_parts.user_template, mapping)
    raw, called = _cached_llm(
        cfg, db, prompt_parts.full_template, system=system, user=user,
        temperature=art_cfg.enrich_temperature,
        label=f"enrich queries | tema={clip_text(topic)} | alvo={count}",
    )
    data = json.loads(extract_json(raw))
    queries = [str(q).strip() for q in (data.get("queries") or []) if str(q).strip()]

    ordered: list[str] = []
    for q in extras + queries:
        if q not in ordered:
            ordered.append(q)
    if topic.strip() and topic.strip() not in ordered:
        ordered.insert(0, topic.strip())
    return ordered[: max(count, len(extras) + 1)], called


def catalog_from_retrieved(
    cfg: "AppConfig",
    db: "StateDB",
    topic: str,
    style: ArticleStyle,
    retrieved_notes: list[dict],
    moc_ids: Optional[list[str]] = None,
    retrieval_params: Optional[dict] = None,
) -> ArticleCatalog:
    """Build ArticleCatalog from accumulated retrieved note dicts."""
    art_cfg = cfg.retrieval.article
    hits = [dict_to_retrieved_note(d) for d in retrieved_notes]
    catalog = ArticleCatalog(
        topic=topic,
        style=style,
        moc_ids=list(moc_ids or []),
        retrieval_params=dict(retrieval_params or {}),
    )
    _populate_catalog(
        db, catalog, hits, art_cfg.max_figures, art_cfg.max_chars_per_note
    )
    return catalog


def load_personalities(path: Path) -> dict[str, dict]:
    """Load personality profiles from YAML. Returns id -> profile dict."""
    import yaml

    if not path.exists():
        return {
            "neutral": {
                "name": "Neutro",
                "temperature": 0.5,
                "style_prompt": "Sem reescrita.",
            }
        }
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profiles = data.get("personalities") or data
    return {str(k): dict(v) for k, v in profiles.items()}


def apply_personality_rewrite(
    cfg: "AppConfig",
    db: "StateDB",
    body: str,
    personality_id: str,
    custom_style_notes: str = "",
) -> tuple[str, bool]:
    """Rewrite article body with personality. Returns (body, llm_called).

    ``neutral`` without custom notes is a no-op (no LLM call).
    """
    art_cfg = cfg.retrieval.article
    pid = (personality_id or art_cfg.default_personality or "neutral").strip()
    notes = (custom_style_notes or "").strip()
    if pid == "neutral" and not notes:
        return body, False

    profiles = load_personalities(Path(art_cfg.personalities_path))
    profile = profiles.get(pid) or profiles.get("neutral") or {
        "name": pid,
        "temperature": 0.7,
        "style_prompt": notes or "Reescreva com clareza.",
    }
    prompt_parts = load_prompt_parts(cfg.prompts_path / "article_personality.md")
    mapping = {
        "language": cfg.language,
        "personality_name": str(profile.get("name") or pid),
        "style_prompt": str(profile.get("style_prompt") or ""),
        "custom_style_notes": notes or "(nenhuma)",
        "article_body": body,
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    user = fill_template(prompt_parts.user_template, mapping)
    temp = float(profile.get("temperature", 0.7))
    raw, called = _cached_llm(
        cfg, db, prompt_parts.full_template, system=system, user=user, temperature=temp,
        label=f"personality | perfil={pid} | {clip_text(str(profile.get('name') or pid), 40)}",
    )
    return raw.strip(), called


def judge_article_body(
    cfg: "AppConfig",
    db: "StateDB",
    catalog: ArticleCatalog,
    body: str,
) -> tuple[dict, bool]:
    """Run judge LLM. Returns (scores_dict with verdict/feedback, llm_called)."""
    art_cfg = cfg.retrieval.article
    prompt_parts = load_prompt_parts(cfg.prompts_path / "article_judge.md")
    mapping = {
        "language": cfg.language,
        "topic": catalog.topic,
        "style": catalog.style,
        "notes_catalog": _format_notes_for_outline(catalog),
        "article_body": body,
    }
    system = fill_template(prompt_parts.system, mapping) if prompt_parts.system else ""
    user = fill_template(prompt_parts.user_template, mapping)
    raw, called = _cached_llm(
        cfg, db, prompt_parts.full_template, system=system, user=user,
        temperature=art_cfg.judge_temperature,
        label=f"judge | tema={clip_text(catalog.topic)} | estilo={catalog.style}",
    )
    data = json.loads(extract_json(raw))
    fidelity = float(data.get("fidelity") or 0)
    coverage = float(data.get("coverage") or 0)
    references = float(data.get("references") or 0)
    naturalness = float(data.get("naturalness") or 0)
    average = data.get("average")
    if average is None:
        average = (fidelity + coverage + references + naturalness) / 4.0
    else:
        average = float(average)
    verdict = str(data.get("verdict") or "REJECTED").upper()
    if average < art_cfg.judge_min_score:
        verdict = "REJECTED"
    elif verdict not in ("APPROVED", "REJECTED"):
        verdict = (
            "APPROVED" if average >= art_cfg.judge_min_score else "REJECTED"
        )
    return {
        "fidelity": fidelity,
        "coverage": coverage,
        "references": references,
        "naturalness": naturalness,
        "average": average,
        "verdict": verdict,
        "feedback": str(data.get("feedback") or ""),
    }, called
