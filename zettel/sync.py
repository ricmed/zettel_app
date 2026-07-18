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
from zettel.state import StateDB
from zettel.vault import parse_frontmatter, safe_update_managed_blocks, _slug

logger = logging.getLogger(__name__)

# Citekey embedded in a SRC/LIT filename, e.g. "LIT - @Author2024Slug - titulo.md".
_CITEKEY_IN_NAME = re.compile(r"-\s*@?([A-Za-z0-9]+)\s*-")


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
        for md_file in scan_dir.glob("*.md"):
            result = _sync_single_note(cfg, db, idx, md_file, note_type)
            stats[result] += 1
            if result in ("new", "updated"):
                stats[counter] += 1

    logger.info(
        "Sync manual: %d novas, %d atualizadas, %d sem alteração "
        "(sources=%d, literature=%d, permanent=%d, mocs=%d)",
        stats["new"], stats["updated"], stats["skipped"],
        stats["sources"], stats["literature"], stats["permanent"], stats["mocs"],
    )
    return stats


def _citekey_from_filename(file_path: Path) -> str | None:
    m = _CITEKEY_IN_NAME.search(file_path.stem)
    return m.group(1) if m else None


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
    from zettel.harvester import _generate_citekey

    source_id = meta.get("source_id")
    citekey = None
    if source_id:
        citekey = source_id.lstrip("@")
    else:
        citekey = _citekey_from_filename(file_path) or _generate_citekey(
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
    db.upsert_source(
        source_id=source_id, citekey=citekey, title=meta.get("title", file_path.stem),
        authors=list(authors), year=year if isinstance(year, int) else None,
        file_checksum="", origin_path=str(file_path), origin_type="md", origin=origin,
    )
    idx.upsert_source(source_id, f"{meta.get('title', file_path.stem)} -- {', '.join(authors)}", {
        "citekey": citekey, "title": meta.get("title", file_path.stem), "origin_type": "md",
    })
    return "new"


def _sync_literature(
    cfg: AppConfig, db: StateDB, file_path: Path, meta: dict, body: str,
) -> str:
    """Adopt a hand-created LIT note: link to its source and persist its body.

    LIT notes are not embedded (mirrors the pipeline), so this only registers the
    source link and snapshots the LIT body into sources.lit_body for retention.
    """
    from zettel.harvester import _generate_citekey

    source_id = meta.get("source_id") or meta.get("literature_id")
    citekey = None
    if source_id:
        citekey = source_id.lstrip("@")
    else:
        citekey = _citekey_from_filename(file_path)
        if citekey:
            source_id = f"@{citekey}"

    # Orphan LIT (no resolvable source): create a minimal manual source to attach to.
    if not source_id:
        citekey = _generate_citekey(db, [], meta.get("year"), meta.get("title", file_path.stem))
        source_id = f"@{citekey}"

    if not db.get_source(source_id):
        db.upsert_source(
            source_id=source_id, citekey=citekey, title=meta.get("title", file_path.stem),
            authors=[], year=meta.get("year") if isinstance(meta.get("year"), int) else None,
            file_checksum="", origin_path=str(file_path), origin_type="md", origin="manual",
        )

    if source_id != meta.get("source_id"):
        meta["source_id"] = source_id
        meta["type"] = "literature"
        meta.setdefault("origin", "manual")
        _rewrite_frontmatter(file_path, meta, body)

    # Snapshot the full LIT file for retention (skip if unchanged).
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
    db.upsert_moc(
        moc_id, topic, str(file_path), semantic_checksum,
        body=body, frontmatter_json=json.dumps(meta, ensure_ascii=False), origin=origin,
    )
    # Unified MOC embedding text (matches gardener + reindex).
    idx.upsert_moc(moc_id, _moc_embeddable(topic, _moc_summary_from_body(body)), {"topic": topic})
    return "new" if not existing else "updated"


def _suggest_connections(
    cfg: AppConfig, db: StateDB, idx: VectorIndex,
    note_id: str, embeddable: str, file_path: Path,
) -> None:
    """Suggest connections for a note via the auto-connections managed block."""
    similar = idx.query_similar_notes(embeddable, n_results=cfg.linking.topk, exclude_id=note_id)
    if not similar:
        return

    links: list[str] = []
    for n in similar:
        nid = n.get("id", "?")
        meta = n.get("metadata", {})
        title = meta.get("title", "Sem título")
        links.append(f"- [[ZTL - {nid} - {_slug(title)}]]")

    if links:
        safe_update_managed_blocks(file_path, {
            "auto-connections": "\n".join(links),
        })


def _rewrite_frontmatter(file_path: Path, meta: dict, body: str) -> None:
    """Rewrite a file with updated frontmatter."""
    from zettel.vault import compose_note
    content = compose_note(meta, body)
    file_path.write_text(content, encoding="utf-8")
