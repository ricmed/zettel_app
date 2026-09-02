"""Tests for structural (H3-H6) section splitting and chunking — Fase 1."""

import pytest

from zettel.config import AppConfig
from zettel.harvester import (
    chunk_and_persist as _chunk_and_persist,
    iter_fenced_spans as _iter_fenced_spans,
    merge_small_sections as _merge_small_sections,
    run_rechunk,
    split_chapter_into_chunks as _split_chapter_into_chunks,
    split_chapter_into_sections as _split_chapter_into_sections,
    split_into_chapters as _split_into_chapters,
)
from zettel.state import StateDB


class _FakeIdx:
    def __init__(self):
        self.chunks_store: set[str] = set()

    def upsert_chunk(self, chunk_id, text, metadata, **kwargs):
        self.chunks_store.add(chunk_id)

    def delete_chunks(self, chunk_ids):
        for cid in chunk_ids:
            self.chunks_store.discard(cid)

    def existing_ids(self, collection_name, ids):
        return {cid for cid in ids if cid in self.chunks_store}


def _cfg(**chunking):
    cfg = AppConfig()
    for k, v in chunking.items():
        setattr(cfg.chunking, k, v)
    return cfg


def test_sections_build_hierarchical_path():
    filler = "conteudo suficientemente longo para nao ser fundido pela regra de tamanho minimo. "
    text = (
        f"Preambulo do capitulo com {filler}\n\n"
        f"### Sub A\n\nTexto A {filler}\n\n"
        f"#### Sub A1\n\nTexto A1 {filler}\n\n"
        f"### Sub B\n\nTexto B {filler}"
    )
    sections = _split_chapter_into_sections("Capitulo 1", text, min_section_chars=20)
    paths = [s["section_path"] for s in sections]
    assert "Capitulo 1" in paths  # preamble under chapter title
    assert "Capitulo 1 > Sub A" in paths
    assert "Capitulo 1 > Sub A > Sub A1" in paths
    assert "Capitulo 1 > Sub B" in paths  # A1 (H4) popped, B is H3 again


def test_no_headings_yields_single_section():
    sections = _split_chapter_into_sections("Documento completo", "Apenas texto plano.", 200)
    assert len(sections) == 1
    assert sections[0]["section_path"] == "Documento completo"
    assert sections[0]["text"] == "Apenas texto plano."


def test_small_sections_are_merged_forward():
    sections = [
        {"section_path": "C > A", "text": "curto"},
        {"section_path": "C > B", "text": "x" * 300},
    ]
    merged = _merge_small_sections(sections, min_section_chars=200)
    assert len(merged) == 1
    assert merged[0]["section_path"] == "C > B"
    assert "curto" in merged[0]["text"]


def test_trailing_small_section_merges_into_previous():
    sections = [
        {"section_path": "C > A", "text": "y" * 300},
        {"section_path": "C > B", "text": "curto"},
    ]
    merged = _merge_small_sections(sections, min_section_chars=200)
    assert len(merged) == 1
    assert merged[0]["section_path"] == "C > A"
    assert "curto" in merged[0]["text"]


def test_large_section_is_subdivided():
    cfg = _cfg(chunk_size=100, chunk_overlap=10, min_section_chars=50)
    chapter = {"title": "Cap", "text": "palavra " * 200, "locator": "Cap"}
    pairs = _split_chapter_into_chunks(cfg, chapter)
    assert len(pairs) > 1
    assert all(path == "Cap" for path, _ in pairs)


def test_chunk_pairs_carry_section_path():
    cfg = _cfg(chunk_size=1000, chunk_overlap=100, min_section_chars=20)
    chapter = {
        "title": "Cap",
        "text": "### Alpha\n\nConteudo alpha suficiente.\n\n### Beta\n\nConteudo beta suficiente.",
        "locator": "Cap",
    }
    pairs = _split_chapter_into_chunks(cfg, chapter)
    paths = {path for path, _ in pairs}
    assert paths == {"Cap > Alpha", "Cap > Beta"}


