"""Rebuild derived stores from the durable SQLite state.

Two rebuilders, both LLM-free:

- `run_reindex`  — regenerate ChromaDB collections from SQLite (Fase 2). The vector
  store is a disposable cache; this recreates it from `chunks.text` and the persisted
  note/MOC bodies, so it can be deleted and rebuilt at will.
- `run_rebuild_vault` — recreate the Obsidian `.md` files from the persisted bodies
  (Fase 5), without reprocessing the LLM. Never overwrites manual notes silently.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.hashing import (
    compute_embedding_input_hash,
    extract_embeddable_text,
    normalize_text_for_hash,
    sha256_hex,
)
from zettel.index import COL_CHUNKS, COL_MOCS, COL_PERMANENT, COL_SOURCES, VectorIndex
from zettel.state import StateDB
from zettel.vault import build_source_note, compose_note, note_filename

logger = logging.getLogger(__name__)


# ── MOC body parsing (summary between the H1 and the first H2) ──────────


def _moc_summary_from_body(body: str) -> str:
    if not body:
        return ""
    summary_lines: list[str] = []
    in_summary = False
    for line in body.split("\n"):
        if line.startswith("## "):
            break
        if line.startswith("# "):
            in_summary = True
            continue
        if in_summary:
            summary_lines.append(line)
    return "\n".join(summary_lines).strip()


def _tags_from_frontmatter(frontmatter_json: str | None) -> list[str]:
    if not frontmatter_json:
        return []
    try:
        meta = json.loads(frontmatter_json)
    except (json.JSONDecodeError, TypeError):
        return []
    tags = meta.get("tags", [])
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    return [str(t) for t in tags] if isinstance(tags, list) else []


# ── Reindex ChromaDB from SQLite ───────────────────────────────────────

_ALL_COLLECTIONS = [COL_SOURCES, COL_CHUNKS, COL_PERMANENT, COL_MOCS]


def run_reindex(
    cfg: AppConfig, db: StateDB, idx: VectorIndex,
    collection: str | None = None, force: bool = False,
) -> dict[str, int]:
    """Rebuild ChromaDB collections from the SQLite state (no LLM calls).

    Args:
        collection: rebuild only this collection (one of sources/chunks/
            permanent_notes/mocs). None rebuilds all.
        force: reset each target collection before repopulating.

    When the embedding provider/model changes, callers **must** pass
    ``force=True`` (or reset collections first). Without force, sources/chunks
    already present under the old vector space are skipped and the spaces mix.
    """
    targets = [collection] if collection else _ALL_COLLECTIONS
    for t in targets:
        if t not in _ALL_COLLECTIONS:
            raise ValueError(f"Colecao desconhecida: {t}")

    stats: dict[str, int] = {}
    for t in targets:
        if force:
            idx.reset_collection(t)
        if t == COL_SOURCES:
            stats[t] = _reindex_sources(db, idx)
        elif t == COL_CHUNKS:
            stats[t] = _reindex_chunks(db, idx)
        elif t == COL_PERMANENT:
            stats[t] = _reindex_permanent(cfg, db, idx)
        elif t == COL_MOCS:
            stats[t] = _reindex_mocs(db, idx)

    # The FTS5 lexical index is another disposable cache reconstructible from
    # SQLite — rebuild it whenever a full reindex runs (no specific collection).
    if collection is None and getattr(db, "fts_enabled", False):
        fts_counts = db.rebuild_fts()
        stats["fts_notes"] = fts_counts.get("fts_notes", 0)
        stats["fts_chunks"] = fts_counts.get("fts_chunks", 0)
    return stats


def _reindex_sources(db: StateDB, idx: VectorIndex) -> int:
    n = 0
    for src in db.list_sources():
        authors = json.loads(src.get("authors") or "[]")
        summary = f"{src['title']} -- {', '.join(authors)}"
        existing = idx.existing_ids(COL_SOURCES, [src["source_id"]])
        if src["source_id"] in existing:
            continue
        idx.upsert_source(src["source_id"], summary, {
            "citekey": src["citekey"], "title": src["title"],
            "origin_type": src["origin_type"],
        })
        n += 1
    return n


def _reindex_chunks(db: StateDB, idx: VectorIndex) -> int:
    n = 0
    for src in db.list_sources():
        chunks = db.get_chunks_for_source(src["source_id"])
        ids = [c["chunk_id"] for c in chunks]
        already = idx.existing_ids(COL_CHUNKS, ids)
        for c in chunks:
            if c["chunk_id"] in already:
                continue
            idx.upsert_chunk(c["chunk_id"], c["text"], {
                "source_id": c["source_id"], "chapter_id": c["chapter_id"],
                "locator": c.get("locator", ""), "section_path": c.get("section_path", ""),
            })
            n += 1
    return n


def _reindex_permanent(cfg: AppConfig, db: StateDB, idx: VectorIndex) -> int:
    n = 0
    for note in db.list_notes():
        body = note.get("body")
        if not body:
            logger.warning(
                "Nota %s sem corpo persistido (anterior a Fase 0) - pulando no reindex.",
                note["note_id"],
            )
            continue
        embeddable = extract_embeddable_text(body)
        semantic_checksum = sha256_hex(normalize_text_for_hash(embeddable))
        tags = _tags_from_frontmatter(note.get("frontmatter_json"))
        idx.upsert_permanent_note(note["note_id"], embeddable, {
            "title": note.get("title", ""), "source_id": note.get("source_id") or "",
            "tags": ", ".join(tags), "note_semantic_checksum": semantic_checksum,
        })
        emb_hash = compute_embedding_input_hash(
            semantic_checksum, cfg.embedding.provider, cfg.embedding.model
        )
        db.update_note_embedding(note["note_id"], emb_hash, cfg.embedding.model)
        n += 1
    return n


def _reindex_mocs(db: StateDB, idx: VectorIndex) -> int:
    from zettel.gardener import _moc_embeddable

    n = 0
    for moc in db.list_mocs():
        summary = _moc_summary_from_body(moc.get("body") or "")
        idx.upsert_moc(moc["moc_id"], _moc_embeddable(moc["topic"], summary), {
            "topic": moc["topic"],
        })
        n += 1
    return n


# ── Rebuild vault .md files from SQLite (Fase 5) ───────────────────────


def run_rebuild_vault(
    cfg: AppConfig, db: StateDB, force: bool = False, dry_run: bool = False,
) -> dict[str, int]:
    """Recreate vault .md files from the persisted bodies in SQLite.

    Writes a file only if it does not already exist; with `force`, overwrites but
    only for records whose origin is 'pipeline' (never clobbers manual notes).
    Returns counts per note type plus 'written' and 'skipped'.
    """
    stats = {"sources": 0, "literature": 0, "permanent": 0, "mocs": 0,
             "written": 0, "skipped": 0, "missing_body": 0}

    def _write(path: Path, content: str, origin: str) -> bool:
        if path.exists():
            if not force:
                stats["skipped"] += 1
                return False
            if origin == "manual":
                logger.info("Preservando nota manual (nao sobrescrita): %s", path.name)
                stats["skipped"] += 1
                return False
        if dry_run:
            stats["written"] += 1
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        stats["written"] += 1
        return True

    # SRC + LIT (both derived from the sources table).
    for src in db.list_sources():
        citekey = src["citekey"]
        title = src["title"]
        authors = json.loads(src.get("authors") or "[]")
        origin = src.get("origin", "pipeline")
        biblio_fields = None
        if src.get("bibliography_json"):
            try:
                raw = json.loads(src["bibliography_json"])
                biblio_fields = {
                    k: v for k, v in raw.items()
                    if k not in ("document_type", "title", "authors", "year", "confidence")
                }
            except (json.JSONDecodeError, TypeError):
                biblio_fields = None

        src_meta, src_body = build_source_note(
            src["source_id"], citekey, title, authors, src.get("year"),
            src.get("origin_path", ""), src.get("origin_type", "md"),
            src.get("file_checksum", ""), origin=origin,
            document_type=src.get("document_type"),
            biblio_fields=biblio_fields,
            abnt_reference=src.get("abnt_reference"),
        )
        src_path = cfg.vault_path / "10_Sources" / note_filename("SRC", f"@{citekey}", title)
        if _write(src_path, compose_note(src_meta, src_body), origin):
            stats["sources"] += 1

        lit_body = src.get("lit_body")
        if lit_body:
            lit_path = cfg.vault_path / "20_Literature" / note_filename("LIT", f"@{citekey}", title)
            if _write(lit_path, lit_body, origin):
                stats["literature"] += 1
        else:
            stats["missing_body"] += 1

    # ZTL permanent notes.
    for note in db.list_notes():
        body = note.get("body")
        fm_json = note.get("frontmatter_json")
        if not body or not fm_json:
            stats["missing_body"] += 1
            continue
        meta = json.loads(fm_json)
        path_str = note.get("path")
        note_path = Path(path_str) if path_str else (
            cfg.vault_path / "30_Permanent"
            / note_filename("ZTL", note["note_id"], note.get("title", ""))
        )
        if _write(note_path, compose_note(meta, body), note.get("origin", "pipeline")):
            stats["permanent"] += 1

    # MOCs.
    for moc in db.list_mocs():
        body = moc.get("body")
        fm_json = moc.get("frontmatter_json")
        if not body or not fm_json:
            stats["missing_body"] += 1
            continue
        meta = json.loads(fm_json)
        path_str = moc.get("path")
        moc_path = Path(path_str) if path_str else (
            cfg.vault_path / "40_MOCs" / note_filename("MOC", moc["moc_id"], moc["topic"])
        )
        if _write(moc_path, compose_note(meta, body), moc.get("origin", "pipeline")):
            stats["mocs"] += 1

    return stats
