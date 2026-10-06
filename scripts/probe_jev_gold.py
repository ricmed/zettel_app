"""Measure the typed decision model (TypeSafe Jev) against the human gold set (#206).

The gold set of #175 labels whether a human would keep a chunk as permanent-note
material. None of the shadow sites (dedupe, cluster category, article judge) has
labels of its own, but all of them lean on that same judgement, in PT-BR. This
script answers the question the pre-registration
(`evals/preregistration/206-jev-camada-decisao.md`) commits to before any call:
does the model separate keep from discard in Portuguese, and how stably?

Two tasks, same model, instructions in English or Portuguese (`--lang`):

* `extract` -- every judged item (keep/discard) of the 120: a `noul` "would a
  curator keep this?" over the passage, and a `choice` among the extract
  categories (`accepted` + the five rejection categories), order-permuted.
* `reader`  -- the 38 judged *accepted* items (the stratum of
  `probe_reader_signal.py`): the same `noul` over passage plus extracted notes,
  and a 1-5 `score` with the reader-judgement scale.

**Where the text comes from.** Not from `data/state.db`: the development vault is
reset at will, and the gold sources are no longer harvested there. The passage is
read from the labelling sheet (`evals/gold/extracao-planilha.csv`, gitignored for
copyright) -- exactly the text the human judged. The reader's notes come from the
recorded Prompt 1 run of the production extractor (`--notes-run`, by default
`.eval-work/prompt1/gemini-t01-a.json` from #181, ADR-050). Both are local files.

The `noul` probability is the signal; its AUC against the human label, with a 95%
interval, is the pre-registered rule. With `--out-key`, the `extract` run also
writes a GABARITO-shaped key (verdict at probability 0.5) so
`scripts/compare_gold_runs.py` can put it next to the production extractor.

**This is measurement, not a gate.** ADR-049 (no pre-LLM gate on extract) stands.

Replay: answers are recorded under `.eval-work/jev-gold/` (gitignored), keyed by
task, language, model, permutations and the question set. A recorded run makes
no call; uncached calls need `--yes`. Nothing is written to state.db or Chroma.

Usage:
    .venv/Scripts/python.exe scripts/probe_jev_gold.py --task extract --lang en
    .venv/Scripts/python.exe scripts/probe_jev_gold.py --task extract --lang en --yes \\
        --out evals/results/jev-gold-extract-en.json \\
        --out-key evals/gold/extracao-GABARITO-jev-en.json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Run from a clone without installing the package (mirrors scripts/ precedent).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from calibrate_review_confidence import auc_ci, load_gold, min_detectable_auc

DEFAULT_RECORD_DIR = Path(".eval-work/jev-gold")
DEFAULT_SHEET = Path("evals/gold/extracao-planilha.csv")
DEFAULT_NOTES_RUN = Path(".eval-work/prompt1/gemini-t01-a.json")
KEEP_THRESHOLD = 0.5
TASKS = ("extract", "reader")
_NOTE_FIELDS = ("thesis", "definition", "intuition", "limits")


# -- Pure pieces ---------------------------------------------------------


def judged_items(labels_payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    """chunk_id -> {item_id, verdict, category}; `?`/unjudgeable excluded."""
    return {
        lab["chunk_id"]: {
            "item_id": lab["item_id"],
            "verdict": lab["human_verdict"],
            "category": lab.get("human_category") or "",
        }
        for lab in labels_payload["labels"]
        if lab["human_verdict"] in ("keep", "discard")
    }


def notes_for_reader(candidates: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The notes extract produced, without empty fields."""
    return [
        {f: str(c.get(f) or "").strip() for f in _NOTE_FIELDS if str(c.get(f) or "").strip()}
        for c in candidates
    ]


def build_decision(task: str, row: dict[str, Any], lang: str) -> Any:
    from zettel.decision import sites

    if task == "extract":
        return sites.extract_gold(source_title=row["source_title"], passage=row["text"], lang=lang)
    return sites.reader_gold(
        source_title=row["source_title"],
        passage=row["text"],
        notes=notes_for_reader(row["candidates"]),
        lang=lang,
    )


