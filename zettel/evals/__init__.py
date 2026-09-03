"""Offline evaluation harness for `zettel ask` — routing vs. representation.

Research code, deliberately kept out of the production path: nothing under
`zettel harvest|extract|connect|ask` imports this package. It reads only what
`AskResult` already exposes publicly (`hits`, `candidates`, `floor_reason`,
`retrieval_params`), so an experiment can never quietly depend on an internal.

A null result is a valid result. Nothing here changes production behaviour;
`docs/adrs` records that any claim comparing conditions needs an identical
question/model envelope on both sides.
"""

from .manifest import RunManifest, manifest_identity
from .score import QuestionScore, RunScore, Verdict, score_question, score_run

__all__ = [
    "QuestionScore",
    "RunManifest",
    "RunScore",
    "Verdict",
    "manifest_identity",
    "score_question",
    "score_run",
]
