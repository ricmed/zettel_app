"""Tests for vault I/O operations."""

from zettel.vault import (
    compose_note,
    parse_frontmatter,
    read_managed_block,
    upsert_managed_block,
    _slug,
    note_filename,
)


def test_parse_frontmatter_basic():
    content = "---\ntype: permanent\nnote_id: abc123\n---\n\n# Title\n\nBody"
    meta, body = parse_frontmatter(content)
    assert meta["type"] == "permanent"
    assert meta["note_id"] == "abc123"
    assert "# Title" in body


def test_parse_frontmatter_no_frontmatter():
    content = "# Just a heading\n\nSome body text"
    meta, body = parse_frontmatter(content)
    assert meta == {}
    assert body == content


def test_compose_note():
    meta = {"type": "permanent", "note_id": "abc"}
    body = "# Title\n\nBody"
    result = compose_note(meta, body)
    assert result.startswith("---\n")
    assert "type: permanent" in result
    assert "# Title" in result


def test_read_managed_block():
    content = (
        "Some text\n"
        "<!-- zettel:auto-backlinks:start -->\n"
        "- link1\n"
        "- link2\n"
        "<!-- zettel:auto-backlinks:end -->\n"
        "More text"
    )
    block = read_managed_block(content, "auto-backlinks")
    assert block is not None
    assert "link1" in block
    assert "link2" in block


def test_read_managed_block_not_found():
    content = "No blocks here"
    result = read_managed_block(content, "auto-backlinks")
    assert result is None


def test_upsert_managed_block_insert():
    content = "# Title\n\nBody text"
    result = upsert_managed_block(content, "auto-backlinks", "- new link")
    assert "<!-- zettel:auto-backlinks:start -->" in result
    assert "- new link" in result
    assert "<!-- zettel:auto-backlinks:end -->" in result


def test_upsert_managed_block_replace():
    content = (
        "# Title\n\n"
        "<!-- zettel:auto-backlinks:start -->\n"
        "- old link\n"
        "<!-- zettel:auto-backlinks:end -->\n"
    )
    result = upsert_managed_block(content, "auto-backlinks", "- new link")
    assert "old link" not in result
    assert "- new link" in result


def test_slug():
    assert _slug("Hello World! Test 123") == "hello-world-test-123"
    assert len(_slug("a" * 100)) <= 50


def test_note_filename():
    name = note_filename("ZTL", "ABC123", "My Great Note")
    assert name == "ZTL - ABC123 - my-great-note.md"