def recording_key(task: str, lang: str, model: str, permutations: int, inputs: str) -> str:
    """Identity of a run: the questions do not depend on the item, so one sample fixes them.

    ``inputs`` names where the content came from (the notes run, for the reader), so
    answers over different notes never share a recording.
    """
    from zettel.hashing import sha256_hex

    sample = build_decision(task, {"source_title": "", "text": "", "candidates": []}, lang)
    questions = json.dumps(sample.questions, ensure_ascii=False, sort_keys=True)
    return sha256_hex(f"{task}|{lang}|{model}|{permutations}|{inputs}|{questions}")[:12]


def collect(
    chunk_ids: list[str],
    recorded: dict[str, dict[str, Any]],
    ask: Callable[[str], dict[str, Any]],
    on_record: Callable[[str, dict[str, Any]], None],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Reuse recorded answers; ask only for what is missing. Errors are not recorded."""
    answers: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    for chunk_id in chunk_ids:
        if chunk_id in recorded:
            answers[chunk_id] = recorded[chunk_id]
            continue
        entry = ask(chunk_id)
        if entry.get("error"):
            failures[chunk_id] = entry["error"]
            continue
        answers[chunk_id] = entry
        on_record(chunk_id, entry)
    return answers, failures


def _auc(signal: dict[str, float], gold: dict[str, str]) -> dict[str, Any]:
    keep = [signal[c] for c, v in gold.items() if v == "keep" and c in signal]
    discard = [signal[c] for c, v in gold.items() if v == "discard" and c in signal]
    return {
        **auc_ci(keep, discard),
        "n_keep": len(keep),
        "n_discard": len(discard),
        "mean_keep": round(statistics.fmean(keep), 4) if keep else None,
        "mean_discard": round(statistics.fmean(discard), 4) if discard else None,
        "min_detectable_auc": min_detectable_auc(len(keep), len(discard)),
    }


def score(
    task: str, answers: dict[str, dict[str, Any]], items: dict[str, dict[str, str]]
) -> dict[str, Any]:
    gold = {c: items[c]["verdict"] for c in answers if c in items}
    result: dict[str, Any] = {
        "keep_noul": _auc({c: float(a["keep"]["noul"]) for c, a in answers.items()}, gold)
    }
    if task == "reader":
        result["quality_score"] = _auc(
            {c: float(a["quality"]["score"]) for c, a in answers.items()}, gold
        )
        return result

    category = {c: a["category"] for c, a in answers.items()}
    result["category_spread_mean"] = round(
        statistics.fmean(float(v.get("spread", 0.0)) for v in category.values()), 4
    )
    labelled = [c for c in category if items[c]["verdict"] == "discard" and items[c]["category"]]
    hits = sum(category[c]["choice"] == items[c]["category"] for c in labelled)
    result["category_on_human_discards"] = {
        "n": len(labelled),
        "agreement": round(hits / len(labelled), 4) if labelled else None,
        "confusion": dict(
            sorted(
                Counter(
                    f"{items[c]['category']}->{category[c]['choice']}" for c in labelled
                ).items()
            )
        ),
    }
    return result


def verdict_from(answer: dict[str, Any]) -> tuple[str, str]:
    """GABARITO verdict at the pre-registered 0.5; the category is the best rejection class."""
    if float(answer["keep"]["noul"]) >= KEEP_THRESHOLD:
        return "accepted", ""
    probs = {k: v for k, v in answer["category"]["probabilities"].items() if k != "accepted"}
    return "rejected", max(probs, key=lambda k: probs[k])


def regenerate_key(
    original: dict[str, Any], answers: dict[str, dict[str, Any]], meta: dict[str, Any]
) -> dict[str, Any]:
    """Same items, strata and population as the frozen key; this run's verdicts."""
    items = []
    for entry in original["items"]:
        answer = answers.get(entry["chunk_id"])
        if answer is None:
            continue
        verdict, category = verdict_from(answer)
        items.append({**entry, "llm_verdict": verdict, "llm_category": category})
    return {
        **{k: v for k, v in original.items() if k != "items"},
        "n_items": len(items),
        "items": items,
        "regenerated": meta,
    }


# -- IO ------------------------------------------------------------------


def sheet_rows(sheet: Path) -> dict[str, dict[str, str]]:
    """item_id -> {text, source}: the passage exactly as the labeller read it."""
    from zettel.evals.extraction import decode_sheet

    text, _encoding = decode_sheet(sheet.read_bytes())
    return {
        row["item_id"]: {"text": row["texto"], "source": row["fonte"]}
        for row in csv.DictReader(io.StringIO(text), delimiter=";")
    }


def recorded_notes(notes_run: Path) -> dict[str, list[dict[str, Any]]]:
    """chunk_id -> candidates of a recorded Prompt 1 run (`probe_prompt1_variant.py`)."""
    from zettel.llm import parse_llm_json

    responses = json.loads(notes_run.read_text(encoding="utf-8"))["responses"]
    return {
        chunk_id: (parse_llm_json(entry["response"]) or {}).get("candidates") or []
        for chunk_id, entry in responses.items()
    }


def build_rows(
    items: dict[str, dict[str, str]],
    sheet: dict[str, dict[str, str]],
    notes: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """chunk_id -> the decision input; items missing from the sheet are left out."""
    rows = {}
    for chunk_id, item in items.items():
        row = sheet.get(item["item_id"])
        if row is None:
            continue
        rows[chunk_id] = {
            "text": row["text"],
            "source_title": row["source"],
            "candidates": notes.get(chunk_id, []),
        }
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--task", choices=TASKS, required=True)
    parser.add_argument("--lang", choices=("en", "pt"), required=True)
    parser.add_argument("--sheet", type=Path, default=DEFAULT_SHEET)
    parser.add_argument("--notes-run", type=Path, default=DEFAULT_NOTES_RUN)
    parser.add_argument(
        "--gold-labels", type=Path, default=Path("evals/gold/extracao-rotulos.json")
    )
    parser.add_argument(
        "--gold-key", type=Path, default=Path("evals/gold/extracao-GABARITO-NAO-ABRIR.json")
    )
    parser.add_argument("--record-dir", type=Path, default=DEFAULT_RECORD_DIR)
    parser.add_argument("--yes", action="store_true", help="Autoriza as chamadas nao gravadas")
    parser.add_argument("--out", type=Path, default=None, help="Grava o resultado (sem texto)")
    parser.add_argument("--out-key", type=Path, default=None, help="Gabarito (so --task extract)")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.out_key and args.task != "extract":
        parser.error("--out-key so vale para --task extract")

    from dotenv import load_dotenv

    load_dotenv()
    from zettel.config import load_config
    from zettel.decision import permute
    from zettel.decision.client import get_decision_client

    cfg = load_config()
    dcfg = cfg.decision
    client = get_decision_client(cfg)
    labels = json.loads(args.gold_labels.read_text(encoding="utf-8"))
    items = judged_items(labels)
    if args.task == "reader":
        accepted = load_gold(args.gold_labels, args.gold_key)
        items = {c: v for c, v in items.items() if c in accepted}
    notes = recorded_notes(args.notes_run) if args.task == "reader" else {}
    rows = build_rows(items, sheet_rows(args.sheet), notes)
    chunk_ids = sorted(rows)
    missing_rows = sorted(set(items) - set(rows))
    inputs = args.notes_run.name if args.task == "reader" else "sheet"
    without_notes = sum(1 for r in rows.values() if not r["candidates"])

    key = recording_key(args.task, args.lang, client.model, dcfg.order_permutations, inputs)
    record_path = args.record_dir / f"{args.task}-{args.lang}-{key}.json"
    recorded: dict[str, dict[str, Any]] = {}
    if record_path.exists():
        recorded = json.loads(record_path.read_text(encoding="utf-8"))["answers"]

    to_ask = [c for c in chunk_ids if c not in recorded]
    print(f"modelo: {client.model} | tarefa: {args.task} | idioma: {args.lang}")
    print(f"gravacao: {record_path}")
    print(
        f"itens julgados: {len(items)} | ausentes da planilha: {len(missing_rows)} | "
        f"ja gravados: {len(recorded)}"
    )
    if args.task == "reader":
        print(f"notas: {args.notes_run} | itens sem nota nesta rodada: {without_notes}")
    if to_ask:
        chars = sum(
            len(json.dumps(build_decision(args.task, rows[c], args.lang).state)) for c in to_ask
        )
        tokens = chars // 4
        print(
            f"a chamar: {len(to_ask)} | tokens de estado estimados: {tokens} | custo estimado: "
            f"USD {tokens * dcfg.input_price_per_mtok / 1_000_000:.4f}"
        )
        if not args.yes:
            print("Sem --yes: nenhuma chamada feita.")
            return 1

    def ask(chunk_id: str) -> dict[str, Any]:
        decision = build_decision(args.task, rows[chunk_id], args.lang)
        result = client.ask(
            decision.state,
            permute.expand(decision.questions, dcfg.order_permutations),
            label=f"gold:{args.task}",
        )
        if not result.ok:
            return {"error": result.error}
        return {
            **permute.collapse(decision.questions, result.answers),
            "input_tokens": result.input_tokens,
            "latency_ms": result.latency_ms,
        }

    def on_record(chunk_id: str, entry: dict[str, Any]) -> None:
        recorded[chunk_id] = entry
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(
                {
                    "key": key,
                    "model": client.model,
                    "task": args.task,
                    "lang": args.lang,
                    "permutations": dcfg.order_permutations,
                    "answers": recorded,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    answers, failures = collect(chunk_ids, recorded, ask, on_record)
    result = score(args.task, answers, items)
    latencies = sorted(int(a.get("latency_ms") or 0) for a in answers.values())

    print()
    keep = result["keep_noul"]
    print(
        f"noul guardar: AUC={keep['auc']:.3f} IC95 {keep['ci95']} -> {keep['verdict']} "
        f"(guardar={keep['n_keep']}, descartar={keep['n_discard']}, "
        f"menor AUC detectavel={keep['min_detectable_auc']})"
    )
    print(f"  media guardar={keep['mean_keep']}  descartar={keep['mean_discard']}")
    if "quality_score" in result:
        q = result["quality_score"]
        print(f"score 1-5:    AUC={q['auc']:.3f} IC95 {q['ci95']} -> {q['verdict']}")
    if "category_on_human_discards" in result:
        cat = result["category_on_human_discards"]
        print(f"categoria nos descartes humanos: {cat['agreement']} (n={cat['n']})")
        print(f"  desvio medio entre permutacoes: {result['category_spread_mean']}")
    if latencies:
        print(f"latencia p50={latencies[len(latencies) // 2]} ms  max={latencies[-1]} ms")
    if failures:
        print(f"FALHAS (nao gravadas, repetidas no proximo run): {len(failures)}")
        for chunk_id, err in failures.items():
            print(f"  {chunk_id[-32:]}: {err}")

    item_of = {c: v["item_id"] for c, v in items.items()}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "signal": f"jev_gold_{args.task}",
                    "model": client.model,
                    "lang": args.lang,
                    "permutations": dcfg.order_permutations,
                    "recording_key": key,
                    "n_judged": len(items),
                    "missing_rows": len(missing_rows),
                    "inputs": inputs,
                    "items_without_notes": without_notes if args.task == "reader" else None,
                    "failures": len(failures),
                    "result": result,
                    # Probabilities only, keyed by gold item: no text.
                    "keep_noul": {
                        item_of[c]: round(float(a["keep"]["noul"]), 4)
                        for c, a in sorted(answers.items())
                    },
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    if args.out_key:
        original = json.loads(args.gold_key.read_text(encoding="utf-8"))
        meta = {
            "label": f"jev-{args.lang}",
            "extractor": client.model,
            "keep_threshold": KEEP_THRESHOLD,
            "permutations": dcfg.order_permutations,
            "recording_key": key,
        }
        args.out_key.parent.mkdir(parents=True, exist_ok=True)
        args.out_key.write_text(
            json.dumps(
                regenerate_key(original, answers, meta),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\ngabarito regenerado: {args.out_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
