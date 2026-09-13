"""Calibration harness (issue #152): rescore `review_confidence` offline.

Every chunk `extract` processed already carries the model's full structured output
in ``chunks.summary_json``. The confidence score is a pure function of that output
plus config, so the whole scoring policy can be re-measured — old formula vs. new,
threshold sweep, per-term variance — with **zero LLM calls and zero embedding
calls**, over exactly the corpus the pipeline already produced.

Usage:
    .venv/Scripts/python.exe scripts/calibrate_review_confidence.py
    .venv/Scripts/python.exe scripts/calibrate_review_confidence.py --state-db data/state.db
    .venv/Scripts/python.exe scripts/calibrate_review_confidence.py --threshold 0.75 --json out.json
    .venv/Scripts/python.exe scripts/calibrate_review_confidence.py \
        --gold-labels evals/gold/extracao-rotulos.json \
        --gold-key evals/gold/extracao-GABARITO-NAO-ABRIR.json

With `--gold-labels`/`--gold-key` (issue #176) it also measures every term against
the human gold set of #175: AUC with a Hanley-McNeil 95% interval among accepted
chunks, how saturated each term is, and how many more accepted items would have to
be labeled before a signal of a given strength could be told apart from a coin.

`legacy_confidence` reproduces the pre-#152 formula (30% approval_ratio, 30%
relevance normalized to 0 at the floor, 40% mean definition word count) so a
before/after comparison is made against the real thing, not a description of it.

Caveat the operator must keep in mind: a calibration is only as wide as its
corpus. The script prints the number of sources and chunks it measured — a
threshold tuned on a single document is a provisional default, not a calibration.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as stats
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Run from a clone without installing the package (mirrors scripts/ precedent).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zettel.config import AppConfig, load_config  # noqa: E402
from zettel.extractor import _score_review_confidence  # noqa: E402
from zettel.schemas import LiteratureChunkOutput  # noqa: E402

# Chunks the scorer short-circuits before any term is computed. Keeping them out
# of the distribution stats is the point: they are policy constants (0.1 / 0.2),
# not scores, and averaging them in hides what the formula actually does.
SHORT_CIRCUIT = (0.1, 0.2)


@dataclass
class ChunkScore:
    chunk_id: str
    chunk_index: int
    source_id: str
    section_path: str
    legacy: float
    current: float
    n_approved: int
    n_rejected: int
    avg_relevance: float
    avg_definition_words: float
    completeness: float
    terms: dict[str, float] = field(default_factory=dict)

    @property
    def scored(self) -> bool:
        """True when the formula ran, rather than a short-circuit constant."""
        return self.n_approved > 0


def legacy_confidence(output: LiteratureChunkOutput, cfg: AppConfig, chunk_text: str) -> float:
    """The pre-#152 formula, kept verbatim for before/after comparison."""
    from zettel.extractor import _filter_candidates

    if output.chunk_status == "rejected":
        return 0.1
    if not output.candidates:
        return 0.2
    approved, rejected = _filter_candidates(output.candidates, cfg, chunk_text)
    if not approved:
        return 0.2

    ext = cfg.extraction
    n_total = len(approved) + len(rejected)
    approval_ratio = (len(approved) / n_total) if n_total else 1.0

    rel_span = 5 - ext.min_relevance_score
    avg_rel = sum(c.relevance_score for c in approved) / len(approved)
    rel_component = (
        min(1.0, max(0.0, (avg_rel - ext.min_relevance_score) / rel_span)) if rel_span > 0 else 1.0
    )

    depth_span = ext.min_definition_words * 5
    avg_def = sum(len(c.definition.split()) for c in approved) / len(approved)
    depth_component = (
        min(1.0, max(0.0, (avg_def - ext.min_definition_words) / depth_span))
        if depth_span > 0
        else 1.0
    )
    return round(
        min(1.0, max(0.0, 0.30 * approval_ratio + 0.30 * rel_component + 0.40 * depth_component)), 3
    )


