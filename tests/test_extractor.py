"""Tests for extractor candidate filtering."""

import json

import pytest
from zettel.config import AppConfig, ExtractionConfig
from zettel.extractor import _filter_candidates, _score_review_confidence
from zettel.schemas import LiteratureChunkOutput, PermanentNoteCandidate


def _make_candidate(**overrides) -> PermanentNoteCandidate:
    """Helper to build a valid candidate with sensible defaults."""
    defaults = {
        "thesis": "Gradient descent converge mais rapido com learning rate adaptativo",
        "definition": (
            "O algoritmo de gradient descent ajusta os pesos do modelo iterativamente "
            "na direcao oposta ao gradiente da funcao de perda, e taxas adaptativas "
            "como Adam aceleram a convergencia em superficies nao convexas"
        ),
        "limits": "Pode divergir com learning rates muito altos",
        "anchor_quote": "adaptive learning rates converge faster in practice than fixed schedules",
        "source_locator": "p.42",
        "tags": ["otimizacao", "deep-learning"],
        "relevance_score": 4,
    }
    defaults.update(overrides)
    return PermanentNoteCandidate(**defaults)


def _make_config(**overrides) -> AppConfig:
    """Helper to build an AppConfig with custom extraction settings.

    `verify_anchor_quote` defaults to False here so tests unrelated to
    anchor grounding don't need to supply a matching `chunk_text`.
    """
    ext_kwargs = {
        "min_relevance_score": 3,
        "min_thesis_words": 5,
        "require_anchor_quote": True,
        "min_definition_words": 10,
        "verify_anchor_quote": False,
        "anchor_quote_min_ratio": 0.85,
        "anchor_quote_min_words": 10,
        "anchor_quote_max_words": 25,
    }
    ext_kwargs.update(overrides)
    return AppConfig(extraction=ExtractionConfig(**ext_kwargs))


def test_filter_candidates_force_skips_selectivity():
    """A forced extract keeps a short, low-relevance candidate the model marked rejected."""
    cfg = _make_config(min_relevance_score=3, min_thesis_words=5, min_definition_words=10)
    candidate = _make_candidate(
        relevance_score=1,
        thesis="Curta",
        definition="curta",
        chunk_status="rejected",
        rejection_reason="trivial",
        rejection_category="trivial",
    )
    approved, rejected = _filter_candidates([candidate], cfg, force=True)
    assert approved == [candidate]
    assert rejected == []
    plain_approved, plain_rejected = _filter_candidates([candidate], cfg)
    assert plain_approved == []
    assert plain_rejected


def test_filter_candidates_force_still_drops_an_ungrounded_quote():
    cfg = _make_config(verify_anchor_quote=True)
    quote = "uma citacao que nao esta no chunk de forma alguma mesmo"
    candidate = _make_candidate(anchor_quote=quote, relevance_score=1, thesis="Curta")
    approved, rejected = _filter_candidates(
        [candidate],
        cfg,
        "outro texto completamente diferente sobre outro assunto qualquer",
        force=True,
    )
    assert approved == []
    assert "anchor_quote" in rejected[0][1]


