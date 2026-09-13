"""Re-run Prompt 1 over the gold set, offline, and compare against the recorded verdicts (#177).

`extract` writes drafts, moves chunk status and fills the response cache. None of that
is acceptable for an experiment on the prompt, so this script sends the same call and
writes nothing but its own recording: no state.db change, no vault change, and it never
reads the LLM response cache -- a fresh response is the point.

**The call is built by the pipeline's own code.** `prompt1_images_context`,
`prompt1_messages` and `prompt1_call_checksum` in `zettel/extractor.py` are the functions
`_process_chunk` uses, so a run here measures the prompt `extract` would send and not a
copy that drifted.

**Why the recorded verdicts cannot serve as "before".** The gold key mixes two
extractors: @Kim2022KantAnd (77 of 120) was extracted with gemini-3.5-flash-lite, the rest
with the gpt-4o-mini @ 0.1 now in production. So the first run of this script uses the
production prompt, and it is that run -- not the recorded verdicts -- that a prompt change
is compared against. The same run doubles as a noise floor: on the items whose recorded
call was made under the current configuration, a flip is run-to-run variation, not a
change of model. Which items those are is decided by recomputing the production call
checksum, not by source name.

**Recording identity covers everything sent.** Each chunk's entry is keyed by a hash of
the rendered system and user messages plus the model settings. The production cache key
hashes the template only and misses the few-shots from `domain_examples.yaml`; an edit
there would replay stale responses. Here it cannot.

Usage:
    .venv/Scripts/python.exe scripts/probe_prompt1_variant.py --label gpt4omini-atual
    .venv/Scripts/python.exe scripts/probe_prompt1_variant.py --label gpt4omini-atual --yes \\
        --out-key evals/gold/extracao-GABARITO-gpt4omini-atual.json
    .venv/Scripts/python.exe scripts/probe_prompt1_variant.py --label narrative-v2 --yes \\
        --prompt prompts/literature_note.md --examples config/domain_examples.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Run from a clone without installing the package (mirrors scripts/ precedent).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_KEY = Path("evals/gold/extracao-GABARITO-NAO-ABRIR.json")
DEFAULT_LABELS = Path("evals/gold/extracao-rotulos.json")
DEFAULT_RECORD_DIR = Path(".eval-work/prompt1")


@dataclass(frozen=True)
class Verdict:
    chunk_status: str
    rejection_category: str
    n_candidates: int
    n_approved: int

    @property
    def has_content(self) -> bool:
        """Whether `extract` would have written a draft (extractor.py `has_content`)."""
        return self.chunk_status != "rejected" and self.n_approved > 0


# -- Pure pieces ---------------------------------------------------------


def call_id(system: str, user: str, settings: dict[str, Any]) -> str:
    """Identity of one call: every byte sent, plus the sampling settings."""
    from zettel.hashing import sha256_hex

    blob = json.dumps({"system": system, "user": user, **settings}, sort_keys=True)
    return sha256_hex(blob)


def verdict_from_output(output: Any, cfg: Any, chunk_text: str) -> Verdict:
    from zettel.extractor import _filter_candidates

    approved, _rejected = _filter_candidates(output.candidates, cfg, chunk_text)
    return Verdict(
        chunk_status=output.chunk_status,
        rejection_category=output.rejection_category or "",
        n_candidates=len(output.candidates),
        n_approved=len(approved),
    )


def collect(
    items: list[dict[str, Any]],
    recorded: dict[str, dict[str, Any]],
    call: Callable[[dict[str, Any]], str],
    parse: Callable[[dict[str, Any], str], Verdict],
    on_record: Callable[[str, str, Verdict, str], None],
) -> tuple[dict[str, Verdict], dict[str, str]]:
    """Reuse a recording only when its call identity matches; call the model otherwise.

    Each item carries `chunk_id` and `call_id`. A recording made for a different prompt,
    few-shot set or model has a different `call_id` and is never reused. A parse failure
    is reported and not recorded, so a re-run retries it.
    """
    verdicts: dict[str, Verdict] = {}
    failures: dict[str, str] = {}
    for item in items:
        chunk_id = item["chunk_id"]
        entry = recorded.get(chunk_id)
        if entry and entry.get("call_id") == item["call_id"]:
            verdicts[chunk_id] = Verdict(**entry["verdict"])
            continue
        response = call(item)
        try:
            verdict = parse(item, response)
        except ValueError as exc:  # ValidationError subclasses ValueError
            failures[chunk_id] = str(exc)[:160]
            continue
        verdicts[chunk_id] = verdict
        on_record(chunk_id, item["call_id"], verdict, response)
    return verdicts, failures


def regenerate_key(original: dict[str, Any], verdicts: dict[str, Verdict], meta: dict) -> dict:
    """Same items, strata and population; this run's verdicts. Failed items are dropped."""
    items = []
    for entry in original["items"]:
        verdict = verdicts.get(entry["chunk_id"])
        if verdict is None:
            continue
        items.append(
            {
                **entry,
                "llm_verdict": verdict.chunk_status,
                "llm_category": verdict.rejection_category,
            }
        )
    return {
        **{k: v for k, v in original.items() if k != "items"},
        "n_items": len(items),
        "items": items,
        "regenerated": meta,
    }