def reconstruct_output(raw: dict[str, Any]) -> LiteratureChunkOutput:
    """Rebuild the output `_score_review_confidence` saw at extract time.

    `summary_json` persists the **post-filter** view: `candidates` holds only
    what survived, and the dropped ones are summarised (thesis + reason, no
    full fields) under `rejected_candidates`. Scoring the persisted view
    directly would read `integrity` as 1.0 for every chunk in history and
    silently hide a 30% term.

    Each dropped entry is therefore restored as a stand-in candidate scored
    below any valid floor, so `_filter_candidates` drops it again and the
    ratio matches what the pipeline computed. Only the *ratio* is
    reconstructed — the original field values are gone (see issue #153, which
    adds `anchor_quote` to the persisted record).
    """
    from zettel.schemas import PermanentNoteCandidate

    data = dict(raw)
    dropped = data.pop("rejected_candidates", None) or []
    output = LiteratureChunkOutput(**data)
    for entry in dropped:
        output.candidates.append(
            PermanentNoteCandidate(
                thesis=str(entry.get("thesis") or "candidato descartado no extract"),
                definition="",
                relevance_score=1,
            )
        )
    return output


def load_scores(state_db: Path, cfg: AppConfig) -> list[ChunkScore]:
    """Rescore every chunk that has a persisted `summary_json`."""
    from zettel.extractor import _candidate_completeness, _filter_candidates

    con = sqlite3.connect(state_db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT chunk_id, chunk_index, source_id, section_path, text, summary_json "
        "FROM chunks WHERE summary_json IS NOT NULL ORDER BY source_id, chunk_index"
    ).fetchall()
    con.close()

    scores: list[ChunkScore] = []
    for row in rows:
        output = reconstruct_output(json.loads(row["summary_json"]))
        text = row["text"] or ""
        approved, rejected = _filter_candidates(output.candidates, cfg, text)
        n = len(approved)
        scores.append(
            ChunkScore(
                chunk_id=row["chunk_id"],
                chunk_index=row["chunk_index"],
                source_id=row["source_id"],
                section_path=row["section_path"] or "",
                legacy=legacy_confidence(output, cfg, text),
                current=_score_review_confidence(output, cfg, text),
                n_approved=n,
                n_rejected=len(rejected),
                avg_relevance=(sum(c.relevance_score for c in approved) / n) if n else 0.0,
                avg_definition_words=(
                    sum(len(c.definition.split()) for c in approved) / n if n else 0.0
                ),
                completeness=(sum(_candidate_completeness(c) for c in approved) / n) if n else 0.0,
            )
        )
    return scores


def distribution(values: list[float]) -> dict[str, float]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min": round(min(values), 3),
        "median": round(stats.median(values), 3),
        "max": round(max(values), 3),
        "stdev": round(stats.pstdev(values), 3),
    }


def pass_rate(values: list[float], threshold: float) -> float:
    if not values:
        return 0.0
    return round(sum(1 for v in values if v >= threshold) / len(values), 3)


def reachability(cfg: AppConfig, threshold: float) -> list[dict[str, Any]]:
    """Ceiling of a flawless chunk at each valid `relevance_score`.

    The pre-#152 failure was structural, not a matter of tuning: at
    `relevance_score == min_relevance_score` the best attainable score was below
    the threshold, so an entire class of valid candidates could never
    auto-approve. Any future weight change must keep every row here reachable.
    """
    from zettel.extractor import (
        _RELEVANCE_FLOOR_CREDIT,
        _W_COMPLETENESS,
        _W_INTEGRITY,
        _W_RELEVANCE,
    )

    floor = cfg.extraction.min_relevance_score
    span = 5 - floor
    rows = []
    for rel in range(floor, 6):
        above = ((rel - floor) / span) if span > 0 else 1.0
        rel_component = _RELEVANCE_FLOOR_CREDIT + (1.0 - _RELEVANCE_FLOOR_CREDIT) * above
        ceiling = _W_RELEVANCE * rel_component + _W_INTEGRITY * 1.0 + _W_COMPLETENESS * 1.0
        rows.append(
            {
                "relevance_score": rel,
                "ceiling": round(ceiling, 3),
                "reachable": bool(round(ceiling, 3) >= threshold),
            }
        )
    return rows