def test_filter_candidates_by_relevance():
    """Candidates below relevance threshold are rejected."""
    cfg = _make_config(min_relevance_score=3)
    candidates = [
        _make_candidate(relevance_score=2),
        _make_candidate(relevance_score=3),
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 1
    assert len(rejected) == 1
    cand, reason = rejected[0]
    assert cand.relevance_score == 2
    assert "relevance_score" in reason


def test_filter_candidates_by_thesis_length():
    """Candidates with thesis shorter than min_thesis_words are rejected."""
    cfg = _make_config(min_thesis_words=5)
    candidates = [
        _make_candidate(thesis="Curta demais"),  # 2 words
        _make_candidate(thesis="Esta tese tem palavras suficientes para passar"),
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 1
    assert len(rejected) == 1


def test_filter_candidates_by_definition_length():
    """Candidates with definition shorter than min_definition_words are rejected."""
    cfg = _make_config(min_definition_words=10)
    candidates = [
        _make_candidate(definition="Muito curta"),  # 2 words
        _make_candidate(),  # default definition has >10 words
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 1
    assert len(rejected) == 1


def test_filter_candidates_require_anchor():
    """Candidates without anchor_quote are rejected when required."""
    cfg = _make_config(require_anchor_quote=True)
    candidates = [
        _make_candidate(anchor_quote=""),
        _make_candidate(anchor_quote="   "),  # whitespace only
        _make_candidate(),  # default has anchor_quote
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 1
    assert len(rejected) == 2


def test_filter_candidates_anchor_not_required():
    """Candidates without anchor_quote pass when not required."""
    cfg = _make_config(require_anchor_quote=False)
    candidates = [
        _make_candidate(anchor_quote=""),
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 1
    assert len(rejected) == 0


def test_filter_candidates_all_pass():
    """Valid candidates pass all checks."""
    cfg = _make_config()
    candidates = [
        _make_candidate(relevance_score=4),
        _make_candidate(relevance_score=5),
    ]
    approved, rejected = _filter_candidates(candidates, cfg)
    assert len(approved) == 2
    assert len(rejected) == 0


# ── verify_anchor_quote (grounding + word range) ────────────────────────

_CHUNK_TEXT = (
    "Estudos recentes mostram que adaptive learning rates converge faster "
    "in practice than fixed schedules quando aplicados a redes profundas, "
    "especialmente em cenarios com ruido alto nos gradientes."
)


def test_filter_candidates_verified_quote_verbatim_in_chunk_passes():
    cfg = _make_config(verify_anchor_quote=True)
    candidates = [_make_candidate()]  # default anchor_quote is verbatim in _CHUNK_TEXT
    approved, rejected = _filter_candidates(candidates, cfg, _CHUNK_TEXT)
    assert len(approved) == 1
    assert len(rejected) == 0


def test_filter_candidates_verified_quote_absent_from_chunk_rejected():
    cfg = _make_config(verify_anchor_quote=True)
    candidates = [_make_candidate(anchor_quote="isso nao aparece em lugar nenhum do texto")]
    approved, rejected = _filter_candidates(candidates, cfg, _CHUNK_TEXT)
    assert len(approved) == 0
    assert len(rejected) == 1


def test_filter_candidates_verified_quote_with_editorial_ellipsis_passes():
    cfg = _make_config(verify_anchor_quote=True, anchor_quote_min_words=5)
    quote = "adaptive learning rates [...] than fixed schedules"
    candidates = [_make_candidate(anchor_quote=quote)]
    approved, rejected = _filter_candidates(candidates, cfg, _CHUNK_TEXT)
    assert len(approved) == 1
    assert len(rejected) == 0


def test_filter_candidates_verified_quote_paraphrase_rejected():
    cfg = _make_config(verify_anchor_quote=True)
    paraphrase = (
        "taxas de aprendizado adaptativas convergem mais rapido na pratica que agendas fixas"
    )
    candidates = [_make_candidate(anchor_quote=paraphrase)]
    approved, rejected = _filter_candidates(candidates, cfg, _CHUNK_TEXT)
    assert len(approved) == 0
    assert len(rejected) == 1


def test_filter_candidates_verified_quote_too_short_rejected():
    cfg = _make_config(verify_anchor_quote=True)
    candidates = [_make_candidate(anchor_quote="adaptive learning rates converge")]  # 4 words
    approved, rejected = _filter_candidates(candidates, cfg, _CHUNK_TEXT)
    assert len(approved) == 0
    assert len(rejected) == 1


def test_filter_candidates_paragraph_sized_literal_quote_is_trimmed():
    """Past the tolerated ceiling a grounded quote is cut to 25 words, not dropped."""
    cfg = _make_config(verify_anchor_quote=True)
    long_quote = " ".join(["palavra"] * 40)
    candidates = [_make_candidate(anchor_quote=long_quote)]
    approved, rejected = _filter_candidates(candidates, cfg, "palavra " * 50)
    assert not rejected
    assert len(approved[0].anchor_quote.split()) == 25


def test_filter_candidates_quote_slightly_over_the_ceiling_is_kept():
    """Two words over the prompt's 25 must not cost the whole candidate (#153).

    The model overshoots the count by small margins and always upward; the word
    range is a proxy for the property `quote_is_grounded` tests directly.
    """
    cfg = _make_config(verify_anchor_quote=True)
    chunk = "prefixo " + " ".join(f"palavra{i}" for i in range(27)) + " sufixo"
    quote = " ".join(f"palavra{i}" for i in range(27))  # 27 words, ceiling is 25
    approved, rejected = _filter_candidates([_make_candidate(anchor_quote=quote)], cfg, chunk)
    assert len(approved) == 1
    assert len(rejected) == 0


def test_filter_candidates_over_ceiling_quote_still_must_be_grounded():
    """The tolerance relaxes the count, never the grounding check."""
    cfg = _make_config(verify_anchor_quote=True)
    paraphrase = " ".join(f"inventado{i}" for i in range(27))
    approved, rejected = _filter_candidates(
        [_make_candidate(anchor_quote=paraphrase)], cfg, _CHUNK_TEXT
    )
    assert len(approved) == 0
    assert "nao encontrada" in rejected[0][1]


def test_filter_candidates_tolerance_of_one_trims_a_grounded_overshoot():
    """tolerance 1.0 still marks 26+ words as over the ceiling, but a literal quote is cut."""
    cfg = _make_config(verify_anchor_quote=True, anchor_quote_max_words_tolerance=1.0)
    chunk = "prefixo " + " ".join(f"palavra{i}" for i in range(27)) + " sufixo"
    quote = " ".join(f"palavra{i}" for i in range(27))
    approved, rejected = _filter_candidates([_make_candidate(anchor_quote=quote)], cfg, chunk)
    assert len(rejected) == 0
    assert len(approved[0].anchor_quote.split()) == 25


def test_filter_candidates_short_quote_reason_names_the_floor():
    """Floor and ceiling are separate rules now, so the reason must say which."""
    cfg = _make_config(verify_anchor_quote=True)
    approved, rejected = _filter_candidates(
        [_make_candidate(anchor_quote="adaptive learning rates converge")], cfg, _CHUNK_TEXT
    )
    assert not approved
    assert "< 10" in rejected[0][1]


_CODE_CHUNK = (
    "### 4.10 Prompt Chaining\n\n"
    "```python\n"
    "# Modelo diferente para cada etapa: custo-eficiência por complexidade\n"
    'llm1 = ChatOpenAI(model="gpt-3.5-turbo", temperature=0)  # Extração simples\n'
    "```\n\n"
    "Texto em prosa com poucas palavras aqui."
)
_COMMENT_ANCHOR = "Modelo diferente para cada etapa: custo-eficiência por complexidade"


def test_short_anchor_inside_a_code_fence_is_judged_as_part_of_the_block():
    """Issue #204: a code comment is not a prose sentence; the floor does not apply."""
    cfg = _make_config(verify_anchor_quote=True)
    for force in (False, True):
        approved, rejected = _filter_candidates(
            [_make_candidate(anchor_quote=_COMMENT_ANCHOR)], cfg, _CODE_CHUNK, force=force
        )
        assert len(approved) == 1, rejected


def test_short_anchor_in_prose_still_faces_the_floor():
    cfg = _make_config(verify_anchor_quote=True)
    approved, rejected = _filter_candidates(
        [_make_candidate(anchor_quote="Texto em prosa com poucas palavras aqui")],
        cfg,
        _CODE_CHUNK,
    )
    assert not approved
    assert "< 10" in rejected[0][1]


def test_anchor_not_in_the_fence_is_not_exempted_by_a_fence_elsewhere():
    """A fabricated short quote stays rejected even when the chunk has a fence."""
    cfg = _make_config(verify_anchor_quote=True)
    approved, rejected = _filter_candidates(
        [_make_candidate(anchor_quote="frase inventada que nao existe")], cfg, _CODE_CHUNK
    )
    assert not approved
    assert "< 10" in rejected[0][1]


def test_long_anchor_inside_a_fence_is_trimmed_not_dropped():
    """A literal fence quote past the ceiling is cut; the floor exemption is unrelated."""
    long_comment = " ".join(f"palavra{i}" for i in range(60))
    chunk = f"```python\n# {long_comment}\nx = 1\n```"
    cfg = _make_config(verify_anchor_quote=True)
    approved, rejected = _filter_candidates(
        [_make_candidate(anchor_quote=long_comment)], cfg, chunk
    )
    assert not rejected
    assert len(approved[0].anchor_quote.split()) == 25


def test_literal_anchor_over_the_ceiling_is_trimmed_toward_the_thesis():
    """45 literal words stay, cut to 25, and the window follows the thesis (#253)."""
    from zettel.extractor import anchor_trim_original
    from zettel.hashing import quote_is_grounded

    cfg = _make_config(verify_anchor_quote=True)
    head = [f"filler{i}" for i in range(30)]
    focus = ["convergencia", "adaptativa", "gradiente"]
    tail = [f"resto{i}" for i in range(12)]
    quote = " ".join(head + focus + tail)
    assert len(quote.split()) == 45
    chunk = f"Antes. {quote} Depois."
    thesis = "A convergencia adaptativa do gradiente depende do passo"
    approved, rejected = _filter_candidates(
        [_make_candidate(anchor_quote=quote, thesis=thesis)], cfg, chunk
    )
    assert not rejected
    kept = approved[0].anchor_quote
    assert len(kept.split()) == 25
    assert "convergencia" in kept
    assert "filler0" not in kept.split()
    assert quote_is_grounded(kept, chunk, cfg.extraction.anchor_quote_min_ratio)
    assert anchor_trim_original(approved[0]) == quote


def test_ungrounded_anchor_over_the_ceiling_is_still_dropped():
    cfg = _make_config(verify_anchor_quote=True)
    quote = " ".join(f"inventada{i}" for i in range(45))
    approved, rejected = _filter_candidates([_make_candidate(anchor_quote=quote)], cfg, _CHUNK_TEXT)
    assert not approved
    assert "nao encontrada" in rejected[0][1]
    assert rejected[0][0].anchor_quote == quote


def test_forced_long_literal_anchor_becomes_a_draft_candidate():
    cfg = _make_config(verify_anchor_quote=True)
    quote = " ".join(f"palavra{i}" for i in range(45))
    chunk = f"inicio {quote} fim"
    approved, rejected = _filter_candidates(
        [_make_candidate(anchor_quote=quote, relevance_score=2)],
        cfg,
        chunk,
        force=True,
    )
    assert not rejected
    assert len(approved[0].anchor_quote.split()) == 25


def test_concept_id_is_scoped_to_the_chunk():
    from zettel.extractor import _compute_concept_id

    cand = _make_candidate()
    first = _compute_concept_id("@S", "@S::ch000::a", cand)
    second = _compute_concept_id("@S", "@S::ch000::b", cand)
    assert first != second
    assert first.startswith("@S::ch000::a::concept::")
    assert second.startswith("@S::ch000::b::concept::")


def test_trimmed_anchor_page_stays_inside_the_original_span():
    """A prefix window keeps the start page; it cannot keep both ends of a cross-page quote."""
    from zettel.extractor import _trim_anchor_quote
    from zettel.paging import locate_quote_pages, render_page_marked_markdown

    page_a = " ".join(f"alfa{i}" for i in range(30))
    page_b = " ".join(f"beta{i}" for i in range(15))
    full = f"{page_a} {page_b}"
    extracted = render_page_marked_markdown([(10, page_a), (11, page_b)])
    full_pages = locate_quote_pages(extracted, full, 10)
    trimmed = _trim_anchor_quote(full, "alfa0 alfa1 alfa2", 25)
    trim_pages = locate_quote_pages(extracted, trimmed, 10)
    assert full_pages == (10, 11)
    assert trim_pages is not None
    assert full_pages[0] <= trim_pages[0] <= trim_pages[1] <= full_pages[1]
    assert trim_pages[0] == 10


def test_filter_candidates_verify_anchor_quote_false_restores_old_behavior():
    """With verify_anchor_quote off, only the empty-string check still runs."""
    cfg = _make_config(verify_anchor_quote=False)
    candidates = [_make_candidate(anchor_quote="isso nao aparece em lugar nenhum do texto")]
    approved, rejected = _filter_candidates(candidates, cfg, _CHUNK_TEXT)
    assert len(approved) == 1
    assert len(rejected) == 0


def test_write_literature_draft_records_llm_model(tmp_path):
    """Draft frontmatter gets llm_model from the caller, not a missing spec."""
    from zettel.extractor import _write_literature_draft
    from zettel.schemas import LiteratureChunkOutput
    from zettel.state import StateDB
    from zettel.vault import parse_frontmatter

    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        cache_path=tmp_path / "cache",
        state_db_path=tmp_path / "state.db",
        chroma_path=tmp_path / "chroma",
        prompts_path=tmp_path / "prompts",
    )
    (cfg.vault_path / "00_Inbox" / "Review").mkdir(parents=True)
    db = StateDB(cfg.state_db_path)
    db.upsert_source(
        "@Book2024",
        "Book2024",
        "Livro",
        ["Autor"],
        2024,
        "h",
        "/x.pdf",
        "pdf",
    )
    db.upsert_chapter("@Book2024::ch000", "@Book2024", "Ch1", "chh")
    db.upsert_chunk(
        "@Book2024::ch000::abc",
        "@Book2024",
        "@Book2024::ch000",
        "texto do chunk com conteudo suficiente",
        "ck",
        chunk_index=0,
        page_in_file=1,
        page_in_book=145,
        status="pending",
    )
    chunk_row = db.get_chunk("@Book2024::ch000::abc")
    output = LiteratureChunkOutput(
        chunk_status="accepted",
        rejection_reason="",
        rejection_category="",
        summary="Resumo do trecho",
        key_concepts=["conceito"],
        candidates=[],
    )
    path = _write_literature_draft(
        cfg,
        db,
        chunk_row,
        output,
        "01HTESTLITID00000000000000",
        0.8,
        12,
        candidates=[],
        llm_model="gpt-4o-mini",
    )
    assert path is not None and path.is_file()
    meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert meta["llm_model"] == "gpt-4o-mini"
    db.close()


def test_process_chunk_payload_carries_section_language_and_domain(tmp_path, monkeypatch):
    """Prompt 1 gets the section path, the formatted locator, and config identity.

    `section_path` used to be sent as `chapter_title` filled with `locator`, so the
    model saw the citation string twice and never the heading trail.
    """
    import json
    from pathlib import Path

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts
    from zettel.state import StateDB

    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        cache_path=tmp_path / "cache",
        state_db_path=tmp_path / "state.db",
        chroma_path=tmp_path / "chroma",
        prompts_path=Path(__file__).resolve().parents[1] / "prompts",
    )
    cfg.language = "pt-BR"
    cfg.domain.name = "Ciencia de Dados"
    (cfg.vault_path / "00_Inbox" / "Review").mkdir(parents=True)

    db = StateDB(cfg.state_db_path)
    db.upsert_source("@Book2024", "Book2024", "Livro", ["Autor"], 2024, "h", "/x.pdf", "pdf")
    db.upsert_chapter("@Book2024::ch000", "@Book2024", "Ch1", "chh")
    db.upsert_chunk(
        "@Book2024::ch000::abc",
        "@Book2024",
        "@Book2024::ch000",
        "texto do chunk com conteudo suficiente",
        "ck",
        locator="Ch1",
        section_path="3 Retrieval > 3.2 Reranking",
        chunk_index=0,
        page_in_file=12,
        page_in_book=145,
        status="pending",
    )
    chunk_row = db.get_chunk("@Book2024::ch000::abc")

    captured: dict[str, str] = {}

    def fake_call_llm(llm, user, system=None, **kwargs):
        captured["user"] = user
        captured["system"] = system or ""
        return json.dumps(
            {
                "chunk_status": "rejected",
                "rejection_reason": "sem conceito",
                "rejection_category": "narrative",
                "summary": "Trecho de transicao.",
                "key_concepts": [],
                "candidates": [],
            }
        )

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)

    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")
    db.close()

    assert "3 Retrieval > 3.2 Reranking" in captured["user"]
    assert "p.145" in captured["user"]
    assert "pt-BR" in captured["system"]
    assert "Ciencia de Dados" in captured["system"]
    # No orphan placeholder survived fill_template on either half.
    for half in captured.values():
        assert "{language}" not in half and "{domain}" not in half
        assert "{section_path}" not in half and "{chunk_text}" not in half
        assert "{relevance_examples}" not in half and "{accepted_example}" not in half


def _process_chunk_test_setup(tmp_path):
    from pathlib import Path

    from zettel.state import StateDB

    cfg = AppConfig(
        vault_path=tmp_path / "vault",
        cache_path=tmp_path / "cache",
        state_db_path=tmp_path / "state.db",
        chroma_path=tmp_path / "chroma",
        prompts_path=Path(__file__).resolve().parents[1] / "prompts",
    )
    (cfg.vault_path / "00_Inbox" / "Review").mkdir(parents=True)
    db = StateDB(cfg.state_db_path)
    db.upsert_source("@Book2024", "Book2024", "Livro", ["Autor"], 2024, "h", "/x.pdf", "pdf")
    db.upsert_chapter("@Book2024::ch000", "@Book2024", "Ch1", "chh")
    db.upsert_chunk(
        "@Book2024::ch000::abc",
        "@Book2024",
        "@Book2024::ch000",
        "texto do chunk com conteudo suficiente",
        "ck",
        chunk_index=0,
        status="pending",
    )
    return cfg, db, db.get_chunk("@Book2024::ch000::abc")


def test_process_chunk_persists_rejection_taxonomy(tmp_path, monkeypatch):
    """Chunk-level rejection_category/rejection_reason survive into summary_json."""
    import json

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)

    def fake_call_llm(llm, user, system=None, **kwargs):
        return json.dumps(
            {
                "chunk_status": "rejected",
                "rejection_reason": "trecho e so uma referencia bibliografica",
                "rejection_category": "structural",
                "summary": "Trecho estrutural.",
                "key_concepts": [],
                "candidates": [],
            }
        )

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    persisted = json.loads(db.get_chunk("@Book2024::ch000::abc")["summary_json"])
    assert persisted["rejection_category"] == "structural"
    assert persisted["rejection_reason"] == "trecho e so uma referencia bibliografica"
    assert persisted["rejected_candidates"] == []
    db.close()


def test_prompt1_call_checksum_changes_when_forced():
    from zettel.extractor import prompt1_call_checksum

    cfg = AppConfig()
    row = {"chunk_checksum": "abc"}
    plain = prompt1_call_checksum(cfg, "ph", row, "")
    forced = prompt1_call_checksum(cfg, "ph", row, "", force_extract=True)
    assert plain != forced


def test_process_chunk_force_writes_draft_from_a_rejected_payload(tmp_path, monkeypatch):
    """The reviewer already accepted the chunk: a rejected payload still becomes a draft."""
    import json

    from zettel.extractor import FORCE_EXTRACT_INSTRUCTION, _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)
    cfg.extraction.verify_anchor_quote = False
    db.update_chunk_review(
        chunk_row["chunk_id"],
        summary_json=json.dumps({"candidates": [], "force_extract": True}),
    )
    chunk_row = db.get_chunk(chunk_row["chunk_id"])
    seen: list[str] = []

    def fake_call_llm(llm, user, system=None, **kwargs):
        seen.append(user)
        return json.dumps(
            {
                "chunk_status": "rejected",
                "rejection_reason": "o modelo achou trivial",
                "rejection_category": "trivial",
                "summary": "Trecho que o revisor quis manter.",
                "key_concepts": [],
                "candidates": [
                    {
                        "thesis": "Curta",
                        "definition": "curta",
                        "anchor_quote": "texto do chunk",
                        "relevance_score": 1,
                    }
                ],
            }
        )

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    row = db.get_chunk(chunk_row["chunk_id"])
    persisted = json.loads(row["summary_json"])
    assert row["status"] == "awaiting_review"
    assert row["literature_note_path"]
    assert persisted["candidates"]
    assert "force_extract" not in persisted
    assert FORCE_EXTRACT_INSTRUCTION in seen[0]
    db.close()


