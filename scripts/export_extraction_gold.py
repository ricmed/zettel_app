"""Export a blind labeling sheet for the extraction gold set (issue #175).

Everything the project measures about extraction quality today is self-referential:
`calibrate_review_confidence.py` rescores the verdict `extract` itself wrote, and the
pre-LLM gate study (#173, ADR-049) trained on `summary_json.chunk_status` — the model's
own verdicts. Nothing measures whether the model's judgement matches a human's. This
script produces the one artifact that closes that gap and which only a human can fill.

**Blind by construction.** The sheet carries no `chunk_status`, no `rejection_category`,
no `rejection_reason`, no `review_confidence`, and items are shuffled, so the labeler
cannot tell which chunks the model kept from the ones it discarded. That matters more
than it sounds: a sheet made only of rejected chunks announces the model's verdict by
its own existence, and agreement measured under that framing is confirmation, not
measurement. Accepted chunks are mixed in for that reason first, and to make precision
computable second.

**Why the strata are what they are.** `rejection_category` is emitted by the LLM
(`schemas.py`, `LiteratureChunkOutput`), not derived by code — it is the model
explaining its own decision, with no independent check that the explanation matches
either the decision or reality. So the `structural` sample here is an **audit of that
label**, drawn at random, not a formality: if the model rejects a valuable passage and
rationalises it as "structural", under-sampling that stratum would hide precisely the
error being hunted. The sheet also carries deterministic text features, computed from
the chunk and independent of the model, as a stratification axis that the system under
test cannot bias.

Outputs three files:

  * ``*-planilha.csv``  — what the human fills in (a `veredito` column, plus optional
    `categoria` and `nota`). CSV because filling one column over 120 rows is a
    spreadsheet job, and Python's csv module round-trips embedded newlines safely.
  * ``*-leitura.md``    — the same items, formatted for comfortable reading. Read here,
    record there; a 1600-character cell is miserable to read in a spreadsheet.
  * ``*-GABARITO-NAO-ABRIR.json`` — the item -> verdict key, frozen at export time so a
    later scorer can join even if the corpus moves. Opening it defeats the exercise.

The labeler may answer `s`, `n` or `?`. The third exists so a passage that cannot
honestly be judged in isolation does not have to be forced into a binary; those rows
are reported and excluded from scoring rather than counted as agreement or as error.

Reads SQLite read-only. Writes nothing to the database and calls no LLM.

Usage:
    .venv/Scripts/python.exe scripts/export_extraction_gold.py
    .venv/Scripts/python.exe scripts/export_extraction_gold.py --structural 30 --accepted 40
    .venv/Scripts/python.exe scripts/export_extraction_gold.py --out-dir evals/gold --seed 7
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Run from a clone without installing the package (mirrors scripts/ precedent).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The fields that would tell the labeler what the model decided. Named here so the
# leakage test has one list to assert against instead of a scattering of literals.
FORBIDDEN_IN_SHEET = (
    "chunk_status",
    "rejection_category",
    "rejection_reason",
    "review_confidence",
    "summary_json",
)

# The three answers a labeler may give. `?` exists because forcing a binary on a
# passage that genuinely cannot be judged -- a fragment cut mid-sentence, a table
# whose meaning lives in a figure that is not there -- injects noise into the very
# number being measured. Those rows are counted and reported, never scored.
VERDICT_KEEP = "s"
VERDICT_DISCARD = "n"
VERDICT_UNJUDGEABLE = "?"
VERDICT_VALUES = (VERDICT_KEEP, VERDICT_DISCARD, VERDICT_UNJUDGEABLE)

# The prompt's own rejection vocabulary (`prompts/literature_note.md`), offered as a
# hint rather than a constraint: `categoria` accepts free text on purpose. Forcing the
# labeler into the model's vocabulary would bias category agreement the same way
# showing the verdict would bias verdict agreement -- and needing a word this list
# lacks is a finding about the list.
CATEGORY_HINTS = ("structural", "narrative", "promotional", "trivial", "fragmented")


@dataclass(frozen=True)
class Item:
    """One row of the sheet. ``hidden_*`` never reaches the human-facing files."""

    item_id: str
    chunk_id: str
    source_id: str
    locator: str
    text: str
    table_line_ratio: float
    alnum_ratio: float
    hidden_verdict: str
    hidden_category: str

    @property
    def stratum(self) -> str:
        if self.hidden_verdict == "accepted":
            return "accepted"
        return f"rejected:{self.hidden_category or '(sem categoria)'}"


# -- Loading -------------------------------------------------------------


def load_labeled(state_db_path: Path) -> list[dict]:
    """Chunks carrying an LLM verdict, with what a human needs to judge them."""
    conn = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT chunk_id, source_id, text, section_path, page_in_book, page_in_file, "
            "summary_json FROM chunks"
        ).fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for r in rows:
        if not r["summary_json"] or not (r["text"] or "").strip():
            continue
        try:
            data = json.loads(r["summary_json"])
        except json.JSONDecodeError:
            continue
        verdict = data.get("chunk_status")
        if verdict not in ("accepted", "rejected"):
            continue
        out.append(
            {
                "chunk_id": r["chunk_id"],
                "source_id": r["source_id"] or "",
                "text": r["text"],
                "section_path": r["section_path"] or "",
                "page_in_book": r["page_in_book"],
                "page_in_file": r["page_in_file"],
                "verdict": verdict,
                "category": data.get("rejection_category") or "",
            }
        )
    return out


def text_features(text: str) -> tuple[float, float]:
    """Table-line ratio and alphanumeric ratio — computed from the text, not the model."""
    stripped = text.strip()
    if not stripped:
        return 0.0, 0.0
    alnum = sum(ch.isalnum() for ch in stripped) / len(stripped)
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    table = sum(1 for ln in lines if ln.count("|") >= 2) / len(lines) if lines else 0.0
    return round(table, 3), round(alnum, 3)


# -- Sampling ------------------------------------------------------------


def sample_items(
    records: list[dict],
    *,
    seed: int = 0,
    n_structural: int = 30,
    n_accepted: int = 40,
) -> list[Item]:
    """Census of the contested rejections, an audit slice of `structural`, and accepted.

    Within each sampled stratum the draw is spread across sources proportionally, so one
    dominant document cannot supply the whole sample. The final order is shuffled: an
    export that listed rejections first would leak the verdict through position alone.
    """
    # Sampling for a labeling sheet, not for anything security-sensitive.
    rng = random.Random(seed)  # noqa: S311

    contested = [r for r in records if r["verdict"] == "rejected" and r["category"] != "structural"]
    structural = [
        r for r in records if r["verdict"] == "rejected" and r["category"] == "structural"
    ]
    accepted = [r for r in records if r["verdict"] == "accepted"]

    chosen = contested + _stratified_draw(structural, n_structural, rng)
    chosen += _stratified_draw(accepted, n_accepted, rng)

    rng.shuffle(chosen)
    items: list[Item] = []
    for i, rec in enumerate(chosen, 1):
        table_ratio, alnum = text_features(rec["text"])
        items.append(
            Item(
                item_id=f"G{i:03d}",
                chunk_id=rec["chunk_id"],
                source_id=rec["source_id"],
                locator=_locator(rec),
                text=rec["text"],
                table_line_ratio=table_ratio,
                alnum_ratio=alnum,
                hidden_verdict=rec["verdict"],
                hidden_category=rec["category"],
            )
        )
    return items


def _stratified_draw(records: list[dict], n: int, rng: random.Random) -> list[dict]:
    """Draw ``n`` spread across sources, largest remainder, deterministic under ``rng``."""
    if n <= 0 or not records:
        return []
    if n >= len(records):
        return list(records)

    by_source: dict[str, list[dict]] = defaultdict(list)
    for rec in records:
        by_source[rec["source_id"]].append(rec)

    total = len(records)
    quotas: dict[str, int] = {}
    for src, group in by_source.items():
        quotas[src] = int(n * len(group) / total)
    # Largest remainder, ties broken by source id so the result is reproducible.
    remainders = sorted(by_source, key=lambda s: (-(n * len(by_source[s]) / total - quotas[s]), s))
    i = 0
    while sum(quotas.values()) < n:
        quotas[remainders[i % len(remainders)]] += 1
        i += 1

    drawn: list[dict] = []
    for src in sorted(by_source):
        group = sorted(by_source[src], key=lambda r: r["chunk_id"])
        drawn.extend(rng.sample(group, min(quotas[src], len(group))))
    return drawn


def _locator(rec: dict) -> str:
    from zettel.paging import format_source_locator

    return (
        format_source_locator(rec["page_in_book"], rec["section_path"], rec["page_in_file"])
        or rec["section_path"]
        or ""
    )


# -- Writing -------------------------------------------------------------

SHEET_COLUMNS = [
    "item_id",
    "veredito",
    "categoria",
    "nota",
    "fonte",
    "locator",
    "densidade_tabela",
    "razao_alfanumerica",
    "texto",
]


def write_sheet(items: list[Item], path: Path) -> None:
    """The CSV the human fills. Carries no trace of the model's decision."""
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(SHEET_COLUMNS)
        for it in items:
            writer.writerow(
                [
                    it.item_id,
                    "",  # veredito: s | n | ? (ver o arquivo de leitura)
                    "",  # categoria (opcional, se descartaria)
                    "",  # nota livre
                    it.source_id,
                    it.locator,
                    it.table_line_ratio,
                    it.alnum_ratio,
                    it.text,
                ]
            )


