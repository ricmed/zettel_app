"""Score the extraction gold set: the human's verdict against the LLM's (issue #175).

`python -m zettel.evals.extraction <planilha.csv> <gabarito.json>`

Every quality number the project had about extraction was self-referential — rescored
from, or trained on, the verdict `extract` itself wrote. This module is the first one
that compares that verdict with an independent judgement. It follows ADR-038: offline,
deterministic, stdlib only, never imported by the pipeline.

Three things here are easy to get wrong and are handled explicitly.

**The sample is stratified, so raw counts are not corpus estimates.** The export takes
a census of the contested rejections, a random slice of `structural` and a random slice
of accepted chunks — rejections are over-represented on purpose. Each item is weighted
by its stratum's inverse inclusion probability (`population / sampled`), read from the
key. Raw counts are reported next to the weighted estimates so the sample size behind
every number stays visible.

**The positive class is "a human would keep this".** Against it the LLM's `accepted` is
the prediction, so a false negative is a chunk the model rejected and the human would
have kept: a permanent note lost in silence. That rate — of what the model rejects, how
much a human would keep — is the headline, because it is the one error nothing else in
the pipeline can surface.

**The sheet came back through a spreadsheet.** It was exported as UTF-8 with commas; a
pt-BR spreadsheet writes semicolons and a legacy Windows code page. The reader accepts
both delimiters, and when UTF-8 fails it picks among legacy encodings by which one
yields Portuguese text rather than mojibake. Verdicts are normalised (`y` and `sim` mean
keep) and every normalisation is reported rather than applied silently. `?` — cannot be
judged in isolation — is counted and excluded from scoring, never charged as agreement
or as error.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

RESULT_SCHEMA_VERSION = 1

KEEP = "keep"
DISCARD = "discard"
UNJUDGEABLE = "unjudgeable"

STRATUM_ACCEPTED = "accepted"
STRATUM_STRUCTURAL = "structural"
STRATUM_CONTESTED = "contested"

_VERDICT_ALIASES = {
    "s": KEEP,
    "sim": KEEP,
    "y": KEEP,
    "yes": KEEP,
    "n": DISCARD,
    "nao": DISCARD,
    "não": DISCARD,
    "no": DISCARD,
    "?": UNJUDGEABLE,
}
_CANONICAL_RAW = {"s", "n", "?"}

# Tried in order when the sheet is not valid UTF-8. cp850 is the OEM code page a pt-BR
# Windows console and some spreadsheet exports write; cp1252 is the ANSI one. latin-1
# decodes anything and is the last resort.
_LEGACY_ENCODINGS = ("cp850", "cp1252", "latin-1")
_PT_LETTERS = frozenset("áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ")


def sampling_stratum(llm_verdict: str, llm_category: str) -> str:
    """The stratum an item was drawn from. Single definition, shared with the exporter."""
    if llm_verdict == "accepted":
        return STRATUM_ACCEPTED
    if llm_category == "structural":
        return STRATUM_STRUCTURAL
    return STRATUM_CONTESTED


# -- Reading the filled sheet --------------------------------------------


@dataclass
class HumanLabel:
    item_id: str
    verdict: str  # keep | discard | unjudgeable
    category: str = ""
    note: str = ""


@dataclass
class ReadReport:
    encoding: str
    delimiter: str
    rows: int
    normalized: dict[str, int] = field(default_factory=dict)  # raw value -> count
    missing: list[str] = field(default_factory=list)  # item_ids with no verdict
    invalid: dict[str, str] = field(default_factory=dict)  # item_id -> raw value


def decode_sheet(raw: bytes) -> tuple[str, str]:
    """Return ``(text, encoding)``. UTF-8 first; otherwise the legacy page that reads as PT."""
    for encoding in ("utf-8-sig",):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            pass

    best: tuple[int, str, str] | None = None
    for encoding in _LEGACY_ENCODINGS:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        score = sum(ch in _PT_LETTERS for ch in text)
        if best is None or score > best[0]:
            best = (score, encoding, text)
    if best is None:  # unreachable while latin-1 is a candidate: it maps every byte
        return raw.decode("latin-1"), "latin-1"
    return best[2], best[1]


def _delimiter(text: str) -> str:
    header = text.split("\n", 1)[0]
    return ";" if header.count(";") > header.count(",") else ","


def read_sheet(path: Path) -> tuple[list[HumanLabel], ReadReport]:
    text, encoding = decode_sheet(path.read_bytes())
    delimiter = _delimiter(text)
    rows = list(csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter))

    report = ReadReport(encoding=encoding, delimiter=delimiter, rows=len(rows))
    normalized: Counter[str] = Counter()
    labels: list[HumanLabel] = []
    for row in rows:
        item_id = (row.get("item_id") or "").strip()
        raw = (row.get("veredito") or "").strip()
        if not raw:
            report.missing.append(item_id)
            continue
        verdict = _VERDICT_ALIASES.get(raw.lower())
        if verdict is None:
            report.invalid[item_id] = raw
            continue
        if raw.lower() not in _CANONICAL_RAW:
            normalized[raw.lower()] += 1
        labels.append(
            HumanLabel(
                item_id=item_id,
                verdict=verdict,
                category=(row.get("categoria") or "").strip(),
                note=(row.get("nota") or "").strip(),
            )
        )
    report.normalized = dict(sorted(normalized.items()))
    return labels, report


# -- Scoring -------------------------------------------------------------


@dataclass
class KeyItem:
    item_id: str
    chunk_id: str
    source_id: str
    llm_verdict: str
    llm_category: str

    @property
    def stratum(self) -> str:
        return sampling_stratum(self.llm_verdict, self.llm_category)


def load_key(path: Path) -> tuple[list[KeyItem], dict[str, int]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    population = payload.get("population")
    if not population:
        raise ValueError(
            f"{path} nao registra 'population' por estrato. Sem ela nao ha como ponderar "
            "a amostra estratificada, e os numeros seriam da amostra, nao do corpus."
        )
    items = [
        KeyItem(
            item_id=e["item_id"],
            chunk_id=e["chunk_id"],
            source_id=e["source_id"],
            llm_verdict=e["llm_verdict"],
            llm_category=e.get("llm_category") or "",
        )
        for e in payload["items"]
    ]
    return items, {k: int(v) for k, v in population.items()}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for ``k`` successes in ``n``."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


@dataclass
class StratumScore:
    stratum: str
    population: int
    sampled: int
    judged: int  # sampled minus unjudgeable
    unjudgeable: int
    human_keep: int
    human_discard: int
    census: bool
    # For a rejected stratum: share the human would keep (silent loss).
    # For the accepted stratum: share the human would discard (junk let through).
    disagreement_rate: float
    disagreement_ci95: tuple[float, float]


@dataclass
class ExtractionScore:
    schema_version: int
    items_scored: int
    items_unjudgeable: int
    strata: list[StratumScore]
    # Corpus estimates, weighted by inverse inclusion probability.
    precision: float
    recall: float
    f1: float
    rejected_but_keep_rate: float
    estimated_lost_notes: float
    # Raw sample confusion matrix, positive class = keep.
    raw_confusion: dict[str, int]
    category_agreement: dict[str, object]
    by_source: dict[str, dict[str, int]]
    disagreements: list[dict[str, str]]


def score(
    labels: list[HumanLabel], key: list[KeyItem], population: dict[str, int]
) -> ExtractionScore:
    by_id = {k.item_id: k for k in key}
    sampled = Counter(k.stratum for k in key)
    weight = {s: population.get(s, 0) / sampled[s] for s in sampled if sampled[s]}

    judged = [(lab, by_id[lab.item_id]) for lab in labels if lab.item_id in by_id]
    unjudgeable = [p for p in judged if p[0].verdict == UNJUDGEABLE]
    scored = [p for p in judged if p[0].verdict != UNJUDGEABLE]

    raw = Counter()
    weighted: defaultdict[str, float] = defaultdict(float)
    for lab, item in scored:
        human_keep = lab.verdict == KEEP
        llm_keep = item.llm_verdict == "accepted"
        cell = {
            (True, True): "tp",
            (False, True): "fp",
            (True, False): "fn",
            (False, False): "tn",
        }[(human_keep, llm_keep)]
        raw[cell] += 1
        weighted[cell] += weight[item.stratum]

    tp, fp, fn, tn = (weighted[c] for c in ("tp", "fp", "fn", "tn"))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    rejected_but_keep = fn / (fn + tn) if fn + tn else 0.0

    rejected_population = sum(population.get(s, 0) for s in (STRATUM_STRUCTURAL, STRATUM_CONTESTED))

    strata: list[StratumScore] = []
    for stratum in (STRATUM_CONTESTED, STRATUM_STRUCTURAL, STRATUM_ACCEPTED):
        if not sampled[stratum]:
            continue
        members = [p for p in judged if p[1].stratum == stratum]
        judged_here = [p for p in members if p[0].verdict != UNJUDGEABLE]
        keep = sum(1 for lab, _ in judged_here if lab.verdict == KEEP)
        discard = len(judged_here) - keep
        disagree = discard if stratum == STRATUM_ACCEPTED else keep
        rate = disagree / len(judged_here) if judged_here else 0.0
        census = sampled[stratum] >= population.get(stratum, 0)
        strata.append(
            StratumScore(
                stratum=stratum,
                population=population.get(stratum, 0),
                sampled=sampled[stratum],
                judged=len(judged_here),
                unjudgeable=len(members) - len(judged_here),
                human_keep=keep,
                human_discard=discard,
                census=census,
                disagreement_rate=round(rate, 4),
                # A census has no sampling error: the interval collapses onto the value.
                disagreement_ci95=(round(rate, 4), round(rate, 4))
                if census
                else wilson(disagree, len(judged_here)),
            )
        )

    return ExtractionScore(
        schema_version=RESULT_SCHEMA_VERSION,
        items_scored=len(scored),
        items_unjudgeable=len(unjudgeable),
        strata=strata,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        rejected_but_keep_rate=round(rejected_but_keep, 4),
        estimated_lost_notes=round(rejected_but_keep * rejected_population, 1),
        raw_confusion={c: raw[c] for c in ("tp", "fp", "fn", "tn")},
        category_agreement=_category_agreement(scored),
        by_source=_by_source(scored),
        disagreements=_disagreements(judged),
    )


def _category_agreement(scored: list[tuple[HumanLabel, KeyItem]]) -> dict[str, object]:
    """Where both rejected and the human named a category: do they name the same one?"""
    pairs = [
        (item.llm_category, lab.category.lower())
        for lab, item in scored
        if lab.verdict == DISCARD and item.llm_verdict == "rejected" and lab.category
    ]
    table: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for llm_cat, human_cat in pairs:
        table[llm_cat or "(vazio)"][human_cat] += 1
    agree = sum(1 for a, b in pairs if a == b)
    return {
        "compared": len(pairs),
        "agree": agree,
        "rate": round(agree / len(pairs), 4) if pairs else 0.0,
        "llm_to_human": {k: dict(sorted(v.items())) for k, v in sorted(table.items())},
    }


def _by_source(scored: list[tuple[HumanLabel, KeyItem]]) -> dict[str, dict[str, int]]:
    out: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for lab, item in scored:
        human_keep = lab.verdict == KEEP
        llm_keep = item.llm_verdict == "accepted"
        if human_keep == llm_keep:
            out[item.source_id]["agree"] += 1
        elif human_keep:
            out[item.source_id]["llm_rejected_human_keep"] += 1
        else:
            out[item.source_id]["llm_accepted_human_discard"] += 1
    return {src: dict(sorted(c.items())) for src, c in sorted(out.items())}


def _disagreements(judged: list[tuple[HumanLabel, KeyItem]]) -> list[dict[str, str]]:
    """Every disagreement, plus every item the human annotated. No chunk text."""
    rows = []
    for lab, item in judged:
        llm_keep = item.llm_verdict == "accepted"
        disagrees = lab.verdict != UNJUDGEABLE and (lab.verdict == KEEP) != llm_keep
        if not (disagrees or lab.note or lab.verdict == UNJUDGEABLE):
            continue
        rows.append(
            {
                "item_id": item.item_id,
                "source_id": item.source_id,
                "llm_verdict": item.llm_verdict,
                "llm_category": item.llm_category,
                "human_verdict": lab.verdict,
                "human_category": lab.category,
                "human_note": lab.note,
                "kind": "unjudgeable"
                if lab.verdict == UNJUDGEABLE
                else ("disagreement" if disagrees else "annotated_agreement"),
            }
        )
    return sorted(rows, key=lambda r: r["item_id"])


# -- Artifacts -----------------------------------------------------------


def labels_artifact(labels: list[HumanLabel], key: list[KeyItem], report: ReadReport) -> dict:
    """The durable gold set: the human's judgement keyed by chunk, without any chunk text."""
    by_id = {k.item_id: k for k in key}
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "read": asdict(report),
        "labels": [
            {
                "item_id": lab.item_id,
                "chunk_id": by_id[lab.item_id].chunk_id if lab.item_id in by_id else "",
                "source_id": by_id[lab.item_id].source_id if lab.item_id in by_id else "",
                "human_verdict": lab.verdict,
                "human_category": lab.category,
                "human_note": lab.note,
            }
            for lab in sorted(labels, key=lambda x: x.item_id)
        ],
    }