def test_process_chunk_force_without_a_candidate_stays_failed(tmp_path, monkeypatch):
    """An empty forced response is not cached and does not return to extract-rejected."""
    import json

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)
    db.update_chunk_review(
        chunk_row["chunk_id"],
        summary_json=json.dumps({"candidates": [], "force_extract": True}),
    )
    calls = {"n": 0}

    def fake_call_llm(llm, user, system=None, **kwargs):
        calls["n"] += 1
        return json.dumps(
            {
                "chunk_status": "rejected",
                "rejection_reason": "ainda vazio",
                "rejection_category": "trivial",
                "summary": "Nada.",
                "key_concepts": [],
                "candidates": [],
            }
        )

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    chunk_row = db.get_chunk(chunk_row["chunk_id"])
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    row = db.get_chunk(chunk_row["chunk_id"])
    persisted = json.loads(row["summary_json"])
    assert row["status"] == "failed"
    assert persisted["force_extract"] is True
    assert persisted["candidates"] == []

    chunk_row = db.get_chunk(chunk_row["chunk_id"])
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")
    assert calls["n"] == 2
    db.close()


def test_process_chunk_normalizes_unknown_rejection_category(tmp_path, monkeypatch):
    """An out-of-vocabulary category from the LLM doesn't crash the parse."""
    import json

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)

    def fake_call_llm(llm, user, system=None, **kwargs):
        return json.dumps(
            {
                "chunk_status": "rejected",
                "rejection_reason": "motivo qualquer",
                "rejection_category": "categoria-inventada-pelo-llm",
                "summary": "Trecho.",
                "key_concepts": [],
                "candidates": [],
            }
        )

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    persisted = json.loads(db.get_chunk("@Book2024::ch000::abc")["summary_json"])
    assert persisted["rejection_category"] == ""
    db.close()


