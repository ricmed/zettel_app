# ADR-055: Typed Decision Layer (TypeSafe Jev) in Shadow Mode

**Status**: Accepted (2026-10-06)  
**Depends on**: [ADR-024](./ADR-024-multi-provider-llm-strategy.md), [ADR-054](../INFRA/ADR-054-statedb-as-python-package.md)  
**Relates to**: [ADR-016](../REVIEW/ADR-016-post-approval-concept-deduplication-timing.md), [ADR-019](../GARDEN/ADR-019-taxonomy-first-moc-clustering.md), [ADR-028](../QA-WRITING/ADR-028-langgraph-stategraph-article-orchestration.md), [ADR-038](../QA-WRITING/ADR-038-ask-trajectory-evals-offline-replay.md), [ADR-045](./ADR-045-fail-fast-on-llm-unavailability.md), [ADR-049](../EXTRACT/ADR-049-no-pre-llm-gate-on-extract.md), [ADR-050](../EXTRACT/ADR-050-extract-model-chosen-against-gold-set.md)

## Context

The pipeline separates three kinds of output:

- **facts the code computes**, such as grounding, the relevance floor, the citation page and the `corroborates` edge;
- **prose a model writes**, such as literature and permanent notes, summaries and articles;
- **judgement a human confirms** in `review`.

Several decisions are closed choices, yet they are handled as generation (JSON parsed out of an LLM) or as an argmax with no threshold:

| Decision | Today | Space of answers |
|---|---|---|
| Same-source dedupe | `prompts/dedupe_decision.md`, uncached `call_llm` | `create_new` / `ignore` / `refine_existing` / `merge` + a target note |
| Category of a garden cluster | cosine argmax over embedded category labels | the categories of `config/moc_topics.yaml` |
| Article judge | `prompts/article_judge.md`, four 0–10 scores + verdict + feedback | ordered scales |

A model that answers typed questions with probabilities would let code act on the clear case and send the uncertain one to a human. TypeSafe's Jev (`jev-1.13.0`) is such a model:

- It does not generate text.
- It evaluates a `state` against named questions: `noul` (yes/no as a 0–1 probability), `choice` (one of up to 255 labelled options) and `score` (a position on 2–10 ordered levels).
- It returns the value with its distribution and a confidence.
- All questions in a request run in parallel and in isolation against the same state.

Two facts stood in the way of simply switching:

