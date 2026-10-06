"""Export a blind labelling sheet for the shadow decisions of the decision layer (#206).

The pre-registration of #206 cannot open a gate on a site without human labels,
and two sites have none: nobody ever judged a garden cluster's category, and the
only dedupe label -- the reviewer's m/d -- exists only for what the LLM already
flagged as a repetition. Concepts the LLM let through as "new" are exactly where a
missed duplicate would hide, and nothing looks at them. This script produces the
sheet a human fills to close both gaps.

**Same input as the models.** Each item is rendered from the row's `state_json`:
the candidate and the same-source notes the dedupe LLM and the decision model
saw, or the cluster's notes and terms. The human judges the same evidence, so a
disagreement is about judgement, not about who saw more.

**Blind by construction.** The sheet and the reading file carry no baseline, no
model answer, no confidence and no reviewer label, and items are shuffled. The
answer key (`*-GABARITO-NAO-ABRIR.json`) holds all of that, frozen at export time.

**Strata.** Sampling spends the labelling budget where errors would hide:

* `dedupe` -- by the LLM's decision (`create_new` / `ignore` / `link`). The rare
  `ignore`/`link` are taken whole up to the cap; `create_new` is sampled, which
  is where the missed duplicates live.
* `moc_category` -- by whether the decision model agreed with the embedding
  argmax (`agree` / `disagree`), plus clusters the argmax left `unassigned`.
  Disagreements are where one of the two is wrong.

Each stratum is a census up to `--per-stratum`, a seeded random draw above it; the
key records each stratum's population so a scorer can weight the sample back.

Outputs, per site (`dedupe` / `categoria` prefix):

* ``*-planilha.csv`` -- what the human fills. `;`-separated, UTF-8 with BOM so a
  PT-BR spreadsheet opens it with accents and columns intact.
* ``*-leitura.md``   -- the same items for comfortable reading.
* ``*-GABARITO-NAO-ABRIR.json`` -- the frozen key. Opening it defeats the exercise.

The CSV and reading file carry source text and are gitignored; the key carries ids
only. An existing sheet is never overwritten without `--force`: it may already hold
an afternoon of labels.

Reads SQLite read-only. Writes nothing to the database and calls no model.

Usage:
    .venv/Scripts/python.exe scripts/export_decision_gold.py --site dedupe
    .venv/Scripts/python.exe scripts/export_decision_gold.py --site moc_category --per-stratum 25
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import string
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Run from a clone without installing the package (mirrors scripts/ precedent).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from report_decision_shadow import load_rows

SITES = ("dedupe", "moc_category")
PREFIX = {"dedupe": "dedupe", "moc_category": "categoria"}

# The labeller's answers. `?` exists for the same reason as in #175: forcing a
# verdict on an item that cannot honestly be judged puts noise into the number.
UNJUDGEABLE = "?"
DEDUPE_ANSWERS = {"nova": "create_new", "repete": "ignore", "desenvolve": "link"}
NONE_CATEGORY = "nenhuma"

# Words that would tell the labeller what a model decided. The leakage test
# asserts none of them appears in the sheet or the reading file.
FORBIDDEN_IN_SHEET = ("baseline", "jev", "llm_decision", "confidence", "probabilit", "_unassigned")

SHEET_COLUMNS = {
    "dedupe": [
        "item_id",
        "decisao",
        "alvo",
        "nota",
        "fonte",
        "tese",
        "definicao",
        "notas_existentes",
    ],
    "moc_category": ["item_id", "categoria", "nota", "termos", "notas"],
}


@dataclass(frozen=True)
class Item:
    item_id: str
    row: dict[str, Any]
    stratum: str
    # dedupe: letter shown to the labeller -> note id (a ULID is miserable to type)
    letters: dict[str, str]


# -- Strata and sampling -------------------------------------------------


def eligible(rows: list[dict[str, Any]], site: str) -> list[dict[str, Any]]:
    """Rows of ``site`` the decision model answered, with the input it saw."""
    return [
        r
        for r in rows
        if r["site"] == site and r["jev"] and not r["error"] and r.get("state") is not None
    ]


def stratum_of(row: dict[str, Any]) -> str:
    if row["site"] == "dedupe":
        return row["baseline"]["decision"]
    baseline = row["baseline"]["category"]
    if baseline == "_unassigned":
        return "unassigned"
    return "agree" if row["jev"]["category"]["choice"] == baseline else "disagree"


def source_of(row: dict[str, Any]) -> str:
    """Dedupe subjects are concept ids, which start with the source id."""
    return row["subject_id"].split("::", 1)[0] if row["site"] == "dedupe" else ""


def population(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[stratum_of(r)] += 1
    return dict(sorted(counts.items()))


def sample(rows: list[dict[str, Any]], *, per_stratum: int, seed: int) -> list[Item]:
    """Census up to ``per_stratum`` per stratum, seeded draw above it; shuffled ids."""
    rng = random.Random(seed)  # noqa: S311 -- sampling a sheet, not security
    by_stratum: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_stratum[stratum_of(r)].append(r)

    chosen: list[tuple[str, dict[str, Any]]] = []
    for stratum in sorted(by_stratum):
        group = sorted(by_stratum[stratum], key=lambda r: (r["subject_id"], r["state_checksum"]))
        if len(group) > per_stratum:
            group = rng.sample(group, per_stratum)
        chosen.extend((stratum, r) for r in group)

    # Position must not leak the stratum.
    rng.shuffle(chosen)
    letter = "D" if rows and rows[0]["site"] == "dedupe" else "C"
    items = []
    for i, (stratum, row) in enumerate(chosen, 1):
        letters = {}
        if row["site"] == "dedupe":
            notes = row["state"]["existing_notes"]
            letters = {string.ascii_uppercase[k]: n["id"] for k, n in enumerate(notes)}
        items.append(Item(f"{letter}{i:03d}", row, stratum, letters))
    return items


# -- Rendering -----------------------------------------------------------


def _note_block(letter: str, note: dict[str, Any]) -> str:
    return f"{letter}) {note.get('title') or '(sem titulo)'}: {note.get('text') or ''}".strip()


def dedupe_existing_text(item: Item) -> str:
    notes = item.row["state"]["existing_notes"]
    return "\n\n".join(
        _note_block(letter, n) for letter, n in zip(item.letters, notes, strict=True)
    )


def cluster_notes_text(item: Item) -> str:
    return "\n\n".join(
        f"- {n.get('title') or '(sem titulo)'}: {n.get('text') or ''}".strip()
        for n in item.row["state"]["notes"]
    )


def sheet_row(item: Item) -> list[str]:
    state = item.row["state"]
    if item.row["site"] == "dedupe":
        cand = state["candidate"]
        return [
            item.item_id,
            "",  # decisao: nova | repete | desenvolve | ?
            "",  # alvo: letra da nota (obrigatoria em repete/desenvolve)
            "",  # nota livre
            source_of(item.row),
            cand.get("thesis") or "",
            cand.get("definition") or "",
            dedupe_existing_text(item),
        ]
    return [
        item.item_id,
        "",  # categoria: numero ou nome da lista, `nenhuma` ou ?
        "",  # nota livre
        ", ".join(state.get("frequent_terms") or []),
        cluster_notes_text(item),
    ]


def write_sheet(items: list[Item], site: str, path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(SHEET_COLUMNS[site])
        for item in items:
            writer.writerow(sheet_row(item))


def _dedupe_reading(items: list[Item]) -> list[str]:
    lines = [
        "# Rotulagem cega — dedupe da mesma fonte",
        "",
        "Cada item traz um **candidato** a nota permanente e as notas que já existem",
        "**da mesma obra**. Dentro desta obra, o candidato é:",
        "",
        "- `nova` — uma ideia que nenhuma nota existente registra",
        "- `repete` — a mesma ideia de uma nota existente, sem nada acrescentado",
        "- `desenvolve` — uma ideia que uma nota existente registra, com nuance nova,",
        "  formulação mais completa, ou o autor aprofundando-a adiante na obra",
        f"- `{UNJUDGEABLE}` — não dá para julgar com o que está aqui",
        "",
        "Em `repete` e `desenvolve`, preencha `alvo` com a **letra** da nota existente.",
        "A coluna `nota` é livre — vale sobretudo quando você hesitou.",
        "",
        "Nada aqui diz o que qualquer modelo decidiu. É de propósito.",
        "",
        "---",
        "",
    ]
    for item in items:
        cand = item.row["state"]["candidate"]
        lines += [
            f"## {item.item_id}",
            "",
            f"**Fonte:** {source_of(item.row)}",
            "",
            f"**Candidato — tese:** {cand.get('thesis') or ''}",
            "",
            f"**Candidato — definição:** {cand.get('definition') or ''}",
            "",
            "**Notas existentes:**",
            "",
            dedupe_existing_text(item),
            "",
            "---",
            "",
        ]
    return lines


def _category_reading(items: list[Item], categories: list[tuple[str, str, list[str]]]) -> list[str]:
    lines = [
        "# Rotulagem cega — categoria do cluster",
        "",
        "Cada item é um grupo de notas que o `garden` juntou. Qual categoria da",
        "taxonomia melhor descreve **o que essas notas têm em comum**?",
        "",
        "Na coluna `categoria`, escreva o **número** (ou o nome) da lista abaixo,",
        f"`{NONE_CATEGORY}` se nenhuma cabe, ou `{UNJUDGEABLE}` se não dá para julgar.",
        "A coluna `nota` é livre.",
        "",
        "Nada aqui diz o que qualquer modelo decidiu. É de propósito.",
        "",
        "## Categorias",
        "",
    ]
    for number, (pillar, name, topics) in enumerate(categories, 1):
        extra = f" — {', '.join(topics)}" if topics else ""
        lines.append(
            f"{number}. **{name}** ({pillar}){extra}" if pillar else f"{number}. **{name}**{extra}"
        )
    lines += ["", "---", ""]
    for item in items:
        terms = ", ".join(item.row["state"].get("frequent_terms") or [])
        lines += [
            f"## {item.item_id}",
            "",
            f"**Termos frequentes:** {terms}" if terms else "",
            "",
            cluster_notes_text(item),
            "",
            "---",
            "",
        ]
    return lines


def write_reading(
    items: list[Item], site: str, path: Path, categories: list[tuple[str, str, list[str]]]
) -> None:
    lines = _dedupe_reading(items) if site == "dedupe" else _category_reading(items, categories)
    path.write_text("\n".join(lines), encoding="utf-8")


def build_key(
    items: list[Item],
    site: str,
    *,
    seed: int,
    population_counts: dict[str, int],
    categories: list[tuple[str, str, list[str]]],
) -> dict[str, Any]:
    """The answer key, frozen at export time. Ids only: no source text."""
    payload: dict[str, Any] = {
        "exported_at": datetime.now(UTC).isoformat(),
        "site": site,
        "seed": seed,
        "n_items": len(items),
        "population": population_counts,
        "answers": (
            {"decisao": DEDUPE_ANSWERS, "unjudgeable": UNJUDGEABLE}
            if site == "dedupe"
            else {"none": NONE_CATEGORY, "unjudgeable": UNJUDGEABLE}
        ),
        "items": [],
    }
    if site == "moc_category":
        payload["categories"] = {str(i): name for i, (_, name, _) in enumerate(categories, 1)}
    for item in items:
        row = item.row
        entry: dict[str, Any] = {
            "item_id": item.item_id,
            "subject_id": row["subject_id"],
            "state_checksum": row["state_checksum"],
            "sampling_stratum": item.stratum,
            "model": row["model"],
            "baseline": row["baseline"],
        }
        if site == "dedupe":
            entry["source_id"] = source_of(row)
            entry["letters"] = item.letters
            entry["jev"] = {
                "decision": row["jev"]["decision"]["choice"],
                "decision_confidence": row["jev"]["decision"]["confidence"],
                "target": row["jev"]["target"]["choice"],
            }
            entry["reviewer"] = (row.get("human") or {}).get("verdict")
        else:
            entry["jev"] = {
                "category": row["jev"]["category"]["choice"],
                "confidence": row["jev"]["category"]["confidence"],
            }
        payload["items"].append(entry)
    return payload


# -- CLI -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--site", choices=SITES, required=True)
    parser.add_argument("--state-db", type=Path, default=Path("data/state.db"))
    parser.add_argument("--out-dir", type=Path, default=Path("evals/gold"))
    parser.add_argument("--per-stratum", type=int, default=30)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--force", action="store_true", help="Sobrescreve uma planilha existente")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    prefix = PREFIX[args.site]
    sheet = args.out_dir / f"{prefix}-planilha.csv"
    reading = args.out_dir / f"{prefix}-leitura.md"
    key = args.out_dir / f"{prefix}-GABARITO-NAO-ABRIR.json"
    if sheet.exists() and not args.force:
        print(f"{sheet} ja existe e pode ter rotulos. Use --force para sobrescrever.")
        return 1

    rows = eligible(load_rows(args.state_db), args.site)
    if not rows:
        print(
            f"Nenhuma decisao shadow respondida para '{args.site}'. Rode `zettel review` "
            "(dedupe) ou `zettel garden` (categoria) com decision.sites em shadow e "
            "TYPESAFE_API_KEY no .env."
        )
        return 1

    categories: list[tuple[str, str, list[str]]] = []
    if args.site == "moc_category":
        from zettel.config import load_config
        from zettel.decision.shadow import categories_with_topics

        categories = categories_with_topics(load_config())

    items = sample(rows, per_stratum=args.per_stratum, seed=args.seed)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_sheet(items, args.site, sheet)
    write_reading(items, args.site, reading, categories)
    payload = build_key(
        items, args.site, seed=args.seed, population_counts=population(rows), categories=categories
    )
    key.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    by_stratum: dict[str, int] = defaultdict(int)
    for item in items:
        by_stratum[item.stratum] += 1
    print(f"{len(items)} itens exportados de {len(rows)} decisoes (seed={args.seed})")
    for stratum, total in population(rows).items():
        print(f"  {stratum}: {by_stratum[stratum]} de {total}")
    print(f"planilha: {sheet}\nleitura:  {reading}\ngabarito: {key} (nao abrir antes de rotular)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