def test_process_chunk_persists_rejected_candidates_with_reason(tmp_path, monkeypatch):
    """A candidate dropped by the deterministic filter is recorded with its reason."""
    import json

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)

    low_relevance_candidate = {
        "thesis": "Uma tese qualquer com palavras suficientes para passar no filtro de tamanho",
        "definition": (
            "Uma definicao qualquer com bastante texto explicativo sobre o tema tratado aqui"
        ),
        "anchor_quote": "",
        "relevance_score": 1,
    }

    def fake_call_llm(llm, user, system=None, **kwargs):
        return json.dumps(
            {
                "chunk_status": "accepted",
                "rejection_reason": "",
                "rejection_category": "",
                "summary": (
                    "Resumo com conteudo suficiente para pontuar bem no calculo de confianca."
                ),
                "key_concepts": ["conceito"],
                "candidates": [low_relevance_candidate],
            }
        )

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    persisted = json.loads(db.get_chunk("@Book2024::ch000::abc")["summary_json"])
    assert persisted["candidates"] == []
    assert len(persisted["rejected_candidates"]) == 1
    rejected = persisted["rejected_candidates"][0]
    assert rejected["thesis"] == low_relevance_candidate["thesis"]
    assert "relevance_score" in rejected["reason"]
    db.close()


def test_process_chunk_persists_the_discarded_anchor_quote(tmp_path, monkeypatch):
    """The dropped quote must survive for audit, not just the thesis (#153).

    Without it an audit cannot see what was thrown away without re-running the
    model — the exact gap that stopped `scripts/calibrate_review_confidence.py`
    from rebuilding the integrity term out of history.
    """
    import json

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)
    quote = "uma citacao literal que precisa sobreviver ao descarte para auditoria posterior"

    def fake_call_llm(llm, user, system=None, **kwargs):
        return json.dumps(
            {
                "chunk_status": "accepted",
                "rejection_reason": "",
                "rejection_category": "",
                "summary": "Resumo com conteudo suficiente para pontuar no calculo de confianca.",
                "key_concepts": ["conceito"],
                "candidates": [
                    {
                        "thesis": "Uma tese qualquer com palavras suficientes para o filtro",
                        "definition": "Uma definicao com bastante texto explicativo sobre o tema",
                        "anchor_quote": quote,
                        "relevance_score": 1,  # dropped by the relevance floor
                    }
                ],
            }
        )

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    persisted = json.loads(db.get_chunk("@Book2024::ch000::abc")["summary_json"])
    assert persisted["rejected_candidates"][0]["anchor_quote"] == quote
    assert persisted["rejection_category"] == "filtered"
    db.close()


def test_process_chunk_records_a_parse_failure_with_the_raw_answer(tmp_path, monkeypatch):
    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)

    def fake_call_llm(llm, user, system=None, **kwargs):
        return "isto nao e json"

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    rows = db.llm_failure_counts()
    assert rows and rows[0]["phase"] == "extract"
    stored = db.conn.execute("SELECT raw_response, attempt FROM llm_failures").fetchall()
    assert any(row[0] == "isto nao e json" for row in stored)
    assert db.get_chunk(chunk_row["chunk_id"])["status"] == "failed"
    db.close()


def test_process_chunk_persists_a_trimmed_literal_anchor(tmp_path, monkeypatch):
    import json

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)
    quote = " ".join(f"palavra{i}" for i in range(45))
    db.upsert_chunk(
        chunk_row["chunk_id"],
        "@Book2024",
        "@Book2024::ch000",
        f"Trecho fonte. {quote}",
        "ck",
        chunk_index=0,
        status="pending",
    )
    chunk_row = db.get_chunk(chunk_row["chunk_id"])

    def fake_call_llm(llm, user, system=None, **kwargs):
        return json.dumps(
            {
                "chunk_status": "accepted",
                "rejection_reason": "",
                "rejection_category": "",
                "summary": "Resumo com conteudo suficiente para pontuar no calculo de confianca.",
                "key_concepts": ["conceito"],
                "candidates": [
                    {
                        "thesis": "Uma tese qualquer com palavras suficientes para o filtro",
                        "definition": (
                            "Uma definicao com bastante texto explicativo sobre o tema central"
                        ),
                        "anchor_quote": quote,
                        "relevance_score": 4,
                    }
                ],
            }
        )

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    row = db.get_chunk(chunk_row["chunk_id"])
    persisted = json.loads(row["summary_json"])
    assert row["status"] == "awaiting_review"
    kept = persisted["candidates"][0]
    assert persisted["anchor_trimmed"] is True
    assert kept["anchor_trimmed"] is True
    assert kept["anchor_quote_original"] == quote
    assert len(kept["anchor_quote"].split()) == 25
    concept_ids = [c["concept_id"] for c in db.get_concepts_by_status("awaiting_review")]
    assert concept_ids
    assert concept_ids[0].startswith(f"{chunk_row['chunk_id']}::concept::")
    db.close()


def test_run_extract_defers_chunk_with_pending_neighbor_image(tmp_path, monkeypatch):
    from zettel.extractor import run_extract

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)
    cfg.images.enabled = True
    db.upsert_chunk(
        chunk_row["chunk_id"],
        "@Book2024",
        "@Book2024::ch000",
        chunk_row["text"],
        "ck",
        chunk_index=0,
        status="pending",
        page_in_file=4,
    )
    db.upsert_asset(
        "@Book2024::img::a",
        "@Book2024",
        "90_Assets/a.png",
        "ckimg",
        chapter_id="@Book2024::ch000",
        page_in_file=4,
    )
    monkeypatch.setattr("zettel.assets.describe_pending_assets", lambda *a, **k: 0)
    monkeypatch.setattr("zettel.extractor.get_llm", lambda *a, **k: object())

    def boom(*_a, **_k):
        raise AssertionError("chunk com imagem pendente nao deve chamar o LLM")

    monkeypatch.setattr("zettel.extractor.call_llm", boom)
    try:
        run_extract(cfg, db, None)
        assert db.get_chunk(chunk_row["chunk_id"])["status"] == "pending"
        assert db.get_asset("@Book2024::img::a")["status"] == "pending"
    finally:
        db.close()


# ── No draft file for chunks with nothing to review ──────────────────────


def test_process_chunk_rejected_writes_no_draft_file(tmp_path, monkeypatch):
    """chunk_status=rejected: no LIT draft file, and the chunk lands in `rejected`
    (not `awaiting_review` with nothing to review)."""
    import json

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)

    def fake_call_llm(llm, user, system=None, **kwargs):
        return json.dumps(
            {
                "chunk_status": "rejected",
                "rejection_reason": "so uma referencia bibliografica",
                "rejection_category": "structural",
                "summary": "Trecho estrutural.",
                "key_concepts": [],
                "candidates": [],
            }
        )

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    row = db.get_chunk("@Book2024::ch000::abc")
    assert row["literature_note_path"] is None
    assert row["status"] == "rejected"
    assert json.loads(row["summary_json"])["rejection_category"] == "structural"
    review_dir = cfg.vault_path / "00_Inbox" / "Review"
    assert list(review_dir.rglob("*.md")) == []
    db.close()


def test_process_chunk_accepted_but_all_candidates_filtered_writes_no_draft_file(
    tmp_path,
    monkeypatch,
):
    """chunk_status=accepted but the deterministic filter drops every candidate."""
    import json

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)

    low_relevance_candidate = {
        "thesis": "Uma tese qualquer com palavras suficientes para passar no filtro de tamanho",
        "definition": (
            "Uma definicao qualquer com bastante texto explicativo sobre o tema tratado aqui"
        ),
        "anchor_quote": "",
        "relevance_score": 1,
    }

    def fake_call_llm(llm, user, system=None, **kwargs):
        return json.dumps(
            {
                "chunk_status": "accepted",
                "rejection_reason": "",
                "rejection_category": "",
                "summary": (
                    "Resumo com conteudo suficiente para pontuar bem no calculo de confianca."
                ),
                "key_concepts": ["conceito"],
                "candidates": [low_relevance_candidate],
            }
        )

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    row = db.get_chunk("@Book2024::ch000::abc")
    assert row["literature_note_path"] is None
    review_dir = cfg.vault_path / "00_Inbox" / "Review"
    assert list(review_dir.rglob("*.md")) == []
    db.close()


