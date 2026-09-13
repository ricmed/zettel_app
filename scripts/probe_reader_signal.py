"""Probe a candidate auto-approval signal: an LLM that judges the note as a reader (#176).

Every signal the pipeline already persists is a coin among accepted chunks (ADR-017,
2026-09-13 addendum). This script tests the first new candidate. A separate call reads
the finished note next to its source passage and scores, 1-5, whether it deserves to be
a permanent note. The hypothesis is about **framing**, not criteria: the extract prompt
already demands conceptual density, atomicity and semantic autonomy -- it even lists
"passages that depend critically on surrounding context" as a reason to reject -- and
still accepts chunks a human would discard. Judging may apply the same criteria better
than extracting does.

**Pre-commitment, and why it matters at this sample size.** The prompt in
`evals/prompts/reader_judgement.md` takes its criteria from `prompts/literature_note.md`,
written before the gold set existed, and not from the labeler's notes. It is run once
over the 38 judged accepted chunks. If it does not separate, the answer is not to edit
the prompt until it does: with 38 items that is fitting the prompt to these labels,
and any revised prompt must be validated on fresh labels.

A signal is adopted only if its 95% lower bound clears 0.5. The sample detects a true
AUC of 0.73 or above with 80% power.

**Cost and replay.** Responses are recorded under `.eval-work/reader-signal/` (gitignored:
the reasons paraphrase source text), keyed by prompt, model, provider and temperature.
A re-run with the same key costs nothing; scoring is offline. Uncached calls require
`--yes` after the estimate is printed. Nothing is written to state.db or Chroma.

Limitation to carry into any conclusion: the `review` phase currently runs the same
model as `extract`. A positive result is a framing effect on one model; a negative one
does not rule out a different judge.

Usage:
    .venv/Scripts/python.exe scripts/probe_reader_signal.py
    .venv/Scripts/python.exe scripts/probe_reader_signal.py --yes
    .venv/Scripts/python.exe scripts/probe_reader_signal.py --out evals/results/reader-signal.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Run from a clone without installing the package (mirrors scripts/ precedent).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from calibrate_review_confidence import auc_ci, load_gold, min_detectable_auc

DEFAULT_PROMPT = Path("evals/prompts/reader_judgement.md")
DEFAULT_RECORD_DIR = Path(".eval-work/reader-signal")
PHASE = "review"
TEMPERATURE = 0.0
OUTPUT_TOKENS_ESTIMATE = 120
SCORE_RANGE = range(1, 6)

_CANDIDATE_FIELDS = (
    ("thesis", "Tese"),
    ("definition", "Definicao"),
    ("intuition", "Intuicao"),
    ("limits", "Limites"),
)


@dataclass(frozen=True)
class ReaderVerdict:
    score: int
    reason: str


# -- Pure pieces ---------------------------------------------------------


def render_candidates(candidates: list[dict[str, Any]]) -> str:
    """The notes extract produced, as the reader sees them. Empty fields are omitted."""
    if not candidates:
        return "(nenhuma nota foi extraida desta passagem)"
    blocks = []
    for i, cand in enumerate(candidates, 1):
        lines = [f"### Nota {i}"]
        for key, label in _CANDIDATE_FIELDS:
            value = str(cand.get(key) or "").strip()
            if value:
                lines.append(f"{label}: {value}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def parse_verdict(text: str) -> ReaderVerdict:
    """Parse the reader's JSON. Raises ``ValueError`` rather than guessing a score."""
    from zettel.llm import extract_json

    try:
        data = json.loads(extract_json(text))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"resposta sem JSON valido: {text[:120]!r}") from exc
    raw = data.get("score") if isinstance(data, dict) else None
    try:
        score = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"score ausente ou nao numerico: {raw!r}") from exc
    if score not in SCORE_RANGE:
        raise ValueError(f"score fora de 1-5: {score}")
    return ReaderVerdict(score=score, reason=str(data.get("reason") or "").strip())


