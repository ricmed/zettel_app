"""Adoption of hand-written granular literature notes into the pipeline stores.

A LIT note the user wrote by hand in Obsidian has no ``chunks`` row, so before this
module it was invisible to SQLite, to the ``literature_notes`` collection, to the
source's literature index and to ``connect``. Adoption synthesizes that row (plus a
per-source ``Manual`` chapter, required by the NOT NULL FK on ``chunks.chapter_id``)
and then reuses the exact same downstream steps as ``review.approve_chunk``.

Manual notes are the user's own content, so they never pass through the confidence
gate: they land as ``persisted`` on first sight.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.hashing import normalize_text_for_hash, sha256_hex, short_hash
from zettel.index import VectorIndex
from zettel.schemas import PermanentNoteCandidate
from zettel.state import StateDB
from zettel.vault import compose_note, read_managed_block

logger = logging.getLogger(__name__)

MANUAL_CHAPTER_SUFFIX = "::ch000"
MANUAL_CHAPTER_TITLE = "Manual"


def _ztl_llm_rejected_message(reason: str) -> str:
    """Web/CLI copy when Prompt 2 declines a LIT→ZTL candidate."""
    reason = (reason or "").strip()
    head = "O modelo recusou gerar a nota permanente"
    if reason:
        head = f"{head}: {reason}"
    else:
        head = f"{head}."
    return (
        f"{head} Enriqueça o resumo ou a tese da LIT e tente de novo, "
        "ou crie a ZTL sem o LLM."
    )


_PLACEHOLDERS = {
    "_preencha o resumo._",
    "_preencha a tese._",
    "_preencha a definicao._",
    "_cole o trecho da fonte aqui._",
    "_trecho nao disponivel._",
    "_sem resumo._",
    "_nenhum._",
    "_nenhum candidato._",
    "_nenhuma._",
}


def ensure_manual_chapter(db: StateDB, source_id: str) -> str:
    """Create (idempotently) the synthetic chapter that holds manual chunks."""
    chapter_id = f"{source_id}{MANUAL_CHAPTER_SUFFIX}"
    db.upsert_chapter(
        chapter_id=chapter_id,
        source_id=source_id,
        title=MANUAL_CHAPTER_TITLE,
        chapter_checksum=sha256_hex(chapter_id),
        locator="",
    )
    return chapter_id


# -- Body parsing -------------------------------------------------------


def _section(body: str, heading: str) -> str:
    """Return the text under a `## heading`, empty when absent or a placeholder."""
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(body)
    if not match:
        return ""
    text = match.group(1).strip()
    return "" if text.lower() in _PLACEHOLDERS else text


def _excerpt(content: str) -> str:
    """Source excerpt from the auto-source-excerpt managed block."""
    block = read_managed_block(content, "auto-source-excerpt")
    text = (block or "").strip()
    return "" if text.lower() in _PLACEHOLDERS else text


def _key_concepts(body: str) -> list[str]:
    raw = _section(body, "Conceitos-chave")
    return [tag.lstrip("#") for tag in re.findall(r"#([\w/-]+)", raw)]


_SUB_SUFFIX_RE = re.compile(r"\s*<sub>.*?</sub>\s*$")


def _candidate_theses(body: str) -> list[str]:
    """Thesis text of each checklist line under 'Candidatos a Nota Permanente'.

    Tolerates the pipeline's own rendering (``- [ ] **thesis** <sub>relevancia
    N/5 . locator</sub>``) by stripping the trailing metadata `<sub>` and
    surrounding `**` bold markers, so a note that started as a pipeline draft
    and was later hand-edited into ``origin: manual`` still adopts a clean
    thesis rather than one polluted with markdown/metadata.
    """
    raw = _section(body, "Candidatos a Nota Permanente")
    theses = []
    for line in re.findall(r"^\s*-\s*\[[ xX]\]\s*(.+)$", raw, re.MULTILINE):
        line = _SUB_SUFFIX_RE.sub("", line).strip().strip("*").strip()
        if line:
            theses.append(line)
    return theses


def summary_payload(body: str, content: str) -> dict[str, Any]:
    """Rebuild the `chunks.summary_json` payload from a hand-written LIT body."""
    excerpt = _excerpt(content)
    return {
        "summary": _section(body, "Resumo"),
        "key_concepts": _key_concepts(body),
        "chunk_status": "ok",
        "candidates": [
            {"thesis": thesis, "definition": "", "anchor_quote": excerpt}
            for thesis in _candidate_theses(body)
        ],
    }


def build_candidate_from_literature(
    meta: dict[str, Any],
    body: str,
    content: str,
    *,
    thesis_override: str | None = None,
) -> PermanentNoteCandidate:
    """Derive a permanent-note candidate from a literature note the user wrote."""
    from zettel.paging import format_source_locator

    summary = _section(body, "Resumo")
    theses = _candidate_theses(body)
    thesis = thesis_override or (theses[0] if theses else "")
    if not thesis and summary:
        thesis = summary.split("\n\n")[0].strip()
    if not thesis:
        raise ValueError(
            "Nao foi possivel derivar uma tese da nota de literatura: preencha "
            "'## Resumo' ou '## Candidatos a Nota Permanente', ou informe --thesis."
        )
    return PermanentNoteCandidate(
        thesis=thesis,
        definition=summary or thesis,
        anchor_quote=_excerpt(content),
        source_locator=format_source_locator(
            meta.get("page_in_book"),
            str(meta.get("section_path") or ""),
            meta.get("page_in_file"),
        ),
        tags=_key_concepts(body),
        relevance_score=5,
    )


def concept_id_for(source_id: str, chunk_id: str, thesis: str) -> str:
    """Deterministic concept id for a manually authored candidate."""
    digest = sha256_hex(f"{source_id}|{chunk_id}|{normalize_text_for_hash(thesis)}")
    return f"{source_id}::concept::{short_hash(digest)}"


_WIKILINK_TARGET = re.compile(r"\[\[([^\]|#]+)")
_TESE_LINE = re.compile(r"^>\s*\*\*Tese\*\*:\s*(.+)$", re.MULTILINE)


def wikilink_target(ref: str) -> str:
    """Strip ``[[...]]`` / alias from a wikilink, leaving the path target."""
    match = _WIKILINK_TARGET.search(ref or "")
    return (match.group(1) if match else (ref or "")).strip()


def thesis_from_permanent_note(meta: dict[str, Any], body: str) -> str:
    """Thesis line from a ZTL body (``> **Tese**: ...``), else frontmatter title."""
    match = _TESE_LINE.search(body or "")
    if match:
        return match.group(1).strip()
    return str(meta.get("title") or "").strip()


def _chunk_ref_targets(chunk: dict[str, Any] | None) -> set[str]:
    """Identifiers a ``literature_ref`` may use to point at this chunk's LIT."""
    targets: set[str] = set()
    if not chunk:
        return targets
    on_disk = chunk.get("literature_note_path")
    if on_disk:
        path = Path(on_disk)
        targets.add(f"{path.parent.name}/{path.stem}")
        targets.add(path.stem)
    lit_id = chunk.get("literature_id")
    if lit_id:
        targets.add(str(lit_id))
    return targets


