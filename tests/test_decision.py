"""Typed decision layer, shadow mode (ADR-055). No test reaches the network."""

from types import SimpleNamespace

import pytest
from zettel.config import AppConfig, DecisionConfig, DecisionSitesConfig
from zettel.decision import client as decision_client
from zettel.decision import permute, shadow, sites
from zettel.decision.client import DecisionClient, DecisionResult
from zettel.state import StateDB

# ── Fakes ────────────────────────────────────────────────────────────────


class FakeClient:
    """Answers every question deterministically; the first option of a choice leads."""

    model = "typesafe/jev-test"

    def __init__(self, error: str = "") -> None:
        self.calls: list[dict] = []
        self.error = error

    def ask(self, state, questions, *, label):
        self.calls.append({"state": state, "questions": questions, "label": label})
        if self.error:
            return DecisionResult(error=self.error)
        answers = {}
        for name, q in questions.items():
            if q["type"] == "choice":
                keys = list(q["criteria"])
                probs = {k: (0.7 if i == 0 else 0.3 / (len(keys) - 1)) for i, k in enumerate(keys)}
                answers[name] = {
                    "type": "choice",
                    "choice": keys[0],
                    "probabilities": probs,
                    "confidence": 0.6,
                }
            elif q["type"] == "score":
                answers[name] = {"type": "score", "score": 3.0, "confidence": 0.8}
            else:
                answers[name] = {"type": "noul", "noul": 0.9}
        return DecisionResult(answers=answers, input_tokens=100, latency_ms=12)


def _cfg(tmp_path, **sites_on) -> AppConfig:
    return AppConfig(
        vault_path=tmp_path / "vault",
        state_db_path=tmp_path / "state.db",
        chroma_path=tmp_path / "chroma",
        cache_path=tmp_path / "cache",
        decision=DecisionConfig(sites=DecisionSitesConfig(**sites_on)),
    )


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(decision_client, "get_decision_client", lambda cfg: client)
    return client


@pytest.fixture
def db(tmp_path):
    database = StateDB(tmp_path / "state.db")
    yield database
    database.close()


def _same_source():
    return [
        {"id": "01NOTEA", "document": "Texto da nota A " * 40, "metadata": {"title": "Nota A"}},
        {"id": "01NOTEB", "document": "Texto da nota B", "metadata": {"title": "Nota B"}},
    ]


def _shadow_dedupe(cfg, db, **overrides):
    kwargs = {
        "concept_id": "@X::concept::1",
        "thesis": "Tese do candidato",
        "definition": "Definicao do candidato",
        "same_source": _same_source(),
        "llm_decision": "refine_existing",
        "llm_target": "01NOTEA",
    }
    kwargs.update(overrides)
    shadow.shadow_dedupe(cfg, db, **kwargs)


# ── permute ──────────────────────────────────────────────────────────────


def test_expand_rotates_choice_options_so_each_copy_leads_with_another():
    questions = {"c": {"type": "choice", "criteria": {"a": "A", "b": "B", "c": "C"}}}
    expanded = permute.expand(questions, 3)
    assert [next(iter(q["criteria"])) for q in expanded.values()] == ["a", "b", "c"]
    assert all(set(q["criteria"]) == {"a", "b", "c"} for q in expanded.values())


def test_expand_leaves_noul_and_score_alone_and_caps_copies_at_options():
    questions = {
        "n": {"type": "noul", "instructions": "?"},
        "s": {"type": "score", "criteria": ["x", "y", "z"]},
        "c": {"type": "choice", "criteria": {"a": "A", "b": "B"}},
    }
    expanded = permute.expand(questions, 5)
    assert expanded["n"] is questions["n"]
    assert expanded["s"] is questions["s"]
    assert sorted(k for k in expanded if k.startswith("c")) == ["c@p0", "c@p1"]


def test_collapse_averages_copies_and_measures_spread():
    questions = {"c": {"type": "choice", "criteria": {"a": "A", "b": "B"}}}
    answers = {
        "c@p0": {"choice": "a", "probabilities": {"a": 0.8, "b": 0.2}, "confidence": 0.6},
        "c@p1": {"choice": "a", "probabilities": {"a": 0.6, "b": 0.4}, "confidence": 0.2},
    }
    folded = permute.collapse(questions, answers)["c"]
    assert folded["choice"] == "a"
    assert folded["probabilities"] == {"a": 0.7, "b": 0.3}
    assert folded["confidence"] == 0.4
    assert folded["spread"] == pytest.approx(0.1)
    assert folded["copy_agreement"] == 1.0


def test_collapse_reports_disagreement_between_copies():
    questions = {"c": {"type": "choice", "criteria": {"a": "A", "b": "B"}}}
    answers = {
        "c@p0": {"choice": "a", "probabilities": {"a": 0.9, "b": 0.1}, "confidence": 0.8},
        "c@p1": {"choice": "b", "probabilities": {"a": 0.2, "b": 0.8}, "confidence": 0.6},
    }
    folded = permute.collapse(questions, answers)["c"]
    assert folded["choice"] == "a"
    assert folded["copy_agreement"] == 0.5


# ── sites ────────────────────────────────────────────────────────────────


