"""Spike (issue #66): calibrate a pre-LLM gate on the labeled corpus already in state.db.

Not part of the production pipeline -- a one-off analysis script. Every chunk processed
by ``extract`` already carries the LLM's own verdict (``summary_json.chunk_status``) and
its embedding is already in Chroma's ``chunks`` collection, so a gate can be calibrated
(or trained) entirely from data the pipeline already produced, with zero new LLM calls
and zero new embedding calls.

Usage:
    .venv/Scripts/python.exe scripts/calibrate_pre_llm_gate.py
    .venv/Scripts/python.exe scripts/calibrate_pre_llm_gate.py --state-db data/state.db --chroma-path data/chroma

Label caveat: at the time this spike ran, the local corpus predates issue #52
(rejection-taxonomy persistence), so ``summary_json`` has no ``rejection_category`` for
any chunk. The label used here is therefore the binary ``chunk_status`` (accepted vs.
rejected) the issue explicitly anticipates as the fallback ("Sem ela, o rotulo e binario
e menos util") -- not the 5-way taxonomy. Re-run after a re-extract on a taxonomy-tagged
corpus to get the richer label.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Record:
    chunk_id: str
    text: str
    section_path: str
    label: int  # 1 = accepted, 0 = rejected
    embedding: list[float] | None = None


def load_dataset(state_db_path: Path, chroma_path: Path) -> list[Record]:
    conn = sqlite3.connect(str(state_db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT chunk_id, text, section_path, summary_json FROM chunks"
    ).fetchall()
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
        records.append(Record(
            chunk_id=r["chunk_id"],
            text=r["text"] or "",
            section_path=r["section_path"] or "",
            label=1 if status == "accepted" else 0,
        ))

    if not records:
        return records

    import chromadb
    client = chromadb.PersistentClient(path=str(chroma_path))
    coll = client.get_collection("chunks")
    ids = [r.chunk_id for r in records]
    got = coll.get(ids=ids, include=["embeddings"])
    emb_by_id = dict(zip(got["ids"], got["embeddings"]))
    for r in records:
        vec = emb_by_id.get(r.chunk_id)
        r.embedding = list(vec) if vec is not None else None

    return [r for r in records if r.embedding is not None]


# ── Cheap heuristic baseline ─────────────────────────────────────────────

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
    tp = sum(1 for l, p in zip(labels, preds) if l == 1 and p == 1)
    fp = sum(1 for l, p in zip(labels, preds) if l == 0 and p == 1)
    fn = sum(1 for l, p in zip(labels, preds) if l == 1 and p == 0)
    tn = sum(1 for l, p in zip(labels, preds) if l == 0 and p == 0)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    calls_avoided = (fp + tn) / len(labels) if labels else 0.0
    false_negative_rate = fn / (tp + fn) if (tp + fn) else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "recall": recall,
        "calls_avoided_pct": 100 * calls_avoided,
        "accepted_notes_lost_pct": 100 * false_negative_rate,
    }


# ── Logistic regression on existing embeddings ────────────────────────────

def evaluate_classifier(records: list[Record]) -> dict | None:
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import StratifiedKFold, cross_val_predict
    except ImportError:
        return None

    X = np.array([r.embedding for r in records])
    y = np.array([r.label for r in records])
    if len(set(y.tolist())) < 2:
        return None  # cross-val needs both classes present

    n_splits = min(5, min((y == 0).sum(), (y == 1).sum()))
    if n_splits < 2:
        return None

    cv = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=0)
    clf = LogisticRegression(max_iter=1000, class_weight="balanced")
    proba = cross_val_predict(clf, X, y, cv=cv, method="predict_proba")[:, 1]

    # Recommendation: at most 1% of accepted chunks lost (recall >= 0.99 on
    # the accepted/positive class). Scan from strict (high threshold, few
    # calls) to permissive (low threshold, recall -> 1 as t -> 0); take the
    # first -- i.e. strictest, most calls-avoided -- one that qualifies.
    thresholds = sorted(set(proba.tolist()), reverse=True) + [0.0]
    best = None
    for t in thresholds:
        preds = (proba >= t).astype(int)
        stats = evaluate_predictions(y.tolist(), preds.tolist())
        if stats["accepted_notes_lost_pct"] <= 1.0:
            best = {"threshold": t, **stats}
            break
    return {
        "n": len(records),
        "n_accepted": int(y.sum()),
        "n_rejected": int((y == 0).sum()),
        "recommended_operating_point": best,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", type=Path, default=Path("data/state.db"))
    parser.add_argument("--chroma-path", type=Path, default=Path("data/chroma"))
    args = parser.parse_args()

    records = load_dataset(args.state_db, args.chroma_path)
    print(f"Dataset: {len(records)} chunks rotulados com embedding disponivel")
    if not records:
        print("Nada para calibrar -- sem chunks rotulados no state.db informado.")
        return

    n_accepted = sum(r.label for r in records)
    print(f"  accepted={n_accepted} rejected={len(records) - n_accepted}")

    print("\n== Heuristica barata (piso de tamanho + alnum ratio + densidade de tabela) ==")
    labels = [r.label for r in records]
    preds = [heuristic_predict(r.text) for r in records]
    stats = evaluate_predictions(labels, preds)
    for k, v in stats.items():
        print(f"  {k}: {v:.3f}" if isinstance(v, float) else f"  {k}: {v}")

    print("\n== Classificador (regressao logistica sobre embeddings existentes, CV) ==")
    clf_result = evaluate_classifier(records)
    if clf_result is None:
        print("  scikit-learn indisponivel, ou dataset insuficiente para validacao cruzada.")
    else:
        print(f"  n={clf_result['n']} accepted={clf_result['n_accepted']} rejected={clf_result['n_rejected']}")
        rec = clf_result["recommended_operating_point"]
        if rec is None:
            print("  Nenhum limiar do classificador manteve <=1% de notas aceitas perdidas.")
        else:
            print(f"  Ponto de operacao recomendado: threshold={rec['threshold']:.3f}")
            print(f"    recall (aceitos mantidos): {rec['recall']:.3f}")
            print(f"    precision: {rec['precision']:.3f}")
            print(f"    chamadas evitadas: {rec['calls_avoided_pct']:.1f}%")
            print(f"    notas aceitas perdidas: {rec['accepted_notes_lost_pct']:.2f}%")


if __name__ == "__main__":
    main()
