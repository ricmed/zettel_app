"""Sync manual — detect manual notes across all vault folders, index them.

Adopts notes hand-written in Obsidian (Sources, Literature, Permanent, MOCs) into
the pipeline: assigns ids/citekeys, marks them `origin: manual`, persists their
bodies into SQLite (retention) and indexes the embeddable ones into ChromaDB.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ulid import ULID

from zettel.config import AppConfig
from zettel.hashing import (
    compute_embedding_input_hash,
    extract_embeddable_text,
    normalize_text_for_hash,
    sha256_hex,
)
from zettel.index import VectorIndex
from zettel.retrieval import Retriever
from zettel.state import StateDB
from zettel.vault import (
    PIPELINE_ORIGINS,
    ZTL_LINK_RE,
    MalformedFrontmatter,
    _block_pattern,
    frontmatter_json,
    parse_frontmatter,
    parse_frontmatter_strict,
    permanent_wikilink,
    rewrite_bare_permanent_wikilinks,
    safe_update_managed_blocks,
)

logger = logging.getLogger(__name__)

# What a caller hands sync: the index, a callable that opens it, or None (no vectors).
IndexArg = VectorIndex | Callable[[], VectorIndex] | None


# Managed blocks whose wikilinks are auto-generated (suggestions / backlinks) and
# must NOT be treated as user-accepted connections.
_AUTO_BLOCKS_TO_SKIP = ("auto-connections", "auto-backlinks", "auto-moc-backrefs")


@dataclass
class _Run:
    """What one sync run learns as it scans, for the checks and the second pass (#347)."""

    # chunk_id -> the granular LIT that claimed it first in this run.
    claimed_chunks: dict[str, Path] = field(default_factory=dict)
    # Permanent notes whose body was stored in this run, and those new to SQLite.
    changed_notes: set[str] = field(default_factory=set)
    new_notes: set[str] = field(default_factory=set)


class NoteRejected(Exception):
    """A note sync cannot adopt as written. Reported per note; the file is left untouched."""


class _Vectors:
    """The vector index for one sync run: optional, opened on first use (#343).

    Sync writes SQLite and FTS first, which is all a note needs to exist. Without
    an embedding credential the index is never opened; a provider outage in the
    middle of the run stops further embeds, not the run. A note or source whose
    vector was not written counts as pending, and the next sync that reaches the
    index writes it without the note being edited.
    """

    def __init__(self, cfg: AppConfig, idx: IndexArg) -> None:
        self.cfg = cfg
        self._open = idx if callable(idx) else None
        self._idx = None if callable(idx) else idx
        self.offline_reason: str | None = None if idx is not None else "indice vetorial nao aberto"
        self.pending = 0

    def index(self) -> VectorIndex | None:
        """The open index, or ``None`` once the run is offline."""
        if self.offline_reason:
            return None
        if self._idx is None and self._open is not None:
            from zettel.credentials import embedding_ready

            if not embedding_ready(self.cfg):
                self.offline_reason = "sem credencial de embedding"
                return None
            self._idx, self._open = self._open(), None
        return self._idx

    def write(self, embed: Callable[[VectorIndex], object]) -> bool:
        """Run one embedding write; False (and one more pending) when it could not run."""
        idx = self.index()
        if idx is None:
            self.pending += 1
            return False
        try:
            embed(idx)
        except Exception as exc:
            from zettel.llm import is_llm_unavailable

            if not is_llm_unavailable(exc):
                raise
            self.offline_reason = f"embedding indisponivel ({type(exc).__name__}: {exc})"
            logger.warning("Sync manual segue sem embedding: %s", self.offline_reason)
            self.pending += 1
            return False
        return True


def run_sync_manual(cfg: AppConfig, db: StateDB, idx: IndexArg) -> dict[str, Any]:
    """Scan all four note folders for manual/modified notes and sync them.

    ``idx`` is the vector index, a zero-argument callable that opens it (so a run
    with nothing to embed, or without a credential, never does), or ``None`` for a
    SQLite-only run.

    Returns per-type counters, aggregate new/updated/skipped/failed,
    ``failures`` (``{"path", "reason"}`` per note that could not be adopted; one
    bad note never stops the others, #342), ``embedding_pending`` and
    ``embedding_offline`` (why vectors were not written, or ``None``; #343). Opens a
    ``sync`` run so the embedding cost of the notes it indexes is recorded (#311).
    """
    from zettel.usage import begin_run, finish_pipeline_run

    run_id = db.start_run("sync")
    begin_run(run_id)
    try:
        stats = _sync_all(cfg, db, _Vectors(cfg, idx))
    except Exception:
        finish_pipeline_run(db, run_id, status="failed")
        raise
    finish_pipeline_run(db, run_id)
    return stats


def _sync_all(cfg: AppConfig, db: StateDB, vectors: _Vectors) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "new": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "sources": 0,
        "literature": 0,
        "permanent": 0,
        "mocs": 0,
    }
    failures: list[dict[str, str]] = []
    run = _Run()

    dirs_to_scan = [
        (cfg.vault_path / "10_Sources", "source", "sources"),
        (cfg.vault_path / "20_Literature", "literature", "literature"),
        (cfg.vault_path / "30_Permanent", "permanent", "permanent"),
        (cfg.vault_path / "40_MOCs", "moc", "mocs"),
    ]

    for scan_dir, note_type, counter in dirs_to_scan:
        if not scan_dir.exists():
            continue
        # Literature may live in citekey subfolders (granular LITs). Sorted so a
        # duplicate chunk_id is always reported on the same file.
        pattern = "**/*.md" if note_type == "literature" else "*.md"
        for md_file in sorted(scan_dir.glob(pattern)):
            if not md_file.is_file():
                continue
            try:
                result = _sync_single_note(cfg, db, vectors, md_file, note_type, run)
            except NoteRejected as exc:
                reason = str(exc)
            except Exception as exc:
                if _stops_the_run(exc):
                    raise
                logger.exception("Sync manual: falha em %s", md_file)
                reason = f"{type(exc).__name__}: {exc}"
            else:
                stats[result] += 1
                if result in ("new", "updated"):
                    stats[counter] += 1
                continue
            logger.warning("Sync manual: %s nao adotada: %s", md_file, reason)
            stats["failed"] += 1
            failures.append({"path": str(md_file), "reason": reason})

    stats.update(_link_notes(db, run))
    repair = repair_permanent_links(db, vault_timezone=cfg.vault_timezone)
    stats.update(repair)
    stats["failures"] = failures
    stats["embedding_pending"] = vectors.pending
    stats["embedding_offline"] = vectors.offline_reason if vectors.pending else None

    logger.info(
        "Sync manual: %d novas, %d atualizadas, %d sem alteração, %d com falha "
        "(sources=%d, literature=%d, permanent=%d, mocs=%d) "
        "(wikilinks_reparados=%d, backlinks_reconstruidos=%d)",
        stats["new"],
        stats["updated"],
        stats["skipped"],
        stats["failed"],
        stats["sources"],
        stats["literature"],
        stats["permanent"],
        stats["mocs"],
        stats.get("wikilinks_rewritten", 0),
        stats.get("backlinks_rebuilt", 0),
    )
    return stats


