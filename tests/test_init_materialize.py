"""``zettel init`` recreates a missing note from SQLite (#414).

An existing file, including a manual one, stays byte for byte. ``--reset`` still
deletes only what it deleted before; the message says the manual rows leave
with the database and that ``zettel backup`` is how they come back.
"""

from __future__ import annotations

import json

_GEMINI_KEYS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")
_THESIS = "tese que so estava no banco"


def _upsert_note(db, path, note_id: str, body: str) -> None:
    db.upsert_note(
        note_id,
        None,
        str(path),
        "Nota manual",
        body=body,
        frontmatter_json=json.dumps(
            {
                "type": "permanent",
                "note_id": note_id,
                "origin": "manual",
                "title": "Nota manual",
            }
        ),
        origin="manual",
    )


def test_init_materializes_a_missing_note(cli_instance, monkeypatch):
    """A row whose file is gone is written by init, with no embedding key."""
    for name in _GEMINI_KEYS:
        monkeypatch.delenv(name, raising=False)
    path = (
        cli_instance.vault / "30_Permanent" / "ZTL - 01J0000000000000000000000A - ausente.md"
    ).resolve()
    db = cli_instance.db()
    try:
        _upsert_note(db, path, "01J0000000000000000000000A", f"> **Tese**: {_THESIS}\n")
    finally:
        db.close()

    output = cli_instance.run("init")

    assert path.is_file()
    assert _THESIS in path.read_text(encoding="utf-8")
    assert "1 arquivo(s) recriado(s) a partir do banco." in output


def test_init_leaves_an_existing_manual_file_unchanged(cli_instance):
    path = (
        cli_instance.vault / "30_Permanent" / "ZTL - 01J0000000000000000000000B - minha.md"
    ).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b"---\r\norigin: manual\r\n---\r\n\r\nlinha do autor\r\n"
    path.write_bytes(original)
    db = cli_instance.db()
    try:
        _upsert_note(
            db,
            path,
            "01J0000000000000000000000B",
            "corpo diferente que nao pode ir para o disco\n",
        )
    finally:
        db.close()

    output = cli_instance.run("init")

    assert path.read_bytes() == original
    assert "recriado" not in output


def test_init_reset_says_backup_brings_manual_rows_back(cli_instance):
    path = cli_instance.vault / "30_Permanent" / "ZTL - 01J0000000000000000000000A - minha.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b"---\norigin: manual\n---\n\nminha\n"
    path.write_bytes(original)

    output = cli_instance.run("init", "--reset", input="y\n")

    assert "zettel backup" in output
    assert "As linhas das notas manuais somem com o banco." in output
    assert "sync-manual" in output
    assert path.read_bytes() == original
