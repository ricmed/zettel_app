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
    find_render_hidden_text,
    page_map_for_source,
)
from zettel.paging import page_marker


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
    def __init__(self, pages: str | list[str]):
        self._pages = [pages] if isinstance(pages, str) else list(pages)
        self.origin = _FakeOrigin()
        self.num_pages = len(self._pages)

    def export_to_markdown(self, page_break_placeholder: str | None = None, page_no=None):
        if page_no is None:
            return "\n\n".join(page for page in self._pages if page)
        index = int(page_no) - 1
        if index < 0 or index >= len(self._pages):
            return ""
        return self._pages[index]


class _FakeResult:
    def __init__(self, pages: str | list[str]):
        self.document = _FakeDocument(pages)


class _FakeConverter:
    def __init__(self, *args, **kwargs):
        pass

    def convert(self, path):
        return _FakeResult(
            [
                "# Capa",
                "# Capitulo 1\n\nConteudo unico sobre grafos de conhecimento.",
            ]
        )


def test_extract_pdf_docling_success_builds_text_and_page_map(monkeypatch, tmp_path):
    """A successful Docling conversion returns marked text, metadata, and a page map."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr("docling.document_converter.DocumentConverter", _FakeConverter)

    text, metadata = extract_pdf_docling(_cfg(), pdf)

    assert page_marker(1) in text
    assert page_marker(2) in text
    assert metadata["title"] == "Titulo Extraido"
    assert metadata["authors"] == ["Fulano", "Beltrano"]
    assert metadata["year"] == 2023
    assert metadata["total_pages_file"] == 2
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
    assert page_marker(1) in text
    assert not hasattr(AppConfig(), "pdf_extractor")


def test_page_map_for_source_has_no_pymupdf_fallback(tmp_path):
    """Rechunk/resume only reads numbered markers from persisted text — no PDF reopen."""
    from zettel.paging import render_page_marked_markdown

    marked = render_page_marked_markdown([(1, "# A"), (2, "# B")])
    src_with_markers = {
        "extracted_text": marked,
        "origin_path": str(tmp_path / "missing.pdf"),
    }
    assert page_map_for_source(src_with_markers) == [(1, "# A"), (2, "# B")]

    src_without_markers = {
        "extracted_text": "# Just one page, no markers",
        "origin_path": str(tmp_path / "missing.pdf"),
    }
    assert page_map_for_source(src_without_markers) == []


def test_extract_pdf_keeps_blank_pages_in_the_map(monkeypatch, tmp_path):
    """Pages with no exportable text stay in the map so later pages do not slide."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    class _BlankPages:
        def __init__(self, *args, **kwargs):
            pass

        def convert(self, path):
            return _FakeResult(
                [
                    "",
                    "Trecho unico que so existe na pagina dois.",
                    "",
                    "Trecho unico que so existe na pagina quatro.",
                    "",
                ]
            )

    monkeypatch.setattr("docling.document_converter.DocumentConverter", _BlankPages)
    text, metadata = extract_pdf_docling(_cfg(), pdf)
    page_map = metadata["_page_map"]
    assert [page for page, _ in page_map] == [1, 2, 3, 4, 5]
    assert page_map[0][1] == ""
    assert page_map[2][1] == ""
    assert page_map[4][1] == ""
    assert "pagina dois" in page_map[1][1]
    assert "pagina quatro" in page_map[3][1]
    assert page_marker(2) in text
    assert page_marker(5) in text
    assert (
        page_map_for_source(
            {"extracted_text": text, "total_pages_file": metadata["total_pages_file"]}
        )
        == page_map
    )


def test_extract_pdf_drops_the_map_when_per_page_export_fails(monkeypatch, tmp_path, caplog):
    """A map that is not 1..N is not attached, so no chunk is labeled exact."""
    import logging

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    class _NoPageExport:
        num_pages = 3
        origin = _FakeOrigin()

        def export_to_markdown(self, page_break_placeholder=None, page_no=None):
            if page_no is not None:
                raise TypeError("sem exportacao por pagina")
            return "# Ainda da para ler\n\nTexto do documento."

    class _Converter:
        def __init__(self, *args, **kwargs):
            pass

        def convert(self, path):
            class _Result:
                document = _NoPageExport()

            return _Result()

    monkeypatch.setattr("docling.document_converter.DocumentConverter", _Converter)
    with caplog.at_level(logging.ERROR):
        text, metadata = extract_pdf_docling(_cfg(), pdf)
    assert "_page_map" not in metadata
    assert metadata["total_pages_file"] == 3
    assert page_marker(1) not in text
    assert "Ainda da para ler" in text
    assert "rotulada como exata" in caplog.text.lower()


def _write_text_pdf(path, stream: bytes) -> None:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(offsets)}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


def test_render_hidden_text_flags_white_and_one_point(tmp_path):
    stream = (
        b"BT /F1 12 Tf 0 0 0 rg 72 720 Td (Visible sentence for the reviewer.) Tj "
        b"1 1 1 rg 0 -24 Td (Hidden white sentence for the model.) Tj "
        b"0 0 0 rg /F1 1 Tf 0 -24 Td (Tiny one point sentence here.) Tj ET"
    )
    path = tmp_path / "hidden.pdf"
    _write_text_pdf(path, stream)
    hits = find_render_hidden_text(path)
    reasons = {hit["reason"] for hit in hits}
    assert reasons == {"white", "tiny"}
    assert any("Hidden white" in hit["sample"] for hit in hits)
    assert any("Tiny one point" in hit["sample"] for hit in hits)


def test_render_hidden_text_ignores_ordinary_black_body(tmp_path):
    stream = b"BT /F1 12 Tf 0 0 0 rg 72 720 Td (A normal sentence on the page.) Tj ET"
    path = tmp_path / "plain.pdf"
    _write_text_pdf(path, stream)
    assert find_render_hidden_text(path) == []