def _stops_the_run(exc: Exception) -> bool:
    """A provider outage or a database error is not the note's fault: the next note fails too."""
    import sqlite3

    from zettel.llm import is_llm_unavailable

    return isinstance(exc, sqlite3.Error) or is_llm_unavailable(exc)


def repair_permanent_links(
    db: StateDB, *, vault_timezone: str = "America/Sao_Paulo"
) -> dict[str, int]:
    """Fix malformed ZTL wikilinks and rebuild auto-backlinks from the graph.

    Rewrites ``[[ZTL - ZTL - ULID]]`` / ``[[ZTL - ULID]]`` (no slug) to the
    current file stem when the target note exists on disk, then replaces each
    note's ``auto-backlinks`` block from live ``note_connections``.
    """
    from zettel.connector import rebuild_auto_backlinks

    def lookup_path(note_id: str) -> Path | None:
        row = db.get_note(note_id)
        if not row or not row.get("path"):
            return None
        path = Path(row["path"])
        return path if path.is_file() else None

    wikilinks_rewritten = 0
    backlinks_rebuilt = 0
    for note in db.list_notes():
        raw_path = note.get("path")
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        rewritten = rewrite_bare_permanent_wikilinks(content, lookup_path)
        if rewritten != content:
            path.write_text(rewritten, encoding="utf-8")
            meta, body = parse_frontmatter(rewritten)
            embeddable = extract_embeddable_text(body)
            semantic_checksum = sha256_hex(normalize_text_for_hash(embeddable))
            db.upsert_note(
                note_id=note["note_id"],
                source_id=note.get("source_id"),
                path=str(path),
                title=meta.get("title") or note.get("title") or "",
                note_semantic_checksum=semantic_checksum,
                body=body,
                frontmatter_json=frontmatter_json(meta) if meta else note.get("frontmatter_json"),
                origin=note.get("origin") or "pipeline",
            )
            wikilinks_rewritten += 1
        if rebuild_auto_backlinks(db, note["note_id"], vault_timezone=vault_timezone):
            backlinks_rebuilt += 1
    return {
        "wikilinks_rewritten": wikilinks_rewritten,
        "backlinks_rebuilt": backlinks_rebuilt,
    }


