# ADR-050: The Extract Model Is Chosen Against the Human Gold Set — gemini-3.5-flash-lite

**Status**: Accepted (2026-09-13)

**Depends on:**
- [ADR-015: Granular Per-Chunk Literature Notes with Readable Filenames](./ADR-015-granular-literature-notes-readable-filenames.md)
- [ADR-024: Pluggable Multi-Provider LLM Strategy](../LLM/ADR-024-multi-provider-llm-strategy.md)

**Related to:**
- [ADR-017: Confidence-Band Human-in-the-Loop Approval Gate](../REVIEW/ADR-017-confidence-band-hitl-approval-gate.md)
- [ADR-038: Ask Evaluation as Offline Replay](../QA-WRITING/ADR-038-ask-trajectory-evals-offline-replay.md)
- [ADR-048: Per-Phase LLM Thinking Mode](../LLM/ADR-048-per-phase-thinking-mode.md)
- [ADR-049: No Pre-LLM Gate on Extract](./ADR-049-no-pre-llm-gate-on-extract.md)

## Context and Problem Statement

On 2026-09-09 commit `690ab8c` moved `llm.extract` from `gemini-3.5-flash-lite` to `gpt-4o-mini`, and `5516b96` raised its temperature from 0.0 to 0.1 a day later. Neither change was measured: there was no ground truth to measure against.

Issue #175 built one — 120 chunks, stratified, labeled blind by a human answering *would I keep this passage as a permanent note?* While testing a prompt change for #177, the recorded verdicts turned out to come from two extractors: the 77 items of `@Kim2022KantAnd` had been extracted with Gemini before the switch, the other 43 with `gpt-4o-mini`. Re-running the production extractor offline over the same chunks exposed what the switch had done to that book, on the same items and the same labels:

| extractor | precision | recall |
|---|---|---|
| `gemini-3.5-flash-lite` (recorded) | 62.1% | 72.0% |
| `gpt-4o-mini` @ 0.1 | 46.2% | 96.0% |

Suggestive — exact paired p ≈ 0.03 — but one book, one run of each, at different temperatures, with the historical call not reproducible from the current configuration. Issue #181 measured it properly.

Precision is the pipeline's problem. Every signal that could gate auto-approval is indistinguishable from a coin (ADR-017, 2026-09-13 addendum), so whatever `extract` accepts can become a permanent note. Before this decision the production extractor was accepting roughly a third of its chunks against a human's judgement.

## Decision Drivers

* The choice must rest on the human's judgement, not on the model's own ratings, which collapse onto a constant (ADR-017 addendum).
* **The rule is fixed before the numbers exist.** Committed in `evals/preregistration/181-modelo-extract.md` (`53c105b`) ahead of the first Gemini call.
* **The criterion is the user's.** Net correct items: a lost note and an admitted junk chunk weigh the same. Cost is not part of the decision.
* The comparison has to isolate the model. Prompt, few-shots, filters and call assembly stay production's; only the model identity moves (`scripts/probe_prompt1_variant.py --provider/--model/--temperature`).
* A sample stratified toward the rejection boundary must not decide something the corpus would decide the other way.

## Considered Options

* Keep `gpt-4o-mini` @ 0.1.
* Switch to `gemini-3.5-flash-lite` @ 0.1.
* Switch to `gemini-3.5-flash-lite` @ 0.0, the historical temperature.

## Decision Outcome

**Switch `llm.extract` to `gemini-3.5-flash-lite` @ 0.1**, with *thinking* at the vendor default — the condition that was measured.

Four runs over the 120 gold items, with production prompt and few-shots (`prompt_sha 9f849789e727`, `examples_sha e078fb5c0465`):

| run | precision | recall | F1 | rejections a human would keep | parse failures |
|---|---|---|---|---|---|
| `gpt-4o-mini` @ 0.1 (production) | 60.9% | 97.5% | 75.0% | 4.8% | 0 |
| `gemini` @ 0.1, run A (**decision**) | 65.8% | 92.5% | 76.9% | 10.4% | 0 |
| `gemini` @ 0.1, run B (noise) | 65.6% | 96.2% | 78.0% | 5.9% | 5 |
| `gemini` @ 0.0 (informative) | 65.3% | 92.0% | 76.4% | 10.3% | 5 |

Precision, recall and F1 are corpus-weighted by the inclusion weights frozen in the key.

The pre-registered rule, applied as written:

| condition | required | measured | |
|---|---|---|---|
| 1. Net correct | Gemini right more often, exact McNemar p < 0.05 | 97 vs 83 correct; 18 items only Gemini gets right, 4 only `gpt-4o-mini`; **p = 0.0043** | met |
| 2. Stability | runs A and B agree on ≥ 95% of verdicts | **97.4%** over 115 common items | met |
| Parse failures | ≤ 6 per Gemini run | A: 0, B: 5 | met |
| No corpus conflict | weighted net difference has the same sign as rule 1 | **+28.2 chunks** in Gemini's favour | met |