def test_process_chunk_accepted_with_approved_candidate_writes_draft_file(tmp_path, monkeypatch):
    """A chunk with at least one approved candidate keeps writing its draft."""
    import json
    from pathlib import Path

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)
    cfg.extraction.verify_anchor_quote = (
        False  # chunk text is too short to host a 10-25 word anchor
    )

    good_candidate = {
        "thesis": "Gradient descent converge mais rapido com learning rate adaptativo no treino",
        "definition": (
            "O algoritmo de gradient descent ajusta os pesos do modelo iterativamente "
            "na direcao oposta ao gradiente da funcao de perda observada"
        ),
        "anchor_quote": "texto do chunk com conteudo suficiente",
        "relevance_score": 4,
    }

    def fake_call_llm(llm, user, system=None, **kwargs):
        return json.dumps(
            {
                "chunk_status": "accepted",
                "rejection_reason": "",
                "rejection_category": "",
                "summary": (
                    "Resumo com conteudo suficiente para pontuar bem no calculo de confianca."
                ),
                "key_concepts": ["conceito"],
                "candidates": [good_candidate],
            }
        )

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    row = db.get_chunk("@Book2024::ch000::abc")
    assert row["literature_note_path"] is not None
    assert Path(row["literature_note_path"]).is_file()
    db.close()


# ── #55: cache the response that parsed, not the one that failed ─────────


def test_process_chunk_caches_repaired_response_not_broken_one(tmp_path, monkeypatch):
    """A malformed first response is never cached; the repaired one is, under the same key."""
    import json

    from zettel.extractor import _process_chunk
    from zettel.hashing import compute_llm_call_checksum
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)

    good_response = json.dumps(
        {
            "chunk_status": "rejected",
            "rejection_reason": "estrutural",
            "rejection_category": "structural",
            "summary": "Trecho estrutural.",
            "key_concepts": [],
            "candidates": [],
        }
    )
    calls = {"n": 0}

    def fake_call_llm(llm, user, system=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "isto nao e json valido {{{"
        return good_response

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    assert calls["n"] == 2  # primary call + repair retry

    checksum = compute_llm_call_checksum(
        "prompthash",
        chunk_row["chunk_checksum"],
        cfg.llm.extract.model,
        cfg.llm.temperature,
        cfg.language,
        rag_context_checksum="",
        provider=cfg.llm.extract.provider,
        top_p=cfg.llm.top_p,
    )
    assert db.get_cached_llm_response(checksum) == good_response
    db.close()


def test_process_chunk_validation_error_prompt_includes_error_message(tmp_path, monkeypatch):
    """An out-of-range field (schema violation) gets a repair prompt citing the error."""
    import json

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)

    invalid_response = json.dumps(
        {
            "chunk_status": "accepted",
            "rejection_reason": "",
            "rejection_category": "",
            "summary": "Resumo valido com bastante conteudo para pontuar razoavelmente bem.",
            "key_concepts": ["conceito"],
            "candidates": [
                {
                    "thesis": ("Uma tese qualquer com palavras suficientes para passar no filtro"),
                    "definition": (
                        "Uma definicao qualquer com bastante texto explicativo sobre o tema"
                    ),
                    "anchor_quote": "",
                    # out of 1-5 range -> ValidationError, not filter rejection
                    "relevance_score": 9,
                }
            ],
        }
    )
    captured_retry_prompts: list[str] = []

    def fake_call_llm(llm, user, system=None, **kwargs):
        if kwargs.get("label", "").startswith("extract-retry"):
            captured_retry_prompts.append(user)
            return json.dumps(
                {
                    "chunk_status": "accepted",
                    "rejection_reason": "",
                    "rejection_category": "",
                    "summary": (
                        "Resumo valido com bastante conteudo para pontuar razoavelmente bem."
                    ),
                    "key_concepts": ["conceito"],
                    "candidates": [],
                }
            )
        return invalid_response

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    assert len(captured_retry_prompts) == 1
    retry_prompt = captured_retry_prompts[0]
    assert "contrato esperado" in retry_prompt
    assert "relevance_score" in retry_prompt  # the pydantic error message is embedded
    db.close()


def test_process_chunk_json_decode_error_repair_includes_the_schema(tmp_path, monkeypatch):
    """A malformed answer is repaired with the expected schema, not a bare 'fix this'."""
    import json

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)
    captured_retry_prompts: list[str] = []

    def fake_call_llm(llm, user, system=None, **kwargs):
        if kwargs.get("label", "").startswith("extract-retry"):
            captured_retry_prompts.append(user)
            return json.dumps(
                {
                    "chunk_status": "rejected",
                    "rejection_reason": "x",
                    "rejection_category": "",
                    "summary": "s",
                    "key_concepts": [],
                    "candidates": [],
                }
            )
        return "{ isto: nao fecha"

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    assert len(captured_retry_prompts) == 1
    retry_prompt = captured_retry_prompts[0]
    assert "Esquema esperado" in retry_prompt
    assert "{ isto: nao fecha" in retry_prompt
    assert "Expecting property name" in retry_prompt
    db.close()


def test_process_chunk_fails_after_two_bad_attempts(tmp_path, monkeypatch):
    """Both the primary call and the repair retry fail to parse -> status=failed."""
    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)

    def fake_call_llm(llm, user, system=None, **kwargs):
        return "isto nunca vai ser json valido"

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    assert db.get_chunk("@Book2024::ch000::abc")["status"] == "failed"
    db.close()


# ── #57/#58: draft filename, H1 and note body agree on the same thesis ───


def test_process_chunk_draft_filename_and_h1_share_the_same_thesis(tmp_path, monkeypatch):
    """The chosen candidate's thesis drives both the filename slug and the H1."""
    import json
    from pathlib import Path

    from zettel.extractor import _process_chunk
    from zettel.llm import load_prompt_parts

    cfg, db, chunk_row = _process_chunk_test_setup(tmp_path)
    cfg.extraction.verify_anchor_quote = False  # chunk text is too short for a 10-25 word anchor

    def fake_call_llm(llm, user, system=None, **kwargs):
        return json.dumps(
            {
                "chunk_status": "accepted",
                "rejection_reason": "",
                "rejection_category": "",
                "summary": "Resumo generico que nao deveria vencer a tese.",
                "key_concepts": ["conceito"],
                "candidates": [
                    {
                        "thesis": "Tese fraca e menos relevante para o tema principal",
                        "definition": (
                            "Definicao qualquer com palavras suficientes para passar pelo "
                            "filtro minimo de tamanho estabelecido na configuracao"
                        ),
                        "anchor_quote": "texto do chunk",
                        "relevance_score": 2,
                    },
                    {
                        "thesis": "Backpropagation calcula gradientes via regra da cadeia",
                        "definition": (
                            "Definicao qualquer com palavras suficientes para passar pelo "
                            "filtro minimo de tamanho estabelecido na configuracao"
                        ),
                        "anchor_quote": "texto do chunk",
                        "relevance_score": 5,
                    },
                ],
            }
        )

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call_llm)
    prompt_parts = load_prompt_parts(cfg.prompts_path / "literature_note.md")
    _process_chunk(cfg, db, None, object(), chunk_row, prompt_parts, "prompthash")

    row = db.get_chunk("@Book2024::ch000::abc")
    draft_path = row["literature_note_path"]
    assert draft_path is not None
    body = Path(draft_path).read_text(encoding="utf-8")

    assert "backpropagation" in Path(draft_path).name.lower()
    h1 = next(line for line in body.splitlines() if line.startswith("# "))
    assert "Backpropagation calcula gradientes via regra da cadeia" in h1

    from zettel.vault import literature_chunk_filename_for_row

    assert literature_chunk_filename_for_row("Book2024", row) == Path(draft_path).name
    db.close()


# ── #59: review_confidence recalibration ──────────────────────────────


def _make_output(**overrides) -> LiteratureChunkOutput:
    defaults = {
        "chunk_status": "accepted",
        "rejection_reason": "",
        "rejection_category": "",
        "summary": "Resumo qualquer.",
        "key_concepts": ["conceito"],
        "candidates": [],
    }
    defaults.update(overrides)
    return LiteratureChunkOutput(**defaults)


def test_forced_low_relevance_is_scored_with_the_same_filter():
    """A reviewer-forced relevance-2 candidate must not collapse to the empty-filter score."""
    cfg = _make_config()
    low = _make_candidate(relevance_score=2)
    output = _make_output(chunk_status="accepted", candidates=[low])
    assert _score_review_confidence(output, cfg) == 0.2
    forced = _score_review_confidence(output, cfg, force=True)
    assert forced > 0.2


