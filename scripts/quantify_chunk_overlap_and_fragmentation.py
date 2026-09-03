"""Spike (issue #65), steps 1-2: quantify the overlap-removal case on the real corpus.

Two measurements from data the pipeline already has, no new LLM calls or embeddings:

1. How many rejected chunks are 'fragmented' (needs issue #52's rejection_category,
   persisted going forward but not backfilled onto already-rejected chunks).
2. The real overlap cost: fraction of chunks that share text with their predecessor,
   and the fraction of total corpus characters that duplication accounts for --
   reusing zettel.chunk_dump.overlap_prefix_len, the same diagnostic `dump-chunks` uses.

Step 3 (prototype run with chunk_overlap=0 + neighborhood window) needs live LLM calls
on a real source and is deliberately not run here -- see the issue comment for why.

Usage:
    .venv/Scripts/python.exe scripts/quantify_chunk_overlap_and_fragmentation.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zettel.chunk_dump import overlap_prefix_len  # noqa: E402


def fragmented_rejection_stats(summary_jsons: list[str | None]) -> dict:
    """Pure aggregation over a list of raw `chunks.summary_json` values."""
    rejected = 0
    with_category = 0
    fragmented = 0
    for raw in summary_jsons:
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if data.get("chunk_status") != "rejected":
            continue
        rejected += 1
        cat = data.get("rejection_category")
        if cat:
            with_category += 1
            if cat == "fragmented":
                fragmented += 1
    return {
        "rejected": rejected,
        "with_category": with_category,
        "fragmented": fragmented,
        "fragmented_pct_of_labeled": (100 * fragmented / with_category) if with_category else None,
    }


def overlap_cost_stats(
    chunks_by_chapter: dict[str, list[str]], overlap_cap: int,
) -> dict:
    """Pure aggregation: chapter_id -> ordered list of chunk texts."""
    total_chars = 0
    total_overlap_chars = 0
    chunks_with_overlap = 0
    total_chunks = 0

    for chunk_texts in chunks_by_chapter.values():
        prev_text = ""
        for text in chunk_texts:
            text = text or ""
            total_chunks += 1
            total_chars += len(text)
            if prev_text:
                ov = overlap_prefix_len(prev_text, text, overlap_cap)
                if ov > 0:
                    chunks_with_overlap += 1
                    total_overlap_chars += ov
            prev_text = text

    return {
        "total_chunks": total_chunks,
        "chunks_with_overlap": chunks_with_overlap,
        "chunks_with_overlap_pct": (100 * chunks_with_overlap / total_chunks) if total_chunks else 0.0,
        "total_chars": total_chars,
        "total_overlap_chars": total_overlap_chars,
        "overlap_chars_pct": (100 * total_overlap_chars / total_chars) if total_chars else 0.0,
    }


def _load_summary_jsons(state_db_path: Path) -> list[str | None]:
    conn = sqlite3.connect(str(state_db_path))
    rows = conn.execute("SELECT summary_json FROM chunks").fetchall()
    conn.close()
    return [r[0] for r in rows]


def _load_chunks_by_chapter(state_db_path: Path) -> dict[str, list[str]]:
    conn = sqlite3.connect(str(state_db_path))
    rows = conn.execute(
        "SELECT chapter_id, chunk_index, text FROM chunks ORDER BY chapter_id, chunk_index"
    ).fetchall()
    conn.close()
    by_chapter: dict[str, list[str]] = defaultdict(list)
    for chapter_id, _chunk_index, text in rows:
        by_chapter[chapter_id].append(text)
    return by_chapter


def print_fragmented_rejection_report(state_db_path: Path) -> None:
    stats = fragmented_rejection_stats(_load_summary_jsons(state_db_path))
    print("== Passo 1: chunks rejeitados por 'fragmented' ==")
    print(f"  chunks rejeitados no corpus: {stats['rejected']}")
    print(f"  ...com rejection_category persistida: {stats['with_category']}")
    if stats["with_category"] == 0:
        print(
            "  SEM DADO: nenhum chunk rejeitado neste corpus tem rejection_category "
            "(o corpus local antecede a issue #52 -- backfill era explicitamente fora "
            "de escopo dela). Nao da para concluir a fracao 'fragmented' sem re-extract."
        )
    else:
        print(
            f"  'fragmented' entre os rotulados: {stats['fragmented']} "
            f"({stats['fragmented_pct_of_labeled']:.1f}%)"
        )


def print_overlap_cost_report(state_db_path: Path, overlap_cap: int) -> None:
    stats = overlap_cost_stats(_load_chunks_by_chapter(state_db_path), overlap_cap)
    print("\n== Passo 2: custo real do overlap ==")
    print(f"  total de chunks: {stats['total_chunks']}")
    print(
        f"  chunks com overlap > 0 com o anterior (mesmo capitulo): "
        f"{stats['chunks_with_overlap']} ({stats['chunks_with_overlap_pct']:.1f}%)"
    )
    without = stats["total_chunks"] - stats["chunks_with_overlap"]
    without_pct = 100 - stats["chunks_with_overlap_pct"]
    print(
        f"  chunks SEM overlap com o anterior (secao coube inteira, ou 1o do capitulo): "
        f"{without} ({without_pct:.1f}%)"
    )
    print(
        f"  caracteres duplicados por overlap / caracteres totais do corpus: "
        f"{stats['total_overlap_chars']} / {stats['total_chars']} = "
        f"{stats['overlap_chars_pct']:.1f}%"
    )


def main() -> None:
    from zettel.config import load_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", type=Path, default=Path("data/state.db"))
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    args = parser.parse_args()

    cfg = load_config(args.config)
    print_fragmented_rejection_report(args.state_db)
    print_overlap_cost_report(args.state_db, cfg.chunking.chunk_overlap)


if __name__ == "__main__":
    main()
