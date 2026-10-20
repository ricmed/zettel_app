"""Export a blind, per-note labelling sheet for the category attribution (#295).

The garden puts every permanent note in the taxonomy category whose label is
closest to it (``gardener_assign.assign_notes_to_categories``). Whether that is
right was never judged by a human: the decision-layer shadow (ADR-055) only
asks per cluster, and its 9/22 agreement with the argmax does not say which
side is wrong. This script exports the sheet that answers it **per note**, with
the answer of each condition pre-registered in
``evals/preregistration/295-atribuicao-categoria.md`` frozen in the key:

* ``A`` — ``label``: argmax over ``{pilar}: {categoria}`` (the current default);
* ``B`` — ``leaves_in_label``: argmax over ``{pilar}: {categoria} — folha1, folha2``;
* ``C`` — ``max_leaf``: one vector per leaf, the category of the best leaf;
* ``D`` — the Jev shadow answer of the note's cluster, when a ``moc_category``
  row lists the note (reference only, partial coverage).

For A, B and C the key also keeps the best cosine and the margin to the second
category, so the scorer can sweep ``gardener.assign_min_similarity``.

**Strata.** ``agree`` (A, B and C give the same category) and ``disagree``;
disagreements are where a condition is wrong, so each stratum is a census up to
``--per-stratum`` and a seeded draw above it. Populations go in the key.

**Blind by construction.** The sheet and the reading file carry only the note
(title and content via ``note_content``) and the numbered taxonomy; items are
shuffled. The labeller writes a category number or name, ``nenhuma`` when no
category fits, or ``?`` when the note cannot be judged.

Cost: the category texts of A, B and C are embedded once (a few hundred short
labels); note vectors are read from ``--chroma-path`` (use a copy of
``data/chroma``). SQLite is opened read-only. No LLM call.

Usage:
    .venv/Scripts/python.exe scripts/export_category_gold.py --chroma-path COPY
    .venv/Scripts/python.exe scripts/export_category_gold.py --chroma-path COPY --per-stratum 80
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zettel.config import AppConfig, load_config
from zettel.gardener_assign import (
    category_leaves,
    category_pairs,
    embed_category_labels,
    rank_categories,
)
from zettel.note_content import note_content

CONDITIONS = {"A": "label", "B": "leaves_in_label", "C": "max_leaf"}
NONE_CATEGORY = "nenhuma"
UNJUDGEABLE = "?"
PREFIX = "categoria-notas"
SHEET_COLUMNS = ["item_id", "categoria", "titulo", "conteudo"]
_PERMANENT = "instr(path, '30_Permanent') > 0"


def load_notes(db_path: Path) -> dict[str, dict[str, str]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return {
            r["note_id"]: {"title": r["title"] or "", "body": r["body"] or ""}
            for r in conn.execute(f"SELECT note_id, title, body FROM notes WHERE {_PERMANENT}")
        }
    finally:
        conn.close()


def jev_by_title(db_path: Path) -> dict[str, str]:
    """Note title -> Jev's category for the cluster whose shadow row lists it."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT state_json, jev_json FROM decision_shadow WHERE site='moc_category'"
        ).fetchall()
    except sqlite3.OperationalError:  # a database from before ADR-055
        return {}
    finally:
        conn.close()
    answers: dict[str, str] = {}
    for state_json, jev_json in rows:
        choice = ((json.loads(jev_json or "{}") or {}).get("category") or {}).get("choice")
        if not choice:
            continue
        for note in (json.loads(state_json or "{}") or {}).get("notes", []):
            if note.get("title"):
                answers[note["title"]] = choice
    return answers


def load_vectors(chroma_path: Path) -> dict[str, np.ndarray]:
    import chromadb

    got = (
        chromadb.PersistentClient(path=str(chroma_path))
        .get_collection("permanent_notes")
        .get(include=["embeddings"])
    )
    return {
        nid: np.asarray(v, dtype=float)
        for nid, v in zip(got["ids"], got["embeddings"], strict=True)
    }


