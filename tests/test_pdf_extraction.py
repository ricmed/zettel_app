"""Tests for PDF extraction (ADR-012: Docling is mandatory, no PyMuPDF fallback)."""

from __future__ import annotations

import sys

import pytest
from zettel.config import AppConfig
from zettel.harvester.extract import (
    PdfExtractionError,
    build_pdf_pipeline_options,
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


def test_extract_pdf_docling_conversion_failure_raises_pdf_extraction_error(monkeypatch, tmp_path):
    """A Docling conversion error is fatal, with a message distinct from 'not installed'."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    class _BoomConverter:
        def __init__(self, *args, **kwargs):
            pass

        def convert(self, path):
            raise RuntimeError("modelo indisponivel")

    monkeypatch.setattr("docling.document_converter.DocumentConverter", _BoomConverter)

    with pytest.raises(PdfExtractionError, match="conversao Docling lancou um erro"):
        extract_pdf_docling(_cfg(), pdf)


@pytest.mark.parametrize("fails", [False, True])
def test_docling_pipelines_are_released_after_conversion(monkeypatch, tmp_path, fails):
    """Models must be freed while the interpreter is alive, even on failure.

    Otherwise the CodeFormula VLM's __del__ runs at shutdown and logging crashes
    with "sys.meta_path is None".
    """
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    seen: list[dict] = []

    class _CachingConverter(_FakeConverter):
        def __init__(self, *args, **kwargs):
            self.initialized_pipelines = {"pdf": object()}
            seen.append(self.initialized_pipelines)

        def convert(self, path):
            if fails:
                raise RuntimeError("falhou")
            return super().convert(path)

    monkeypatch.setattr("docling.document_converter.DocumentConverter", _CachingConverter)

    if fails:
        with pytest.raises(PdfExtractionError):
            extract_pdf_docling(_cfg(), pdf)
    else:
        extract_pdf_docling(_cfg(), pdf)
    assert seen == [{}]


# ── Docling enrichment options ────────────────────────────────────────


@pytest.mark.parametrize(
    "formulas, code", [(False, False), (True, False), (False, True), (True, True)]
)
def test_pipeline_options_follow_formula_and_code_config(formulas, code):
    """Built against the installed Docling, so a flag it does not know raises here.

    A misspelled enrichment attribute is not an attribute error at config time: it only
    surfaces on the first real PDF harvest, after the conversion is already paid for.
    """
    cfg = _cfg(formulas={"enabled": formulas}, code={"enabled": code})
    options = build_pdf_pipeline_options(cfg, "cpu")
    assert options.do_formula_enrichment is formulas
    assert options.do_code_enrichment is code


def test_pipeline_options_keep_images_independent_of_enrichment():
    cfg = _cfg(images={"enabled": True, "scale": 3.0}, formulas={"enabled": True})
    options = build_pdf_pipeline_options(cfg, "cpu")
    assert options.generate_picture_images is True
    assert options.images_scale == 3.0
    assert options.do_code_enrichment is False


def _code_formula_dtype(options):
    from docling.datamodel.vlm_engine_options import VlmEngineType

    spec = options.code_formula_options.model_spec
    return spec.engine_overrides[VlmEngineType.TRANSFORMERS].extra_config["torch_dtype"]


def test_code_formula_runs_in_float16_on_a_gpu_without_native_bf16(monkeypatch):
    """Emulated bfloat16 overcommitted a 6 GB RTX 2060 (8.63 GB peak) before a heap crash."""
    monkeypatch.setattr("zettel.harvester.extract.cuda_has_native_bf16", lambda: False)
    options = build_pdf_pipeline_options(_cfg(formulas={"enabled": True}), "cuda")
    assert _code_formula_dtype(options) == "float16"


def test_code_formula_keeps_bfloat16_where_the_gpu_supports_it(monkeypatch):
    monkeypatch.setattr("zettel.harvester.extract.cuda_has_native_bf16", lambda: True)
    options = build_pdf_pipeline_options(_cfg(code={"enabled": True}), "cuda")
    assert _code_formula_dtype(options) == "bfloat16"


@pytest.mark.parametrize(
    "device, enrichment",
    [("cpu", True), ("cuda", False)],
)
def test_dtype_is_untouched_off_gpu_or_without_enrichment(monkeypatch, device, enrichment):
    """No GPU to overcommit, or no CodeFormulaV2 loaded at all: nothing to adjust."""

    def must_not_probe():
        raise AssertionError("a capacidade da GPU nao deve ser consultada aqui")

    monkeypatch.setattr("zettel.harvester.extract.cuda_has_native_bf16", must_not_probe)
    cfg = _cfg(formulas={"enabled": enrichment}, code={"enabled": enrichment})
    assert _code_formula_dtype(build_pdf_pipeline_options(cfg, device)) == "bfloat16"


def test_float16_override_does_not_leak_into_other_options(monkeypatch):
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    monkeypatch.setattr("zettel.harvester.extract.cuda_has_native_bf16", lambda: False)
    build_pdf_pipeline_options(_cfg(formulas={"enabled": True}), "cuda")
    assert _code_formula_dtype(PdfPipelineOptions()) == "bfloat16"


def test_conversion_receives_the_enrichment_options(monkeypatch, tmp_path):
    """extract_pdf_docling must hand the built options to the converter, not a default."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    seen = {}

    class _CapturingConverter(_FakeConverter):
        def __init__(self, *args, format_options=None, **kwargs):
            (option,) = format_options.values()
            seen["options"] = option.pipeline_options

    monkeypatch.setattr("docling.document_converter.DocumentConverter", _CapturingConverter)
    extract_pdf_docling(_cfg(formulas={"enabled": True}, code={"enabled": True}), pdf)

    assert seen["options"].do_formula_enrichment is True
    assert seen["options"].do_code_enrichment is True


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

    monkeypatch.setattr("docling.document_converter.DocumentConverter", _FakeConverter)

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
    monkeypatch.setattr("docling.document_converter.DocumentConverter", _HyphenatedConverter)
    text, _metadata = extract_pdf_docling(_cfg(), pdf)
    assert "palavra quebrada" in text
    assert "pala-\nvra" not in text


def test_extract_pdf_docling_preserves_uppercase_continuation_hyphenation(monkeypatch, tmp_path):
    """An uppercase continuation is treated as a likely genuine compound and left alone."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr("docling.document_converter.DocumentConverter", _HyphenatedConverter)
    text, _metadata = extract_pdf_docling(_cfg(), pdf)
    assert "bem-\nVinda" in text


def test_extract_pdf_dispatches_only_to_docling(monkeypatch, tmp_path):
    """extract_pdf always uses Docling — there is no pdf_extractor config to select PyMuPDF."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr("docling.document_converter.DocumentConverter", _FakeConverter)
    text, _metadata = extract_pdf(_cfg(), pdf)
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
