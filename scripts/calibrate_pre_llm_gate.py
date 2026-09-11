"""Calibrate a pre-LLM gate on the labeled corpus already in state.db (issues #66/#173).

Not part of the production pipeline -- an offline analysis script. Every chunk
processed by ``extract`` carries the LLM's own verdict in
``summary_json.chunk_status``, so the gate's label set is a by-product of using
the vault. This script measures whether a gate could skip the Prompt 1 call on
chunks that would be rejected anyway, and at what cost in lost notes.

It reads SQLite and Chroma and **never writes to either**: no ``upsert_chunk``,
no status change, no LLM call. Vectors missing from the ``chunks`` collection are
embedded on demand, in memory, and thrown away -- so the measurement does not
depend on ``harvest.semantic_duplicate_enabled`` being on (with the flag off the
collection is not populated at harvest time, and ``reindex`` skips it too).

Two numbers decide the gate, and they pull against each other:

  * **calls avoided** -- every chunk the gate predicts as rejected, ``(tn+fn)/n``.
    This is the saving. It counts ``fn`` on purpose: a call not made is a call not
    paid for, whether or not skipping it was a mistake.
  * **accepted notes lost** -- ``fn/(tp+fn)``, the share of chunks that WOULD have
    produced a note and were dropped in silence. This is the damage, and it is why
    the recommended operating point is pinned at zero.

Validation is **grouped by source** (``LeaveOneGroupOut``): train on the other
documents, test on this one. Splitting by chunk instead leaks -- neighbouring
chunks of the same chapter are near-duplicates in embedding space -- and inflates
every number below. Fewer than three distinct sources cannot answer the question
the gate exists to answer, so the script aborts rather than report the leaky
number.

Usage:
    .venv/Scripts/python.exe scripts/calibrate_pre_llm_gate.py
    .venv/Scripts/python.exe scripts/calibrate_pre_llm_gate.py --state-db data/state.db
    .venv/Scripts/python.exe scripts/calibrate_pre_llm_gate.py --chroma-path data/chroma
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Run from a clone without installing the package (mirrors scripts/ precedent).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Below this, "train on the others, test on this one" cannot be expressed in a way
# that says anything about a document the model has never seen.
MIN_SOURCES = 3

Embedder = Callable[[list[str]], Sequence[Sequence[float]]]


@dataclass
class Record:
    chunk_id: str
    source_id: str
    text: str
    section_path: str
    label: int  # 1 = accepted, 0 = rejected
    rejection_category: str = ""
    embedding: list[float] | None = None


@dataclass
class EmbeddingReport:
    """Where the vectors came from. The space they live in is part of the result."""

    reused: int = 0
    computed: int = 0
    unusable: int = 0
    model: str = ""
    provider: str = ""
    dimensions: int | None = None


class InsufficientSourcesError(RuntimeError):
    """Raised when the corpus has too few distinct sources to validate a gate."""


# -- Dataset -------------------------------------------------------------


def load_labeled_chunks(state_db_path: Path) -> list[Record]:
    """Every chunk carrying an LLM verdict. SQLite only -- no Chroma, no vectors."""
    conn = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT chunk_id, source_id, text, section_path, summary_json FROM chunks"
        ).fetchall()
    finally:
        conn.close()

    records: list[Record] = []
    for r in rows:
        if not r["summary_json"]:
            continue
        try:
            data = json.loads(r["summary_json"])
        except json.JSONDecodeError:
            continue
        status = data.get("chunk_status")
        if status not in ("accepted", "rejected"):
            continue
        records.append(
            Record(
                chunk_id=r["chunk_id"],
                source_id=r["source_id"] or "",
                text=r["text"] or "",
                section_path=r["section_path"] or "",
                label=1 if status == "accepted" else 0,
                rejection_category=data.get("rejection_category") or "",
            )
        )
    return records


def attach_embeddings(
    records: list[Record],
    existing: dict[str, Sequence[float]],
    embedder: Embedder,
    *,
    batch_size: int = 64,
) -> tuple[list[Record], EmbeddingReport]:
    """Fill in every record's vector, computing the ones Chroma does not have.

    The previous version dropped a record with no stored vector, silently and
    without a count -- which is how a whole source vanished from the dataset
    unnoticed. Only a record with no vector *and* no text is unusable now, and
    that case is reported.
    """
    report = EmbeddingReport()
    todo: list[Record] = []
    for rec in records:
        vec = existing.get(rec.chunk_id)
        if vec is not None:
            rec.embedding = [float(x) for x in vec]
            report.reused += 1
        elif rec.text.strip():
            todo.append(rec)
        else:
            report.unusable += 1

    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        vectors = embedder([rec.text for rec in batch])
        for rec, vec in zip(batch, vectors, strict=True):
            rec.embedding = [float(x) for x in vec]
            report.computed += 1

    usable = [r for r in records if r.embedding is not None]
    if usable:
        report.dimensions = len(usable[0].embedding or [])
    return usable, report


def load_dataset(
    state_db_path: Path,
    chroma_path: Path,
) -> tuple[list[Record], EmbeddingReport]:
    """Labeled chunks with vectors: reuse what Chroma has, compute the rest."""
    from zettel.config import load_config
    from zettel.index import VectorIndex, index_kwargs

    records = load_labeled_chunks(state_db_path)
    if not records:
        return records, EmbeddingReport()

    cfg = load_config()
    kwargs = index_kwargs(cfg)
    kwargs["chroma_path"] = chroma_path
    # VectorIndex is what enforces that the config's embedding space matches the
    # one the stored vectors live in. Mixing spaces would make the reused and the
    # freshly computed vectors incomparable, and no threshold could repair that.
    idx = VectorIndex(**kwargs)

    stored = idx.chunks.get(ids=[r.chunk_id for r in records], include=["embeddings"])
    existing = dict(zip(stored["ids"], stored["embeddings"], strict=True))

    usable, report = attach_embeddings(records, existing, idx.embedding_fn)
    report.model = idx.embedding_model
    report.provider = idx.embedding_provider
    return usable, report


# -- Cheap heuristic baseline --------------------------------------------

_MIN_CHARS = 200
_MIN_ALNUM_RATIO = 0.5
_MAX_TABLE_LINE_RATIO = 0.6


def heuristic_predict(text: str) -> int:
    """1 = call the LLM (predicted acceptable); 0 = skip the call."""
    stripped = text.strip()
    if len(stripped) < _MIN_CHARS:
        return 0
    if re.fullmatch(r"[-=_*]{3,}", stripped):
        return 0
    alnum = sum(ch.isalnum() for ch in stripped)
    if alnum / max(1, len(stripped)) < _MIN_ALNUM_RATIO:
        return 0
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if lines:
        table_lines = sum(1 for ln in lines if ln.count("|") >= 2)
        if table_lines / len(lines) > _MAX_TABLE_LINE_RATIO:
            return 0
    return 1


def evaluate_predictions(labels: list[int], preds: list[int]) -> dict:
    tp = sum(1 for lab, p in zip(labels, preds, strict=True) if lab == 1 and p == 1)
    fp = sum(1 for lab, p in zip(labels, preds, strict=True) if lab == 0 and p == 1)
    fn = sum(1 for lab, p in zip(labels, preds, strict=True) if lab == 1 and p == 0)
    tn = sum(1 for lab, p in zip(labels, preds, strict=True) if lab == 0 and p == 0)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    # A call is avoided exactly when the gate predicts 0 -- `tn` (rightly) plus
    # `fn` (wrongly). The old formula was `(fp + tn)`, i.e. every rejected chunk in
    # the corpus: it counted `fp` (calls that WERE made) as savings, which made the
    # number the base rate of rejection -- constant, and independent of the
    # predictions it claimed to measure.
    calls_avoided = (tn + fn) / len(labels) if labels else 0.0
    false_negative_rate = fn / (tp + fn) if (tp + fn) else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "calls_avoided_pct": 100 * calls_avoided,
        "accepted_notes_lost_pct": 100 * false_negative_rate,
    }


def category_breakdown(records: list[Record], preds: list[int]) -> dict[str, str]:
    """How many rejections of each category the gate actually catches.

    A gate that only catches `structural` is a deterministic-rule problem, not a
    classifier one -- and the rule is cheaper, free to run and auditable.
    """
    caught: dict[str, int] = {}
    total: dict[str, int] = {}
    for rec, pred in zip(records, preds, strict=True):
        if rec.label != 0:
            continue
        cat = rec.rejection_category or "(sem categoria)"
        total[cat] = total.get(cat, 0) + 1
        if pred == 0:
            caught[cat] = caught.get(cat, 0) + 1
    return {cat: f"{caught.get(cat, 0)}/{n}" for cat, n in sorted(total.items())}


# -- Classifier over embeddings ------------------------------------------


def distinct_sources(records: list[Record]) -> list[str]:
    return sorted({r.source_id for r in records})


def cross_val_proba(records: list[Record]) -> Any:
    """Out-of-fold P(accepted), validated leave-one-source-out.

    Raises ``InsufficientSourcesError`` below ``MIN_SOURCES`` instead of falling
    back to a chunk-level split: that fallback is precisely the leaky measurement
    that makes the gate look viable.
    """
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import LeaveOneGroupOut, cross_val_predict

    sources = distinct_sources(records)
    if len(sources) < MIN_SOURCES:
        raise InsufficientSourcesError(
            f"O corpus tem {len(sources)} fonte(s) rotulada(s) "
            f"({', '.join(sources) or 'nenhuma'}); sao necessarias {MIN_SOURCES}. "
            "Validar por chunk dentro da mesma obra mede memorizacao, nao "
            "generalizacao: chunks vizinhos de um capitulo sao quase duplicatas no "
            "espaco de embedding. Processe mais fontes, de generos diferentes, e "
            "rode de novo."
        )

    y = np.array([r.label for r in records])
    if len(set(y.tolist())) < 2:
        raise InsufficientSourcesError(
            "O corpus rotulado tem uma classe so -- sem chunks aceitos E "
            "rejeitados nao ha o que separar."
        )

    X = np.array([r.embedding for r in records])
    groups = np.array([r.source_id for r in records])
    clf = LogisticRegression(max_iter=1000, class_weight="balanced")
    return cross_val_predict(
        clf, X, y, cv=LeaveOneGroupOut(), groups=groups, method="predict_proba"
    )[:, 1]


def operating_points(
    labels: list[int],
    proba: Sequence[float],
    *,
    max_loss_pct: Sequence[float] = (0.0, 1.0, 5.0),
) -> list[dict]:
    """Strictest threshold (most calls avoided) under each tolerated loss."""
    thresholds = [*sorted({float(p) for p in proba}, reverse=True), 0.0]
    points: list[dict] = []
    for budget in max_loss_pct:
        chosen: dict | None = None
        for t in thresholds:
            preds = [1 if p >= t else 0 for p in proba]
            stats = evaluate_predictions(labels, preds)
            if stats["accepted_notes_lost_pct"] <= budget:
                chosen = {"max_loss_pct": budget, "threshold": t, **stats}
                break
        points.append(chosen or {"max_loss_pct": budget, "threshold": None})
    return points


# -- Cost ----------------------------------------------------------------


def avoided_cost_usd(cfg: Any, skipped: list[Record]) -> float:
    """USD the gate would not spend on ``skipped``, priced like the pre-flight."""
    from zettel.config import llm_phase
    from zettel.preflight import estimate_tokens
    from zettel.pricing import estimate_llm_cost

    if not skipped:
        return 0.0
    prompt_path = Path(cfg.prompts_path) / "literature_note.md"
    try:
        overhead = estimate_tokens(prompt_path.read_text(encoding="utf-8"))
    except OSError:
        overhead = 0
    spec = llm_phase(cfg, "extract")
    input_tokens = sum(estimate_tokens(r.text) + overhead for r in skipped)
    output_tokens = len(skipped) * cfg.extraction.preflight_output_tokens_per_chunk
    return estimate_llm_cost(spec.model, input_tokens, output_tokens, provider=spec.provider)


# -- Report --------------------------------------------------------------


def _print_stats(stats: dict) -> None:
    for k, v in stats.items():
        print(f"  {k}: {v:.3f}" if isinstance(v, float) else f"  {k}: {v}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", type=Path, default=Path("data/state.db"))
    parser.add_argument("--chroma-path", type=Path, default=Path("data/chroma"))
    args = parser.parse_args()

    records, emb = load_dataset(args.state_db, args.chroma_path)
    print(f"Dataset: {len(records)} chunks rotulados com embedding")
    if not records:
        print("Nada para calibrar -- sem chunks rotulados no state.db informado.")
        return 0

    n_accepted = sum(r.label for r in records)
    sources = distinct_sources(records)
    print(f"  accepted={n_accepted} rejected={len(records) - n_accepted}")
    print(f"  fontes={len(sources)}: {', '.join(sources)}")
    unusable = f", {emb.unusable} sem texto (descartados)" if emb.unusable else ""
    print(
        f"  embeddings: {emb.reused} reaproveitados do Chroma, "
        f"{emb.computed} calculados agora{unusable}"
    )
    print(f"  espaco: {emb.provider}/{emb.model} @ {emb.dimensions}d")

    labels = [r.label for r in records]

    print("\n== Heuristica barata (piso de tamanho + alnum ratio + densidade de tabela) ==")
    preds = [heuristic_predict(r.text) for r in records]
    _print_stats(evaluate_predictions(labels, preds))
    print(f"  rejeicoes capturadas por categoria: {category_breakdown(records, preds)}")

    print("\n== Classificador (regressao logistica sobre embeddings, leave-one-source-out) ==")
    try:
        proba = cross_val_proba(records)
    except InsufficientSourcesError as exc:
        print(f"  ABORTADO: {exc}")
        return 1

    from zettel.config import load_config

    cfg = load_config()
    for point in operating_points(labels, proba):
        budget = point["max_loss_pct"]
        threshold = point.get("threshold")
        if threshold is None:
            print(f"  perda <= {budget:.1f}%: nenhum limiar qualifica")
            continue
        gate_preds = [1 if p >= threshold else 0 for p in proba]
        skipped = [rec for rec, p in zip(records, gate_preds, strict=True) if p == 0]
        print(
            f"  perda <= {budget:.1f}%: threshold={threshold:.3f} "
            f"chamadas_evitadas={point['calls_avoided_pct']:.1f}% "
            f"perda_real={point['accepted_notes_lost_pct']:.2f}% "
            f"(fn={point['fn']} tn={point['tn']}) "
            f"economia=USD {avoided_cost_usd(cfg, skipped):.2f}"
        )
        print(f"    rejeicoes capturadas por categoria: {category_breakdown(records, gate_preds)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
