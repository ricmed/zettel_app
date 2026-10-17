"""Text extraction from PDF and Markdown files."""

from __future__ import annotations

import contextlib
import logging
import re
from pathlib import Path
from typing import Any

from zettel.config import AppConfig
from zettel.hashing import dehyphenate_pdf_linebreaks
from zettel.markdown_fences import collapse_table_padding
from zettel.paging import (
    page_map_from_marked_markdown,
    page_map_is_complete,
    read_pdf_page_labels,
    render_page_marked_markdown,
)
from zettel.text_sanitize import sanitize_extracted_text, visible_char_count

logger = logging.getLogger(__name__)


# ── Year Extraction Helpers ────────────────────────────────────────────


def extract_year_from_pdf_date(pdf_date: str | None) -> int | None:
    """Extract year from PDF date strings like 'D:20230415...' or '2023-04-15'."""
    if not pdf_date:
        return None
    m = re.match(r"D:(\d{4})", pdf_date)
    if m:
        return int(m.group(1))
    return extract_year_from_string(pdf_date)


def extract_year_from_string(s: str | None) -> int | None:
    """Extract a plausible 4-digit year (1900-2099) from any string."""
    if not s:
        return None
    m = re.search(r"\b(19\d{2}|20\d{2})\b", s)
    return int(m.group(1)) if m else None


# ── Text Extraction Dispatch ────────────────────────────────────────────


def extract_text(cfg: AppConfig, file_path: Path, origin_type: str) -> tuple[str, dict[str, Any]]:
    """Extract text and basic metadata from a file.

    Extracted images (when images.enabled) are stashed in metadata["_images"] as a
    list of {checksum, path, context_snippet} for later DB registration.
    """
    if origin_type == "pdf":
        text, metadata = extract_pdf(cfg, file_path)
    else:
        text, metadata = extract_markdown(cfg, file_path)

    # Single sanitization point: everything downstream (extraction checksum,
    # chunks, prompts, embeddings, vault) reads the cleaned text.
    text = sanitize_extracted_text(text, file_path.name)
    # Docling pads Markdown tables with spaces. Collapse that before the
    # extraction checksum so chunks and dedupe see the cells, not the alignment.
    text = collapse_table_padding(text)
    if visible_char_count(text) == 0:
        raise EmptyTextLayerError(
            f"Arquivo '{file_path.name}' nao produziu texto visivel apos a "
            "extracao. Se for um PDF escaneado, rode OCR (ex.: "
            "'ocrmypdf entrada.pdf saida.pdf') e tente de novo."
        )
    return text, metadata


# ── PDF Extraction ─────────────────────────────────────────────────────


class PdfExtractionError(RuntimeError):
    """Fatal PDF extraction failure. Docling is mandatory; there is no fallback (ADR-012)."""


class EmptyTextLayerError(PdfExtractionError):
    """The file carries no text layer (photographed/scanned PDF) — OCR it first."""


# A scanned PDF costs ~1.5s/page in Docling and yields nothing, so the text
# layer is probed on the first few pages before the converter is built.
TEXT_LAYER_PROBE_PAGES = 4
TEXT_LAYER_MIN_CHARS = 40
# Rendered glyph height, not the raw Tf operand. A text matrix often leaves
# Tf at 1 while the glyph is body-sized; the character box is the size on
# the page. Body text and footnotes sit well above 2 pt.
_TINY_FONT_PT = 2.0
_NEAR_WHITE = 250


def find_render_hidden_text(file_path: Path) -> list[dict[str, Any]]:
    """Runs of PDF text a reviewer cannot see: near-white fill, or about 1 pt.

    Signaling only. The characters stay in the extracted Markdown: pdfium's
    stream and Docling's text are not the same cut, so deleting a matched
    substring would also delete a visible copy of the same words. The caller
    logs the finding. ``[]`` when pdfium cannot open the file.
    """
    try:
        import pypdfium2 as pdfium
        import pypdfium2.raw as pdfium_c
    except ImportError:
        logger.debug("pdfium indisponivel; probe de texto oculto ignorado.")
        return []

    try:
        document = pdfium.PdfDocument(str(file_path))
    except Exception as exc:
        logger.debug("pdfium nao abriu %s para texto oculto (%s).", file_path.name, exc)
        return []

    try:
        return _hidden_runs(document, pdfium_c)
    finally:
        document.close()


