"""Probe harness: measure the similarity distribution `min_vector_similarity` sits in.

`retrieval.relevance_floor.min_vector_similarity` is a single scalar that decides
what counts as evidence for `ask`, `connect` and `sync`. Its value is only
meaningful relative to the similarity distribution of the *current* embedding
model over the *current* corpus — swap either and the number silently means
something else. ADR-003 records that the original 0.70/0.15 pair has no
calibration record; this script is how that stops being true.

It measures three bands:

  * **off-domain** — hardcoded, domain-neutral queries (cooking, car maintenance,
    health). Every one of these MUST fail the floor. Their maximum is the floor's
    lower bound.
  * **self-match** — permanent-note titles used as their own query. The easiest
    possible retrieval; their minimum is the floor's upper bound.
  * **margin** — the gap between the two. A narrow margin means no scalar
    threshold separates relevant from irrelevant, which is an argument for a
    reranking stage rather than for more threshold tuning (see ADR-044 draft).

It also verifies that stored and query vectors are unit-normalised, because the
floor's `1 - distance / 2` conversion is a valid cosine similarity ONLY under
that assumption (collections are created without an explicit `hnsw:space`, so
Chroma's default squared-L2 applies). A provider that does not normalise would
make every similarity in the system — and this threshold — meaningless.

Cost: **no LLM calls.** It does embed each probe query (local Ollama by default),
which is the only way to obtain a real distance.

`--collection chapter_summaries` probes `retrieval.chapter_floor` instead
(ADR-047). It is a separate threshold on purpose: a chapter summary is longer and
more diffuse than a note, so it scores lower against the same query, and the note
floor would over-reject. Self-match there uses chapter titles.

Usage:
    .venv/Scripts/python.exe scripts/probe_relevance_floor.py
    .venv/Scripts/python.exe scripts/probe_relevance_floor.py --samples 20
    .venv/Scripts/python.exe scripts/probe_relevance_floor.py --off-domain-file mine.txt
    .venv/Scripts/python.exe scripts/probe_relevance_floor.py --json out.json
    .venv/Scripts/python.exe scripts/probe_relevance_floor.py --collection chapter_summaries

Caveat the operator must keep in mind: a probe is only as wide as its corpus. The
script prints how many notes it measured and refuses to suggest a threshold from
a corpus too small to mean anything. A single-domain vault compresses the whole
similarity range and inflates every number below.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Run from a clone without installing the package (mirrors scripts/ precedent).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zettel.config import load_config
from zettel.index import VectorIndex, index_kwargs
from zettel.state import StateDB

# Domain-neutral by construction: a Zettelkasten about cooking, car maintenance
# or clinical medicine would need --off-domain-file, and the script says so.
OFF_DOMAIN_QUERIES = [
    "receita de bolo de cenoura com cobertura de chocolate",
    "como trocar o oleo do motor de um carro flex",
    "sintomas de deficiencia de vitamina D em idosos",
    "escalacao do time para a final do campeonato brasileiro",
    "previsao do tempo para o fim de semana no litoral norte",
    "como podar uma roseira no inverno",
]

# Below this the bands are noise, not a distribution.
MIN_NOTES_FOR_SUGGESTION = 30


def cosine_from_distance(distance: float) -> float:
    """Chroma default space is squared-L2; on unit vectors that is 2 - 2*cos.

    Mirrors `retrieval._apply_relevance_floor` exactly. Valid only when the
    vectors are normalised, which `check_normalisation` is what verifies.
    """
    return 1.0 - distance / 2.0


def l2_norm(vector: list[float]) -> float:
    return math.sqrt(sum(float(x) * float(x) for x in vector))


@dataclass
class Band:
    """One class of probe query and the top-1 similarities it produced."""

    name: str
    similarities: list[float] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    @property
    def lo(self) -> float:
        return min(self.similarities) if self.similarities else float("nan")

    @property
    def hi(self) -> float:
        return max(self.similarities) if self.similarities else float("nan")

    @property
    def mid(self) -> float:
        if not self.similarities:
            return float("nan")
        ordered = sorted(self.similarities)
        n = len(ordered)
        return ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2


@dataclass
class NormalisationCheck:
    query_dim: int
    query_norm: float
    stored: dict[str, float]  # collection -> mean norm of the sample

    @property
    def ok(self) -> bool:
        """True when every measured norm is 1.0 within float tolerance."""
        norms = [self.query_norm, *self.stored.values()]
        return all(abs(n - 1.0) < 1e-3 for n in norms)


def check_normalisation(idx: VectorIndex, probe: str) -> NormalisationCheck:
    query_vec = idx.embedding_fn([probe])[0]
    stored: dict[str, float] = {}
    for name, col in _collections(idx):
        try:
            if not col.count():
                continue
            got = col.get(limit=5, include=["embeddings"])
        except Exception as e:  # pragma: no cover - defensive around Chroma
            print(f"  aviso: nao foi possivel ler embeddings de '{name}': {e}")
            continue
        embeddings = got.get("embeddings")
        if embeddings is None or len(embeddings) == 0:
            continue
        norms = [l2_norm(e) for e in embeddings]
        stored[name] = sum(norms) / len(norms)
    return NormalisationCheck(
        query_dim=len(query_vec),
        query_norm=l2_norm(query_vec),
        stored=stored,
    )


def _collections(idx: VectorIndex) -> list[tuple[str, Any]]:
    return [
        ("permanent_notes", idx.permanent),
        ("chunks", idx.chunks),
        ("sources", idx.sources),
        ("chapter_summaries", idx.chapter_summaries),
    ]


def target_collection(idx: VectorIndex, name: str) -> Any:
    return idx.chapter_summaries if name == "chapter_summaries" else idx.permanent


def top1_similarity(idx: VectorIndex, query: str, collection: str) -> float | None:
    """Top-1 cosine similarity of ``query`` against the probed collection."""
    res = target_collection(idx, collection).query(query_texts=[query], n_results=1)
    distances = res.get("distances") or [[]]
    if not distances[0]:
        return None
    return cosine_from_distance(distances[0][0])


def load_note_titles(db: StateDB, limit: int) -> list[str]:
    rows = db.conn.execute(
        "SELECT title FROM notes WHERE title IS NOT NULL AND title != '' ORDER BY note_id LIMIT ?",
        (limit,),
    ).fetchall()
    return [(r["title"] if not isinstance(r, tuple) else r[0]) for r in rows]


def load_chapter_titles(db: StateDB, limit: int) -> list[str]:
    """Self-match queries for the chapter probe: titles of summarized chapters."""
    rows = db.conn.execute(
        "SELECT title FROM chapters "
        "WHERE summary IS NOT NULL AND summary != '' AND title IS NOT NULL AND title != '' "
        "ORDER BY chapter_id LIMIT ?",
        (limit,),
    ).fetchall()
    return [(r["title"] if not isinstance(r, tuple) else r[0]) for r in rows]


def measure(idx: VectorIndex, name: str, queries: list[str], collection: str) -> Band:
    band = Band(name=name)
    for q in queries:
        sim = top1_similarity(idx, q, collection)
        if sim is None:
            continue
        band.similarities.append(sim)
        band.queries.append(q)
    return band


def build_report(
    cfg: Any,
    note_count: int,
    norm: NormalisationCheck,
    off: Band,
    self_match: Band,
    collection: str = "permanent_notes",
) -> dict[str, Any]:
    floor_cfg = (
        cfg.retrieval.chapter_floor
        if collection == "chapter_summaries"
        else cfg.retrieval.relevance_floor
    )
    current = floor_cfg.min_vector_similarity
    margin = self_match.lo - off.hi if off.similarities and self_match.similarities else None
    suggestion: float | None = None
    if margin is not None and margin > 0 and note_count >= MIN_NOTES_FOR_SUGGESTION:
        # Midpoint of the separating gap. This is an UPPER BOUND, not a
        # recommendation: self-match is the easiest retrieval there is, so its
        # lower edge overstates what a real question scores. The floor belongs
        # near `off.hi`, not here. Rounded to the 0.01 the config is written in.
        suggestion = round((off.hi + self_match.lo) / 2, 2)
    return {
        "embedding": {
            "provider": cfg.embedding.provider,
            "model": cfg.embedding.model,
            "dimensions": cfg.embedding.dimensions,
        },
        "collection": collection,
        "floor_key": (
            "retrieval.chapter_floor"
            if collection == "chapter_summaries"
            else "retrieval.relevance_floor"
        ),
        "corpus": {"permanent_notes": note_count},
        "normalisation": {
            "ok": norm.ok,
            "query_dim": norm.query_dim,
            "query_norm": round(norm.query_norm, 6),
            "stored": {k: round(v, 6) for k, v in norm.stored.items()},
        },
        "bands": {
            b.name: {
                "n": len(b.similarities),
                "lo": round(b.lo, 4),
                "mid": round(b.mid, 4),
                "hi": round(b.hi, 4),
            }
            for b in (off, self_match)
            if b.similarities
        },
        "margin": round(margin, 4) if margin is not None else None,
        "current_min_vector_similarity": current,
        "upper_bound_min_vector_similarity": suggestion,
    }


def render(report: dict[str, Any], off: Band, self_match: Band) -> str:
    out: list[str] = []
    emb = report["embedding"]
    dims = emb["dimensions"] if emb["dimensions"] is not None else "nativo"
    out.append(f"embedding: {emb['provider']}/{emb['model']} @ {dims}d")
    out.append(f"corpus:    {report['corpus']['permanent_notes']} notas permanentes")
    out.append("")

    norm = report["normalisation"]
    verdict = "OK" if norm["ok"] else "FALHA"
    out.append(f"[{verdict}] normalizacao (1 - d/2 e cosseno valido apenas se norma == 1)")
    out.append(f"  query ({norm['query_dim']}d): {norm['query_norm']}")
    for name, value in sorted(norm["stored"].items()):
        out.append(f"  {name}: {value}")
    if not norm["ok"]:
        out.append("  !! vetores NAO normalizados: toda similaridade abaixo e invalida,")
        out.append("     e o piso esta medindo uma grandeza que nao e cosseno.")
    out.append("")

    out.append("bandas (similaridade top-1)")
    out.append(f"  {'banda':<14} {'n':>3} {'min':>7} {'mediana':>8} {'max':>7}")
    for band in (off, self_match):
        if not band.similarities:
            continue
        out.append(
            f"  {band.name:<14} {len(band.similarities):>3} "
            f"{band.lo:>7.3f} {band.mid:>8.3f} {band.hi:>7.3f}"
        )
    out.append("")

    out.append("detalhe fora-do-dominio (todas DEVEM ficar abaixo do piso)")
    for sim, q in sorted(zip(off.similarities, off.queries, strict=True), reverse=True):
        out.append(f"  {sim:.3f}  {q[:64]}")
    out.append("")

    current = report["current_min_vector_similarity"]
    margin = report["margin"]
    suggested = report["upper_bound_min_vector_similarity"]

    if margin is None:
        out.append("Sem bandas suficientes para avaliar o piso.")
        return "\n".join(out)

    out.append(f"piso atual:  {current}")
    out.append(f"margem:      {margin:.4f}  (min self-match - max fora-do-dominio)")
    if margin <= 0:
        out.append("  !! bandas SOBREPOSTAS: nenhum limiar escalar separa relevante de")
        out.append("     irrelevante neste corpus. Nao ajuste o piso — o problema nao e")
        out.append("     o valor dele. Ver o rascunho do ADR-044 (reranking cross-encoder).")
    elif suggested is None:
        out.append(
            f"  corpus abaixo de {MIN_NOTES_FOR_SUGGESTION} notas: bandas medidas, "
            f"sugestao omitida de proposito."
        )
    else:
        out.append(f"teto util:   {suggested}  (ponto medio da faixa de separacao)")
        out.append("  Leia como TETO, nao como recomendacao. Self-match (titulo como sua")
        out.append("  propria consulta) e o caso mais facil que existe e superestima o que")
        out.append("  uma pergunta real marca: uma pergunta do dominio bem formulada cai")
        out.append("  bem abaixo dessa banda. Subir o piso ate aqui custaria recall real.")
        out.append(
            f"  O piso pertence a faixa ({off.hi:.3f}, {suggested}], perto do limite inferior."
        )
        leaks = [s for s in off.similarities if s >= current]
        if leaks:
            out.append(
                f"  !! {len(leaks)} consulta(s) fora-do-dominio passam no piso atual "
                f"(max {max(leaks):.3f} >= {current})."
            )
        losses = [s for s in self_match.similarities if s < current]
        if losses:
            out.append(
                f"  !! {len(losses)} self-match reprovam no piso atual "
                f"(min {min(losses):.3f} < {current})."
            )
        if not leaks and not losses:
            out.append("  piso atual separa as duas bandas corretamente.")

    out.append("")
    out.append(
        f"Ressalva: {report['corpus']['permanent_notes']} notas medidas. Um vault de "
        f"dominio unico comprime a faixa e infla todos os numeros acima. Isto e um "
        f"sinal que motiva medicao, nao uma calibracao."
    )
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--samples",
        type=int,
        default=12,
        help="Quantos titulos de nota usar como self-match (default: 12)",
    )
    parser.add_argument(
        "--off-domain-file",
        type=Path,
        default=None,
        help="Arquivo com uma consulta fora-do-dominio por linha (substitui as embutidas)",
    )
    parser.add_argument(
        "--collection",
        choices=("permanent_notes", "chapter_summaries"),
        default="permanent_notes",
        help=(
            "Colecao a sondar. chapter_summaries mede retrieval.chapter_floor "
            "(ADR-047), um limiar separado por medir outra distribuicao de texto."
        ),
    )
    parser.add_argument("--json", type=Path, default=None, help="Grava o relatorio como JSON")
    args = parser.parse_args()

    # Note titles carry accents; the Windows console defaults to cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cfg = load_config()
    db = StateDB(cfg.state_db_path)
    idx = VectorIndex(**index_kwargs(cfg))

    if args.collection == "chapter_summaries":
        titles = load_chapter_titles(db, args.samples)
        if not titles:
            print("Nenhum capitulo resumido. Rode `zettel summarize` antes.")
            return 1
    else:
        titles = load_note_titles(db, args.samples)
        if not titles:
            print("Nenhuma nota permanente. Rode o pipeline ate `zettel connect` antes.")
            return 1

    if args.off_domain_file:
        off_queries = [
            line.strip()
            for line in args.off_domain_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not off_queries:
            parser.error(f"nenhuma consulta em {args.off_domain_file}")
    else:
        off_queries = OFF_DOMAIN_QUERIES
        print(
            "Usando consultas fora-do-dominio embutidas (culinaria, carro, saude, "
            "esporte, clima, jardinagem).\nSe o vault tratar desses temas, passe "
            "--off-domain-file com consultas realmente alheias a ele.\n"
        )

    if args.collection == "chapter_summaries":
        note_count = db.conn.execute(
            "SELECT COUNT(*) FROM chapters WHERE summary IS NOT NULL AND summary != ''"
        ).fetchone()[0]
    else:
        note_count = db.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    norm = check_normalisation(idx, off_queries[0])
    off = measure(idx, "fora-dominio", off_queries, args.collection)
    self_match = measure(idx, "self-match", titles, args.collection)

    report = build_report(cfg, note_count, norm, off, self_match, args.collection)
    print(render(report, off, self_match))

    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nrelatorio JSON: {args.json}")

    # Fail loudly when the assumption the whole conversion rests on is broken.
    return 0 if norm.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