def _sync_single_note(
    cfg: AppConfig,
    db: StateDB,
    vectors: _Vectors,
    file_path: Path,
    note_type: str,
    run: _Run,
) -> str:
    """Sync a single note file. Returns 'new', 'updated', or 'skipped'.

    Raises ``NoteRejected`` when the note cannot be adopted as written.
    """
    content = file_path.read_text(encoding="utf-8")
    try:
        meta, body = parse_frontmatter_strict(content)
    except MalformedFrontmatter as exc:
        raise NoteRejected(str(exc)) from exc

    if note_type == "source":
        return _sync_source(cfg, db, vectors, file_path, meta, body)
    if note_type == "literature":
        return _sync_literature(cfg, db, vectors, file_path, meta, body, run.claimed_chunks)
    if note_type == "permanent":
        return _sync_permanent(cfg, db, vectors, file_path, meta, body, run)
    if note_type == "moc":
        return _sync_moc(cfg, db, vectors, file_path, meta, body)
    return "skipped"


def _manual_origin(meta: dict) -> str:
    """A note the pipeline created carries origin: pipeline; anything else is manual."""
    return meta.get("origin", "manual")


def _is_pipeline_origin(meta: dict) -> bool:
    """True when frontmatter says the pipeline (or hub garden) authored the note."""
    return str(meta.get("origin") or "") in PIPELINE_ORIGINS


def _author_list(meta: dict) -> list[str]:
    """``author``/``authors`` as a list: a single string is one author, not a list of letters."""
    raw = meta.get("author") or meta.get("authors") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(a).strip() for a in raw if str(a).strip()]


# Frontmatter keys of an SRC that describe the work (sources.bibliography_json).
_BIBLIO_KEYS = (
    "document_type",
    "subtitle",
    "edition",
    "place",
    "publisher",
    "translator",
    "isbn",
    "chapter_authors",
    "chapter_title",
    "book_title",
    "book_editors",
    "pages",
    "journal",
    "volume",
    "issue",
    "doi",
    "url",
    "accessed_at",
    "site_name",
    "published_at",
    "institution",
    "course",
    "discipline",
    "degree",
    "advisor",
    "event_name",
    "report_number",
    "title",
    "authors",
    "year",
)


def _bibliography(meta: dict, authors: list[str]) -> dict[str, Any]:
    """The SRC's bibliographic fields, in the JSON form SQLite stores (dates as text)."""
    payload = {k: meta[k] for k in _BIBLIO_KEYS if k in meta and meta[k] not in (None, "", [])}
    if authors and "authors" not in payload:
        payload["authors"] = list(authors)
    if meta.get("year") is not None and "year" not in payload:
        payload["year"] = meta["year"]
    if meta.get("title") and "title" not in payload:
        payload["title"] = meta["title"]
    return json.loads(frontmatter_json(payload))


