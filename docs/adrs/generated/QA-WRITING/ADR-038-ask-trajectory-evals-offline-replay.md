# ADR-038: Ask Evaluation as Offline Replay, Isolated from the Production Path

**Status**: Accepted (2026-09-03)

**Depends on:** [ADR-010: Retrieval Result Transparency — Hits vs Candidates](../RETRIEVAL/ADR-010-retrieval-result-transparency-hits-vs-candidates.md)

**Related to:**
- [ADR-003: Hybrid Dense+BM25 Retrieval with RRF Fusion and Absolute Relevance Floor](../INFRA/ADR-003-hybrid-dense-bm25-retrieval.md)
- [ADR-036: A Topic Index for Routing, Fed Back Through the Relevance Floor](../RETRIEVAL/ADR-036-topic-index-routing-not-representation.md)
- [ADR-037: Pre-Flight Cost Estimate as a Pure Function](../CLI/ADR-037-llm-cost-preflight-estimate.md)

## Context and Problem Statement

Several retrieval decisions in this project were made on reasoning rather than measurement: the `0.70` similarity floor, the BM25 bypass rank cutoff, the graph decay, and now the topic-index boost (ADR-036). Each is defensible; none is measured. Worse, when a change *is* made, there is no repeatable way to tell whether it helped, and no vocabulary for *how* it helped — retrieval failures get discussed as one undifferentiated "the answer was bad".

The pieces for measurement already exist and are public: `AskResult` exposes `hits` vs `candidates` (ADR-010), a `floor_reason` string per candidate, `llm_called`, and a `retrieval_params` snapshot. What is missing is a way to record a run, score it against a gold expectation, and get the same numbers back tomorrow.

This is research infrastructure. It must not be able to change production behaviour, and it must not become something CI pays for.

## Decision Drivers

* CI must never call an LLM or open a socket. An eval suite that costs money or flakes on a network is an eval suite that gets disabled.
* Two runs of the same inputs must produce byte-identical output, or a diff cannot be read as a signal.
* A score is meaningless without its envelope: same questions, same models, same thresholds. Comparing across a code change must be *visible*, not assumed away.
* Eval code must not creep into the pipeline. If `zettel ask` ever imports the eval package, the experiment has started shaping the thing it measures.
* Results are committed, so a manifest must never carry a credential.
* Retrieval failures need distinguishable causes, or the measurement adds numbers without adding understanding.

## Considered Options

* A live harness that runs `zettel ask` against a real vault and a real model.
* Offline replay of recorded trajectories, scored deterministically.
* Reuse the existing pytest suite with assertions on `run_ask` output.

## Decision Outcome

**Replay first; live later, and only if replay is green.** `zettel/evals/` holds three modules — `manifest.py` (run identity), `score.py` (verdicts), `replay.py` (the runner and CLI) — and scores JSON trajectories recorded from `AskResult`. `tests/evals/` runs entirely offline; `evals/fixtures|configs|results/` hold small, synthetic, committable artifacts; `.eval-work/` is gitignored for raw trajectories and private vaults.

**Verdicts separate routing from representation**, which is the whole point:

| Verdict | Meaning |
| --- | --- |
| `routing_miss` | the target never entered the candidate pool |
| `floor_reject` | the target *was* retrieved; the floor gated it |
| `answer_fail` | the target was used; the answer missed the declared rubric |
| `ok` | as expected |
| `unknown` | the gold names no target — measured, not guessed |

A no-evidence question is scored on the behaviour worth protecting: empty `hits` must mean the LLM was never called. If it answered anyway, that is `answer_fail`, not a pass.

**The only answer check is a declared substring rubric.** Nothing infers hidden reasoning from the answer string. A gold entry with no `answer_must_contain` is judged on retrieval alone — an honest "we did not specify" rather than a silently permissive pass.

**Identity is a hash of the manifest, and `commit_sha` is part of it.** Two runs with the same identity are comparable; a run after a code change gets a different identity by construction, so a cross-commit comparison has to be a deliberate act rather than an accident. Only the knobs listed in `FLOOR_KEYS` are recorded, in fixed order, so an unrelated config edit does not invalidate a comparison and key ordering cannot perturb the hash.

