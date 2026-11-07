"""Connect several sources at once: one Prompt 2 call in flight per source.

A note's RAG context is every note written before it, and most typed edges
join notes a few positions apart in the same source (measured on the two-book
run of 2026-11-06: 17% to the previous note, 38% within four, 4.9% across
sources). So the order inside a source stays strictly sequential: a source
prepares its next note only after the previous one is written. Different
sources overlap, which only changes which notes of *another* source a note
can see.

Only the model's HTTP round trip leaves the main thread. Preparing a note
(SQLite, the vector index, the query embedding) and finishing it (cache,
parsing, PT-BR guard, vault, edges, status) stay on the thread that owns the
``StateDB`` connection and the Chroma client.
"""

from __future__ import annotations

import contextvars
import itertools
import logging
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any

from zettel.config import llm_phase
from zettel.connector import prompt
from zettel.connector.note import (
    ConnectSession,
    PreparedNote,
    finish_candidate,
    prepare_candidate,
)
from zettel.usage import begin_run, clear_progress, get_tracker, set_progress, set_source

logger = logging.getLogger(__name__)

# What a work callable returns when the candidate's Prompt 2 call was handed to
# a worker: the outcome comes later, when the call completes.
PENDING = object()


def source_groups(candidates: list[dict]) -> dict[str, deque[dict]]:
    """The queue split by ``source_id``, each keeping the order it had."""
    groups: dict[str, deque[dict]] = {}
    for cand_dict in candidates:
        groups.setdefault(cand_dict.get("source_id") or "", deque()).append(cand_dict)
    return groups


def invoke_prompt2(
    session: ConnectSession, prepared: PreparedNote, step: int, total: int
) -> tuple[str, list[Any]]:
    """The Prompt 2 call alone, on a worker thread with a private cost tracker.

    Runs inside a copy of the caller's context, so ``begin_run`` here does not
    touch the run's tracker. The events come back with the answer and are
    merged when the main thread finishes the note.
    """
    begin_run()
    spec = llm_phase(session.cfg, "connect")
    text = prompt.call_llm(
        session.llm,
        prepared.user,
        system=prepared.system or None,
        label=f"connect:{prepared.cand_dict['concept_id']}",
        step=step,
        total=total,
        provider=spec.provider,
        prompt_cache=session.cfg.llm.prompt_cache,
    )
    tracker = get_tracker()
    return text, list(tracker.events) if tracker else []


def replay(future: Future) -> Callable[..., str]:
    """A ``call`` for ``finish_candidate`` that returns the worker's answer.

    An exception raised in the worker is raised here instead, inside the same
    cached call, so it is handled exactly as in a sequential run.
    """

    def call(*_args: Any, **_kwargs: Any) -> str:
        text, events = future.result()
        tracker = get_tracker()
        if tracker is not None:
            tracker.absorb(events)
        return text

    return call


def connect_by_source(
    session: ConnectSession,
    candidates: list[dict],
    *,
    workers: int,
    announce: Callable[[dict, int], None],
    settle: Callable[[dict, int, Callable[[], Any]], None],
) -> None:
    """Process ``candidates`` with up to ``workers`` sources in flight.

    ``announce(cand, i)`` reports that candidate ``i`` starts. ``settle(cand, i,
    work)`` runs ``work`` and files its outcome (note, skip, rejection,
    failure); ``work`` returns ``PENDING`` when the call went to a worker.
    An exception ``settle`` lets through (provider outage, embedding outage)
    abandons the calls still in flight and propagates.
    """
    total = len(candidates)
    queues = source_groups(candidates)
    counter = itertools.count(1)
    inflight: dict[Future, tuple[dict, PreparedNote, int]] = {}
    busy: set[str] = set()
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="connect")

    def start(cand_dict: dict, i: int) -> Any:
        prepared = prepare_candidate(session, cand_dict)
        if prepared is None:
            return None
        if session.db.get_cached_llm_response(prepared.checksum) is not None:
            return finish_candidate(session, prepared, step=i, total=total)
        future = pool.submit(
            contextvars.copy_context().run, invoke_prompt2, session, prepared, i, total
        )
        inflight[future] = (cand_dict, prepared, i)
        busy.add(cand_dict.get("source_id") or "")
        return PENDING

    try:
        while True:
            for sid, queue in queues.items():
                while sid not in busy and queue:
                    cand_dict = queue.popleft()
                    i = next(counter)
                    set_source(sid or None)
                    set_progress(i, total, "nota")
                    announce(cand_dict, i)
                    try:
                        settle(cand_dict, i, lambda c=cand_dict, n=i: start(c, n))
                    finally:
                        clear_progress()
            if not inflight:
                break
            done, _ = wait(inflight, return_when=FIRST_COMPLETED)
            for future in done:
                cand_dict, prepared, i = inflight.pop(future)
                sid = cand_dict.get("source_id") or ""
                busy.discard(sid)
                set_source(sid or None)
                set_progress(i, total, "nota")
                try:
                    settle(
                        cand_dict,
                        i,
                        lambda p=prepared, n=i, f=future: finish_candidate(
                            session, p, step=n, total=total, call=replay(f)
                        ),
                    )
                finally:
                    clear_progress()
    finally:
        if inflight:
            logger.warning(
                "Connect interrompido: %d chamada(s) em voo descartada(s)", len(inflight)
            )
        pool.shutdown(wait=False, cancel_futures=True)