def test_dedupe_collapses_refine_and_merge_and_offers_none_target():
    decision = sites.dedupe(
        thesis="t",
        definition="d",
        existing=[{"id": "01A", "title": "A", "text": "x"}],
        lang="en",
    )
    assert tuple(decision.questions["decision"]["criteria"]) == sites.DEDUPE_OPTIONS
    assert list(decision.questions["target"]["criteria"]) == ["01A", sites.NONE_OPTION]
    assert sites.dedupe_baseline_decision("merge") == "link"
    assert sites.dedupe_baseline_decision("refine_existing") == "link"
    assert sites.dedupe_baseline_decision("ignore") == "ignore"


def test_instructions_follow_language_but_content_does_not():
    en = sites.dedupe(thesis="Tese", definition="Def", existing=[], lang="en")
    pt = sites.dedupe(thesis="Tese", definition="Def", existing=[], lang="pt")
    assert en.state == pt.state
    assert en.questions["decision"]["instructions"] != pt.questions["decision"]["instructions"]


def test_moc_category_offers_every_category_plus_none():
    decision = sites.moc_category(
        notes=[{"title": "n", "text": "t"}],
        terms=["x"],
        categories=[("Pilar", "Cat A", ["t1"]), ("Pilar", "Cat B", [])],
        lang="en",
    )
    criteria = decision.questions["category"]["criteria"]
    assert list(criteria) == ["Cat A", "Cat B", sites.NONE_OPTION]
    assert criteria["Cat A"] == "Pilar: Cat A (t1)"


@pytest.mark.parametrize("dimension", sites.JUDGE_DIMENSIONS)
def test_judge_dimension_is_a_five_level_score(dimension):
    decision = sites.judge_dimension(
        dimension, topic="T", style="blog", article="A", catalog="C", lang="pt"
    )
    question = decision.questions[dimension]
    assert question["type"] == "score"
    assert len(question["criteria"]) == sites.JUDGE_TOP_LEVEL + 1


def test_judge_states_are_minimal():
    def state(dim):
        return sites.judge_dimension(
            dim, topic="T", style="blog", article="A", catalog="C", lang="en"
        ).state

    assert state("naturalness") == {"article": "A"}
    assert "notes" not in state("references")
    assert state("fidelity") == {"article": "A", "notes": "C"}


def test_judge_score_maps_to_the_llm_scale():
    assert sites.judge_score_0_10(4.0) == 10.0
    assert sites.judge_score_0_10(2.0) == 5.0


def test_gold_builders_ask_keep_as_noul():
    extract = sites.extract_gold(source_title="S", passage="P", lang="en")
    assert extract.questions["keep"]["type"] == "noul"
    assert tuple(extract.questions["category"]["criteria"]) == sites.EXTRACT_CATEGORIES
    reader = sites.reader_gold(source_title="S", passage="P", notes=[], lang="pt")
    assert reader.questions["quality"]["type"] == "score"
    assert len(reader.questions["quality"]["criteria"]) == 5


# ── client ───────────────────────────────────────────────────────────────


def test_client_fails_open_and_decides_unavailability_once(monkeypatch):
    builds = []

    def refuse(dcfg):
        builds.append(dcfg)
        raise decision_client.DecisionUnavailable("TYPESAFE_API_KEY ausente")

    monkeypatch.setattr(decision_client, "_build_sdk_client", refuse)
    client = DecisionClient(DecisionConfig())
    first = client.ask({"x": 1}, {"n": {"type": "noul"}}, label="t")
    second = client.ask({"x": 1}, {"n": {"type": "noul"}}, label="t")
    assert not first.ok and "TYPESAFE_API_KEY" in first.error
    assert second.error == first.error
    assert len(builds) == 1


def test_client_converts_sdk_errors_into_results(monkeypatch):
    import typesafe_sdk as sdk

    class Broken:
        def system_one(self, state, questions):
            raise sdk.TypeSafeError("boom")

    monkeypatch.setattr(decision_client, "_build_sdk_client", lambda dcfg: Broken())
    result = DecisionClient(DecisionConfig()).ask({}, {"n": {"type": "noul"}}, label="t")
    assert "boom" in result.error


def test_client_returns_answers_and_records_usage(monkeypatch):
    from zettel.usage import begin_run, get_tracker

    class Answer:
        def __init__(self, payload):
            self.payload = payload

        def model_dump(self, mode="python"):
            return self.payload

    class Fine:
        def system_one(self, state, questions):
            assert set(questions) == {"n"}
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=1_000_000),
                answers={"n": Answer({"type": "noul", "noul": 0.75})},
            )

    monkeypatch.setattr(decision_client, "_build_sdk_client", lambda dcfg: Fine())
    begin_run()
    result = DecisionClient(DecisionConfig()).ask({}, {"n": {"type": "noul"}}, label="dedupe")
    assert result.ok
    assert result.answers == {"n": {"type": "noul", "noul": 0.75}}
    event = get_tracker().events[-1]
    assert event.label == "jev:dedupe"
    assert event.cost_usd == pytest.approx(0.042)


# ── shadow ───────────────────────────────────────────────────────────────