def test_score_review_confidence_rejected_chunk_is_lowest():
    cfg = _make_config()
    output = _make_output(chunk_status="rejected", candidates=[])
    assert _score_review_confidence(output, cfg) == 0.1


def test_score_review_confidence_accepted_without_candidates_is_low_but_distinguishable():
    cfg = _make_config()
    output = _make_output(chunk_status="accepted", candidates=[])
    score = _score_review_confidence(output, cfg)
    assert score == 0.2
    rejected_score = _score_review_confidence(_make_output(chunk_status="rejected"), cfg)
    assert score > rejected_score


def test_score_review_confidence_all_candidates_filtered_out_is_low():
    cfg = _make_config()
    weak = _make_candidate(relevance_score=1)  # below min_relevance_score=3
    output = _make_output(candidates=[weak])
    assert _score_review_confidence(output, cfg) == 0.2


def test_score_review_confidence_has_real_dispersion_not_saturated():
    """Relevance, not verbosity, is what separates two chunks (issue #152).

    An earlier formula converged on ~0.98 for any well-formed chunk; the one
    that replaced it separated on `definition` word count. Separation must come
    from the model's own judgement of the concept.
    """
    cfg = _make_config()
    floor = _make_candidate(relevance_score=3)
    fundamental = _make_candidate(relevance_score=5)

    floor_score = _score_review_confidence(_make_output(candidates=[floor]), cfg)
    top_score = _score_review_confidence(_make_output(candidates=[fundamental]), cfg)

    assert top_score > floor_score
    assert top_score - floor_score >= 0.19  # meaningfully separated, not a rounding blip
    assert floor_score < top_score <= 1.0


def test_score_review_confidence_ignores_definition_length():
    """Word count is not a quality signal and must not move the score.

    The concrete regression: a candidate whose definition ran 47 words failed
    the gate while an otherwise identical 48-word one passed.
    """
    cfg = _make_config()
    terse = _make_candidate(definition=" ".join(f"palavra{i}" for i in range(12)))
    verbose = _make_candidate(definition=" ".join(f"palavra{i}" for i in range(120)))

    assert _score_review_confidence(
        _make_output(candidates=[terse]), cfg
    ) == _score_review_confidence(_make_output(candidates=[verbose]), cfg)


def test_score_review_confidence_floor_relevance_can_reach_the_threshold():
    """No valid `relevance_score` may be structurally barred from auto-approve.

    Before #152 a chunk at `min_relevance_score` had a ceiling of 0.70 against
    a 0.75 threshold, so an entire class of valid candidates could never pass
    however clean it was.
    """
    cfg = _make_config()
    flawless_at_floor = _make_output(
        candidates=[_make_candidate(relevance_score=cfg.extraction.min_relevance_score)]
    )
    score = _score_review_confidence(flawless_at_floor, cfg)
    assert score >= cfg.literature_review.auto_approve_min_confidence


def test_score_review_confidence_does_not_penalize_optional_limits():
    """`limits` is strictly conditional; omitting it does not lower the score."""
    cfg = _make_config()
    with_limits = _make_candidate()
    without_limits = _make_candidate(limits="")

    assert _score_review_confidence(
        _make_output(candidates=[with_limits]), cfg
    ) == _score_review_confidence(_make_output(candidates=[without_limits]), cfg)


def test_score_review_confidence_partial_filter_rejection_lowers_score():
    """A chunk where the filter dropped half the candidates scores lower."""
    cfg = _make_config()
    good = _make_candidate(relevance_score=5)
    weak = _make_candidate(relevance_score=1)  # gets filtered out

    all_good = _score_review_confidence(
        _make_output(candidates=[good, _make_candidate(relevance_score=5)]),
        cfg,
    )
    half_filtered = _score_review_confidence(
        _make_output(candidates=[good, weak]),
        cfg,
    )
    assert all_good > half_filtered


def test_score_review_confidence_deterministic():
    cfg = _make_config()
    output = _make_output(candidates=[_make_candidate()])
    a = _score_review_confidence(output, cfg)
    b = _score_review_confidence(output, cfg)
    assert a == b


def test_score_review_confidence_handles_degenerate_relevance_range():
    """min_relevance_score == 5 (rel_span == 0) must not raise ZeroDivisionError."""
    cfg = _make_config(min_relevance_score=5)
    output = _make_output(candidates=[_make_candidate(relevance_score=5)])
    score = _score_review_confidence(output, cfg)
    assert 0.0 <= score <= 1.0


def test_score_review_confidence_handles_degenerate_definition_floor():
    """min_definition_words == 0 (depth_span == 0) must not raise ZeroDivisionError."""
    cfg = _make_config(min_definition_words=0)
    output = _make_output(candidates=[_make_candidate()])
    score = _score_review_confidence(output, cfg)
    assert 0.0 <= score <= 1.0


def test_score_review_confidence_never_calls_llm():
    """No `llm` argument exists on the signature -- purely a function of output/cfg."""
    import inspect

    params = inspect.signature(_score_review_confidence).parameters
    assert "llm" not in params


# ── #60: summary gets a hard cap, truncated rather than rejected ─────────


def test_oversized_summary_is_truncated_not_rejected():
    long_summary = "x" * 400
    output = _make_output(summary=long_summary)
    assert len(output.summary) == 280
    assert output.summary == long_summary[:280]


def test_summary_within_cap_is_untouched():
    summary = "Um resumo normal de tamanho razoavel."
    output = _make_output(summary=summary)
    assert output.summary == summary


# ── #63: intra-batch candidate dedupe ─────────────────────────────────

from zettel.extractor import _intra_batch_dedupe, deduplicate_candidates

from tests.fakes import FakeVectorIndex, calls_to


def _cand_dict(concept_id: str, **overrides) -> dict:
    return {
        "concept_id": concept_id,
        "source_id": "@S",
        "chunk_id": f"@S::ch000::{concept_id}",
        "candidate": _make_candidate(**overrides),
    }


def test_intra_batch_dedupe_exact_thesis_hash_match_keeps_higher_relevance():
    cfg = _make_config()
    idx = FakeVectorIndex()
    same_thesis = "Gradient descent converge mais rapido com learning rate adaptativo"
    a = _cand_dict("c1", thesis=same_thesis, relevance_score=3)
    b = _cand_dict("c2", thesis=same_thesis, relevance_score=5)

    kept, duplicates = _intra_batch_dedupe(cfg, idx, [a, b])

    assert len(kept) == 1
    assert kept[0]["concept_id"] == "c2"  # higher relevance_score wins
    assert len(duplicates) == 1
    assert duplicates[0]["concept_id"] == "c1"
    assert duplicates[0]["duplicate_of"] == "c2"
    assert calls_to(idx, "embed_texts") == []  # exact-hash path never needs to embed


def test_intra_batch_dedupe_semantic_match_within_threshold():
    cfg = _make_config()
    a = _cand_dict("c1", thesis="Primeira formulacao da mesma ideia central", relevance_score=4)
    b = _cand_dict("c2", thesis="Segunda formulacao da mesma ideia central", relevance_score=5)
    text_a = f"{a['candidate'].thesis} {a['candidate'].definition}"
    text_b = f"{b['candidate'].thesis} {b['candidate'].definition}"
    # Unit vectors, cosine 0.99 — above dedupe_threshold 0.85.
    idx = FakeVectorIndex(vectors={text_a: [1.0, 0.0], text_b: [0.99, 0.1410673598]})

    kept, duplicates = _intra_batch_dedupe(cfg, idx, [a, b])

    assert len(kept) == 1
    assert kept[0]["concept_id"] == "c2"
    assert len(duplicates) == 1
    assert duplicates[0]["concept_id"] == "c1"
    assert calls_to(idx, "embed_texts") == [[text_a, text_b]]  # one batched call, not per-pair


def test_intra_batch_dedupe_distinct_candidates_both_survive():
    cfg = _make_config()
    a = _cand_dict("c1", thesis="Uma ideia sobre regularizacao L1 em modelos lineares")
    b = _cand_dict("c2", thesis="Uma ideia completamente diferente sobre grafos de conhecimento")
    text_a = f"{a['candidate'].thesis} {a['candidate'].definition}"
    text_b = f"{b['candidate'].thesis} {b['candidate'].definition}"
    idx = FakeVectorIndex(vectors={text_a: [1.0, 0.0], text_b: [0.0, 1.0]})  # cosine 0

    kept, duplicates = _intra_batch_dedupe(cfg, idx, [a, b])

    assert {c["concept_id"] for c in kept} == {"c1", "c2"}
    assert duplicates == []


def test_intra_batch_dedupe_single_candidate_is_a_noop():
    cfg = _make_config()
    idx = FakeVectorIndex()
    a = _cand_dict("c1")
    kept, duplicates = _intra_batch_dedupe(cfg, idx, [a])
    assert kept == [a]
    assert duplicates == []
    assert calls_to(idx, "embed_texts") == []


