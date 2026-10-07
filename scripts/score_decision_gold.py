"""Score shadow decisions against the blind human sheet (#206, #208, #209).

`scripts/export_decision_gold.py` freezes a key (`*-GABARITO-NAO-ABRIR.json`) and a
blind sheet; a human fills the sheet. This script joins the two and answers, per
deciding system, how often it agrees with the human -- the number the
pre-registrations (`evals/preregistration/206-jev-camada-decisao.md`, `#209`) rule
on. Today: `dedupe`.

* **Conditions.** The key carries two: `llm` (the decision the pipeline took) and
  `jev` (the shadow answer). Other scripts add more through
  `score_conditions` -- #209 re-runs the dedupe LLM with full note text over the
  same labelled items.
* **Decision.** Three-way: `create_new` / `ignore` / `link`. `refine_existing` and
  `merge` are already collapsed into `link` in the key, as in the pipeline, where
  both have the same effect.
* **Target.** Scored only where the human *and* the condition both name a target
  (`ignore` / `link`): does it point at the same note?
* **Weighting.** The sample over-represents the rare strata (`ignore` taken
  whole), so corpus estimates weight each item by `population / sampled` of its
  stratum; raw counts are reported next to them.
* **Paired test.** Exact McNemar on the items exactly one of two conditions gets
  right (`compare_gold_runs.paired_exact_p`).
* `?` answers are counted and excluded, never scored.

Offline and deterministic: reads the sheet and the key, calls nothing. With
`--labels-out` it writes the human labels without any source text (committable).

Usage:
    .venv/Scripts/python.exe scripts/score_decision_gold.py --site dedupe
    .venv/Scripts/python.exe scripts/score_decision_gold.py --site dedupe \\
        --labels-out evals/gold/dedupe-rotulos.json --out evals/results/dedupe-gold-206.json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# Run from a clone without installing the package (mirrors scripts/ precedent).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_gold_runs import paired_exact_p

DEDUPE_ANSWERS = {"nova": "create_new", "repete": "ignore", "desenvolve": "link"}
UNJUDGEABLE = "?"
BANDS = (("high", 0.9, 1.01), ("mid", 0.6, 0.9), ("low", 0.0, 0.6))
# Pre-registration #206: the human rule needs at least this many labels.
MIN_HUMAN_LABELS = 10


# -- Reading -------------------------------------------------------------


def read_sheet(raw: bytes) -> dict[str, dict[str, str]]:
    """item_id -> filled columns. Any encoding / `;` or `,` the spreadsheet saved."""
    from zettel.evals.extraction import decode_sheet

    text, _encoding = decode_sheet(raw)
    first = text.splitlines()[0] if text else ""
    delimiter = ";" if first.count(";") >= first.count(",") else ","
    return {
        row["item_id"].strip(): {k: (v or "").strip() for k, v in row.items() if k}
        for row in csv.DictReader(io.StringIO(text), delimiter=delimiter)
        if (row.get("item_id") or "").strip()
    }


def dedupe_labels(
    sheet: dict[str, dict[str, str]], key: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Human labels joined to the key; plus the rows that could not be read.

    A label is ``decision`` in create_new/ignore/link (or None for `?`) and
    ``target`` -- the note id behind the letter -- for ignore/link.
    """
    labels: list[dict[str, Any]] = []
    problems: dict[str, list[str]] = defaultdict(list)
    for item in key["items"]:
        row = sheet.get(item["item_id"])
        if row is None or not row.get("decisao"):
            problems["sem_resposta"].append(item["item_id"])
            continue
        answer = row["decisao"].lower()
        if answer == UNJUDGEABLE:
            decision = None
        elif answer in DEDUPE_ANSWERS:
            decision = DEDUPE_ANSWERS[answer]
        else:
            problems["decisao_invalida"].append(item["item_id"])
            continue
        target = None
        if decision in ("ignore", "link"):
            letter = row.get("alvo", "").upper()
            target = item["letters"].get(letter)
            if target is None:
                problems["alvo_invalido"].append(item["item_id"])
                continue
        labels.append(
            {
                "item_id": item["item_id"],
                "subject_id": item["subject_id"],
                "sampling_stratum": item["sampling_stratum"],
                "human_decision": decision,
                "human_target": target,
                "human_note": row.get("nota", ""),
            }
        )
    return labels, dict(problems)