def _lit_ref_covers_chunk(lit_ref: str, chunk: dict[str, Any] | None) -> bool:
    if not lit_ref or not chunk:
        return False
    targets = _chunk_ref_targets(chunk)
    if lit_ref in targets:
        return True
    stem = lit_ref.rsplit("/", 1)[-1]
    return any(t == stem or t.rsplit("/", 1)[-1] == stem for t in targets)


def claim_concepts_for_note(
    db: StateDB, note_id: str, meta: dict[str, Any], body: str,
) -> int:
    """Mark unnoted concepts covered by this permanent note as ``noted``.

    Matching is structural, not semantic: same ``thesis_hash``, or a manual note
    whose ``literature_ref`` points at the concept's chunk. Does not write the
    note file and does not change ``origin``.
    """
    source_id = str(meta.get("source_id") or "")
    if not note_id or not source_id:
        return 0
    origin = str(meta.get("origin") or "manual")
    thesis = thesis_from_permanent_note(meta, body)
    thesis_hash = sha256_hex(normalize_text_for_hash(thesis)) if thesis else ""
    lit_ref = wikilink_target(str(meta.get("literature_ref") or ""))
    claimed = 0
    for concept in db.get_concepts_for_source(source_id, without_notes=True):
        matched = bool(thesis_hash and concept.get("thesis_hash") == thesis_hash)
        if not matched and origin == "manual" and lit_ref:
            chunk = db.get_chunk(concept["chunk_id"])
            matched = _lit_ref_covers_chunk(lit_ref, chunk)
        if not matched:
            continue
        db.upsert_concept(
            concept["concept_id"],
            concept["source_id"],
            concept["chunk_id"],
            note_id=note_id,
            status="noted",
        )
        claimed += 1
        logger.info(
            "Conceito %s coberto pela nota %s; marcado noted",
            concept["concept_id"], note_id,
        )
    return claimed