def write_reading(items: list[Item], path: Path) -> None:
    """The companion to read from. Same items, same ids, no verdict."""
    lines = [
        "# Planilha de rotulagem — leitura",
        "",
        "Para cada item: **você guardaria esta passagem como nota permanente?**",
        "Registre a resposta na coluna `veredito` da planilha CSV, pelo `item_id`:",
        "",
        f"- `{VERDICT_KEEP}` — eu guardaria",
        f"- `{VERDICT_DISCARD}` — eu descartaria",
        f"- `{VERDICT_UNJUDGEABLE}` — não dá para julgar esta passagem isolada",
        "",
        f"Use `{VERDICT_UNJUDGEABLE}` sem culpa: um fragmento cortado no meio da frase, uma",
        "passagem que só faz sentido com o parágrafo anterior, uma tabela cujo sentido está",
        "numa figura que não veio junto. Esses itens são contados e relatados à parte, e",
        "ficam fora do cálculo — forçar um `s`/`n` neles poria ruído justamente no número",
        "que esta planilha existe para medir.",
        "",
        f"Se `{VERDICT_DISCARD}`, a coluna `categoria` aceita: "
        + ", ".join(f"`{c}`" for c in CATEGORY_HINTS),
        "— ou o termo que você achar melhor; texto livre é bem-vindo.",
        "",
        "A coluna `nota` é livre e opcional. Vale sobretudo nos casos limítrofes: quando",
        "você hesitou, ou quando o motivo não cabe numa categoria.",
        "",
        "Nada aqui diz o que o modelo decidiu. É de propósito.",
        "",
        "---",
        "",
    ]
    for it in items:
        lines += [
            f"## {it.item_id}",
            "",
            f"**Fonte:** {it.source_id} — {it.locator}"
            if it.locator
            else f"**Fonte:** {it.source_id}",
            "",
            it.text.strip(),
            "",
            "---",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_key(items: list[Item], path: Path, *, seed: int) -> None:
    """The answer key, frozen at export time. Not for the labeler."""
    payload = {
        "exported_at": datetime.now(UTC).isoformat(),
        "seed": seed,
        "n_items": len(items),
        "items": [
            {
                "item_id": it.item_id,
                "chunk_id": it.chunk_id,
                "source_id": it.source_id,
                "llm_verdict": it.hidden_verdict,
                "llm_category": it.hidden_category,
            }
            for it in items
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), "utf-8")


# -- CLI -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", type=Path, default=Path("data/state.db"))
    parser.add_argument("--out-dir", type=Path, default=Path("evals/gold"))
    parser.add_argument("--prefix", default="extracao")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--structural",
        type=int,
        default=30,
        help="Amostra de auditoria do rotulo 'structural' (o LLM e quem o atribui)",
    )
    parser.add_argument(
        "--accepted",
        type=int,
        default=40,
        help="Aceitos misturados: e o que torna a rotulagem cega, alem de liberar precisao",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Sobrescrever uma planilha existente (por padrao recusa: pode haver rotulo dentro)",
    )
    args = parser.parse_args()

    records = load_labeled(args.state_db)
    if not records:
        print("Sem chunks rotulados pelo extract em", args.state_db)
        return 1

    items = sample_items(
        records,
        seed=args.seed,
        n_structural=args.structural,
        n_accepted=args.accepted,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    sheet = args.out_dir / f"{args.prefix}-planilha.csv"
    reading = args.out_dir / f"{args.prefix}-leitura.md"
    key = args.out_dir / f"{args.prefix}-GABARITO-NAO-ABRIR.json"

    if sheet.exists() and not args.overwrite:
        print(
            f"ABORTADO: {sheet} ja existe e pode conter rotulos.\n"
            "Rotulagem e a parte cara deste processo -- use --overwrite so se tiver certeza."
        )
        return 1

    write_sheet(items, sheet)
    write_reading(items, reading)
    write_key(items, key, seed=args.seed)

    counts: dict[str, int] = defaultdict(int)
    for it in items:
        counts[it.stratum] += 1
    print(f"{len(items)} itens exportados (seed={args.seed})")
    for stratum in sorted(counts):
        print(f"  {stratum:<28} {counts[stratum]:>4}")
    print(f"\n  planilha : {sheet}")
    print(f"  leitura  : {reading}")
    print(f"  gabarito : {key}  (nao abra antes de rotular)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
