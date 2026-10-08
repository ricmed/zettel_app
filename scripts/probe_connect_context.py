"""Re-run Prompt 2 over frozen retrieval: which relations agree with a human? (#212, #231)

Prompt 2 (`prompts/permanent_note.md`) picks typed relations (`supports`,
`contradicts`, `extends`, ...) between a new note and the notes `connect` retrieved.
Until #212 it saw each neighbour as a 150-character excerpt
(`linking.rag_note_chars`). This probe re-asks Prompt 2 with the production model
over a sample of concepts that already have a note, in two conditions that differ
**only** in that number:

* `trunc` -- 150 characters per neighbour (the historical excerpt);
* `full` -- `FULL_NOTE_CHARS` (6000, above any real note): the whole `note_content`.

Each condition runs twice (`-a`, `-b`) to show the noise at the connect temperature.
The pre-registered rule (`evals/preregistration/212-connect-contexto-completo.md`)
reads the `-a` runs.

Three steps, each reusing what the previous one recorded under
`.eval-work/connect-context/` (gitignored):

1. **Snapshot** (`--sample N --seed S`): picks N concepts, round-robin across
   sources, and freezes what `connect` would hand Prompt 2 for each -- the candidate,
   the retrieved neighbours (`note_content`, hop, relation), the distant analogies,
   the literature link and the image context. Costs query embeddings, no LLM. Both
   conditions read the same snapshot, so retrieval cannot differ between them.
2. **Runs** (`--yes` to call the model): one Prompt 2 call per concept and run.
   Answers are recorded by run, prompt text, model and temperature; a recorded
   answer makes no call.
3. **Export** (`--export`): a blind sheet of (concept, neighbour) pairs, stratified
   by which `-a` run proposed an edge (`both`, `trunc_only`, `full_only`,
   `neither`), for `scripts/score_decision_gold.py --site relations`. Distant
   analogies stay out: they become suggestions, never edges (ADR-043).

#231 reuses the same snapshot to compare **prompt versions** (`--prompt NOME=CAMINHO`,
repeatable; `--runs` limits the conditions) on pairs already labelled
(`--score-key` + `--labels`), and writes a revision sheet of those labels under new
relation definitions (`--revise`).

Reads state.db and the vector index; writes nothing to either.

Usage:
    .venv/Scripts/python.exe scripts/probe_connect_context.py --sample 40 --seed 0
    .venv/Scripts/python.exe scripts/probe_connect_context.py --sample 40 --seed 0 --yes \\
        --out evals/results/connect-context-212.json
    .venv/Scripts/python.exe scripts/probe_connect_context.py --sample 40 --seed 0 --export
    git show main:prompts/permanent_note.md > .eval-work/prompts/permanent-current.md
    .venv/Scripts/python.exe scripts/probe_connect_context.py --sample 40 --seed 0 --yes \\
        --runs trunc-a,trunc-b --prompt current=.eval-work/prompts/permanent-current.md \\
        --prompt new=prompts/permanent_note.md \\
        --score-key evals/gold/relacoes-GABARITO-NAO-ABRIR.json \\
        --labels evals/gold/relacoes-rotulos-v2.json
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Run from a clone without installing the package (mirrors scripts/ precedent).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RUNS = ("trunc-a", "trunc-b", "full-a", "full-b")
FULL_NOTE_CHARS = 6000
CONTEXT_CHARS = {"trunc": 150, "full": FULL_NOTE_CHARS}
RELATIONS = ("supports", "contradicts", "extends", "depends_on", "exemplifies", "related")
NO_EDGE = "nenhuma"
UNJUDGEABLE = "?"
STRATA = ("both", "trunc_only", "full_only", "neither")
DEFAULT_RECORD_DIR = Path(".eval-work/connect-context")
SHEET_PREFIX = "relacoes"
MAX_INVALID_SHARE = 0.05  # pre-registered validity condition (#212)


# -- Pure pieces ---------------------------------------------------------


def condition_of(run: str) -> str:
    """``trunc-a`` -> ``trunc``."""
    return run.split("-", 1)[0]


def pick_concepts(rows: list[dict[str, Any]], n: int, seed: int) -> list[dict[str, Any]]:
    """N concepts, round-robin across sources after a seeded shuffle per source.

    One large source must not fill the sample: the relations a book's notes form
    among themselves are not the relations across works.
    """
    rng = random.Random(seed)  # noqa: S311 -- sampling, not security
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: r["concept_id"]):
        by_source[row["source_id"]].append(row)
    queues = []
    for source in sorted(by_source):
        group = by_source[source][:]
        rng.shuffle(group)
        queues.append(group)
    picked: list[dict[str, Any]] = []
    while len(picked) < n and any(queues):
        for queue in queues:
            if queue and len(picked) < n:
                picked.append(queue.pop())
    return picked


def parse_answer(text: str, offered: set[str]) -> dict[str, Any]:
    """Prompt 2 text -> ``{status, edges: {note_id: relation}}`` or ``{error}``.

    Mirrors what `connect` keeps: ids are canonicalized, a model-emitted
    `corroborates` is demoted to `supports`, the first relation per target wins and
    a target outside the offered neighbours is dropped.
    """
    from pydantic import ValidationError
    from zettel.connector.links import demote_llm_corroborates, relation_type_value
    from zettel.connector.prompt import parse_permanent_note_output
    from zettel.vault import normalize_note_id

    try:
        output = parse_permanent_note_output(text)
    except (ValueError, ValidationError, TypeError) as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    if output.status == "rejected":
        return {"status": "rejected", "edges": {}, "reason": output.reason}
    edges: dict[str, str] = {}
    for conn in demote_llm_corroborates(list(output.connections)):
        note_id = normalize_note_id(conn.related_note_id)
        if note_id in offered and note_id not in edges:
            edges[note_id] = relation_type_value(conn.relation_type)
    return {"status": "accepted", "edges": edges}


def answer_for(entry: dict[str, Any] | None, note_id: str) -> str:
    """The relation a run gave one pair, or `nenhuma`."""
    return (entry or {}).get("edges", {}).get(note_id, NO_EDGE)


def usable(entry: dict[str, Any] | None) -> bool:
    return bool(entry) and entry.get("status") == "accepted"


def stratum_of(trunc: str, full: str) -> str:
    if NO_EDGE not in (trunc, full):
        return "both"
    if trunc != NO_EDGE:
        return "trunc_only"
    return "full_only" if full != NO_EDGE else "neither"


def build_pairs(
    snapshot: list[dict[str, Any]], runs: dict[str, dict[str, dict[str, Any]]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Every (concept, retrieved neighbour) pair whose concept both `-a` runs accepted.

    Returns the pairs and the concepts left out (rejected or invalid in a `-a` run).
    """
    pairs, excluded = [], []
    for item in snapshot:
        key = item["item_key"]
        trunc, full = runs["trunc-a"].get(key), runs["full-a"].get(key)
        if not (usable(trunc) and usable(full)):
            excluded.append(key)
            continue
        for neighbour in item["similar"]:
            nid = neighbour["note_id"]
            pairs.append(
                {
                    "pair": f"{key}|{nid}",
                    "item_key": key,
                    "note_id": nid,
                    "sampling_stratum": stratum_of(answer_for(trunc, nid), answer_for(full, nid)),
                    "answers": {run: answer_for(runs[run].get(key), nid) for run in RUNS},
                }
            )
    return pairs, excluded