1. **No labels.** The only human gold set in the repository (`evals/gold/extracao-rotulos.json`, #175) labels extract keep/discard. Nothing labels dedupe, cluster category or judge scores.
2. **Declared weaknesses that hit this project.** The model's documentation lists three:
   - lower accuracy outside English, while all content here is PT-BR;
   - a lean toward the first option of a `choice`;
   - weak numerical calibration of `score`.

   It also says accuracy drops when the state carries material the question does not use.

## Decision

Add a **typed decision layer** that runs **only in shadow mode**. Beside each of the three decisions above, it records what the model would have decided, and nothing branches on that record. Switching any decision is a separate issue, gated by the numbers in the pre-registration of #206 (`evals/preregistration/206-jev-camada-decisao.md`).

```
zettel/decision/
  client.py   -- the only SDK caller; fail-open; get_decision_client is the seam
  permute.py  -- order-rotated copies of each choice, folded back into one answer
  sites.py    -- pure builders: decision inputs -> Decision(state, questions)
  shadow.py   -- per-site hooks; persist to decision_shadow
```

### Rules

1. **Not an LLM phase.** The client does not go through `get_llm`/`call_llm`, and `decision` is not in `LLM_PHASES`. The SDK (`typesafe-sdk`) is imported lazily, so `zettel --help` does not load it.
2. **Fail-open, the opposite of ADR-045.** A missing package, a missing `TYPESAFE_API_KEY` or an API error becomes `DecisionResult(error=...)` and is written to the row. ADR-045 fails fast because a pipeline without its LLM produces nothing. A shadow without its model only loses an observation. Unavailability is decided once per process and warned once.
3. **Pinned model.** `decision.model` is `jev-1.13.0`, never `jev-latest`, so the report never mixes two distributions.
4. **Order permutations in one request.** Every `choice` is asked `decision.order_permutations` times (default 3), with its options rotated so a different one leads each copy.
   - The copies cost no latency, since they are parallel questions on the same state.
   - The aggregate averages the per-option probabilities.
   - `spread` (the mean standard deviation across copies) is the stability measure.
   - `noul` and `score` are not permuted: `noul` has no options, and `score` levels are ordered.
5. **Minimal state, one dimension per question.** Each builder passes only what the question reads:
   - the dedupe state is exactly the same-source notes the LLM saw, with the same 200-character excerpt;
   - the judge asks each dimension in its own request — naturalness sees only the article, fidelity the article and the notes;
   - every `choice` whose list may not fit gets a `none` option.
6. **Questions live in code** (`sites.py`), not in `prompts/`, because their option keys are contracts with code (`DedupeDecision`, taxonomy category names). Instructions and criteria exist in English and Portuguese (`decision.instructions_language`), and the content stays PT-BR. The language is chosen by the pre-registered probe, not by preference.
7. **Persistence.** `decision_shadow` has one row per `(site, subject_id, state_checksum)`, holding the baseline the pipeline decided, the folded answer, latency, input tokens, error, and a human label when one exists.
   - Re-running an unchanged decision refreshes the baseline and reuses the stored answer.
   - The human label survives that refresh.
   - The table is created with `CREATE TABLE IF NOT EXISTS`, so no reset is needed.
8. **Human labels where they already exist.** In dedupe, the reviewer's `m` (keep) / `d` (discard) on a `dedupe_pending` concept is attached to its shadow row as `not_ignore` / `ignore`. That label only exists for what the LLM already flagged as redundant, and the report must say so.
9. **Cost is recorded, not decisive.** Usage goes to the active `CostTracker` as `jev:<site>`, priced from `decision.input_price_per_mtok`. LiteLLM does not know the model and `pricing.py` would report $0. Output tokens are free on this model.
10. **Tests never reach the network.** A suite-wide fixture (`tests/conftest.py`) replaces the SDK builder with a refusal the client treats as "unavailable". Tests that need answers inject a fake through `get_decision_client`.

### What is measured, and how

- **Capability in PT-BR, against real labels.** `scripts/probe_jev_gold.py` asks two things:
  - `extract`: a `noul` "would a curator keep this?" and a category `choice`, over the 118 judged gold items;
  - `reader`: the same `noul` plus a 1–5 `score`, over the 38 accepted items with the production extractor's notes.

  Passages come from the labelling sheet, the text the human judged, not from `state.db`. The signal is the AUC of the `noul` with a 95% interval. The extract run also writes a GABARITO key for `scripts/compare_gold_runs.py`. This is **measurement, not a gate**: ADR-049 stands.
- **Agreement in place.** `scripts/report_decision_shadow.py` reports, per site:
  - agreement with the current decision, overall and by the model's confidence band (≥ 0.9, 0.6–0.9, < 0.6);
  - human agreement for dedupe, next to the LLM's;
  - the mean spread across permutations, latency, and the error rate.

## Consequences

- **Pipeline output is identical** with shadow on or off. The only new side effects are rows in `decision_shadow`, `jev:*` events in the run's cost, and up to a few hundred milliseconds per decision.
- **Doctor reports the layer.** `zettel doctor` shows it, with active sites, model, credential and dependency. With shadow on and no key, the check fails, while the pipeline itself still runs.
- **A public helper.** `article.format_notes_catalog` is now public, because the judge hook renders the same catalog the LLM judge reads.
- **Two baselines are imperfect, and the report has to say so.**
  - The dedupe label is biased toward the LLM's own flags.
  - The cluster baseline is the embedding argmax, not the LLM's `topic`. When a suggestion matches, `gardener._create_new_moc` overwrites the LLM's topic with the embedding category, so that topic is not an independent judgement.
- **The language decision is reversible by config** (`decision.instructions_language`). Each recording is keyed by language, model, permutations and the question set.

## Alternatives

- **Switch the decisions directly.** Rejected: no labels, declared PT-BR and ordering weaknesses, and a threshold chosen without data would be a guess with a probability attached.
- **Use the LLM's own logprobs as the probability.** Rejected for now. It is provider-specific, absent on several gateways the project supports, and a JSON field's token probability is not a calibrated class probability.
- **Record shadow verdicts as JSONL under `.eval-work/`.** Rejected: the dedupe human label is written by `review`, so the verdict and the label must share a key in the store that `review` already writes.
- **One request for all four judge dimensions.** Rejected: fidelity needs the notes catalog, naturalness only the prose. A shared state is the "material the question does not use" the model's documentation warns about.

## References

* `zettel/decision/client.py` — `DecisionClient`, `get_decision_client`, `DecisionUnavailable`
* `zettel/decision/permute.py` — `expand`, `collapse`
* `zettel/decision/sites.py` — `dedupe`, `moc_category`, `judge_dimension`, `extract_gold`, `reader_gold`
* `zettel/decision/shadow.py` — `shadow_dedupe`, `label_dedupe`, `shadow_moc_category`, `shadow_article_judge`
* `zettel/state/decisions.py` — `DecisionsMixin`
* `zettel/config.py` — `DecisionConfig`, `DecisionSitesConfig`
* `scripts/probe_jev_gold.py`, `scripts/report_decision_shadow.py`
* `evals/preregistration/206-jev-camada-decisao.md`
* `tests/test_decision.py`, `tests/test_probe_jev_gold.py`, `tests/test_report_decision_shadow.py`

## Results (2026-10-06): the pre-registered probe

The four runs of `evals/preregistration/206-jev-camada-decisao.md` had no API failures and no missing items. Passages came from the labelling sheet; the reader's notes came from the recorded `gemini-t01-a` run.

| run | `noul` keep AUC | IC95 | verdict |
|---|---|---|---|
| `extract-en` | 0.818 | [0.725, 0.911] | separates |
| `extract-pt` | 0.853 | [0.768, 0.939] | separates |
| `reader-en` | 0.637 | [0.457, 0.816] | indistinguishable from a coin |
| `reader-pt` | 0.661 | [0.485, 0.836] | indistinguishable from a coin |

- **Rule 1 (signal) holds** on extract, in both languages. The model separates what a human would keep from what they would discard in PT-BR content. On the reader stratum it does not: 38 items can only detect an AUC of 0.73 or more, the same limit that stopped #176.
- **Rule 2 (language): `en`.** `pt` leads by 0.035, below the pre-registered 0.05, so `decision.instructions_language` stays `en`.
- **Rule 3 (stability) holds.** The mean spread across the three permutations of the category `choice` is 0.006 (`en`) and 0.008 (`pt`), against a limit of 0.05. Option order barely moves this model here.
- **Category agreement on human discards is about 0.48** (n = 65). It is reported, not ruled on.
- **Informative comparison with the production extractor** (`evals/results/jev-vs-extract-206.json`): at the fixed 0.5 threshold, `jev-en` has recall 60.2% against `gemini-t01-a`'s 92.5%, and the paired McNemar test is not significant (p = 0.21).

The probabilities **rank well but are not centred on 0.5**: the mean keep probability of items a human kept is 0.47 (`en`). Any future gate must therefore measure its own threshold on labelled data. It cannot assume the model's 0.5, nor the 0.9 the documentation suggests for acting unattended.

Outcome under the pre-registration: **keep accumulating shadow**. No gate issue is opened yet. The per-site criterion still needs at least 30 shadow decisions per site, and the vault has none at the time of writing.