**`fixture_hash` normalises line endings.** Fixtures are committed text and git rewrites CRLF/LF per platform on checkout, so hashing raw bytes made the same fixture hash differently on Windows and Linux — the committed baseline in `evals/results/` could never have matched on both. Found by rebasing this work onto a fresh checkout, which is exactly the scenario the baseline exists to catch.

**The secret guard reads values, not field names.** An early version rejected its own config because `max_input_tokens` contains "token" — a good reminder that a naive scan produces false positives on legitimate budget knobs. The guard now walks string *values* looking for credential shapes (`sk-`, `api_key`, `Bearer `, `ghp_`).

**Isolation is asserted, not assumed.** A test spawns a subprocess importing `harvester`, `extractor`, `connector` and `ask`, and fails if `zettel.evals` appears in `sys.modules`. Two more tests make `socket.connect` and `llm.call_llm` raise, so a future change that reaches for either fails loudly.

### Positive Consequences

* A change to the floor, the bypass rank or the topic-index boost can be argued with a diff of two result files instead of an anecdote.
* `pytest tests/evals/` needs no key, no vault and no network, so it runs anywhere the rest of the suite runs.
* `trajectory_from_ask_result` is the bridge to a live runner and uses only the public `AskResult` surface, so a live harness will not need new internals exposed.
* The committed baseline in `evals/results/` makes unintended scoring drift show up as a normal code review diff.

### Negative Consequences

* Replay measures the **scorer**, not the retriever. A recorded trajectory is only as truthful as the run that produced it, and the shipped fixture is synthetic — it proves the classifications work, not that the vault retrieves well.
* Gold questions are hand-written and therefore carry the author's assumptions about what a good answer looks like.
* The isolation test spawns a Python subprocess, which is the slowest test in the suite.
* `evals/results/` will accumulate files. Nothing prunes them.

## Pros and Cons of the Options

### Live harness first

* Good, because it measures the real system end to end.
* Bad, because it costs money per run and cannot live in CI.
* Bad, because the scorer would be unverified: a wrong verdict would be indistinguishable from a wrong retrieval.

### Offline replay first (chosen)

* Good, because the scorer is verified before it is trusted to judge anything.
* Good, because it is free, deterministic and CI-safe.
* Bad, because it cannot, on its own, tell you whether retrieval improved.

### Assertions in the existing pytest suite

* Good, because it adds no new structure.
* Bad, because a pass/fail assertion cannot express *how* something failed, which is the distinction being built.
* Bad, because it would tie experiment fixtures to the production test suite's lifecycle.

## Consequences

The manifest accepts a `condition` string, so ablations (`no_graph`, `vector_only`, `lit_only`, `topic_index_off`) can be recorded **one per run** without any code change. None are implemented here; the field exists so that adding one later does not require re-deriving run identity.

A live runner (PD-04 equivalent) is deliberately deferred. When it lands, it should record through `trajectory_from_ask_result` and fail closed against the manifest's `max_calls` / `max_input_tokens` — those fields are already part of the identity for that reason.

**Claims guardrail**: no comparison between conditions may be published without an identical question/model envelope on both sides, and a null result is a valid result. The README says as much; this ADR is where the rule lives.

## References

* `zettel/evals/manifest.py` — `RunManifest`, `build_manifest`, `manifest_identity`, `FLOOR_KEYS`, `hash_files` (LF normalisation), `_reject_secrets`
* `zettel/evals/score.py` — `Verdict`, `GoldQuestion`, `Trajectory`, `score_question`, `score_run`
* `zettel/evals/replay.py` — `replay`, `render`, `trajectory_from_ask_result`, the `python -m` entry point
* `zettel/ask.py` — `AskResult` (`sources`, `candidates`, `llm_called`, `retrieval_params`): the public surface being read
* `evals/fixtures/sintetico-pt/` — 4 synthetic notes, 5 gold questions, 5 recorded trajectories
* `tests/evals/` — identity, the four verdicts, determinism, no-network, no-LLM, no-production-import
