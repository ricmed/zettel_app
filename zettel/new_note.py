"""Scaffold manual vault notes for later adoption via ``sync-manual``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ulid import ULID

from zettel.config import AppConfig
from zettel.vault import (
    author_year_label,
    build_literature_chunk_note,
    build_literature_index_note,
    build_permanent_note_body,
    build_source_note,
    literature_chunk_filename,
    literature_index_filename,
    literature_source_dirname,
    note_filename,
    parse_frontmatter,
    safe_write_note,
    source_note_filename,
    source_note_stem,
)

_NOTE_TYPE_ALIASES: dict[str, str] = {
    "ztl": "permanent",
    "permanent": "permanent",
    "lit": "literature",
    "literature": "literature",
    "src": "source",
    "source": "source",
    "moc": "moc",
}


@dataclass
class NewNoteResult:
    path: Path
    note_type: str
    meta: dict[str, Any]
    warnings: list[str] | None = None


def normalize_note_type(raw: str) -> str:
    """Map CLI aliases (ztl, lit, src, moc) to internal note types."""
    key = raw.strip().lower()
    if key not in _NOTE_TYPE_ALIASES:
        allowed = ", ".join(sorted(_NOTE_TYPE_ALIASES))
        raise ValueError(f"Tipo de nota invalido: {raw!r}. Use um de: {allowed}")
    return _NOTE_TYPE_ALIASES[key]


def provisional_citekey(
    authors: list[str] | None,
    year: int | None,
    title: str,
) -> str:
    """Derive a citekey from metadata without touching SQLite (sync may refine)."""
    surname = ""
    if authors and authors[0]:
        parts = authors[0].strip().split()
        if parts:
            surname = parts[-1]

    words = re.sub(r"[^\w\s]", "", title).split()

    if surname and year is not None:
        slug_words = [w.capitalize() for w in words[:2]]
        slug = "".join(slug_words) if slug_words else "Untitled"
        return f"{surname}{year}{slug}"
    if surname:
        slug_words = [w.capitalize() for w in words[:3]]
        slug = "".join(slug_words) if slug_words else "Untitled"
        return f"{surname}{slug}"
    if year is not None:
        slug_words = [w.capitalize() for w in words[:3]]
        slug = "".join(slug_words) if slug_words else "Untitled"
        return f"{year}{slug}"
    slug_words = [w.capitalize() for w in words[:4]]
    return "".join(slug_words) if slug_words else "Untitled"


def _resolve_citekey(
    citekey: str | None,
    authors: list[str] | None,
    year: int | None,
    title: str,
) -> str:
    key = citekey.lstrip("@") if citekey else provisional_citekey(authors, year, title)
    return _validate_citekey(key)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def normalize_source_id(raw: str) -> str:
    """Normalize citekey/source_id to ``@Citekey`` form."""
    key = raw.strip().lstrip("@")
    return f"@{_validate_citekey(key)}"


def _validate_citekey(key: str) -> str:
    """Reject source identifiers that could escape vault directories."""
    if (
        not key
        or len(key) > 160
        or re.fullmatch(r"[\w][\w.:-]*", key, flags=re.UNICODE) is None
    ):
        raise ValueError(
            "source_id/citekey invalido; use apenas letras, numeros, ponto, "
            "hifen, sublinhado ou dois-pontos"
        )
    return key


def resolve_src_in_vault(
    cfg: AppConfig,
    source_id: str,
) -> tuple[Path | None, dict[str, Any] | None]:
    """Locate the SRC note in ``10_Sources/`` by ``source_id`` or citekey."""
    citekey = source_id.lstrip("@")
    sources_dir = cfg.vault_path / "10_Sources"
    if not sources_dir.is_dir():
        return None, None
    for candidate in sources_dir.glob("SRC - *.md"):
        meta, _ = parse_frontmatter(candidate.read_text(encoding="utf-8"))
        sid = str(meta.get("source_id") or "")
        ck = str(meta.get("citekey") or "")
        if sid == source_id or sid.lstrip("@") == citekey or ck == citekey:
            return candidate, meta
    return None, None


def source_wikilink(
    citekey: str,
    *,
    path: Path | None = None,
    title: str = "",
) -> str:
    """Build a wikilink to the SRC note (exact stem when path is known)."""
    if path is not None:
        return f"[[{path.stem}]]"
    if title:
        return f"[[{source_note_stem(citekey, title)}]]"
    return f"[[SRC - {author_year_label(citekey)}]]"


def _write_scaffold(path: Path, meta: dict[str, Any], body: str, *, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"Arquivo ja existe: {path}")
    _ensure_parent(path)
    safe_write_note(path, meta, body)


def _collect_biblio_fields(
    *,
    place: str | None = None,
    publisher: str | None = None,
    doi: str | None = None,
    url: str | None = None,
    journal: str | None = None,
    edition: str | None = None,
    institution: str | None = None,
    pages: str | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, val in (
        ("place", place),
        ("publisher", publisher),
        ("doi", doi),
        ("url", url),
        ("journal", journal),
        ("edition", edition),
        ("institution", institution),
        ("pages", pages),
    ):
        if val:
            fields[key] = val
    return fields


def _append_src_ztl_hints(
    body: str,
    *,
    source_id: str,
    citekey: str,
    title: str,
    path: Path,
) -> str:
    src_link = source_wikilink(citekey, path=path, title=title)
    return (
        f"{body}\n"
        "## Referencia para notas permanentes\n\n"
        f"- **source_id** (frontmatter ZTL): `{source_id}`\n"
        f"- Wikilink desta fonte: {src_link}\n"
        "- Em ZTL, use `source_id` no frontmatter e cite esta nota ou uma LIT em **Fonte**.\n"
    )


def _write_literature_index(
    cfg: AppConfig, source_id: str, citekey: str, title: str, *, force: bool = False,
) -> Path:
    """Create the source's literature index note, mirroring what harvest writes.

    Without it the SRC note's `## Indice de Literatura` wikilink is born dead. An
    index that already exists is left alone: it carries the `auto-lit-index` block
    that review/sync maintain.
    """
    path = cfg.vault_path / "20_Literature" / literature_index_filename(citekey, title)
    if path.exists() and not force:
        return path
    meta, body = build_literature_index_note(
        source_id=source_id, citekey=citekey, title=title, origin="manual",
    )
    _ensure_parent(path)
    safe_write_note(path, meta, body)
    return path


def scaffold_manual_note(
    cfg: AppConfig,
    note_type: str,
    title: str,
    *,
    citekey: str | None = None,
    source_id: str | None = None,
    authors: list[str] | None = None,
    year: int | None = None,
    document_type: str | None = None,
    abnt_reference: str | None = None,
    place: str | None = None,
    publisher: str | None = None,
    doi: str | None = None,
    url: str | None = None,
    journal: str | None = None,
    edition: str | None = None,
    institution: str | None = None,
    pages: str | None = None,
    thesis: str | None = None,
    granular: bool = False,
    chunk_index: int = 1,
    page: int | None = None,
    force: bool = False,
) -> NewNoteResult:
    """Create a manual note file in the vault. Does not index into SQLite/Chroma."""
    normalized = normalize_note_type(note_type)
    now = datetime.now().isoformat()
    author_list = list(authors or [])

    if normalized == "source":
        if source_id:
            sid = normalize_source_id(source_id)
            ck = sid.lstrip("@")
        else:
            ck = _resolve_citekey(citekey, author_list, year, title)
            sid = f"@{ck}"
        rel_path = source_note_filename(ck, title)
        path = cfg.vault_path / "10_Sources" / rel_path
        biblio_fields = _collect_biblio_fields(
            place=place,
            publisher=publisher,
            doi=doi,
            url=url,
            journal=journal,
            edition=edition,
            institution=institution,
            pages=pages,
        )
        meta, body = build_source_note(
            source_id=sid,
            citekey=ck,
            title=title,
            authors=author_list,
            year=year,
            origin_path=str(path),
            origin_type="md",
            checksum="",
            origin="manual",
            document_type=document_type,
            biblio_fields=biblio_fields or None,
            abnt_reference=abnt_reference,
        )
        meta["citekey"] = ck
        body = _append_src_ztl_hints(
            body, source_id=sid, citekey=ck, title=title, path=path,
        )
        _write_scaffold(path, meta, body, force=force)
        # Never overwrite an index that already carries the auto-lit-index block
        # maintained by review/sync, even when the SRC scaffold itself is forced.
        _write_literature_index(cfg, sid, ck, title, force=False)
        return NewNoteResult(path=path, note_type=normalized, meta=meta)

    if normalized == "literature":
        if source_id or citekey:
            sid = normalize_source_id(source_id or citekey or "")
            ck = sid.lstrip("@")
        else:
            ck = _resolve_citekey(None, author_list, year, title)
            sid = f"@{ck}"
        source_id = sid
        if granular:
            lit_id = f"{source_id}::manual-{int(chunk_index):04d}"
            chunk_id = f"{source_id}::manual::{int(chunk_index):04d}"
            # `title` here is the topic of this note; the source's own title drives
            # the backlink to the literature index, so recover it from the SRC note.
            src_path, src_meta = resolve_src_in_vault(cfg, source_id)
            source_title = str((src_meta or {}).get("title") or "") or title
            filename = literature_chunk_filename(
                ck,
                chunk_index=chunk_index,
                page_in_book=page,
                section_path=title,
            )
            path = (
                cfg.vault_path
                / "20_Literature"
                / literature_source_dirname(ck)
                / filename
            )
            meta, body = build_literature_chunk_note(
                source_id=source_id,
                citekey=ck,
                title=source_title,
                chunk_id=chunk_id,
                chunk_index=chunk_index,
                literature_id=lit_id,
                summary="_Preencha o resumo._",
                key_concepts=[],
                candidates=[],
                section_path=title,
                source_text="_Cole o trecho da fonte aqui._",
                page_in_book=page,
                status="approved",
                origin="manual",
            )
        else:
            rel_path = literature_index_filename(ck, title)
            path = cfg.vault_path / "20_Literature" / rel_path
            meta, body = build_literature_index_note(
                source_id=source_id,
                citekey=ck,
                title=title,
                origin="manual",
            )
        _write_scaffold(path, meta, body, force=force)
        return NewNoteResult(path=path, note_type=normalized, meta=meta)

    if normalized == "permanent":
        note_id = str(ULID())
        path = cfg.vault_path / "30_Permanent" / note_filename("ZTL", note_id, title)
        meta: dict[str, Any] = {
            "type": "permanent",
            "note_id": note_id,
            "title": title,
            "tags": [],
            "origin": "manual",
            "created_at": now,
            "updated_at": now,
        }
        warnings: list[str] = []
        source_ref = ""
        raw_source = source_id or citekey
        if raw_source:
            sid = normalize_source_id(raw_source)
            meta["source_id"] = sid
            src_path, _src_meta = resolve_src_in_vault(cfg, sid)
            ck = sid.lstrip("@")
            if src_path is not None:
                source_ref = source_wikilink(ck, path=src_path)
            else:
                source_ref = source_wikilink(ck)
                warnings.append(
                    f"SRC nao encontrada em 10_Sources/ para {sid}; "
                    "wikilink provisorio usado (crie a fonte com new-note src ou harvest)."
                )
        body = build_permanent_note_body(
            thesis=thesis or "_Preencha a tese._",
            definition="_Preencha a definicao._",
            intuition="",
            example="",
            limits="",
            connections=[],
            literature_ref="",
            source_ref=source_ref,
            source_locator="",
        )
        body += (
            "\n## Sugestoes de conexao\n\n"
            "<!-- zettel:auto-connections:start -->\n"
            "_Sincronize com sync-manual para sugestoes automaticas._\n"
            "<!-- zettel:auto-connections:end -->\n"
        )
        _write_scaffold(path, meta, body, force=force)
        return NewNoteResult(
            path=path, note_type=normalized, meta=meta, warnings=warnings or None,
        )

    if normalized == "moc":
        moc_id = str(ULID())
        path = cfg.vault_path / "40_MOCs" / note_filename("MOC", moc_id, title)
        meta = {
            "type": "moc",
            "moc_id": moc_id,
            "topic": title,
            "origin": "manual",
            "created_at": now,
            "updated_at": now,
        }
        body = (
            f"# {title}\n\n"
            "_Resumo do mapa de conteudo._\n\n"
            "## Secao\n\n"
            "_Adicione links para notas permanentes aqui._\n"
        )
        _write_scaffold(path, meta, body, force=force)
        return NewNoteResult(path=path, note_type=normalized, meta=meta)

    raise ValueError(f"Tipo nao suportado: {normalized}")