def key_conditions(key: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """The two conditions every dedupe key carries: the pipeline LLM and the shadow."""
    llm, jev = {}, {}
    for item in key["items"]:
        base = item["baseline"]
        llm[item["item_id"]] = {
            "decision": base["decision"],
            "target": None if base["target"] == "none" else base["target"],
        }
        jev[item["item_id"]] = {
            "decision": item["jev"]["decision"],
            "target": None if item["jev"]["target"] == "none" else item["jev"]["target"],
            "confidence": item["jev"]["decision_confidence"],
        }
    return {"llm": llm, "jev": jev}


# -- Scoring -------------------------------------------------------------


def _band(confidence: float) -> str:
    return next(name for name, low, high in BANDS if low <= confidence < high)


def _rate(hits: float, n: float) -> float | None:
    return round(hits / n, 4) if n else None


def score_condition(
    labels: list[dict[str, Any]],
    answers: dict[str, dict[str, Any]],
    weights: dict[str, float],
) -> dict[str, Any]:
    judged = [lab for lab in labels if lab["human_decision"] and lab["item_id"] in answers]
    right = {
        lab["item_id"]: answers[lab["item_id"]]["decision"] == lab["human_decision"]
        for lab in judged
    }
    weighted_total = sum(weights[i] for i in right)
    weighted_right = sum(weights[i] for i, ok in right.items() if ok)
    by_stratum: dict[str, list[bool]] = defaultdict(list)
    for lab in judged:
        by_stratum[lab["sampling_stratum"]].append(right[lab["item_id"]])
    targets = [
        answers[lab["item_id"]]["target"] == lab["human_target"]
        for lab in judged
        if lab["human_decision"] != "create_new"
        and answers[lab["item_id"]]["decision"] != "create_new"
    ]
    out: dict[str, Any] = {
        "n": len(judged),
        "correct": sum(right.values()),
        "accuracy": _rate(sum(right.values()), len(judged)),
        "accuracy_weighted": _rate(weighted_right, weighted_total),
        "by_stratum": {
            s: {"n": len(v), "accuracy": _rate(sum(v), len(v))}
            for s, v in sorted(by_stratum.items())
        },
        "confusion_human_to_model": dict(
            sorted(
                Counter(
                    f"{lab['human_decision']}->{answers[lab['item_id']]['decision']}"
                    for lab in judged
                ).items()
            )
        ),
        "target": {"n": len(targets), "accuracy": _rate(sum(targets), len(targets))},
    }
    if all("confidence" in answers[lab["item_id"]] for lab in judged) and judged:
        bands: dict[str, list[bool]] = defaultdict(list)
        for lab in judged:
            bands[_band(float(answers[lab["item_id"]]["confidence"]))].append(right[lab["item_id"]])
        out["by_confidence_band"] = {
            name: {"n": len(bands[name]), "accuracy": _rate(sum(bands[name]), len(bands[name]))}
            for name, _, _ in BANDS
        }
    return {"summary": out, "right": right}


def score_conditions(
    labels: list[dict[str, Any]],
    conditions: dict[str, dict[str, dict[str, Any]]],
    population: dict[str, int],
    pairs: list[tuple[str, str]],
) -> dict[str, Any]:
    sampled = Counter(lab["sampling_stratum"] for lab in labels)
    weights = {
        lab["item_id"]: population.get(lab["sampling_stratum"], 0)
        / sampled[lab["sampling_stratum"]]
        for lab in labels
    }
    scored = {
        name: score_condition(labels, answers, weights) for name, answers in conditions.items()
    }
    paired = {}
    for a, b in pairs:
        common = set(scored[a]["right"]) & set(scored[b]["right"])
        only_a = sum(scored[a]["right"][i] and not scored[b]["right"][i] for i in common)
        only_b = sum(scored[b]["right"][i] and not scored[a]["right"][i] for i in common)
        paired[f"{a}:{b}"] = {
            "n": len(common),
            "only_a_right": only_a,
            "only_b_right": only_b,
            "mcnemar_p": round(paired_exact_p(only_a, only_b), 4),
        }
    return {
        "conditions": {name: s["summary"] for name, s in scored.items()},
        "pairs": paired,
    }


def reviewer_agreement(labels: list[dict[str, Any]], key: dict[str, Any]) -> dict[str, Any]:
    """The reviewer's m/d in `zettel review` vs the same human's sheet answer."""
    reviewer = {i["item_id"]: i.get("reviewer") for i in key["items"] if i.get("reviewer")}
    pairs = [
        (reviewer[lab["item_id"]], lab["human_decision"])
        for lab in labels
        if lab["item_id"] in reviewer and lab["human_decision"]
    ]
    agree = sum((r == "ignore") == (h == "ignore") for r, h in pairs)
    return {"n": len(pairs), "agreement": _rate(agree, len(pairs))}


def preregistered_human_rule(result: dict[str, Any]) -> dict[str, Any]:
    """#206: with >= 10 human labels, the shadow must agree with the human at least as
    often as the current LLM does. Necessary, not sufficient, for a gate issue."""
    llm, jev = result["conditions"]["llm"], result["conditions"]["jev"]
    enough = jev["n"] >= MIN_HUMAN_LABELS
    return {
        "labels": jev["n"],
        "enough_labels": enough,
        "jev_at_least_llm": bool(enough and jev["accuracy"] >= llm["accuracy"]),
    }


# -- CLI -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--site", choices=("dedupe",), required=True)
    parser.add_argument("--gold-dir", type=Path, default=Path("evals/gold"))
    parser.add_argument("--labels-out", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    key = json.loads((args.gold_dir / "dedupe-GABARITO-NAO-ABRIR.json").read_text(encoding="utf-8"))
    sheet = read_sheet((args.gold_dir / "dedupe-planilha.csv").read_bytes())
    labels, problems = dedupe_labels(sheet, key)
    if problems:
        print(f"Linhas fora da conta: {json.dumps(problems, ensure_ascii=False)}")

    result = score_conditions(labels, key_conditions(key), key["population"], [("llm", "jev")])
    result["unjudgeable"] = sum(1 for lab in labels if lab["human_decision"] is None)
    result["reviewer_vs_sheet"] = reviewer_agreement(labels, key)
    result["preregistered_human_rule_206"] = preregistered_human_rule(result)
    result["human_distribution"] = dict(Counter(lab["human_decision"] for lab in labels))

    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(text, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    if args.labels_out:
        payload = {"site": "dedupe", "key_exported_at": key["exported_at"], "labels": labels}
        args.labels_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