Every discordant verdict on the decision pair moved the same way: 22 chunks `gpt-4o-mini` accepted and Gemini rejects — **18 a human discards, 4 a human keeps.** Gemini is the stricter extractor, and on this set its strictness is mostly right.

**The trade, stated in the corpus's own units** (655 labeled-scope chunks, errors weighted by inclusion weight):

| | lost notes (FN) | admitted junk (FP) | total errors |
|---|---|---|---|
| `gpt-4o-mini` @ 0.1 | ~7 | ~186 | ~194 |
| `gemini` @ 0.1, run A | **~22** | **~143** | **~165** |

About 15 more notes lost to keep out about 43 junk chunks. Under the chosen criterion that is ~28 fewer errors, and the rule passes. It is a real cost nonetheless, and the reason the criterion had to be the user's.

Supporting observations:

* **Temperature does not explain the gap.** Gemini at 0.0 against 0.1 agrees on 94.8% of verdicts; the correctness split is 4 vs 2, p = 0.69.
* **The historical Gemini behaviour reproduces today.** Against the verdicts recorded on 2026-09-09, run A changes only 5 of the 77 book items. The #177 comparison was not an artefact of old conditions.
* **The difference is almost entirely one book.** Of the 22 discordant verdicts on the decision pair, 20 are on `@Kim2022KantAnd` (17 discards, 3 keeps), where sample precision goes from 46% to 66%; the other 2 are single items on `@Pate2026ReplicatingHuman`, one of each. On the remaining four sources the two extractors give identical verdicts.

### Positive Consequences

* Estimated corpus precision rises from 60.9% to 65.8% and total errors fall by about 28 chunks — the first measured improvement in extraction quality this project has recorded.
* Precision is the problem ADR-017 cannot gate, so the gain lands where it is needed.
* No new provider dependency: `connect` already runs on Gemini (`gemini-3.1-flash-lite`).
* The decision is reproducible end to end: gold set, pre-registration, recorded runs and comparison script are all committed or scripted.

### Negative Consequences

* **Lost notes roughly triple**, from ~7 to ~22 in the corpus, and the share of rejections a human would keep rises from 4.8% to 10.4%. These are silent: a rejected chunk writes no draft and nothing downstream sees it.
* **Gemini returned unparseable output on 5 of 120 chunks in two of three runs, skewed toward chunks a human keeps** (3 of 5 in run B; `G086` failed in both). Run A had none, so the decision is unaffected. Production `extract` makes one repair attempt that the probe does not, so the production failure rate is expected to be lower, but it is not measured. A chunk that still fails is marked `failed`, not lost, and `zettel retry-failed` requeues it.
* **Cost rises about 2.7×** for extract (estimated USD 0.458 against 0.167 per 120 chunks; more if the vendor's default thinking is billed). Excluded from the decision by the user.
* **Precision stays low.** About a third of accepted chunks are still something a human would discard. This decision reduces the problem; it does not close it.
* **The existing vault is not re-extracted.** The LLM cache key includes the model, so a Gemini call never replays a response cached from `gpt-4o-mini`; but chunks already extracted keep their verdicts until explicitly reset.
* **The evidence is concentrated.** The measured effect comes from one book dominating the gold set (77 of 120 items), and the sample holds 40 accepted chunks. Confirmation on new labels, especially from papers, would strengthen it.

## What Would Reopen This

* A rerun on a gold set with materially more non-book sources that reverses the direction.
* A production parse-failure rate for Gemini, after the repair attempt, above a few percent.
* A different criterion. With a lost note weighing more than an admitted junk chunk — about three times more is the break-even here, 43 against 15 — this decision flips.
* A new extractor candidate, measured the same way: pre-registered, same prompt, same comparison.

## Reproduce

```bash
.venv/Scripts/python.exe scripts/compare_gold_runs.py \
    --run gpt4omini=evals/gold/extracao-GABARITO-gpt4omini-atual.json \
    --run gemini-t01-a=evals/gold/extracao-GABARITO-gemini-t01-a.json \
    --run gemini-t01-b=evals/gold/extracao-GABARITO-gemini-t01-b.json \
    --run gemini-t00=evals/gold/extracao-GABARITO-gemini-t00.json \
    --pair gpt4omini:gemini-t01-a --pair gemini-t01-a:gemini-t01-b --pair gemini-t01-a:gemini-t00
```

Offline and free: it reads the committed keys. Regenerating a key re-calls the model (`scripts/probe_prompt1_variant.py`, commands in the pre-registration).

## References

* `evals/preregistration/181-modelo-extract.md` — the rule, committed before the runs (`53c105b`)
* `evals/results/extract-models-181.json` — per-run and per-pair results
* `evals/gold/extracao-GABARITO-{gpt4omini-atual,gemini-t01-a,gemini-t01-b,gemini-t00}.json` — regenerated keys (ids and verdicts only)
* `scripts/probe_prompt1_variant.py`, `scripts/compare_gold_runs.py` — the instruments
* `config/config.yaml` — `llm.extract`
* Issues #175 (gold set), #177 (discovery of the model mix), #181 (this decision)