def condition_answers(
    vectors: Mapping[str, np.ndarray],
    label_vectors: Mapping[str, Mapping[str, np.ndarray]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """note_id -> condition -> ``{choice, score, second, margin}`` for A, B and C."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for nid, vec in vectors.items():
        per: dict[str, dict[str, Any]] = {}
        for cond, cats in label_vectors.items():
            ranked = rank_categories(vec, dict(cats))
            (best, score), (second, second_score) = ranked[0], ranked[1]
            per[cond] = {
                "choice": best,
                "score": round(score, 4),
                "second": second,
                "margin": round(score - second_score, 4),
            }
        out[nid] = per
    return out


def stratum_of(answers: Mapping[str, Mapping[str, Any]]) -> str:
    return "agree" if len({answers[c]["choice"] for c in CONDITIONS}) == 1 else "disagree"


def sample(
    answers: Mapping[str, Mapping[str, Any]], *, per_stratum: int, seed: int
) -> tuple[list[str], dict[str, int]]:
    """Note ids drawn per stratum (census up to ``per_stratum``), shuffled; and populations."""
    strata: dict[str, list[str]] = {}
    for nid in sorted(answers):
        strata.setdefault(stratum_of(answers[nid]), []).append(nid)
    rng = random.Random(seed)  # noqa: S311 -- sampling a sheet, not security
    chosen: list[str] = []
    for name in sorted(strata):
        ids = strata[name]
        chosen.extend(ids if len(ids) <= per_stratum else rng.sample(ids, per_stratum))
    rng.shuffle(chosen)
    return chosen, {name: len(ids) for name, ids in sorted(strata.items())}


def numbered_categories(cfg: AppConfig) -> list[dict[str, Any]]:
    leaves = category_leaves(cfg.gardener)
    return [
        {"n": i, "pilar": pilar, "categoria": cat, "folhas": leaves.get(cat, [])}
        for i, (pilar, cat) in enumerate(category_pairs(cfg.gardener), start=1)
    ]


def _guard(paths: list[Path], force: bool) -> None:
    existing = [p for p in paths if p.exists()]
    if existing and not force:
        raise SystemExit(
            f"Ja existe: {', '.join(map(str, existing))}. Pode conter rotulos; use --force."
        )


def export(
    out_dir: Path,
    notes: Mapping[str, Mapping[str, str]],
    answers: Mapping[str, Mapping[str, Any]],
    jev: Mapping[str, str],
    categories: list[dict[str, Any]],
    *,
    per_stratum: int,
    seed: int,
    force: bool,
    meta: Mapping[str, Any],
) -> int:
    """Write the sheet, the reading file and the frozen key. Returns how many items."""
    sheet = out_dir / f"{PREFIX}-planilha.csv"
    reading = out_dir / f"{PREFIX}-leitura.md"
    key_path = out_dir / f"{PREFIX}-GABARITO-NAO-ABRIR.json"
    _guard([sheet, reading, key_path], force)
    out_dir.mkdir(parents=True, exist_ok=True)

    chosen, population = sample(
        {n: a for n, a in answers.items() if n in notes}, per_stratum=per_stratum, seed=seed
    )
    items: list[dict[str, Any]] = []
    rows: list[list[str]] = []
    lines = ["# Atribuicao de categoria por nota (#295)", "", "## Categorias", ""]
    for cat in categories:
        leaves = f" ({', '.join(cat['folhas'])})" if cat["folhas"] else ""
        lines.append(f"{cat['n']}. **{cat['categoria']}** — {cat['pilar']}{leaves}")
    lines += [
        "",
        (
            f"Responda com o numero ou o nome; `{NONE_CATEGORY}` se nenhuma serve; "
            f"`{UNJUDGEABLE}` se nao da para julgar."
        ),
        "",
        "## Notas",
        "",
    ]
    for i, nid in enumerate(chosen, start=1):
        item_id = f"N{i:03d}"
        note = notes[nid]
        content = note_content(note["title"], note["body"])
        rows.append([item_id, "", note["title"], content])
        lines += [f"### {item_id} — {note['title']}", "", content, ""]
        cond = {c: dict(answers[nid][c]) for c in CONDITIONS}
        cond["D"] = {"choice": jev.get(note["title"])}
        items.append(
            {
                "item_id": item_id,
                "note_id": nid,
                "stratum": stratum_of(answers[nid]),
                "conditions": cond,
            }
        )

    with sheet.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(SHEET_COLUMNS)
        writer.writerows(rows)
    reading.write_text("\n".join(lines) + "\n", encoding="utf-8")
    key = {
        "issue": 295,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "seed": seed,
        "per_stratum": per_stratum,
        "population": population,
        "conditions": {**CONDITIONS, "D": "jev_shadow"},
        "answers": {"none": NONE_CATEGORY, "unjudgeable": UNJUDGEABLE},
        "categories": categories,
        "items": items,
        **dict(meta),
    }
    key_path.write_text(json.dumps(key, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(items)


def embed_conditions(
    cfg: AppConfig, embed: Callable[[list[tuple[str, str]], str], dict[str, np.ndarray]]
) -> dict[str, dict[str, np.ndarray]]:
    """Category label vectors of A, B and C (``embed(pairs, mode)``)."""
    pairs = category_pairs(cfg.gardener)
    return {cond: embed(pairs, mode) for cond, mode in CONDITIONS.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--db", type=Path, default=Path("data/state.db"))
    parser.add_argument("--chroma-path", type=Path, required=True, help="copia de data/chroma")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=Path(".eval-work/categoria-notas"))
    parser.add_argument("--per-stratum", type=int, default=75)
    parser.add_argument("--seed", type=int, default=295)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    from zettel.index import VectorIndex, index_kwargs

    cfg = load_config(args.config)
    idx = VectorIndex(**{**index_kwargs(cfg), "chroma_path": args.chroma_path})
    leaves = category_leaves(cfg.gardener)
    gcfg = cfg.gardener

    def embed(pairs: list[tuple[str, str]], mode: str) -> dict[str, np.ndarray]:
        return embed_category_labels(
            idx, pairs, cfg.domain.name, gcfg.category_label_template, mode=mode, leaves=leaves
        )

    label_vectors = embed_conditions(cfg, embed)
    answers = condition_answers(load_vectors(args.chroma_path), label_vectors)
    n = export(
        args.out,
        load_notes(args.db),
        answers,
        jev_by_title(args.db),
        numbered_categories(cfg),
        per_stratum=args.per_stratum,
        seed=args.seed,
        force=args.force,
        meta={
            "embedding": (
                f"{cfg.embedding.provider}/{cfg.embedding.model}@{cfg.embedding.dimensions}"
            ),
            "label_template": gcfg.category_label_template,
        },
    )
    print(f"{n} nota(s) exportadas em {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
