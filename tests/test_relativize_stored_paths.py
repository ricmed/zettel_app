"""The one-shot path rewrite runs as a script, not as a package module."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from zettel.config import load_config
from zettel.state import StateDB

from tests.cli_support import REPO, write_operational_config

_SCRIPT = REPO / "scripts" / "relativize_stored_paths.py"


def _run(config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--config", str(config), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_script_dry_run_leaves_files_and_apply_rewrites_them(tmp_path: Path):
    config = write_operational_config(tmp_path)
    cfg = load_config(config)
    src = cfg.vault_path / "10_Sources" / "SRC - Autor2020 - livro.md"
    src.parent.mkdir(parents=True)
    absolute = str(src.resolve())
    text = f"---\ntype: source\nsource_id: SRC1\norigin_path: {absolute}\n---\n\n# Livro\n"
    src.write_text(text, encoding="utf-8", newline="\n")

    inbox_pdf = cfg.inbox_path / "outro.pdf"
    inbox_pdf.write_bytes(b"%PDF-1.4")
    collided = cfg.inbox_path / "livro.pdf"
    collided.write_bytes(b"%PDF-1.4")
    via_parent = collided.parent / "extra" / ".." / collided.name
    outside = tmp_path / "Downloads" / "solto.pdf"
    outside.parent.mkdir()
    outside.write_bytes(b"%PDF-1.4")
    note = cfg.vault_path / "30_Permanent" / "ZTL - n.md"

    db = StateDB(cfg.state_db_path)
    db.upsert_source(
        "SRC1",
        "Autor2020",
        "Livro",
        ["Autor"],
        2020,
        "checksum",
        absolute,
        "md",
    )
    db.conn.execute("UPDATE sources SET card_body=? WHERE source_id=?", (text, "SRC1"))
    db.conn.commit()
    db.upsert_file(str(inbox_pdf.resolve()), "sum-outro", "pdf")
    db.upsert_file(str(collided.resolve()), "sum-a", "pdf")
    db.upsert_file(str(via_parent), "sum-b", "pdf")
    db.upsert_note("N1", None, str(note.resolve()), "Nota", origin="manual")
    db.upsert_note("N-OUT", None, str(outside.resolve()), "Fora", origin="manual")
    db.close()

    dry = _run(config)
    assert dry.returncode == 0, dry.stderr
    assert "dry-run" in dry.stdout
    assert "Nada foi gravado" in dry.stdout
    assert absolute in src.read_text(encoding="utf-8")
    peeked = StateDB(cfg.state_db_path)
    assert (
        peeked.conn.execute("SELECT origin_path FROM sources WHERE source_id='SRC1'").fetchone()[0]
        == absolute
    )
    peeked.close()

    applied = _run(config, "--apply")
    assert applied.returncode == 0, applied.stderr
    assert "Gravado." in applied.stdout
    rewritten = src.read_text(encoding="utf-8")
    assert absolute not in rewritten
    assert "origin_path: 10_Sources/SRC - Autor2020 - livro.md" in rewritten

    done = StateDB(cfg.state_db_path)
    origin = done.conn.execute("SELECT origin_path FROM sources WHERE source_id='SRC1'").fetchone()[
        0
    ]
    card = done.conn.execute("SELECT card_body FROM sources WHERE source_id='SRC1'").fetchone()[0]
    note_path = done.conn.execute("SELECT path FROM notes WHERE note_id='N1'").fetchone()[0]
    outside_path = done.conn.execute("SELECT path FROM notes WHERE note_id='N-OUT'").fetchone()[0]
    inbox_row = done.get_file("outro.pdf")
    done.close()

    assert origin == "10_Sources/SRC - Autor2020 - livro.md"
    assert "origin_path: 10_Sources/SRC - Autor2020 - livro.md" in card
    assert note_path == "30_Permanent/ZTL - n.md"
    assert outside_path == str(outside.resolve())
    assert inbox_row is not None
    assert "colisao files.path" in applied.stdout
