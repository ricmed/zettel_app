"""Per-run / per-source cost aggregation via contextvars."""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class UsageEvent:
    kind: str  # llm | embed | cache_hit
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    label: str = ""
    source_id: Optional[str] = None
    progress: str = ""
    # Provider prompt-prefix cache (not SQLite llm_cache hits).
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class UsageSummary:
    cost_usd_total: float = 0.0
    cost_usd_llm: float = 0.0
    cost_usd_embedding: float = 0.0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    tokens_embedding: int = 0
    llm_calls: int = 0
    cache_hits: int = 0
    embed_calls: int = 0
    prompt_cache_read_tokens: int = 0
    prompt_cache_write_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "cost_usd_total": round(self.cost_usd_total, 6),
            "cost_usd_llm": round(self.cost_usd_llm, 6),
            "cost_usd_embedding": round(self.cost_usd_embedding, 6),
            "tokens_prompt": self.tokens_prompt,
            "tokens_completion": self.tokens_completion,
            "tokens_embedding": self.tokens_embedding,
            "llm_calls": self.llm_calls,
            "cache_hits": self.cache_hits,
            "embed_calls": self.embed_calls,
            "prompt_cache_read_tokens": self.prompt_cache_read_tokens,
            "prompt_cache_write_tokens": self.prompt_cache_write_tokens,
        }

    def add(self, other: "UsageSummary") -> None:
        self.cost_usd_total += other.cost_usd_total
        self.cost_usd_llm += other.cost_usd_llm
        self.cost_usd_embedding += other.cost_usd_embedding
        self.tokens_prompt += other.tokens_prompt
        self.tokens_completion += other.tokens_completion
        self.tokens_embedding += other.tokens_embedding
        self.llm_calls += other.llm_calls
        self.cache_hits += other.cache_hits
        self.embed_calls += other.embed_calls
        self.prompt_cache_read_tokens += other.prompt_cache_read_tokens
        self.prompt_cache_write_tokens += other.prompt_cache_write_tokens


def format_progress(
    step: Optional[int] = None,
    total: Optional[int] = None,
    kind: str = "",
) -> str:
    """Human progress label, e.g. ``imagem 3/40`` or ``3/40``."""
    if step is None:
        return ""
    prefix = f"{kind} " if kind else ""
    if total is not None:
        return f"{prefix}{step}/{total}".strip()
    return f"{prefix}{step}".strip()


def format_progress_from_context() -> str:
    p = _progress.get()
    if not p:
        return ""
    step, total, kind = p
    return format_progress(step, total, kind)


def _progress_tag(
    step: Optional[int] = None,
    total: Optional[int] = None,
    kind: Optional[str] = None,
) -> str:
    """Resolve progress for a COST line: explicit args win over context."""
    ctx = _progress.get()
    ctx_step, ctx_total, ctx_kind = ctx if ctx else (None, None, "")
    use_step = step if step is not None else ctx_step
    use_total = total if total is not None else ctx_total
    use_kind = kind if kind else ctx_kind
    return format_progress(use_step, use_total, use_kind or "")


