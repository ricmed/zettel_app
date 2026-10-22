"""Every ``zettel`` command, invoked as a command (#330).

``tests/test_cli.py`` pins the command surface and the package's import rules;
the domain functions are tested on their own. What sat between them -- flag
parsing, ``load_deps`` and the operation lock, exit codes, the summaries a user
reads -- ran in no test for most commands (the CLI had 28% line coverage).

Read-only commands share one vault taken through the whole pipeline offline
(``pipelined``). Commands that write get a fresh instance each (``cli_instance``),
harvested first when they need a source. Nothing here calls a provider:
``tests.cli_support`` answers every LLM phase and the embedding is local.
"""

from __future__ import annotations

import pytest
import yaml
from zettel import index

from tests.cli_support import (
    CHAPTERS,
    CliInstance,
    install_fake_llm,
    source_markdown,
    write_operational_config,
)
from tests.fakes import token_hash_ef_builder


@pytest.fixture(scope="module")
def pipelined(tmp_path_factory):
    """One vault through ``run-all`` (two ZTL from two chapters), shared read-only."""
    patch = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("pipelined")
    config = write_operational_config(root)
    provider = yaml.safe_load(config.read_text(encoding="utf-8"))["embedding"]["provider"]
    patch.setitem(index._EF_BUILDERS, provider, token_hash_ef_builder)
    install_fake_llm(patch)
    instance = CliInstance(root=root, config=config)
    (instance.inbox / "capital.md").write_text(source_markdown(), encoding="utf-8")
    instance.run("run-all", "--yes", "--auto-approve")
    yield instance
    patch.undo()


@pytest.fixture
def harvested(cli_instance) -> tuple[CliInstance, str]:
    """A fresh instance with the two-chapter source harvested; returns its source_id."""
    (cli_instance.inbox / "capital.md").write_text(source_markdown(), encoding="utf-8")
    cli_instance.run("harvest", "--yes")
    db = cli_instance.db()
    try:
        (source,) = db.list_sources()
    finally:
        db.close()
    return cli_instance, source["source_id"]


def _source_id(instance: CliInstance) -> str:
    db = instance.db()
    try:
        return db.list_sources()[0]["source_id"]
    finally:
        db.close()


# -- maintenance -------------------------------------------------------------


def test_init_creates_the_vault_folders(cli_instance):
    output = cli_instance.run("init")
    for folder in ("00_Inbox", "10_Sources", "20_Literature", "30_Permanent", "40_MOCs"):
        assert (cli_instance.vault / folder).is_dir()
    assert "Vault" in output


def test_reindex_and_rebuild_run_on_a_harvested_source(harvested):
    instance, _sid = harvested
    assert "sources" in instance.run("reindex", "--yes")
    instance.run("rebuild", "--what", "vault", "--dry-run", "--yes")


# -- ingest ------------------------------------------------------------------


def test_harvest_creates_the_source_and_its_notes(harvested):
    instance, sid = harvested
    assert sid.startswith("@")
    assert list((instance.vault / "10_Sources").glob("SRC - *.md"))
    assert list((instance.vault / "20_Literature").glob("LIT - *.md"))


def test_harvest_reports_an_unusable_file_and_exits_1(cli_instance):
    (cli_instance.inbox / "vazio.md").write_text("---\ntitle: Vazio\n---\n\n", encoding="utf-8")
    output = cli_instance.run("harvest", "--yes", code=1)
    assert "vazio.md" in output


def test_rechunk_and_the_dumps_read_the_harvested_source(harvested, tmp_path):
    instance, sid = harvested
    instance.run("rechunk", "--source-id", sid, "--yes")
    instance.run("dump-chunks", "--source-id", sid, "--dump-dir", str(tmp_path / "chunks"))
    instance.run("dump-extraction", "--source-id", sid, "--dump-dir", str(tmp_path / "extraction"))
    assert list((tmp_path / "chunks").glob("*.md"))
    assert list((tmp_path / "extraction").glob("*.md"))


def test_set_paging_refuses_a_source_without_pages(harvested):
    """Native Markdown has no pages (ADR-013): the command says so and exits 1."""
    instance, sid = harvested
    instance.run("set-paging", "--source-id", sid, "--content-start-file", "2", code=1)


# -- curation ----------------------------------------------------------------


def test_extract_exits_1_when_the_llm_is_down(harvested, monkeypatch):
    from zettel.llm import LLMUnavailableError

    instance, _sid = harvested

    def down(*_a, **_k):
        raise LLMUnavailableError("connection refused", phase="extract")

    monkeypatch.setattr("zettel.extractor.run_extract", down)
    output = instance.run("extract", "--yes", code=1)
    assert "connection refused" in output


def test_summarize_writes_the_chapter_map(harvested, monkeypatch):
    instance, sid = harvested
    install_fake_llm(monkeypatch)
    output = instance.run("summarize", "--source-id", sid, "--yes")
    assert "2" in output
    index_note = next((instance.vault / "20_Literature").glob("LIT - *.md"))
    assert "Resumo do trecho." in index_note.read_text(encoding="utf-8")


def test_review_with_nothing_to_review_exits_0(harvested):
    instance, _sid = harvested
    instance.run("review", "--yes")


def test_links_rejects_a_malformed_edge_and_an_unknown_one(harvested):
    instance, _sid = harvested
    assert "SRC:ALVO:TIPO" in instance.run("links", "--accept", "so-um-campo", code=1)
    assert "nao encontrada" in instance.run("links", "--accept", "A:B:supports", code=1)


