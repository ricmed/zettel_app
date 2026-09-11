"""One-line pipeline log format.

Leaf module: no Chroma, LLM or config imports. Callers pass already-computed
numbers; this only shapes the string the operator reads.
"""

from __future__ import annotations

import logging

VERB_WIDTH = 6

_QUERY_UNITS: dict[str, str] = {
    "densa": "notas",
    "topic index": "notas",
    "analogias": "notas",
    "dedupe": "notas",
    "resumos": "capitulos",
    "chunks": "chunks",
}


def fmt_money(cost_usd: float | None) -> str:
    """Compact USD: ``$0`` locally, ``$0.003`` when there is a bill."""
    if cost_usd is None:
        return ""
    if abs(float(cost_usd)) < 5e-7:
        return "$0"
    return f"${float(cost_usd):.3f}"


def fmt_tokens(n: int | None, *, compact: bool = False) -> str:
    if n is None:
        return ""
    count = int(n)
    if compact and count >= 1000:
        val = count / 1000
        if val >= 10:
            return f"{val:.0f}k tok"
        text = f"{val:.1f}k tok"
        return text.replace(".0k", "k")
    return f"{count} tok"


def clip_one_line(text: str, max_len: int = 48) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= max_len:
        return collapsed
    suffix = "..."
    return collapsed[: max(0, max_len - len(suffix))].rstrip() + suffix


def format_step(
    verb: str,
    detail: str,
    *,
    model: str = "",
    tokens: str = "",
    cost: str = "",
    extra: str = "",
    progress: str = "",
) -> str:
    """Stable one-liner: verb, why, then optional model/tokens/cost/extra/[progress]."""
    head = f"{verb:<{VERB_WIDTH}} {detail}".rstrip()
    tail = [p for p in (model, tokens, cost, extra) if p]
    if progress:
        tail.append(f"[{progress}]")
    if not tail:
        return head
    return f"{head}  {'  '.join(tail)}"


def log_step(
    logger: logging.Logger,
    verb: str,
    detail: str,
    *,
    model: str = "",
    tokens: str = "",
    cost: str = "",
    extra: str = "",
    progress: str = "",
    level: int = logging.INFO,
) -> str:
    """Emit ``format_step`` and return the same string (for tests)."""
    message = format_step(
        verb,
        detail,
        model=model,
        tokens=tokens,
        cost=cost,
        extra=extra,
        progress=progress,
    )
    logger.log(level, message)
    return message


def embed_step(purpose: str, label: str, count: int | None = None) -> tuple[str, str]:
    """Map an embed call to ``(verb, detail)``: busca vs index."""
    why = (purpose or "").strip()
    if why in _QUERY_UNITS:
        unit = _QUERY_UNITS[why]
        if count is not None:
            return "busca", f"{why} ({int(count)} {unit})"
        return "busca", why

    raw = (label or "").strip()
    if raw == "query_notes" or raw.startswith("query_notes:"):
        return "busca", _with_count("densa", count, "notas")
    if raw == "query_notes_by_ids":
        return "busca", _with_count("topic index", count, "notas")
    if raw == "query_chapter_summaries":
        return "busca", _with_count("resumos", count, "capitulos")
    if raw == "query_chunks":
        return "busca", _with_count("chunks", count, "chunks")
    if raw == "dedupe-intra-lote":
        return "busca", "dedupe"

    kind, _, ident = raw.partition(":")
    if kind == "note" and ident:
        return "index", f"ZTL {ident}"
    if kind == "chunk" and ident:
        return "index", f"chunk {ident}"
    if kind == "source" and ident:
        return "index", f"fonte {ident}"
    if kind == "chapter" and ident:
        return "index", f"capitulo {ident}"
    if kind == "moc" and ident:
        return "index", f"MOC {ident}"
    if why:
        return "index", why
    return "index", raw or "embed"


def llm_extra(
    *,
    cache_local_hit: bool | None = None,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> str:
    """SQLite cache and provider prefix cache, never mixed into one word."""
    parts: list[str] = []
    if cache_local_hit is True:
        parts.append("cache local hit")
    elif cache_local_hit is False:
        parts.append("cache local miss")
    if cache_read_tokens:
        parts.append(f"prefixo {fmt_tokens(cache_read_tokens, compact=True)}")
    if cache_write_tokens:
        parts.append(f"grava {fmt_tokens(cache_write_tokens, compact=True)}")
    return "  ".join(parts)


def _with_count(label: str, count: int | None, unit: str) -> str:
    if count is None:
        return label
    return f"{label} ({int(count)} {unit})"
