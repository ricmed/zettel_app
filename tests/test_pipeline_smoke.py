"""One Markdown source crosses the real pipeline: the contract between phases (#322).

Every other test seeds the state a phase is supposed to receive. This one lets
each phase receive what the previous one actually wrote: ``zettel run-all`` over
a two-chapter Markdown, with the operational ``config/config.yaml`` (paths moved
to ``tmp_path``, the decision shadow off), a fixed LLM answer per phase and a
deterministic token-hash embedding behind the real ``VectorIndex`` and Chroma.

If a phase renames a status, a ``candidate_json`` field or the link from a
concept to its chunk, the permanent note below stops being written or stops
citing its literature note, and this test fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.cli_support import CHAPTERS, chapter_in, install_fake_llm, source_markdown


@pytest.fixture
def pipeline(cli_instance, monkeypatch: pytest.MonkeyPatch):
    """A project root in ``tmp_path`` running the operational config offline."""
    instance = cli_instance
    (instance.inbox / "capital.md").write_text(source_markdown(), encoding="utf-8")
    calls = install_fake_llm(monkeypatch)

    def run_all() -> None:
        instance.run("run-all", "--yes", "--auto-approve")

    return instance.root, run_all, calls


def _vault_bytes(vault: Path) -> dict[str, bytes]:
    return {str(p.relative_to(vault)): p.read_bytes() for p in sorted(vault.rglob("*.md"))}


@pytest.mark.slow
def test_a_markdown_source_becomes_cited_permanent_notes(pipeline):
    root, run_all, calls = pipeline
    run_all()

    from zettel.state import StateDB
    from zettel.vault import parse_frontmatter, read_managed_block

    vault = root / "vault"
    ztls = sorted((vault / "30_Permanent").glob("ZTL - *.md"))
    assert len(ztls) == len(CHAPTERS), calls

    db = StateDB(root / "data" / "state.db")
    try:
        (source,) = db.list_sources()
        citekey = source["citekey"]
        for path in ztls:
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            note = db.get_note(meta["note_id"])
            chunk = db.get_chunk(meta["chunk_id"])
            chapter = chapter_in(chunk["text"])

            # review approved the draft and connect consumed the concept it left
            noted = db.get_concepts_by_status("noted")
            assert [c["note_id"] for c in noted].count(note["note_id"]) == 1
            assert chunk["status"] in ("approved", "persisted")

            # the note cites the approved granular LIT of its own chunk ...
            lit = Path(chunk["literature_note_path"])
            assert lit.is_file() and lit.parent.name == citekey
            assert lit.stem in body.split("## Fonte", 1)[1]

            # ... with a citation computed by code: Markdown has no pages (ADR-013, ADR-051)
            assert "page" not in meta
            provenance = json.loads(note["provenance_json"])
            assert provenance["anchor_quote"] == chapter["anchor"]
            assert provenance["citation"].startswith("(MARX, 2013")
            assert "p." not in provenance["citation"]
            assert chapter["anchor"] in read_managed_block(body, "auto-evidence")

            # the approved LIT lists the note it produced
            lit_block = read_managed_block(lit.read_text(encoding="utf-8"), "auto-lit-permanent")
            assert path.stem in lit_block
    finally:
        db.close()

    index_note = next((vault / "20_Literature").glob("LIT - *.md"))
    chapter_map = read_managed_block(index_note.read_text(encoding="utf-8"), "auto-chapter-map")
    for path in ztls:
        assert path.stem in chapter_map


@pytest.mark.slow
def test_a_second_run_changes_nothing_and_calls_no_llm(pipeline):
    root, run_all, calls = pipeline
    run_all()
    before = _vault_bytes(root / "vault")
    calls_before = len(calls)

    run_all()

    assert _vault_bytes(root / "vault") == before
    assert len(calls) == calls_before
