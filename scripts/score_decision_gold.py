"""Score shadow decisions against the blind human sheet (#206, #208, #209).

`scripts/export_decision_gold.py` freezes a key (`*-GABARITO-NAO-ABRIR.json`) and a
blind sheet; a human fills the sheet. This script joins the two and answers, per
deciding system, how often it agrees with the human -- the number the
pre-registrations (`evals/preregistration/206-jev-camada-decisao.md`, `#209`,
`208-jev-corroborates.md`) rule on. Sites: `dedupe` and `corroborates`.

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
* **Corroborates (#208).** The edge is binary, so a pair is `same_idea` only when
  the human answered `mesma-ideia`. Conditions: `threshold` (the edge `connect`
  actually created), `jev` (level 2 of the mean of both orders) and, reported
  only, `cosine` (similarity >= threshold, which differs from the edge where the
  hit was not a seed or the per-note cap was reached). The three levels are
  reported as a confusion matrix, and the continuous signals (Jev mean score,
  cosine) as an AUC against `same_idea`.

Offline and deterministic: reads the sheet and the key, calls nothing. With
`--labels-out` it writes the human labels without any source text (committable).

Usage:
    .venv/Scripts/python.exe scripts/score_decision_gold.py --site dedupe
    .venv/Scripts/python.exe scripts/score_decision_gold.py --site dedupe \\
        --labels-out evals/gold/dedupe-rotulos.json --out evals/results/dedupe-gold-206.json
    .venv/Scripts/python.exe scripts/score_decision_gold.py --site dedupe \\
        --key evals/gold/dedupe-r2-GABARITO-NAO-ABRIR.json \\
        --sheet evals/gold/dedupe-r2-planilha.csv --labels-out evals/gold/dedupe-r2-rotulos.json
    .venv/Scripts/python.exe scripts/score_decision_gold.py --site corroborates \\
        --labels-out evals/gold/corroboracao-rotulos.json \\
        --out evals/results/corroborates-gold-208.json
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

from calibrate_review_confidence import auc_ci, min_detectable_auc
from compare_gold_runs import paired_exact_p

DEDUPE_ANSWERS = {"nova": "create_new", "repete": "ignore", "desenvolve": "link"}
UNJUDGEABLE = "?"
BANDS = (("high", 0.9, 1.01), ("mid", 0.6, 0.9), ("low", 0.0, 0.6))
# Pre-registration #206: the human rule needs at least this many labels.
MIN_HUMAN_LABELS = 10
CORROBORATES_ANSWERS = {"diferente": 0, "mesmo-tema": 1, "mesma-ideia": 2}
SAME_IDEA, OTHER = "same_idea", "other"
# Pre-registration #208: sample size, per-band floor and the order-bias ceiling.
MIN_CORROBORATES_LABELS = 30
MIN_CORROBORATES_PER_BAND = 5
MAX_ORDER_DIVERGENCE = 0.3
CORROBORATES_BANDS = ("low_band", "near_threshold", "above_threshold")
DEFAULT_FILES = {
    "dedupe": ("evals/gold/dedupe-GABARITO-NAO-ABRIR.json", "evals/gold/dedupe-planilha.csv"),
    "corroborates": (
        "evals/gold/corroboracao-GABARITO-NAO-ABRIR.json",
        "evals/gold/corroboracao-planilha.csv",
    ),
}


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


def corroborates_labels(
    sheet: dict[str, dict[str, str]], key: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Human labels joined to the key: ``human_level`` 0..2 and the binary
    ``human_decision`` the rule scores (both None for `?`)."""
    labels: list[dict[str, Any]] = []
    problems: dict[str, list[str]] = defaultdict(list)
    for item in key["items"]:
        row = sheet.get(item["item_id"])
        if row is None or not row.get("decisao"):
            problems["sem_resposta"].append(item["item_id"])
            continue
        answer = row["decisao"].lower()
        if answer != UNJUDGEABLE and answer not in CORROBORATES_ANSWERS:
            problems["decisao_invalida"].append(item["item_id"])
            continue
        level = CORROBORATES_ANSWERS.get(answer)
        labels.append(
            {
                "item_id": item["item_id"],
                "subject_id": item["subject_id"],
                "sampling_stratum": item["sampling_stratum"],
                "human_level": level,
                "human_decision": None if level is None else SAME_IDEA if level == 2 else OTHER,
                "human_note": row.get("nota", ""),
            }
        )
    return labels, dict(problems)


def jev_mean_score(item: dict[str, Any]) -> float:
    """The pair's 0..2 score: mean of both orders, as `report_decision_shadow` reads it."""
    return (float(item["jev"]["ab_score"]) + float(item["jev"]["ba_score"])) / 2


def corroborates_conditions(key: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """threshold (edge created), jev (level 2) and cosine (similarity >= threshold)."""
    from zettel.decision.sites import corroborates_level

    def binary(same: bool) -> dict[str, Any]:
        return {"decision": SAME_IDEA if same else OTHER}

    out: dict[str, dict[str, dict[str, Any]]] = {"threshold": {}, "jev": {}, "cosine": {}}
    for item in key["items"]:
        base = item["baseline"]
        out["threshold"][item["item_id"]] = binary(bool(base["edge"]))
        out["jev"][item["item_id"]] = binary(corroborates_level(jev_mean_score(item)) == 2)
        out["cosine"][item["item_id"]] = binary(base["similarity"] >= base["threshold"])
    return out


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
        if lab.get("human_target") is not None
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
    }
    if any("human_target" in lab for lab in judged):
        out["target"] = {"n": len(targets), "accuracy": _rate(sum(targets), len(targets))}
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


