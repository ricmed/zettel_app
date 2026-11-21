"""Rewrite stored paths so they are relative to the vault or the inbox.

The rule lives in ``zettel.stored_path``. This script only walks what is
already stored and calls that rule. Dry-run is the default.

    .venv/Scripts/python.exe scripts/relativize_stored_paths.py
    .venv/Scripts/python.exe scripts/relativize_stored_paths.py --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zettel.config import load_config
from zettel.oplock import LockBusy, OperationLock, lock_path
from zettel.state import StateDB
from zettel.stored_path import relativize

_COLUMNS = (
    ("sources", "source_id", "origin_path", "origin"),
    ("chunks", "chunk_id", "literature_note_path", "vault"),
    ("notes", "note_id", "path", "vault"),
    ("mocs", "moc_id", "path", "vault"),
    ("projects", "project_id", "path", "vault"),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Caminhos do acervo relativos ao vault ou à inbox."
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--apply", action="store_true", help="Grava. Sem isto, só mostra.")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    db = StateDB(cfg.state_db_path)
    try:
        plan = _plan(db, cfg.vault_path, cfg.inbox_path)
        _print_plan(plan, apply=args.apply)
        if not args.apply:
            print("Nada foi gravado. Use --apply para gravar.")
            return 0
        lock = OperationLock(lock_path(cfg))
        try:
            lock.acquire("relativize-paths", "cli")
        except LockBusy as busy:
            print(busy)
            return 1
        try:
            _apply(db, plan)
        finally:
            lock.release()
    finally:
        db.close()
    print("Gravado.")
    return 1 if plan.file_errors else 0


class _Plan:
    def __init__(self) -> None:
        self.converted = 0
        self.already = 0
        self.outside: list[str] = []
        self.invalid: list[str] = []
        self.collisions: list[tuple[str, list[str]]] = []
        self.files: list[tuple[str, str]] = []
        self.columns: list[tuple[str, str, str, str]] = []
        self.cards: list[tuple[str, str]] = []
        self.vault_files: list[tuple[Path, str]] = []
        self.file_errors: list[str] = []


def _plan(db: StateDB, vault: Path, inbox: Path) -> _Plan:
    plan = _Plan()
    _plan_files(db, plan, vault, inbox)
    for table, id_col, path_col, kind in _COLUMNS:
        _plan_column(db, plan, table, id_col, path_col, kind, vault, inbox)
    _plan_cards(db, plan, vault, inbox)
    _plan_sources_on_disk(plan, vault, inbox)
    return plan


def _consider(plan: _Plan, stored: str, *, kind: str, vault: Path, inbox: Path) -> tuple[str, str]:
    canonical, status = relativize(stored, kind=kind, vault=vault, inbox=inbox)
    if status == "converted":
        plan.converted += 1
    elif status == "already":
        plan.already += 1
    elif status == "outside":
        plan.outside.append(stored)
    else:
        plan.invalid.append(stored)
    return canonical, status


def _plan_files(db: StateDB, plan: _Plan, vault: Path, inbox: Path) -> None:
    rows = db.conn.execute("SELECT path FROM files").fetchall()
    groups: dict[str, list[tuple[str, str]]] = {}
    for (old,) in rows:
        if not old:
            continue
        canonical, status = _consider(plan, old, kind="inbox", vault=vault, inbox=inbox)
        groups.setdefault(canonical, []).append((old, status))
    for canonical, group in groups.items():
        originals = [old for old, _status in group]
        if len(set(originals)) > 1 and any(status == "converted" for _old, status in group):
            plan.collisions.append((canonical, originals))
            plan.converted -= sum(status == "converted" for _old, status in group)
            continue
        for old, status in group:
            if status == "converted":
                plan.files.append((old, canonical))


def _plan_column(
    db: StateDB,
    plan: _Plan,
    table: str,
    id_col: str,
    path_col: str,
    kind: str,
    vault: Path,
    inbox: Path,
) -> None:
    rows = db.conn.execute(f"SELECT {id_col}, {path_col} FROM {table}").fetchall()
    for row_id, old in rows:
        if not old:
            continue
        canonical, status = _consider(plan, old, kind=kind, vault=vault, inbox=inbox)
        if status == "converted":
            plan.columns.append((table, row_id, path_col, canonical))


def _plan_cards(db: StateDB, plan: _Plan, vault: Path, inbox: Path) -> None:
    rows = db.conn.execute(
        "SELECT source_id, card_body FROM sources WHERE card_body IS NOT NULL"
    ).fetchall()
    for source_id, body in rows:
        updated = _rewrite_origin(body, vault=vault, inbox=inbox)
        if updated is not None and updated != body:
            plan.cards.append((source_id, updated))
            plan.converted += 1


def _plan_sources_on_disk(plan: _Plan, vault: Path, inbox: Path) -> None:
    folder = vault / "10_Sources"
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("SRC - *.md")):
        text = path.read_text(encoding="utf-8")
        updated = _rewrite_origin(text, vault=vault, inbox=inbox)
        if updated is not None and updated != text:
            plan.vault_files.append((path, updated))
            plan.converted += 1


def _rewrite_origin(text: str, *, vault: Path, inbox: Path) -> str | None:
    """Replace the frontmatter ``origin_path`` line when it converts. None if absent."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    head, tail = text[:end], text[end:]
    lines = head.splitlines(keepends=True)
    rewritten: list[str] = []
    found = False
    changed = False
    for line in lines:
        bare = line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith("\n") else line
        if found or not bare.startswith("origin_path:"):
            rewritten.append(line)
            continue
        found = True
        raw = bare.split(":", 1)[1].strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
            raw = raw[1:-1]
        canonical, status = relativize(raw, kind="origin", vault=vault, inbox=inbox)
        if status != "converted":
            rewritten.append(line)
            continue
        ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        rewritten.append(f"origin_path: {_yaml_scalar(canonical)}{ending}")
        changed = True
    if not found or not changed:
        return None
    return "".join(rewritten) + tail