# -- synthesis ---------------------------------------------------------------


def test_garden_exits_1_when_the_llm_is_down(harvested, monkeypatch):
    from zettel.llm import LLMUnavailableError

    instance, _sid = harvested

    def down(*_a, **_k):
        raise LLMUnavailableError("connection refused", phase="garden")

    monkeypatch.setattr("zettel.gardener.run_garden", down)
    instance.run("garden", "--yes", code=1)


def test_reconnect_without_pipeline_notes_exits_1(harvested):
    instance, _sid = harvested
    assert "Nenhuma nota" in instance.run("reconnect", "--yes", code=1)


# -- purge -------------------------------------------------------------------


def test_purge_rejected_and_delete_source(harvested):
    instance, sid = harvested
    instance.run("purge-rejected", "--yes")
    instance.run("delete-source", sid, "--yes")
    db = instance.db()
    try:
        assert db.list_sources() == []
    finally:
        db.close()
    assert not list((instance.vault / "10_Sources").glob("SRC - *.md"))


# -- manual notes ------------------------------------------------------------


def test_new_note_then_sync_manual_then_suggest_links(cli_instance):
    cli_instance.run("init")
    cli_instance.run("new-note", "ztl", "Uma ideia escrita a mao")
    (note,) = (cli_instance.vault / "30_Permanent").glob("ZTL - *.md")
    cli_instance.run("sync-manual")
    db = cli_instance.db()
    try:
        assert len(db.list_notes()) == 1
    finally:
        db.close()
    cli_instance.run("suggest-links", str(note), "--yes")


def test_adopt_moc_of_an_unknown_moc_exits_1(cli_instance):
    cli_instance.run("adopt-moc", "NAOEXISTE", code=1)


# -- read-only commands over a pipelined vault -------------------------------


def test_status_counts_what_the_pipeline_wrote(pipelined):
    output = pipelined.run("status")
    assert "Notas permanentes" in output or "permanent" in output.lower()


def test_db_report_describes_both_stores(pipelined):
    output = pipelined.run("db-report")
    assert "state.db" in output and "Chroma" in output


def test_catalog_finds_the_source_of_a_subject(pipelined):
    output = pipelined.run("catalog", "mercadoria", "--yes")
    assert "Marx" in output or "Capital" in output


def test_ask_answers_from_the_vault_and_saves_where_told(pipelined, tmp_path):
    saved = tmp_path / "resposta.md"
    output = pipelined.run(
        "ask", "o que e a mercadoria", "--yes", "--show-context", "--save-to", str(saved)
    )
    assert "satisfaz necessidades" in output
    assert saved.is_file()


def test_skill_exports_the_source_slice(pipelined, tmp_path):
    output = pipelined.run("skill", "--source-id", _source_id(pipelined), "--out", str(tmp_path))
    assert "Skill gerada" in output
    assert list(tmp_path.rglob("SKILL.md"))


def test_skill_without_a_selector_exits_1(pipelined):
    pipelined.run("skill", code=1)


def test_garden_plan_writes_nothing(pipelined):
    before = sorted(p.name for p in (pipelined.vault / "40_MOCs").glob("*.md"))
    pipelined.run("garden", "--plan", "--yes")
    assert sorted(p.name for p in (pipelined.vault / "40_MOCs").glob("*.md")) == before


def test_reconnect_dry_run_lists_and_writes_nothing(pipelined):
    output = pipelined.run("reconnect", "--dry-run", "--yes")
    assert "dry-run" in output


def test_links_lists_nothing_pending_after_a_run_without_edges(pipelined):
    assert "Nenhuma aresta" in pipelined.run("links")


def test_the_pipelined_vault_has_one_note_per_chapter(pipelined):
    assert len(list((pipelined.vault / "30_Permanent").glob("ZTL - *.md"))) == len(CHAPTERS)


@pytest.mark.slow
def test_doctor_reports_without_crashing(pipelined):
    """Doctor imports docling and torch to check them, hence slow."""
    output = pipelined.run("doctor", code=None)
    assert "Doctor" in output


# -- the LLM commands outside the pipeline leave without a traceback ----------


@pytest.mark.parametrize(
    ("command", "target", "args"),
    [
        ("ask", "zettel.ask.run_ask", ["uma pergunta", "--yes", "--no-save-prompt"]),
        ("article", "zettel.article_graph.run_article_graph", ["um tema", "--yes"]),
    ],
)
def test_ask_and_article_exit_1_with_the_classified_message_when_the_llm_is_down(
    cli_instance, monkeypatch, command, target, args
):
    """Like extract, connect and garden: a message and exit 1, not a traceback (#330)."""
    from zettel.llm import LLMUnavailableError

    def down(*_a, **_k):
        raise LLMUnavailableError("connection refused", phase=command)

    cli_instance.run("init")
    monkeypatch.setattr(target, down)
    output = cli_instance.run(command, *args, code=1)
    assert "connection refused" in output


def test_article_prints_and_saves_what_the_graph_wrote(cli_instance, monkeypatch, tmp_path):
    from zettel.article import ArticleResult

    cli_instance.run("init")
    result = ArticleResult(
        topic="valor", style="blog", title="Sobre o valor", body="# Sobre o valor\n\nTexto."
    )
    monkeypatch.setattr("zettel.article_graph.run_article_graph", lambda *_a, **_k: result)
    target = tmp_path / "artigo.md"
    output = cli_instance.run("article", "valor", "--yes", "--save-to", str(target))
    assert "Sobre o valor" in output
    assert target.is_file()
