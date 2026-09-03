"""Tests for hashing utilities."""

from zettel.hashing import (
    compute_embedding_input_hash,
    compute_llm_call_checksum,
    dehyphenate_pdf_linebreaks,
    extract_embeddable_text,
    fold_for_match,
    normalize_text_for_hash,
    quote_is_grounded,
    sha256_hex,
    short_hash,
)


def test_compute_embedding_input_hash():
    a = compute_embedding_input_hash("sem123", "openai", "text-embedding-3-small")
    b = compute_embedding_input_hash("sem123", "openai", "text-embedding-3-small")
    assert a == b  # deterministic
    # Any component change alters the hash.
    assert a != compute_embedding_input_hash("sem124", "openai", "text-embedding-3-small")
    assert a != compute_embedding_input_hash("sem123", "sentence-transformers", "text-embedding-3-small")
    assert a != compute_embedding_input_hash("sem123", "openai", "outro-modelo")


def test_normalize_collapses_whitespace():
    text = "hello   world\t\ttab"
    result = normalize_text_for_hash(text)
    assert "   " not in result
    assert "\t" not in result
    assert result == "hello world tab"


def test_normalize_crlf():
    text = "line1\r\nline2\rline3\nline4"
    result = normalize_text_for_hash(text)
    assert "\r" not in result
    assert result.count("\n") == 3


def test_normalize_limits_blank_lines():
    text = "a\n\n\n\n\nb"
    result = normalize_text_for_hash(text)
    assert result == "a\n\nb"


def test_fold_for_match_strips_accents_case_and_punctuation():
    assert fold_for_match("Ação, Reação!") == "acao reacao"
    assert fold_for_match("hello,  world.") == "hello world"


def test_fold_for_match_empty_string():
    assert fold_for_match("") == ""


def test_quote_is_grounded_verbatim_substring():
    chunk = "O modelo converge mais rapido com learning rate adaptativo em cenarios ruidosos."
    assert quote_is_grounded("converge mais rapido com learning rate adaptativo", chunk)


def test_quote_is_grounded_case_and_accent_insensitive():
    chunk = "AÇÃO imediata resolve o problema de forma definitiva."
    assert quote_is_grounded("acao imediata resolve o problema", chunk)


def test_quote_is_grounded_tolerates_editorial_ellipsis():
    chunk = "adaptive learning rates converge faster in practice than fixed schedules"
    quote = "adaptive learning rates [...] than fixed schedules"
    assert quote_is_grounded(quote, chunk, min_ratio=0.85)


def test_quote_is_grounded_rejects_paraphrase():
    chunk = "adaptive learning rates converge faster in practice than fixed schedules"
    paraphrase = "taxas de aprendizado adaptativas convergem mais rapido que agendas fixas"
    assert not quote_is_grounded(paraphrase, chunk, min_ratio=0.85)


def test_quote_is_grounded_absent_quote():
    chunk = "texto totalmente diferente sem nenhuma relacao"
    assert not quote_is_grounded("isso nao aparece em lugar nenhum", chunk)


def test_quote_is_grounded_empty_quote_is_never_grounded():
    assert not quote_is_grounded("", "qualquer texto de chunk")
    assert not quote_is_grounded("   ", "qualquer texto de chunk")


def test_normalize_dehyphenation():
    text = "experi-\nmental"
    result = normalize_text_for_hash(text)
    assert result == "experimental"


def test_dehyphenate_pdf_linebreaks_merges_lowercase_continuation():
    assert dehyphenate_pdf_linebreaks("pala-\nvra") == "palavra"


def test_dehyphenate_pdf_linebreaks_preserves_uppercase_continuation():
    text = "bem-\nVindo"
    assert dehyphenate_pdf_linebreaks(text) == text


def test_dehyphenate_pdf_linebreaks_tolerates_surrounding_whitespace():
    assert dehyphenate_pdf_linebreaks("pala- \n vra") == "palavra"


def test_dehyphenate_pdf_linebreaks_is_idempotent():
    once = dehyphenate_pdf_linebreaks("experi-\nmental e outra pala-\nvra")
    twice = dehyphenate_pdf_linebreaks(once)
    assert once == twice == "experimental e outra palavra"


def test_dehyphenate_pdf_linebreaks_leaves_unrelated_hyphens_alone():
    text = "well-known e um hifen no meio da linha, nao no fim."
    assert dehyphenate_pdf_linebreaks(text) == text


def test_normalize_text_for_hash_preserves_uppercase_continuation_hyphen():
    text = "bem-\nVindo ao capitulo"
    result = normalize_text_for_hash(text)
    assert "bem-\nVindo" in result


# ── #61: provider + top_p in the LLM call checksum ────────────────────


def _base_checksum(**overrides) -> str:
    kwargs = dict(
        prompt_hash="ph", chunk_checksum="cc", model="gpt-4o-mini",
        temperature=0.0, language="pt-BR", provider="openai", top_p=1.0,
    )
    kwargs.update(overrides)
    return compute_llm_call_checksum(**kwargs)


def test_compute_llm_call_checksum_differs_by_provider():
    """Same model string, different provider (e.g. an OpenAI-compatible gateway)."""
    a = _base_checksum(provider="openai")
    b = _base_checksum(provider="openrouter")
    assert a != b


def test_compute_llm_call_checksum_differs_by_top_p():
    a = _base_checksum(top_p=0.5)
    b = _base_checksum(top_p=1.0)
    assert a != b


def test_compute_llm_call_checksum_provider_and_top_p_have_defaults():
    """Old callers that don't pass provider/top_p still get a stable checksum."""
    a = compute_llm_call_checksum("ph", "cc", "gpt-4o-mini", 0.0, "pt-BR")
    b = compute_llm_call_checksum("ph", "cc", "gpt-4o-mini", 0.0, "pt-BR")
    assert a == b


def test_sha256_deterministic():
    assert sha256_hex("hello") == sha256_hex("hello")
    assert sha256_hex("hello") != sha256_hex("world")


def test_short_hash_length():
    h = short_hash("test", length=12)
    assert len(h) == 12


def test_extract_embeddable_text_strips_frontmatter():
    md = "---\ntype: permanent\nnote_id: abc\n---\n\n# Title\n\nBody text here."
    result = extract_embeddable_text(md)
    assert "type:" not in result
    assert "note_id:" not in result
    assert "Body text here" in result


def test_extract_embeddable_text_strips_managed_blocks():
    md = (
        "# Title\n\nBody text\n\n"
        "<!-- zettel:auto-backlinks:start -->\n"
        "- [[some link]]\n"
        "<!-- zettel:auto-backlinks:end -->\n"
    )
    result = extract_embeddable_text(md)
    assert "some link" not in result
    assert "Body text" in result