def test_shadow_off_never_asks(tmp_path, db, fake):
    _shadow_dedupe(_cfg(tmp_path), db)
    assert fake.calls == []
    assert db.list_decision_shadow() == []


def test_shadow_dedupe_records_baseline_and_folded_answer(tmp_path, db, fake):
    _shadow_dedupe(_cfg(tmp_path, dedupe="shadow"), db)
    (row,) = db.list_decision_shadow("dedupe")
    assert row["baseline"] == {
        "decision": "link",
        "llm_decision": "refine_existing",
        "target": "01NOTEA",
    }
    assert set(row["jev"]) == {"decision", "target"}
    assert row["jev"]["decision"]["copies"] == 3
    assert row["model"] == "typesafe/jev-test"
    assert row["error"] is None
    # The row keeps exactly what the model was shown (the blind sheet is built from it).
    assert row["state"] == fake.calls[0]["state"]
    # The model saw the same 200-char excerpt the dedupe LLM sees.
    note_a = fake.calls[0]["state"]["existing_notes"][0]
    assert len(note_a["text"]) == shadow.DEDUPE_NOTE_CHARS


def test_shadow_reuses_a_stored_answer_and_refreshes_the_baseline(tmp_path, db, fake):
    cfg = _cfg(tmp_path, dedupe="shadow")
    _shadow_dedupe(cfg, db)
    _shadow_dedupe(cfg, db, llm_decision="ignore", llm_target=None)
    assert len(fake.calls) == 1
    (row,) = db.list_decision_shadow("dedupe")
    assert row["baseline"]["decision"] == "ignore"
    assert row["baseline"]["target"] == sites.NONE_OPTION


def test_shadow_retries_after_an_error(tmp_path, db, monkeypatch):
    cfg = _cfg(tmp_path, dedupe="shadow")
    broken = FakeClient(error="429")
    monkeypatch.setattr(decision_client, "get_decision_client", lambda c: broken)
    _shadow_dedupe(cfg, db)
    assert db.list_decision_shadow()[0]["error"] == "429"

    fine = FakeClient()
    monkeypatch.setattr(decision_client, "get_decision_client", lambda c: fine)
    _shadow_dedupe(cfg, db)
    assert len(fine.calls) == 1
    assert db.list_decision_shadow()[0]["error"] is None


def test_shadow_skips_a_state_too_large_to_ask(tmp_path, db, fake):
    huge = [{"id": "01A", "document": "x", "metadata": {"title": "y" * 200_000}}]
    _shadow_dedupe(_cfg(tmp_path, dedupe="shadow"), db, same_source=huge)
    assert fake.calls == []
    assert db.list_decision_shadow()[0]["error"] == "skipped:too_large"


def test_reviewer_decision_labels_the_shadow_row(tmp_path, db, fake):
    from zettel.review import discard_duplicate

    _shadow_dedupe(_cfg(tmp_path, dedupe="shadow"), db)
    discard_duplicate(db, "@X::concept::1")
    assert db.list_decision_shadow()[0]["human"] == {"verdict": "ignore"}
    # A later refresh of the baseline keeps the human label.
    _shadow_dedupe(_cfg(tmp_path, dedupe="shadow"), db, llm_decision="ignore")
    assert db.list_decision_shadow()[0]["human"] == {"verdict": "ignore"}


def test_shadow_moc_category_reads_cluster_notes(tmp_path, db, fake):
    for nid in ("01B", "01A"):
        db.upsert_note(nid, "@S", None, title=f"Titulo {nid}", body=f"Corpo {nid}")
    cfg = _cfg(tmp_path, moc_category="shadow")
    cfg.gardener.topics_path = None
    cfg.gardener.allowed_topics = ["Cat A", "Cat B"]
    shadow.shadow_moc_category(cfg, db, category="Cat B", note_ids=["01B", "01A"], terms=["t"])
    (row,) = db.list_decision_shadow("moc_category")
    assert row["baseline"] == {"category": "Cat B"}
    state = fake.calls[0]["state"]
    assert [n["title"] for n in state["notes"]] == ["Titulo 01A", "Titulo 01B"]
    assert "none" in fake.calls[0]["questions"]["category@p0"]["criteria"]


def test_shadow_article_judge_writes_one_row_per_dimension(tmp_path, db, fake):
    cfg = _cfg(tmp_path, article_judge="shadow")
    llm_scores = {
        "fidelity": 8.0,
        "coverage": 7.0,
        "references": 6.0,
        "naturalness": 5.0,
        "average": 6.5,
        "verdict": "REJECTED",
    }
    shadow.shadow_article_judge(
        cfg, db, topic="T", style="blog", catalog="C", body="Artigo", llm_scores=llm_scores
    )
    rows = db.list_decision_shadow("article_judge")
    assert len(rows) == 4
    by_dim = {r["subject_id"].rsplit(":", 1)[1]: r for r in rows}
    assert by_dim["naturalness"]["baseline"]["score_0_10"] == 5.0
    assert by_dim["fidelity"]["jev"]["fidelity"]["score"] == 3.0
    assert {c["label"] for c in fake.calls} == {"article_judge"}
