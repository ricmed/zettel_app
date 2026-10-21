"""Tests for the three-layer duplicate detection in the harvest phase."""

from __future__ import annotations

import pytest
from zettel.config import AppConfig, HarvestConfig
from zettel.harvester import (
    HarvestAborted,
)
from zettel.harvester import (
    find_exact_bibliographic_match as _find_exact_bibliographic_match,
)
from zettel.harvester import (
    find_semantic_duplicate_candidates as _find_semantic_duplicate_candidates,
)
from zettel.harvester import (
    find_title_author_candidates as _find_title_author_candidates,
)
from zettel.harvester import (
    normalize_doi as _normalize_doi,
)
from zettel.harvester import (
    normalize_isbn as _normalize_isbn,
)
from zettel.harvester import (
    normalize_title as _normalize_title,
)
from zettel.harvester import (
    resolve_duplicate_decision as _resolve_duplicate_decision,
)
from zettel.harvester import (
    resolve_title_author_decision as _resolve_title_author_decision,
)
from zettel.harvester import (
    sample_chunk_texts as _sample_chunk_texts,
)
from zettel.harvester.pipeline import HarvestDeclined, _process_file
from zettel.state import StateDB


class FakeVectorIndex:
    """Minimal stand-in for VectorIndex, avoiding real embeddings/network calls."""

    def __init__(self, chunk_matches: list[dict] | None = None):
        self._chunk_matches = chunk_matches or []
        self.upserted_sources: list[str] = []
        self.upserted_chunks: list[str] = []

    def find_similar_chunks(self, texts, n_results=3):
        return self._chunk_matches if texts else []

    def upsert_source(self, source_id, summary, metadata):
        self.upserted_sources.append(source_id)

    def upsert_chunk(self, chunk_id, text, metadata, **kwargs):
        self.upserted_chunks.append(chunk_id)

    def delete_chunks(self, chunk_ids):
        for cid in chunk_ids:
            if cid in self.upserted_chunks:
                self.upserted_chunks.remove(cid)

    def existing_ids(self, collection_name, ids):
        return {cid for cid in ids if cid in self.upserted_chunks}

    def chunk_ids_for_source(self, source_id):
        return set(self.upserted_chunks)


@pytest.fixture
def db(tmp_path):
    d = StateDB(tmp_path / "state.db")
    yield d
    d.close()


@pytest.fixture
def cfg(tmp_path):
    c = AppConfig(
        vault_path=tmp_path / "vault",
        harvest=HarvestConfig(
            # Layer 5 is off by default; this module is what tests it.
            semantic_duplicate_enabled=True,
            duplicate_chunk_threshold=0.85,
            biblio_llm_enabled=False,
        ),
    )
    (tmp_path / "vault" / "10_Sources").mkdir(parents=True, exist_ok=True)
    (tmp_path / "vault" / "20_Literature").mkdir(parents=True, exist_ok=True)
    return c


# ── _sample_chunk_texts ──────────────────────────────────────────────────


def test_sample_chunk_texts_returns_all_when_fewer_than_sample_size(cfg):
    chapters = [{"title": "Intro", "text": "Um texto curto para dividir em poucos chunks."}]
    sample = _sample_chunk_texts(cfg, chapters, sample_size=10)
    assert len(sample) >= 1


def test_sample_chunk_texts_distributes_evenly(cfg):
    long_text = " ".join(f"palavra{i}" for i in range(2000))
    chapters = [{"title": "Cap", "text": long_text}]
    sample = _sample_chunk_texts(cfg, chapters, sample_size=3)
    assert len(sample) == 3


# ── _find_semantic_duplicate_candidates ─────────────────────────────────


