"""Managed blocks never cost the author a word written outside them (#321).

The pipeline writes only between ``<!-- zettel:NAME:start -->`` and
``<!-- zettel:NAME:end -->``. A note whose markers for a block are not exactly
one start followed by one end is refused: rewriting it would have to guess where
the block ends, and the old guess (first start, first end after it) erased the
author's text between an orphan start and a freshly appended block.
"""

from __future__ import annotations

import logging

import pytest
from zettel.vault import (
    MalformedManagedBlock,
    malformed_managed_blocks,
    read_managed_block,
    safe_update_managed_blocks,
    upsert_managed_block,
    vault_notes_with_malformed_blocks,
)

START = "<!-- zettel:auto-backlinks:start -->"
END = "<!-- zettel:auto-backlinks:end -->"
MANUAL = "MEU TEXTO MANUAL"


def test_orphan_start_does_not_erase_the_text_after_it():
    """The reported case: the author deleted the end marker, then wrote below."""
    content = f"# T\n\n{START}\n- old\n\n{MANUAL}\n"
    with pytest.raises(MalformedManagedBlock):
        upsert_managed_block(content, "auto-backlinks", "- new")


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (f"# T\n\n{END}\n{MANUAL}\n", "end without start"),
        (f"# T\n\n{END}\n{MANUAL}\n{START}\n- x\n", "end before start"),
        (f"# T\n\n{START}\n- a\n{END}\n{START}\n- b\n{END}\n", "two blocks"),
        (f"# T\n\n{START}\n{START}\n- a\n{END}\n", "two starts"),
        (f"# T\n\n{START}\n- a\n{END}\n{MANUAL}\n{END}\n", "two ends"),
    ],
)
def test_malformed_markers_are_refused(content, reason):
    with pytest.raises(MalformedManagedBlock) as refused:
        upsert_managed_block(content, "auto-backlinks", "- new")
    assert refused.value.block_name == "auto-backlinks", reason


def test_other_blocks_markers_do_not_count():
    content = f"# T\n\n<!-- zettel:auto-connections:start -->\n- s\n\n{START}\n- a\n{END}\n"
    result = upsert_managed_block(content, "auto-backlinks", "- b")
    assert "- b" in result
    assert "<!-- zettel:auto-connections:start -->\n- s\n" in result


def test_read_of_a_malformed_block_is_none():
    assert read_managed_block(f"# T\n{START}\n- old\n{MANUAL}\n", "auto-backlinks") is None


def test_safe_update_leaves_a_malformed_note_untouched_and_warns(tmp_path, caplog):
    path = tmp_path / "note.md"
    original = f"---\nupdated_at: '2020-01-01T00:00:00'\n---\n\n# T\n\n{START}\n- old\n\n{MANUAL}\n"
    path.write_text(original, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="zettel.vault"):
        safe_update_managed_blocks(path, {"auto-backlinks": "- new"})

    assert path.read_text(encoding="utf-8") == original
    assert any(
        "auto-backlinks" in r.getMessage() and str(path) in r.getMessage() for r in caplog.records
    )


def test_safe_update_still_writes_the_well_formed_blocks_of_other_notes(tmp_path):
    good = tmp_path / "good.md"
    good.write_text(f"# T\n\n{START}\n- old\n{END}\n", encoding="utf-8")
    bad = tmp_path / "bad.md"
    bad.write_text(f"# T\n\n{START}\n- old\n{MANUAL}\n", encoding="utf-8")

    for path in (bad, good):
        safe_update_managed_blocks(path, {"auto-backlinks": "- new"})

    assert "- new" in good.read_text(encoding="utf-8")
    assert MANUAL in bad.read_text(encoding="utf-8")


def test_malformed_managed_blocks_names_each_broken_block():
    content = (
        f"# T\n{START}\n- a\n"
        "<!-- zettel:auto-connections:start -->\n- s\n<!-- zettel:auto-connections:end -->\n"
        "<!-- zettel:auto-evidence:end -->\n"
    )
    assert malformed_managed_blocks(content) == ["auto-backlinks", "auto-evidence"]


def test_well_formed_note_has_no_malformed_block():
    assert malformed_managed_blocks(f"# T\n{START}\n- a\n{END}\n") == []


def test_vault_scan_lists_only_notes_with_broken_markers(tmp_path):
    (tmp_path / "30_Permanent").mkdir()
    (tmp_path / "30_Permanent" / "ok.md").write_text(f"{START}\n{END}\n", encoding="utf-8")
    broken = tmp_path / "30_Permanent" / "broken.md"
    broken.write_text(f"{START}\n{MANUAL}\n", encoding="utf-8")

    assert vault_notes_with_malformed_blocks(tmp_path) == [(broken, ["auto-backlinks"])]
