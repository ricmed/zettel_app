"""Project an approved slice of the vault as a flat Agent Skill.

This is a **projection**, not a second pipeline: no LLM call, no new state. It
reads what `review`, `connect` and `garden` already approved and renders it in the
layout a coding agent can route through — a small `SKILL.md` that is always loaded
plus per-note files it opens on demand.

Flat on purpose (one routing level, no child skills): the progressive-disclosure
literature treats flat as the default to beat, and a nested pack would spend
context on navigation the note graph already encodes.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.state import StateDB
from zettel.time import vault_date_iso
from zettel.topic_index import TermEntry, TermSource, build_term_map
from zettel.vault import _slug, parse_frontmatter

logger = logging.getLogger(__name__)


class SkillExportError(RuntimeError):
    """The requested slice cannot be exported (missing, ambiguous, or empty)."""


# `SKILL.md` is loaded into every session that triggers the skill, so it is the
# one file whose size is a running cost rather than an on-demand one.
SKILL_TOKEN_BUDGET = 4000
DEFAULT_SKILL_ROOT = Path(".claude") / "skills"
_NOTE_ID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_THESIS_RE = re.compile(r"^>\s*\*\*Tese\*\*:\s*(.+?)\s*$", re.MULTILINE)


def estimate_tokens(text: str) -> int:
    """Chars/4, the same rough estimator the cost layer uses."""
    return max(0, len(text or "")) // 4


# ── Slice model ────────────────────────────────────────────────────────


@dataclass
class SkillNote:
    """One note as it will appear in the pack."""

    note_id: str
    kind: str  # "permanent" | "literature"
    title: str
    thesis: str
    body: str
    citekey: str = ""
    locator: str = ""
    tags: list[str] = field(default_factory=list)
    limits: str = ""
    decision_rules: list[str] = field(default_factory=list)
    anti_patterns: list[str] = field(default_factory=list)
    named_frameworks: list[str] = field(default_factory=list)
    relevance: int = 3
    degree: float = 0.0
    filename: str = ""

    @property
    def rank(self) -> tuple[float, str]:
        """Sort key: strongest first, ties broken by id so runs are reproducible."""
        return (-(self.relevance + self.degree), self.note_id)


@dataclass
class SkillPack:
    """Everything the renderer needs; no database access below this point."""

    slug: str
    title: str
    origin: str
    notes: list[SkillNote] = field(default_factory=list)
    contradictions: list[tuple[str, str]] = field(default_factory=list)
    generated_on: str = ""
    include_excerpts: bool = False


# ── Slice resolution ───────────────────────────────────────────────────


def resolve_slice(
    cfg: AppConfig,
    db: StateDB,
    *,
    source_id: str | None = None,
    moc_id: str | None = None,
    topic: str | None = None,
) -> tuple[str, str, list[dict]]:
    """Resolve one selector into ``(title, origin_label, note_rows)``."""
    selectors = [bool(source_id), bool(moc_id), bool(topic)]
    if sum(selectors) != 1:
        raise SkillExportError("Informe exatamente um seletor: --source-id, --moc-id ou --topic.")
    if source_id:
        return _slice_from_source(db, source_id)
    if moc_id:
        return _slice_from_moc(db, moc_id)
    return _slice_from_topic(cfg, db, topic or "")


def _slice_from_source(db: StateDB, source_id: str) -> tuple[str, str, list[dict]]:
    sid = source_id if source_id.startswith("@") else f"@{source_id}"
    source = db.get_source(sid)
    if not source:
        raise SkillExportError(f"Fonte nao encontrada: {sid}")
    rows = [note for note in db.get_notes_for_source(sid) if _is_permanent(note)]
    return source.get("title") or sid, f"fonte {sid}", rows


def _slice_from_moc(db: StateDB, moc_id: str) -> tuple[str, str, list[dict]]:
    moc = db.get_moc(moc_id)
    if not moc:
        raise SkillExportError(f"MOC nao encontrado: {moc_id}")
    note_ids = sorted(_NOTE_ID_RE.findall(moc.get("body") or ""))
    rows = [n for n in (db.get_note(nid) for nid in note_ids) if n]
    return moc.get("topic") or moc_id, f"MOC {moc_id}", rows


def _slice_from_topic(cfg: AppConfig, db: StateDB, topic: str) -> tuple[str, str, list[dict]]:
    wanted = topic.strip().lower()
    if not wanted:
        raise SkillExportError("--topic vazio.")

    matches = [
        moc
        for moc in db.list_mocs()
        if wanted in (moc.get("topic") or "").lower() or (moc.get("topic") or "").lower() in wanted
    ]
    if not matches:
        known = sorted({moc.get("topic") or "" for moc in db.list_mocs()} - {""})
        listing = ", ".join(known) if known else "(nenhum MOC no vault)"
        raise SkillExportError(
            f"Nenhum MOC casa com o topico '{topic}'. Topicos disponiveis: {listing}"
        )

    topics = {moc.get("topic") or "" for moc in matches}
    if len(topics) > 1:
        raise SkillExportError(
            f"Topico '{topic}' e ambiguo. Candidatos: {', '.join(sorted(topics))}. "
            "Repita com o nome completo da categoria."
        )

    note_ids: list[str] = []
    for moc in matches:
        for nid in _NOTE_ID_RE.findall(moc.get("body") or ""):
            if nid not in note_ids:
                note_ids.append(nid)
    rows = [n for n in (db.get_note(nid) for nid in sorted(note_ids)) if n]
    return topics.pop() or topic, f"topico '{topic}'", rows


def _is_permanent(note: dict) -> bool:
    return "30_Permanent" in (note.get("path") or "").replace("\\", "/")


# ── Note loading ───────────────────────────────────────────────────────


def load_notes(
    cfg: AppConfig,
    db: StateDB,
    note_rows: list[dict],
    source_id: str | None = None,
) -> list[SkillNote]:
    """Turn note rows into `SkillNote`s, falling back to approved LIT when empty.

    The fallback only applies to a source slice: a MOC or a topic *is* a set of
    permanent notes, so an empty one means the slice is empty, not that we should
    reach for literature notes belonging to some other source.
    """
    from zettel.config import DEFAULT_RELATION_WEIGHTS

    if not note_rows and source_id:
        return _literature_notes(db, source_id)
    if not note_rows:
        return []

    degrees = db.get_weighted_note_degrees(DEFAULT_RELATION_WEIGHTS)
    concepts = db.get_concepts_for_notes([r["note_id"] for r in note_rows])

    notes: list[SkillNote] = []
    for row in note_rows:
        note_id = row["note_id"]
        meta = _frontmatter(row)
        candidate = _candidate(concepts.get(note_id))
        body = row.get("body") or ""
        notes.append(
            SkillNote(
                note_id=note_id,
                kind="permanent",
                title=row.get("title") or meta.get("title") or note_id,
                thesis=_thesis_from_body(body),
                body=body,
                citekey=str(meta.get("source_id") or row.get("source_id") or ""),
                locator=str(meta.get("source_locator") or ""),
                tags=[str(t) for t in (meta.get("tags") or [])],
                limits=_section(body, "Limites"),
                decision_rules=_judgement(meta, candidate, "decision_rules"),
                anti_patterns=_judgement(meta, candidate, "anti_patterns"),
                named_frameworks=_judgement(meta, candidate, "named_frameworks"),
                relevance=int(candidate.get("relevance_score") or 3),
                degree=float(degrees.get(note_id, 0.0)),
            )
        )
    return _with_filenames(sorted(notes, key=lambda n: n.rank))


def _literature_notes(db: StateDB, source_id: str) -> list[SkillNote]:
    """Approved granular LIT notes, used when a source has no permanent note yet."""
    source = db.get_source(source_id) or {}
    citekey = source.get("citekey") or source_id
    notes: list[SkillNote] = []
    for status in ("approved", "persisted"):
        for chunk in db.get_chunks_by_status(status, source_id):
            summary = _json(chunk.get("summary_json"))
            candidates = summary.get("candidates") or []
            if not candidates:
                continue
            best = max(candidates, key=lambda c: c.get("relevance_score") or 0)
            path = chunk.get("literature_note_path")
            body = Path(path).read_text(encoding="utf-8") if path and Path(path).is_file() else ""
            notes.append(
                SkillNote(
                    note_id=chunk.get("literature_id") or chunk["chunk_id"],
                    kind="literature",
                    title=(summary.get("summary") or best.get("thesis") or "")[:100],
                    thesis=str(best.get("thesis") or ""),
                    body=parse_frontmatter(body)[1] if body else "",
                    citekey=f"@{citekey}",
                    locator=str(best.get("source_locator") or ""),
                    tags=[str(t) for t in (best.get("tags") or [])],
                    limits=str(best.get("limits") or ""),
                    decision_rules=list(best.get("decision_rules") or []),
                    anti_patterns=list(best.get("anti_patterns") or []),
                    named_frameworks=list(best.get("named_frameworks") or []),
                    relevance=int(best.get("relevance_score") or 3),
                )
            )
    return _with_filenames(sorted(notes, key=lambda n: n.rank))


def load_contradictions(db: StateDB, notes: list[SkillNote]) -> list[tuple[str, str]]:
    """`contradicts` edges inside the slice — the tension a cheatsheet should show."""
    titles = {n.note_id: n.title for n in notes}
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for edge in db.get_connections_for_notes(list(titles)):
        if (edge.get("relation_type") or "") != "contradicts":
            continue
        a, b = edge.get("source_note_id"), edge.get("target_note_id")
        if a not in titles or b not in titles:
            continue
        key = tuple(sorted((a, b)))
        if key in seen:
            continue
        seen.add(key)
        pairs.append((titles[key[0]], titles[key[1]]))
    return sorted(pairs)


# ── Rendering ──────────────────────────────────────────────────────────


def build_pack(
    *,
    slug: str,
    title: str,
    origin: str,
    notes: list[SkillNote],
    contradictions: list[tuple[str, str]],
    include_excerpts: bool = False,
    generated_on: str | None = None,
    vault_timezone: str = "America/Sao_Paulo",
) -> SkillPack:
    return SkillPack(
        slug=slug,
        title=title,
        origin=origin,
        notes=notes,
        contradictions=contradictions,
        generated_on=generated_on or vault_date_iso(vault_timezone),
        include_excerpts=include_excerpts,
    )


def render_skill_md(pack: SkillPack) -> str:
    """`SKILL.md`: frontmatter, usage, core theses, topic index, note index.

    Only the Core is budgeted. The two indexes are the routing table — dropping
    rows from them would make notes unreachable, which is worse than a longer
    file. A slice big enough to push the indexes past the budget is a slice that
    wants a narrower selector, and the CLI reports the estimate so that shows up.
    """
    terms = build_term_map(
        [
            TermSource(
                note_id=n.note_id,
                label=n.filename,
                frameworks=tuple(n.named_frameworks),
                tags=tuple(n.tags),
                thesis=n.thesis,
            )
            for n in pack.notes
        ]
    )
    header, topics, notes = _skill_header(pack), _topic_index(terms), _note_index(pack)
    fixed_tokens = estimate_tokens(header + topics + notes)
    core = _core_section(pack, budget=max(0, SKILL_TOKEN_BUDGET - fixed_tokens))
    return header + core + topics + notes


def _skill_header(pack: SkillPack) -> str:
    description = (
        f"Acervo Zettel derivado de {pack.title}. Use ao aplicar {_description_terms(pack)}."
    )
    frontmatter = json.dumps(description, ensure_ascii=False)
    return (
        "---\n"
        f"name: {pack.slug}\n"
        f"description: {frontmatter}\n"
        "---\n\n"
        f"# {pack.title}\n\n"
        f"**Origem**: Zettel export | **Recorte**: {pack.origin} | "
        f"**Gerado**: {pack.generated_on}\n\n"
        "## How to Use This Skill\n\n"
        "- Sem argumentos: leia os frameworks e teses do Core abaixo.\n"
        "- Com um tópico: abra o arquivo indicado no Topic Index.\n"
        '- "quais notas você tem?": liste o Note Index.\n'
        "- Regras de decisão e tensões: `cheatsheet.md`. Termos: `glossary.md`.\n\n"
    )


def _description_terms(pack: SkillPack) -> str:
    terms: list[str] = []
    for note in pack.notes:
        for term in [*note.named_frameworks, *note.tags]:
            clean = str(term).strip().lstrip("#").replace("_", " ")
            if clean and clean not in terms:
                terms.append(clean)
        if len(terms) >= 6:
            break
    return ", ".join(terms[:6]) if terms else pack.title


def _core_section(pack: SkillPack, budget: int) -> str:
    """Theses of the strongest notes, front-loaded until the budget runs out.

    Front-loading matters more than completeness here: what does not fit is not
    lost, it is one file-open away through the Note Index below.
    """
    lines = ["## Core Frameworks & Mental Models\n"]
    spent = estimate_tokens(lines[0])
    omitted = 0
    for note in pack.notes:
        if not note.thesis:
            continue
        entry = f"- **{note.title}** — {note.thesis}"
        if note.named_frameworks:
            entry += f" <sub>{', '.join(note.named_frameworks)}</sub>"
        entry += "\n"
        cost = estimate_tokens(entry)
        if spent + cost > budget:
            omitted += 1
            continue
        lines.append(entry)
        spent += cost
    if len(lines) == 1:
        lines.append("_Nenhuma tese registrada neste recorte._\n")
    if omitted:
        lines.append(
            f"\n_{omitted} nota(s) fora do Core por orçamento de contexto — veja o Note Index._\n"
        )
    return "".join(lines) + "\n"


def _topic_index(terms: list[TermEntry]) -> str:
    if not terms:
        return "## Topic Index\n\n_Nenhum termo indexado._\n\n"
    lines = ["## Topic Index\n"]
    for entry in terms:
        targets = ", ".join(f"`{label}`" for label in entry.labels)
        lines.append(f"- **{entry.term}** -> {targets}\n")
    return "".join(lines) + "\n"


def _note_index(pack: SkillPack) -> str:
    lines = [
        "## Note Index\n",
        "| Nota | Tese | Localizador | Arquivo |\n",
        "| --- | --- | --- | --- |\n",
    ]
    for note in pack.notes:
        lines.append(
            f"| {_cell(note.title)} | {_cell(note.thesis)} | "
            f"{_cell(note.locator or note.citekey)} | `{note.filename}` |\n"
        )
    return "".join(lines)


def render_note_file(note: SkillNote, pack: SkillPack) -> str:
    """One note as a standalone file the agent can open."""
    body = note.body if pack.include_excerpts else _drop_excerpt(note.body)
    head = f"# {note.title}\n\n"
    provenance = []
    if note.citekey:
        provenance.append(f"**Fonte**: {note.citekey}")
    if note.locator:
        provenance.append(f"**Localizador**: {note.locator}")
    if provenance:
        head += " | ".join(provenance) + "\n\n"
    return head + body.strip() + "\n"


def render_cheatsheet(pack: SkillPack) -> str:
    """Decision rules, anti-patterns, limits and contradictions — the judgement layer."""
    intro = (
        "Regras e tensões extraídas das notas deste recorte. "
        "Nada aqui é inferido: cada item foi enunciado na fonte.\n\n"
    )
    lines = [f"# Cheatsheet — {pack.title}\n\n", intro]

    rules = _collect(pack.notes, "decision_rules")
    lines.append("## Regras de decisão\n\n")
    lines.append(_bullets(rules, "_Nenhuma regra registrada neste recorte._"))

    anti = _collect(pack.notes, "anti_patterns")
    lines.append("\n## Anti-padrões\n\n")
    lines.append(_bullets(anti, "_Nenhum anti-padrão registrado neste recorte._"))

    lines.append("\n## Limites\n\n")
    limits = [f"**{n.title}** — {n.limits.strip()}" for n in pack.notes if n.limits.strip()]
    lines.append(_bullets(limits, "_Nenhum limite registrado._"))

    lines.append("\n## Tensões (contradicts)\n\n")
    tensions = [f"{a} <-> {b}" for a, b in pack.contradictions]
    lines.append(_bullets(tensions, "_Nenhuma contradição registrada no grafo._"))
    return "".join(lines)


def render_glossary(pack: SkillPack) -> str:
    """Terms the reader may search for, with the note that defines each one."""
    lines = [f"# Glossário — {pack.title}\n\n"]
    entries: dict[str, str] = {}
    for note in pack.notes:
        for term in [*note.named_frameworks, *note.tags]:
            clean = str(term).strip().lstrip("#").replace("_", " ")
            if clean and clean not in entries:
                entries[clean] = f"{note.thesis or note.title} (`{note.filename}`)"
    if not entries:
        return lines[0] + "_Nenhum termo registrado neste recorte._\n"
    for term in sorted(entries, key=str.lower):
        lines.append(f"- **{term}** — {entries[term]}\n")
    return "".join(lines)


# ── Writing ────────────────────────────────────────────────────────────


def write_pack(pack: SkillPack, pack_dir: Path, *, overwrite: bool = False) -> Path:
    if pack_dir.exists() and any(pack_dir.iterdir()):
        if not overwrite:
            raise SkillExportError(
                f"Destino ja existe e nao esta vazio: {pack_dir}. Use --overwrite para regenerar."
            )
        for existing in sorted(pack_dir.rglob("*"), reverse=True):
            existing.unlink() if existing.is_file() else existing.rmdir()

    (pack_dir / "notes").mkdir(parents=True, exist_ok=True)
    (pack_dir / "SKILL.md").write_text(render_skill_md(pack), encoding="utf-8")
    (pack_dir / "cheatsheet.md").write_text(render_cheatsheet(pack), encoding="utf-8")
    (pack_dir / "glossary.md").write_text(render_glossary(pack), encoding="utf-8")
    for note in pack.notes:
        (pack_dir / note.filename).write_text(render_note_file(note, pack), encoding="utf-8")
    return pack_dir


def run_skill_export(
    cfg: AppConfig,
    db: StateDB,
    *,
    source_id: str | None = None,
    moc_id: str | None = None,
    topic: str | None = None,
    out: Path | None = None,
    slug: str | None = None,
    overwrite: bool = False,
    include_excerpts: bool = False,
) -> tuple[Path, SkillPack]:
    """Export one vault slice as a flat Agent Skill. Deterministic, no LLM."""
    title, origin, rows = resolve_slice(
        cfg,
        db,
        source_id=source_id,
        moc_id=moc_id,
        topic=topic,
    )
    notes = load_notes(cfg, db, rows, source_id=source_id)
    if not notes:
        raise SkillExportError(f"Recorte vazio ({origin}): nenhuma nota aprovada para exportar.")

    pack = build_pack(
        slug=slug or _default_slug(source_id, moc_id, topic, title),
        title=title,
        origin=origin,
        notes=notes,
        contradictions=load_contradictions(db, notes),
        include_excerpts=include_excerpts,
        vault_timezone=cfg.vault_timezone,
    )
    root = out or (cfg.vault_path / DEFAULT_SKILL_ROOT)
    pack_dir = write_pack(pack, root / pack.slug, overwrite=overwrite)
    logger.info("Skill exportada: %s (%d notas)", pack_dir, len(pack.notes))
    return pack_dir, pack


# ── Helpers ────────────────────────────────────────────────────────────


def _default_slug(
    source_id: str | None,
    moc_id: str | None,
    topic: str | None,
    title: str,
) -> str:
    if source_id:
        return _slug(source_id.lstrip("@"), 60)
    if topic:
        return _slug(topic, 60)
    return _slug(title or moc_id or "skill", 60)


def _with_filenames(notes: list[SkillNote]) -> list[SkillNote]:
    """Assign a stable, readable `notes/` filename to each note."""
    taken: set[str] = set()
    for note in notes:
        base = _slug(note.title or note.thesis or note.note_id, 50) or "nota"
        name = f"{base}-{note.note_id[-6:].lower()}"
        suffix = 2
        while name in taken:
            name = f"{base}-{note.note_id[-6:].lower()}-{suffix}"
            suffix += 1
        taken.add(name)
        note.filename = f"notes/{name}.md"
    return notes


def _frontmatter(row: dict) -> dict[str, Any]:
    return _json(row.get("frontmatter_json"))


def _json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _candidate(concept_row: dict | None) -> dict[str, Any]:
    return _json((concept_row or {}).get("candidate_json"))


def _judgement(meta: dict, candidate: dict, key: str) -> list[str]:
    """Frontmatter wins; the concept row is the fallback for older notes."""
    values = meta.get(key) or candidate.get(key) or []
    return [str(v) for v in values if str(v).strip()]


def _thesis_from_body(body: str) -> str:
    match = _THESIS_RE.search(body or "")
    return match.group(1).strip() if match else ""


def _section(body: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(body or "")
    return match.group(1).strip() if match else ""


def _drop_excerpt(body: str) -> str:
    """Remove the source excerpt so a published pack carries interpretation only."""
    cleaned = re.sub(
        r"<!--\s*zettel:auto-source-excerpt:start\s*-->.*?"
        r"<!--\s*zettel:auto-source-excerpt:end\s*-->",
        "_(trecho da fonte omitido no export)_",
        body or "",
        flags=re.DOTALL,
    )
    return cleaned


def _collect(notes: list[SkillNote], key: str) -> list[str]:
    """Judgement items across the slice, deduplicated, note order preserved."""
    seen: dict[str, None] = {}
    for note in notes:
        for item in getattr(note, key):
            text = str(item).strip()
            if text:
                seen.setdefault(text, None)
    return list(seen)


def _bullets(items: list[str], empty: str) -> str:
    if not items:
        return f"{empty}\n"
    return "".join(f"- {item}\n" for item in items)


def _cell(text: str) -> str:
    """Table-cell-safe one-liner."""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip() or "-"