def test_intra_batch_dedupe_skips_pairwise_above_cap():
    """Above the pairwise cap, only the cheap hash pass runs -- no embedding call."""
    cfg = _make_config()
    idx = FakeVectorIndex()
    from zettel.extractor import _INTRA_BATCH_PAIRWISE_MAX

    candidates = [
        _cand_dict(f"c{i}", thesis=f"Tese numero {i} totalmente distinta das outras aqui")
        for i in range(_INTRA_BATCH_PAIRWISE_MAX + 1)
    ]
    kept, duplicates = _intra_batch_dedupe(cfg, idx, candidates)
    assert len(kept) == _INTRA_BATCH_PAIRWISE_MAX + 1
    assert duplicates == []
    assert calls_to(idx, "embed_texts") == []


def test_deduplicate_candidates_intra_batch_duplicate_marked_before_existing_notes_pass(
    tmp_path,
    monkeypatch,
):
    """A batch-internal duplicate never reaches the (LLM-backed) existing-notes pass."""
    from pathlib import Path

    from zettel.state import StateDB

    cfg = _make_config()
    cfg.prompts_path = Path(__file__).resolve().parents[1] / "prompts"

    db = StateDB(tmp_path / "state.db")
    db.upsert_source("@S", "S", "T", [], None, "h", "/p", "md")
    db.upsert_chapter("@S::ch000", "@S", "Ch", "chk")
    db.upsert_chunk("@S::ch000::a", "@S", "@S::ch000", "txt", "ck")

    same_thesis = "Backpropagation calcula gradientes via regra da cadeia no grafo"
    a = _cand_dict("c1", thesis=same_thesis, relevance_score=3)
    b = _cand_dict("c2", thesis=same_thesis, relevance_score=5)
    for cd in (a, b):
        db.upsert_concept(cd["concept_id"], "@S", "@S::ch000::a", status="extracted")

    idx = FakeVectorIndex()
    monkeypatch.setattr(
        "zettel.extractor.call_llm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM should not be called")),
    )

    approved = deduplicate_candidates(cfg, db, idx, object(), [a, b])

    assert [c["concept_id"] for c in approved] == ["c2"]
    # Absorbed in the batch is a proposal, not a drop: it waits for the reviewer.
    loser = db.get_concept("c1")
    assert loser["status"] == "dedupe_pending"
    assert json.loads(loser["dedupe_json"])["duplicate_of"] == "c2"
    assert db.get_concept("c2")["status"] == "approved"
    db.close()


# ── Dedupe is scoped to one source; cross-source hits are corroboration ──


class _FakeNeighbourIndex(FakeVectorIndex):
    """One near-identical existing permanent note for every candidate, whatever ``where`` says.

    Dedupe guards the source twice: the Chroma ``where`` filter and
    ``_same_source_notes`` over SQLite. Ignoring the filter on purpose is what
    lets these tests pin the second guard. Distance 0.0 is cosine 1.0.
    """

    def __init__(self, note_id: str, distance: float = 0.0):
        super().__init__(notes={note_id: distance})

    def query_similar_notes(
        self, query_text, n_results=5, exclude_id=None, *, purpose="densa", where=None
    ):
        return self._hits("permanent_notes", n_results, exclude_id=exclude_id)


def _dedupe_env(tmp_path, note_source_id: str):
    """A vault with one existing permanent note belonging to ``note_source_id``."""
    from pathlib import Path

    from zettel.state import StateDB

    cfg = _make_config()
    cfg.prompts_path = Path(__file__).resolve().parents[1] / "prompts"

    db = StateDB(tmp_path / "state.db")
    for sid in {"@S", note_source_id}:
        db.upsert_source(sid, sid.lstrip("@"), "T", [], None, f"h{sid}", f"/{sid}.md", "md")
    db.upsert_chapter("@S::ch000", "@S", "Ch", "chk")
    db.upsert_chunk("@S::ch000::a", "@S", "@S::ch000", "txt", "ck")
    db.upsert_note("N1", note_source_id, "/n1.md", title="Nota existente", body="corpo")
    return cfg, db


def test_intra_batch_dedupe_cosine_threshold_marks_086_and_not_084():
    """#267: tau 0.85 is cosine. 0.86 is a duplicate; 0.84 is not."""
    import math

    cfg = _make_config()

    def _pair(cosine: float):
        a = _cand_dict("c1", thesis=f"Tese A com cosseno {cosine}", relevance_score=4)
        b = _cand_dict("c2", thesis=f"Tese B com cosseno {cosine}", relevance_score=5)
        text_a = f"{a['candidate'].thesis} {a['candidate'].definition}"
        text_b = f"{b['candidate'].thesis} {b['candidate'].definition}"
        ortho = math.sqrt(1.0 - cosine * cosine)
        idx = FakeVectorIndex(vectors={text_a: [1.0, 0.0], text_b: [cosine, ortho]})
        return _intra_batch_dedupe(cfg, idx, [a, b])

    _kept, duplicates = _pair(0.86)
    assert [c["concept_id"] for c in duplicates] == ["c1"]
    kept, duplicates = _pair(0.84)
    assert duplicates == []
    assert {c["concept_id"] for c in kept} == {"c1", "c2"}


def test_dedupe_same_source_note_at_cosine_080_does_not_reach_the_llm(tmp_path, monkeypatch):
    """#267: cosine 0.80 is below tau 0.85, so the candidate is approved without an LLM call.

    Chroma cosine distance of 0.80 is ``1 - 0.80 = 0.20``.
    """
    cfg, db = _dedupe_env(tmp_path, note_source_id="@S")
    cand = _cand_dict("c1", thesis="Redes profundas generalizam por vies implicito")
    db.upsert_concept("c1", "@S", "@S::ch000::a", status="extracted")

    monkeypatch.setattr(
        "zettel.extractor.call_llm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM should not be called")),
    )
    approved = deduplicate_candidates(
        cfg, db, _FakeNeighbourIndex("N1", distance=0.20), object(), [cand]
    )
    assert [c["concept_id"] for c in approved] == ["c1"]
    assert db.get_concept("c1")["status"] == "approved"
    db.close()


def test_second_dedupe_call_flags_a_repeat_of_an_approved_concept_without_a_note(
    tmp_path, monkeypatch
):
    """#271: an approved concept that is not a note yet is visible to the next call."""
    from zettel.review import format_dedupe_item

    cfg, db = _dedupe_env(tmp_path, note_source_id="@S")
    thesis = "A mesma tese aprovada antes do connect ainda nao virou nota"
    first = _cand_dict("c1", thesis=thesis)
    second = _cand_dict("c2", thesis=thesis)
    for cand in (first, second):
        db.upsert_concept(
            cand["concept_id"],
            "@S",
            "@S::ch000::a",
            candidate_json=cand["candidate"].model_dump_json(),
            status="extracted",
        )
    monkeypatch.setattr(
        "zettel.extractor.call_llm",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("LLM should not be called")),
    )
    idx = FakeVectorIndex()
    approved = deduplicate_candidates(cfg, db, idx, object(), [first])
    assert [c["concept_id"] for c in approved] == ["c1"]
    approved_again = deduplicate_candidates(cfg, db, idx, object(), [second])
    assert approved_again == []
    row = db.get_concept("c2")
    assert row["status"] == "dedupe_pending"
    assert json.loads(row["dedupe_json"])["duplicate_of"] == "c1"
    card = format_dedupe_item(db, row)
    assert "Conceito aprovado ainda sem nota" in card
    assert thesis in card
    db.close()


def test_dedupe_unavailable_llm_leaves_the_concept_extracted(tmp_path, monkeypatch):
    """#272: a down provider stops the call; nothing in it becomes approved."""
    from zettel.llm import LLMUnavailableError

    cfg, db = _dedupe_env(tmp_path, note_source_id="@S")
    cand = _cand_dict("c1", thesis="Redes profundas generalizam por vies implicito")
    db.upsert_concept("c1", "@S", "@S::ch000::a", status="extracted")

    def down(*_a, **_k):
        raise LLMUnavailableError("provider fora", phase="review")

    monkeypatch.setattr("zettel.extractor.call_llm", down)
    with pytest.raises(LLMUnavailableError):
        deduplicate_candidates(cfg, db, _FakeNeighbourIndex("N1"), object(), [cand])
    assert db.get_concept("c1")["status"] == "extracted"
    db.close()