def build_report(scores: list[ChunkScore], cfg: AppConfig, threshold: float) -> dict[str, Any]:
    scored = [s for s in scores if s.scored]
    legacy = [s.legacy for s in scored]
    current = [s.current for s in scored]
    return {
        "corpus": {
            "sources": len(({s.source_id for s in scores})),
            "chunks_with_output": len(scores),
            "chunks_scored": len(scored),
            "chunks_short_circuited": len(scores) - len(scored),
            "candidates_approved": sum(s.n_approved for s in scored),
            "candidates_dropped_by_filter": sum(s.n_rejected for s in scored),
        },
        "threshold": threshold,
        "legacy": {**distribution(legacy), "pass_rate": pass_rate(legacy, threshold)},
        "current": {**distribution(current), "pass_rate": pass_rate(current, threshold)},
        "inputs": {
            "relevance": distribution([s.avg_relevance for s in scored]),
            "definition_words": distribution([s.avg_definition_words for s in scored]),
            "completeness": distribution([s.completeness for s in scored]),
            "integrity": distribution(
                [s.n_approved / (s.n_approved + s.n_rejected) for s in scored]
            ),
        },
        "reachability": reachability(cfg, threshold),
        "sweep": {
            f"{t:.2f}": pass_rate(current, t) for t in (0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90)
        },
    }


# -- Gold set (issue #176) -----------------------------------------------
#
# `_score_review_confidence` was designed "for separation, not a calibrated
# probability (there is no ground truth to calibrate against)". Issue #175 built
# that ground truth. This section asks the only question the auto-approval gate
# depends on: among the chunks `extract` accepts, does a signal rank what a human
# would keep above what a human would discard?
#
# Only the accepted stratum is measured, because it is the only one the gate ever
# sees -- a rejected chunk never reaches review. Inside one stratum every sampled
# item has the same inclusion weight, so the AUC below is exact for that stratum
# rather than an approximation of the stratified estimate.

Z_95 = 1.96
# Power for the sample-size questions. Requiring only `a - 1.96*SE > 0.5` would be
# 50% power: half the samples drawn from a signal at exactly that AUC would still
# land below the bar. 80% is the conventional standard, so detection needs the
# true AUC to sit (1.96 + 0.84) standard errors above a coin.
Z_POWER_80 = 0.84


def auc(positive: list[float], negative: list[float]) -> float:
    """P(score of a random keep > score of a random discard), ties counting half."""
    if not positive or not negative:
        return float("nan")
    wins = 0.0
    for p in positive:
        for n in negative:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(positive) * len(negative))


def hanley_mcneil_se(a: float, n_positive: int, n_negative: int) -> float:
    """Standard error of an AUC (Hanley & McNeil, 1982). Closed form, no resampling."""
    if n_positive == 0 or n_negative == 0:
        return float("nan")
    q1 = a / (2 - a)
    q2 = 2 * a * a / (1 + a)
    var = (a * (1 - a) + (n_positive - 1) * (q1 - a * a) + (n_negative - 1) * (q2 - a * a)) / (
        n_positive * n_negative
    )
    return max(var, 0.0) ** 0.5


def auc_ci(positive: list[float], negative: list[float]) -> dict[str, Any]:
    a = auc(positive, negative)
    se = hanley_mcneil_se(a, len(positive), len(negative))
    low, high = max(0.0, a - Z_95 * se), min(1.0, a + Z_95 * se)
    if low > 0.5:
        verdict = "separa"
    elif high < 0.5:
        verdict = "invertido"
    else:
        verdict = "indistinguivel de moeda"
    return {
        "auc": round(a, 3),
        "ci95": [round(low, 3), round(high, 3)],
        "verdict": verdict,
    }


def _detectable(a: float, n_positive: int, n_negative: int) -> bool:
    """True if a signal whose true AUC is `a` clears 0.5 with 95% confidence and 80% power."""
    return a - (Z_95 + Z_POWER_80) * hanley_mcneil_se(a, n_positive, n_negative) > 0.5


def min_detectable_auc(n_positive: int, n_negative: int) -> float:
    """Smallest true AUC this sample would tell apart from a coin (95% conf., 80% power)."""
    for step in range(101):
        a = 0.5 + step * 0.005
        if _detectable(a, n_positive, n_negative):
            return round(a, 3)
    return 1.0


def items_needed(target_auc: float, keep_share: float, cap: int = 5000) -> int | None:
    """Accepted items to label before a signal at `target_auc` is detectable (80% power)."""
    for total in range(4, cap + 1):
        n_pos = round(total * keep_share)
        n_neg = total - n_pos
        if n_pos < 1 or n_neg < 1:
            continue
        if _detectable(target_auc, n_pos, n_neg):
            return total
    return None