def test_find_semantic_duplicate_candidates_aggregates_by_source(db, cfg):
    db.upsert_source(
        source_id="@Existing2024",
        citekey="Existing2024",
        title="Fonte Existente",
        authors=[],
        year=2024,
        file_checksum="h",
        origin_path="/p",
        origin_type="pdf",
    )
    idx = FakeVectorIndex(
        chunk_matches=[
            {"id": "c1", "distance": 0.10, "metadata": {"source_id": "@Existing2024"}},
            {"id": "c2", "distance": 0.05, "metadata": {"source_id": "@Existing2024"}},
            {"id": "c3", "distance": 1.90, "metadata": {"source_id": "@Other2024"}},
        ]
    )
    chapters = [{"title": "Cap", "text": "Algum conteudo qualquer para amostragem de chunks."}]
    candidates = _find_semantic_duplicate_candidates(cfg, db, idx, chapters)

    assert len(candidates) == 1
    assert candidates[0]["source_id"] == "@Existing2024"
    assert candidates[0]["citekey"] == "Existing2024"
    assert candidates[0]["similarity"] > 0.85


def test_find_semantic_duplicate_candidates_below_threshold_ignored(db, cfg):
    idx = FakeVectorIndex(
        chunk_matches=[
            {"id": "c1", "distance": 1.5, "metadata": {"source_id": "@Distant2024"}},
        ]
    )
    chapters = [{"title": "Cap", "text": "Texto sem relacao com nada existente."}]
    candidates = _find_semantic_duplicate_candidates(cfg, db, idx, chapters)
    assert candidates == []


# ── _resolve_duplicate_decision (non-interactive) ───────────────────────


def test_resolve_duplicate_decision_noninteractive_uses_override(cfg, tmp_path):
    action = _resolve_duplicate_decision(
        tmp_path / "file.pdf",
        [{"citekey": "X", "title": "T", "similarity": 0.9}],
        interactive=False,
        duplicate_action="continue",
        cfg=cfg,
    )
    assert action == "continue"


def test_resolve_duplicate_decision_noninteractive_uses_config_default(cfg, tmp_path):
    cfg.harvest.non_interactive_duplicate_action = "skip"
    action = _resolve_duplicate_decision(
        tmp_path / "file.pdf",
        [{"citekey": "X", "title": "T", "similarity": 0.9}],
        interactive=False,
        duplicate_action=None,
        cfg=cfg,
    )
    assert action == "skip"


# ── _process_file: layered duplicate detection ──────────────────────────


def _write_pdf_like_md(path, text):
    path.write_text(text, encoding="utf-8")


