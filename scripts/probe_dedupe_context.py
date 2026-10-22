"""Re-ask the dedupe LLM over labelled items: what changes its agreement with a human? (#209, #218)

The same-source dedupe prompt (`prompts/dedupe_decision.md`) is re-asked, with the
production model, over items a human labelled blind (`--labels`, default the round-1
sheet of #206 `evals/gold/dedupe-rotulos.json`; `--key` names that round's frozen key).
Two things can vary:

* **context** -- `trunc`: each existing note is the 200-character excerpt the
  pipeline used before #209 (stored verbatim in the shadow row's `state_json`);
  `full`: each existing note's full content (`extractor.existing_note_contents`, what
  the pipeline uses now). #209 measured this axis.
* **prompt** -- `--prompt NAME=PATH`, repeatable (default: `current=` the production
  prompt). #218 compares the current prompt against a candidate rewrite on the same
  items, under both contexts.

The candidate (thesis + definition) and the set of existing notes are identical
across runs. No run shows the L2 distance the pre-#209 prompt printed: the shadow
state never stored it, and showing it in one run only would confound the effect.
Each (prompt, context) runs twice (`-a`, `-b`) to show the noise at `llm.temperature`;
pre-registered rules use the `-a` runs (`evals/preregistration/209-...`, `218-...`).

Answers are recorded under `.eval-work/dedupe-context/` (gitignored), keyed by run,
prompt text, model and temperature: a recorded run makes no call (so an old prompt
saved to a file replays for free), and uncached calls need `--yes`. Reads state.db
read-only; writes nothing to it.

Usage:
    .venv/Scripts/python.exe scripts/probe_dedupe_context.py --yes \\
        --out evals/results/dedupe-context-209.json
    git show main:prompts/dedupe_decision.md > .eval-work/prompts/dedupe-current.md
    .venv/Scripts/python.exe scripts/probe_dedupe_context.py --yes \\
        --prompt current=.eval-work/prompts/dedupe-current.md \\
        --prompt new=prompts/dedupe_decision.md --out evals/results/dedupe-prompt-218.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable
from itertools import combinations
from pathlib import Path
from typing import Any

# Run from a clone without installing the package (mirrors scripts/ precedent).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from score_decision_gold import key_conditions, score_conditions

RUNS = ("trunc-a", "trunc-b", "full-a", "full-b")
DEFAULT_RECORD_DIR = Path(".eval-work/dedupe-context")
MAX_INVALID_SHARE = 0.05  # pre-registered validity condition (#209, #218)


# -- Pure pieces ---------------------------------------------------------


def condition_of(run: str) -> str:
    """``trunc-a`` / ``new:full-b`` -> ``trunc`` / ``full``."""
    return run.rsplit(":", 1)[-1].split("-", 1)[0]


def run_ids(prompts: list[str], runs: tuple[str, ...] = RUNS) -> list[str]:
    """Result names: the bare run for one prompt (as #209 recorded it), else ``prompt:run``."""
    if len(prompts) == 1:
        return list(runs)
    return [f"{p}:{r}" for p in prompts for r in runs]


def runs_for_context(context: str) -> tuple[str, ...]:
    """``full`` is the production context (#273). ``both`` keeps trunc and full (#209)."""
    if context == "full":
        return ("full-a", "full-b")
    if context == "both":
        return RUNS
    raise ValueError(f"contexto desconhecido: {context}")


def parse_prompt_args(values: list[str]) -> list[tuple[str, Path]]:
    """``NAME=PATH`` pairs, names unique and free of ``:``."""
    parsed: list[tuple[str, Path]] = []
    for value in values:
        name, sep, path = value.partition("=")
        if not sep or not name or not path or ":" in name:
            raise ValueError(f"--prompt espera NOME=CAMINHO, recebeu {value!r}")
        parsed.append((name, Path(path)))
    names = [n for n, _ in parsed]
    if len(set(names)) != len(names):
        raise ValueError(f"nomes de --prompt repetidos: {names}")
    return parsed


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


def parse_answer(text: str, compared_ids: set[str] | None = None) -> dict[str, Any]:
    """LLM text -> ``{decision, target}`` in the scorer's vocabulary, or ``{error}``."""
    from pydantic import ValidationError
    from zettel.decision.sites import dedupe_baseline_decision
    from zettel.llm import parse_llm_json
    from zettel.schemas import DedupeResult, checked_dedupe_result

    try:
        data = parse_llm_json(text)
        if compared_ids is None:
            result = DedupeResult(**data)
        else:
            result = checked_dedupe_result(data, compared_ids)
    except (ValueError, ValidationError, TypeError) as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    return {
        "decision": dedupe_baseline_decision(result.decision.value),
        "llm_decision": result.decision.value,
        "target": result.target_note_id or None,
        "reason": result.reason,
    }


def decision_id(item: dict[str, Any]) -> str:
    """The recording key of one labelled decision.

    Sheet ids (`D001`...) restart every labelling round, so keying recordings by
    them would replay a round-1 answer for a different round-2 item. The shadow
    row's subject and state checksum identify the decision itself.
    """
    return f"{item['subject_id']}|{item['state_checksum']}"


def collect(
    item_ids: list[str],
    recorded: dict[str, dict[str, Any]],
    ask: Callable[[str], dict[str, Any]],
    on_record: Callable[[str, dict[str, Any]], None],
    record_key: Callable[[str], str] = lambda item_id: item_id,
) -> dict[str, dict[str, Any]]:
    """Reuse recorded answers; ask only for what is missing. Parse errors are kept
    (they count against validity) but not recorded, so a re-run retries them.

    Answers are returned by item id; ``record_key`` maps an item to the key it is
    recorded under (``decision_id`` in the probe)."""
    answers: dict[str, dict[str, Any]] = {}
    for item_id in item_ids:
        if record_key(item_id) in recorded:
            answers[item_id] = recorded[record_key(item_id)]
            continue
        entry = ask(item_id)
        answers[item_id] = entry
        if "error" not in entry:
            on_record(record_key(item_id), entry)
    return answers


def _pairs(prompts: list[str], present: set[str] | None = None) -> list[tuple[str, str]]:
    """Every comparison a rule reads: context within a prompt, prompt within a
    context, noise within a run, and the recorded shadow against each full run."""

    def rid(p: str, run: str) -> str:
        return run if len(prompts) == 1 else f"{p}:{run}"

    pairs: list[tuple[str, str]] = []
    for p in prompts:
        pairs += [
            (rid(p, "trunc-a"), rid(p, "full-a")),
            (rid(p, "trunc-a"), rid(p, "trunc-b")),
            (rid(p, "full-a"), rid(p, "full-b")),
            ("llm", rid(p, "full-a")),
            ("jev", rid(p, "full-a")),
        ]
    for a, b in combinations(prompts, 2):
        pairs += [(rid(a, "trunc-a"), rid(b, "trunc-a")), (rid(a, "full-a"), rid(b, "full-a"))]
        pairs.append((rid(a, "trunc-a"), rid(b, "full-a")))
    if present is None:
        return pairs
    return [(a, b) for a, b in pairs if a in present and b in present]


def ignore_recall(
    labels: list[dict[str, Any]], answers: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Share of human ``ignore`` items the model also called ``ignore``. Reported, not a gate."""
    gold = [lab["item_id"] for lab in labels if lab.get("human_decision") == "ignore"]
    recalled = sum(
        1 for item_id in gold if answers.get(item_id, {}).get("llm_decision") == "ignore"
    )
    return {
        "n_ignore": len(gold),
        "recalled": recalled,
        "recall": round(recalled / len(gold), 4) if gold else None,
    }


def model_comparison_rule(
    mini: dict[str, Any],
    luna: dict[str, Any],
    *,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The two gates in ``evals/preregistration/273-modelo-dedupe.md``.

    ``mini`` and ``luna`` are probe result dicts for the current prompt.
    ``contract`` is the luna run of the new prompt, or None when that
    condition was not executed.
    """
    from zettel.evals.stats import paired_exact_p, paired_mid_p

    def correct(result: dict[str, Any]) -> int:
        return int(result["conditions"]["full-a"]["correct"])

    def recall_of(result: dict[str, Any]) -> Any:
        return result.get("ignore_recall", {}).get("full-a", {}).get("recall")

    def invalid(result: dict[str, Any], which: str) -> float:
        return float(result["validity"][which]["invalid_share"])

    only_mini, only_luna = _discordant(mini, luna, "full-a")
    p_value = paired_exact_p(only_mini, only_luna)
    luna_correct = correct(luna)
    mini_correct = correct(mini)
    valid = invalid(luna, "full-a") <= MAX_INVALID_SHARE and invalid(luna, "full-b") <= 0.05
    beats = luna_correct > mini_correct and p_value < 0.05
    out: dict[str, Any] = {
        "adopt_luna": bool(beats and valid),
        "luna_correct": luna_correct,
        "mini_correct": mini_correct,
        "only_luna_right": only_luna,
        "only_mini_right": only_mini,
        "mcnemar_p": round(p_value, 4),
        "mcnemar_mid_p": round(paired_mid_p(only_mini, only_luna), 4),
        "valid": valid,
        "mini_ignore_recall": recall_of(mini),
        "luna_ignore_recall": recall_of(luna),
        "adopt_contract": False,
    }
    if contract is None:
        return out
    _only_current, only_contract = _discordant(luna, contract, "full-a")
    contract_p = paired_exact_p(_only_current, only_contract)
    contract_correct = correct(contract)
    contract_valid = (
        invalid(contract, "full-a") <= MAX_INVALID_SHARE
        and invalid(contract, "full-b") <= MAX_INVALID_SHARE
    )
    out["adopt_contract"] = bool(
        contract_correct > luna_correct and contract_p < 0.05 and contract_valid
    )
    out["contract_correct"] = contract_correct
    out["contract_mcnemar_p"] = round(contract_p, 4)
    out["contract_mcnemar_mid_p"] = round(paired_mid_p(_only_current, only_contract), 4)
    out["contract_valid"] = contract_valid
    out["contract_ignore_recall"] = recall_of(contract)
    return out


def _answers_right(
    labels: list[dict[str, Any]], runs: dict[str, dict[str, dict[str, Any]]]
) -> dict[str, dict[str, bool]]:
    human = {lab["item_id"]: lab["human_decision"] for lab in labels if lab.get("human_decision")}
    return {
        run: {
            item_id: answer.get("decision") == human[item_id]
            for item_id, answer in answers.items()
            if "error" not in answer and item_id in human
        }
        for run, answers in runs.items()
    }


def _discordant(left: dict[str, Any], right: dict[str, Any], run: str) -> tuple[int, int]:
    """Items only the left run gets right, then only the right run."""
    left_right = left.get("answers_right", {}).get(run, {})
    right_right = right.get("answers_right", {}).get(run, {})
    common = set(left_right) & set(right_right)
    only_left = sum(left_right[i] and not right_right[i] for i in common)
    only_right = sum(right_right[i] and not left_right[i] for i in common)
    return only_left, only_right


def summarize_runs(
    labels: list[dict[str, Any]],
    key: dict[str, Any],
    runs: dict[str, dict[str, dict[str, Any]]],
    prompts: list[str] | None = None,
) -> dict[str, Any]:
    prompts = prompts or ["current"]
    valid = {
        run: {i: a for i, a in answers.items() if "error" not in a} for run, answers in runs.items()
    }
    conditions = {**key_conditions(key), **valid}
    present = set(conditions)
    result = score_conditions(labels, conditions, key["population"], _pairs(prompts, present))
    n = len(labels)
    result["validity"] = {
        run: {
            "invalid": n - len(valid[run]),
            "invalid_share": round((n - len(valid[run])) / n, 4) if n else None,
        }
        for run in runs
    }
    prefix = "" if len(prompts) == 1 else "{p}:"
    result["stability"] = {}
    for p in prompts:
        for cond in ("trunc", "full"):
            left = prefix.format(p=p) + f"{cond}-a"
            right = prefix.format(p=p) + f"{cond}-b"
            if left in valid and right in valid:
                result["stability"][prefix.format(p=p) + cond] = _agreement(
                    valid[left], valid[right]
                )
    result["ignore_recall"] = {
        run: ignore_recall(labels, answers) for run, answers in valid.items()
    }
    result["decision_distribution"] = {
        run: dict(sorted(Counter(a["decision"] for a in valid[run].values()).items()))
        for run in runs
    }
    result["human_distribution"] = dict(
        sorted(Counter(lab["human_decision"] for lab in labels if lab["human_decision"]).items())
    )
    all_valid = all(v["invalid_share"] <= MAX_INVALID_SHARE for v in result["validity"].values())
    if len(prompts) == 1 and {"trunc-a", "full-a"} <= set(result["conditions"]):
        a, b = result["conditions"]["trunc-a"], result["conditions"]["full-a"]
        result["preregistered_rule_209"] = {
            "full_a_correct": b["correct"],
            "trunc_a_correct": a["correct"],
            "non_inferior": b["correct"] >= a["correct"],
            "valid": all_valid,
            "mcnemar_p_trunc_a_vs_full_a": result["pairs"]["trunc-a:full-a"]["mcnemar_p"],
        }
    if {"current", "new"} <= set(prompts):
        result["preregistered_rule_218"] = _rule_218(result, valid, all_valid)
    return result


def _rule_218(
    result: dict[str, Any], valid: dict[str, dict[str, dict[str, Any]]], all_valid: bool
) -> dict[str, Any]:
    """The numbers `evals/preregistration/218-prompt-dedupe.md` decides on."""
    cond = result["conditions"]
    human_link = result["human_distribution"].get("link", 0)
    new_link = sum(a["decision"] == "link" for a in valid["new:full-a"].values())
    new_full = cond["new:full-a"]["correct"]
    return {
        "new_full_a_correct": new_full,
        "current_full_a_correct": cond["current:full-a"]["correct"],
        "current_trunc_a_correct": cond["current:trunc-a"]["correct"],
        "beats_current_full": new_full >= cond["current:full-a"]["correct"],
        "not_worse_than_current_trunc": new_full >= cond["current:trunc-a"]["correct"],
        "new_link_answers": new_link,
        "human_link_answers": human_link,
        "mcnemar_p_current_trunc_a_vs_new_full_a": result["pairs"]["current:trunc-a:new:full-a"][
            "mcnemar_p"
        ],
        "mcnemar_p_current_full_a_vs_new_full_a": result["pairs"]["current:full-a:new:full-a"][
            "mcnemar_p"
        ],
        "valid": all_valid,
    }


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
    parser.add_argument(
        "--key", type=Path, default=Path("evals/gold/dedupe-GABARITO-NAO-ABRIR.json")
    )
    parser.add_argument("--record-dir", type=Path, default=DEFAULT_RECORD_DIR)
    parser.add_argument(
        "--labels",
        type=Path,
        default=Path("evals/gold/dedupe-rotulos.json"),
        help="Rotulos usados como gabarito (padrao: rodada 1, planilha cega manual)",
    )
    parser.add_argument(
        "--prompt",
        action="append",
        default=[],
        metavar="NOME=CAMINHO",
        help="Versao do prompt de dedupe (repetivel). Padrao: current=<prompt de producao>",
    )
    parser.add_argument("--yes", action="store_true", help="Autoriza as chamadas nao gravadas")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--model",
        default=None,
        help="provider/model no lugar de llm.review (ex.: openai/gpt-4o-mini)",
    )
    parser.add_argument(
        "--context",
        choices=("both", "full"),
        default="both",
        help="both = trunc e full (#209); full = so o contexto de producao (#273)",
    )
    parser.add_argument(
        "--strict-target",
        action="store_true",
        help="Alvo obrigatorio e dentre as notas comparadas (contrato #273)",
    )
    parser.add_argument(
        "--thinking",
        choices=("none",),
        default=None,
        help="reasoning_effort none. O gpt-6-luna exige isso na temperature 0.2",
    )
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
    if args.model:
        provider, sep, model_name = args.model.partition("/")
        if not sep or not provider or not model_name:
            parser.error("--model espera provider/model")
        cfg.llm.review.provider = provider
        cfg.llm.review.model = model_name
    if args.thinking == "none":
        cfg.llm.review.thinking = False
    spec = llm_phase(cfg, "review")
    temperature = effective_temperature(cfg, spec)
    try:
        prompt_args = parse_prompt_args(args.prompt) or [
            ("current", cfg.prompts_path / "dedupe_decision.md")
        ]
    except ValueError as exc:
        parser.error(str(exc))
    prompts = {name: load_prompt_parts(path) for name, path in prompt_args}
    names = list(prompts)
    key = json.loads(args.key.read_text(encoding="utf-8"))
    labels_payload = json.loads(args.labels.read_text(encoding="utf-8"))
    labels = labels_payload["labels"]
    states = load_states(args.state_db, key)
    item_ids = sorted(i for i in states if any(lab["item_id"] == i for lab in labels))
    recording_key = {item["item_id"]: decision_id(item) for item in key["items"]}
    thinking_key = "|thinking=none" if args.thinking == "none" else ""
    print(f"modelo: {spec.provider}/{spec.model} @ {temperature} thinking={spec.thinking}")
    print(f"prompts: {', '.join(f'{n}={p}' for n, p in prompt_args)}")
    print(f"itens com estado: {len(item_ids)}/{len(labels)}")

    db = StateDB(args.state_db)

    def full(hits: list[dict[str, Any]]) -> list[dict[str, str]]:
        return existing_note_contents(db, hits, cfg.linking.dedupe_note_chars)

    def build(name: str, run: str, item_id: str) -> tuple[str, str]:
        parts = prompts[name]
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

    active_runs = runs_for_context(args.context)
    runs: dict[str, dict[str, dict[str, Any]]] = {}
    for name in names:
        for run in active_runs:
            result_id = run if len(names) == 1 else f"{name}:{run}"
            # The prompt text is in the key, so a prompt saved elsewhere replays
            # what was recorded under its production path.
            identity = f"{spec.provider}/{spec.model}@{temperature}{thinking_key}"
            run_key = sha256_hex(f"{run}|{prompts[name].full_template}|{identity}")[:12]
            record_path = args.record_dir / f"{run}-{run_key}.json"
            recorded = (
                json.loads(record_path.read_text(encoding="utf-8"))["answers"]
                if record_path.exists()
                else {}
            )
            missing = [i for i in item_ids if recording_key[i] not in recorded]
            chars = sum(len("".join(build(name, run, i))) for i in missing)
            print(
                f"{result_id}: gravados {len(recorded)} | a chamar {len(missing)} "
                f"(~{chars // 4} tokens)"
            )
            if missing and not args.yes:
                continue

            def ask(item_id: str, name: str = name, run: str = run) -> dict[str, Any]:
                from zettel.llm import call_llm

                system, user = build(name, run, item_id)
                text = call_llm(
                    llm,
                    user,
                    system=system or None,
                    provider=spec.provider,
                    prompt_cache=cfg.llm.prompt_cache,
                    label=f"probe-dedupe:{name}:{run}",
                )
                compared = None
                if args.strict_target:
                    state = states[item_id]
                    notes = existing_notes_for(condition_of(run), state, full)
                    compared = {note["id"] for note in notes}
                return parse_answer(text, compared)

            def on_record(
                key: str, entry: dict[str, Any], path=record_path, rec=recorded, run=run
            ) -> None:
                rec[key] = entry
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

            runs[result_id] = collect(item_ids, recorded, ask, on_record, recording_key.__getitem__)
    db.close()

    if len(runs) < len(run_ids(names, active_runs)):
        print("Sem --yes: rodadas incompletas, nada pontuado.")
        return 1

    labelled = [lab for lab in labels if lab["item_id"] in states]
    result = summarize_runs(labelled, key, runs, names)
    result["model"] = f"{spec.provider}/{spec.model}"
    result["key"] = str(args.key)
    result["labels"] = str(args.labels)
    result["labels_method"] = labels_payload.get("method", "manual_blind")
    result["prompts"] = {
        name: {"path": str(path), "sha": sha256_hex(prompts[name].full_template)[:12]}
        for name, path in prompt_args
    }
    result["temperature"] = temperature
    result["thinking"] = args.thinking
    result["answers_right"] = _answers_right(labelled, runs)
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(text, end="")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
