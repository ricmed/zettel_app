"""Sync manual — detect manual notes, index them, suggest connections."""

from __future__ import annotations

import logging
from pathlib import Path

from ulid import ULID

from zettel.config import AppConfig
from zettel.hashing import extract_embeddable_text, normalize_text_for_hash, sha256_hex
from zettel.index import VectorIndex
from zettel.state import StateDB
from zettel.vault import parse_frontmatter, safe_update_managed_blocks, _slug

logger = logging.getLogger(__name__)


def run_sync_manual(cfg: AppConfig, db: StateDB, idx: VectorIndex) -> dict[str, int]:
    """Scan vault for manual/modified notes and sync them to the index.

    Returns stats: {"new": N, "updated": M, "skipped": K}
    """
    stats = {"new": 0, "updated": 0, "skipped": 0}

    # Scan permanent notes and MOCs
    dirs_to_scan = [
        (cfg.vault_path / "30_Permanent", "permanent"),
        (cfg.vault_path / "40_MOCs", "moc"),
    ]

    for scan_dir, note_type in dirs_to_scan:
        if not scan_dir.exists():
            continue
        for md_file in scan_dir.glob("*.md"):
            result = _sync_single_note(cfg, db, idx, md_file, note_type)
            stats[result] += 1

    logger.info(
        "Sync manual: %d novas, %d atualizadas, %d sem alteração",
        stats["new"], stats["updated"], stats["skipped"],
    )
    return stats


def _sync_single_note(
    cfg: AppConfig, db: StateDB, idx: VectorIndex,
    file_path: Path, note_type: str,
) -> str:
    """Sync a single note file. Returns 'new', 'updated', or 'skipped'."""
    content = file_path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(content)

    # Get or create ID
    if note_type == "permanent":
        note_id = meta.get("note_id")
        if not note_id:
            note_id = str(ULID())
            meta["note_id"] = note_id
            meta["type"] = "permanent"
            _rewrite_frontmatter(file_path, meta, body)

        # Compute semantic checksum
        embeddable = extract_embeddable_text(body)
        semantic_checksum = sha256_hex(normalize_text_for_hash(embeddable))

        # Check if already indexed
        existing = db.get_note(note_id)
        if existing and existing.get("note_semantic_checksum") == semantic_checksum:
            return "skipped"

        # Upsert to state
        title = meta.get("title", file_path.stem)
        source_id = meta.get("source_id")
        db.upsert_note(
            note_id=note_id, source_id=source_id, path=str(file_path),
            title=title, note_semantic_checksum=semantic_checksum,
            embedding_model=cfg.embedding.model,
        )

        # Upsert to index
        tags = meta.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        idx.upsert_permanent_note(note_id, embeddable, {
            "title": title,
            "source_id": source_id or "",
            "tags": ", ".join(tags),
        })

        # Suggest connections via managed block
        _suggest_connections(cfg, db, idx, note_id, embeddable, file_path)

        return "new" if not existing else "updated"

    elif note_type == "moc":
        moc_id = meta.get("moc_id")
        if not moc_id:
            moc_id = str(ULID())
            meta["moc_id"] = moc_id
            meta["type"] = "moc"
            _rewrite_frontmatter(file_path, meta, body)

        topic = meta.get("topic", file_path.stem)
        embeddable = extract_embeddable_text(body)
        semantic_checksum = sha256_hex(normalize_text_for_hash(embeddable))

        existing = db.get_moc_by_signature(semantic_checksum)
        if existing:
            return "skipped"

        db.upsert_moc(moc_id, topic, str(file_path), semantic_checksum)
        idx.upsert_moc(moc_id, embeddable, {"topic": topic})

        return "new" if not existing else "updated"

    return "skipped"


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
