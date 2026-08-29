"""Sync MOC back-reference blocks on permanent notes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from zettel.gardener_assign import extract_note_ids_from_moc_body
from zettel.vault import note_filename, parse_frontmatter, read_managed_block, safe_update_managed_blocks

if TYPE_CHECKING:
    from zettel.state import StateDB

logger = logging.getLogger(__name__)

MOC_BACKREFS_BLOCK = "auto-moc-backrefs"


def moc_wikilink(
    moc_id: str,
    topic: str = "",
    *,
    path: str | Path | None = None,
) -> str:
    """Build a wikilink to a MOC note file on disk."""
    if path:
        stem = Path(path).stem
        if stem:
            return f"[[{stem}]]"
    slug_topic = topic or moc_id
    prefix = "HUB" if str(path or "").startswith("HUB -") else "MOC"
    return f"[[{note_filename(prefix, moc_id, slug_topic).removesuffix('.md')}]]"


def moc_link_line(
    moc_id: str,
    topic: str = "",
    *,
    path: str | Path | None = None,
) -> str:
    return f"- {moc_wikilink(moc_id, topic, path=path)}"


def _note_path_from_db(db: StateDB, note_id: str) -> Path | None:
    note = db.get_note(note_id)
    if not note or not note.get("path"):
        return None
    path = Path(note["path"])
    return path if path.is_file() else None


def _link_references_moc(line: str, moc_id: str) -> bool:
    return moc_id in line


def _add_moc_link_to_note(note_path: Path, link_line: str) -> None:
    content = note_path.read_text(encoding="utf-8")
    existing = read_managed_block(content, MOC_BACKREFS_BLOCK)
    if existing and link_line.strip() in existing:
        return
    inner = f"{existing}\n{link_line}".strip() if existing else link_line
    safe_update_managed_blocks(note_path, {MOC_BACKREFS_BLOCK: inner})


def _remove_moc_link_from_note(note_path: Path, moc_id: str) -> None:
    content = note_path.read_text(encoding="utf-8")
    existing = read_managed_block(content, MOC_BACKREFS_BLOCK)
    if not existing:
        return
    lines = [
        line for line in existing.splitlines()
        if line.strip() and not _link_references_moc(line, moc_id)
    ]
    safe_update_managed_blocks(note_path, {MOC_BACKREFS_BLOCK: "\n".join(lines)})


def sync_moc_backrefs(
    db: StateDB,
    moc_id: str,
    moc_topic: str,
    moc_path: Path | str,
    *,
    previous_body: str | None = None,
    new_body: str | None = None,
) -> None:
    """Update permanent-note backref blocks after a MOC is written or changed."""
    path = Path(moc_path)
    if new_body is None:
        if not path.is_file():
            return
        _, new_body = parse_frontmatter(path.read_text(encoding="utf-8"))

    new_ids = extract_note_ids_from_moc_body(new_body or "")
    old_ids = extract_note_ids_from_moc_body(previous_body or "") if previous_body else set()
    link_line = moc_link_line(moc_id, moc_topic, path=path)

    for note_id in old_ids - new_ids:
        note_path = _note_path_from_db(db, note_id)
        if note_path:
            _remove_moc_link_from_note(note_path, moc_id)

    for note_id in new_ids - old_ids:
        note_path = _note_path_from_db(db, note_id)
        if note_path:
            _add_moc_link_to_note(note_path, link_line)


def clear_moc_backrefs(db: StateDB, moc: dict) -> None:
    """Remove this MOC from all permanent notes that referenced it."""
    body = moc.get("body") or ""
    if not body and moc.get("path"):
        moc_path = Path(moc["path"])
        if moc_path.is_file():
            _, body = parse_frontmatter(moc_path.read_text(encoding="utf-8"))

    moc_id = moc.get("moc_id", "")
    if not moc_id:
        return

    for note_id in extract_note_ids_from_moc_body(body):
        note_path = _note_path_from_db(db, note_id)
        if note_path:
            _remove_moc_link_from_note(note_path, moc_id)