@dataclass
class CostTracker:
    run_id: Optional[int] = None
    events: list[UsageEvent] = field(default_factory=list)
    _by_source: dict[str, UsageSummary] = field(default_factory=dict)
    _total: UsageSummary = field(default_factory=UsageSummary)

    def record_llm(
        self,
        *,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        label: str = "",
        source_id: Optional[str] = None,
        step: Optional[int] = None,
        total: Optional[int] = None,
        kind: Optional[str] = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> UsageEvent:
        sid = source_id if source_id is not None else get_source_id()
        prog = _progress_tag(step, total, kind)
        event = UsageEvent(
            kind="llm",
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            label=label,
            source_id=sid,
            progress=prog,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        self.events.append(event)
        self._total.llm_calls += 1
        self._total.tokens_prompt += tokens_in
        self._total.tokens_completion += tokens_out
        self._total.prompt_cache_read_tokens += cache_read_tokens
        self._total.prompt_cache_write_tokens += cache_write_tokens
        self._total.cost_usd_llm += cost_usd
        self._total.cost_usd_total += cost_usd
        if sid:
            bucket = self._by_source.setdefault(sid, UsageSummary())
            bucket.llm_calls += 1
            bucket.tokens_prompt += tokens_in
            bucket.tokens_completion += tokens_out
            bucket.prompt_cache_read_tokens += cache_read_tokens
            bucket.prompt_cache_write_tokens += cache_write_tokens
            bucket.cost_usd_llm += cost_usd
            bucket.cost_usd_total += cost_usd
        cache_tag = ""
        if cache_read_tokens or cache_write_tokens:
            cache_tag = f" cache_read={cache_read_tokens} cache_write={cache_write_tokens}"
        if prog:
            logger.info(
                "COST llm [%s] model=%s in=%d out=%d usd=%.6f label=%s source=%s%s",
                prog, model, tokens_in, tokens_out, cost_usd, label or "-", sid or "-",
                cache_tag,
            )
        else:
            logger.info(
                "COST llm model=%s in=%d out=%d usd=%.6f label=%s source=%s%s",
                model, tokens_in, tokens_out, cost_usd, label or "-", sid or "-",
                cache_tag,
            )
        return event

    def record_embed(
        self,
        *,
        model: str,
        tokens: int,
        cost_usd: float,
        label: str = "",
        source_id: Optional[str] = None,
        step: Optional[int] = None,
        total: Optional[int] = None,
        kind: Optional[str] = None,
    ) -> UsageEvent:
        sid = source_id if source_id is not None else get_source_id()
        prog = _progress_tag(step, total, kind)
        event = UsageEvent(
            kind="embed",
            model=model,
            tokens_in=tokens,
            tokens_out=0,
            cost_usd=cost_usd,
            label=label,
            source_id=sid,
            progress=prog,
        )
        self.events.append(event)
        self._total.embed_calls += 1
        self._total.tokens_embedding += tokens
        self._total.cost_usd_embedding += cost_usd
        self._total.cost_usd_total += cost_usd
        if sid:
            bucket = self._by_source.setdefault(sid, UsageSummary())
            bucket.embed_calls += 1
            bucket.tokens_embedding += tokens
            bucket.cost_usd_embedding += cost_usd
            bucket.cost_usd_total += cost_usd
        if prog:
            logger.info(
                "COST embed [%s] model=%s tokens=%d usd=%.6f label=%s source=%s",
                prog, model, tokens, cost_usd, label or "-", sid or "-",
            )
        else:
            logger.info(
                "COST embed model=%s tokens=%d usd=%.6f label=%s source=%s",
                model, tokens, cost_usd, label or "-", sid or "-",
            )
        return event

    def record_cache_hit(
        self,
        *,
        label: str = "",
        source_id: Optional[str] = None,
        model: str = "",
        step: Optional[int] = None,
        total: Optional[int] = None,
        kind: Optional[str] = None,
    ) -> UsageEvent:
        sid = source_id if source_id is not None else get_source_id()
        prog = _progress_tag(step, total, kind)
        event = UsageEvent(
            kind="cache_hit",
            model=model,
            cost_usd=0.0,
            label=label,
            source_id=sid,
            progress=prog,
        )
        self.events.append(event)
        self._total.cache_hits += 1
        if sid:
            self._by_source.setdefault(sid, UsageSummary()).cache_hits += 1
        if prog:
            logger.info(
                "COST cache_hit [%s] label=%s source=%s usd=0",
                prog, label or "-", sid or "-",
            )
        else:
            logger.info(
                "COST cache_hit label=%s source=%s usd=0",
                label or "-", sid or "-",
            )
        return event

    def summary(self) -> UsageSummary:
        return self._total

    def summary_for_source(self, source_id: str) -> UsageSummary:
        return self._by_source.get(source_id, UsageSummary())

    def sources_touched(self) -> list[str]:
        return list(self._by_source.keys())


_tracker: ContextVar[Optional[CostTracker]] = ContextVar("cost_tracker", default=None)
_source_id: ContextVar[Optional[str]] = ContextVar("cost_source_id", default=None)
# (step, total, kind) e.g. (3, 40, "imagem")
_progress: ContextVar[Optional[tuple[int, Optional[int], str]]] = ContextVar(
    "cost_progress", default=None,
)


def begin_run(run_id: Optional[int] = None) -> CostTracker:
    """Start a fresh tracker for a pipeline command."""
    tracker = CostTracker(run_id=run_id)
    _tracker.set(tracker)
    _source_id.set(None)
    _progress.set(None)
    return tracker


def get_tracker() -> Optional[CostTracker]:
    return _tracker.get()


def require_tracker() -> CostTracker:
    tracker = _tracker.get()
    if tracker is None:
        tracker = begin_run()
    return tracker


def set_source(source_id: Optional[str]) -> None:
    _source_id.set(source_id)


def get_source_id() -> Optional[str]:
    return _source_id.get()


def set_progress(
    step: int,
    total: Optional[int] = None,
    kind: str = "",
) -> None:
    """Set current item position for COST logs (e.g. imagem 3/40)."""
    _progress.set((step, total, kind))


def clear_progress() -> None:
    _progress.set(None)


def reset() -> None:
    _tracker.set(None)
    _source_id.set(None)
    _progress.set(None)


def record_llm(
    *,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    label: str = "",
    source_id: Optional[str] = None,
    step: Optional[int] = None,
    total: Optional[int] = None,
    kind: Optional[str] = None,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> None:
    require_tracker().record_llm(
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        label=label,
        source_id=source_id,
        step=step,
        total=total,
        kind=kind,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )


def record_embed(
    *,
    model: str,
    tokens: int,
    cost_usd: float,
    label: str = "",
    source_id: Optional[str] = None,
    step: Optional[int] = None,
    total: Optional[int] = None,
    kind: Optional[str] = None,
) -> None:
    require_tracker().record_embed(
        model=model,
        tokens=tokens,
        cost_usd=cost_usd,
        label=label,
        source_id=source_id,
        step=step,
        total=total,
        kind=kind,
    )


def record_cache_hit(
    *,
    label: str = "",
    source_id: Optional[str] = None,
    model: str = "",
    step: Optional[int] = None,
    total: Optional[int] = None,
    kind: Optional[str] = None,
) -> None:
    require_tracker().record_cache_hit(
        label=label,
        source_id=source_id,
        model=model,
        step=step,
        total=total,
        kind=kind,
    )


def log_run_summary(prefix: str = "Custo do run") -> None:
    tracker = get_tracker()
    if tracker is None:
        return
    s = tracker.summary().as_dict()
    logger.info(
        "%s: usd_total=%.6f llm=%.6f embed=%.6f "
        "tokens_in=%d tokens_out=%d tokens_embed=%d "
        "llm_calls=%d cache_hits=%d embed_calls=%d "
        "prompt_cache_read=%d prompt_cache_write=%d",
        prefix,
        s["cost_usd_total"],
        s["cost_usd_llm"],
        s["cost_usd_embedding"],
        s["tokens_prompt"],
        s["tokens_completion"],
        s["tokens_embedding"],
        s["llm_calls"],
        s["cache_hits"],
        s["embed_calls"],
        s.get("prompt_cache_read_tokens", 0),
        s.get("prompt_cache_write_tokens", 0),
    )


def finish_pipeline_run(db: Any, run_id: int, status: str = "completed") -> dict[str, Any]:
    """Persist tracker totals on the run row, log summary, and clear context."""
    tracker = get_tracker()
    usage = tracker.summary().as_dict() if tracker else {}
    db.finish_run(run_id, status, usage or None)
    log_run_summary()
    reset()
    return usage
