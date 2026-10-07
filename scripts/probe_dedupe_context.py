"""Does the dedupe LLM agree more with a human when it sees whole notes? (#209)

Until #209 the same-source dedupe prompt (`prompts/dedupe_decision.md`) showed the
LLM the first 200 characters of each existing note -- often not even the whole
thesis. This probe re-asks the production prompt and model over the 64 items a
human labelled in #206 (`evals/gold/dedupe-rotulos.json`), changing one thing:

* `trunc` -- each existing note is the 200-character excerpt the pipeline used
  (stored verbatim in the shadow row's `state_json`);
* `full`  -- each existing note's full content (`extractor.existing_note_contents`,
  the function the pipeline now uses).

The candidate (thesis + definition), the set of existing notes and the prompt are
identical across conditions. Both omit the L2 distance the old prompt printed:
the shadow state never stored it, and showing it in one condition only would
confound the effect. Each condition runs twice (`-a`, `-b`) to show the noise at
`llm.temperature`. The pre-registered rule
(`evals/preregistration/209-dedupe-texto-completo.md`) uses the `-a` runs.

Answers are recorded under `.eval-work/dedupe-context/` (gitignored), keyed by
run, condition, prompt, model and temperature: a recorded run makes no call, and
uncached calls need `--yes`. Reads state.db read-only; writes nothing to it.

Usage:
    .venv/Scripts/python.exe scripts/probe_dedupe_context.py
    .venv/Scripts/python.exe scripts/probe_dedupe_context.py --yes \\
        --out evals/results/dedupe-context-209.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Run from a clone without installing the package (mirrors scripts/ precedent).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from score_decision_gold import key_conditions, score_conditions

RUNS = ("trunc-a", "trunc-b", "full-a", "full-b")
DEFAULT_RECORD_DIR = Path(".eval-work/dedupe-context")
MAX_INVALID_SHARE = 0.05  # pre-registered validity condition


# -- Pure pieces ---------------------------------------------------------


def condition_of(run: str) -> str:
    return run.split("-", 1)[0]


def existing_notes_for(
    condition: str,
    state: dict[str, Any],
    full: Callable[[list[dict[str, Any]]], list[dict[str, str]]],
) -> list[dict[str, str]]:
    """The existing notes as one condition shows them; same ids and titles in both."""
    stored = state["existing_notes"]
    if condition == "trunc":
        return [
            {"id": n["id"], "title": n.get("title") or "", "text": n.get("text") or ""}
            for n in stored
        ]
    hits = [
        {
            "id": n["id"],
            "metadata": {"title": n.get("title") or ""},
            "document": n.get("text") or "",
        }
        for n in stored
    ]
    return full(hits)


def parse_answer(text: str) -> dict[str, Any]:
    """LLM text -> ``{decision, target}`` in the scorer's vocabulary, or ``{error}``."""
    from pydantic import ValidationError
    from zettel.decision.sites import dedupe_baseline_decision
    from zettel.llm import parse_llm_json
    from zettel.schemas import DedupeResult

    try:
        result = DedupeResult(**parse_llm_json(text))
    except (ValueError, ValidationError, TypeError) as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    return {
        "decision": dedupe_baseline_decision(result.decision.value),
        "llm_decision": result.decision.value,
        "target": result.target_note_id or None,
        "reason": result.reason,
    }


def collect(
    item_ids: list[str],
    recorded: dict[str, dict[str, Any]],
    ask: Callable[[str], dict[str, Any]],
    on_record: Callable[[str, dict[str, Any]], None],
) -> dict[str, dict[str, Any]]:
    """Reuse recorded answers; ask only for what is missing. Parse errors are kept
    (they count against validity) but not recorded, so a re-run retries them."""
    answers: dict[str, dict[str, Any]] = {}
    for item_id in item_ids:
        if item_id in recorded:
            answers[item_id] = recorded[item_id]
            continue
        entry = ask(item_id)
        answers[item_id] = entry
        if "error" not in entry:
            on_record(item_id, entry)
    return answers