def _identifiers(biblio: dict[str, Any]) -> tuple[str | None, str | None]:
    """DOI and ISBN normalized as harvest stores them (``None`` when absent or invalid)."""
    from zettel.harvester.biblio_dedupe import normalize_doi, normalize_isbn

    return (
        normalize_doi(str(biblio.get("doi") or "")) or None,
        normalize_isbn(str(biblio.get("isbn") or "")) or None,
    )


def _sync_source(
    cfg: AppConfig,
    db: StateDB,
    vectors: _Vectors,
    file_path: Path,
    meta: dict,
    body: str,
) -> str:
    """Adopt a hand-created SRC note, or carry an edited one into SQLite (#346)."""
    from zettel.harvester.citekey import generate_citekey

    authors = _author_list(meta)
    source_id = meta.get("source_id")
    citekey = None
    if source_id:
        citekey = source_id.lstrip("@")
    else:
        citekey = generate_citekey(db, authors, meta.get("year"), meta.get("title", file_path.stem))
        source_id = f"@{citekey}"
        meta["source_id"] = source_id
        meta["type"] = "source"
        meta.setdefault("origin", "manual")
        _rewrite_frontmatter(file_path, meta, body)

    title = str(meta.get("title") or file_path.stem)
    year = meta.get("year") if isinstance(meta.get("year"), int) else None
    biblio = _bibliography(meta, authors)
    existing = db.get_source(source_id)
    if existing:
        return _resync_source(cfg, db, vectors, existing, meta, title, authors, year, biblio)

    doi, isbn = _identifiers(biblio)
    db.upsert_source(
        source_id=source_id,
        citekey=citekey,
        title=title,
        authors=list(authors),
        year=year,
        file_checksum="",
        origin_path=str(file_path),
        origin_type="md",
        origin=_manual_origin(meta),
        document_type=meta.get("document_type"),
        bibliography_json=json.dumps(biblio, ensure_ascii=False) if biblio else None,
        doi=doi,
        isbn=isbn,
        abnt_reference=meta.get("abnt_reference"),
    )
    _embed_source(vectors, source_id, citekey, title, authors)
    return "new"


def _resync_source(
    cfg: AppConfig,
    db: StateDB,
    vectors: _Vectors,
    existing: dict,
    meta: dict,
    title: str,
    authors: list[str],
    year: int | None,
    biblio: dict[str, Any],
) -> str:
    """Bring an edited SRC's bibliography into SQLite; the pipeline's columns stay.

    A field the user wrote wins (ADR-057) and is recorded ``user`` in
    ``bibliography_json.provenance``. A minimal source a LIT created before its
    SRC was synced gets the SRC's authors and title. New authors or year
    recompute the citation of the notes that cite the source.
    """
    from zettel.citation import refresh_source_citations

    source_id = existing["source_id"]
    stored = json.loads(existing.get("bibliography_json") or "{}")
    edited = {k: v for k, v in biblio.items() if stored.get(k) != v}
    stored_authors = json.loads(existing.get("authors") or "[]")
    renamed = title != existing["title"] or authors != stored_authors
    recite = authors != stored_authors or year != existing.get("year")
    other = any(
        meta.get(key) and meta[key] != existing.get(key)
        for key in ("document_type", "abnt_reference")
    )
    if not (edited or renamed or recite or other):
        idx = vectors.index()
        if idx is None or idx.existing_ids("sources", [source_id]):
            return "skipped"
        # Adopted while offline: write the vector the earlier run could not (#343).
        embedded = _embed_source(vectors, source_id, existing["citekey"], title, authors)
        return "updated" if embedded else "skipped"

    merged = {**stored, **edited}
    if edited:
        merged["provenance"] = {**(stored.get("provenance") or {}), **dict.fromkeys(edited, "user")}
    doi, isbn = _identifiers(biblio)
    db.update_source_bibliography(
        source_id,
        title=title,
        authors=authors,
        year=year,
        document_type=meta.get("document_type"),
        bibliography_json=json.dumps(merged, ensure_ascii=False),
        abnt_reference=meta.get("abnt_reference"),
        doi=doi,
        isbn=isbn,
    )
    idx = vectors.index()
    if renamed or (idx is not None and not idx.existing_ids("sources", [source_id])):
        _embed_source(vectors, source_id, existing["citekey"], title, authors)
    if recite:
        refresh_source_citations(db, source_id, vault_timezone=cfg.vault_timezone)
    return "updated"