def compare(
    original: dict[str, Any],
    verdicts: dict[str, Verdict],
    same_config: dict[str, bool],
) -> dict[str, Any]:
    """Flips between the recorded verdict and this run, split by how the record was made."""
    groups: dict[str, Counter[str]] = {"mesma_config": Counter(), "config_antiga": Counter()}
    flips: list[dict[str, str]] = []
    for entry in original["items"]:
        verdict = verdicts.get(entry["chunk_id"])
        if verdict is None:
            continue
        group = "mesma_config" if same_config.get(entry["chunk_id"]) else "config_antiga"
        groups[group]["n"] += 1
        if verdict.chunk_status != entry["llm_verdict"]:
            groups[group]["flips"] += 1
            flips.append(
                {
                    "item_id": entry["item_id"],
                    "group": group,
                    "before": f"{entry['llm_verdict']}/{entry.get('llm_category') or ''}",
                    "after": f"{verdict.chunk_status}/{verdict.rejection_category}",
                }
            )
    return {"groups": {g: dict(c) for g, c in groups.items()}, "flips": flips}


def labels_from_artifact(path: Path) -> list[Any]:
    from zettel.evals.extraction import HumanLabel

    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        HumanLabel(
            item_id=lab["item_id"],
            verdict=lab["human_verdict"],
            category=lab.get("human_category") or "",
            note=lab.get("human_note") or "",
        )
        for lab in payload["labels"]
    ]


def narrative_losses(key: dict[str, Any], labels: list[Any], narrative_items: set[str]) -> dict:
    """Among items drawn as LLM-`narrative` rejections: human keeps that the key still rejects."""
    human = {lab.item_id: lab.verdict for lab in labels}
    by_id = {e["item_id"]: e for e in key["items"]}
    kept = [i for i in narrative_items if human.get(i) == "keep" and i in by_id]
    lost = [i for i in kept if by_id[i]["llm_verdict"] == "rejected"]
    return {"human_keeps": len(kept), "still_rejected": len(lost), "items": sorted(lost)}


