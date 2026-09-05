"""Graph state contract, runtime handle, and run-option resolution for `zettel article`.

``runtime.py`` (not ``state.py``): package modules also import ``zettel/state.py``
(``StateDB``), and two things called "state" in one import block hurts readability.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict

from .. import article as art
from ..config import llm_phase
from ..schemas import ArticleOutline

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from ..config import AppConfig
    from ..index import VectorIndex
    from ..state import StateDB

ContextCallback = Callable[[dict], dict]
OutlineCallback = art.ApproveOutlineFn


class ArticleGraphState(TypedDict, total=False):
    # ── Run input / config ──────────────────────────────────────────
    topic: str
    style: Literal["blog", "academic"]
    personality: str
    custom_style_notes: str
    topk: int
    use_graph: bool
    mode: str
    outline_only: bool
    skip_context_review: bool
    skip_judge: bool
    max_judge_iterations: int

    # ── Retrieval ────────────────────────────────────────────────────
    search_queries: list[str]
    executed_queries: list[str]
    extra_queries: list[str]
    retrieved_notes: list[dict]
    moc_ids: list[str]
    retrieval_params: dict

    # ── HITL decisions ───────────────────────────────────────────────
    context_decision: str
    outline_decision: str
    outline_feedback: str

    # ── Writing ──────────────────────────────────────────────────────
    outline: dict
    section_bodies: list[str]
    used_note_ids: list[str]
    draft_body: str
    styled_body: str
    judge_feedback: str
    judge_scores: dict
    iteration_count: int

    # ── Output ───────────────────────────────────────────────────────
    final_body: str
    frontmatter: dict
    warnings: list[str]
    cited_source_ids: list[str]
    no_evidence: bool
    aborted: bool
    llm_called: bool


@dataclass
class ArticleRuntime:
    cfg: AppConfig
    db: StateDB
    idx: VectorIndex
    catalog: art.ArticleCatalog | None = None
    context_callback: ContextCallback | None = None
    outline_callback: OutlineCallback | None = None
    llm_called: bool = False


def runtime_from(config: RunnableConfig) -> ArticleRuntime:
    return config["configurable"]["runtime"]  # type: ignore[index]


def mark_llm(rt: ArticleRuntime, called: bool) -> dict:
    if called:
        rt.llm_called = True
        return {"llm_called": True}
    return {}


@dataclass
class RunOptions:
    """Resolved run() options, after the three defaulting rules below are applied."""

    outline_callback: OutlineCallback
    skip_context_review: bool
    skip_judge: bool
    use_graph: bool


def resolve_run_options(
    cfg: AppConfig,
    *,
    approve_outline: OutlineCallback | None,
    context_callback: ContextCallback | None,
    hitl_handler: Callable[[dict], dict] | None,
    skip_context_review: bool,
    skip_judge: bool,
    outline_only: bool,
    use_graph: bool | None,
) -> RunOptions:
    """Apply the three run()-time defaulting rules in one place.

    - Auto-approve the outline when neither ``approve_outline`` nor
      ``hitl_handler`` was supplied (non-interactive callers / tests).
    - Force-skip context review when neither ``hitl_handler`` nor
      ``context_callback`` was supplied.
    - ``outline_only`` implies skipping the judge loop.
    """
    outline_callback = approve_outline
    if outline_callback is None and hitl_handler is None:

        def outline_callback(_o):
            return ("approve", None)

    effective_skip_context = skip_context_review
    if hitl_handler is None and context_callback is None:
        effective_skip_context = True

    resolved_use_graph = use_graph
    if resolved_use_graph is None:
        resolved_use_graph = cfg.retrieval.graph_expansion.enabled

    return RunOptions(
        outline_callback=outline_callback,
        skip_context_review=effective_skip_context,
        skip_judge=skip_judge or outline_only,
        use_graph=bool(resolved_use_graph),
    )


def build_initial_state(
    topic: str,
    style: art.ArticleStyle,
    cfg: AppConfig,
    options: RunOptions,
    *,
    personality: str | None,
    custom_style_notes: str | None,
    topk: int | None,
    mode: str | None,
    outline_only: bool,
    max_judge_iterations: int | None,
) -> ArticleGraphState:
    art_cfg = cfg.retrieval.article
    return {
        "topic": topic,
        "style": style,
        "personality": personality or art_cfg.default_personality,
        "custom_style_notes": custom_style_notes or "",
        "topk": topk if topk is not None else art_cfg.topk,
        "use_graph": options.use_graph,
        "mode": mode or cfg.retrieval.mode,
        "outline_only": outline_only,
        "skip_context_review": options.skip_context_review,
        "skip_judge": options.skip_judge,
        "max_judge_iterations": (
            max_judge_iterations
            if max_judge_iterations is not None
            else art_cfg.max_judge_iterations
        ),
        "search_queries": [],
        "executed_queries": [],
        "extra_queries": [],
        "retrieved_notes": [],
        "moc_ids": [],
        "iteration_count": 0,
        "judge_feedback": "",
        "llm_called": False,
        "warnings": [],
    }


def result_from_state(
    state: dict, rt: ArticleRuntime, topic: str, style: art.ArticleStyle
) -> art.ArticleResult:
    outline = None
    if state.get("outline"):
        outline = ArticleOutline.model_validate(state["outline"])
    body = state.get("final_body") or ""
    no_evidence = bool(state.get("no_evidence"))
    title = ""
    if outline:
        title = outline.title
    elif state.get("frontmatter"):
        title = str(state["frontmatter"].get("title") or "")

    return art.ArticleResult(
        topic=topic,
        style=style,
        title=title,
        body=body,
        answer=body,
        frontmatter=dict(state.get("frontmatter") or {}),
        outline=outline,
        warnings=list(state.get("warnings") or []),
        llm_called=bool(state.get("llm_called") or rt.llm_called),
        llm_model=llm_phase(rt.cfg, "article").model,
        note_ids=list(
            state.get("used_note_ids") or (list(rt.catalog.notes.keys()) if rt.catalog else [])
        ),
        source_ids=list(state.get("cited_source_ids") or []),
        no_evidence=no_evidence,
        aborted=bool(state.get("aborted")),
    )
