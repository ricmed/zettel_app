"""The four verdicts, each from observable evidence only."""

from __future__ import annotations

from zettel.evals.score import (
    GoldQuestion,
    Trajectory,
    Verdict,
    score_question,
    score_run,
)

TARGET = "01HEVALAAAAAAAAAAAAAAAAAAA"
OTHER = "01HEVALBBBBBBBBBBBBBBBBBBB"


def _gold(**overrides) -> GoldQuestion:
    payload = {"question_id": "q1", "question": "pergunta", "target_note_id": TARGET}
    payload.update(overrides)
    return GoldQuestion.from_dict(payload)


def _trajectory(**overrides) -> Trajectory:
    payload = {
        "question_id": "q1",
        "question": "pergunta",
        "hit_ids": [TARGET],
        "candidate_ids": [TARGET, OTHER],
        "passed_floor": {TARGET: True, OTHER: False},
        "floor_reasons": {TARGET: "similaridade 0.88 >= piso 0.70"},
        "llm_called": True,
        "answer": "Resposta com a evidencia certa.",
    }
    payload.update(overrides)
    return Trajectory.from_dict(payload)


# ── The four classifications ───────────────────────────────────────────


def test_ok_when_the_target_is_used_and_the_rubric_holds():
    score = score_question(_gold(answer_must_contain=["evidencia"]), _trajectory())
    assert score.verdict == Verdict.OK.value
    assert score.target_in_hits and score.target_passed_floor


def test_routing_miss_when_the_target_never_reached_the_pool():
    score = score_question(_gold(), _trajectory(hit_ids=[OTHER], candidate_ids=[OTHER]))
    assert score.verdict == Verdict.ROUTING_MISS.value
    assert score.target_in_candidates is False


def test_floor_reject_when_the_target_was_retrieved_but_gated():
    score = score_question(_gold(), _trajectory(
        hit_ids=[],
        passed_floor={TARGET: False},
        floor_reasons={TARGET: "similaridade 0.62 abaixo do piso (0.70)"},
        llm_called=False,
    ))
    assert score.verdict == Verdict.FLOOR_REJECT.value
    assert score.target_in_candidates is True
    assert "abaixo do piso" in score.floor_reason


def test_answer_fail_when_the_target_was_used_but_the_rubric_missed():
    score = score_question(
        _gold(answer_must_contain=["comprimento do caminho"]), _trajectory(),
    )
    assert score.verdict == Verdict.ANSWER_FAIL.value


def test_unknown_when_the_gold_names_no_target():
    score = score_question(_gold(target_note_id=""), _trajectory())
    assert score.verdict == Verdict.UNKNOWN.value


# ── Rubric ─────────────────────────────────────────────────────────────


def test_rubric_is_case_insensitive():
    score = score_question(_gold(answer_must_contain=["EVIDENCIA"]), _trajectory())
    assert score.verdict == Verdict.OK.value


def test_no_rubric_means_retrieval_alone_decides():
    """Without a declared rubric nothing is inferred from the answer string."""
    score = score_question(_gold(), _trajectory(answer="qualquer coisa"))
    assert score.verdict == Verdict.OK.value


# ── No-evidence questions ──────────────────────────────────────────────


def test_no_evidence_is_ok_when_the_llm_was_never_called():
    gold = GoldQuestion.from_dict({
        "question_id": "q4", "question": "fora do acervo", "expect_no_evidence": True,
    })
    score = score_question(gold, _trajectory(
        question_id="q4", hit_ids=[], llm_called=False, answer="Nao encontrei evidencia.",
    ))
    assert score.verdict == Verdict.OK.value


def test_no_evidence_fails_when_the_llm_answered_anyway():
    """The behaviour worth protecting: empty hits must not reach the LLM."""
    gold = GoldQuestion.from_dict({
        "question_id": "q4", "question": "fora do acervo", "expect_no_evidence": True,
    })
    score = score_question(gold, _trajectory(question_id="q4", hit_ids=[], llm_called=True))
    assert score.verdict == Verdict.ANSWER_FAIL.value


# ── Aggregation ────────────────────────────────────────────────────────


def test_a_question_with_no_trajectory_counts_as_a_routing_miss():
    run = score_run([_gold(question_id="q9")], [])
    assert run.counts[Verdict.ROUTING_MISS.value] == 1


def test_run_is_ordered_by_question_id():
    golds = [_gold(question_id="q3"), _gold(question_id="q1"), _gold(question_id="q2")]
    run = score_run(golds, [])
    assert [q.question_id for q in run.questions] == ["q1", "q2", "q3"]


def test_counts_cover_every_verdict_even_at_zero():
    run = score_run([_gold()], [_trajectory()])
    assert set(run.counts) == {v.value for v in Verdict}
