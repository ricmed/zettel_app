"""Probe harness: is the BM25 bypass admitting notes on incidental word overlap?

`retrieval.relevance_floor.bm25_bypass_max_rank` lets a well-ranked lexical hit
skip the vector-similarity gate. Rank is a **relative** test — "did you rank well
among whoever matched" — and `_fts_match_expr` joins the query's terms with
``OR``, so a note is returned for matching *any one* of them. When a query's
match pool is smaller than the cutoff (routine on a small corpus) "top 5"
degenerates into "everything that matched at all", and a note sharing one common
word with the question bypasses the floor.

`bm25_bypass_min_coverage` is the absolute counterpart: the fraction of the
query's terms actually present in the note. This script measures the two bands
that threshold has to separate, and sweeps it.

Cost: **no LLM calls, no embedding calls.** It reads SQLite/FTS5 only, which is
also why it can be run freely while tuning.

Usage:
    .venv/Scripts/python.exe scripts/probe_bm25_bypass.py
    .venv/Scripts/python.exe scripts/probe_bm25_bypass.py --off-domain-file mine.txt
    .venv/Scripts/python.exe scripts/probe_bm25_bypass.py --in-domain-file mine.txt
    .venv/Scripts/python.exe scripts/probe_bm25_bypass.py --json out.json

The in-domain list MUST be edited for your own vault — the built-in one is about
this repository's corpus (prompt engineering, time series, reasoning in LLMs) and
means nothing elsewhere. Keep a few **single-term jargon queries** in it: that is
the case the bypass exists to rescue (ADR-003), and any gate that kills those is
the wrong gate however well it suppresses noise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Run from a clone without installing the package (mirrors scripts/ precedent).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zettel.config import load_config
from zettel.hashing import fold_for_match
from zettel.state import StateDB, fts_query_terms

OFF_DOMAIN = [
    "como fazer risoto de cogumelos",
    "manutencao preventiva do motor do carro",
    "receita de bolo de cenoura com cobertura de chocolate",
    "escalacao do time para a final do campeonato brasileiro",
    "sintomas de deficiencia de vitamina D em idosos",
    "como podar uma roseira no inverno",
    "previsao do tempo para o fim de semana no litoral norte",
    "qual o melhor tenis para corrida de rua",
    "quanto custa trocar a fechadura da porta",
    "historia da colonizacao portuguesa na africa",
]

IN_DOMAIN = [
    "o que e chain of thought prompting",
    "few-shot e zero-shot na escolha de prompts",
    "modelos ARIMA e series temporais nao estacionarias",
    "raciocinio de senso comum em LLMs",
    "componentes de uma serie historica: tendencia e sazonalidade",
    "anti-padroes na escrita de prompts",
    "regressao polinomial",
    "estacionaridade de uma serie",
    # Consultas de UM termo: o caso de uso do bypass. Nao remova.
    "sazonalidade",
    "ARIMA",
    "benchmarks",
    "prompt injection",
]


def coverage(db: StateDB, note_id: str, terms: list[str]) -> float:
    texts = db.get_note_texts([note_id])
    folded = " " + fold_for_match(texts.get(note_id, "")) + " "
    found = sum(1 for t in terms if " " + t + " " in folded)
    return found / len(terms) if terms else 0.0


def measure(db: StateDB, queries: list[str], max_rank: int) -> list[dict[str, Any]]:
    """Every hit that the rank cutoff alone would let bypass the floor."""
    rows: list[dict[str, Any]] = []
    for q in queries:
        terms = [t for t in (fold_for_match(x).strip() for x in fts_query_terms(q)) if t]
        if not terms:
            continue
        hits = db.search_notes_fts(q, limit=200)
        for rank, hit in enumerate(hits[:max_rank], start=1):
            rows.append(
                {
                    "query": q,
                    "note_id": hit["note_id"],
                    "rank": rank,
                    "pool": len(hits),
                    "n_terms": len(terms),
                    "coverage": round(coverage(db, hit["note_id"], terms), 4),
                }
            )
    return rows


def _stats(rows: list[dict[str, Any]], key: str) -> dict[str, float]:
    vals = sorted(r[key] for r in rows)
    if not vals:
        return {}
    return {"n": len(vals), "min": vals[0], "median": vals[len(vals) // 2], "max": vals[-1]}


def render(off: list[dict], ind: list[dict], current: float, max_rank: int) -> str:
    out: list[str] = []
    out.append(f"bypass atual: rank <= {max_rank} e cobertura >= {current}")
    out.append("")
    out.append("bandas de COBERTURA (hits que o rank sozinho deixaria passar)")
    out.append("  banda              n     min  mediana     max")
    for name, rows in (("fora-dominio", off), ("dentro-dominio", ind)):
        s = _stats(rows, "coverage")
        if s:
            out.append(
                f"  {name:17s} {s['n']:3d}  {s['min']:6.2f}  {s['median']:7.2f} {s['max']:7.2f}"
            )
    pools = _stats(off, "pool")
    if pools:
        out.append("")
        out.append(
            f"  pool de match fora-dominio: min {pools['min']:.0f} | "
            f"mediana {pools['median']:.0f} | max {pools['max']:.0f}"
        )
        if pools["min"] <= max_rank:
            out.append(
                f"  !! pool minimo ({pools['min']:.0f}) <= bm25_bypass_max_rank ({max_rank}): "
                "para essas consultas o rank nao filtra nada -- 'top N' quer dizer 'todos'."
            )

    out.append("")
    out.append("varredura de bm25_bypass_min_coverage")
    out.append("  limiar   fora-dominio bloqueado   dentro-dominio preservado")
    for t in (0.0, 0.25, 0.34, 0.40, 0.50, 0.60, 0.67, 0.75):
        ob = sum(1 for r in off if r["coverage"] < t)
        ik = sum(1 for r in ind if r["coverage"] >= t)
        mark = "  <- atual" if abs(t - current) < 1e-9 else ""
        out.append(
            f"  {t:5.2f}    {ob:3d}/{len(off):<3d} ({100 * ob / max(len(off), 1):5.1f}%)"
            f"        {ik:3d}/{len(ind):<3d} ({100 * ik / max(len(ind), 1):5.1f}%){mark}"
        )

    single = [r for r in ind if r["n_terms"] == 1]
    if single:
        lost = [r for r in single if r["coverage"] < current]
        out.append("")
        out.append(
            f"consultas de um termo so (siglas/jargao): {len(single)} hits, "
            f"{len(lost)} perderiam o bypass no limiar atual"
        )
        if lost:
            out.append(
                "  !! o bypass existe justamente para resgatar esses (ADR-003). "
                "Um limiar que os mata esta errado, por melhor que suprima ruido."
            )
    out.append("")
    out.append(
        "Ressalva: as consultas dentro-do-dominio embutidas descrevem ESTE acervo. "
        "Edite-as (--in-domain-file) antes de tirar conclusoes sobre o seu."
    )
    return "\n".join(out)


def _load(path: Path | None, default: list[str]) -> list[str]:
    if path is None:
        return default
    lines = [x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not lines:
        raise SystemExit(f"nenhuma consulta em {path}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--off-domain-file", type=Path, default=None)
    parser.add_argument("--in-domain-file", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cfg = load_config()
    db = StateDB(cfg.state_db_path)
    if not db.fts_enabled:
        print("SQLite sem FTS5: o bypass lexical nao existe nesta instalacao.")
        return 1
    if db.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0:
        print("Nenhuma nota permanente. Rode o pipeline ate `zettel connect` antes.")
        return 1

    floor = cfg.retrieval.relevance_floor
    off = measure(db, _load(args.off_domain_file, OFF_DOMAIN), floor.bm25_bypass_max_rank)
    ind = measure(db, _load(args.in_domain_file, IN_DOMAIN), floor.bm25_bypass_max_rank)
    print(render(off, ind, floor.bm25_bypass_min_coverage, floor.bm25_bypass_max_rank))

    if args.json:
        args.json.write_text(
            json.dumps({"off_domain": off, "in_domain": ind}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"\nrelatorio JSON: {args.json}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
