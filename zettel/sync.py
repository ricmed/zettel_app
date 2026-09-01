"""Sync manual — detect manual notes across all vault folders, index them.

Adopts notes hand-written in Obsidian (Sources, Literature, Permanent, MOCs) into
the pipeline: assigns ids/citekeys, marks them `origin: manual`, persists their
bodies into SQLite (retention) and indexes the embeddable ones into ChromaDB.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

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
    _block_pattern,
    parse_frontmatter,
    permanent_wikilink,
    rewrite_bare_permanent_wikilinks,
    safe_update_managed_blocks,
)

logger = logging.getLogger(__name__)

# A ZTL wikilink target: ULID is Crockford base32 (no I, L, O, U), 26 chars.
_ZTL_WIKILINK = re.compile(r"\[\[ZTL - ([0-9A-HJKMNP-TV-Z]{26})")

# Managed blocks whose wikilinks are auto-generated (suggestions / backlinks) and
# must NOT be treated as user-accepted connections.
_AUTO_BLOCKS_TO_SKIP = ("auto-connections", "auto-backlinks", "auto-moc-backrefs")


def run_sync_manual(cfg: AppConfig, db: StateDB, idx: VectorIndex) -> dict[str, int]:
    """Scan all four note folders for manual/modified notes and sync them.

    Returns per-type counters plus aggregate new/updated/skipped.
    """
    stats = {
        "new": 0, "updated": 0, "skipped": 0,
        "sources": 0, "literature": 0, "permanent": 0, "mocs": 0,
    }

    dirs_to_scan = [
        (cfg.vault_path / "10_Sources", "source", "sources"),
        (cfg.vault_path / "20_Literature", "literature", "literature"),
        (cfg.vault_path / "30_Permanent", "permanent", "permanent"),
        (cfg.vault_path / "40_MOCs", "moc", "mocs"),
    ]

    for scan_dir, note_type, counter in dirs_to_scan:
        if not scan_dir.exists():
            continue
        # Literature may live in citekey subfolders (granular LITs).
        pattern = "**/*.md" if note_type == "literature" else "*.md"
        for md_file in scan_dir.glob(pattern):
            if not md_file.is_file():
                continue
            result = _sync_single_note(cfg, db, idx, md_file, note_type)
            stats[result] += 1
            if result in ("new", "updated"):
                stats[counter] += 1

    repair = repair_permanent_links(db)
    stats.update(repair)

    logger.info(
        "Sync manual: %d novas, %d atualizadas, %d sem alteração "
        "(sources=%d, literature=%d, permanent=%d, mocs=%d) "
        "(wikilinks_reparados=%d, backlinks_reconstruidos=%d)",
        stats["new"], stats["updated"], stats["skipped"],
        stats["sources"], stats["literature"], stats["permanent"], stats["mocs"],
        stats.get("wikilinks_rewritten", 0), stats.get("backlinks_rebuilt", 0),
    )
    return stats


def repair_permanent_links(db: StateDB) -> dict[str, int]:
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
                frontmatter_json=json.dumps(meta, ensure_ascii=False) if meta else note.get("frontmatter_json"),
                origin=note.get("origin") or "pipeline",
            )
            wikilinks_rewritten += 1
        if rebuild_auto_backlinks(db, note["note_id"]):
            backlinks_rebuilt += 1
    return {
        "wikilinks_rewritten": wikilinks_rewritten,
        "backlinks_rebuilt": backlinks_rebuilt,
    }


def _sync_single_note(
    cfg: AppConfig, db: StateDB, idx: VectorIndex,
    file_path: Path, note_type: str,
) -> str:
    """Sync a single note file. Returns 'new', 'updated', or 'skipped'."""
    content = file_path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(content)

    if note_type == "source":
        return _sync_source(cfg, db, idx, file_path, meta, body)
    if note_type == "literature":
        return _sync_literature(cfg, db, file_path, meta, body)
    if note_type == "permanent":
        return _sync_permanent(cfg, db, idx, file_path, meta, body)
    if note_type == "moc":
        return _sync_moc(cfg, db, idx, file_path, meta, body)
    return "skipped"


def _manual_origin(meta: dict) -> str:
    """A note the pipeline created carries origin: pipeline; anything else is manual."""
    return meta.get("origin", "manual")


def _sync_source(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, file_path: Path, meta: dict, body: str,
) -> str:
    """Adopt a hand-created SRC note: register the source in the DB + index."""
    from zettel.harvester.citekey import generate_citekey

    source_id = meta.get("source_id")
    citekey = None
    if source_id:
        citekey = source_id.lstrip("@")
    else:
        citekey = generate_citekey(
            db, meta.get("author") or meta.get("authors") or [], meta.get("year"),
            meta.get("title", file_path.stem),
        )
        source_id = f"@{citekey}"
        meta["source_id"] = source_id
        meta["type"] = "source"
        meta.setdefault("origin", "manual")
        _rewrite_frontmatter(file_path, meta, body)

    existing = db.get_source(source_id)
    origin = _manual_origin(meta)
    if existing:
        return "skipped"

    authors = meta.get("author") or meta.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    year = meta.get("year")

    biblio_payload = {
        k: meta[k] for k in (
            "document_type", "subtitle", "edition", "place", "publisher",
            "translator", "isbn", "chapter_authors", "chapter_title", "book_title",
            "book_editors", "pages", "journal", "volume", "issue", "doi", "url",
            "accessed_at", "site_name", "published_at", "institution", "course",
            "discipline", "degree", "advisor", "event_name", "report_number",
            "title", "authors", "year",
        ) if k in meta and meta[k] not in (None, "", [])
    }
    if authors and "authors" not in biblio_payload:
        biblio_payload["authors"] = list(authors)
    if year is not None and "year" not in biblio_payload:
        biblio_payload["year"] = year
    if meta.get("title") and "title" not in biblio_payload:
        biblio_payload["title"] = meta["title"]

    biblio_json = json.dumps(biblio_payload, ensure_ascii=False) if biblio_payload else None

    db.upsert_source(
        source_id=source_id, citekey=citekey, title=meta.get("title", file_path.stem),
        authors=list(authors), year=year if isinstance(year, int) else None,
        file_checksum="", origin_path=str(file_path), origin_type="md", origin=origin,
        document_type=meta.get("document_type"),
        bibliography_json=biblio_json,
        abnt_reference=meta.get("abnt_reference"),
    )
    idx.upsert_source(source_id, f"{meta.get('title', file_path.stem)} -- {', '.join(authors)}", {
        "citekey": citekey, "title": meta.get("title", file_path.stem), "origin_type": "md",
    })
    return "new"


def _sync_literature(
    cfg: AppConfig, db: StateDB, file_path: Path, meta: dict, body: str,
) -> str:
    """Adopt a hand-created LIT note (index or granular chunk).

    Index notes (type=literature_index) snapshot into sources.lit_body.
    Granular notes (type=literature with chunk_id) update the matching chunk row.
    """
    from zettel.harvester.citekey import generate_citekey

    note_type = meta.get("type") or "literature"
    # Skip drafts under Review
    if "00_Inbox" in file_path.parts or "Review" in file_path.parts:
        return "skipped"

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
            source_id=source_id, citekey=citekey, title=meta.get("title", file_path.stem),
            authors=[], year=meta.get("year") if isinstance(meta.get("year"), int) else None,
            file_checksum="", origin_path=str(file_path), origin_type="md", origin="manual",
        )

    # Granular chunk LIT
    if note_type == "literature" and meta.get("chunk_id"):
        chunk_id = meta["chunk_id"]
        chunk = db.get_chunk(chunk_id)
        status = meta.get("status") or "approved"
        if chunk:
            db.update_chunk_review(
                chunk_id,
                status=status if status in ("approved", "persisted", "awaiting_review") else "approved",
                literature_note_path=str(file_path),
                literature_id=meta.get("literature_id"),
            )
        return "updated" if chunk else "skipped"

    # Index / legacy monolithic LIT → lit_body
    if source_id != meta.get("source_id"):
        meta["source_id"] = source_id
        meta["type"] = note_type if note_type in ("literature", "literature_index") else "literature_index"
        meta.setdefault("origin", "manual")
        _rewrite_frontmatter(file_path, meta, body)

    full = file_path.read_text(encoding="utf-8")
    existing = db.get_source(source_id)
    if existing and existing.get("lit_body") == full:
        return "skipped"
    db.update_source_texts(source_id, lit_body=full)
    return "new" if not (existing and existing.get("lit_body")) else "updated"


def _sync_permanent(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, file_path: Path, meta: dict, body: str,
) -> str:
    """Sync a permanent (ZTL) note. Returns 'new', 'updated', or 'skipped'."""
    note_id = meta.get("note_id")
    if not note_id:
        note_id = str(ULID())
        meta["note_id"] = note_id
        meta["type"] = "permanent"
        meta.setdefault("origin", "manual")
        _rewrite_frontmatter(file_path, meta, body)

    embeddable = extract_embeddable_text(body)
    semantic_checksum = sha256_hex(normalize_text_for_hash(embeddable))

    existing = db.get_note(note_id)
    if existing and existing.get("note_semantic_checksum") == semantic_checksum:
        return "skipped"

    title = meta.get("title", file_path.stem)
    source_id = meta.get("source_id")
    origin = _manual_origin(meta)
    db.upsert_note(
        note_id=note_id, source_id=source_id, path=str(file_path),
        title=title, note_semantic_checksum=semantic_checksum,
        embedding_model=cfg.embedding.model,
        body=body, frontmatter_json=json.dumps(meta, ensure_ascii=False), origin=origin,
    )

    tags = meta.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]

    # Skip re-embedding when semantic content + model are unchanged.
    emb_hash = compute_embedding_input_hash(
        semantic_checksum, cfg.embedding.provider, cfg.embedding.model
    )
    if not existing or existing.get("embedding_input_hash") != emb_hash:
        idx.upsert_permanent_note(note_id, embeddable, {
            "title": title, "source_id": source_id or "", "tags": ", ".join(tags),
        })
        db.update_note_embedding(note_id, emb_hash, cfg.embedding.model)

    _extract_body_edges(db, note_id, body)
    _suggest_connections(cfg, db, idx, note_id, embeddable, file_path)
    return "new" if not existing else "updated"


def _sync_moc(
    cfg: AppConfig, db: StateDB, idx: VectorIndex, file_path: Path, meta: dict, body: str,
) -> str:
    """Sync a MOC note. Returns 'new', 'updated', or 'skipped'."""
    from zettel.gardener import _moc_embeddable
    from zettel.rebuild import _moc_summary_from_body

    moc_id = meta.get("moc_id")
    if not moc_id:
        moc_id = str(ULID())
        meta["moc_id"] = moc_id
        meta["type"] = "moc"
        meta.setdefault("origin", "manual")
        _rewrite_frontmatter(file_path, meta, body)

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
        moc_id, topic, str(file_path), semantic_checksum,
        body=body, frontmatter_json=json.dumps(meta, ensure_ascii=False), origin=origin,
    )
    # Unified MOC embedding text (matches gardener + reindex).
    idx.upsert_moc(moc_id, _moc_embeddable(topic, _moc_summary_from_body(body)), {"topic": topic})
    from zettel.moc_backrefs import sync_moc_backrefs

    sync_moc_backrefs(
        db, moc_id, topic, file_path, previous_body=previous_body, new_body=body,
    )
    return "new" if not existing else "updated"


def _suggest_connections(
    cfg: AppConfig, db: StateDB, idx: VectorIndex,
    note_id: str, embeddable: str, file_path: Path,
) -> None:
    """Suggest connections for a note via the auto-connections managed block.

    Uses the hybrid Retriever (dense + BM25 + graph). These remain *suggestions*
    only — they are written to the vault block, not persisted as graph edges (a
    suggestion is not an accepted connection).
    """
    retriever = Retriever(cfg, db, idx)
    similar = retriever.search_notes(
        embeddable, topk=cfg.linking.topk, exclude_id=note_id
    ).hits
    if not similar:
        return

    links: list[str] = []
    for n in similar:
        title = n.title or n.metadata.get("title", "Sem título")
        row = db.get_note(n.note_id)
        wiki = permanent_wikilink(
            n.note_id, title, path=row.get("path") if row else None,
        )
        links.append(f"- {wiki}")

    if links:
        safe_update_managed_blocks(file_path, {
            "auto-connections": "\n".join(links),
        })


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
            body = body[:start] + body[end + len(end_tag):]
    return body


def _extract_body_edges(db: StateDB, note_id: str, body: str) -> int:
    """Persist manual wikilinks in a note body as `related` graph edges.

    This closes the graph loop for hand-written notes: a wikilink the user placed
    in the body (e.g. under `## Conexoes`) is an *accepted* connection, so it
    becomes a real edge in note_connections. Auto-generated blocks
    (`auto-connections` suggestions, `auto-backlinks`) are excluded — a suggestion
    is not an acceptance. Never downgrades an already-typed edge: an edge is only
    inserted when the pair has no existing connection in either direction.

    Returns the number of new edges created.
    """
    stripped = _strip_auto_blocks(body)
    targets = {m for m in _ZTL_WIKILINK.findall(stripped) if m != note_id}
    if not targets:
        return 0

    existing_edges = db.get_note_connections(note_id)
    connected_pairs = {
        frozenset((e["source_note_id"], e["target_note_id"])) for e in existing_edges
    }

    created = 0
    for target in targets:
        if not db.get_note(target):
            continue  # only link to notes the pipeline knows about
        if frozenset((note_id, target)) in connected_pairs:
            continue  # already connected (any type / direction) — do not downgrade
        db.upsert_note_connection(note_id, target, "related", "wikilink manual")
        connected_pairs.add(frozenset((note_id, target)))
        created += 1
    return created


def rebuild_manual_edges(db: StateDB) -> dict[str, int]:
    """Re-derive `related` edges from every note body already stored in SQLite.

    Backfills the graph for a vault written before this feature existed, without
    touching any file (note bodies are persisted in the notes table).
    """
    notes = db.list_notes()
    total_edges = 0
    scanned = 0
    for note in notes:
        body = note.get("body")
        if not body:
            continue
        scanned += 1
        total_edges += _extract_body_edges(db, note["note_id"], body)
    return {"notes_scanned": scanned, "edges_created": total_edges}


def _rewrite_frontmatter(file_path: Path, meta: dict, body: str) -> None:
    """Rewrite a file with updated frontmatter."""
    from zettel.vault import compose_note
    content = compose_note(meta, body)
    file_path.write_text(content, encoding="utf-8")