def _yaml_scalar(value: str) -> str:
    if value and not any(ch in value for ch in ":#{}[]&*!|>'\"%@`,"):
        return value
    return json.dumps(value, ensure_ascii=False)


def _print_plan(plan: _Plan, *, apply: bool) -> None:
    print("aplicar" if apply else "dry-run")
    for old, new in plan.files:
        print(f"files.path: {old} -> {new}")
    for table, row_id, column, new in plan.columns:
        print(f"{table}.{column} {row_id} -> {new}")
    for source_id, _body in plan.cards:
        print(f"sources.card_body {source_id}: origin_path")
    for path, _text in plan.vault_files:
        print(f"arquivo {path.name}: origin_path")
    for canonical, originals in plan.collisions:
        joined = " | ".join(originals)
        print(f"colisao files.path -> {canonical}: {joined}")
    for stored in plan.outside:
        print(f"absoluto: {stored}")
    print(f"convertidos: {plan.converted}")
    print(f"ja_relativos: {plan.already}")
    print(f"absolutos: {len(plan.outside)}")
    print(f"invalidos: {len(plan.invalid)}")
    print(f"colisoes: {len(plan.collisions)}")


def _apply(db: StateDB, plan: _Plan) -> None:
    conn = db.conn
    try:
        for old, new in plan.files:
            conn.execute("UPDATE files SET path=? WHERE path=?", (new, old))
        for table, row_id, column, new in plan.columns:
            id_col = _id_column(table)
            conn.execute(
                f"UPDATE {table} SET {column}=? WHERE {id_col}=?",
                (new, row_id),
            )
        for source_id, body in plan.cards:
            conn.execute(
                "UPDATE sources SET card_body=? WHERE source_id=?",
                (body, source_id),
            )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    for path, text in plan.vault_files:
        try:
            path.write_text(text, encoding="utf-8", newline="\n")
        except OSError as exc:
            plan.file_errors.append(f"{path}: {exc}")
            print(f"arquivo nao gravado: {path}: {exc}")


def _id_column(table: str) -> str:
    for name, id_col, _path_col, _kind in _COLUMNS:
        if name == table:
            return id_col
    raise KeyError(table)


if __name__ == "__main__":
    raise SystemExit(main())