def test_dedupe_parse_error_keeps_that_item_extracted(tmp_path, monkeypatch):
    """A bad answer on one candidate does not approve it and does not stop the next."""
    cfg, db = _dedupe_env(tmp_path, note_source_id="@S")
    first = _cand_dict("c1", thesis="Primeira tese sobre generalizacao implicita")
    second = _cand_dict("c2", thesis="Segunda tese sobre regularizacao explicita")
    for cand in (first, second):
        db.upsert_concept(cand["concept_id"], "@S", "@S::ch000::a", status="extracted")
    answers = iter(
        [
            "nao e json",
            '{"decision": "create_new", "target_note_id": null, "reason": "nova"}',
        ]
    )
    monkeypatch.setattr("zettel.extractor.call_llm", lambda *_a, **_k: next(answers))
    idx = _FakeNeighbourIndex("N1")
    idx.vectors = {
        f"{first['candidate'].thesis} {first['candidate'].definition}": [1.0, 0.0],
        f"{second['candidate'].thesis} {second['candidate'].definition}": [0.0, 1.0],
    }
    approved = deduplicate_candidates(cfg, db, idx, object(), [first, second])
    assert [c["concept_id"] for c in approved] == ["c2"]
    assert db.get_concept("c1")["status"] == "extracted"
    assert db.get_concept("c2")["status"] == "approved"
    db.close()


def test_dedupe_answer_quoting_a_status_code_is_a_parse_error_not_an_outage(tmp_path, monkeypatch):
    """A schema error whose text holds "429" fails that item only, never the run (#323)."""
    cfg, db = _dedupe_env(tmp_path, note_source_id="@S")
    first = _cand_dict("c1", thesis="Primeira tese sobre generalizacao implicita")
    second = _cand_dict("c2", thesis="Segunda tese sobre regularizacao explicita")
    for cand in (first, second):
        db.upsert_concept(cand["concept_id"], "@S", "@S::ch000::a", status="extracted")
    answers = iter(
        [
            '{"decision": "ver p. 429", "target_note_id": null, "reason": "x"}',
            '{"decision": "create_new", "target_note_id": null, "reason": "nova"}',
        ]
    )
    monkeypatch.setattr("zettel.extractor.call_llm", lambda *_a, **_k: next(answers))
    idx = _FakeNeighbourIndex("N1")
    idx.vectors = {
        f"{first['candidate'].thesis} {first['candidate'].definition}": [1.0, 0.0],
        f"{second['candidate'].thesis} {second['candidate'].definition}": [0.0, 1.0],
    }
    approved = deduplicate_candidates(cfg, db, idx, object(), [first, second])
    assert [c["concept_id"] for c in approved] == ["c2"]
    assert db.get_concept("c1")["status"] == "extracted"
    db.close()


def test_dedupe_does_not_build_the_llm_when_nothing_needs_it(tmp_path, monkeypatch):
    cfg, db = _dedupe_env(tmp_path, note_source_id="@Other")
    cand = _cand_dict("c1", thesis="Redes profundas generalizam por vies implicito")
    db.upsert_concept("c1", "@S", "@S::ch000::a", status="extracted")
    monkeypatch.setattr(
        "zettel.extractor.get_llm",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("get_llm")),
    )
    approved = deduplicate_candidates(cfg, db, _FakeNeighbourIndex("N1"), None, [cand])
    assert [c["concept_id"] for c in approved] == ["c1"]
    db.close()


def test_dedupe_skips_llm_when_nearest_note_is_from_another_source(tmp_path, monkeypatch):
    """Cross-source overlap is corroboration: never dropped, never worth an LLM call.

    This is where the cost saving lives — the old code paid for a decision whose
    only admissible answer was already known from `source_id`.
    """
    cfg, db = _dedupe_env(tmp_path, note_source_id="@Other")
    cand = _cand_dict("c1", thesis="Redes profundas generalizam por vies implicito")
    db.upsert_concept("c1", "@S", "@S::ch000::a", status="extracted")

    monkeypatch.setattr(
        "zettel.extractor.call_llm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM should not be called")),
    )

    approved = deduplicate_candidates(cfg, db, _FakeNeighbourIndex("N1"), object(), [cand])

    assert [c["concept_id"] for c in approved] == ["c1"]
    assert db.get_concept("c1")["status"] == "approved"
    db.close()


def test_dedupe_flags_a_repeat_within_the_same_source(tmp_path, monkeypatch):
    """`ignore` on the same source no longer drops: the concept waits for review."""
    cfg, db = _dedupe_env(tmp_path, note_source_id="@S")
    cand = _cand_dict("c1", thesis="Redes profundas generalizam por vies implicito")
    db.upsert_concept("c1", "@S", "@S::ch000::a", status="extracted")

    monkeypatch.setattr(
        "zettel.extractor.call_llm",
        lambda *a, **k: '{"decision": "ignore", "target_note_id": null, "reason": "repetido"}',
    )

    approved = deduplicate_candidates(cfg, db, _FakeNeighbourIndex("N1"), object(), [cand])

    assert approved == []
    concept = db.get_concept("c1")
    assert concept["status"] == "dedupe_pending"
    assert json.loads(concept["dedupe_json"])["reason"] == "repetido"
    # The notes the LLM compared against travel with the flag, for the review card.
    assert json.loads(concept["dedupe_json"])["compared_note_ids"] == ["N1"]
    db.close()


def test_dedupe_override_skips_every_check(tmp_path, monkeypatch):
    """A concept the reviewer chose to keep is never flagged again."""
    cfg, db = _dedupe_env(tmp_path, note_source_id="@S")
    cand = {**_cand_dict("c1", thesis="Redes profundas generalizam"), "dedupe_override": True}
    db.upsert_concept("c1", "@S", "@S::ch000::a", status="extracted")
    db.set_concept_dedupe("c1", "extracted", {"override": True})

    monkeypatch.setattr(
        "zettel.extractor.call_llm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM should not be called")),
    )
    idx = _FakeNeighbourIndex("N1")
    approved = deduplicate_candidates(cfg, db, idx, object(), [cand])

    assert [c["concept_id"] for c in approved] == ["c1"]
    concept = db.get_concept("c1")
    assert concept["status"] == "approved"
    assert json.loads(concept["dedupe_json"]) == {"override": True}
    assert calls_to(idx, "embed_texts") == []
    db.close()


def test_dedupe_same_source_can_still_refine(tmp_path, monkeypatch):
    """An author expanding a concept as the book advances is kept and linked."""
    cfg, db = _dedupe_env(tmp_path, note_source_id="@S")
    cand = _cand_dict("c1", thesis="Redes profundas generalizam por vies implicito")
    db.upsert_concept("c1", "@S", "@S::ch000::a", status="extracted")

    refine = '{"decision": "refine_existing", "target_note_id": "N1", "reason": "nuance"}'
    monkeypatch.setattr("zettel.extractor.call_llm", lambda *a, **k: refine)

    approved = deduplicate_candidates(cfg, db, _FakeNeighbourIndex("N1"), object(), [cand])

    assert [c["concept_id"] for c in approved] == ["c1"]
    assert approved[0]["refines_note_id"] == "N1"
    # The target must reach `connect` through SQLite, where it becomes `extends`.
    concept = db.get_concept("c1")
    assert concept["status"] == "approved"
    assert json.loads(concept["dedupe_json"]) == {"refines_note_id": "N1", "reason": "nuance"}
    db.close()


def test_dedupe_prompt_shows_each_existing_note_in_full(tmp_path, monkeypatch):
    """#209: the LLM sees the whole note -- not a 200-char excerpt, not its connections."""
    cfg, db = _dedupe_env(tmp_path, note_source_id="@S")
    definition = "Definicao longa que ultrapassa de longe duzentos caracteres. " * 10
    db.upsert_note(
        "N1",
        "@S",
        "/n1.md",
        title="Nota existente",
        body=(
            "> **Tese**: Tese da nota existente\n\n"
            f"{definition}\n\n"
            "## Limites\n\nSo vale para X.\n\n"
            "## Conexões\n\n- corroborado por [[ZTL - 01OUTRA]]\n"
        ),
    )
    db.upsert_concept("c1", "@S", "@S::ch000::a", status="extracted")
    seen = {}

    def fake_call(llm, user, **kw):
        seen["user"] = user
        return '{"decision": "create_new", "target_note_id": null, "reason": "nova"}'

    monkeypatch.setattr("zettel.extractor.call_llm", fake_call)
    deduplicate_candidates(cfg, db, _FakeNeighbourIndex("N1"), object(), [_cand_dict("c1")])

    prompt = seen["user"]
    assert definition.strip() in prompt
    assert "Limites: So vale para X." in prompt
    assert "corroborado" not in prompt and "dist=" not in prompt
    db.close()