def find_covering_note_id(
    db: StateDB, *, source_id: str, chunk_id: str, thesis: str,
) -> str | None:
    """Return an existing permanent note that already covers this candidate."""
    thesis_hash = sha256_hex(normalize_text_for_hash(thesis)) if thesis else ""
    chunk = db.get_chunk(chunk_id) if chunk_id else None

    if chunk_id:
        for concept in db.get_concepts_for_chunk(chunk_id):
            nid = concept.get("note_id")
            if not nid or not thesis_hash:
                continue
            if concept.get("thesis_hash") == thesis_hash and db.get_note(nid):
                return nid

    if not source_id:
        return None
    for note in db.get_notes_for_source(source_id):
        try:
            meta = json.loads(note.get("frontmatter_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        origin = str(note.get("origin") or meta.get("origin") or "")
        note_thesis = thesis_from_permanent_note(meta, note.get("body") or "")
        if thesis_hash and note_thesis:
            if sha256_hex(normalize_text_for_hash(note_thesis)) == thesis_hash:
                return note["note_id"]
        lit_ref = wikilink_target(str(meta.get("literature_ref") or ""))
        if origin == "manual" and _lit_ref_covers_chunk(lit_ref, chunk):
            return note["note_id"]
    return None


# -- Adoption -----------------------------------------------------------


def adopt_manual_literature(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    file_path: Path,
    meta: dict[str, Any],
    body: str,
) -> str:
    """Register a hand-written granular LIT note in SQLite, Chroma and the index.

    Returns 'new', 'updated' or 'skipped'. Idempotent: a note whose excerpt and body
    are unchanged since the last adoption is skipped without re-embedding.
    """
    from zettel.assets import adopt_vault_images
    from zettel.review import _literature_embed_text, _refresh_literature_index

    chunk_id = str(meta.get("chunk_id") or "")
    source_id = str(meta.get("source_id") or "")
    if not chunk_id or not source_id:
        return "skipped"

    source = db.get_source(source_id)
    if not source:
        return "skipped"
    citekey = source["citekey"]

    chapter_id = ensure_manual_chapter(db, source_id)

    # Images the author pasted into the note: copy into 90_Assets, rewrite refs.
    new_body, adopted = adopt_vault_images(
        cfg, db, source_id, chapter_id, file_path, body,
        page_in_file=meta.get("page_in_file"),
    )
    if adopted and new_body != body:
        file_path.write_text(compose_note(meta, new_body), encoding="utf-8")
        body = new_body

    content = file_path.read_text(encoding="utf-8")
    excerpt = _excerpt(content)
    chunk_checksum = sha256_hex(normalize_text_for_hash(f"{excerpt}\n{body}"))

    existing = db.get_chunk(chunk_id)
    if existing and existing.get("chunk_checksum") == chunk_checksum:
        return "skipped"

    literature_id = meta.get("literature_id") or chunk_id
    db.upsert_chunk(
        chunk_id=chunk_id,
        source_id=source_id,
        chapter_id=chapter_id,
        text=excerpt,
        chunk_checksum=chunk_checksum,
        locator=str(meta.get("section_path") or ""),
        status="persisted",
        section_path=str(meta.get("section_path") or ""),
        chunk_index=meta.get("chunk_index"),
        page_in_file=meta.get("page_in_file"),
        page_in_book=meta.get("page_in_book"),
        page_confidence=str(meta.get("page_confidence") or "unknown"),
        literature_note_path=str(file_path),
        literature_id=literature_id,
    )
    # upsert_chunk COALESCEs summary_json, so an edited body needs an explicit update.
    db.update_chunk_review(
        chunk_id,
        status="persisted",
        literature_note_path=str(file_path),
        literature_id=literature_id,
        summary_json=json.dumps(summary_payload(body, content), ensure_ascii=False),
    )

    idx.upsert_literature_note(
        literature_id,
        _literature_embed_text(file_path),
        {
            "source_id": source_id,
            "chunk_id": chunk_id,
            "citekey": citekey,
            "path": str(file_path.relative_to(cfg.vault_path)).replace("\\", "/"),
            "chunk_index": int(meta.get("chunk_index") or 0),
            "page_in_book": meta.get("page_in_book") or -1,
        },
    )

    _refresh_literature_index(cfg, db, source_id)
    logger.info(
        "[NOTE=%s] LIT manual adotada -> chunks + literature_notes", file_path.name,
    )
    return "updated" if existing else "new"


# -- Literature note -> permanent note ----------------------------------


def resolve_literature_note(db: StateDB, ref: str) -> tuple[Path, dict[str, Any], str, str]:
    """Resolve a LIT note from a file path or a chunk_id.

    Returns ``(path, frontmatter, body, raw_content)``.
    """
    from zettel.vault import parse_frontmatter

    path = Path(ref)
    if not path.is_file():
        chunk = db.get_chunk(ref)
        raw = (chunk or {}).get("literature_note_path")
        path = Path(raw) if raw else path
    if not path.is_file():
        raise FileNotFoundError(
            f"Nota de literatura nao encontrada: {ref!r} "
            "(informe o caminho do arquivo ou um chunk_id ja indexado)."
        )
    content = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(content)
    if str(meta.get("type") or "") != "literature" or not meta.get("chunk_id"):
        raise ValueError(
            f"{path.name} nao e uma nota de literatura granular "
            "(precisa de type: literature e chunk_id no frontmatter)."
        )
    return path, meta, body, content


def create_permanent_from_literature(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    ref: str,
    *,
    use_llm: bool = False,
    thesis: str | None = None,
    force: bool = False,
) -> tuple[Path, bool]:
    """Create a permanent note from a literature note. Returns (path, used_llm).

    With ``use_llm`` the candidate goes through the connector's Prompt 2 — same RAG,
    same relation typing, same backlink maintenance as the pipeline — but stamped
    ``origin: manual``. Without it, a pre-filled scaffold is written for the user to
    complete and adopt later with ``sync-manual``. Neither path needs approval.
    """
    from zettel.connector import ConnectRejected, _literature_ref_for_chunk, run_connect
    from zettel.new_note import resolve_src_in_vault, source_wikilink
    from zettel.vault import (
        build_permanent_note_body,
        note_filename,
        safe_write_note,
    )

    path, meta, body, content = resolve_literature_note(db, ref)
    chunk_id = str(meta["chunk_id"])
    source_id = str(meta.get("source_id") or "")

    if db.get_chunk(chunk_id) is None:
        adopt_manual_literature(cfg, db, idx, path, meta, body)

    candidate = build_candidate_from_literature(
        meta, body, content, thesis_override=thesis,
    )
    source = db.get_source(source_id)
    citekey = source["citekey"] if source else str(meta.get("citekey") or "")
    title_src = source["title"] if source else ""
    literature_ref = _literature_ref_for_chunk(
        cfg, db, source_id, citekey, title_src, chunk_id,
    )

    if use_llm:
        concept_id = concept_id_for(source_id, chunk_id, candidate.thesis)
        db.upsert_concept(
            concept_id,
            source_id,
            chunk_id,
            anchor_hash=sha256_hex(normalize_text_for_hash(candidate.anchor_quote)),
            thesis_hash=sha256_hex(normalize_text_for_hash(candidate.thesis)),
            candidate_json=candidate.model_dump_json(),
            status="approved",
        )
        try:
            note_ids = run_connect(
                cfg, db, idx,
                [{
                    "concept_id": concept_id,
                    "source_id": source_id,
                    "chunk_id": chunk_id,
                    "candidate": candidate,
                }],
                origin="manual",
            )
        except ConnectRejected as exc:
            raise ConnectRejected(
                _ztl_llm_rejected_message(exc.reason),
                reason=exc.reason,
            ) from exc
        if not note_ids:
            # The concept row survives with status=approved, so a later connect
            # retries it without re-deriving anything.
            raise ConnectRejected(_ztl_llm_rejected_message(""))
        row = db.get_note(note_ids[0]) or {}
        return Path(row.get("path") or ""), True

    # Manual path: a pre-filled scaffold, indexed later by `sync-manual`.
    from ulid import ULID

    note_id = str(ULID())
    title = candidate.thesis[:100]
    src_path, _ = resolve_src_in_vault(cfg, source_id) if source_id else (None, None)
    source_ref = (
        source_wikilink(citekey, path=src_path, title=title_src) if citekey else ""
    )
    definition = candidate.definition if candidate.definition != candidate.thesis else ""
    note_body = build_permanent_note_body(
        thesis=candidate.thesis,
        definition=definition or "_Preencha a definicao._",
        intuition="",
        example="",
        limits="",
        connections=[],
        literature_ref=literature_ref,
        source_ref=source_ref,
        source_locator=candidate.source_locator,
    )
    if candidate.anchor_quote:
        note_body += f"\n## Trecho de apoio\n\n> {candidate.anchor_quote}\n"
    note_body += (
        "\n## Sugestoes de conexao\n\n"
        "<!-- zettel:auto-connections:start -->\n"
        "_Sincronize com sync-manual para sugestoes automaticas._\n"
        "<!-- zettel:auto-connections:end -->\n"
    )

    from datetime import datetime
    now = datetime.now().isoformat()
    note_meta = {
        "type": "permanent",
        "note_id": note_id,
        "title": title,
        "source_id": source_id,
        "literature_ref": literature_ref,
        "source_locator": candidate.source_locator,
        "tags": candidate.tags,
        "origin": "manual",
        "created_at": now,
        "updated_at": now,
    }
    note_path = cfg.vault_path / "30_Permanent" / note_filename("ZTL", note_id, title)
    if note_path.exists() and not force:
        raise FileExistsError(f"Arquivo ja existe: {note_path}")
    safe_write_note(note_path, note_meta, note_body)
    # Consume extract/review concepts that this scaffold already covers, so a
    # later `zettel connect` cannot mint a duplicate pipeline note.
    claim_concepts_for_note(db, note_id, note_meta, note_body)
    return note_path, False
