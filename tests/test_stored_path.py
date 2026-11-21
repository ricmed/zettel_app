"""Canonical stored paths: relative to the vault or the inbox."""

from __future__ import annotations

from pathlib import Path

import pytest
from zettel.stored_path import (
    StoredPathError,
    relativize,
    resolve_origin,
    resolve_stored,
    same_stored,
    store_inbox,
    store_origin,
    store_vault,
)


def test_vault_file_is_relative(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    note = vault / "30_Permanent" / "ZTL - abc - tese.md"
    note.parent.mkdir(parents=True)
    note.write_text("x", encoding="utf-8")
    assert store_vault(note, vault) == "30_Permanent/ZTL - abc - tese.md"


def test_inbox_file_is_relative_to_the_inbox(tmp_path: Path) -> None:
    inbox = tmp_path / "data" / "inbox"
    pdf = inbox / "livro.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF")
    assert store_inbox(pdf, inbox) == "livro.pdf"


def test_origin_prefers_the_vault_then_the_inbox(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    inbox = tmp_path / "inbox"
    src = vault / "10_Sources" / "SRC - A - titulo.md"
    pdf = inbox / "livro.pdf"
    src.parent.mkdir(parents=True)
    inbox.mkdir()
    src.write_text("x", encoding="utf-8")
    pdf.write_bytes(b"%PDF")
    outside = tmp_path / "downloads" / "solto.pdf"
    outside.parent.mkdir()
    outside.write_bytes(b"%PDF")

    assert store_origin(src, vault, inbox) == "10_Sources/SRC - A - titulo.md"
    assert store_origin(pdf, vault, inbox) == "livro.pdf"
    assert store_origin(outside, vault, inbox) == outside.resolve().as_posix()


def test_resolve_joins_a_relative_path_and_keeps_an_absolute_one(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    note = vault / "30_Permanent" / "n.md"
    note.parent.mkdir(parents=True)
    note.write_text("x", encoding="utf-8")
    assert resolve_stored("30_Permanent/n.md", vault) == note.resolve()
    assert resolve_stored(str(note.resolve()), vault) == note.resolve()


def test_resolve_refuses_a_path_that_escapes_the_base(tmp_path: Path) -> None:
    with pytest.raises(StoredPathError):
        resolve_stored("../secret.md", tmp_path)


def test_origin_of_a_pdf_resolves_in_the_inbox(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    inbox = tmp_path / "inbox"
    vault.mkdir()
    pdf = inbox / "livro.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF")
    assert resolve_origin("livro.pdf", vault, inbox) == pdf.resolve()
    src = vault / "10_Sources" / "SRC.md"
    src.parent.mkdir()
    src.write_text("x", encoding="utf-8")
    assert resolve_origin("10_Sources/SRC.md", vault, inbox) == src.resolve()


def test_same_stored_accepts_the_absolute_and_the_relative_form(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    note = vault / "40_MOCs" / "MOC - id - tema.md"
    note.parent.mkdir(parents=True)
    note.write_text("x", encoding="utf-8")
    relative = store_vault(note, vault)
    assert same_stored(relative, note, vault)
    assert same_stored(str(note.resolve()), note, vault)
    assert not same_stored("40_MOCs/outra.md", note, vault)


def test_relativize_converts_an_absolute_inside_and_keeps_one_outside(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    inbox = tmp_path / "inbox"
    note = vault / "30_Permanent" / "n.md"
    note.parent.mkdir(parents=True)
    note.write_text("x", encoding="utf-8")
    outside = tmp_path / "downloads" / "solto.pdf"
    outside.parent.mkdir()
    outside.write_bytes(b"x")

    converted, status = relativize(str(note.resolve()), kind="vault", vault=vault, inbox=inbox)
    assert status == "converted"
    assert converted == "30_Permanent/n.md"

    again, status = relativize(converted, kind="vault", vault=vault, inbox=inbox)
    assert status == "already"
    assert again == converted

    kept, status = relativize(str(outside.resolve()), kind="vault", vault=vault, inbox=inbox)
    assert status == "outside"
    assert kept == str(outside.resolve())

    bad, status = relativize("../secret.md", kind="vault", vault=vault, inbox=inbox)
    assert status == "invalid"
    assert bad == "../secret.md"
