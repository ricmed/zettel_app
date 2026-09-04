"""`zettel skill` — flat Agent Skill export from an approved vault slice (ADR-035).

The export is a deterministic projection: same fixture in, same bytes out, no LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zettel.config import AppConfig
from zettel.skill_export import (
    SkillExportError,
    estimate_tokens,
    render_skill_md,
    run_skill_export,
)
from zettel.state import StateDB
from zettel.topic_index import TermSource, build_term_map

# ── Fixture vault ──────────────────────────────────────────────────────


def _note_body(thesis: str, limits: str = "") -> str:
    body = f"> **Tese**: {thesis}\n\n## Definição\n\nExplicação autônoma.\n"
    if limits:
        body += f"\n## Limites\n\n{limits}\n"
    body += (
        "\n## Trecho da fonte\n\n"
        "<!-- zettel:auto-source-excerpt:start -->\n"
        "TEXTO LITERAL DA FONTE PROTEGIDO POR DIREITO AUTORAL\n"
        "<!-- zettel:auto-source-excerpt:end -->\n"
    )
    return body


def _add_source(database, source_id: str, citekey: str, title: str) -> None:
    database.upsert_source(
        source_id, citekey=citekey, title=title, authors=["Autor"], year=2020,
        file_checksum=f"chk-{citekey}", origin_path=f"/inbox/{citekey}.pdf",
        origin_type="pdf",
    )


NOTE_A = "01HAAAAAAAAAAAAAAAAAAAAAAA"
NOTE_B = "01HBBBBBBBBBBBBBBBBBBBBBBB"
NOTE_C = "01HCCCCCCCCCCCCCCCCCCCCCCC"


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(vault_path=tmp_path / "vault")


@pytest.fixture
def db(tmp_path: Path, cfg: AppConfig):
    database = StateDB(tmp_path / "state.db")
    _add_source(database, "@Autor2020Livro", "Autor2020Livro", "Livro de Teste")
    database.upsert_chapter("@Autor2020Livro::ch000", "@Autor2020Livro", "Cap", "chk")
    database.upsert_chunk(
        "@Autor2020Livro::ch000::aaaa1111", "@Autor2020Livro",
        "@Autor2020Livro::ch000", "texto", "chk1",
    )
    permanent = cfg.vault_path / "30_Permanent"
    permanent.mkdir(parents=True, exist_ok=True)

    specs = [
        (NOTE_A, "Dropout como ensemble", "Dropout treina sub-redes e as media na inferencia",
         "Falha quando a rede ja e pequena", 5, ["regularizacao", "dropout"], ["The 5 Whys"]),
        (NOTE_B, "Batch norm e covariate shift", "Batch norm reduz covariate shift interno",
         "", 4, ["normalizacao"], []),
        (NOTE_C, "Attention sem recorrencia", "Attention modela dependencias longas sem recorrencia",
         "", 3, ["attention"], []),
    ]
    for note_id, title, thesis, limits, relevance, tags, frameworks in specs:
        path = permanent / f"ZTL - {note_id} - nota.md"
        path.write_text("corpo", encoding="utf-8")
        meta = {"type": "permanent", "title": title, "source_id": "@Autor2020Livro",
                "source_locator": "p.10", "tags": tags}
        if frameworks:
            meta["named_frameworks"] = frameworks
        if note_id == NOTE_A:
            meta["decision_rules"] = ["Quando a rede for grande, use dropout, porque reduz variancia"]
            meta["anti_patterns"] = ["O que evitar: dropout na inferencia - por que falha: escala errada"]
        database.upsert_note(
            note_id=note_id, source_id="@Autor2020Livro", path=str(path),
            title=title, body=_note_body(thesis, limits),
            frontmatter_json=json.dumps(meta, ensure_ascii=False),
        )
        database.upsert_concept(
            f"cid-{note_id}", "@Autor2020Livro", "@Autor2020Livro::ch000::aaaa1111",
            note_id=note_id,
            candidate_json=json.dumps({"relevance_score": relevance}),
        )
    database.upsert_note_connection(NOTE_A, NOTE_B, "contradicts", "tensao")
    yield database
    database.close()


def _export(cfg, db, tmp_path, **kwargs):
    return run_skill_export(cfg, db, out=tmp_path / "packs", **kwargs)


# ── Selector contract ──────────────────────────────────────────────────


def test_requires_exactly_one_selector(cfg, db, tmp_path):
    with pytest.raises(SkillExportError, match="exatamente um seletor"):
        _export(cfg, db, tmp_path)
    with pytest.raises(SkillExportError, match="exatamente um seletor"):
        _export(cfg, db, tmp_path, source_id="@Autor2020Livro", topic="X")


def test_unknown_source_fails(cfg, db, tmp_path):
    with pytest.raises(SkillExportError, match="Fonte nao encontrada"):
        _export(cfg, db, tmp_path, source_id="@NaoExiste")


def test_unknown_moc_fails(cfg, db, tmp_path):
    with pytest.raises(SkillExportError, match="MOC nao encontrado"):
        _export(cfg, db, tmp_path, moc_id="01HZZZZZZZZZZZZZZZZZZZZZZZ")


def test_unknown_topic_lists_what_exists(cfg, db, tmp_path):
    db.upsert_moc("01HMMMMMMMMMMMMMMMMMMMMMMM", topic="Aprendizado Profundo", body="x")
    with pytest.raises(SkillExportError, match="Aprendizado Profundo"):
        _export(cfg, db, tmp_path, topic="Astrofisica")


def test_ambiguous_topic_lists_candidates(cfg, db, tmp_path):
    db.upsert_moc("01HMMMMMMMMMMMMMMMMMMMMMM1", topic="Redes", body=f"[[ZTL - {NOTE_A}]]")
    db.upsert_moc("01HMMMMMMMMMMMMMMMMMMMMMM2", topic="Redes Neurais", body=f"[[ZTL - {NOTE_B}]]")
    with pytest.raises(SkillExportError, match="ambiguo"):
        _export(cfg, db, tmp_path, topic="Redes")


def test_source_id_accepts_bare_citekey(cfg, db, tmp_path):
    pack_dir, _ = _export(cfg, db, tmp_path, source_id="Autor2020Livro")
    assert pack_dir.name == "autor2020livro"


# ── Pack layout ────────────────────────────────────────────────────────


def test_creates_the_flat_layout(cfg, db, tmp_path):
    pack_dir, pack = _export(cfg, db, tmp_path, source_id="@Autor2020Livro")
    assert (pack_dir / "SKILL.md").is_file()
    assert (pack_dir / "cheatsheet.md").is_file()
    assert (pack_dir / "glossary.md").is_file()
    assert len(list((pack_dir / "notes").glob("*.md"))) == 3
    # Flat: no nested skill inside the pack.
    assert not list(pack_dir.glob("*/SKILL.md"))
    assert all(note.filename.startswith("notes/") for note in pack.notes)


def test_default_destination_is_project_local(cfg, db):
    pack_dir, _ = run_skill_export(cfg, db, source_id="@Autor2020Livro")
    assert pack_dir == cfg.vault_path / ".claude" / "skills" / "autor2020livro"
    assert (pack_dir / "SKILL.md").is_file()


def test_existing_destination_refuses_without_overwrite(cfg, db, tmp_path):
    _export(cfg, db, tmp_path, source_id="@Autor2020Livro")
    with pytest.raises(SkillExportError, match="--overwrite"):
        _export(cfg, db, tmp_path, source_id="@Autor2020Livro")


def test_overwrite_regenerates_and_drops_stale_files(cfg, db, tmp_path):
    pack_dir, _ = _export(cfg, db, tmp_path, source_id="@Autor2020Livro")
    (pack_dir / "notes" / "obsoleta.md").write_text("velho", encoding="utf-8")
    _export(cfg, db, tmp_path, source_id="@Autor2020Livro", overwrite=True)
    assert not (pack_dir / "notes" / "obsoleta.md").exists()
    assert (pack_dir / "SKILL.md").is_file()


# ── SKILL.md contents ──────────────────────────────────────────────────


def test_skill_md_frontmatter_and_indexes(cfg, db, tmp_path):
    pack_dir, pack = _export(cfg, db, tmp_path, source_id="@Autor2020Livro")
    text = (pack_dir / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert f"name: {pack.slug}\n" in text
    assert "description:" in text
    for heading in ("## Core Frameworks & Mental Models", "## Topic Index", "## Note Index"):
        assert heading in text
    # Note Index points at the real relative paths in the pack.
    for note in pack.notes:
        assert f"`{note.filename}`" in text
        assert (pack_dir / note.filename).is_file()


def test_core_is_ordered_by_relevance_and_degree(cfg, db, tmp_path):
    _, pack = _export(cfg, db, tmp_path, source_id="@Autor2020Livro")
    assert [n.note_id for n in pack.notes] == [NOTE_A, NOTE_B, NOTE_C]


def test_skill_md_stays_inside_the_token_budget(cfg, db, tmp_path):
    pack_dir, _ = _export(cfg, db, tmp_path, source_id="@Autor2020Livro")
    assert estimate_tokens((pack_dir / "SKILL.md").read_text(encoding="utf-8")) <= 4000


def test_core_truncates_but_note_index_keeps_everything():
    from zettel.skill_export import SkillNote, build_pack

    notes = [
        SkillNote(
            note_id=f"01H{i:023d}", kind="permanent",
            title=f"Nota {i}", thesis="tese " * 200, body="",
            filename=f"notes/nota-{i}.md",
        )
        for i in range(40)
    ]
    pack = build_pack(
        slug="grande", title="Grande", origin="teste",
        notes=notes, contradictions=[], generated_on="2026-09-03",
    )
    text = render_skill_md(pack)
    core = text.split("## Core Frameworks & Mental Models", 1)[1].split("## Topic Index", 1)[0]
    assert estimate_tokens(core) <= 4000
    assert "fora do Core por orçamento de contexto" in text
    # The indexes are the routing table: every note stays reachable.
    for note in notes:
        assert f"`{note.filename}`" in text


# ── Excerpts ───────────────────────────────────────────────────────────


def test_excerpts_are_omitted_by_default(cfg, db, tmp_path):
    pack_dir, pack = _export(cfg, db, tmp_path, source_id="@Autor2020Livro")
    for note in pack.notes:
        content = (pack_dir / note.filename).read_text(encoding="utf-8")
        assert "TEXTO LITERAL DA FONTE" not in content
        assert "trecho da fonte omitido" in content
        # Citekey, locator and thesis survive.
        assert "@Autor2020Livro" in content
        assert "p.10" in content


def test_include_excerpts_copies_the_source_text(cfg, db, tmp_path):
    pack_dir, pack = _export(
        cfg, db, tmp_path, source_id="@Autor2020Livro", include_excerpts=True,
    )
    content = (pack_dir / pack.notes[0].filename).read_text(encoding="utf-8")
    assert "TEXTO LITERAL DA FONTE" in content


# ── Cheatsheet and glossary ────────────────────────────────────────────


def test_cheatsheet_uses_judgement_limits_and_contradictions(cfg, db, tmp_path):
    pack_dir, _ = _export(cfg, db, tmp_path, source_id="@Autor2020Livro")
    text = (pack_dir / "cheatsheet.md").read_text(encoding="utf-8")
    assert "Quando a rede for grande, use dropout" in text
    assert "O que evitar: dropout na inferencia" in text
    assert "Falha quando a rede ja e pequena" in text
    assert "Dropout como ensemble <-> Batch norm e covariate shift" in text


def test_cheatsheet_is_honest_when_nothing_was_stated(cfg, db, tmp_path):
    _add_source(db, "@Outro2021", "Outro2021", "Outro")
    path = cfg.vault_path / "30_Permanent" / "ZTL - 01HDDDDDDDDDDDDDDDDDDDDDDD - x.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("corpo", encoding="utf-8")
    db.upsert_note(
        note_id="01HDDDDDDDDDDDDDDDDDDDDDDD", source_id="@Outro2021", path=str(path),
        title="Sem julgamento", body=_note_body("Uma tese qualquer"),
        frontmatter_json=json.dumps({"tags": ["tema"]}),
    )
    pack_dir, _ = _export(cfg, db, tmp_path, source_id="@Outro2021")
    text = (pack_dir / "cheatsheet.md").read_text(encoding="utf-8")
    assert "_Nenhuma regra registrada neste recorte._" in text
    assert "_Nenhuma contradição registrada no grafo._" in text


def test_glossary_lists_frameworks_and_tags(cfg, db, tmp_path):
    pack_dir, _ = _export(cfg, db, tmp_path, source_id="@Autor2020Livro")
    text = (pack_dir / "glossary.md").read_text(encoding="utf-8")
    assert "**The 5 Whys**" in text
    assert "**dropout**" in text


# ── Determinism ────────────────────────────────────────────────────────


def test_same_fixture_produces_identical_bytes(cfg, db, tmp_path):
    first, _pack = _export(cfg, db, tmp_path, source_id="@Autor2020Livro")
    snapshot = {
        p.relative_to(first).as_posix(): p.read_bytes()
        for p in sorted(first.rglob("*")) if p.is_file()
    }
    _export(cfg, db, tmp_path, source_id="@Autor2020Livro", overwrite=True)
    again = {
        p.relative_to(first).as_posix(): p.read_bytes()
        for p in sorted(first.rglob("*")) if p.is_file()
    }
    assert snapshot == again


# ── MOC and topic slices ───────────────────────────────────────────────


def test_moc_slice_exports_the_notes_it_links(cfg, db, tmp_path):
    db.upsert_moc(
        "01HMMMMMMMMMMMMMMMMMMMMMMM", topic="Regularizacao",
        body=f"- [[ZTL - {NOTE_A} - a]]\n- [[ZTL - {NOTE_B} - b]]\n",
    )
    _, pack = _export(cfg, db, tmp_path, moc_id="01HMMMMMMMMMMMMMMMMMMMMMMM")
    assert {n.note_id for n in pack.notes} == {NOTE_A, NOTE_B}
    assert pack.slug == "regularizacao"


def test_topic_slice_unions_matching_mocs(cfg, db, tmp_path):
    db.upsert_moc("01HMMMMMMMMMMMMMMMMMMMMMM1", topic="Redes Neurais", body=f"[[ZTL - {NOTE_A} - a]]")
    db.upsert_moc("01HMMMMMMMMMMMMMMMMMMMMMM2", topic="Redes Neurais", body=f"[[ZTL - {NOTE_C} - c]]")
    _, pack = _export(cfg, db, tmp_path, topic="Redes Neurais")
    assert {n.note_id for n in pack.notes} == {NOTE_A, NOTE_C}


def test_empty_slice_fails_loudly(cfg, db, tmp_path):
    db.upsert_moc("01HMMMMMMMMMMMMMMMMMMMMMM3", topic="Vazio", body="sem notas")
    with pytest.raises(SkillExportError, match="Recorte vazio"):
        _export(cfg, db, tmp_path, moc_id="01HMMMMMMMMMMMMMMMMMMMMMM3")


# ── Literature fallback ────────────────────────────────────────────────


def test_source_without_permanent_notes_falls_back_to_approved_lit(cfg, db, tmp_path):
    _add_source(db, "@So2022Lit", "So2022Lit", "So LIT")
    db.upsert_chapter("@So2022Lit::ch000", "@So2022Lit", "Cap", "chk")
    summary = {
        "summary": "Resumo do chunk.",
        "candidates": [{
            "thesis": "Uma tese vinda da nota de literatura",
            "relevance_score": 4,
            "source_locator": "p.7",
            "tags": ["lit"],
            "named_frameworks": ["Framework X"],
            "decision_rules": ["Quando P, faca Q, porque R"],
        }],
    }
    db.upsert_chunk(
        "@So2022Lit::ch000::abc12345", "@So2022Lit", "@So2022Lit::ch000", 0,
        "texto do chunk", "chk1",
    )
    db.update_chunk_review(
        "@So2022Lit::ch000::abc12345", status="persisted",
        literature_id="01HLLLLLLLLLLLLLLLLLLLLLLL",
        summary_json=json.dumps(summary, ensure_ascii=False),
    )
    pack_dir, pack = _export(cfg, db, tmp_path, source_id="@So2022Lit")
    assert [n.kind for n in pack.notes] == ["literature"]
    assert "Quando P, faca Q, porque R" in (pack_dir / "cheatsheet.md").read_text(encoding="utf-8")


# ── Term map (shared with the topic index) ─────────────────────────────


def test_term_map_prefers_frameworks_and_tags_over_the_thesis():
    entries = build_term_map([
        TermSource("n1", "notes/a.md", frameworks=("The 5 Whys",), tags=("causa raiz",),
                   thesis="Perguntar por que cinco vezes revela a causa"),
    ])
    # The thesis head is a fallback, not a competitor: a truncated sentence is a
    # worse key than a tag the pipeline actually assigned.
    assert [e.term for e in entries] == ["causa raiz", "The 5 Whys"]


def test_term_map_falls_back_to_the_thesis_head():
    entries = build_term_map([
        TermSource("n1", "notes/a.md", thesis="Dropout funciona como ensemble implicito de sub-redes"),
    ])
    assert [e.term for e in entries] == ["Dropout funciona como ensemble"]


def test_term_map_drops_stopwords():
    entries = build_term_map([
        TermSource("n1", "notes/a.md", tags=("que", "de", "dropout"), thesis="que de a"),
    ])
    assert [e.term for e in entries] == ["dropout"]


def test_term_map_merges_accent_and_case_variants():
    entries = build_term_map([
        TermSource("n1", "notes/a.md", tags=("Regularização",)),
        TermSource("n2", "notes/b.md", tags=("regularizacao",)),
    ])
    assert len(entries) == 1
    assert entries[0].note_ids == ["n1", "n2"]


def test_term_map_caps_notes_per_term():
    entries = build_term_map([
        TermSource(f"n{i}", f"notes/{i}.md", tags=("dropout",)) for i in range(6)
    ])
    assert len(entries[0].note_ids) == 3