def recording_key(template: str, model: str, provider: str, temperature: float) -> str:
    """Identity of a recording: change the prompt or the judge and it is a different run."""
    from zettel.hashing import sha256_hex

    return sha256_hex(f"{template}\n--\n{provider}/{model}@{temperature}")[:12]


def collect_verdicts(
    chunk_ids: list[str],
    recorded: dict[str, dict[str, Any]],
    call: Callable[[str], str],
    on_record: Callable[[str, ReaderVerdict], None],
) -> tuple[dict[str, ReaderVerdict], dict[str, str]]:
    """Reuse recorded verdicts; call the judge only for what is missing.

    ``call`` is injected so the replay path can be tested with a judge that must never
    be invoked. A parse failure is reported per chunk and not recorded, so a re-run
    retries it instead of silently scoring it.
    """
    verdicts: dict[str, ReaderVerdict] = {}
    failures: dict[str, str] = {}
    for chunk_id in chunk_ids:
        if chunk_id in recorded:
            entry = recorded[chunk_id]
            verdicts[chunk_id] = ReaderVerdict(int(entry["score"]), entry.get("reason", ""))
            continue
        try:
            verdict = parse_verdict(call(chunk_id))
        except ValueError as exc:
            failures[chunk_id] = str(exc)
            continue
        verdicts[chunk_id] = verdict
        on_record(chunk_id, verdict)
    return verdicts, failures


def score_signal(verdicts: dict[str, ReaderVerdict], gold: dict[str, str]) -> dict[str, Any]:
    keep = [verdicts[c].score for c, v in gold.items() if v == "keep" and c in verdicts]
    discard = [verdicts[c].score for c, v in gold.items() if v == "discard" and c in verdicts]
    return {
        **auc_ci(keep, discard),
        "n_keep": len(keep),
        "n_discard": len(discard),
        "mean_keep": round(statistics.mean(keep), 3) if keep else None,
        "mean_discard": round(statistics.mean(discard), 3) if discard else None,
        "distribution_keep": {s: keep.count(s) for s in SCORE_RANGE},
        "distribution_discard": {s: discard.count(s) for s in SCORE_RANGE},
        "min_detectable_auc": min_detectable_auc(len(keep), len(discard)),
    }


# -- IO ------------------------------------------------------------------