def summarize_runs(
    labels: list[dict[str, Any]],
    key: dict[str, Any],
    runs: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    valid = {
        run: {i: a for i, a in answers.items() if "error" not in a} for run, answers in runs.items()
    }
    conditions = {**key_conditions(key), **valid}
    pairs = [
        ("trunc-a", "full-a"),
        ("trunc-a", "trunc-b"),
        ("full-a", "full-b"),
        ("llm", "full-a"),
        ("jev", "full-a"),
    ]
    result = score_conditions(labels, conditions, key["population"], pairs)
    n = len(labels)
    result["validity"] = {
        run: {
            "invalid": n - len(valid[run]),
            "invalid_share": round((n - len(valid[run])) / n, 4) if n else None,
        }
        for run in runs
    }
    result["stability"] = {
        cond: _agreement(valid[f"{cond}-a"], valid[f"{cond}-b"]) for cond in ("trunc", "full")
    }
    result["decision_distribution"] = {
        run: dict(sorted(Counter(a["decision"] for a in valid[run].values()).items()))
        for run in runs
    }
    a, b = result["conditions"]["trunc-a"], result["conditions"]["full-a"]
    result["preregistered_rule_209"] = {
        "full_a_correct": b["correct"],
        "trunc_a_correct": a["correct"],
        "non_inferior": b["correct"] >= a["correct"],
        "valid": all(v["invalid_share"] <= MAX_INVALID_SHARE for v in result["validity"].values()),
        "mcnemar_p_trunc_a_vs_full_a": result["pairs"]["trunc-a:full-a"]["mcnemar_p"],
    }
    return result


def _agreement(a: dict[str, dict[str, Any]], b: dict[str, dict[str, Any]]) -> dict[str, Any]:
    common = set(a) & set(b)
    same = sum(a[i]["decision"] == b[i]["decision"] for i in common)
    return {"n": len(common), "same_decision": round(same / len(common), 4) if common else None}


# -- IO ------------------------------------------------------------------


def load_states(state_db: Path, key: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """item_id -> the exact state the shadow stored for that labelled decision."""
    con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        states = {}
        for item in key["items"]:
            row = con.execute(
                "SELECT state_json FROM decision_shadow "
                "WHERE site='dedupe' AND subject_id=? AND state_checksum=?",
                (item["subject_id"], item["state_checksum"]),
            ).fetchone()
            if row:
                states[item["item_id"]] = json.loads(row[0])
        return states
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--state-db", type=Path, default=Path("data/state.db"))
    parser.add_argument("--gold-dir", type=Path, default=Path("evals/gold"))
    parser.add_argument("--record-dir", type=Path, default=DEFAULT_RECORD_DIR)
    parser.add_argument("--yes", action="store_true", help="Autoriza as chamadas nao gravadas")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from dotenv import load_dotenv

    load_dotenv()
    from zettel.config import effective_temperature, llm_phase, load_config
    from zettel.extractor import existing_note_contents, format_existing_notes
    from zettel.hashing import sha256_hex
    from zettel.llm import fill_template, load_prompt_parts
    from zettel.state import StateDB

    cfg = load_config()
    spec = llm_phase(cfg, "review")
    temperature = effective_temperature(cfg, spec)
    parts = load_prompt_parts(cfg.prompts_path / "dedupe_decision.md")
    key = json.loads((args.gold_dir / "dedupe-GABARITO-NAO-ABRIR.json").read_text(encoding="utf-8"))
    labels = json.loads((args.gold_dir / "dedupe-rotulos.json").read_text(encoding="utf-8"))[
        "labels"
    ]
    states = load_states(args.state_db, key)
    item_ids = sorted(i for i in states if any(lab["item_id"] == i for lab in labels))
    print(f"modelo: {spec.provider}/{spec.model} @ {temperature}")
    print(f"itens com estado: {len(item_ids)}/{len(labels)}")

    db = StateDB(args.state_db)

    def full(hits: list[dict[str, Any]]) -> list[dict[str, str]]:
        return existing_note_contents(db, hits, cfg.linking.dedupe_note_chars)

    def user_prompt(run: str, item_id: str) -> tuple[str, str]:
        state = states[item_id]
        mapping = {
            "new_thesis": state["candidate"]["thesis"],
            "new_definition": state["candidate"]["definition"],
            "existing_notes": format_existing_notes(
                existing_notes_for(condition_of(run), state, full)
            ),
        }
        system = fill_template(parts.system, mapping) if parts.system else ""
        return system, fill_template(parts.user_template, mapping)

    # Building the client costs nothing; calling it is what --yes authorises.
    llm = None
    if args.yes:
        from zettel.llm import get_llm

        llm = get_llm(cfg, "review")
    runs: dict[str, dict[str, dict[str, Any]]] = {}
    for run in RUNS:
        run_key = sha256_hex(
            f"{run}|{parts.full_template}|{spec.provider}/{spec.model}@{temperature}"
        )[:12]
        record_path = args.record_dir / f"{run}-{run_key}.json"
        recorded = (
            json.loads(record_path.read_text(encoding="utf-8"))["answers"]
            if record_path.exists()
            else {}
        )
        missing = [i for i in item_ids if i not in recorded]
        chars = sum(len("".join(user_prompt(run, i))) for i in missing)
        print(f"{run}: gravados {len(recorded)} | a chamar {len(missing)} (~{chars // 4} tokens)")
        if missing and not args.yes:
            continue

        def ask(item_id: str, run: str = run) -> dict[str, Any]:
            from zettel.llm import call_llm

            system, user = user_prompt(run, item_id)
            text = call_llm(
                llm,
                user,
                system=system or None,
                provider=spec.provider,
                prompt_cache=cfg.llm.prompt_cache,
                label=f"probe-209:{run}",
            )
            return parse_answer(text)

        def on_record(
            item_id: str, entry: dict[str, Any], path=record_path, rec=recorded, run=run
        ) -> None:
            rec[item_id] = entry
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "run": run,
                        "model": f"{spec.provider}/{spec.model}",
                        "temperature": temperature,
                        "answers": rec,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

        runs[run] = collect(item_ids, recorded, ask, on_record)
    db.close()

    if len(runs) < len(RUNS):
        print("Sem --yes: rodadas incompletas, nada pontuado.")
        return 1

    result = summarize_runs([lab for lab in labels if lab["item_id"] in states], key, runs)
    result["model"] = f"{spec.provider}/{spec.model}"
    result["temperature"] = temperature
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(text, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
