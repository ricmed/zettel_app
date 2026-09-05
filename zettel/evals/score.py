"""Deterministic scoring of one `ask` trajectory against a gold answer.

The classification separates the two axes the research keeps confusing:
*routing* (did the target ever enter the candidate pool?) and *representation*
(having been retrieved, did it survive the floor and support an answer?).

Only observable evidence is used. Nothing here infers hidden reasoning from the
answer string beyond the declared rubric — a substring rule the gold file states
explicitly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum


class Verdict(StrEnum):
    OK = "ok"
    ROUTING_MISS = "routing_miss"  # target never reached the candidate pool
    FLOOR_REJECT = "floor_reject"  # target was in the pool, floor rejected it
    ANSWER_FAIL = "answer_fail"  # target was used, the answer missed the rubric
    UNKNOWN = "unknown"  # the gold does not name a target


@dataclass
class Trajectory:
    """What one recorded `ask` run observably did."""

    question_id: str
    question: str
    hit_ids: list[str] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    floor_reasons: dict[str, str] = field(default_factory=dict)
    passed_floor: dict[str, bool] = field(default_factory=dict)
    llm_called: bool = False
    answer: str = ""
    tokens_input: int = 0
    tokens_output: int = 0

    @classmethod
    def from_dict(cls, payload: dict) -> Trajectory:
        return cls(
            question_id=str(payload.get("question_id") or ""),
            question=str(payload.get("question") or ""),
            hit_ids=[str(x) for x in payload.get("hit_ids") or []],
            candidate_ids=[str(x) for x in payload.get("candidate_ids") or []],
            floor_reasons=dict(payload.get("floor_reasons") or {}),
            passed_floor={k: bool(v) for k, v in (payload.get("passed_floor") or {}).items()},
            llm_called=bool(payload.get("llm_called")),
            answer=str(payload.get("answer") or ""),
            tokens_input=int(payload.get("tokens_input") or 0),
            tokens_output=int(payload.get("tokens_output") or 0),
        )


@dataclass
class GoldQuestion:
    """The expectation a trajectory is judged against."""

    question_id: str
    question: str
    target_note_id: str = ""
    expect_no_evidence: bool = False
    answer_must_contain: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict) -> GoldQuestion:
        return cls(
            question_id=str(payload.get("question_id") or ""),
            question=str(payload.get("question") or ""),
            target_note_id=str(payload.get("target_note_id") or ""),
            expect_no_evidence=bool(payload.get("expect_no_evidence")),
            answer_must_contain=[str(x) for x in payload.get("answer_must_contain") or []],
        )


@dataclass
class QuestionScore:
    """One question's verdict plus the observations behind it."""

    question_id: str
    verdict: str
    target_note_id: str = ""
    target_in_candidates: bool = False
    target_in_hits: bool = False
    target_passed_floor: bool = False
    floor_reason: str = ""
    llm_called: bool = False
    n_hits: int = 0
    n_candidates: int = 0
    tokens_input: int = 0
    tokens_output: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunScore:
    """Aggregate over a question set — counts only, no derived rates."""

    questions: list[QuestionScore] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        tally = dict.fromkeys((v.value for v in Verdict), 0)
        for q in self.questions:
            tally[q.verdict] = tally.get(q.verdict, 0) + 1
        return tally

    def as_dict(self) -> dict:
        return {
            "counts": self.counts,
            "questions": [q.as_dict() for q in self.questions],
        }


def score_question(gold: GoldQuestion, trajectory: Trajectory) -> QuestionScore:
    """Classify one trajectory. See :class:`Verdict` for what each outcome means."""
    target = gold.target_note_id
    in_candidates = target in trajectory.candidate_ids if target else False
    in_hits = target in trajectory.hit_ids if target else False
    passed = trajectory.passed_floor.get(target, in_hits) if target else False

    score = QuestionScore(
        question_id=gold.question_id,
        verdict=Verdict.UNKNOWN.value,
        target_note_id=target,
        target_in_candidates=in_candidates,
        target_in_hits=in_hits,
        target_passed_floor=bool(passed),
        floor_reason=trajectory.floor_reasons.get(target, ""),
        llm_called=trajectory.llm_called,
        n_hits=len(trajectory.hit_ids),
        n_candidates=len(trajectory.candidate_ids),
        tokens_input=trajectory.tokens_input,
        tokens_output=trajectory.tokens_output,
    )

    if gold.expect_no_evidence:
        # The point of a no-evidence question is that the LLM is never called.
        score.verdict = (
            Verdict.OK.value
            if not trajectory.hit_ids and not trajectory.llm_called
            else Verdict.ANSWER_FAIL.value
        )
        return score

    if not target:
        return score  # UNKNOWN: nothing to measure against

    if not in_candidates:
        score.verdict = Verdict.ROUTING_MISS.value
        return score
    if not in_hits or not passed:
        score.verdict = Verdict.FLOOR_REJECT.value
        return score

    score.verdict = (
        Verdict.OK.value if _rubric_holds(gold, trajectory.answer) else Verdict.ANSWER_FAIL.value
    )
    return score


def _rubric_holds(gold: GoldQuestion, answer: str) -> bool:
    """The only answer check: every declared substring is present, case-folded."""
    if not gold.answer_must_contain:
        return True
    folded = answer.casefold()
    return all(term.casefold() in folded for term in gold.answer_must_contain)


def score_run(
    gold_questions: list[GoldQuestion],
    trajectories: list[Trajectory],
) -> RunScore:
    """Score a whole question set. A question with no trajectory is a routing miss."""
    by_id = {t.question_id: t for t in trajectories}
    return RunScore(
        questions=[
            score_question(
                gold,
                by_id.get(gold.question_id, Trajectory(gold.question_id, gold.question)),
            )
            for gold in sorted(gold_questions, key=lambda g: g.question_id)
        ]
    )