def test_split_into_chapters_regression_no_headings():
    chapters = _split_into_chapters("Sem nenhum heading aqui.", "md")
    assert len(chapters) == 1
    assert chapters[0]["title"] == "Documento completo"


def test_chunk_and_persist_writes_section_path(tmp_path):
    db = StateDB(tmp_path / "s.db")
    try:
        db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
        idx = _FakeIdx()
        chapters = [{
            "title": "Cap",
            "text": "### Alpha\n\nConteudo alpha suficientemente longo para virar chunk.\n\n"
                    "### Beta\n\nConteudo beta suficientemente longo para virar chunk.",
            "locator": "Cap",
        }]
        n = _chunk_and_persist(_cfg(min_section_chars=20), db, idx, "@S", chapters)
        assert n >= 2
        paths = {c["section_path"] for c in db.get_chunks_for_source("@S")}
        assert paths == {"Cap > Alpha", "Cap > Beta"}
    finally:
        db.close()


def test_chunk_and_persist_collapses_identical_content(tmp_path):
    """Same normalized text in two sections shares one content-addressed chunk_id."""
    db = StateDB(tmp_path / "s.db")
    try:
        db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
        idx = _FakeIdx()
        same = "Conteudo identico suficientemente longo para virar chunk unico."
        chapters = [{
            "title": "Cap",
            "text": f"### Alpha\n\n{same}\n\n### Beta\n\n{same}",
            "locator": "Cap",
        }]
        n = _chunk_and_persist(_cfg(min_section_chars=20), db, idx, "@S", chapters)
        chunks = db.get_chunks_for_source("@S")
        assert n == 1
        assert len(chunks) == 1
        assert chunks[0]["section_path"] == "Cap > Alpha"
        assert idx.chunks_store == {chunks[0]["chunk_id"]}
    finally:
        db.close()


def test_rechunk_removes_orphans_on_changed_text(tmp_path):
    db = StateDB(tmp_path / "s.db")
    try:
        db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
        idx = _FakeIdx()

        text_v1 = "# Cap\n\n" + "conteudo original bem longo. " * 20
        db.update_source_texts("@S", extracted_text=text_v1)
        run_rechunk(AppConfig(), db, idx, "@S")
        ids_v1 = {c["chunk_id"] for c in db.get_chunks_for_source("@S")}
        assert ids_v1 and ids_v1 == idx.chunks_store

        # Change the source text and rechunk: old chunks must be pruned from both stores.
        text_v2 = "# Cap\n\n" + "conteudo completamente diferente agora. " * 20
        db.update_source_texts("@S", extracted_text=text_v2)
        run_rechunk(AppConfig(), db, idx, "@S")
        ids_v2 = {c["chunk_id"] for c in db.get_chunks_for_source("@S")}

        assert ids_v2 != ids_v1
        assert not (ids_v1 & ids_v2)          # no stale ids remain in SQLite
        assert idx.chunks_store == ids_v2      # index matches SQLite exactly
    finally:
        db.close()


def test_rechunk_skips_source_without_extracted_text(tmp_path):
    db = StateDB(tmp_path / "s.db")
    try:
        db.upsert_source("@Old", "Old", "T", [], None, "h", "/p", "md")  # no extracted_text
        idx = _FakeIdx()
        stats = run_rechunk(AppConfig(), db, idx, "@Old")
        assert stats["skipped"] == 1
        assert stats["sources"] == 0
    finally:
        db.close()