def corroborates_detail(labels: list[dict[str, Any]], key: dict[str, Any]) -> dict[str, Any]:
    """The three levels (human x Jev), the AUC of the continuous signals against
    `same_idea`, and the order divergence over the labelled pairs."""
    from zettel.decision.sites import corroborates_level

    items = {i["item_id"]: i for i in key["items"]}
    judged = [items[lab["item_id"]] | lab for lab in labels if lab["human_level"] is not None]
    same = [i for i in judged if i["human_decision"] == SAME_IDEA]
    other = [i for i in judged if i["human_decision"] == OTHER]

    def signal_auc(read: Any) -> dict[str, Any]:
        positive, negative = [read(i) for i in same], [read(i) for i in other]
        return {
            **auc_ci(positive, negative),
            "n_same_idea": len(positive),
            "n_other": len(negative),
            "min_detectable_auc": min_detectable_auc(len(positive), len(negative)),
        }

    levels = Counter(f"{i['human_level']}->{corroborates_level(jev_mean_score(i))}" for i in judged)
    by_band: dict[str, Counter] = defaultdict(Counter)
    for i in judged:
        by_band[i["sampling_stratum"]][str(i["human_level"])] += 1
    divergence = [abs(float(i["jev"]["ab_score"]) - float(i["jev"]["ba_score"])) for i in judged]
    return {
        "human_level_to_jev_level": dict(sorted(levels.items())),
        "human_levels_by_band": {b: dict(sorted(c.items())) for b, c in sorted(by_band.items())},
        "auc_same_idea": {
            "jev_mean_score": signal_auc(jev_mean_score),
            "cosine": signal_auc(lambda i: float(i["baseline"]["similarity"])),
        },
        "order_divergence_mean": _rate(sum(divergence), len(divergence)),
    }


def preregistered_rule_208(result: dict[str, Any]) -> dict[str, Any]:
    """#208: open a gate issue iff the sample suffices (rule 1) and Jev beats the
    threshold with exact McNemar p < 0.05 (rule 2). Rule 3 (order) only binds a
    future gate to asking both orders."""
    jev, threshold = result["conditions"]["jev"], result["conditions"]["threshold"]
    per_band = {b: jev["by_stratum"].get(b, {}).get("n", 0) for b in CORROBORATES_BANDS}
    sample = jev["n"] >= MIN_CORROBORATES_LABELS and all(
        n >= MIN_CORROBORATES_PER_BAND for n in per_band.values()
    )
    beats = (
        jev["correct"] > threshold["correct"]
        and result["pairs"]["threshold:jev"]["mcnemar_p"] < 0.05
    )
    divergence = result["detail"]["order_divergence_mean"]
    return {
        "labels_per_band": per_band,
        "rule1_sample": sample,
        "rule2_jev_beats_threshold": beats,
        "rule3_order_divergence_ok": divergence is not None and divergence <= MAX_ORDER_DIVERGENCE,
        "open_gate_issue": sample and beats,
    }


def score_dedupe(labels: list[dict[str, Any]], key: dict[str, Any]) -> dict[str, Any]:
    result = score_conditions(labels, key_conditions(key), key["population"], [("llm", "jev")])
    result["reviewer_vs_sheet"] = reviewer_agreement(labels, key)
    result["preregistered_human_rule_206"] = preregistered_human_rule(result)
    return result


def score_corroborates(labels: list[dict[str, Any]], key: dict[str, Any]) -> dict[str, Any]:
    result = score_conditions(
        labels,
        corroborates_conditions(key),
        key["population"],
        [("threshold", "jev"), ("cosine", "jev")],
    )
    result["detail"] = corroborates_detail(labels, key)
    result["preregistered_rule_208"] = preregistered_rule_208(result)
    return result


SITES = {
    "dedupe": (dedupe_labels, score_dedupe),
    "corroborates": (corroborates_labels, score_corroborates),
}


# -- CLI -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--site", choices=tuple(SITES), required=True)
    parser.add_argument("--key", type=Path, default=None, help="Padrao: o gabarito do site")
    parser.add_argument(
        "--sheet",
        type=Path,
        default=None,
        help="Planilha preenchida da mesma rodada do gabarito (--key); padrao: a do site",
    )
    parser.add_argument("--labels-out", type=Path, default=None)
    parser.add_argument(
        "--labels-method",
        default="manual_blind",
        help="Como os rotulos foram produzidos (manual_blind, score_thresholds, llm...)",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    default_key, default_sheet = DEFAULT_FILES[args.site]
    key = json.loads((args.key or Path(default_key)).read_text(encoding="utf-8"))
    if key.get("site", args.site) != args.site:
        parser.error(f"o gabarito e do site {key['site']!r}, nao {args.site!r}")
    sheet = read_sheet((args.sheet or Path(default_sheet)).read_bytes())
    read_labels, score = SITES[args.site]
    labels, problems = read_labels(sheet, key)
    if problems:
        print(f"Linhas fora da conta: {json.dumps(problems, ensure_ascii=False)}")

    result = score(labels, key)
    result["unjudgeable"] = sum(1 for lab in labels if lab["human_decision"] is None)
    result["human_distribution"] = dict(Counter(lab["human_decision"] for lab in labels))

    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(text, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    if args.labels_out:
        payload = {
            "site": args.site,
            "key_exported_at": key["exported_at"],
            "method": args.labels_method,
            "labels": labels,
        }
        args.labels_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