def _embed_source(
    vectors: _Vectors, source_id: str, citekey: str, title: str, authors: list[str]
) -> bool:
    return vectors.write(
        lambda idx: idx.upsert_source(
            source_id,
            f"{title} -- {', '.join(authors)}",
            {"citekey": citekey, "title": title, "origin_type": "md"},
        )
    )


def _sync_literature(
    cfg: AppConfig,
    db: StateDB,
    vectors: _Vectors,
    file_path: Path,
    meta: dict,
    body: str,
    claimed_chunks: dict[str, Path],
) -> str:
    """Adopt a hand-created LIT note (index or granular chunk).

    Index notes (type=literature_index) snapshot into sources.lit_body.
    Granular notes (type=literature with chunk_id) are adopted through
    ``manual_lit.adopt_manual_literature`` when they are the user's own content:
    that synthesizes the chunk row a hand-written note never had, so the note
    reaches SQLite and the source index like an approved one.
    Pipeline-authored granular notes keep the lightweight path (their chunk row
    already carries the real extracted text and checksum): skip when path and
    literature_id already match, and never copy frontmatter ``status: approved``
    onto ``chunks.status`` (review persists that row as ``persisted``).
    """
    from zettel.harvester.citekey import generate_citekey

    note_type = meta.get("type") or "literature"
    # Skip drafts under Review
    if "00_Inbox" in file_path.parts or "Review" in file_path.parts:
        return "skipped"

    chunk_id = str(meta.get("chunk_id") or "")
    is_granular = note_type == "literature" and bool(chunk_id)
    origin = str(meta.get("origin") or "manual")
    _check_literature_note(cfg, file_path, is_granular, chunk_id, origin, claimed_chunks)

    source_id = meta.get("source_id")
    citekey = meta.get("citekey")
    if source_id and "::" not in str(source_id):
        citekey = citekey or str(source_id).lstrip("@")
    elif citekey:
        source_id = f"@{str(citekey).lstrip('@')}"
        citekey = str(citekey).lstrip("@")

    if not source_id or "::" in str(source_id):
        citekey = generate_citekey(db, [], meta.get("year"), meta.get("title", file_path.stem))
        source_id = f"@{citekey}"

    if not db.get_source(source_id):
        db.upsert_source(
            source_id=source_id,
            citekey=citekey,
            title=meta.get("title", file_path.stem),
            authors=[],
            year=meta.get("year") if isinstance(meta.get("year"), int) else None,
            file_checksum="",
            origin_path=str(file_path),
            origin_type="md",
            origin="manual",
        )

    # Granular chunk LIT
    if is_granular:
        meta["source_id"] = source_id
        if origin == "manual":
            from zettel.manual_lit import adopt_manual_literature

            return adopt_manual_literature(cfg, db, vectors.index(), file_path, meta, body)
        return _sync_pipeline_granular_literature(db, file_path, meta, chunk_id)

    # Index / legacy monolithic LIT → lit_body
    if source_id != meta.get("source_id"):
        meta["source_id"] = source_id
        meta["type"] = (
            note_type if note_type in ("literature", "literature_index") else "literature_index"
        )
        meta.setdefault("origin", "manual")
        _rewrite_frontmatter(file_path, meta, body)

    full = file_path.read_text(encoding="utf-8")
    existing = db.get_source(source_id)
    if existing and existing.get("lit_body") == full:
        return "skipped"
    db.update_source_texts(source_id, lit_body=full)
    return "new" if not (existing and existing.get("lit_body")) else "updated"