def _hidden_runs(document: Any, pdfium_c: Any) -> list[dict[str, Any]]:
    import ctypes

    hits: list[dict[str, Any]] = []
    for page_index in range(len(document)):
        page = document[page_index]
        text_page = page.get_textpage()
        try:
            count = text_page.count_chars()
            reasons: list[str | None] = []
            chars: list[str] = []
            for index in range(count):
                chars.append(text_page.get_text_range(index, 1) or "")
                reasons.append(_char_hidden_reason(pdfium_c, ctypes, text_page, index))
            hits.extend(_collapse_hidden_runs(page_index + 1, chars, reasons))
        finally:
            text_page.close()
            page.close()
    return hits


def _rendered_font_pt(text_page: Any, index: int) -> float:
    """Font size on the page. ``Tf`` alone is 1 when the text matrix does the scaling."""
    obj = text_page.get_textobj(index)
    if obj is None:
        return 0.0
    try:
        matrix = obj.get_matrix()
        scale = (matrix.c * matrix.c + matrix.d * matrix.d) ** 0.5
        return float(obj.get_font_size()) * (scale or 1.0)
    finally:
        obj.close()


def _char_hidden_reason(pdfium_c: Any, ctypes: Any, text_page: Any, index: int) -> str | None:
    red, green, blue, alpha = (
        ctypes.c_uint(),
        ctypes.c_uint(),
        ctypes.c_uint(),
        ctypes.c_uint(),
    )
    if pdfium_c.FPDFText_GetFillColor(
        text_page.raw,
        index,
        ctypes.byref(red),
        ctypes.byref(green),
        ctypes.byref(blue),
        ctypes.byref(alpha),
    ):
        if alpha.value == 0 or min(red.value, green.value, blue.value) >= _NEAR_WHITE:
            return "white"
    rendered = _rendered_font_pt(text_page, index)
    if 0 < rendered <= _TINY_FONT_PT:
        return "tiny"
    return None


