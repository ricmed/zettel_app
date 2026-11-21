"""Retain the current vault markdown in SQLite so a missing file can be recreated.

The file stays the live record. ``flush`` copies it into the store for every
origin: permanent notes and MOCs (body + frontmatter), the literature index,
the granular LIT, the SRC card and Mesa projects. The skip key is
:func:`whole_file_sha256` of the whole file, so a frontmatter-only edit or a
managed block refreshes the row. Concept status is not touched and nothing
goes through the review gate.

``sync-manual`` calls ``flush`` after adoption. Backup calls the same function.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from zettel.config import AppConfig
from zettel.hashing import file_sha256, sha256_hex
from zettel.projects import _PROJECT_ID, PROJECT_TYPE
from zettel.state import StateDB
from zettel.stored_path import store_vault
from zettel.vault import (
    PROJECTS_DIR,
    MalformedFrontmatter,
    frontmatter_json,
    parse_frontmatter_strict,
)

logger = logging.getLogger(__name__)


def whole_file_sha256(path: Path | str) -> str:
    """SHA-256 of every byte in ``path``. The retention flush skip key."""
    return file_sha256(path)


def flush(cfg: AppConfig, db: StateDB) -> dict[str, int]:
    """Copy the current vault markdown into SQLite.

    Updates rows that already exist for ZTL and MOC notes of every origin,
    including pipeline. A ZTL or MOC that has no row is left for adoption
    (``sync-manual`` does not adopt pipeline notes). Granular LIT, the
    literature index, the SRC card and Mesa projects are stored in full.
    A manual granular LIT with no chunk yet is adopted through
    ``adopt_manual_literature`` and then its ``literature_body`` is set.

    Counts: ``notes``, ``mocs``, ``literature``, ``literature_index``,
    ``sources``, ``projects`` (rows written), ``skipped`` (whole-file checksum
    unchanged) and ``unmatched`` (a file that could not be attached).
    """
    stats = {
        "notes": 0,
        "mocs": 0,
        "literature": 0,
        "literature_index": 0,
        "sources": 0,
        "projects": 0,
        "skipped": 0,
        "unmatched": 0,
    }
    vault = Path(cfg.vault_path)
    _walk_split(db, vault / "30_Permanent", "note_id", _retain_note, stats)
    _walk_split(db, vault / "40_MOCs", "moc_id", _retain_moc, stats)
    _walk_sources(db, vault / "10_Sources", stats)
    _walk_literature(cfg, db, vault, stats)
    _walk_projects(db, vault, stats)
    logger.info(
        "Retencao: notes=%d mocs=%d literature=%d index=%d sources=%d "
        "projects=%d skipped=%d unmatched=%d",
        stats["notes"],
        stats["mocs"],
        stats["literature"],
        stats["literature_index"],
        stats["sources"],
        stats["projects"],
        stats["skipped"],
        stats["unmatched"],
    )
    return stats


def path_inside_vault(vault: Path, stored: str) -> Path | None:
    """Absolute path of ``stored`` when it stays inside ``vault``.

    Relative paths are joined to the vault. ``..``, an absolute path outside
    the vault, the vault root itself and a directory are refused.
    """
    if not stored or "\x00" in stored:
        return None
    root = Path(vault).resolve()
    try:
        raw = Path(stored)
        dest = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
        dest.relative_to(root)
    except (OSError, ValueError):
        return None
    if dest == root or dest.is_dir():
        return None
    return dest


def retained_projects(db: StateDB) -> list[dict]:
    """Mesa retention rows, in project id order. The file stays the live record."""
    return db._fetchall(
        "SELECT project_id, path, body, frontmatter_json, file_checksum "
        "FROM projects ORDER BY project_id"
    )


def _snapshot(path: Path) -> tuple[str, str] | None:
    """``(decoded text, whole-file sha256)``. ``None`` when the file cannot be read."""
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except OSError:
        logger.warning("Retencao: nao leu %s", path)
        return None
    except UnicodeDecodeError:
        logger.warning("Retencao: %s nao e UTF-8", path)
        return None
    return text, hashlib.sha256(raw).hexdigest()


def _markdown(folder: Path, *, nested: bool) -> list[Path]:
    if not folder.is_dir():
        return []
    pattern = "**/*.md" if nested else "*.md"
    return [path for path in sorted(folder.glob(pattern)) if path.is_file()]


def _same_text(stored: str | None, digest: str) -> bool:
    """True when ``stored`` is the exact UTF-8 of the file ``digest`` names."""
    return stored is not None and sha256_hex(stored) == digest


def _parse(text: str, path: Path, stats: dict[str, int]) -> tuple[dict, str] | None:
    try:
        meta, body = parse_frontmatter_strict(text)
    except MalformedFrontmatter as exc:
        logger.warning("Retencao: %s ignorado (%s)", path.name, exc)
        stats["unmatched"] += 1
        return None
    return meta, body


def _walk_split(db: StateDB, folder: Path, id_key: str, retain, stats: dict[str, int]) -> None:
    for path in _markdown(folder, nested=False):
        snap = _snapshot(path)
        if snap is None:
            stats["unmatched"] += 1
            continue
        text, digest = snap
        parsed = _parse(text, path, stats)
        if parsed is None:
            continue
        meta, body = parsed
        retain(db, path, meta, body, digest, id_key, stats)


def _retain_note(
    db: StateDB,
    path: Path,
    meta: dict,
    body: str,
    digest: str,
    id_key: str,
    stats: dict[str, int],
) -> None:
    note_id = str(meta.get(id_key) or "")
    row = db.get_note(note_id) if note_id else None
    if row is None:
        logger.debug("Retencao: sem nota para %s", path.name)
        stats["unmatched"] += 1
        return
    if row.get("file_checksum") == digest:
        stats["skipped"] += 1
        return
    db.conn.execute(
        """UPDATE notes
              SET body=?, frontmatter_json=?, file_checksum=?, updated_at=?
            WHERE note_id=?""",
        (body, frontmatter_json(meta), digest, db._now(), note_id),
    )
    db._fts_index_note(note_id)
    db.conn.commit()
    stats["notes"] += 1


def _retain_moc(
    db: StateDB,
    path: Path,
    meta: dict,
    body: str,
    digest: str,
    id_key: str,
    stats: dict[str, int],
) -> None:
    moc_id = str(meta.get(id_key) or "")
    row = db.get_moc(moc_id) if moc_id else None
    if row is None:
        logger.debug("Retencao: sem MOC para %s", path.name)
        stats["unmatched"] += 1
        return
    if row.get("file_checksum") == digest:
        stats["skipped"] += 1
        return
    db.conn.execute(
        """UPDATE mocs
              SET body=?, frontmatter_json=?, file_checksum=?, updated_at=?
            WHERE moc_id=?""",
        (body, frontmatter_json(meta), digest, db._now(), moc_id),
    )
    db.conn.commit()
    stats["mocs"] += 1


def _walk_sources(db: StateDB, folder: Path, stats: dict[str, int]) -> None:
    for path in _markdown(folder, nested=False):
        snap = _snapshot(path)
        if snap is None:
            stats["unmatched"] += 1
            continue
        text, digest = snap
        parsed = _parse(text, path, stats)
        if parsed is None:
            continue
        meta, _body = parsed
        if str(meta.get("type") or "source") != "source":
            stats["unmatched"] += 1
            continue
        source_id = str(meta.get("source_id") or "")
        row = db.get_source(source_id) if source_id else None
        if row is None:
            logger.debug("Retencao: sem fonte para %s", path.name)
            stats["unmatched"] += 1
            continue
        if _same_text(row.get("card_body"), digest):
            stats["skipped"] += 1
            continue
        db.conn.execute(
            "UPDATE sources SET card_body=?, updated_at=? WHERE source_id=?",
            (text, db._now(), source_id),
        )
        db.conn.commit()
        stats["sources"] += 1


def _literature_kind(vault: Path, path: Path, meta: dict) -> str:
    """``granular``, ``index`` or ``skip``. Same split ``sync-manual`` uses."""
    folder = vault / "20_Literature"
    in_source_folder = len(path.relative_to(folder).parts) > 1
    note_type = str(meta.get("type") or "literature")
    is_granular = note_type == "literature" and bool(str(meta.get("chunk_id") or ""))
    if in_source_folder and not is_granular:
        return "skip"
    if is_granular:
        return "granular"
    if note_type in ("literature", "literature_index"):
        return "index"
    return "skip"


def _source_id_of(meta: dict) -> str:
    raw = str(meta.get("source_id") or "")
    if raw and "::" not in raw:
        return raw if raw.startswith("@") else f"@{raw.lstrip('@')}"
    citekey = str(meta.get("citekey") or "").lstrip("@")
    return f"@{citekey}" if citekey else ""


def _walk_literature(cfg: AppConfig, db: StateDB, vault: Path, stats: dict[str, int]) -> None:
    folder = vault / "20_Literature"
    indexes: list[Path] = []
    seen: set[Path] = set()
    for path in _markdown(folder, nested=True):
        seen.add(path.resolve())
        snap = _snapshot(path)
        if snap is None:
            stats["unmatched"] += 1
            continue
        text, digest = snap
        parsed = _parse(text, path, stats)
        if parsed is None:
            continue
        meta, body = parsed
        kind = _literature_kind(vault, path, meta)
        if kind == "granular":
            _retain_granular(cfg, db, path, text, digest, meta, body, stats)
        elif kind == "index":
            indexes.append(path)
        else:
            stats["unmatched"] += 1
    # Adoption of a manual LIT may create or rewrite the literature index.
    for path in indexes:
        _retain_index_file(db, vault, path, stats)
    for path in _markdown(folder, nested=True):
        if path.resolve() in seen:
            continue
        _retain_index_file(db, vault, path, stats)


def _retain_index_file(db: StateDB, vault: Path, path: Path, stats: dict[str, int]) -> None:
    snap = _snapshot(path)
    if snap is None:
        stats["unmatched"] += 1
        return
    text, digest = snap
    parsed = _parse(text, path, stats)
    if parsed is None:
        return
    meta, _body = parsed
    if _literature_kind(vault, path, meta) != "index":
        return
    _retain_index(db, path, text, digest, meta, stats)


def _find_chunk(db: StateDB, path: Path, meta: dict, vault: Path) -> dict | None:
    """Match a granular LIT the way sync already identifies it.

    ``chunk_id`` first, then ``literature_id``, then ``literature_note_path``.
    """
    chunk_id = str(meta.get("chunk_id") or "")
    if chunk_id:
        row = db.get_chunk(chunk_id)
        if row:
            return row
    literature_id = str(meta.get("literature_id") or "")
    if literature_id:
        row = db._fetchone(
            "SELECT * FROM chunks WHERE literature_id=? ORDER BY chunk_index, chunk_id LIMIT 1",
            (literature_id,),
        )
        if row:
            return row
    target = store_vault(path, vault)
    resolved = str(path.resolve())
    row = db._fetchone(
        "SELECT * FROM chunks WHERE literature_note_path IN (?, ?) LIMIT 1",
        (target, resolved),
    )
    if row:
        return row
    folded = target.replace("\\", "/")
    return db._fetchone(
        "SELECT * FROM chunks WHERE replace(literature_note_path, '\\', '/') = ? LIMIT 1",
        (folded,),
    )


def _retain_granular(
    cfg: AppConfig,
    db: StateDB,
    path: Path,
    text: str,
    digest: str,
    meta: dict,
    body: str,
    stats: dict[str, int],
) -> None:
    chunk = _find_chunk(db, path, meta, Path(cfg.vault_path))
    if chunk is None and str(meta.get("origin") or "manual") == "manual":
        from zettel.manual_lit import adopt_manual_literature

        adopt_manual_literature(cfg, db, None, path, meta, body)
        snap = _snapshot(path)
        if snap is None:
            stats["unmatched"] += 1
            return
        text, digest = snap
        parsed = _parse(text, path, stats)
        if parsed is None:
            return
        meta, _body = parsed
        chunk = _find_chunk(db, path, meta, Path(cfg.vault_path))
    if chunk is None:
        logger.debug("Retencao: sem chunk para %s", path.name)
        stats["unmatched"] += 1
        return
    if _same_text(chunk.get("literature_body"), digest):
        stats["skipped"] += 1
        return
    db.conn.execute(
        "UPDATE chunks SET literature_body=? WHERE chunk_id=?",
        (text, chunk["chunk_id"]),
    )
    db.conn.commit()
    stats["literature"] += 1


def _retain_index(
    db: StateDB, path: Path, text: str, digest: str, meta: dict, stats: dict[str, int]
) -> None:
    source_id = _source_id_of(meta)
    row = db.get_source(source_id) if source_id else None
    if row is None:
        logger.debug("Retencao: sem fonte para o indice %s", path.name)
        stats["unmatched"] += 1
        return
    if _same_text(row.get("lit_body"), digest):
        stats["skipped"] += 1
        return
    db.conn.execute(
        "UPDATE sources SET lit_body=?, updated_at=? WHERE source_id=?",
        (text, db._now(), source_id),
    )
    db.conn.commit()
    stats["literature_index"] += 1


def _walk_projects(db: StateDB, vault: Path, stats: dict[str, int]) -> None:
    for path in _markdown(vault / PROJECTS_DIR, nested=False):
        snap = _snapshot(path)
        if snap is None:
            stats["unmatched"] += 1
            continue
        text, digest = snap
        parsed = _parse(text, path, stats)
        if parsed is None:
            continue
        meta, body = parsed
        if str(meta.get("type") or "") != PROJECT_TYPE:
            continue
        project_id = str(meta.get("project_id") or "")
        if not _PROJECT_ID.fullmatch(project_id):
            stats["unmatched"] += 1
            continue
        row = db._fetchone("SELECT file_checksum FROM projects WHERE project_id=?", (project_id,))
        if row and row["file_checksum"] == digest:
            stats["skipped"] += 1
            continue
        db.conn.execute(
            """INSERT INTO projects (project_id, path, body, frontmatter_json, file_checksum)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(project_id) DO UPDATE SET
                 path=excluded.path,
                 body=excluded.body,
                 frontmatter_json=excluded.frontmatter_json,
                 file_checksum=excluded.file_checksum""",
            (project_id, store_vault(path, vault), body, frontmatter_json(meta), digest),
        )
        db.conn.commit()
        stats["projects"] += 1