def _check_literature_note(
    cfg: AppConfig,
    file_path: Path,
    is_granular: bool,
    chunk_id: str,
    origin: str,
    claimed_chunks: dict[str, Path],
) -> None:
    """Refuse a LIT note that would be adopted wrongly, before anything is written (#342).

    A note in a source folder without ``chunk_id`` used to fall into the index
    path and overwrite ``sources.lit_body``; an unknown ``origin`` was skipped in
    silence; two files with one ``chunk_id`` overwrote the same chunk row.
    """
    in_source_folder = len(file_path.relative_to(cfg.vault_path / "20_Literature").parts) > 1
    if in_source_folder and not is_granular:
        raise NoteRejected(
            "LIT dentro da pasta de uma fonte precisa de 'type: literature' e 'chunk_id' "
            "no frontmatter (ex.: '@Citekey::manual::0001')"
        )
    if not is_granular:
        return
    if origin != "manual" and origin not in PIPELINE_ORIGINS:
        raise NoteRejected(f"origin {origin!r} desconhecido numa LIT granular; use 'manual'")
    first = claimed_chunks.setdefault(chunk_id, file_path)
    if first != file_path:
        raise NoteRejected(f"chunk_id {chunk_id!r} repetido; ja usado por {first.name}")


def _sync_pipeline_granular_literature(
    db: StateDB,
    file_path: Path,
    meta: dict,
    chunk_id: str,
) -> str:
    """Keep a pipeline granular LIT's SQLite path in sync without re-adopting it.

    The vault frontmatter uses ``status: approved`` after review; the chunk row
    is ``persisted``. Never copy the YAML status back onto the row, and never
    copy an edited draft body onto ``candidate_json``: that copy happens only
    at approval (#269). Skip when path and literature_id already match so a
    no-op run is not counted updated.
    """
    chunk = db.get_chunk(chunk_id)
    if not chunk:
        return "skipped"
    path_str = str(file_path)
    literature_id = meta.get("literature_id")
    same_path = chunk.get("literature_note_path") == path_str
    same_id = not literature_id or chunk.get("literature_id") == literature_id
    if same_path and same_id:
        return "skipped"
    db.update_chunk_review(
        chunk_id,
        literature_note_path=path_str,
        literature_id=literature_id,
    )
    return "updated"


def _sync_permanent(
    cfg: AppConfig,
    db: StateDB,
    vectors: _Vectors,
    file_path: Path,
    meta: dict,
    body: str,
    run: _Run,
) -> str:
    """Sync a permanent (ZTL) note. Returns 'new', 'updated', or 'skipped'."""
    note_id = meta.get("note_id")
    if not note_id:
        note_id = str(ULID())
        meta["note_id"] = note_id
        meta["type"] = "permanent"
        meta.setdefault("origin", "manual")
        _rewrite_frontmatter(file_path, meta, body)

    # connect already indexed these; adopting images here rewrites the body,
    # bumps the checksum and re-embeds every pipeline ZTL that cites a figure.
    if _is_pipeline_origin(meta):
        return "skipped"

    body = _adopt_note_images(cfg, db, file_path, meta, body)

    embeddable = extract_embeddable_text(body)
    semantic_checksum = sha256_hex(normalize_text_for_hash(embeddable))

    from zettel.manual_lit import claim_concepts_for_note

    existing = db.get_note(note_id)
    emb_hash = compute_embedding_input_hash(
        semantic_checksum, cfg.embedding.provider, cfg.embedding.model
    )
    changed = not existing or existing.get("note_semantic_checksum") != semantic_checksum
    # A note whose vector a past offline run could not write is not skipped:
    # skipping on the semantic checksum alone left it without a vector for good.
    needs_vector = not existing or existing.get("embedding_input_hash") != emb_hash
    if not changed and not needs_vector:
        claim_concepts_for_note(db, note_id, meta, body)
        return "skipped"

    title = meta.get("title", file_path.stem)
    source_id = meta.get("source_id")
    if changed:
        db.upsert_note(
            note_id=note_id,
            source_id=source_id,
            path=str(file_path),
            title=title,
            note_semantic_checksum=semantic_checksum,
            embedding_model=cfg.embedding.model,
            body=body,
            frontmatter_json=frontmatter_json(meta),
            origin=_manual_origin(meta),
        )
        # Edges wait for the second pass, when every note of the vault is known.
        run.changed_notes.add(note_id)
        if not existing:
            run.new_notes.add(note_id)

    tags = meta.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    metadata = {"title": title, "source_id": source_id or "", "tags": ", ".join(tags)}
    embedded = needs_vector and vectors.write(
        lambda idx: idx.upsert_permanent_note(note_id, embeddable, metadata)
    )
    if embedded:
        db.update_note_embedding(note_id, emb_hash, cfg.embedding.model)
        _suggest_connections(cfg, db, vectors.index(), note_id, embeddable, file_path)

    claim_concepts_for_note(db, note_id, meta, body)
    if not changed and not embedded:
        return "skipped"
    return "new" if not existing else "updated"