def load_gold(labels_path: Path, key_path: Path) -> dict[str, str]:
    """chunk_id -> 'keep' | 'discard', accepted stratum only, `?` excluded."""
    from zettel.evals.extraction import STRATUM_ACCEPTED

    # By the stratum each item was DRAWN from, not by its current verdict: that is what
    # keeps every selected item at the same inclusion weight, and the AUC exact.
    key = json.loads(key_path.read_text(encoding="utf-8"))
    accepted = {e["item_id"] for e in key["items"] if e["sampling_stratum"] == STRATUM_ACCEPTED}
    labels = json.loads(labels_path.read_text(encoding="utf-8"))["labels"]
    return {
        lab["chunk_id"]: lab["human_verdict"]
        for lab in labels
        if lab["item_id"] in accepted and lab["human_verdict"] in ("keep", "discard")
    }


def gold_signals(score: ChunkScore) -> dict[str, float]:
    """Every persisted signal worth testing. Adding one is adding a line."""
    total = score.n_approved + score.n_rejected
    return {
        "review_confidence": score.current,
        "relevance": score.avg_relevance,
        "integrity": (score.n_approved / total) if total else 1.0,
        "completeness": score.completeness,
        "legacy_confidence": score.legacy,
        "definition_words": score.avg_definition_words,
    }


def build_gold_report(scores: list[ChunkScore], gold: dict[str, str]) -> dict[str, Any]:
    matched = [(s, gold[s.chunk_id]) for s in scores if s.chunk_id in gold]
    keep = [s for s, v in matched if v == "keep"]
    discard = [s for s, v in matched if v == "discard"]
    n_keep, n_discard = len(keep), len(discard)

    signals: dict[str, Any] = {}
    for name in gold_signals(matched[0][0]) if matched else {}:
        pos = [gold_signals(s)[name] for s in keep]
        neg = [gold_signals(s)[name] for s in discard]
        signals[name] = {
            **auc_ci(pos, neg),
            "mean_keep": round(stats.mean(pos), 3) if pos else None,
            "mean_discard": round(stats.mean(neg), 3) if neg else None,
        }

    def saturated(term: str) -> float:
        values = [gold_signals(s)[term] for s, _ in matched]
        return round(sum(1 for v in values if v >= 1.0) / len(values), 3) if values else 0.0

    keep_share = n_keep / (n_keep + n_discard) if matched else 0.0
    return {
        "labels_matched": len(matched),
        "labels_unmatched": len(gold) - len(matched),
        "n_keep": n_keep,
        "n_discard": n_discard,
        "signals": signals,
        # Share of judged accepted chunks where a term is already at its maximum.
        # A saturated term cannot separate anything: it is a constant.
        "saturation": {
            "integrity": saturated("integrity"),
            "completeness": saturated("completeness"),
        },
        "power": {
            "min_detectable_auc": min_detectable_auc(n_keep, n_discard) if matched else None,
            "items_needed_for_auc_0.70": items_needed(0.70, keep_share) if matched else None,
            "items_needed_for_auc_0.80": items_needed(0.80, keep_share) if matched else None,
        },
    }


def render_gold(gold: dict[str, Any]) -> str:
    out = [
        "",
        "gold set humano (#176) -- estrato de ACEITOS, o unico que o gate ve:",
        f"  rotulos casados: {gold['labels_matched']} "
        f"(guardaria {gold['n_keep']}, descartaria {gold['n_discard']})"
        + (
            f", SEM CHUNK no state.db: {gold['labels_unmatched']}"
            if gold["labels_unmatched"]
            else ""
        ),
        "",
        "  AUC para 'um humano guardaria' (0.5 = moeda; IC95 Hanley-McNeil):",
    ]
    for name, sig in sorted(gold["signals"].items(), key=lambda kv: -abs(kv[1]["auc"] - 0.5)):
        low, high = sig["ci95"]
        out.append(
            f"    {name:<18} AUC={sig['auc']:.3f}  IC95 [{low:.3f}, {high:.3f}]  "
            f"media guardar={sig['mean_keep']} descartar={sig['mean_discard']}  -> {sig['verdict']}"
        )
    sat = gold["saturation"]
    out += [
        "",
        (
            f"  saturacao (termo ja no maximo): integrity {sat['integrity']:.0%}, "
            f"completeness {sat['completeness']:.0%}"
        ),
    ]
    pw = gold["power"]
    need_70 = pw["items_needed_for_auc_0.70"]
    need_80 = pw["items_needed_for_auc_0.80"]
    out += [
        "",
        "  poder estatistico com esta amostra (95% de confianca, 80% de poder):",
        f"    menor AUC verdadeiro distinguivel de moeda: {pw['min_detectable_auc']}",
        f"    aceitos rotulados necessarios p/ detectar AUC 0.70: {need_70}",
        f"    aceitos rotulados necessarios p/ detectar AUC 0.80: {need_80}",
    ]
    return "\n".join(out)