def test_process_file_detects_renamed_copy_by_file_hash(db, cfg, tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    original = inbox / "original.md"
    copy = inbox / "copy_of_original.md"
    content = "# Titulo\n\nConteudo do artigo original."
    _write_pdf_like_md(original, content)
    _write_pdf_like_md(copy, content)

    idx = FakeVectorIndex()

    sid1, _stats1 = _process_file(
        cfg,
        db,
        idx,
        original,
        run_id=db.start_run("sig"),
        interactive=False,
        skip_biblio=True,
    )
    assert sid1 is not None

    run_id2 = db.start_run("sig2")
    with pytest.raises(HarvestDeclined) as declined:
        _process_file(
            cfg,
            db,
            idx,
            copy,
            run_id=run_id2,
            interactive=False,
            skip_biblio=True,
        )
    assert declined.value.reason == "duplicate_file"
    assert "camada 1" in declined.value.message

    copy_record = db.get_file(str(copy))
    assert copy_record["source_id"] == sid1

    last_run = db.get_run(run_id2)
    assert last_run["duplicate_file_count"] == 1


def test_process_file_detects_cross_format_content_duplicate(db, cfg, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    md_file = inbox / "article.md"
    pdf_like_file = inbox / "article_copy.txt"  # different bytes, same normalized text

    body = "# Artigo Importante\n\nEste e o conteudo do artigo, identico em ambos formatos."
    md_file.write_text(body, encoding="utf-8")
    # Different raw bytes (extra trailing whitespace/newlines) but same normalized text.
    pdf_like_file.write_text(body + "   \n\n\n", encoding="utf-8")

    idx = FakeVectorIndex()

    run_id1 = db.start_run("sig1")
    sid1, _ = _process_file(
        cfg,
        db,
        idx,
        md_file,
        run_id=run_id1,
        interactive=False,
        skip_biblio=True,
    )
    assert sid1 is not None

    run_id2 = db.start_run("sig2")
    with pytest.raises(HarvestDeclined) as declined:
        _process_file(
            cfg,
            db,
            idx,
            pdf_like_file,
            run_id=run_id2,
            interactive=False,
            skip_biblio=True,
        )
    assert declined.value.reason == "duplicate_content"
    assert "camada 2" in declined.value.message

    second_file_record = db.get_file(str(pdf_like_file))
    assert second_file_record["source_id"] == sid1

    run2 = db.get_run(run_id2)
    assert run2["duplicate_content_count"] == 1


def test_process_file_semantic_duplicate_skip(db, cfg, tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    new_file = inbox / "new_article.md"
    new_file.write_text(
        "# Outro Titulo\n\nTexto totalmente diferente do indexado.", encoding="utf-8"
    )

    db.upsert_source(
        source_id="@Similar2024",
        citekey="Similar2024",
        title="Fonte Similar",
        authors=[],
        year=2024,
        file_checksum="h",
        origin_path="/p",
        origin_type="pdf",
    )
    idx = FakeVectorIndex(
        chunk_matches=[
            {"id": "c1", "distance": 0.05, "metadata": {"source_id": "@Similar2024"}},
        ]
    )

    run_id = db.start_run("sig")
    with pytest.raises(HarvestDeclined) as declined:
        _process_file(
            cfg,
            db,
            idx,
            new_file,
            run_id=run_id,
            interactive=False,
            duplicate_action="skip",
            skip_biblio=True,
        )
    assert declined.value.reason == "duplicate_skipped"
    assert "camada 5" in declined.value.message
    run = db.get_run(run_id)
    assert run["duplicate_semantic_count"] == 1
    # No source should have been created for the skipped file.
    assert db.get_file(str(new_file)) is None


def test_process_file_semantic_duplicate_continue_creates_source(db, cfg, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    new_file = inbox / "new_article2.md"
    new_file.write_text(
        "# Outro Titulo 2\n\nTexto diferente mas sinalizado como suspeito.",
        encoding="utf-8",
    )

    db.upsert_source(
        source_id="@Similar2024b",
        citekey="Similar2024b",
        title="Fonte Similar 2",
        authors=[],
        year=2024,
        file_checksum="h",
        origin_path="/p",
        origin_type="pdf",
    )
    idx = FakeVectorIndex(
        chunk_matches=[
            {"id": "c1", "distance": 0.05, "metadata": {"source_id": "@Similar2024b"}},
        ]
    )

    run_id = db.start_run("sig")
    sid, stats = _process_file(
        cfg,
        db,
        idx,
        new_file,
        run_id=run_id,
        interactive=False,
        duplicate_action="continue",
        skip_biblio=True,
    )
    assert sid is not None
    assert stats.get("chunks", 0) >= 1
    run = db.get_run(run_id)
    assert run["duplicate_semantic_count"] == 1


def test_process_file_semantic_duplicate_abort_raises(db, cfg, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    new_file = inbox / "new_article3.md"
    new_file.write_text(
        "# Titulo 3\n\nConteudo suspeito de duplicidade semantica.", encoding="utf-8"
    )

    db.upsert_source(
        source_id="@Similar2024c",
        citekey="Similar2024c",
        title="Fonte Similar 3",
        authors=[],
        year=2024,
        file_checksum="h",
        origin_path="/p",
        origin_type="pdf",
    )
    idx = FakeVectorIndex(
        chunk_matches=[
            {"id": "c1", "distance": 0.05, "metadata": {"source_id": "@Similar2024c"}},
        ]
    )

    run_id = db.start_run("sig")
    with pytest.raises(HarvestAborted):
        _process_file(
            cfg,
            db,
            idx,
            new_file,
            run_id=run_id,
            interactive=False,
            duplicate_action="abort",
            skip_biblio=True,
        )


# ── Layers 3 & 4: bibliographic dedupe (pure SQLite, before any embedding) ──


def _seed_source(db, *, sid, citekey, title, authors, year, doi=None, isbn=None):
    db.upsert_source(
        source_id=sid,
        citekey=citekey,
        title=title,
        authors=authors,
        year=year,
        file_checksum=f"h-{citekey}",
        origin_path=f"/{citekey}.pdf",
        origin_type="pdf",
        doi=doi,
        isbn=isbn,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.1000/XyZ", "10.1000/xyz"),
        ("https://doi.org/10.1000/xyz", "10.1000/xyz"),
        ("doi: 10.1000/xyz", "10.1000/xyz"),
        ("nao-e-um-doi", ""),  # sem prefixo de registrante 10.
        (None, ""),
    ],
)
def test_normalize_doi(raw, expected):
    assert _normalize_doi(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("978-85-333-0227-3", "9788533302273"),
        ("0-306-40615-2", "0306406152"),
        ("12345", ""),  # comprimento implausivel
        (None, ""),
    ],
)
def test_normalize_isbn(raw, expected):
    assert _normalize_isbn(raw) == expected


def test_normalize_title_drops_subtitle_and_folds():
    """O subtitulo e onde a catalogacao mais diverge; o titulo principal e o estavel."""
    assert _normalize_title("Pensar, Rápido e Devagar: uma introdução") == "pensar rapido e devagar"
    assert _normalize_title("PENSAR, RAPIDO E DEVAGAR") == _normalize_title(
        "Pensar, Rápido e Devagar: outra edição"
    )


def test_exact_match_prefers_doi_over_isbn(db):
    _seed_source(db, sid="@A", citekey="A2024", title="T", authors=["X"], year=2024, doi="10.1/a")
    _seed_source(
        db, sid="@B", citekey="B2024", title="U", authors=["Y"], year=2024, isbn="9788533302273"
    )
    row, kind = _find_exact_bibliographic_match(db, doi="10.1/a", isbn="9788533302273")
    assert (row["source_id"], kind) == ("@A", "doi")


def test_exact_match_returns_none_without_keys(db):
    _seed_source(db, sid="@A", citekey="A2024", title="T", authors=["X"], year=2024)
    assert _find_exact_bibliographic_match(db, doi="", isbn="") is None


def test_title_author_needs_both_title_and_author(db):
    _seed_source(
        db,
        sid="@Kahneman2011",
        citekey="Kahneman2011",
        title="Rapido e Devagar",
        authors=["Daniel Kahneman"],
        year=2011,
    )
    # Mesmo titulo, outro autor -> nao e candidato.
    assert (
        _find_title_author_candidates(
            db, title="Rapido e Devagar", authors=["Outro Nome"], year=2011
        )
        == []
    )
    # Mesmo autor, outro titulo -> nao e candidato.
    assert (
        _find_title_author_candidates(
            db, title="Outra Obra", authors=["Daniel Kahneman"], year=2011
        )
        == []
    )


def test_title_author_matches_across_accents_and_initials(db):
    _seed_source(
        db,
        sid="@Kahneman2011",
        citekey="Kahneman2011",
        title="Rápido e Devagar: duas formas de pensar",
        authors=["Daniel Kahneman"],
        year=2011,
    )
    found = _find_title_author_candidates(
        db, title="Rapido e Devagar", authors=["D. Kahneman"], year=2011
    )
    assert [c["citekey"] for c in found] == ["Kahneman2011"]
    assert found[0]["same_year"] is True


def test_title_author_reports_a_different_year_instead_of_rejecting_it(db):
    """2a edicao vs 1a: plausivelmente a mesma obra, plausivelmente distintas.

    O ano e reportado para que o usuario decida, nunca usado para descartar.
    """
    _seed_source(
        db,
        sid="@Silva2018",
        citekey="Silva2018",
        title="Metodologia",
        authors=["Ana Silva"],
        year=2018,
    )
    found = _find_title_author_candidates(db, title="Metodologia", authors=["Ana Silva"], year=2023)
    assert len(found) == 1
    assert found[0]["same_year"] is False
    assert found[0]["year"] == 2018


def test_title_author_decision_defaults_to_new_source_without_a_tty(tmp_path):
    """Fundir e a direcao irreversivel; sem TTY, cria fonte nova (ADR-011)."""
    candidates = [
        {"citekey": "Silva2018", "title": "Metodologia", "year": 2018, "same_year": False}
    ]
    assert (
        _resolve_title_author_decision(tmp_path / "novo.pdf", candidates, interactive=False)
        == "continue"
    )


def test_process_file_reuses_source_on_identical_doi(db, cfg, tmp_path, monkeypatch):
    """DOI e identidade: reusa a fonte sem perguntar e sem gastar embedding."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    first = inbox / "artigo.md"
    second = inbox / "artigo-reexportado.md"
    first.write_text("# Artigo\n\nConteudo original do artigo.", encoding="utf-8")
    # Bytes e texto normalizado diferentes: as camadas 1 e 2 nao pegam.
    second.write_text("# Artigo\n\nConteudo reescrito apos OCR, bem diferente.", encoding="utf-8")

    from zettel.harvester import biblio_dedupe

    monkeypatch.setattr(biblio_dedupe, "normalize_doi", lambda raw: "10.1234/artigo")

    idx = FakeVectorIndex()
    run1 = db.start_run("sig1")
    sid1, _ = _process_file(cfg, db, idx, first, run_id=run1, interactive=False, skip_biblio=True)
    assert sid1 is not None
    assert db.get_source(sid1)["doi"] == "10.1234/artigo"

    run2 = db.start_run("sig2")
    with pytest.raises(HarvestDeclined) as declined:
        _process_file(cfg, db, idx, second, run_id=run2, interactive=False, skip_biblio=True)
    assert declined.value.reason == "duplicate_id"
    assert "camada 3, DOI" in declined.value.message
    assert db.get_file(str(second))["source_id"] == sid1
    assert db.get_run(run2)["duplicate_biblio_count"] == 1


# ── Layer 5 flag: write and read are gated together, never separately ──


def test_layer5_off_skips_chunk_embeddings_and_the_check(db, cfg, tmp_path, monkeypatch):
    """The flag governs both sides at once.

    Gating only the write would leave the check querying an index the pipeline
    stopped populating — a silent false negative, not an error.
    """
    cfg.harvest.semantic_duplicate_enabled = False
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    f = inbox / "artigo.md"
    f.write_text(
        "# Artigo\n\n" + ("conteudo suficiente para gerar chunks. " * 60), encoding="utf-8"
    )

    idx = FakeVectorIndex()
    monkeypatch.setattr(
        idx,
        "find_similar_chunks",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("camada 5 nao deveria consultar")),
    )

    run_id = db.start_run("sig")
    sid, _ = _process_file(cfg, db, idx, f, run_id=run_id, interactive=False, skip_biblio=True)

    assert sid is not None, "o arquivo deve ser ingerido normalmente"
    # SQLite keeps every chunk; only the vector index is skipped.
    assert db.get_chunks_for_source(sid), "chunks devem existir no SQLite"
    assert idx.upserted_chunks == [], "nenhum chunk deve ser embedado"
    assert db.get_run(run_id)["duplicate_semantic_count"] == 0


def test_layer5_on_still_embeds_and_checks(db, cfg, tmp_path):
    """The same file with the flag on: chunks are embedded and the check runs."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    f = inbox / "artigo.md"
    f.write_text(
        "# Artigo\n\n" + ("conteudo suficiente para gerar chunks. " * 60), encoding="utf-8"
    )

    idx = FakeVectorIndex()
    run_id = db.start_run("sig")
    sid, _ = _process_file(cfg, db, idx, f, run_id=run_id, interactive=False, skip_biblio=True)

    assert sid is not None
    assert idx.upserted_chunks, "com a camada 5 ligada, os chunks alimentam o indice-alvo"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10.1000/xyz.", "10.1000/xyz"),
        ("10.1000/xyz)", "10.1000/xyz"),
        ("10.1000/xyz;", "10.1000/xyz"),
        ("10.1/curto", ""),
    ],
)
def test_normalize_doi_strips_trailing_punctuation_and_rejects_junk(raw, expected):
    assert _normalize_doi(raw) == expected


def test_normalize_isbn_rejects_a_bad_check_digit():
    assert _normalize_isbn("978-85-333-0227-4") == ""
    assert _normalize_isbn("0-306-40615-3") == ""
    assert _normalize_isbn("0-8044-2957-X") == "080442957X"


def test_process_file_reuses_source_on_doi_with_trailing_punctuation(db, cfg, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    first = inbox / "com-doi.md"
    second = inbox / "doi-com-ponto.md"
    first.write_text(
        "---\ntitle: Artigo Um\nauthor: Ana Silva\nyear: 2020\ndoi: 10.1000/artigo\n---\n\n"
        "Corpo original do artigo.",
        encoding="utf-8",
    )
    second.write_text(
        "---\ntitle: Outro texto\nauthor: Outra Pessoa\nyear: 2021\ndoi: 10.1000/artigo.\n---\n\n"
        "Corpo completamente diferente, so o DOI coincide.",
        encoding="utf-8",
    )
    idx = FakeVectorIndex()
    sid1, _ = _process_file(
        cfg, db, idx, first, run_id=db.start_run("s1"), interactive=False, skip_biblio=True
    )
    with pytest.raises(HarvestDeclined) as declined:
        _process_file(
            cfg, db, idx, second, run_id=db.start_run("s2"), interactive=False, skip_biblio=True
        )
    assert declined.value.reason == "duplicate_id"
    assert "camada 3, DOI" in declined.value.message
    assert sid1 is not None
    assert db.get_file(str(second))["source_id"] == sid1


def test_invalid_isbn_does_not_merge(db, cfg, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    first = inbox / "livro.md"
    second = inbox / "outro.md"
    first.write_text(
        "---\ntitle: Obra A\nauthor: Ana Silva\nyear: 2020\nisbn: 978-85-333-0227-4\n"
        "document_type: livro\n---\n\nCorpo do primeiro livro, bem longo.",
        encoding="utf-8",
    )
    second.write_text(
        "---\ntitle: Obra B\nauthor: Bruno Costa\nyear: 2021\nisbn: 9788533302274\n"
        "document_type: livro\n---\n\nCorpo do segundo livro, diferente.",
        encoding="utf-8",
    )
    idx = FakeVectorIndex()
    sid1, _ = _process_file(
        cfg, db, idx, first, run_id=db.start_run("s1"), interactive=False, skip_biblio=True
    )
    sid2, _ = _process_file(
        cfg, db, idx, second, run_id=db.start_run("s2"), interactive=False, skip_biblio=True
    )
    assert sid1 and sid2 and sid1 != sid2


def test_same_isbn_and_title_still_merges_without_asking(db, cfg, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    isbn = "978-85-333-0227-3"
    first = inbox / "edicao.md"
    second = inbox / "reedicao.md"
    first.write_text(
        f"---\ntitle: Manual de Metodo\nauthor: Ana Silva\nyear: 2020\nisbn: {isbn}\n"
        "document_type: livro\n---\n\nPrimeira extracao do manual.",
        encoding="utf-8",
    )
    second.write_text(
        f"---\ntitle: Manual de Metodo\nauthor: Ana Silva\nyear: 2020\nisbn: {isbn}\n"
        "document_type: livro\n---\n\nSegunda extracao, texto outro.",
        encoding="utf-8",
    )
    idx = FakeVectorIndex()
    sid1, _ = _process_file(
        cfg, db, idx, first, run_id=db.start_run("s1"), interactive=False, skip_biblio=True
    )
    with pytest.raises(HarvestDeclined) as declined:
        _process_file(
            cfg, db, idx, second, run_id=db.start_run("s2"), interactive=False, skip_biblio=True
        )
    assert declined.value.reason == "duplicate_id"
    assert "camada 3, ISBN" in declined.value.message
    assert sid1 is not None
    assert db.get_file(str(second))["source_id"] == sid1


def test_chapter_isbn_does_not_merge_without_a_tty(db, cfg, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    isbn = "978-85-333-0227-3"
    book = inbox / "livro-inteiro.md"
    chapter = inbox / "capitulo.md"
    book.write_text(
        f"---\ntitle: Manual de Metodo\nauthor: Ana Silva\nyear: 2020\nisbn: {isbn}\n"
        "document_type: livro\n---\n\nO livro inteiro.",
        encoding="utf-8",
    )
    chapter.write_text(
        f"---\ntitle: So o capitulo tres\nauthor: Ana Silva\nyear: 2020\nisbn: {isbn}\n"
        "document_type: capitulo_livro\n---\n\nO capitulo avulso, texto proprio.",
        encoding="utf-8",
    )
    idx = FakeVectorIndex()
    sid_book, _ = _process_file(
        cfg, db, idx, book, run_id=db.start_run("s1"), interactive=False, skip_biblio=True
    )
    sid_chapter, _ = _process_file(
        cfg, db, idx, chapter, run_id=db.start_run("s2"), interactive=False, skip_biblio=True
    )
    assert sid_book and sid_chapter and sid_book != sid_chapter


def test_chapter_isbn_asks_when_interactive(tmp_path, monkeypatch):
    from zettel.harvester.biblio_dedupe import resolve_isbn_decision

    asked = {}

    def _ask(*args, **kwargs):
        asked["choices"] = kwargs.get("choices")
        return "pular"

    monkeypatch.setattr("rich.prompt.Prompt.ask", _ask)
    decision = resolve_isbn_decision(
        tmp_path / "cap.md",
        {"citekey": "Silva2020", "title": "Manual", "year": 2020},
        interactive=True,
    )
    assert asked.get("choices") == ["pular", "continuar", "abortar"]
    assert decision == "skip"


def test_citekey_drops_path_separators_and_keeps_accents(db):
    from zettel.harvester.citekey import generate_citekey

    hostile = generate_citekey(db, ["John ../../evil"], 2020, "Some Title")
    assert "/" not in hostile and "\\" not in hostile and ".." not in hostile
    assert hostile.startswith("evil2020")
    empty = generate_citekey(db, ["../.."], 2020, "Some Title")
    assert empty.startswith("2020")
    accented = generate_citekey(db, ["Ana Gonçalves"], 2021, "Análise")
    assert accented.startswith("Gonçalves2021")


def test_citekey_disambiguation_past_z_uses_aa(db, monkeypatch):
    from zettel.harvester.citekey import generate_citekey

    calls = {"n": 0}

    def _taken(citekey):
        calls["n"] += 1
        if calls["n"] <= 27:
            return {"citekey": citekey}
        return None

    monkeypatch.setattr(db, "get_source_by_citekey", _taken)
    key = generate_citekey(db, ["Ann Silva"], 2020, "Obra")
    assert key.endswith("aa")
    assert "{" not in key


def _two_chapter_book(second: str) -> str:
    stable = "Conteudo estavel do primeiro capitulo para o chunk. " * 8
    return f"# Cap A\n\n{stable}\n\n# Cap B\n\n{second}\n"


def _harvest_markdown(cfg, db, path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return _process_file(
        cfg,
        db,
        FakeVectorIndex(),
        path,
        run_id=db.start_run("mod"),
        interactive=False,
        skip_biblio=True,
    )


def test_modified_file_skip_keeps_the_path_on_the_same_source(cfg, db, tmp_path, caplog):
    path = tmp_path / "inbox" / "livro.md"
    sid, _stats = _harvest_markdown(
        cfg, db, path, _two_chapter_book("Texto original do segundo capitulo. " * 8)
    )
    assert sid
    before = {row["chunk_id"] for row in db.get_chunks_for_source(sid)}
    with caplog.at_level("WARNING"), pytest.raises(HarvestDeclined) as declined:
        _harvest_markdown(
            cfg, db, path, _two_chapter_book("Texto revisado do segundo capitulo. " * 8)
        )
    assert declined.value.reason == "modified_kept"
    assert "mudou" in declined.value.message
    assert db.get_file(str(path))["source_id"] == sid
    assert {row["chunk_id"] for row in db.get_chunks_for_source(sid)} == before
    assert len(db.list_sources()) == 1
    assert "acao='skip'" in caplog.text


def test_modified_file_update_keeps_unchanged_chunks_and_pends_new_ones(cfg, db, tmp_path):
    cfg.harvest.modified_file_action = "update"
    path = tmp_path / "inbox" / "livro.md"
    sid, _stats = _harvest_markdown(
        cfg, db, path, _two_chapter_book("Texto original do segundo capitulo. " * 8)
    )
    stable = next(row for row in db.get_chunks_for_source(sid) if "estavel" in row["text"])
    db.update_chunk_status(stable["chunk_id"], "persisted")
    again, _stats = _harvest_markdown(
        cfg, db, path, _two_chapter_book("Texto revisado do segundo capitulo. " * 8)
    )
    assert again == sid
    assert len(db.list_sources()) == 1
    assert db.get_file(str(path))["source_id"] == sid
    kept = db.get_chunk(stable["chunk_id"])
    assert kept is not None
    assert kept["status"] == "persisted"
    assert "estavel" in kept["text"]
    revised = [row for row in db.get_chunks_for_source(sid) if "revisado" in row["text"]]
    assert revised
    assert all(row["status"] == "pending" for row in revised)


def test_modified_file_new_moves_the_path_and_keeps_the_old_source(cfg, db, tmp_path):
    cfg.harvest.modified_file_action = "new"
    path = tmp_path / "inbox" / "livro.md"
    sid, _stats = _harvest_markdown(
        cfg, db, path, _two_chapter_book("Texto original do segundo capitulo. " * 8)
    )
    again, _stats = _harvest_markdown(
        cfg, db, path, _two_chapter_book("Texto revisado do segundo capitulo. " * 8)
    )
    assert again and again != sid
    assert db.get_source(sid) is not None
    assert db.get_file(str(path))["source_id"] == again


def test_update_does_not_treat_the_sources_own_doi_as_a_duplicate(cfg, db, tmp_path):
    cfg.harvest.modified_file_action = "update"
    path = tmp_path / "inbox" / "obra.md"
    body = (
        "---\n"
        "title: Obra\n"
        "author: Ana Silva\n"
        "year: 2020\n"
        "document_type: livro\n"
        "place: Sao Paulo\n"
        "publisher: Editora\n"
        "doi: 10.1000/xyz123\n"
        "---\n\n"
        "# Cap\n\n" + ("Texto original longo o bastante para um chunk. " * 8)
    )
    sid, _stats = _harvest_markdown(cfg, db, path, body)
    revised = body.replace("original", "revisado")
    again, _stats = _harvest_markdown(cfg, db, path, revised)
    assert again == sid
    assert any("revisado" in row["text"] for row in db.get_chunks_for_source(sid))


def test_update_rewrites_bibliography_when_only_frontmatter_changes(cfg, db, tmp_path):
    cfg.harvest.modified_file_action = "update"
    path = tmp_path / "inbox" / "obra.md"
    prose = "Texto estavel do corpo para o chunk. " * 8
    body = (
        "---\n"
        "title: Obra\n"
        "author: Ana Silva\n"
        "year: 2020\n"
        "document_type: livro\n"
        "place: Sao Paulo\n"
        "publisher: Editora\n"
        "doi: 10.1000/xyz123\n"
        "---\n\n"
        f"# Cap\n\n{prose}"
    )
    sid, _stats = _harvest_markdown(cfg, db, path, body)
    before = {row["chunk_id"] for row in db.get_chunks_for_source(sid)}
    citekey = db.get_source(sid)["citekey"]
    revised = body.replace("author: Ana Silva", "author: Bruno Costa").replace(
        "doi: 10.1000/xyz123", "doi: 10.1000/abc999"
    )
    again, _stats = _harvest_markdown(cfg, db, path, revised)
    src = db.get_source(sid)
    assert again == sid
    assert src["citekey"] == citekey
    assert "Bruno Costa" in src["authors"]
    assert "COSTA" in (src["abnt_reference"] or "")
    assert src["doi"] == "10.1000/abc999"
    assert {row["chunk_id"] for row in db.get_chunks_for_source(sid)} == before