def _adopt_note_images(
    cfg: AppConfig,
    db: StateDB,
    file_path: Path,
    meta: dict,
    body: str,
) -> str:
    """Adopt images pasted into a note whose frontmatter names a known source.

    Assets are keyed by source (``assets.source_id`` is NOT NULL with an FK), so a
    note with no resolvable ``source_id`` keeps its references untouched.
    """
    from zettel.assets import adopt_vault_images
    from zettel.manual_lit import ensure_manual_chapter

    source_id = str(meta.get("source_id") or "")
    if not source_id or not db.get_source(source_id):
        return body

    chapter_id = ensure_manual_chapter(db, source_id)
    new_body, adopted = adopt_vault_images(
        cfg,
        db,
        source_id,
        chapter_id,
        file_path,
        body,
    )
    if adopted and new_body != body:
        _rewrite_frontmatter(file_path, meta, new_body)
        return new_body
    return body


def _sync_moc(
    cfg: AppConfig,
    db: StateDB,
    vectors: _Vectors,
    file_path: Path,
    meta: dict,
    body: str,
) -> str:
    """Sync a MOC note. Returns 'new', 'updated', or 'skipped'."""
    moc_id = meta.get("moc_id")
    if not moc_id:
        moc_id = str(ULID())
        meta["moc_id"] = moc_id
        meta["type"] = "moc"
        meta.setdefault("origin", "manual")
        _rewrite_frontmatter(file_path, meta, body)

    if _is_pipeline_origin(meta):
        return "skipped"

    topic = meta.get("topic", file_path.stem)
    embeddable = extract_embeddable_text(body)
    semantic_checksum = sha256_hex(normalize_text_for_hash(embeddable))

    # Look up by moc_id (not by signature) so an edited MOC is detected as 'updated'.
    existing = db.get_moc(moc_id)
    if existing and existing.get("cluster_signature") == semantic_checksum:
        return "skipped"

    origin = _manual_origin(meta)
    previous_body = existing.get("body") if existing else None
    db.upsert_moc(
        moc_id,
        topic,
        str(file_path),
        semantic_checksum,
        body=body,
        frontmatter_json=frontmatter_json(meta),
        origin=origin,
    )
    from zettel.moc_backrefs import sync_moc_backrefs

    sync_moc_backrefs(
        db,
        moc_id,
        topic,
        file_path,
        vault_timezone=cfg.vault_timezone,
        previous_body=previous_body,
        new_body=body,
    )
    return "new" if not existing else "updated"


def _suggest_connections(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    note_id: str,
    embeddable: str,
    file_path: Path,
) -> None:
    """Suggest connections for a note via the auto-connections managed block.

    Uses the hybrid Retriever (dense + BM25 + graph). These remain *suggestions*
    only — they are written to the vault block, not persisted as graph edges (a
    suggestion is not an accepted connection).
    """
    retriever = Retriever(cfg, db, idx)
    similar = retriever.search_notes(embeddable, topk=cfg.linking.topk, exclude_id=note_id).hits
    if not similar:
        return

    links: list[str] = []
    for n in similar:
        title = n.title or n.metadata.get("title", "Sem título")
        row = db.get_note(n.note_id)
        wiki = permanent_wikilink(
            n.note_id,
            title,
            path=row.get("path") if row else None,
        )
        links.append(f"- {wiki}")

    if links:
        safe_update_managed_blocks(
            file_path,
            {
                "auto-connections": "\n".join(links),
            },
            vault_timezone=cfg.vault_timezone,
        )


