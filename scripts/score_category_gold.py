"""Score the per-note category sheet of #295 and apply the pre-registered rule.

Reads the sheet the human filled (``categoria-notas-planilha.csv``) and the
frozen key written by ``scripts/export_category_gold.py``, and reports:

* accuracy of each condition (A ``label``, B ``leaves_in_label``, C ``max_leaf``,
  D Jev on the notes it covers) against the human label, ``nenhuma`` included
  (an argmax with no threshold always answers a category, so it is wrong there);
* exact McNemar of B, C and D against A on the same items;
* for the winning condition, a sweep of ``gardener.assign_min_similarity``: a
  note under the threshold answers ``nenhuma``;
* the decisions of ``evals/preregistration/295-atribuicao-categoria.md``.

No model call. Usage:
    .venv/Scripts/python.exe scripts/score_category_gold.py \
        --key .eval-work/categoria-notas/categoria-notas-GABARITO-NAO-ABRIR.json \
        --sheet .eval-work/categoria-notas/categoria-notas-planilha.csv \
        --out evals/results/295-atribuicao-categoria.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from score_decision_gold import read_sheet
from zettel.evals.stats import paired_exact_p, paired_mid_p, wilson
from zettel.search_terms import fold

MIN_ITEMS = 100
MIN_GAIN = 0.05
MIN_THRESHOLD_GAIN = 0.02
MIN_JEV_ITEMS = 30
ALPHA = 0.05
CHALLENGERS = ("B", "C")


def labels(sheet: Mapping[str, Mapping[str, str]], key: Mapping[str, Any]) -> dict[str, str]:
    """item_id -> category name or ``nenhuma``; blank, ``?`` and unknown answers are dropped."""
    by_number = {str(c["n"]): c["categoria"] for c in key["categories"]}
    by_name = {fold(c["categoria"]): c["categoria"] for c in key["categories"]}
    none = key["answers"]["none"]
    out: dict[str, str] = {}
    for item_id, row in sheet.items():
        raw = (row.get("categoria") or "").strip()
        if not raw or raw == key["answers"]["unjudgeable"]:
            continue
        if fold(raw) == fold(none):
            out[item_id] = none
        elif raw in by_number:
            out[item_id] = by_number[raw]
        elif fold(raw) in by_name:
            out[item_id] = by_name[fold(raw)]
    return out


def predict(item: Mapping[str, Any], cond: str, threshold: float | None, none: str) -> str | None:
    answer = item["conditions"][cond]
    if answer.get("choice") is None:
        return None
    if threshold is not None and answer["score"] < threshold:
        return none
    return answer["choice"]


def hits(
    items: list[Mapping[str, Any]],
    gold: Mapping[str, str],
    cond: str,
    none: str,
    threshold: float | None = None,
) -> dict[str, bool]:
    """item_id -> correct, for the labelled items the condition answers."""
    out: dict[str, bool] = {}
    for item in items:
        if item["item_id"] not in gold:
            continue
        guess = predict(item, cond, threshold, none)
        if guess is not None:
            out[item["item_id"]] = guess == gold[item["item_id"]]
    return out


def compare(a: Mapping[str, bool], b: Mapping[str, bool]) -> dict[str, Any]:
    """Accuracy of both on their shared items and the exact McNemar between them."""
    shared = sorted(set(a) & set(b))
    only_a = sum(1 for i in shared if a[i] and not b[i])
    only_b = sum(1 for i in shared if b[i] and not a[i])
    n = len(shared)
    return {
        "n": n,
        "acc_a": round(sum(a[i] for i in shared) / n, 4) if n else None,
        "acc_b": round(sum(b[i] for i in shared) / n, 4) if n else None,
        "acc_a_ci95": wilson(sum(a[i] for i in shared), n),
        "acc_b_ci95": wilson(sum(b[i] for i in shared), n),
        "only_a_right": only_a,
        "only_b_right": only_b,
        "mcnemar_p": round(paired_exact_p(only_a, only_b), 4),
        "mid_p": round(paired_mid_p(only_a, only_b), 4),
    }


def threshold_sweep(
    items: list[Mapping[str, Any]], gold: Mapping[str, str], cond: str, none: str
) -> list[dict[str, Any]]:
    """Accuracy at every observed score of ``cond`` used as the threshold."""
    scores = sorted(
        {item["conditions"][cond]["score"] for item in items if item["item_id"] in gold}
    )
    rows = []
    for t in scores:
        h = hits(items, gold, cond, none, t)
        unassigned = sum(
            1 for i in items if i["item_id"] in gold and i["conditions"][cond]["score"] < t
        )
        rows.append(
            {
                "threshold": t,
                "accuracy": round(sum(h.values()) / len(h), 4) if h else 0.0,
                "unassigned": unassigned,
            }
        )
    return rows


def score(key: Mapping[str, Any], sheet: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    none = key["answers"]["none"]
    gold = labels(sheet, key)
    items = key["items"]
    result: dict[str, Any] = {
        "labelled": len(gold),
        "labelled_none": sum(1 for v in gold.values() if v == none),
        "population": key["population"],
    }
    base = hits(items, gold, "A", none)
    by_cond = {cond: hits(items, gold, cond, none) for cond in ("A", "B", "C", "D")}
    result["accuracy"] = {
        cond: round(sum(h.values()) / len(h), 4) if h else None for cond, h in by_cond.items()
    }
    result["accuracy_ci95"] = {cond: wilson(sum(h.values()), len(h)) for cond, h in by_cond.items()}
    result["vs_A"] = {
        cond: compare(base, hits(items, gold, cond, none)) for cond in (*CHALLENGERS, "D")
    }
    result["decision"] = decide(result, items, gold, none)
    return result


def decide(
    result: dict[str, Any], items: list[Mapping[str, Any]], gold: Mapping[str, str], none: str
) -> dict[str, Any]:
    """The rules of evals/preregistration/295-atribuicao-categoria.md."""
    if result["labelled"] < MIN_ITEMS:
        return {"status": "inconclusivo", "reason": f"menos de {MIN_ITEMS} itens rotulados"}

    winners = [
        cond
        for cond in CHALLENGERS
        if (cmp := result["vs_A"][cond])["acc_b"] is not None
        and cmp["acc_b"] - cmp["acc_a"] >= MIN_GAIN
        and cmp["mcnemar_p"] < ALPHA
    ]
    winner = max(winners, key=lambda c: result["accuracy"][c]) if winners else "A"

    plain = hits(items, gold, winner, none)
    sweep = threshold_sweep(items, gold, winner, none)
    best = max(sweep, key=lambda r: (r["accuracy"], -r["threshold"])) if sweep else None
    threshold = None
    threshold_test = None
    if best is not None:
        threshold_test = compare(plain, hits(items, gold, winner, none, best["threshold"]))
        gain = (threshold_test["acc_b"] or 0) - (threshold_test["acc_a"] or 0)
        if gain >= MIN_THRESHOLD_GAIN and threshold_test["mcnemar_p"] < ALPHA:
            threshold = best["threshold"]

    jev = compare(plain, hits(items, gold, "D", none))
    if jev["n"] < MIN_JEV_ITEMS:
        shadow = "manter em shadow (cobertura insuficiente)"
    elif (jev["acc_b"] or 0) - (jev["acc_a"] or 0) >= MIN_GAIN and jev["mcnemar_p"] < ALPHA:
        shadow = "abrir issue de portao (Jev supera a atribuicao vencedora)"
    else:
        shadow = "desligar o shadow (nao supera a atribuicao vencedora)"

    modes = {"A": "label", "B": "leaves_in_label", "C": "max_leaf"}
    return {
        "status": "decidido",
        "category_label_mode": modes[winner],
        "assign_min_similarity": threshold,
        "threshold_best": best,
        "threshold_test": threshold_test,
        "threshold_sweep": sweep,
        "jev_vs_winner": jev,
        "moc_category_shadow": shadow,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--sheet", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    key = json.loads(args.key.read_text(encoding="utf-8"))
    result = score(key, read_sheet(args.sheet.read_bytes()))
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
