"""Export a blind labelling sheet for the shadow decisions of the decision layer (#206).

The pre-registration of #206 cannot open a gate on a site without human labels,
and two sites have none: nobody ever judged a garden cluster's category, and the
only dedupe label -- the reviewer's m/d -- exists only for what the LLM already
flagged as a repetition. Concepts the LLM let through as "new" are exactly where a
missed duplicate would hide, and nothing looks at them. This script produces the
sheet a human fills to close both gaps.

**The whole note, not the excerpt.** Each item is identified by the row's
`state_json`, but notes are rendered **in full** from `state.db` at export time
(thesis, definition, intuition, example, limits). The models saw less -- dedupe
shows the LLM a 200-character excerpt of each existing note, corroborates only
thesis and definition -- and that is the point: the label must be the best
judgement available, and a model that errs because it saw too little is exactly
what the comparison should expose. Only content sections are shown; `## Conexoes`
is left out because it would reveal whether two notes are already linked.

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
* `corroborates` -- one item per pair of notes from different works, by cosine
  band: `above_threshold` (the pipeline linked them), `near_threshold` and
  `low_band` (it did not). The bands below the threshold are where the same idea
  in other words -- a missing link -- would hide. Neither the similarity nor the
  sources reach the sheet.

Each stratum is a census up to `--per-stratum`, a seeded random draw above it; the
key records each stratum's population so a scorer can weight the sample back.

Outputs, per site (`dedupe` / `categoria` / `corroboracao` prefix):

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
    .venv/Scripts/python.exe scripts/export_decision_gold.py --site corroborates
"""

from __future__ import annotations

import argparse
import copy
import csv
import dataclasses
import json
import random
import sqlite3
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

from report_decision_shadow import load_rows, similarity_band

SITES = ("dedupe", "moc_category", "corroborates")
PREFIX = {"dedupe": "dedupe", "moc_category": "categoria", "corroborates": "corroboracao"}
ITEM_LETTER = {"dedupe": "D", "moc_category": "C", "corroborates": "P"}

# The labeller's answers. `?` exists for the same reason as in #175: forcing a
# verdict on an item that cannot honestly be judged puts noise into the number.
UNJUDGEABLE = "?"
DEDUPE_ANSWERS = {"nova": "create_new", "repete": "ignore", "desenvolve": "link"}
NONE_CATEGORY = "nenhuma"
CORROBORATES_ANSWERS = {"diferente": 0, "mesmo-tema": 1, "mesma-ideia": 2}

# Words that would tell the labeller what a model decided. The leakage test
# asserts none of them appears in the sheet or the reading file.
FORBIDDEN_IN_SHEET = (
    "baseline",
    "jev",
    "llm_decision",
    "confidence",
    "probabilit",
    "_unassigned",
    "similarity",
    "threshold",
)

