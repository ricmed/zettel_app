"""Reflective reading (#396, ADR-069): reading mode, prompt routing, reflective frontmatter."""

from __future__ import annotations

import pytest
from zettel.extractor import Prompt1Set, _reading_modes
from zettel.library import book_file as bf
from zettel.library.mirror import refresh
from zettel.library.reading_mode import effective_nature, effective_reading_mode
from zettel.library.reading_notes import REFLECTION_KINDS
from zettel.library.schema import load_library_schema
from zettel.schemas import REFLECTION_KINDS as CANDIDATE_KINDS
from zettel.schemas import PermanentNoteCandidate
from zettel.vault import reflection_frontmatter

from tests.library_support import REPO, library_config, library_db

SCHEMA = load_library_schema(REPO / "config" / "library.yaml")


@pytest.mark.parametrize(
    ("meta", "nature", "mode"),
    [
        ({"genres": ["Fantasia"]}, "ficcao", "reflexao"),
        ({"genres": ["Técnico"]}, "nao_ficcao", "conceito"),
        ({"genres": ["Filosofia"]}, "nao_ficcao", "reflexao"),
        ({"genres": ["Quadrinho", "Biografia"]}, None, "reflexao"),
        # Neutro reflexivo decide só quando o livro não é não ficção.
        ({"genres": ["Clássico"]}, None, "reflexao"),
        ({"genres": ["Clássico", "Técnico"]}, "nao_ficcao", "conceito"),
        ({"genres": ["Crônica"]}, None, "reflexao"),
        ({"genres": ["Fantasia"], "reading_mode": "conceito"}, "ficcao", "conceito"),
        ({"genres": ["Técnico"], "nature": "ficcao"}, "ficcao", "reflexao"),
        ({}, None, "conceito"),
    ],
)
def test_reading_mode_follows_the_book(meta, nature, mode):
    assert effective_nature(meta, SCHEMA) == nature
    assert effective_reading_mode(meta, SCHEMA) == mode


def test_kinds_offered_on_the_web_are_the_candidate_kinds():
    assert set(REFLECTION_KINDS) == set(CANDIDATE_KINDS)


def test_candidate_reflection_fields_are_optional_and_cleaned():
    cand = PermanentNoteCandidate(
        thesis="t",
        definition="d",
        reflection_kind="inventado",
        characters=[" Bilbo ", "", "Bilbo", "Gandalf", "a", "b", "c", "d"],
    )
    assert cand.reflection_kind == ""
    assert cand.characters == ["Bilbo", "Gandalf", "a", "b", "c"]
    assert reflection_frontmatter(PermanentNoteCandidate(thesis="t", definition="d")) == {}
    reflective = PermanentNoteCandidate(
        thesis="t", definition="d", reflection_kind="metafora", characters=["Paul"]
    )
    assert reflection_frontmatter(reflective) == {
        "reading_mode": "reflexao",
        "reflection_kind": "metafora",
        "characters": ["Paul"],
    }


def test_extract_picks_the_prompt_by_the_source_reading_mode(tmp_path):
    cfg = library_config(tmp_path)
    cfg.prompts_path = REPO / "prompts"
    db = library_db(cfg)
    novel = bf.create_book(cfg, {"title": "O Hobbit", "genres": ["Fantasia"]})
    novel.meta["source_id"] = "@Tolkien1937Hobbit"
    bf.save_book(cfg, novel, expected_version=novel.version)
    manual = bf.create_book(cfg, {"title": "Código Limpo", "genres": ["Técnico"]})
    manual.meta["source_id"] = "@Martin2008Codigo"
    bf.save_book(cfg, manual, expected_version=manual.version)

    modes = _reading_modes(cfg, db)
    assert modes == {"@Tolkien1937Hobbit": "reflexao"}

    prompts = Prompt1Set(cfg)
    reflective, reflective_hash, reflective_examples = prompts.for_mode("reflexao")
    conceptual, conceptual_hash, _ = prompts.for_mode(modes.get("@Martin2008Codigo", "conceito"))
    assert "Nota de Literatura Reflexiva" in reflective.system
    assert "Nota de Literatura Reflexiva" not in conceptual.system
    assert reflective_hash != conceptual_hash
    assert "heroi_improvavel" in reflective_examples["tag_examples"]


def test_a_broken_vocabulary_leaves_every_source_conceptual(tmp_path):
    cfg = library_config(tmp_path, rating_path=tmp_path / "nada.yaml")
    db = library_db(cfg)
    assert _reading_modes(cfg, db) == {}


@pytest.mark.slow
def test_a_reflective_book_goes_through_run_all_with_its_own_prompts(
    cli_instance, monkeypatch: pytest.MonkeyPatch
):
    """The contract: LIV in reflexao -> reflective Prompt 1 and 2 -> reflective ZTL frontmatter."""
    from tests import cli_support
    from tests.cli_support import install_fake_llm, source_markdown

    instance = cli_instance
    (instance.inbox / "capital.md").write_text(source_markdown(), encoding="utf-8")
    calls = install_fake_llm(monkeypatch)
    systems: dict[str, list[str]] = {}
    original_invoke = cli_support._FakeChat.invoke

    def spying_invoke(self, messages, **kwargs):
        systems.setdefault(self.phase, []).append(str(messages[0].content))
        return original_invoke(self, messages, **kwargs)

    monkeypatch.setattr(cli_support._FakeChat, "invoke", spying_invoke)
    original_extract = cli_support.ANSWERS["extract"]

    def reflective_extract(user):
        answer = original_extract(user)
        for cand in answer["candidates"]:
            cand.update(reflection_kind="pensamento", characters=["Karl Marx"])
        return answer

    monkeypatch.setitem(cli_support.ANSWERS, "extract", reflective_extract)

    # The book is on the shelf before its file is harvested: harvest links it.
    from zettel.config import load_config

    cfg = load_config(instance.config)
    book = bf.create_book(
        cfg, {"title": "O Capital", "authors": ["Karl Marx"], "reading_mode": "reflexao"}
    )
    instance.run("run-all", "--yes", "--auto-approve")

    assert bf.load_book(cfg, book.book_id).source_id
    assert systems["extract"] and all(
        "Nota de Literatura Reflexiva" in s for s in systems["extract"]
    )
    assert systems["connect"] and all("Nota Permanente Reflexiva" in s for s in systems["connect"])
    from zettel.vault import parse_frontmatter

    ztls = sorted((instance.vault / "30_Permanent").glob("ZTL - *.md"))
    assert ztls, calls
    for path in ztls:
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["reading_mode"] == "reflexao"
        assert meta["reflection_kind"] == "pensamento"
        assert meta["characters"] == ["Karl Marx"]
    db = instance.db()
    try:
        refresh(db, cfg)
        assert db.list_reflective_source_ids() == [bf.load_book(cfg, book.book_id).source_id]
    finally:
        db.close()