def _collapse_hidden_runs(
    page: int, chars: list[str], reasons: list[str | None]
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    start: int | None = None
    reason = ""
    for index, current in enumerate([*reasons, None]):
        if start is None:
            if current:
                start = index
                reason = current
            continue
        if current == reason:
            continue
        sample = "".join(chars[start:index]).strip()
        # A white page number is a few glyphs. A hidden instruction is a phrase.
        if len(sample) >= 12:
            runs.append(
                {
                    "page": page,
                    "reason": reason,
                    "chars": index - start,
                    "sample": sample[:80],
                }
            )
        start = index if current else None
        reason = current or ""
    return runs


def assert_pdf_has_text_layer(file_path: Path) -> None:
    """Raise ``EmptyTextLayerError`` when the PDF looks like a scan.

    Uses pdfium (already pulled in by Docling) to read the raw text of the first
    ``TEXT_LAYER_PROBE_PAGES`` pages. Fewer than ``TEXT_LAYER_MIN_CHARS`` visible
    characters across them means there is nothing for Docling to extract. When
    pdfium is unavailable the probe is skipped: the post-extraction emptiness
    check in ``extract_text`` still catches the same file, only later.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        logger.debug("pdfium indisponivel; probe de camada de texto ignorado.")
        return

    try:
        document = pdfium.PdfDocument(str(file_path))
    except Exception as e:  # corrupt/encrypted file — let Docling report it
        logger.debug("pdfium nao abriu %s (%s); probe ignorado.", file_path.name, e)
        return

    try:
        sampled = "".join(
            _pdfium_page_text(document, page_no)
            for page_no in range(min(TEXT_LAYER_PROBE_PAGES, len(document)))
        )
    finally:
        document.close()

    if visible_char_count(sampled) >= TEXT_LAYER_MIN_CHARS:
        return
    raise EmptyTextLayerError(
        f"PDF '{file_path.name}' nao tem camada de texto (provavelmente "
        f"escaneado): menos de {TEXT_LAYER_MIN_CHARS} caracteres nas primeiras "
        f"{TEXT_LAYER_PROBE_PAGES} paginas. Rode OCR antes do harvest, ex.: "
        "'ocrmypdf entrada.pdf saida.pdf'."
    )


def _pdfium_page_text(document: Any, page_no: int) -> str:
    try:
        page = document[page_no]
        textpage = page.get_textpage()
        try:
            return textpage.get_text_bounded() or ""
        finally:
            textpage.close()
    except Exception:
        return ""


def extract_pdf(cfg: AppConfig, file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from PDF using Docling (the only supported extractor)."""
    assert_pdf_has_text_layer(file_path)
    hidden = find_render_hidden_text(file_path)
    if hidden:
        preview = "; ".join(
            f"p.{hit['page']} {hit['reason']} ({hit['chars']}c) {hit['sample']!r}"
            for hit in hidden[:6]
        )
        logger.warning(
            "PDF '%s' tem texto invisivel por renderizacao (%d trechos). "
            "O texto permanece na extracao; o revisor nao o ve no PDF. %s",
            file_path.name,
            len(hidden),
            preview,
        )
    text, metadata = extract_pdf_docling(cfg, file_path)
    if hidden:
        metadata["_render_hidden"] = hidden
    return text, metadata


def build_pdf_pipeline_options(cfg: AppConfig, device: str) -> Any:
    """Docling's PDF options for this config. Building them loads no model.

    Kept apart from the conversion so the options can be checked against the installed
    Docling: an attribute that does not exist (e.g. a misspelled enrichment flag) raises
    here, in a test, instead of on the first real harvest.

    Formula and code enrichment are both served by one local vision model,
    ``CodeFormulaV2``: a formula region is decoded to LaTeX and a code region to text,
    in place of the ``<!-- formula-not-decoded -->`` placeholder or the flattened code
    Docling writes otherwise.
    """
    from docling.datamodel.pipeline_options import (
        AcceleratorDevice,
        AcceleratorOptions,
        PdfPipelineOptions,
    )

    accel_device = AcceleratorDevice.CUDA if device == "cuda" else AcceleratorDevice.CPU
    options = PdfPipelineOptions()
    options.accelerator_options = AcceleratorOptions(num_threads=4, device=accel_device)
    if cfg.images.enabled:
        options.generate_picture_images = True
        options.images_scale = cfg.images.scale
    options.do_formula_enrichment = cfg.formulas.enabled
    options.do_code_enrichment = cfg.code.enabled
    if (
        (cfg.formulas.enabled or cfg.code.enabled)
        and device == "cuda"
        and not cuda_has_native_bf16()
    ):
        _load_code_formula_in_float16(options)
    return options


# Docling loads CodeFormulaV2 in bfloat16. On a GPU without native bfloat16 (e.g. Turing,
# compute capability 7.5) torch emulates it, and the emulation costs MORE memory than
# float32, not less. Measured on an RTX 2060 (6 GB) over a 7-page math PDF:
#
#   bfloat16 (emulated)  776 s  peak 8.63 GB  -- past physical VRAM, spills into system RAM
#   float32              150-163 s  peak 4.13 GB
#   float16              123 s      peak 2.45 GB
#
# The bfloat16 run had image extraction off; float16 and float32 were measured with it both
# off and on, with the same peak. With images on, float16 and float32 produced
# byte-identical Markdown. The bfloat16 run is the condition
# under which a real harvest died with STATUS_HEAP_CORRUPTION (0xc0000374) and no Python
# traceback; the crash itself did not reproduce, the memory overcommit did.
def cuda_has_native_bf16() -> bool:
    import torch

    return torch.cuda.is_available() and torch.cuda.is_bf16_supported(including_emulation=False)


def _load_code_formula_in_float16(options: Any) -> None:
    from docling.datamodel.vlm_engine_options import VlmEngineType

    engine = options.code_formula_options.model_spec.engine_overrides[VlmEngineType.TRANSFORMERS]
    engine.extra_config["torch_dtype"] = "float16"
    logger.info(
        "Docling: GPU sem bfloat16 nativo -- CodeFormulaV2 carregado em float16 "
        "(bfloat16 emulado estoura a VRAM)"
    )


def release_docling_pipelines(converter: Any) -> None:
    """Free the converter's pipelines (and their models) now, not at exit.

    The converter should die with this module's frame, but it does not: an
    exception cached during the lazy Docling/transformers import keeps a
    traceback whose ``f_back`` chain reaches the frame that ran the import,
    with ``converter`` still in its locals. The CodeFormula VLM then survives
    until interpreter shutdown, where its ``__del__`` logs through a handler
    that can no longer import anything ("sys.meta_path is None").

    Emptying the pipeline cache runs that cleanup while the interpreter is
    alive, and gives the VRAM back after every file. ``initialized_pipelines``
    is Docling's own per-converter cache; there is no public release method.
    """
    pipelines = getattr(converter, "initialized_pipelines", None)
    if isinstance(pipelines, dict):
        pipelines.clear()


def extract_pdf_docling(cfg: AppConfig, file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text from PDF using Docling, with GPU acceleration when available.

    Docling is mandatory (ADR-012, no PyMuPDF fallback): a missing install or a
    failed conversion raises ``PdfExtractionError`` instead of degrading to
    plain text or an empty source.
    """
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as e:
        raise PdfExtractionError(
            f"Extracao de PDF falhou para '{file_path.name}': Docling nao esta "
            "instalado. Docling e obrigatorio e nao ha fallback. Verifique a "
            "instalacao ('uv sync' / 'pip install docling') e a disponibilidade "
            "de GPU/CPU."
        ) from e

    from zettel.config import detect_device

    device = detect_device(cfg.device)
    pipeline_options = build_pdf_pipeline_options(cfg, device)

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
        }
    )
    logger.info(
        "Docling: Iniciando conversao de %s (dispositivo: %s)",
        file_path.name,
        device.upper(),
    )

    try:
        result = converter.convert(str(file_path))
    except Exception as e:
        raise PdfExtractionError(
            f"Extracao de PDF falhou para '{file_path.name}': a conversao "
            f"Docling lancou um erro ({e}). Docling e obrigatorio e nao ha "
            "fallback. Verifique a instalacao e a disponibilidade de GPU/CPU."
        ) from e
    finally:
        release_docling_pipelines(converter)

    # One entry per PDF file page, from Docling's per-page export (prov.page_no).
    # An unnumbered page-break placeholder drops pages that have no exportable
    # item, and every later chunk then cites a page that is too small.
    num_pages = docling_num_pages(result.document)
    page_map = docling_page_map_by_export(result.document, num_pages)
    exact = page_map_is_complete(page_map, num_pages)
    if exact:
        text = render_page_marked_markdown(page_map)
    else:
        logger.error(
            "Mapa de paginas incompleto para %s (%s paginas no arquivo, %s no mapa). "
            "Nenhuma pagina sera rotulada como exata.",
            file_path.name,
            num_pages if num_pages is not None else "?",
            len(page_map),
        )
        text = result.document.export_to_markdown()
        page_map = []

    images: list[dict[str, Any]] = []
    if cfg.images.enabled:
        from zettel.assets import extract_docling_images

        text, images = extract_docling_images(cfg, result.document, text)

    # PDF-only cleanup: merge line-break hyphenation before persistence, so the
    # LLM prompt, the embedding and the anchor_quote never see "pala-\nvra".
    # normalize_text_for_hash() also runs this (idempotent) for the hashing path.
    text = dehyphenate_pdf_linebreaks(text)
    if exact:
        parsed = page_map_from_marked_markdown(text)
        if page_map_is_complete(parsed, num_pages):
            page_map = parsed
        else:
            logger.error(
                "Mapa de paginas de %s deixou de cobrir 1..%s apos a limpeza do texto. "
                "Nenhuma pagina sera rotulada como exata.",
                file_path.name,
                num_pages,
            )
            page_map = []
            exact = False

    metadata: dict[str, Any] = {
        "title": file_path.stem,
        "authors": [],
        "year": None,
        "_images": images,
        "_file_fields": [],
    }
    try:
        origin = getattr(result.document, "origin", None)
        if origin:
            file_fields: list[str] = []
            if getattr(origin, "title", None):
                metadata["title"] = origin.title
                file_fields.append("title")
            if getattr(origin, "author", None):
                metadata["authors"] = [a.strip() for a in origin.author.split(",") if a.strip()]
                file_fields.append("authors")
            if getattr(origin, "date", None):
                metadata["year"] = extract_year_from_string(origin.date)
                if metadata["year"] is not None:
                    file_fields.append("year")
            metadata["_file_fields"] = file_fields
    except Exception as exc:
        logger.warning(
            "Metadados embutidos do PDF %s nao foram lidos: %s",
            file_path.name,
            exc,
        )

    if num_pages:
        metadata["total_pages_file"] = num_pages

    metadata["_page_labels"] = read_pdf_page_labels(file_path)
    outline = read_pdf_outline(file_path)
    if outline:
        from zettel.harvester.chunking import apply_pdf_outline

        rewritten = apply_pdf_outline(text, outline)
        if rewritten != text:
            text = rewritten
            logger.info(
                "Outline do PDF aplicado a %s (%d entradas).",
                file_path.name,
                len(outline),
            )

    if exact and page_map:
        metadata["_page_map"] = page_map
        logger.info("Mapa de paginas (Docling): %d paginas do arquivo", len(page_map))
    elif not exact:
        logger.warning(
            "Nenhum mapa de paginas exato para %s; paging.py nao rotula pagina "
            "como explicita (sem PyMuPDF).",
            file_path.name,
        )

    logger.info(
        "Docling: Conversao concluida - %s (%d caracteres, %s paginas)",
        file_path.name,
        len(text),
        metadata.get("total_pages_file", "?"),
    )
    return text, metadata


# ── PDF Page Mapping Helpers ───────────────────────────────────────────


def docling_num_pages(document: Any) -> int | None:
    raw = getattr(document, "num_pages", None)
    if callable(raw):
        try:
            raw = raw()
        except Exception:
            raw = None
    if isinstance(raw, int) and raw > 0:
        return raw
    pages = getattr(document, "pages", None)
    if isinstance(pages, dict) and pages:
        try:
            return max(int(k) for k in pages)
        except (TypeError, ValueError):
            return len(pages)
    return None


def docling_page_map_by_export(document: Any, num_pages: int | None) -> list[tuple[int, str]]:
    """One Markdown slice per PDF file page, including pages with no items.

    ``export_to_markdown(page_no=)`` is filtered by Docling's provenance, so a
    blank or image-only page is an empty string instead of a missing slot.
    ``[]`` when the document does not support per-page export — the caller
    then refuses to label any page as exact.
    """
    n = num_pages or docling_num_pages(document) or 0
    if n <= 0:
        return []
    page_map: list[tuple[int, str]] = []
    for page_no in range(1, n + 1):
        try:
            md = document.export_to_markdown(page_no=page_no)
        except TypeError:
            logger.error(
                "Docling nao exporta por pagina; mapa de %d paginas descartado.",
                n,
            )
            return []
        page_map.append((page_no, (md or "").strip()))
    return page_map


def read_pdf_outline(file_path: Path) -> list[tuple[int, str]]:
    """Bookmark ``(level, title)`` pairs. ``[]`` when the PDF has no outline.

    ``level`` is pdfium's: 0 is the top of the tree. Heading text is what the
    chunker matches; the destination page is not required.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        logger.debug("pdfium indisponivel; outline do PDF ignorado.")
        return []
    try:
        document = pdfium.PdfDocument(str(file_path))
    except Exception as exc:
        logger.debug("pdfium nao abriu %s para o outline (%s).", file_path.name, exc)
        return []
    try:
        entries: list[tuple[int, str]] = []
        for bookmark in document.get_toc(max_depth=6):
            title = str(bookmark.get_title() or "").strip()
            if title:
                entries.append((int(bookmark.level), title))
        return entries
    finally:
        document.close()


def page_map_for_source(src: dict[str, Any]) -> list[tuple[int, str]]:
    """Rebuild a page map for rechunk / resume from persisted marked text.

    Only numbered ``<!-- zettel:page N -->`` markers in ``extracted_text``
    are used — there is no PyMuPDF fallback to reopen the original PDF.
    The map is returned only when it covers ``1..total_pages_file`` (or the
    source has no stored page count and the markers themselves are 1..N
    with no gaps). A divergent map is dropped so resume cannot label a
    slid page as exact.
    """
    text = src.get("extracted_text") or ""
    page_map = page_map_from_marked_markdown(text)
    if not page_map:
        return []
    total = src.get("total_pages_file")
    if total:
        if page_map_is_complete(page_map, int(total)):
            return page_map
        logger.error(
            "Mapa persistido tem %d entradas e a fonte declara %s paginas; "
            "paginas nao serao rotuladas como exatas.",
            len(page_map),
            total,
        )
        return []
    if page_map_is_complete(page_map, page_map[-1][0]):
        return page_map
    logger.error(
        "Mapa persistido nao cobre 1..%s sem buracos; paginas nao serao rotuladas como exatas.",
        page_map[-1][0],
    )
    return []


# ── Markdown Extraction ────────────────────────────────────────────────


def extract_markdown(cfg: AppConfig, file_path: Path) -> tuple[str, dict[str, Any]]:
    """Extract text and metadata from a Markdown file.

    Reads YAML frontmatter to populate author, year, and title so that
    Markdown sources generate proper citekeys and SRC notes — instead of
    discarding the frontmatter entirely.
    """
    import yaml

    content = file_path.read_text(encoding="utf-8")
    fm_meta: dict[str, Any] = {}
    body = content

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                fm_meta = yaml.safe_load(parts[1]) or {}
            except Exception:
                fm_meta = {}
            body = parts[2].strip()

    # Extract title: frontmatter > first H1 heading > filename stem
    title: str = file_path.stem
    title_match = re.match(r"^#\s+(.+)$", body, re.MULTILINE)
    if title_match:
        title = title_match.group(1).strip()
    if fm_meta.get("title"):
        title = str(fm_meta["title"])

    # Extract authors from frontmatter
    authors: list[str] = []
    raw_authors = fm_meta.get("authors") or fm_meta.get("author")
    if raw_authors:
        if isinstance(raw_authors, list):
            authors = [str(a) for a in raw_authors if a]
        elif isinstance(raw_authors, str) and raw_authors.strip():
            authors = [raw_authors.strip()]

    # Extract year from frontmatter
    year: int | None = None
    if fm_meta.get("year"):
        with contextlib.suppress(ValueError, TypeError):
            year = int(fm_meta["year"])
    if year is None and fm_meta.get("date"):
        year = extract_year_from_string(str(fm_meta["date"]))

    from zettel.assets import extract_markdown_images

    body, images = extract_markdown_images(cfg, body, file_path)

    user_fields: list[str] = []
    if fm_meta.get("title"):
        user_fields.append("title")
    if authors:
        user_fields.append("authors")
    if year is not None and (fm_meta.get("year") or fm_meta.get("date")):
        user_fields.append("year")

    meta: dict[str, Any] = {
        "title": title,
        "authors": authors,
        "year": year,
        "_images": images,
        "_user_fields": user_fields,
    }
    # Pass through known bibliographic frontmatter fields for ABNT inference.
    _FM_CANONICAL = {
        "city": "place",
        "editora": "publisher",
        "traducao": "translator",
        "periodico": "journal",
        "number": "issue",
        "paginas": "pages",
        "access_date": "accessed_at",
        "instituicao": "institution",
        "curso": "course",
        "disciplina": "discipline",
        "orientador": "advisor",
        "editors": "book_editors",
    }
    _BIBLIO_KEYS = (
        "document_type",
        "subtitle",
        "edition",
        "place",
        "city",
        "publisher",
        "editora",
        "translator",
        "traducao",
        "isbn",
        "journal",
        "periodico",
        "volume",
        "issue",
        "number",
        "pages",
        "paginas",
        "doi",
        "url",
        "accessed_at",
        "access_date",
        "site_name",
        "published_at",
        "institution",
        "instituicao",
        "course",
        "curso",
        "discipline",
        "disciplina",
        "degree",
        "advisor",
        "orientador",
        "event_name",
        "report_number",
        "chapter_title",
        "book_title",
        "chapter_authors",
        "book_editors",
        "editors",
    )
    for key in _BIBLIO_KEYS:
        if key in fm_meta and fm_meta[key] not in (None, ""):
            meta[key] = fm_meta[key]
            user_fields.append(_FM_CANONICAL.get(key, key))

    return body, meta