SHEET_COLUMNS = {
    "dedupe": [
        "item_id",
        "decisao",
        "alvo",
        "nota",
        "fonte",
        "candidato",
        "notas_existentes",
    ],
    "moc_category": ["item_id", "categoria", "nota", "termos", "notas"],
    "corroborates": ["item_id", "decisao", "nota", "nota_a", "nota_b"],
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
    """Rows of ``site`` the decision model answered, with the input it saw.

    A ``corroborates`` pair is stored twice (``:ab`` new note first, ``:ba``
    reversed); it becomes one item, from the ``ab`` row, carrying the ``ba``
    answer along for the key.
    """
    answered = [
        r
        for r in rows
        if r["site"] == site and r["jev"] and not r["error"] and r.get("state") is not None
    ]
    if site != "corroborates":
        return answered
    reversed_of = {
        r["subject_id"].rsplit(":", 1)[0]: r for r in answered if r["subject_id"].endswith(":ba")
    }
    pairs = []
    for r in answered:
        pair, order = r["subject_id"].rsplit(":", 1)
        if order == "ab":
            ba = reversed_of.get(pair)
            pairs.append({**r, "jev_ba": ba["jev"] if ba else None})
    return pairs


def stratum_of(row: dict[str, Any]) -> str:
    if row["site"] == "dedupe":
        return row["baseline"]["decision"]
    if row["site"] == "corroborates":
        base = row["baseline"]
        return similarity_band(float(base["similarity"]), float(base["threshold"]))
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
    letter = ITEM_LETTER[rows[0]["site"]] if rows else "X"
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
    title = note.get("title") or "(sem titulo)"
    body = note_text(note) if note.get("thesis") else (note.get("text") or "")
    return f"{letter}) {title}\n\n{body}".strip()


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


NOTE_FIELDS = (
    ("thesis", "Tese"),
    ("definition", "Definição"),
    ("intuition", "Intuição"),
    ("example", "Exemplo"),
    ("limits", "Limites"),
)
_NOTE_HEADINGS = {
    "definition": "Definição",
    "intuition": "Intuição",
    "example": "Exemplo",
    "limits": "Limites",
}


def note_text(note: dict[str, Any]) -> str:
    """Every filled content field of a note or candidate, labelled."""
    return "\n\n".join(f"{label}: {note[key]}" for key, label in NOTE_FIELDS if note.get(key))


def note_sections(title: str, body: str) -> dict[str, str]:
    """Content sections of a permanent note body; connections and managed blocks left out."""
    from zettel.manual_lit import thesis_from_permanent_note
    from zettel.markdown_fences import h2_section

    sections = {"thesis": thesis_from_permanent_note({"title": title}, body)}
    for key, heading in _NOTE_HEADINGS.items():
        sections[key] = h2_section(body, heading)
    return sections


def with_full_notes(items: list[Item], state_db: Path) -> list[Item]:
    """Replace the excerpts in each item's state with the full notes from ``state_db``.

    A note or concept that no longer exists keeps the excerpt the model saw.
    """
    con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:

        def full_note(note_id: str) -> dict[str, str] | None:
            row = con.execute(
                "SELECT title, body FROM notes WHERE note_id = ?", (note_id,)
            ).fetchone()
            return note_sections(row[0] or "", row[1] or "") if row else None

        enriched = []
        for item in items:
            row = copy.deepcopy(item.row)
            state = row["state"]
            if row["site"] == "dedupe":
                found = con.execute(
                    "SELECT candidate_json FROM concepts WHERE concept_id = ?", (row["subject_id"],)
                ).fetchone()
                if found and found[0]:
                    cand = json.loads(found[0])
                    state["candidate"] = {k: cand.get(k) or "" for k, _ in NOTE_FIELDS}
                for note in state["existing_notes"]:
                    note.update(full_note(note["id"]) or {})
            elif row["site"] == "corroborates":
                new_id = row["baseline"].get("new_note") or ""
                pair = row["subject_id"].rsplit(":", 1)[0].split("|")
                other_id = next((n for n in pair if n != new_id), "")
                state["note_a"] = full_note(new_id) or state["note_a"]
                state["note_b"] = full_note(other_id) or state["note_b"]
            enriched.append(dataclasses.replace(item, row=row))
        return enriched
    finally:
        con.close()


def sheet_row(item: Item) -> list[str]:
    state = item.row["state"]
    if item.row["site"] == "corroborates":
        return [
            item.item_id,
            "",  # decisao: diferente | mesmo-tema | mesma-ideia | ?
            "",  # nota livre
            note_text(state["note_a"]),
            note_text(state["note_b"]),
        ]
    if item.row["site"] == "dedupe":
        cand = state["candidate"]
        return [
            item.item_id,
            "",  # decisao: nova | repete | desenvolve | ?
            "",  # alvo: letra da nota (obrigatoria em repete/desenvolve)
            "",  # nota livre
            source_of(item.row),
            note_text(cand),
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
        "- `repete` — se o candidato fosse apagado, nada se perderia: a nota existente já",
        "  diz o mesmo",
        "- `desenvolve` — a nota existente afirma a ideia, e o candidato acrescenta uma",
        "  condição, um aspecto ou uma consequência",
        "- `nova` — o candidato afirma algo que nenhuma nota existente afirma, mesmo",
        "  usando o mesmo vocabulário",
        f"- `{UNJUDGEABLE}` — não dá para julgar com o que está aqui",
        "",
        "Em `repete` e `desenvolve`, preencha `alvo` com a **letra** da nota existente.",
        "A coluna `nota` é livre — vale sobretudo quando você hesitou.",
        "",
        "Decida pelo **sentido**, lendo as notas. Não use score de similaridade nem outro",
        "modelo: esta planilha é o gabarito contra o qual os modelos são medidos, e uma",
        "resposta derivada de um score ou de um LLM mede concordância com ele, não acerto.",
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
            "**Candidato**",
            "",
            note_text(cand),
            "",
            "**Notas existentes:**",
            "",
            dedupe_existing_text(item),
            "",
            "---",
            "",
        ]
    return lines


def _corroborates_reading(items: list[Item]) -> list[str]:
    lines = [
        "# Rotulagem cega — mesma ideia entre obras",
        "",
        "Cada item traz duas notas permanentes escritas a partir de **obras diferentes**.",
        "Elas afirmam a mesma ideia?",
        "",
        "- `diferente` — afirmam ideias diferentes",
        "- `mesmo-tema` — tratam do mesmo tema, mas as teses diferem: uma acrescenta,",
        "  restringe ou contradiz a outra",
        "- `mesma-ideia` — afirmam a mesma ideia, com outras palavras ou outros exemplos",
        f"- `{UNJUDGEABLE}` — não dá para julgar com o que está aqui",
        "",
        "Responda na coluna `decisao`. A coluna `nota` é livre.",
        "",
        "Nada aqui diz o que qualquer modelo decidiu, nem quão parecidas as notas são",
        "para o embedding. É de propósito.",
        "",
        "---",
        "",
    ]
    for item in items:
        state = item.row["state"]
        lines += [
            f"## {item.item_id}",
            "",
            "**Nota A**",
            "",
            note_text(state["note_a"]),
            "",
            "**Nota B**",
            "",
            note_text(state["note_b"]),
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
    if site == "dedupe":
        lines = _dedupe_reading(items)
    elif site == "corroborates":
        lines = _corroborates_reading(items)
    else:
        lines = _category_reading(items, categories)
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
        "answers": {
            "dedupe": {"decisao": DEDUPE_ANSWERS, "unjudgeable": UNJUDGEABLE},
            "moc_category": {"none": NONE_CATEGORY, "unjudgeable": UNJUDGEABLE},
            "corroborates": {"decisao": CORROBORATES_ANSWERS, "unjudgeable": UNJUDGEABLE},
        }[site],
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
        elif site == "corroborates":
            ba = (row.get("jev_ba") or {}).get("same_idea") or {}
            entry["jev"] = {
                "ab_score": row["jev"]["same_idea"]["score"],
                "ba_score": ba.get("score"),
                "ab_converge": row["jev"]["converge"]["noul"],
            }
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
            "(dedupe), `zettel garden` (categoria) ou `zettel connect` (corroborates) "
            "com decision.sites em shadow e TYPESAFE_API_KEY no .env."
        )
        return 1

    categories: list[tuple[str, str, list[str]]] = []
    if args.site == "moc_category":
        from zettel.config import load_config
        from zettel.decision.shadow import categories_with_topics

        categories = categories_with_topics(load_config())

    items = with_full_notes(
        sample(rows, per_stratum=args.per_stratum, seed=args.seed), args.state_db
    )
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