def _strip_auto_blocks(body: str) -> str:
    """Remove auto-generated managed blocks so their wikilinks are not read as edges."""
    for name in _AUTO_BLOCKS_TO_SKIP:
        start_tag, end_tag = _block_pattern(name)
        while True:
            start = body.find(start_tag)
            if start == -1:
                break
            end = body.find(end_tag, start)
            if end == -1:
                body = body[:start]
                break
            body = body[:start] + body[end + len(end_tag) :]
    return body


def linked_note_ids(body: str) -> set[str]:
    """Permanent notes a body links to, outside the auto-generated blocks."""
    return set(ZTL_LINK_RE.findall(_strip_auto_blocks(body or "")))


def _link_notes(db: StateDB, run: _Run) -> dict[str, int]:
    """Second pass: derive body edges once every note of the vault is in SQLite (#347).

    Covers the notes stored in this run and the hand-written ones whose body
    links to a note that only now became known: their link pointed at nothing
    when they were synced, and an unchanged note is not read again.
    """
    todo = set(run.changed_notes)
    if run.new_notes:
        for note in db.list_notes():
            if note["note_id"] in todo or note.get("origin") in PIPELINE_ORIGINS:
                continue
            if linked_note_ids(note.get("body") or "") & run.new_notes:
                todo.add(note["note_id"])
    created = removed = 0
    for note_id in sorted(todo):
        row = db.get_note(note_id)
        if row:
            added, dropped = _extract_body_edges(db, note_id, row.get("body") or "")
            created += added
            removed += dropped
    return {"edges_created": created, "edges_removed": removed}


def _extract_body_edges(db: StateDB, note_id: str, body: str) -> tuple[int, int]:
    """Make a note's ``origin='manual'`` edges match the links in its body.

    A wikilink the user placed in the body (e.g. under `## Conexoes`) is an
    *accepted* connection, so it becomes a `related` edge in note_connections.
    Auto-generated blocks (`auto-connections` suggestions, `auto-backlinks`) are
    excluded: a suggestion is not an acceptance. An edge is only inserted when the
    pair has no connection in either direction, so a typed edge is never
    downgraded (ADR-009). A manual edge whose link left the body is removed;
    ``llm`` and ``derived`` edges are never touched (#347).

    Returns ``(created, removed)``.
    """
    targets = linked_note_ids(body) - {note_id}

    removed = 0
    for edge in db.list_outgoing_connections(note_id, origin="manual"):
        if edge["target_note_id"] not in targets:
            removed += db.delete_note_connection(
                note_id, edge["target_note_id"], edge["relation_type"], origin="manual"
            )
    if not targets:
        return 0, removed

    connected_pairs = {
        frozenset((e["source_note_id"], e["target_note_id"]))
        for e in db.get_note_connections(note_id)
    }
    created = 0
    for target in sorted(targets):
        if not db.get_note(target):
            continue  # only link to notes the pipeline knows about
        if frozenset((note_id, target)) in connected_pairs:
            continue  # already connected (any type / direction): do not downgrade
        db.upsert_note_connection(note_id, target, "related", "wikilink manual", origin="manual")
        connected_pairs.add(frozenset((note_id, target)))
        created += 1
    return created, removed


def rebuild_manual_edges(db: StateDB) -> dict[str, int]:
    """Re-derive the manual edges of every note from the bodies already in SQLite.

    Backfills the graph for a vault written before this feature existed, and
    drops manual edges whose link is gone, without touching any file.
    """
    scanned = created = removed = 0
    for note in db.list_notes():
        body = note.get("body")
        if not body:
            continue
        scanned += 1
        added, dropped = _extract_body_edges(db, note["note_id"], body)
        created += added
        removed += dropped
    return {"notes_scanned": scanned, "edges_created": created, "edges_removed": removed}


def _rewrite_frontmatter(file_path: Path, meta: dict, body: str) -> None:
    """Rewrite a file with updated frontmatter."""
    from zettel.vault import compose_note

    content = compose_note(meta, body)
    file_path.write_text(content, encoding="utf-8")