def sample_pairs(
    pairs: list[dict[str, Any]], *, per_stratum: int, seed: int
) -> list[dict[str, Any]]:
    """Census up to ``per_stratum`` per stratum, seeded draw above it, shuffled ids."""
    rng = random.Random(seed)  # noqa: S311 -- sampling a sheet, not security
    by_stratum: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in sorted(pairs, key=lambda p: p["pair"]):
        by_stratum[p["sampling_stratum"]].append(p)
    chosen = []
    for stratum in STRATA:
        group = by_stratum.get(stratum, [])
        chosen.extend(rng.sample(group, per_stratum) if len(group) > per_stratum else group)
    rng.shuffle(chosen)  # position must not leak the stratum
    return [{**p, "item_id": f"R{i:03d}"} for i, p in enumerate(chosen, 1)]


def summarize_runs(
    snapshot: list[dict[str, Any]],
    runs: dict[str, dict[str, dict[str, Any]]],
    comparisons: list[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Validity, edges per note and relation mix per run; `-a`/`-b` stability and
    the agreement of each ``comparisons`` pair over the offered pairs.
    Informative: the rules read human labels."""
    keys = [item["item_key"] for item in snapshot]
    out: dict[str, Any] = {"items": len(keys), "runs": {}}
    for run in sorted(runs):
        answers = runs[run]
        valid = [answers[k] for k in keys if k in answers and "error" not in answers[k]]
        accepted = [a for a in valid if a["status"] == "accepted"]
        relations = Counter(r for a in accepted for r in a["edges"].values())
        missing_or_invalid = len(keys) - len(valid)
        out["runs"][run] = {
            "answered": len(valid),
            "invalid_or_missing": missing_or_invalid,
            "invalid_share": round(missing_or_invalid / len(keys), 4) if keys else None,
            "rejected": len(valid) - len(accepted),
            "edges_per_note": round(sum(relations.values()) / len(accepted), 3)
            if accepted
            else None,
            "relations": dict(sorted(relations.items())),
        }

    def agreement(a: str, b: str) -> dict[str, Any]:
        same = total = 0
        for item in snapshot:
            ea, eb = runs.get(a, {}).get(item["item_key"]), runs.get(b, {}).get(item["item_key"])
            if not (usable(ea) and usable(eb)):
                continue
            for n in item["similar"]:
                total += 1
                same += answer_for(ea, n["note_id"]) == answer_for(eb, n["note_id"])
        return {"pairs": total, "agreement": round(same / total, 4) if total else None}

    out["stability"] = {
        run[:-2]: agreement(run, f"{run[:-2]}-b")
        for run in sorted(runs)
        if run.endswith("-a") and f"{run[:-2]}-b" in runs
    }
    out["agreement"] = {f"{a} x {b}": agreement(a, b) for a, b in comparisons}
    return out


def result_ids(prompts: list[str], runs: list[str]) -> list[str]:
    """The bare run for one prompt (as #212 recorded it), else ``prompt:run``."""
    if len(prompts) == 1:
        return list(runs)
    return [f"{p}:{r}" for p in prompts for r in runs]


def labelled_conditions(
    key: dict[str, Any], runs: dict[str, dict[str, dict[str, Any]]]
) -> dict[str, dict[str, dict[str, Any]]]:
    """Each run's relation for every pair of a sheet key, as scorer conditions."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for run, answers in runs.items():
        out[run] = {}
        for item in key["items"]:
            item_key, note_id = item["subject_id"].rsplit("|", 1)
            out[run][item["item_id"]] = {"decision": answer_for(answers.get(item_key), note_id)}
    return out


# -- Snapshot ------------------------------------------------------------


def _hit_dict(hit: Any, db: Any) -> dict[str, Any]:
    row = db.get_note(hit.note_id) or {}
    return {
        "note_id": hit.note_id,
        "title": hit.title,
        "document": hit.document,
        "hop": hit.hop,
        "via": hit.via,
        "tags": hit.metadata.get("tags", ""),
        "path": row.get("path"),
    }


def build_snapshot(cfg: Any, db: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What `connect` would hand Prompt 2 for each concept, frozen (no LLM call)."""
    from zettel.connector import context
    from zettel.connector.note import literature_ref_for_chunk
    from zettel.index import VectorIndex, index_kwargs
    from zettel.retrieval import Retriever
    from zettel.schemas import PermanentNoteCandidate

    idx = VectorIndex(**index_kwargs(cfg))
    retriever = Retriever(cfg, db, idx)
    taxonomy = context.load_connect_taxonomy(cfg, idx)
    snapshot = []
    for row in rows:
        cand = PermanentNoteCandidate.model_validate_json(row["candidate_json"])
        note_id = row["note_id"]
        query = f"{cand.thesis} {cand.definition}"
        similar = retriever.search_notes(query, topk=cfg.linking.topk, exclude_id=note_id).hits
        distant = context.search_distant_analogies(
            cfg, idx, retriever, query, note_id, similar, taxonomy
        )
        source = db.get_source(row["source_id"]) or {}
        chunk = db.get_chunk(row["chunk_id"]) if row.get("chunk_id") else None
        image_ids = cand.relevant_image_ids or context.fallback_image_ids(
            db, row["source_id"], chunk
        )
        snapshot.append(
            {
                "item_key": f"{row['concept_id']}|{note_id}",
                "concept_id": row["concept_id"],
                "note_id": note_id,
                "source_id": row["source_id"],
                "candidate": cand.model_dump(mode="json"),
                "literature_ref": literature_ref_for_chunk(
                    source.get("citekey") or "unknown", source.get("title") or "", chunk
                ),
                "images_context": context.images_context(db, image_ids),
                "similar": [_hit_dict(h, db) for h in similar],
                "distant": [_hit_dict(h, db) for h in distant],
            }
        )
    return snapshot


class _SnapshotDB:
    """`build_rag_context` reads only a note's path: serve it from the snapshot."""

    def __init__(self, item: dict[str, Any]):
        self._paths = {n["note_id"]: n for n in item["similar"] + item["distant"]}

    def get_note(self, note_id: str) -> dict[str, Any] | None:
        note = self._paths.get(note_id)
        return {"path": note["path"], "title": note["title"]} if note else None


def rag_context_for(item: dict[str, Any], note_chars: int) -> str:
    from zettel.connector.context import build_rag_context
    from zettel.retrieval import RetrievedNote

    def hits(group: str) -> list[RetrievedNote]:
        return [
            RetrievedNote(
                note_id=n["note_id"],
                score=0.0,
                title=n["title"],
                document=n["document"],
                metadata={"tags": n["tags"]},
                hop=n["hop"],
                via=n["via"],
            )
            for n in item[group]
        ]

    return build_rag_context(
        _SnapshotDB(item), hits("similar"), hits("distant"), note_chars=note_chars
    )


# -- Export --------------------------------------------------------------


def _concept_text(candidate: dict[str, Any]) -> str:
    from zettel.note_content import render_note_content

    return render_note_content(candidate)


def write_export(
    sampled: list[dict[str, Any]],
    snapshot: list[dict[str, Any]],
    *,
    out_dir: Path,
    population: dict[str, int],
    seed: int,
    run_meta: dict[str, Any],
    excluded: list[str],
) -> tuple[Path, Path, Path]:
    items = {item["item_key"]: item for item in snapshot}
    notes = {(item["item_key"], n["note_id"]): n for item in snapshot for n in item["similar"]}
    sheet = out_dir / f"{SHEET_PREFIX}-planilha.csv"
    reading = out_dir / f"{SHEET_PREFIX}-leitura.md"
    key = out_dir / f"{SHEET_PREFIX}-GABARITO-NAO-ABRIR.json"
    out_dir.mkdir(parents=True, exist_ok=True)

    with sheet.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(["item_id", "relacao", "nota", "conceito", "nota_existente"])
        for p in sampled:
            neighbour = notes[(p["item_key"], p["note_id"])]
            writer.writerow(
                [
                    p["item_id"],
                    "",
                    "",
                    _concept_text(items[p["item_key"]]["candidate"]),
                    neighbour["document"],
                ]
            )

    lines = [
        "# Rotulagem cega — relação entre um conceito novo e uma nota existente",
        "",
        "Cada item traz um **conceito** que vai virar nota permanente e uma **nota que já",
        "existe** no vault, recuperada por proximidade. Qual relação o conceito tem com a",
        "nota existente? Leia como: *o conceito ___ a nota existente*.",
        "",
        "- `supports` — reforça ou valida a tese dela com evidência ou argumento",
        "- `contradicts` — contradiz ou tensiona a tese dela",
        "- `extends` — amplia, aprofunda ou especializa o conceito dela",
        "- `depends_on` — pressupõe a nota existente; não se entende sem ela",
        "- `exemplifies` — é um caso particular dela, ou ela é um caso particular dele",
        "- `related` — relação temática clara, mas que não cabe acima",
        f"- `{NO_EDGE}` — não há relação conceitual que valha uma aresta; dividir o tema",
        "  não basta",
        f"- `{UNJUDGEABLE}` — não dá para julgar com o que está aqui",
        "",
        "Responda na coluna `relacao`. A coluna `nota` é livre. Na dúvida entre uma",
        f"relação fraca e `{NO_EDGE}`, prefira `{NO_EDGE}`: o prompt pede só conexões",
        "genuínas, de 0 a 3 por nota.",
        "",
        "Decida pelo **sentido**, sem score de similaridade e sem outro modelo. Nada aqui",
        "diz o que qualquer modelo decidiu, nem por que a nota foi recuperada.",
        "",
        "---",
    ]
    for p in sampled:
        neighbour = notes[(p["item_key"], p["note_id"])]
        lines += [
            "",
            f"## {p['item_id']}",
            "",
            "**Conceito**",
            "",
            _concept_text(items[p["item_key"]]["candidate"]),
            "",
            f"**Nota existente** — {neighbour['title']}",
            "",
            neighbour["document"],
            "",
            "---",
        ]
    reading.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "site": "relations",
        "exported_at": datetime.now(UTC).isoformat(),
        "seed": seed,
        "n_items": len(sampled),
        "population": population,
        "excluded_concepts": excluded,
        "runs": run_meta,
        "answers": {"relacao": [*RELATIONS, NO_EDGE], "unjudgeable": UNJUDGEABLE},
        "items": [
            {
                "item_id": p["item_id"],
                "subject_id": p["pair"],
                "sampling_stratum": p["sampling_stratum"],
                "answers": p["answers"],
            }
            for p in sampled
        ],
    }
    key.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return sheet, reading, key


REVISION_GUIDE = [
    "# Revisão dos rótulos de relação sob as definições de #231",
    "",
    "Os mesmos 80 pares de #212. Cada item mostra o rótulo que você deu antes",
    "(`relacao_anterior`). Confirme ou troque, aplicando as regras **na ordem**: a",
    "primeira que valer decide. Leia como *o conceito ___ a nota existente*.",
    "",
    "1. `contradicts` — as duas teses **não podem ser verdadeiras juntas**. Resolver ou",
    "   contornar uma limitação que a outra aponta **não** é contradição (é `extends`).",
    "2. `depends_on` — o conceito **não pode ser definido nem entendido** sem o conceito",
    "   da nota existente. Partir dela ou construir sobre ela não basta.",
    "3. `exemplifies` — um é um **caso concreto** do outro (dados, domínio, situação),",
    "   sem acrescentar mecanismo, condição ou técnica.",
    "4. `extends` — acrescenta **condição, mecanismo, especialização, técnica,",
    "   consequência** ou a solução de uma limitação apontada pela outra.",
    "5. `supports` — traz **evidência ou argumento para a mesma afirmação**, sem afirmar",
    "   nada novo.",
    "6. `related` — relação conceitual que se descreve numa frase e não cabe acima",
    "   (soluções alternativas para o mesmo problema; o mesmo mecanismo em outro",
    "   domínio).",
    "7. `nenhuma` — **tema em comum não basta**: se a única descrição possível é",
    '   "ambos tratam de X", é `nenhuma`. Uma relação fraca também é `nenhuma`.',
    "",
    "Responda em `relacao` (pode repetir a anterior). `?` se não der para julgar.",
    "",
    "---",
]


def write_revision(
    key: dict[str, Any],
    labels: list[dict[str, Any]],
    snapshot: list[dict[str, Any]],
    out_dir: Path,
) -> tuple[Path, Path]:
    """The labelled pairs again, with the previous label, under the new definitions."""
    items = {item["item_key"]: item for item in snapshot}
    previous = {lab["item_id"]: lab["human_decision"] or UNJUDGEABLE for lab in labels}
    sheet = out_dir / f"{SHEET_PREFIX}-revisao-planilha.csv"
    reading = out_dir / f"{SHEET_PREFIX}-revisao-leitura.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = list(REVISION_GUIDE)
    with sheet.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(
            ["item_id", "relacao_anterior", "relacao", "nota", "conceito", "nota_existente"]
        )
        for entry in key["items"]:
            item_key, note_id = entry["subject_id"].rsplit("|", 1)
            item = items[item_key]
            neighbour = next(n for n in item["similar"] if n["note_id"] == note_id)
            concept = _concept_text(item["candidate"])
            old = previous.get(entry["item_id"], "")
            writer.writerow([entry["item_id"], old, "", "", concept, neighbour["document"]])
            lines += [
                "",
                f"## {entry['item_id']} — antes: `{old}`",
                "",
                "**Conceito**",
                "",
                concept,
                "",
                f"**Nota existente** — {neighbour['title']}",
                "",
                neighbour["document"],
                "",
                "---",
            ]
    reading.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sheet, reading


# -- CLI -----------------------------------------------------------------


def _eligible_rows(db: Any) -> list[dict[str, Any]]:
    """Concepts with a pipeline note still on record and a stored candidate."""
    return [
        dict(r)
        for r in db._fetchall(
            """SELECT c.concept_id, c.source_id, c.chunk_id, c.note_id, c.candidate_json
                 FROM concepts c JOIN notes n ON n.note_id = c.note_id
                WHERE c.candidate_json IS NOT NULL AND COALESCE(n.origin, '') != 'manual'"""
        )
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--state-db", type=Path, default=Path("data/state.db"))
    parser.add_argument("--record-dir", type=Path, default=DEFAULT_RECORD_DIR)
    parser.add_argument("--sample", type=int, default=40)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--yes", action="store_true", help="Autoriza as chamadas nao gravadas")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--export", action="store_true", help="Exporta a planilha cega de pares")
    parser.add_argument("--per-stratum", type=int, default=20)
    parser.add_argument("--out-dir", type=Path, default=Path("evals/gold"))
    parser.add_argument("--force", action="store_true", help="Sobrescreve planilha e gabarito")
    parser.add_argument(
        "--prompt",
        action="append",
        default=[],
        metavar="NOME=CAMINHO",
        help="Versao do Prompt 2 (repetivel). Padrao: current=<prompt de producao>",
    )
    parser.add_argument(
        "--runs", default=",".join(RUNS), help=f"Rodadas, separadas por virgula ({','.join(RUNS)})"
    )
    parser.add_argument("--score-key", type=Path, default=None, help="Gabarito de pares rotulados")
    parser.add_argument("--labels", type=Path, default=None, help="Rotulos desse gabarito")
    parser.add_argument(
        "--revise", type=Path, default=None, help="Rotulos a revisar: gera a planilha de revisao"
    )
    args = parser.parse_args(argv)
    run_names = [r for r in args.runs.split(",") if r]
    if unknown := set(run_names) - set(RUNS):
        parser.error(f"rodadas desconhecidas: {sorted(unknown)}")

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from dotenv import load_dotenv

    load_dotenv()
    from zettel.config import effective_temperature, llm_phase, load_config
    from zettel.connector.prompt import Prompt2Payload, prompt2_messages
    from zettel.domain_examples import load_domain_examples, render_for_prompt
    from zettel.hashing import sha256_hex
    from zettel.llm import load_prompt_parts
    from zettel.schemas import PermanentNoteCandidate
    from zettel.state import StateDB

    cfg = load_config()
    spec = llm_phase(cfg, "connect")
    temperature = effective_temperature(cfg, spec)
    db = StateDB(args.state_db)

    snapshot_path = args.record_dir / f"snapshot-n{args.sample}-s{args.seed}.json"
    if snapshot_path.exists():
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))["items"]
    else:
        rows = pick_concepts(_eligible_rows(db), args.sample, args.seed)
        snapshot = build_snapshot(cfg, db, rows)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(
            json.dumps({"items": snapshot}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"modelo: {spec.provider}/{spec.model} @ {temperature}")
    print(f"snapshot: {snapshot_path} ({len(snapshot)} conceitos)")

    if args.revise:
        key = json.loads(
            (args.score_key or args.out_dir / f"{SHEET_PREFIX}-GABARITO-NAO-ABRIR.json").read_text(
                encoding="utf-8"
            )
        )
        labels = json.loads(args.revise.read_text(encoding="utf-8"))["labels"]
        sheet, reading = write_revision(key, labels, snapshot, args.out_dir)
        print(f"revisao: {sheet}\nleitura: {reading}")
        return 0

    prompt_args = []
    for value in args.prompt:
        name, sep, path = value.partition("=")
        if not sep or not name or not path or ":" in name:
            parser.error(f"--prompt espera NOME=CAMINHO, recebeu {value!r}")
        prompt_args.append((name, Path(path)))
    prompt_args = prompt_args or [("current", cfg.prompts_path / "permanent_note.md")]
    prompts = {name: load_prompt_parts(path) for name, path in prompt_args}
    examples = render_for_prompt(load_domain_examples(cfg.domain.examples_path), "permanent_note")

    def build(run: str, item: dict[str, Any], parts: Any) -> tuple[str, str]:
        payload = Prompt2Payload(
            source_id=item["source_id"],
            literature_ref=item["literature_ref"],
            rag_context=rag_context_for(item, CONTEXT_CHARS[condition_of(run)]),
            images_context=item["images_context"],
            examples=examples,
        )
        cand = PermanentNoteCandidate.model_validate(item["candidate"])
        return prompt2_messages(cfg, parts, cand, payload)

    llm = None
    if args.yes:
        from zettel.llm import get_llm

        llm = get_llm(cfg, "connect")

    runs: dict[str, dict[str, dict[str, Any]]] = {}
    run_meta: dict[str, Any] = {}
    for result_id in result_ids(list(prompts), run_names):
        name, _, run = result_id.rpartition(":")
        parts = prompts[name or next(iter(prompts))]
        run_key = sha256_hex(
            f"{run}|{CONTEXT_CHARS[condition_of(run)]}|{parts.full_template}|"
            f"{spec.provider}/{spec.model}@{temperature}"
        )[:12]
        record_path = args.record_dir / f"{run}-{run_key}.json"
        recorded = (
            json.loads(record_path.read_text(encoding="utf-8"))["answers"]
            if record_path.exists()
            else {}
        )
        missing = [item for item in snapshot if item["item_key"] not in recorded]
        chars = sum(len("".join(build(run, item, parts))) for item in missing)
        print(
            f"{result_id}: gravados {len(recorded)} | a chamar {len(missing)} "
            f"(~{chars // 4} tokens)"
        )
        run_meta[result_id] = {
            "record": record_path.name,
            "note_chars": CONTEXT_CHARS[condition_of(run)],
        }
        if missing and args.yes:
            from zettel.llm import call_llm

            for item in missing:
                system, user = build(run, item, parts)
                text = call_llm(
                    llm,
                    user,
                    system=system or None,
                    provider=spec.provider,
                    prompt_cache=cfg.llm.prompt_cache,
                    label=f"probe-connect:{result_id}",
                )
                entry = parse_answer(
                    text, {n["note_id"] for n in item["similar"] + item["distant"]}
                )
                if "error" in entry:
                    print(f"  {item['item_key']}: {entry['error']}")
                    continue
                recorded[item["item_key"]] = entry
                record_path.parent.mkdir(parents=True, exist_ok=True)
                record_path.write_text(
                    json.dumps(
                        {
                            "run": run,
                            "model": f"{spec.provider}/{spec.model}",
                            "temperature": temperature,
                            "answers": recorded,
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        runs[result_id] = recorded

    comparisons = [("trunc-a", "full-a")] if {"trunc-a", "full-a"} <= set(runs) else []
    names = list(prompts)
    comparisons += [
        (f"{names[0]}:{run}", f"{other}:{run}")
        for other in names[1:]
        for run in run_names
        if run.endswith("-a")
    ]
    summary = summarize_runs(snapshot, runs, comparisons)
    summary["model"] = f"{spec.provider}/{spec.model}"
    summary["temperature"] = temperature
    summary["note_chars"] = CONTEXT_CHARS
    summary["prompts"] = {name: str(path) for name, path in prompt_args}
    if args.score_key and args.labels:
        from score_decision_gold import relations_detail, score_conditions

        key = json.loads(args.score_key.read_text(encoding="utf-8"))
        labels = json.loads(args.labels.read_text(encoding="utf-8"))["labels"]
        conditions = labelled_conditions(key, runs)
        scored = score_conditions(labels, conditions, key["population"], comparisons)
        scored["detail"] = relations_detail(labels, conditions)
        summary["labelled"] = scored
    text = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(text, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")

    if args.export:
        if len(prompts) > 1 or any(
            len(runs.get(r, {})) < len(snapshot) for r in ("trunc-a", "full-a")
        ):
            print("Exportacao exige trunc-a e full-a completos: rode com --yes antes.")
            return 1
        existing = [
            p
            for p in (
                args.out_dir / f"{SHEET_PREFIX}-planilha.csv",
                args.out_dir / f"{SHEET_PREFIX}-GABARITO-NAO-ABRIR.json",
            )
            if p.exists()
        ]
        if existing and not args.force:
            print(f"{', '.join(map(str, existing))} ja existe(m). Use --force para sobrescrever.")
            return 1
        pairs, excluded = build_pairs(snapshot, runs)
        population = dict(Counter(p["sampling_stratum"] for p in pairs))
        sampled = sample_pairs(pairs, per_stratum=args.per_stratum, seed=args.seed)
        paths = write_export(
            sampled,
            snapshot,
            out_dir=args.out_dir,
            population=population,
            seed=args.seed,
            run_meta=run_meta,
            excluded=excluded,
        )
        print(f"{len(sampled)} pares exportados de {len(pairs)} (fora: {len(excluded)} conceitos)")
        for stratum in STRATA:
            print(
                f"  {stratum}: {sum(p['sampling_stratum'] == stratum for p in sampled)}"
                f" de {population.get(stratum, 0)}"
            )
        print(
            "planilha: {}\nleitura:  {}\ngabarito: {} (nao abrir antes de rotular)".format(*paths)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
