"""Tests for PDF extraction (ADR-012: Docling is mandatory, no PyMuPDF fallback)."""

from __future__ import annotations

import sys

import pytest

from zettel.config import AppConfig
from zettel.harvester.extract import (
    PdfExtractionError,
    extract_pdf,
    extract_pdf_docling,
    page_map_for_source,
)
from zettel.paging import PAGE_BREAK_MARKER


def _cfg(**kwargs) -> AppConfig:
    return AppConfig(device="cpu", **kwargs)


def test_extract_pdf_docling_missing_raises_pdf_extraction_error(monkeypatch, tmp_path):
    """A missing Docling install fails harvest explicitly (no PyMuPDF fallback)."""
    monkeypatch.setitem(sys.modules, "docling", None)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    with pytest.raises(PdfExtractionError, match="nao esta instalado"):
        extract_pdf_docling(_cfg(), pdf)


def test_extract_pdf_docling_conversion_failure_raises_pdf_extraction_error(
    monkeypatch, tmp_path
):
    """A Docling conversion error is fatal, with a message distinct from 'not installed'."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    class _BoomConverter:
        def __init__(self, *args, **kwargs):
            pass

        def convert(self, path):
            raise RuntimeError("modelo indisponivel")

    monkeypatch.setattr(
        "docling.document_converter.DocumentConverter", _BoomConverter
    )

    with pytest.raises(PdfExtractionError, match="conversao Docling lancou um erro"):
        extract_pdf_docling(_cfg(), pdf)


class _FakeOrigin:
    title = "Titulo Extraido"
    author = "Fulano, Beltrano"
    date = "2023-05-01"


class _FakeDocument:
    def __init__(self, markdown: str):
        self._markdown = markdown
        self.origin = _FakeOrigin()
        self.num_pages = 2

    def export_to_markdown(self, page_break_placeholder: str | None = None, page_no=None):
        if page_break_placeholder is not None:
            return self._markdown
        return self._markdown


class _FakeResult:
    def __init__(self, markdown: str):
        self.document = _FakeDocument(markdown)


class _FakeConverter:
    def __init__(self, *args, **kwargs):
        pass

    def convert(self, path):
        marked = (
            f"# Capa\n\n{PAGE_BREAK_MARKER}\n\n"
            f"# Capitulo 1\n\nConteudo unico sobre grafos de conhecimento."
        )
        return _FakeResult(marked)


def test_extract_pdf_docling_success_builds_text_and_page_map(monkeypatch, tmp_path):
    """A successful Docling conversion returns marked text, metadata, and a page map."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr(
        "docling.document_converter.DocumentConverter", _FakeConverter
    )

    text, metadata = extract_pdf_docling(_cfg(), pdf)

    assert PAGE_BREAK_MARKER in text
    assert metadata["title"] == "Titulo Extraido"
    assert metadata["authors"] == ["Fulano", "Beltrano"]
    assert metadata["year"] == 2023
    assert metadata["_page_map"] == [
        (1, "# Capa"),
        (2, "# Capitulo 1\n\nConteudo unico sobre grafos de conhecimento."),
    ]


class _HyphenatedConverter:
    def __init__(self, *args, **kwargs):
        pass

    def convert(self, path):
        marked = (
            "# Capitulo 1\n\n"
            "Uma pala-\nvra quebrada pelo layout do PDF, e outra bem-\nVinda mantida."
        )
        return _FakeResult(marked)


def test_extract_pdf_docling_merges_lowercase_continuation_hyphenation(monkeypatch, tmp_path):
    """A word split across a PDF line break ("pala-\\nvra") is merged before persistence."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(
        "docling.document_converter.DocumentConverter", _HyphenatedConverter
    )
    text, _metadata = extract_pdf_docling(_cfg(), pdf)
    assert "palavra quebrada" in text
    assert "pala-\nvra" not in text


def test_extract_pdf_docling_preserves_uppercase_continuation_hyphenation(monkeypatch, tmp_path):
    """An uppercase continuation is treated as a likely genuine compound and left alone."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(
        "docling.document_converter.DocumentConverter", _HyphenatedConverter
    )
    text, _metadata = extract_pdf_docling(_cfg(), pdf)
    assert "bem-\nVinda" in text


def test_extract_pdf_dispatches_only_to_docling(monkeypatch, tmp_path):
    """extract_pdf always uses Docling — there is no pdf_extractor config to select PyMuPDF."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(
        "docling.document_converter.DocumentConverter", _FakeConverter
    )
    text, metadata = extract_pdf(_cfg(), pdf)
    assert PAGE_BREAK_MARKER in text
    assert not hasattr(AppConfig(), "pdf_extractor")


def test_page_map_for_source_has_no_pymupdf_fallback(tmp_path):
    """Rechunk/resume only reads Docling markers from persisted text — no PDF reopen."""
    src_with_markers = {
        "extracted_text": f"# A\n\n{PAGE_BREAK_MARKER}\n\n# B",
        "origin_path": str(tmp_path / "missing.pdf"),
    }
    assert page_map_for_source(src_with_markers) == [(1, "# A"), (2, "# B")]

    src_without_markers = {
        "extracted_text": "# Just one page, no markers",
        "origin_path": str(tmp_path / "missing.pdf"),
    }
    assert page_map_for_source(src_without_markers) == []