# -- CLI -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--label", required=True, help="Nome desta rodada (ex.: gpt4omini-atual)")
    parser.add_argument("--gold-key", type=Path, default=DEFAULT_KEY)
    parser.add_argument("--gold-labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--prompt", type=Path, default=None, help="Default: prompt de producao")
    parser.add_argument("--examples", type=Path, default=None, help="Default: domain.examples_path")
    parser.add_argument("--record-dir", type=Path, default=DEFAULT_RECORD_DIR)
    parser.add_argument("--yes", action="store_true", help="Autoriza as chamadas nao gravadas")
    parser.add_argument("--out-key", type=Path, default=None, help="Grava o gabarito regenerado")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from zettel.config import effective_temperature, llm_phase, load_config
    from zettel.domain_examples import load_domain_examples, render_for_prompt
    from zettel.evals.extraction import score
    from zettel.extractor import (
        _parse_literature_output,
        prompt1_call_checksum,
        prompt1_images_context,
        prompt1_messages,
    )
    from zettel.hashing import sha256_hex
    from zettel.llm import load_prompt_parts
    from zettel.preflight import estimate_tokens
    from zettel.pricing import estimate_llm_cost
    from zettel.state import StateDB

    cfg = load_config()
    spec = llm_phase(cfg, "extract")
    production_prompt = Path(cfg.prompts_path) / "literature_note.md"
    prompt_path = args.prompt or production_prompt
    examples_path = args.examples or Path(cfg.domain.examples_path)
    parts = load_prompt_parts(prompt_path)
    examples = render_for_prompt(load_domain_examples(examples_path), "literature_note")
    production_hash = sha256_hex(load_prompt_parts(production_prompt).full_template)
    settings = {
        "provider": spec.provider,
        "model": spec.model,
        "temperature": effective_temperature(cfg, spec),
        "top_p": cfg.llm.top_p,
        "thinking": str(spec.thinking),
    }

    original = json.loads(args.gold_key.read_text(encoding="utf-8"))
    narrative_items = {
        e["item_id"] for e in original["items"] if e.get("llm_category") == "narrative"
    }

    db = StateDB(cfg.state_db_path)
    try:
        items: list[dict[str, Any]] = []
        same_config: dict[str, bool] = {}
        for entry in original["items"]:
            row = db.conn.execute("SELECT * FROM chunks WHERE chunk_id = ?", (entry["chunk_id"],))
            row = row.fetchone()
            if row is None:
                continue
            row = dict(row)
            context = prompt1_images_context(db, row)
            system, user = prompt1_messages(cfg, db, row, parts, examples, context)
            same_config[row["chunk_id"]] = (
                prompt1_call_checksum(cfg, production_hash, row, context)
                == row["llm_call_checksum_prompt1"]
            )
            items.append(
                {
                    "chunk_id": row["chunk_id"],
                    "text": row["text"] or "",
                    "system": system,
                    "user": user,
                    "call_id": call_id(system, user, settings),
                }
            )
    finally:
        db.close()

    record_path = args.record_dir / f"{args.label}.json"
    recorded: dict[str, dict[str, Any]] = {}
    if record_path.exists():
        recorded = json.loads(record_path.read_text(encoding="utf-8"))["responses"]

    missing = [i for i in items if recorded.get(i["chunk_id"], {}).get("call_id") != i["call_id"]]
    print(f"rodada: {args.label}")
    print(
        f"extrator: {spec.provider}/{spec.model} @ {settings['temperature']} (config de producao)"
    )
    print(f"prompt: {prompt_path} | exemplos: {examples_path}")
    print(
        f"itens do gold: {len(items)} | gravados e validos: {len(items) - len(missing)} | "
        f"a chamar: {len(missing)}"
    )
    if missing:
        tokens_in = sum(estimate_tokens(i["system"]) + estimate_tokens(i["user"]) for i in missing)
        tokens_out = len(missing) * cfg.extraction.preflight_output_tokens_per_chunk
        usd = estimate_llm_cost(spec.model, tokens_in, tokens_out, provider=spec.provider)
        print(f"custo estimado: USD {usd:.3f} ({tokens_in} tokens entrada + {tokens_out} saida)")
        if not args.yes:
            print("Sem --yes: nenhuma chamada feita.")
            return 1

    from zettel.llm import call_llm, get_llm

    llm = get_llm(cfg, "extract") if missing else None

    def call(item: dict[str, Any]) -> str:
        return call_llm(
            llm,
            item["user"],
            system=item["system"] or None,
            label=f"probe-extract:{item['chunk_id']}",
            provider=spec.provider,
            prompt_cache=cfg.llm.prompt_cache,
        )

    def parse(item: dict[str, Any], response: str) -> Verdict:
        return verdict_from_output(_parse_literature_output(response), cfg, item["text"])

    def on_record(chunk_id: str, cid: str, verdict: Verdict, response: str) -> None:
        recorded[chunk_id] = {"call_id": cid, "verdict": asdict(verdict), "response": response}
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(
                {"label": args.label, "settings": settings, "responses": recorded},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    verdicts, failures = collect(items, recorded, call, parse, on_record)
    meta = {
        "label": args.label,
        "extractor": f"{spec.provider}/{spec.model}@{settings['temperature']}",
        "prompt": str(prompt_path),
        "prompt_sha": sha256_hex(parts.full_template)[:12],
        "examples": str(examples_path),
        "examples_sha": sha256_hex(json.dumps(examples, sort_keys=True))[:12],
    }
    regenerated = regenerate_key(original, verdicts, meta)
    diff = compare(original, verdicts, same_config)

    from zettel.evals.extraction import KeyItem

    def as_key_items(payload: dict[str, Any]) -> list[KeyItem]:
        return [
            KeyItem(
                e["item_id"],
                e["chunk_id"],
                e["source_id"],
                e["llm_verdict"],
                e.get("llm_category") or "",
                e["sampling_stratum"],
            )
            for e in payload["items"]
        ]

    labels = labels_from_artifact(args.gold_labels)
    population = {k: int(v) for k, v in original["population"].items()}
    before = score(labels, as_key_items(original), population)
    after = score(labels, as_key_items(regenerated), population)

    print()
    print("variacao contra os vereditos gravados:")
    for group, counts in diff["groups"].items():
        n, f = counts.get("n", 0), counts.get("flips", 0)
        what = "ruido entre rodadas" if group == "mesma_config" else "troca de modelo + ruido"
        print(f"  {group:<14} n={n:<3} trocaram de veredito: {f:<3} ({what})")
    for flip in diff["flips"]:
        print(f"    {flip['item_id']} [{flip['group']}] {flip['before']} -> {flip['after']}")

    print()
    print(f"{'':<40}{'gravado':>10}{args.label:>22}")
    for name, attr in (
        ("precisao (corpus)", "precision"),
        ("recall (corpus)", "recall"),
        ("rejeicoes que o humano guardaria", "rejected_but_keep_rate"),
    ):
        print(f"  {name:<38}{getattr(before, attr):>10.1%}{getattr(after, attr):>22.1%}")
    nl_before = narrative_losses(original, labels, narrative_items)
    nl_after = narrative_losses(regenerated, labels, narrative_items)
    print(
        f"  {'perdas narrative (humano guarda)':<38}"
        f"{nl_before['still_rejected']:>7}/{nl_before['human_keeps']:<2}"
        f"{nl_after['still_rejected']:>19}/{nl_after['human_keeps']:<2}"
    )
    print(f"  perdas narrative restantes nesta rodada: {nl_after['items']}")
    if failures:
        print(f"\nFALHAS DE PARSE (fora da conta, repetidas no proximo run): {len(failures)}")
        for chunk_id, err in failures.items():
            print(f"  {chunk_id[-32:]}: {err}")

    if args.out_key:
        args.out_key.parent.mkdir(parents=True, exist_ok=True)
        args.out_key.write_text(
            json.dumps(regenerated, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\ngabarito regenerado: {args.out_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
