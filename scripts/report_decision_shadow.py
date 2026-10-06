"""Report the typed decision layer's shadow verdicts against what the pipeline decided (#206).

Reads `decision_shadow` from `data/state.db` (read-only) and answers, per site,
the questions the pre-registration (`evals/preregistration/206-jev-camada-decisao.md`)
uses to decide whether a gate issue is worth opening:

* how many decisions, and how many failed or were skipped;
* how often the decision model agrees with the current decision -- overall and
  by its own confidence band (>= 0.9, 0.6-0.9, < 0.6);
* for dedupe, agreement with the reviewer's m/d decision, next to the LLM's;
* stability (mean spread across order permutations) and latency.

Offline and deterministic: no model call, sorted keys, no timestamps.

Usage:
    .venv/Scripts/python.exe scripts/report_decision_shadow.py
    .venv/Scripts/python.exe scripts/report_decision_shadow.py --out evals/results/shadow.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# Run from a clone without installing the package (mirrors scripts/ precedent).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BANDS = (("high", 0.9, 1.01), ("mid", 0.6, 0.9), ("low", 0.0, 0.6))
JUDGE_PASS = 7.0  # retrieval.article.judge_min_score


# -- Pure pieces ---------------------------------------------------------


def band_of(confidence: float) -> str:
    return next(name for name, low, high in BANDS if low <= confidence < high)


def _rate(hits: int, n: int) -> float | None:
    return round(hits / n, 4) if n else None


def agreement_by_band(pairs: list[tuple[bool, float]]) -> dict[str, Any]:
    """pairs: (agrees with baseline, confidence)."""
    out: dict[str, Any] = {
        "all": {"n": len(pairs), "agreement": _rate(sum(a for a, _ in pairs), len(pairs))}
    }
    for name, _, _ in BANDS:
        sub = [a for a, c in pairs if band_of(c) == name]
        out[name] = {"n": len(sub), "agreement": _rate(sum(sub), len(sub))}
    return out


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _common(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    answered = [r for r in rows if r["jev"] and not r["error"]]
    errors = Counter(
        "skipped:too_large" if r["error"] == "skipped:too_large" else "api"
        for r in rows
        if r["error"]
    )
    latencies = [int(r["latency_ms"]) for r in answered if r["latency_ms"] is not None]
    spreads = [
        float(a["spread"])
        for r in answered
        for a in r["jev"].values()
        if isinstance(a, dict) and "spread" in a
    ]
    return answered, {
        "n": len(rows),
        "answered": len(answered),
        "errors": dict(sorted(errors.items())),
        "error_rate": _rate(errors.get("api", 0), len(rows)),
        "latency_ms_p50": _percentile(latencies, 0.5),
        "latency_ms_p95": _percentile(latencies, 0.95),
        "spread_mean": round(statistics.fmean(spreads), 4) if spreads else None,
        "input_tokens": sum(int(r["input_tokens"] or 0) for r in answered),
    }


def report_dedupe(rows: list[dict[str, Any]]) -> dict[str, Any]:
    answered, out = _common(rows)
    decision_pairs = [
        (
            r["jev"]["decision"]["choice"] == r["baseline"]["decision"],
            float(r["jev"]["decision"]["confidence"]),
        )
        for r in answered
    ]
    out["decision"] = agreement_by_band(decision_pairs)
    out["decision_confusion"] = dict(
        sorted(
            Counter(
                f"{r['baseline']['decision']}->{r['jev']['decision']['choice']}" for r in answered
            ).items()
        )
    )
    linked = [r for r in answered if r["baseline"]["decision"] != "create_new"]
    out["target_when_llm_flags"] = agreement_by_band(
        [
            (
                r["jev"]["target"]["choice"] == r["baseline"]["target"],
                float(r["jev"]["target"]["confidence"]),
            )
            for r in linked
        ]
    )
    labelled = [r for r in answered if r["human"]]
    llm_right = sum(
        (r["baseline"]["decision"] == "ignore") == (r["human"]["verdict"] == "ignore")
        for r in labelled
    )
    jev_right = sum(
        (r["jev"]["decision"]["choice"] == "ignore") == (r["human"]["verdict"] == "ignore")
        for r in labelled
    )
    out["human"] = {
        "n": len(labelled),
        "llm_agreement": _rate(llm_right, len(labelled)),
        "jev_agreement": _rate(jev_right, len(labelled)),
    }
    return out


def report_moc_category(rows: list[dict[str, Any]]) -> dict[str, Any]:
    answered, out = _common(rows)
    assigned = [r for r in answered if r["baseline"]["category"] != "_unassigned"]
    out["category"] = agreement_by_band(
        [
            (
                r["jev"]["category"]["choice"] == r["baseline"]["category"],
                float(r["jev"]["category"]["confidence"]),
            )
            for r in assigned
        ]
    )
    out["unassigned_baseline"] = len(answered) - len(assigned)
    out["jev_none_rate"] = _rate(
        sum(r["jev"]["category"]["choice"] == "none" for r in answered), len(answered)
    )
    return out


def report_article_judge(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from zettel.decision.sites import judge_score_0_10

    answered, out = _common(rows)
    by_dim: dict[str, list[tuple[float, float]]] = {}
    for r in answered:
        dimension = r["subject_id"].rsplit(":", 1)[1]
        llm = r["baseline"]["score_0_10"]
        if llm is None:
            continue
        by_dim.setdefault(dimension, []).append(
            (judge_score_0_10(float(r["jev"][dimension]["score"])), float(llm))
        )
    out["dimensions"] = {
        dim: {
            "n": len(pairs),
            "mean_abs_diff_0_10": round(statistics.fmean(abs(j - llm) for j, llm in pairs), 3),
            "jev_mean_0_10": round(statistics.fmean(j for j, _ in pairs), 3),
            "llm_mean_0_10": round(statistics.fmean(llm for _, llm in pairs), 3),
            "pass_agreement": _rate(
                sum((j >= JUDGE_PASS) == (llm >= JUDGE_PASS) for j, llm in pairs), len(pairs)
            ),
        }
        for dim, pairs in sorted(by_dim.items())
    }
    return out


REPORTERS = {
    "dedupe": report_dedupe,
    "moc_category": report_moc_category,
    "article_judge": report_article_judge,
}


def build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_site: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_site.setdefault(r["site"], []).append(r)
    return {
        "models": sorted({r["model"] for r in rows}),
        "sites": {
            site: REPORTERS[site](site_rows)
            for site, site_rows in sorted(by_site.items())
            if site in REPORTERS
        },
    }


# -- IO ------------------------------------------------------------------


def load_rows(state_db: Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_shadow'"
        ).fetchone()
        if not exists:  # a state.db no pipeline command has opened since ADR-055
            return []
        rows = []
        for r in con.execute("SELECT * FROM decision_shadow ORDER BY site, created_at, subject_id"):
            row = dict(r)
            for col, key in (
                ("baseline_json", "baseline"),
                ("jev_json", "jev"),
                ("human_json", "human"),
            ):
                value = row.pop(col)
                row[key] = json.loads(value) if value else None
            rows.append(row)
        return rows
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--state-db", type=Path, default=Path("data/state.db"))
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    report = build_report(load_rows(args.state_db))
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(text, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
