"""Complete removal of a harvested source from vault, SQLite, and Chroma."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.index import VectorIndex
from zettel.state import StateDB
from zettel.vault import (
    compose_note,
    literature_chunk_filename_for_row,
    literature_index_filename,
    literature_index_stem,
    literature_source_dirname,
    parse_frontmatter,
    permanent_wikilink,
    source_note_filename,
    source_note_stem,
    strip_matching_wikilinks,
)

logger = logging.getLogger(__name__)

_VAULT_SCAN_DIRS = (
    "10_Sources",
    "20_Literature",
    "30_Permanent",
    "40_MOCs",
    "00_Inbox",
)


def normalize_source_id(citekey: str) -> str:
    key = citekey.strip()
    return key if key.startswith("@") else f"@{key}"


def collect_link_targets(
    *,
    citekey: str,
    title: str,
    chunks: list[dict],
    permanent_note_ids: list[str],
    db: StateDB,
) -> set[str]:
    """Build wikilink targets that become dead after deleting a source."""
    targets: set[str] = set()
    targets.add(source_note_stem(citekey, title))
    targets.add(literature_index_stem(citekey, title))
    dirname = literature_source_dirname(citekey)
    targets.add(dirname)
    for chunk in chunks:
        stem = literature_chunk_filename_for_row(citekey, chunk).removesuffix(".md")
        targets.add(stem)
        targets.add(f"{dirname}/{stem}")
        lit_id = chunk.get("literature_id")
        if lit_id:
            targets.add(str(lit_id))
    for note_id in permanent_note_ids:
        row = db.get_note(note_id)
        if not row:
            continue
        targets.add(note_id)
        targets.add(
            permanent_wikilink(
                note_id,
                row.get("title") or "",
                path=row.get("path"),
            ).strip("[]")
        )
        if row.get("path"):
            targets.add(Path(row["path"]).stem)
    return targets


def _resolve_src_path(cfg: AppConfig, source_id: str, citekey: str, title: str) -> Path | None:
    path = cfg.vault_path / "10_Sources" / source_note_filename(citekey, title)
    if path.exists():
        return path
    sources_dir = cfg.vault_path / "10_Sources"
    if not sources_dir.exists():
        return None
    for candidate in sources_dir.glob("SRC - *.md"):
        meta, _ = parse_frontmatter(candidate.read_text(encoding="utf-8"))
        if meta.get("source_id") == source_id:
            return candidate
    return path if path.exists() else None


def _remove_tree(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_file():
        path.unlink()
        return True
    shutil.rmtree(path)
    return True


def _delete_vault_source_files(
    cfg: AppConfig,
    db: StateDB,
    *,
    source_id: str,
    citekey: str,
    title: str,
) -> dict[str, int]:
    removed = {
        "src": 0,
        "lit_index": 0,
        "lit_granular": 0,
        "review_drafts": 0,
        "assets": 0,
    }

    src_path = _resolve_src_path(cfg, source_id, citekey, title)
    if src_path and _remove_tree(src_path):
        removed["src"] = 1

    lit_index = cfg.vault_path / "20_Literature" / literature_index_filename(citekey, title)
    if _remove_tree(lit_index):
        removed["lit_index"] = 1

    lit_dir = cfg.vault_path / "20_Literature" / literature_source_dirname(citekey)
    if lit_dir.exists():
        count = sum(1 for _ in lit_dir.rglob("*.md"))
        if _remove_tree(lit_dir):
            removed["lit_granular"] = count

    review_dir = cfg.vault_path / "00_Inbox" / "Review" / literature_source_dirname(citekey)
    if review_dir.exists():
        count = sum(1 for _ in review_dir.rglob("*.md"))
        if _remove_tree(review_dir):
            removed["review_drafts"] = count

    for asset in db.get_assets_for_source(source_id):
        rel = asset.get("path")
        if not rel:
            continue
        asset_path = cfg.vault_path / rel
        if asset_path.is_file():
            asset_path.unlink()
            removed["assets"] += 1

    return removed


def _clean_note_file(
    path: Path,
    link_targets: set[str],
    db: StateDB,
    *,
    deleted_source_id: str | None = None,
) -> bool:
    if not path.is_file():
        return False
    original = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(original)
    cleaned_body = strip_matching_wikilinks(body, link_targets)
    meta_changed = False
    if meta and deleted_source_id and meta.get("source_id") == deleted_source_id:
        meta.pop("source_id", None)
        meta_changed = True
    if cleaned_body == body and not meta_changed:
        return False
    if meta:
        meta["updated_at"] = datetime.now(UTC).isoformat()
        content = compose_note(meta, cleaned_body)
    else:
        content = cleaned_body
    path.write_text(content, encoding="utf-8")
    note_id = meta.get("note_id") if meta else None
    if note_id and db.get_note(note_id):
        db.upsert_note(
            note_id,
            meta.get("source_id"),
            str(path),
            title=meta.get("title") or path.stem,
            body=cleaned_body,
            frontmatter_json=json.dumps(meta, ensure_ascii=False),
            origin=meta.get("origin") or "pipeline",
        )
    return True


def clean_wikilinks_in_vault(
    cfg: AppConfig,
    db: StateDB,
    link_targets: set[str],
    *,
    exclude_paths: set[Path] | None = None,
    deleted_source_id: str | None = None,
) -> int:
    """Strip dead wikilinks from surviving vault notes. Returns files updated."""
    exclude = {p.resolve() for p in (exclude_paths or set())}
    updated = 0
    for subdir in _VAULT_SCAN_DIRS:
        scan_root = cfg.vault_path / subdir
        if not scan_root.exists():
            continue
        for md_file in scan_root.rglob("*.md"):
            if md_file.resolve() in exclude:
                continue
            if _clean_note_file(
                md_file,
                link_targets,
                db,
                deleted_source_id=deleted_source_id,
            ):
                updated += 1
    return updated


def purge_source(
    cfg: AppConfig,
    db: StateDB,
    idx: VectorIndex,
    source_id: str,
    *,
    delete_permanent: bool = False,
    compact: bool = True,
) -> dict[str, Any]:
    """Delete a source completely from vault, SQLite, and Chroma.

    When ``delete_permanent`` is False (default), permanent notes (ZTL) linked to
    the source are kept but wikilinks to removed SRC/LIT notes are stripped from
    all surviving vault notes. When True, those ZTL notes are also removed.

    Follows the same Chroma/SQLite cleanup patterns as ``purge_rejected``.
    """
    source_id = normalize_source_id(source_id)
    source = db.get_source(source_id)
    if not source:
        return {"found": False}

    citekey = source["citekey"]
    title = source.get("title") or citekey
    chunks = db.get_chunks_for_source(source_id)
    chunk_ids = [c["chunk_id"] for c in chunks]
    lit_ids = [
        lit
        for lit in (
            [c.get("literature_id") for c in chunks if c.get("literature_id")]
            + [f"{source_id}::index"]
        )
        if lit
    ]
    permanent_ids = db.get_note_ids_for_source(source_id)
    link_targets = collect_link_targets(
        citekey=citekey,
        title=title,
        chunks=chunks,
        permanent_note_ids=permanent_ids if delete_permanent else [],
        db=db,
    )

    vault_removed = _delete_vault_source_files(
        cfg,
        db,
        source_id=source_id,
        citekey=citekey,
        title=title,
    )

    permanent_vault_removed = 0
    exclude_paths: set[Path] = set()
    if delete_permanent:
        for note_id in permanent_ids:
            row = db.get_note(note_id)
            if not row or not row.get("path"):
                continue
            note_path = Path(row["path"])
            exclude_paths.add(note_path.resolve())
            if note_path.is_file():
                note_path.unlink()
                permanent_vault_removed += 1
        for note_id in permanent_ids:
            db.delete_note(note_id)
        if permanent_ids:
            try:
                idx.delete_permanent_notes(permanent_ids)
            except Exception as e:
                logger.warning("Falha ao limpar notas permanentes no Chroma: %s", e)
    else:
        db.clear_source_id_on_notes(source_id)

    wikilinks_cleaned = clean_wikilinks_in_vault(
        cfg,
        db,
        link_targets,
        exclude_paths=exclude_paths,
        deleted_source_id=source_id if not delete_permanent else None,
    )

    if chunk_ids:
        idx.delete_chunks(chunk_ids)
    if lit_ids:
        try:
            idx.delete_literature_notes(lit_ids)
        except Exception as e:
            logger.warning("Falha ao limpar literature_notes no Chroma: %s", e)
    try:
        idx.delete_sources([source_id])
    except Exception as e:
        logger.warning("Falha ao limpar source no Chroma: %s", e)

    sqlite_removed = db.delete_source_cascade(source_id)

    result: dict[str, Any] = {
        "found": True,
        "source_id": source_id,
        "citekey": citekey,
        "vault": vault_removed,
        "permanent_vault_removed": permanent_vault_removed,
        "permanent_deleted": len(permanent_ids) if delete_permanent else 0,
        "wikilinks_cleaned": wikilinks_cleaned,
        "sqlite": sqlite_removed,
        "chunks_chroma": len(chunk_ids),
        "literature_chroma": len(lit_ids),
        "compacted": False,
        "state_mb_before": 0.0,
        "state_mb_after": 0.0,
        "chroma_mb_before": 0.0,
        "chroma_mb_after": 0.0,
    }

    if compact and (
        sqlite_removed.get("chunks") or sqlite_removed.get("sources") or delete_permanent
    ):
        state_path = Path(db.db_path)
        chroma_db = Path(cfg.chroma_path) / "chroma.sqlite3"
        result["state_mb_before"] = round(state_path.stat().st_size / 1e6, 2)
        result["chroma_mb_before"] = (
            round(chroma_db.stat().st_size / 1e6, 2) if chroma_db.exists() else 0.0
        )
        db.vacuum()
        idx.vacuum()
        result["state_mb_after"] = round(state_path.stat().st_size / 1e6, 2)
        result["chroma_mb_after"] = (
            round(chroma_db.stat().st_size / 1e6, 2) if chroma_db.exists() else 0.0
        )
        result["compacted"] = True
        logger.info(
            "Compactacao: state %.2f->%.2f MB, chroma.sqlite3 %.2f->%.2f MB",
            result["state_mb_before"],
            result["state_mb_after"],
            result["chroma_mb_before"],
            result["chroma_mb_after"],
        )

    logger.info(
        "Purge source %s: vault=%s sqlite_chunks=%d permanent_deleted=%d wikilinks_cleaned=%d",
        source_id,
        vault_removed,
        sqlite_removed.get("chunks", 0),
        result["permanent_deleted"],
        wikilinks_cleaned,
    )
    return result
