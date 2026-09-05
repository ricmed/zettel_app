"""Invisible-Unicode hygiene and the scanned-PDF early abort (ADR-033)."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from zettel.harvester import extract
from zettel.hashing import normalize_text_for_hash, sha256_hex
from zettel.text_sanitize import (
    sanitize_extracted_text,
    strip_invisible_unicode,
    visible_char_count,
)

# Built from codepoints: writing these literally would make the test file itself
# carry the payload it is about (and trip the linter's control-character rule).
ZWSP = chr(0x200B)
BOM = chr(0xFEFF)
TAG_A = chr(0xE0041)
WORD_JOINER = chr(0x2060)
RLO = chr(0x202E)


# ── strip_invisible_unicode ────────────────────────────────────────────


def test_strips_zero_width_bom_and_tag_block():
    dirty = f"a{ZWSP}b{BOM}c{TAG_A}d{WORD_JOINER}e{RLO}f"
    clean, removed = strip_invisible_unicode(dirty)
    assert clean == "abcdef"
    assert removed == 5


def test_is_idempotent():
    once, first = strip_invisible_unicode(f"x{ZWSP}y")
    twice, second = strip_invisible_unicode(once)
    assert once == twice == "xy"
    assert (first, second) == (1, 0)


def test_leaves_visible_punctuation_and_nbsp_untouched():
    # normalize_text_for_hash owns visible-text rewriting; the sanitizer must not
    # compete with it (NBSP, hyphens and accents survive here).
    text = "café \u00a0 bem-vindo — fim"
    clean, removed = strip_invisible_unicode(text)
    assert clean == text
    assert removed == 0


def test_empty_text_short_circuits():
    assert strip_invisible_unicode("") == ("", 0)


def test_sanitize_logs_removal_count(caplog):
    with caplog.at_level("INFO"):
        assert sanitize_extracted_text(f"a{ZWSP}b", "doc.pdf") == "ab"
    assert "removed 1 invisible chars from doc.pdf" in caplog.text


def test_sanitize_stays_quiet_when_nothing_removed(caplog):
    with caplog.at_level("INFO"):
        assert sanitize_extracted_text("limpo", "doc.pdf") == "limpo"
    assert "sanitize:" not in caplog.text


def test_visible_char_count_ignores_invisibles_and_whitespace():
    assert visible_char_count(f" {ZWSP}\n {BOM}\t") == 0
    assert visible_char_count(f"a{ZWSP} b") == 2


# ── extraction checksum ────────────────────────────────────────────────


def test_sanitized_variants_collide_on_the_extraction_checksum():
    """Layer 2 (extraction hash) still catches the same content in two files."""
    plain = "Texto identico para as duas copias."
    smuggled = f"Texto{ZWSP} identico para as duas{TAG_A} copias."
    checksums = {
        sha256_hex(normalize_text_for_hash(sanitize_extracted_text(t, "f")))
        for t in (plain, smuggled)
    }
    assert len(checksums) == 1


# ── extract_text integration ───────────────────────────────────────────


def _cfg():
    from zettel.config import AppConfig

    return AppConfig()


def test_markdown_extraction_is_sanitized(tmp_path: Path):
    md = tmp_path / "nota.md"
    md.write_text(f"# T{ZWSP}itulo\n\nCorpo{TAG_A} da nota.\n", encoding="utf-8")
    text, _ = extract.extract_text(_cfg(), md, "md")
    assert ZWSP not in text and TAG_A not in text
    assert "Corpo da nota." in text


def test_markdown_without_visible_text_aborts(tmp_path: Path):
    md = tmp_path / "vazio.md"
    md.write_text(f"{ZWSP}{BOM}   \n", encoding="utf-8")
    with pytest.raises(extract.EmptyTextLayerError) as exc:
        extract.extract_text(_cfg(), md, "md")
    assert "ocrmypdf" in str(exc.value)


def test_pdf_extraction_is_sanitized(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "artigo.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(extract, "assert_pdf_has_text_layer", lambda path: None)
    monkeypatch.setattr(
        extract,
        "extract_pdf_docling",
        lambda cfg, path: (f"Texto{ZWSP} do PDF{TAG_A}.", {"title": "t"}),
    )
    text, _ = extract.extract_text(_cfg(), pdf, "pdf")
    assert text == "Texto do PDF."


# ── text-layer probe ───────────────────────────────────────────────────


def _fake_pdfium(monkeypatch, page_texts: list[str]):
    """Install a pypdfium2 stub whose pages return ``page_texts``."""

    class FakeTextPage:
        def __init__(self, text: str):
            self._text = text

        def get_text_bounded(self) -> str:
            return self._text

        def close(self) -> None:
            pass

    class FakePage:
        def __init__(self, text: str):
            self._text = text

        def get_textpage(self) -> FakeTextPage:
            return FakeTextPage(self._text)

    class FakeDocument:
        def __init__(self, path: str):
            self.closed = False

        def __len__(self) -> int:
            return len(page_texts)

        def __getitem__(self, index: int) -> FakePage:
            return FakePage(page_texts[index])

        def close(self) -> None:
            self.closed = True

    module = types.ModuleType("pypdfium2")
    module.PdfDocument = FakeDocument
    monkeypatch.setitem(sys.modules, "pypdfium2", module)


def test_probe_accepts_pdf_with_a_text_layer(tmp_path: Path, monkeypatch):
    _fake_pdfium(monkeypatch, ["Uma pagina com bastante texto legivel para o probe."])
    extract.assert_pdf_has_text_layer(tmp_path / "ok.pdf")


def test_probe_rejects_scanned_pdf(tmp_path: Path, monkeypatch):
    _fake_pdfium(monkeypatch, ["", " ", "12"])
    with pytest.raises(extract.EmptyTextLayerError) as exc:
        extract.assert_pdf_has_text_layer(tmp_path / "scan.pdf")
    assert "ocrmypdf" in str(exc.value)
    assert isinstance(exc.value, extract.PdfExtractionError)


def test_probe_ignores_invisible_only_text_layer(tmp_path: Path, monkeypatch):
    _fake_pdfium(monkeypatch, [ZWSP * 200])
    with pytest.raises(extract.EmptyTextLayerError):
        extract.assert_pdf_has_text_layer(tmp_path / "invisivel.pdf")


def test_probe_reads_at_most_three_pages(tmp_path: Path, monkeypatch):
    # Text only from page 4 onward: the probe must not rescue the file by
    # scanning the whole document (that is the cost it exists to avoid).
    _fake_pdfium(monkeypatch, ["", "", "", "Texto abundante somente na quarta pagina."])
    with pytest.raises(extract.EmptyTextLayerError):
        extract.assert_pdf_has_text_layer(tmp_path / "tardio.pdf")


def test_probe_is_skipped_without_pdfium(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    extract.assert_pdf_has_text_layer(tmp_path / "sem-pdfium.pdf")


# ── batch safety ───────────────────────────────────────────────────────


def test_one_unusable_file_does_not_stop_the_batch(tmp_path: Path):
    """A bad file is reported; the good ones in the same inbox still harvest."""
    from zettel.config import AppConfig, HarvestConfig
    from zettel.harvester import run_harvest
    from zettel.state import StateDB

    from tests.test_harvester_dedup import FakeVectorIndex

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "bom.md").write_text(
        "---\ntitle: Bom\nauthors: [Autor]\nyear: 2020\n---\n\n"
        "# Bom\n\nUm paragrafo com conteudo suficiente para virar chunk.\n",
        encoding="utf-8",
    )
    (inbox / "vazio.md").write_text(f"{ZWSP}{BOM}\n", encoding="utf-8")

    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        inbox_path=inbox,
        harvest=HarvestConfig(biblio_llm_enabled=False),
    )
    db = StateDB(tmp_path / "state.db")
    try:
        outcome = run_harvest(
            cfg,
            db,
            FakeVectorIndex(),
            interactive=False,
            duplicate_action="skip",
            skip_biblio=True,
            skip_paging=True,
        )
    finally:
        db.close()

    assert len(outcome.source_ids) == 1
    assert [(s.path.name, s.reason) for s in outcome.skipped] == [("vazio.md", "empty_text_layer")]
