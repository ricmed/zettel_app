"""Rich renderers shared by the CLI commands.

Nothing here touches the pipeline or the databases: these functions take values
that a command has already computed and turn them into terminal output. Keeping
presentation in one module is what lets the pipeline modules stay
framework-agnostic (ADR-026) — a command formats, the domain computes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from rich.table import Table

from zettel.cli.app import console


def fmt_embedding_id(
    provider: str | None,
    model: str | None,
    dimensions: int | None,
) -> str:
    """Human-readable identity of an embedding space, e.g. ``ollama/qwen3@1024d``.

    ``dimensions`` is part of the identity, not decoration: the same model asked
    for 1024 dimensions instead of its native width produces vectors that cannot
    be compared with the others in the store.
    """
    base = f"{provider}/{model}"
    if dimensions is not None:
        return f"{base}@{dimensions}d"
    return base


def fmt_usd(value: object) -> str:
    """Six decimal places, because a single cheap call costs ~$0.000002."""
    return f"{float(value or 0):.6f}"


def fmt_prompt_cache_ratio(u) -> str:
    """``'read/write tokens (pct% do prompt)'``, or ``'-'`` when the cache was idle.

    ``u`` is a usage aggregate from ``zettel.usage``. The percentage is over
    ``tokens_prompt`` and is omitted when that is zero (a run whose only LLM work
    came from the SQLite cache), which also keeps this away from a ZeroDivisionError.
    """
    read, write = u.prompt_cache_read_tokens, u.prompt_cache_write_tokens
    if not read and not write:
        return "-"
    pct = f" ({100 * read / u.tokens_prompt:.0f}%)" if u.tokens_prompt else ""
    return f"{read}r/{write}w{pct}"


def metrics_table(
    title: str,
    rows: Mapping[str, object] | Iterable[tuple[str, object]],
    *,
    key_label: str = "Metrica",
    value_label: str = "Valor",
    capitalize_keys: bool = False,
) -> None:
    """Print a two-column table of name/number pairs.

    The shape every "here is what the run did" summary in the CLI uses: reindex
    and rebuild report vectors per collection, sync-manual reports counters. They
    were four hand-built copies of the same six lines before.

    Args:
        rows: mapping (or pairs) of label to value; values are stringified.
        capitalize_keys: ``sync-manual`` shows raw stat keys and wants them
            title-cased; collection names must stay verbatim.
    """
    table = Table(title=title)
    table.add_column(key_label, style="bold")
    table.add_column(value_label, justify="right")
    pairs = rows.items() if isinstance(rows, Mapping) else rows
    for key, value in pairs:
        table.add_row(key.capitalize() if capitalize_keys else key, str(value))
    console.print(table)


def print_cost_by_phase(db, *, title: str = "Custo por fase") -> None:
    """One row per phase of the latest pipeline session, then a total.

    A "session" is the run of consecutive phases that ``zettel.usage`` groups by
    pipeline signature — so this reports the last pipeline pass, not the lifetime
    of the vault. Prints nothing when there is no session yet.
    """
    from zettel.usage import (
        latest_pipeline_session,
        pipeline_phase_name,
        sum_run_usage,
        usage_from_run,
    )

    session = latest_pipeline_session(db.get_recent_runs())
    if not session:
        return
    table = Table(title=title)
    table.add_column("Fase", style="bold")
    table.add_column("USD total", justify="right")
    table.add_column("USD LLM", justify="right")
    table.add_column("USD embeddings", justify="right")
    table.add_column("Tokens prompt", justify="right")
    table.add_column("Tokens completion", justify="right")
    table.add_column("Tokens embedding", justify="right")
    table.add_column("LLM calls", justify="right")
    table.add_column("Cache hits (SQLite)", justify="right")
    table.add_column("Cache prompt (provider)", justify="right")
    for row in session:
        u = usage_from_run(row)
        table.add_row(
            pipeline_phase_name(row.get("pipeline_signature")),
            fmt_usd(u.cost_usd_total),
            fmt_usd(u.cost_usd_llm),
            fmt_usd(u.cost_usd_embedding),
            str(u.tokens_prompt),
            str(u.tokens_completion),
            str(u.tokens_embedding),
            str(u.llm_calls),
            str(u.cache_hits),
            fmt_prompt_cache_ratio(u),
        )
    total = sum_run_usage(session)
    table.add_row(
        "Total",
        fmt_usd(total.cost_usd_total),
        fmt_usd(total.cost_usd_llm),
        fmt_usd(total.cost_usd_embedding),
        str(total.tokens_prompt),
        str(total.tokens_completion),
        str(total.tokens_embedding),
        str(total.llm_calls),
        str(total.cache_hits),
        fmt_prompt_cache_ratio(total),
        style="bold",
    )
    console.print(table)
    # The two cache layers are independent and are constantly confused for each
    # other when reading this table, hence the legend under it.
    console.print(
        "[dim]'Cache hits (SQLite)' = respostas reaproveitadas de llm_cache (custo $0). "
        "'Cache prompt (provider)' = tokens de prompt lidos do cache do provedor "
        "(Anthropic/OpenAI/Gemini) numa chamada que ainda assim foi feita -- reduz custo "
        "por token, nao elimina a chamada. As duas camadas sao independentes.[/dim]"
    )
