"""Order-rotated copies of each ``choice`` question, and their aggregate.

The model "leans toward the option that comes first" (jev-1.13 jaggedness
notes). Each ``choice`` is therefore asked ``n`` times **in the same request**,
with its options rotated so a different one leads each copy. The copies are
evaluated in parallel and in isolation against the same state, so this costs
latency nothing and tokens only for the extra questions.

The aggregate averages the per-option probabilities; ``spread`` -- the mean,
over options, of the standard deviation across copies -- is the stability
measure the pre-registration of #206 thresholds at 0.05. ``noul`` has no
options and ``score`` levels are ordered, so neither is permuted.
"""

from __future__ import annotations

import statistics
from typing import Any

_SEP = "@p"


def expand(questions: dict[str, dict[str, Any]], n: int) -> dict[str, dict[str, Any]]:
    """Every ``choice`` becomes ``min(n, options)`` rotated copies named ``name@pK``."""
    expanded: dict[str, dict[str, Any]] = {}
    for name, question in questions.items():
        criteria = question.get("criteria") or {}
        copies = min(n, len(criteria)) if question["type"] == "choice" else 1
        if copies <= 1:
            expanded[name] = question
            continue
        keys = list(criteria)
        for k in range(copies):
            shift = k * len(keys) // copies
            order = keys[shift:] + keys[:shift]
            expanded[f"{name}{_SEP}{k}"] = {**question, "criteria": {o: criteria[o] for o in order}}
    return expanded


def collapse(
    questions: dict[str, dict[str, Any]], answers: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Fold the copies of each permuted ``choice`` back into one answer per question."""
    folded: dict[str, dict[str, Any]] = {}
    for name, question in questions.items():
        if name in answers:
            folded[name] = answers[name]
            continue
        copies = [a for key, a in answers.items() if key.startswith(f"{name}{_SEP}")]
        if copies:
            folded[name] = _aggregate_choice(list(question["criteria"]), copies)
    return folded


def _aggregate_choice(options: list[str], copies: list[dict[str, Any]]) -> dict[str, Any]:
    per_option = {o: [float(c["probabilities"].get(o, 0.0)) for c in copies] for o in options}
    probabilities = {o: statistics.fmean(ps) for o, ps in per_option.items()}
    spread = (
        statistics.fmean(statistics.pstdev(ps) for ps in per_option.values())
        if len(copies) > 1
        else 0.0
    )
    choices = [c["choice"] for c in copies]
    return {
        "type": "choice",
        "choice": max(probabilities, key=lambda o: probabilities[o]),
        "probabilities": {o: round(p, 4) for o, p in probabilities.items()},
        "confidence": round(statistics.fmean(float(c["confidence"]) for c in copies), 4),
        "spread": round(spread, 4),
        "copies": len(copies),
        "copy_agreement": round(
            choices.count(max(set(choices), key=choices.count)) / len(choices), 4
        ),
    }