def _dump(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render(result: ExtractionScore, report: ReadReport) -> str:
    def pct(x: float) -> str:
        return f"{100 * x:.1f}%"

    lines = [
        (
            f"Planilha: {report.rows} linhas | encoding={report.encoding} "
            f"| separador={report.delimiter!r}"
        ),
    ]
    if report.normalized:
        lines.append(f"  veredito normalizado: {report.normalized}")
    if report.missing:
        lines.append(f"  SEM veredito (fora da conta): {report.missing}")
    if report.invalid:
        lines.append(f"  veredito INVALIDO (fora da conta): {report.invalid}")
    lines += [
        f"Pontuados: {result.items_scored} | '?': {result.items_unjudgeable}",
        "",
        "== Por estrato (contagem real da amostra) ==",
    ]
    for s in result.strata:
        accepted = s.stratum == STRATUM_ACCEPTED
        what = "humano descartaria" if accepted else "humano guardaria"
        hits = s.human_discard if accepted else s.human_keep
        low, high = s.disagreement_ci95
        ci = "censo, sem erro amostral" if s.census else f"IC95 {pct(low)}-{pct(high)}"
        lines.append(
            f"  {s.stratum:<11} pop={s.population:>4} amostra={s.sampled:>3} "
            f"julgados={s.judged:>3} '?'={s.unjudgeable}  "
            f"{what}: {hits}/{s.judged} = {pct(s.disagreement_rate)}  ({ci})"
        )
    agreement = result.category_agreement
    lines += [
        "",
        "== Estimativa para o corpus (ponderada pelo tamanho do estrato) ==",
        (
            f"  das rejeicoes do LLM, humano guardaria : {pct(result.rejected_but_keep_rate)}"
            f"  (~{result.estimated_lost_notes:.0f} chunks no corpus)"
        ),
        f"  precisao do extract                    : {pct(result.precision)}",
        f"  recall do extract                      : {pct(result.recall)}",
        f"  F1                                     : {pct(result.f1)}",
        f"  matriz crua da amostra (positivo=guardar): {result.raw_confusion}",
        "",
        "== Categoria (onde ambos rejeitaram e voce nomeou uma) ==",
        (
            f"  concordancia: {agreement['agree']}/{agreement['compared']}"
            f" = {pct(agreement['rate'])}"
        ),
    ]
    for llm_cat, human in agreement["llm_to_human"].items():
        lines.append(f"    LLM {llm_cat:<12} -> voce {human}")
    lines += ["", "== Por fonte (amostra) =="]
    for src, c in result.by_source.items():
        lines.append(f"  {src:<32} {c}")
    lines += ["", "== Discordancias e itens anotados =="]
    tags = {"disagreement": "DISCORDA", "unjudgeable": "?", "annotated_agreement": "nota"}
    for d in result.disagreements:
        cat = f"/{d['llm_category']}" if d["llm_category"] else ""
        hcat = f"/{d['human_category']}" if d["human_category"] else ""
        note = f"  -- {d['human_note']}" if d["human_note"] else ""
        lines.append(
            f"  {d['item_id']} [{tags[d['kind']]:<8}] LLM={d['llm_verdict']}{cat} "
            f"voce={d['human_verdict']}{hcat}{note}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("sheet", type=Path, help="Planilha preenchida (CSV)")
    parser.add_argument("key", type=Path, help="Gabarito da exportacao (JSON)")
    parser.add_argument("--labels-out", type=Path, help="Grava os rotulos sem texto (commitavel)")
    parser.add_argument("--out", type=Path, help="Grava o resultado em JSON estavel")
    args = parser.parse_args(argv)

    key, population = load_key(args.key)
    labels, report = read_sheet(args.sheet)
    result = score(labels, key, population)

    if args.labels_out:
        args.labels_out.parent.mkdir(parents=True, exist_ok=True)
        args.labels_out.write_text(_dump(labels_artifact(labels, key, report)), encoding="utf-8")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(_dump(asdict(result)), encoding="utf-8")

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    print(render(result, report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