def load_rows(state_db: Path, chunk_ids: list[str]) -> dict[str, dict[str, Any]]:
    con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = {}
        for chunk_id in chunk_ids:
            r = con.execute(
                "SELECT c.text, c.summary_json, s.title FROM chunks c "
                "LEFT JOIN sources s ON s.source_id = c.source_id WHERE c.chunk_id = ?",
                (chunk_id,),
            ).fetchone()
            if r is None:
                continue
            summary = json.loads(r["summary_json"]) if r["summary_json"] else {}
            rows[chunk_id] = {
                "chunk_text": r["text"] or "",
                "source_title": r["title"] or "",
                "candidates": summary.get("candidates") or [],
            }
        return rows
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--state-db", type=Path, default=Path("data/state.db"))
    parser.add_argument(
        "--gold-labels", type=Path, default=Path("evals/gold/extracao-rotulos.json")
    )
    parser.add_argument(
        "--gold-key", type=Path, default=Path("evals/gold/extracao-GABARITO-NAO-ABRIR.json")
    )
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--record-dir", type=Path, default=DEFAULT_RECORD_DIR)
    parser.add_argument("--yes", action="store_true", help="Autoriza as chamadas nao gravadas")
    parser.add_argument("--out", type=Path, default=None, help="Grava o resultado (sem texto)")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from zettel.config import llm_phase, load_config
    from zettel.llm import fill_template, load_prompt_parts
    from zettel.preflight import estimate_tokens
    from zettel.pricing import estimate_llm_cost

    cfg = load_config()
    spec = llm_phase(cfg, PHASE)
    parts = load_prompt_parts(args.prompt)
    gold = load_gold(args.gold_labels, args.gold_key)
    rows = load_rows(args.state_db, sorted(gold))
    chunk_ids = sorted(rows)

    key = recording_key(parts.full_template, spec.model, spec.provider, TEMPERATURE)
    record_path = args.record_dir / f"{key}.json"
    recorded: dict[str, dict[str, Any]] = {}
    if record_path.exists():
        recorded = json.loads(record_path.read_text(encoding="utf-8"))["responses"]

    def payload(chunk_id: str) -> str:
        row = rows[chunk_id]
        return fill_template(
            parts.user_template,
            {**row, "candidates": render_candidates(row["candidates"])},
        )

    missing = [c for c in chunk_ids if c not in recorded]
    print(f"juiz: {spec.provider}/{spec.model} @ temperatura {TEMPERATURE} (fase {PHASE})")
    print(f"gravacao: {record_path}")
    print(f"itens do gold (aceitos julgados): {len(chunk_ids)} | ja gravados: {len(recorded)}")

    if missing:
        tokens_in = sum(
            estimate_tokens(parts.system) + estimate_tokens(payload(c)) for c in missing
        )
        tokens_out = OUTPUT_TOKENS_ESTIMATE * len(missing)
        usd = estimate_llm_cost(spec.model, tokens_in, tokens_out, provider=spec.provider)
        print(
            f"a chamar: {len(missing)} | tokens estimados: {tokens_in} entrada + "
            f"{tokens_out} saida | custo estimado: USD {usd:.4f}"
        )
        if not args.yes:
            print("Sem --yes: nenhuma chamada feita.")
            return 1

    from zettel.llm import call_llm, get_llm

    llm = get_llm(cfg, PHASE, temperature=TEMPERATURE) if missing else None

    def call(chunk_id: str) -> str:
        return call_llm(
            llm,
            user=payload(chunk_id),
            system=parts.system,
            label=f"reader:{chunk_id}",
            model=spec.model,
            provider=spec.provider,
        )

    def on_record(chunk_id: str, verdict: ReaderVerdict) -> None:
        recorded[chunk_id] = {"score": verdict.score, "reason": verdict.reason}
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(
                {
                    "key": key,
                    "model": spec.model,
                    "provider": spec.provider,
                    "temperature": TEMPERATURE,
                    "prompt": str(args.prompt),
                    "responses": recorded,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    verdicts, failures = collect_verdicts(chunk_ids, recorded, call, on_record)
    result = score_signal(verdicts, gold)

    print()
    print("sinal: LLM como leitor (1-5), estrato de aceitos")
    print(
        f"  AUC={result['auc']:.3f}  IC95 {result['ci95']}  -> {result['verdict']}"
        f"  (n guardar={result['n_keep']}, descartar={result['n_discard']})"
    )
    print(f"  media guardar={result['mean_keep']}  descartar={result['mean_discard']}")
    print(f"  distribuicao guardar:   {result['distribution_keep']}")
    print(f"  distribuicao descartar: {result['distribution_discard']}")
    print(f"  menor AUC detectavel com esta amostra (80% poder): {result['min_detectable_auc']}")
    if failures:
        print(f"  FALHAS DE PARSE (nao gravadas, repetidas no proximo run): {len(failures)}")
        for chunk_id, err in failures.items():
            print(f"    {chunk_id[-30:]}: {err}")

    if args.out:
        key_items = json.loads(args.gold_key.read_text(encoding="utf-8"))["items"]
        item_of = {e["chunk_id"]: e["item_id"] for e in key_items}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "signal": "llm_reader_judgement",
                    "judge": f"{spec.provider}/{spec.model}",
                    "temperature": TEMPERATURE,
                    "recording_key": key,
                    "prompt": str(args.prompt),
                    "result": result,
                    "failures": len(failures),
                    # Scores only, keyed by gold item: no chunk text and no reasons.
                    "scores": {item_of[c]: v.score for c, v in sorted(verdicts.items())},
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
