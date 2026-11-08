"""Writing projects (Mesa, ADR-067): vault notes under ``50_Projetos``.

The file is the project's only record: no SQLite row, no index. It is listed
by scanning its folder, which holds a handful of files. A save is refused when
the file changed since it was read (an edit in Obsidian, another tab), so the
web never overwrites what it did not show.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zettel.project_file import ProjectBody, parse_body, render_body
from zettel.vault import (
    PROJECTS_DIR,
    _slug,
    compose_note,
    parse_frontmatter,
    permanent_wikilink,
)

PROJECT_TYPE = "project"
PROJECT_PREFIX = "PRJ"
STYLES = ("academic", "blog")
_PROJECT_ID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
FIRST_SECTION = "Introdução"


class ProjectNotFound(LookupError):
    pass


class ProjectConflict(RuntimeError):
    """The file changed on disk after the page that edits it was rendered."""


@dataclass
class Project:
    project_id: str
    path: Path
    meta: dict[str, Any]
    body: ProjectBody
    version: str

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or "Sem título")

    @property
    def style(self) -> str:
        style = str(self.meta.get("style") or "")
        return style if style in STYLES else "academic"


def projects_dir(cfg: Any) -> Path:
    return Path(cfg.vault_path) / PROJECTS_DIR


def version_of(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def read_project(path: Path) -> Project | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    meta, body = parse_frontmatter(raw.decode("utf-8", errors="replace"))
    project_id = str(meta.get("project_id") or "") if isinstance(meta, dict) else ""
    if meta.get("type") != PROJECT_TYPE or not _PROJECT_ID.fullmatch(project_id):
        return None
    return Project(project_id, path, meta, parse_body(body), version_of(raw))


def list_projects(cfg: Any) -> list[Project]:
    """Every project in the folder, most recently updated first."""
    folder = projects_dir(cfg)
    if not folder.is_dir():
        return []
    found = [p for p in (read_project(path) for path in sorted(folder.glob("*.md"))) if p]
    return sorted(found, key=lambda p: str(p.meta.get("updated_at") or ""), reverse=True)


def load_project(cfg: Any, project_id: str) -> Project:
    if not _PROJECT_ID.fullmatch(project_id or ""):
        raise ProjectNotFound(project_id)
    folder = projects_dir(cfg)
    for path in sorted(folder.glob(f"{PROJECT_PREFIX} - {project_id}*.md")):
        project = read_project(path)
        if project and project.project_id == project_id:
            return project
    # A file renamed in Obsidian keeps its id in the frontmatter.
    for project in list_projects(cfg):
        if project.project_id == project_id:
            return project
    raise ProjectNotFound(project_id)


def _write(path: Path, content: str) -> str:
    data = content.encode("utf-8")
    partial = path.with_name(f".{path.name}.part")
    partial.write_bytes(data)
    os.replace(partial, path)
    return version_of(data)


def create_project(cfg: Any, title: str, style: str = "academic") -> Project:
    from ulid import ULID

    from zettel.time import now_vault_iso

    clean = " ".join(title.split())[:200] or "Nova mesa"
    project_id = str(ULID())
    now = now_vault_iso(cfg.vault_timezone)
    meta = {
        "type": PROJECT_TYPE,
        "project_id": project_id,
        "title": clean,
        "style": style if style in STYLES else "academic",
        "origin": "manual",
        "created_at": now,
        "updated_at": now,
    }
    folder = projects_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    slug = _slug(clean, max_len=60) or "mesa"
    path = folder / f"{PROJECT_PREFIX} - {project_id} - {slug}.md"
    body = parse_body(f"## {FIRST_SECTION}\n")
    version = _write(path, compose_note(meta, render_body(body)))
    return Project(project_id, path, meta, body, version)


def save_project(cfg: Any, project: Project, *, expected_version: str | None) -> Project:
    """Write the edited project; refuse when the file is no longer the one edited."""
    from zettel.time import now_vault_iso

    try:
        current = version_of(project.path.read_bytes())
    except OSError as exc:
        raise ProjectNotFound(project.project_id) from exc
    if expected_version is not None and current != expected_version:
        raise ProjectConflict(project.project_id)
    project.meta["updated_at"] = now_vault_iso(cfg.vault_timezone)
    project.version = _write(project.path, compose_note(project.meta, render_body(project.body)))
    return project


def note_links(db: Any, note_ids: list[str]) -> list[tuple[str, str]]:
    """``(note_id, wikilink)`` for the ids that are notes, in the given order."""
    rows = db.get_notes_by_ids(note_ids)
    links = []
    for note_id in dict.fromkeys(note_ids):
        row = rows.get(note_id)
        if row:
            links.append(
                (note_id, permanent_wikilink(note_id, row.get("title") or "", path=row.get("path")))
            )
    return links


@dataclass
class CardView:
    note_id: str
    exists: bool
    title: str = ""
    thesis: str = ""
    citation: str = ""
    source_id: str | None = None
    source_title: str = ""
    figure_url: str | None = None


def card_views(cfg: Any, db: Any, note_ids: list[str]) -> dict[str, CardView]:
    """What a card shows, read in one note query and one source query per source."""
    from zettel.assets import embedded_asset_paths
    from zettel.citation import load_provenance
    from zettel.markdown import asset_url
    from zettel.note_content import note_sections

    rows = db.get_notes_by_ids(note_ids)
    sources: dict[str, dict | None] = {}
    views: dict[str, CardView] = {}
    for note_id in note_ids:
        row = rows.get(note_id)
        if not row:
            views[note_id] = CardView(note_id=note_id, exists=False)
            continue
        source_id = row.get("source_id")
        if source_id and source_id not in sources:
            sources[source_id] = db.get_source(source_id)
        figures = embedded_asset_paths(Path(cfg.vault_path), row.get("body") or "")
        views[note_id] = CardView(
            note_id=note_id,
            exists=True,
            title=row.get("title") or note_id,
            thesis=note_sections(row.get("title") or "", row.get("body") or "")["thesis"],
            citation=str(load_provenance(row).get("citation") or ""),
            source_id=source_id,
            source_title=((sources.get(source_id) or {}).get("title") or "") if source_id else "",
            figure_url=asset_url(figures[0]) if figures else None,
        )
    return views