def test_incomplete_chunking_detected_and_rechunk_completes(tmp_path):
    """Simulates interrupted harvest: only early chapters persisted; rechunk fills the rest."""
    from zettel.hashing import sha256_hex, normalize_text_for_hash
    from zettel.harvester import source_chunking_incomplete

    db = StateDB(tmp_path / "s.db")
    try:
        filler = "conteudo suficientemente longo para formar um chunk de capitulo. "
        text = (
            f"# Cap A\n\n{filler * 5}\n\n"
            f"# Cap B\n\n{filler * 5}\n\n"
            f"# Cap C\n\n{filler * 5} veja 90_Assets/img-late.png aqui\n"
        )
        db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
        db.update_source_texts("@S", extracted_text=text)

        # Persist only the first chapter (interrupted harvest).
        ch0_text = filler * 5
        db.upsert_chapter(
            "@S::ch000", "@S", "Cap A",
            sha256_hex(normalize_text_for_hash(ch0_text)), "Cap A",
        )
        db.upsert_chunk(
            "@S::@S::ch000::aaaa", "@S", "@S::ch000", ch0_text,
            sha256_hex(normalize_text_for_hash(ch0_text)),
        )
        # Asset registered against the late chapter id that does not exist yet.
        db.upsert_asset(
            "@S::img::late", "@S", "90_Assets/img-late.png", "cklate",
            chapter_id="@S::ch002",
        )

        assert source_chunking_incomplete(db, "@S")
        assert len(db.get_chapters_for_source("@S")) == 1

        idx = _FakeIdx()
        stats = run_rechunk(AppConfig(), db, idx, "@S")
        assert stats["sources"] == 1
        assert not source_chunking_incomplete(db, "@S")
        chapters = db.get_chapters_for_source("@S")
        assert len(chapters) == 3
        # Asset chapter_id re-resolved to the chapter that contains the path.
        asset = db.get_asset("@S::img::late")
        assert asset["chapter_id"] == "@S::ch002"
        # Late chapter text is now in some chunk.
        joined = "\n".join(c["text"] for c in db.get_chunks_for_source("@S"))
        assert "img-late.png" in joined
    finally:
        db.close()


# ── Fenced code blocks as atomic units ────────────────────────────────

# Template like the one in data/cache/chunk-dumps/chunks-DesignEArquitetura.md:
# a single ```markdown fence whose internal headings are illustrative, not structure.
HLD_FENCE = (
    "```markdown\n"
    "# Objetivo tecnico\n\n"
    "Descreva o objetivo do sistema em uma frase.\n\n"
    "## Arquitetura geral\n\n"
    "Liste os componentes e como se comunicam.\n\n"
    "### Decisoes\n\n"
    "Justifique cada escolha relevante.\n\n"
    "#### Riscos\n\n"
    "Enumere os riscos conhecidos e mitigacoes.\n"
    "```"
)


def _filler(n: int) -> str:
    return ("prosa real do documento fora de qualquer fence. " * n).strip()


def test_iter_fenced_spans_basic_backtick_fence():
    text = f"antes\n\n{HLD_FENCE}\n\ndepois\n"
    spans = _iter_fenced_spans(text)
    assert len(spans) == 1
    start, end = spans[0]
    assert text[start:end].strip() == HLD_FENCE


def test_iter_fenced_spans_tilde_family_and_indent():
    text = "intro\n\n   ~~~~\ncorpo ``` nao fecha\n   ~~~~\n\nfim\n"
    spans = _iter_fenced_spans(text)
    assert len(spans) == 1
    assert "corpo" in text[spans[0][0]:spans[0][1]]


def test_iter_fenced_spans_shorter_marker_does_not_close():
    text = "````\ncorpo\n```\nainda dentro\n````\nfora\n"
    spans = _iter_fenced_spans(text)
    assert len(spans) == 1
    body = text[spans[0][0]:spans[0][1]]
    assert "ainda dentro" in body
    assert "fora" not in body


def test_info_string_does_not_close_outer_fence():
    """```json inside ```markdown is content, not a closing fence."""
    text = (
        "```markdown\n"
        "# Titulo interno\n\n"
        "```json\n"
        '{"a": 1}\n'
        "```\n"
    )
    spans = _iter_fenced_spans(text)
    # The ```json line has an info string, so it cannot close; the bare ``` does.
    assert len(spans) == 1
    assert '{"a": 1}' in text[spans[0][0]:spans[0][1]]
    assert _split_into_chapters(text, "md")[0]["title"] == "Documento completo"


