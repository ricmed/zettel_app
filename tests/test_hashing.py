"""Tests for hashing utilities."""

from zettel.hashing import (
    compute_embedding_input_hash,
    extract_embeddable_text,
    normalize_text_for_hash,
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


def test_normalize_dehyphenation():
    text = "experi-\nmental"
    result = normalize_text_for_hash(text)
    assert result == "experimental"


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