def render(report: dict[str, Any], scores: list[ChunkScore]) -> str:
    out: list[str] = []
    c = report["corpus"]
    out.append(
        f"corpus: {c['sources']} fonte(s), {c['chunks_scored']} chunks pontuados "
        f"({c['chunks_short_circuited']} short-circuit), "
        f"{c['candidates_approved']} candidatos aprovados, "
        f"{c['candidates_dropped_by_filter']} descartados pelo filtro"
    )
    if c["sources"] <= 1:
        out.append(
            "  AVISO: uma unica fonte. O limiar derivado daqui e um default "
            "provisorio, nao uma calibracao."
        )
    out.append("")
    out.append(f"limiar avaliado: {report['threshold']}")
    for key in ("legacy", "current"):
        d = report[key]
        out.append(
            f"  {key:8s} min={d['min']:.3f} mediana={d['median']:.3f} max={d['max']:.3f} "
            f"desvio={d['stdev']:.3f} aprovacao={d['pass_rate']:.0%}"
        )
    out.append("")
    out.append("alcancabilidade (chunk impecavel, por relevance_score):")
    for row in report["reachability"]:
        mark = "ok" if row["reachable"] else "INALCANCAVEL"
        out.append(f"  rel={row['relevance_score']} teto={row['ceiling']:.3f}  {mark}")
    out.append("")
    out.append("varredura de limiar (formula nova):")
    for t, rate in report["sweep"].items():
        out.append(f"  {t} -> {rate:.0%} aprovados")
    out.append("")
    out.append("por chunk:")
    out.append(
        f"  {'idx':>4s} {'legacy':>7s} {'novo':>7s} {'rel':>4s} {'defw':>5s} {'compl':>6s}  secao"
    )
    for s in scores:
        if not s.scored:
            continue
        out.append(
            f"  {s.chunk_index:>4d} {s.legacy:>7.3f} {s.current:>7.3f} "
            f"{s.avg_relevance:>4.1f} {s.avg_definition_words:>5.1f} {s.completeness:>6.2f}  "
            f"{s.section_path[-52:]}"
        )
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", type=Path, default=Path("data/state.db"))
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Limiar a avaliar (default: literature_review.auto_approve_min_confidence)",
    )
    parser.add_argument("--json", type=Path, default=None, help="Grava o relatorio como JSON")
    parser.add_argument(
        "--gold-labels",
        type=Path,
        default=None,
        help="Rotulos humanos (#175), ex.: evals/gold/extracao-rotulos.json",
    )
    parser.add_argument(
        "--gold-key",
        type=Path,
        default=None,
        help="Gabarito da exportacao (#175), ex.: evals/gold/extracao-GABARITO-NAO-ABRIR.json",
    )
    args = parser.parse_args()
    if (args.gold_labels is None) != (args.gold_key is None):
        parser.error("--gold-labels e --gold-key vao juntos")

    # Section paths carry accents; the Windows console defaults to cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not args.state_db.exists():
        parser.error(f"state.db nao encontrado: {args.state_db}")

    cfg = load_config()
    threshold = (
        args.threshold
        if args.threshold is not None
        else cfg.literature_review.auto_approve_min_confidence
    )
    scores = load_scores(args.state_db, cfg)
    if not scores:
        print("Nenhum chunk com summary_json. Rode `zettel extract` antes.")
        return 1

    report = build_report(scores, cfg, threshold)
    print(render(report, scores))
    if args.gold_labels:
        report["gold"] = build_gold_report(scores, load_gold(args.gold_labels, args.gold_key))
        print(render_gold(report["gold"]))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nrelatorio JSON: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