def test_unclosed_fence_spans_to_eof_and_hides_headings():
    text = f"## Capitulo real\n\n{_filler(3)}\n\n```markdown\n# Nao e capitulo\n\n{_filler(2)}\n"
    spans = _iter_fenced_spans(text)
    assert len(spans) == 1
    assert spans[0][1] == len(text)

    chapters = _split_into_chapters(text, "md")
    assert [c["title"] for c in chapters] == ["Capitulo real"]
    assert "# Nao e capitulo" in chapters[0]["text"]


def test_headings_inside_fence_do_not_create_chapters():
    text = f"# Documento HLD\n\n{_filler(2)}\n\n{HLD_FENCE}\n"
    chapters = _split_into_chapters(text, "md")
    assert [c["title"] for c in chapters] == ["Documento HLD"]
    assert "#### Riscos" in chapters[0]["text"]


def test_headings_inside_fence_do_not_create_sections():
    chapter_text = f"{_filler(2)}\n\n{HLD_FENCE}"
    sections = _split_chapter_into_sections("Cap", chapter_text, min_section_chars=200)
    assert len(sections) == 1
    assert sections[0]["section_path"] == "Cap"
    assert "### Decisoes" in sections[0]["text"]


def test_real_headings_outside_fence_still_split():
    chapter_text = (
        f"### Secao verdadeira\n\n{_filler(3)}\n\n{HLD_FENCE}\n\n"
        f"### Outra secao\n\n{_filler(3)}"
    )
    sections = _split_chapter_into_sections("Cap", chapter_text, min_section_chars=50)
    paths = [s["section_path"] for s in sections]
    assert paths == ["Cap > Secao verdadeira", "Cap > Outra secao"]
    assert HLD_FENCE in sections[0]["text"]


def test_fence_is_never_cut_by_the_size_splitter():
    cfg = _cfg(chunk_size=200, chunk_overlap=20, min_section_chars=50)
    chapter = {
        "title": "Cap",
        "text": f"{_filler(6)}\n\n{HLD_FENCE}\n\n{_filler(6)}",
        "locator": "Cap",
    }
    pairs = _split_chapter_into_chunks(cfg, chapter)
    texts = [t for _, t in pairs]
    assert HLD_FENCE in texts                      # emitted whole, exactly once
    assert texts.count(HLD_FENCE) == 1
    assert all(path == "Cap" for path, _ in pairs)  # no path from fenced headings
    assert len(pairs) > 1                           # prose around it still splits


def test_oversized_fence_becomes_a_single_chunk():
    fence = "```text\n" + "linha de template do HLD\n" * 40 + "```"
    assert len(fence) > 200
    cfg = _cfg(chunk_size=200, chunk_overlap=20, min_section_chars=50)
    chapter = {"title": "Cap", "text": f"{_filler(6)}\n\n{fence}", "locator": "Cap"}
    texts = [t for _, t in _split_chapter_into_chunks(cfg, chapter)]
    assert fence in texts
    assert len(fence) > cfg.chunking.chunk_size  # documented oversized-chunk exception


def test_multiple_fences_are_independent_atoms():
    fence_a = "```python\n" + "print('a')\n" * 12 + "```"
    fence_b = "~~~sql\n" + "select 1;\n" * 12 + "~~~"
    cfg = _cfg(chunk_size=200, chunk_overlap=20, min_section_chars=50)
    chapter = {
        "title": "Cap",
        "text": f"{fence_a}\n\n{_filler(8)}\n\n{fence_b}",
        "locator": "Cap",
    }
    texts = [t for _, t in _split_chapter_into_chunks(cfg, chapter)]
    assert fence_a in texts
    assert fence_b in texts
    prose_pieces = [t for t in texts if t not in {fence_a, fence_b}]
    assert len(prose_pieces) > 1  # prose between the fences is still sliced


def test_long_prose_without_fence_still_splits():
    cfg = _cfg(chunk_size=200, chunk_overlap=20, min_section_chars=50)
    chapter = {"title": "Cap", "text": _filler(30), "locator": "Cap"}
    pairs = _split_chapter_into_chunks(cfg, chapter)
    assert len(pairs) > 1
    assert all(len(t) <= cfg.chunking.chunk_size for _, t in pairs)
