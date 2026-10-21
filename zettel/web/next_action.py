"""The "next action" banner: the earliest pipeline phase with work waiting (#316).

The user is the orchestrator of harvest -> extract -> review -> connect ->
edge review -> garden; the dashboard had the counts but not the next step.
``next_action`` is pure (counts in, one action out) so the rule is tested on
its own; ``guidance_counts`` reads the counts, and the phase's own
``preconditions.blocker`` turns a missing credential into a pointer to the
settings instead of a button that would refuse.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NextAction:
    label: str
    detail: str
    href: str


# (count key, pipeline operation the blocker knows, label, detail template, href)
_STEPS = (
    (
        "chunks_pending",
        "extract",
        "Rodar o extract",
        "{n} chunk(s) aguardam o extract.",
        "/pipeline",
    ),
    (
        "chunks_awaiting_review",
        None,
        "Revisar os drafts",
        "{n} drafts LIT aguardam aprovação.",
        "/review",
    ),
    (
        "concepts_dedupe_pending",
        None,
        "Decidir as duplicatas",
        "{n} conceito(s) marcados como duplicata aguardam decisão.",
        "/review?queue=duplicates",
    ),
    (
        "connect_queue",
        "connect",
        "Rodar o connect",
        "{n} conceito(s) aprovados aguardam o connect para virar nota permanente.",
        "/pipeline",
    ),
    (
        "links_proposed",
        None,
        "Revisar as arestas propostas",
        "{n} arestas propostas pelo LLM aguardam revisão.",
        "/review?queue=links",
    ),
    (
        "notes_since_garden",
        "garden",
        "Rodar o garden",
        "{n} nota(s) nova(s) desde o último garden ainda não entraram nos MOCs.",
        "/pipeline",
    ),
)


def next_action(
    counts: Mapping[str, int],
    *,
    active_job: Mapping[str, Any] | None = None,
    blocker: Callable[[str], str | None] | None = None,
) -> NextAction | None:
    """The one step to suggest now, or ``None`` when nothing waits."""
    if active_job:
        return NextAction(
            f"Acompanhar o {active_job['operation']} em andamento",
            "Uma operação mutante ocupa a fila; as próximas esperam por ela.",
            f"/jobs/{active_job['job_id']}",
        )
    if not counts.get("sources") and not counts.get("notes"):
        return NextAction(
            "Enviar o primeiro documento",
            "O acervo está vazio: envie um PDF ou Markdown e processe-o.",
            "/documents",
        )
    for key, operation, label, detail, href in _STEPS:
        n = counts.get(key) or 0
        if not n:
            continue
        reason = blocker(operation) if blocker and operation else None
        if reason:
            return NextAction(label, reason, "/settings")
        return NextAction(label, detail.format(n=n), href)
    return None


def guidance_counts(db: Any) -> dict[str, int]:
    """The counts ``next_action`` reads, from the same state the Pipeline gates on."""
    from zettel.web.preconditions import pipeline_state

    stats = db.get_stats()
    return {**stats, **pipeline_state(db, stats)}
