# ADR-065: Test Strategy — Three Layers, No Network, Contracts at the Boundaries

**Status:** Accepted
**Date:** 2026-10-22
**Issue:** #326 (epic #320)
**Related to:** [ADR-038: Ask Trajectory Evals by Offline Replay](../QA-WRITING/ADR-038-ask-trajectory-evals-offline-replay.md), [ADR-052: Embedding Providers via Registry](./ADR-052-embedding-providers-registry.md), [ADR-055: Typed Decision Layer in Shadow](../LLM/ADR-055-typed-decision-layer-shadow.md), [ADR-064: Managed Blocks Refuse Malformed Markers](./ADR-064-managed-blocks-refuse-malformed-markers.md)

## Context and Problem Statement

A pipeline that calls LLMs has three kinds of correctness, and each needs its own instrument: does the code do what it says (deterministic, cheap, every commit); does the model do what we want (stochastic, costly, needs human labels); is the human decision well informed (review screens show the evidence). The repository already separated them in practice — 2,000+ `pytest` tests that never call a provider, an offline replay harness for `ask` (ADR-038), gold sets scored with McNemar and Wilson under pre-registered rules (ADR-055) — but no decision recorded it, and the audit of the suite (`docs/artigo/07-testes.md`) found the gaps an unrecorded strategy leaves: no `pytest` configuration, no coverage measurement, the network reachable from tests, thresholds probed far from their boundary, and no test that let one phase consume what the previous one actually wrote.

## Decision Outcome

1. **Three layers, three gates.**
   - *Code*: `pytest tests/` in CI on every push and PR (`.github/workflows/tests.yml`), with Ruff. Never calls a provider, never opens the network.
   - *Observable behaviour, offline*: `zettel.evals.replay` scores recorded `ask` trajectories; tests prove it opens no socket and calls no LLM (ADR-038).
   - *Model quality*: gold sets under `evals/gold/`, rules pre-registered under `evals/preregistration/`, scored by `scripts/score_*` and `scripts/compare_gold_runs.py` (exact McNemar, Wilson intervals, stratum weights). Those scripts are themselves tested. A prompt, threshold or model change is accepted by its measurement, not by green tests.
2. **Seams, not mocks of everything.** LLM calls are replaced where they are consumed (`zettel.connector.prompt.call_llm`, `zettel.article.call_llm`, the `get_llm` of each phase); the decision model through `zettel.decision.client.get_decision_client`; the embedding through a test entry in `index._EF_BUILDERS` (`tests/fakes.py`, ADR-052 addendum) so the real `VectorIndex` and Chroma run. Shared doubles live in `tests/fakes.py`, web helpers in `tests/web_support.py`; test files never import each other. Where the index itself is replaced, it is by `tests.fakes.FakeVectorIndex` or a subclass of it (#327): `tests/test_fakes_contract.py` pins its public methods to `VectorIndex`'s, signature by signature, compares the shape of its search results with the real index on the offline embedding, and requires, by `ast`, that every index fake in the suite derive from it, override only real methods with the real parameter names, and that no test file import another. The migration removed about twenty hand-written fakes; two still offered `upsert_moc`, a method the index lost with the MOC collection (#301).
3. **No network.** `tests/conftest.py` refuses every connection outside loopback and pins LiteLLM to its bundled price map (#324). A test that needs the network does not belong in this suite.
4. **Contracts where the bugs live.**
   - *Between phases*: `tests/test_pipeline_smoke.py` runs `zettel run-all` over a real Markdown source with fixed per-phase LLM answers (#322).
   - *Invariants*: property tests with Hypothesis where the requirement is a property, not an example (`tests/test_managed_blocks_properties.py`, ADR-064).
   - *Thresholds*: every similarity threshold probed at `theta ± 0.005` and `theta`, read from the config (`tests/test_threshold_boundaries.py`, ADR-058 addendum).
   - *Architecture*: `ast`-based tests pin import rules, package layouts and route contracts (`test_cli.py`, `test_web_package.py`, `test_state_package.py`, `test_connector_package.py`, `test_catalog.py`).
5. **Configuration** (`[tool.pytest.ini_options]` in `pyproject.toml`):
   - `--strict-markers --strict-config`: a misspelt marker or ini key is an error.
   - `filterwarnings`: a `DeprecationWarning` attributed to `zettel` is an error; third-party warnings are ignored only by name, each with the reason and the issue that tracks it.
   - `timeout = 300` (`pytest-timeout`): a hung lock or worker fails one test instead of holding CI for an hour.
   - `pytest-randomly` shuffles the order on every run (the seed is printed; `--randomly-seed=N` replays it), so an order dependency surfaces instead of hiding.
   - Marker `slow`: tests whose own body needs UMAP/numba, Docling, scikit-learn or a subprocess, and the pipeline smoke test. `pytest -m "not slow" -n auto` (`pytest-xdist`) is the edit loop; CI runs everything.
6. **Coverage with a floor.** CI runs with `--cov` (branch coverage, `[tool.coverage.*]`) and fails under `fail_under`, set to the measured total rounded down. The floor only rises. Coverage is a floor against losing tests, not a quality target: §6.5 of the audit shows fully covered lines whose tests would not notice a wrong threshold.
7. **Time.** No test asserts a wall-clock budget outside `slow`; a growth rate is tested as a ratio of two sizes, measured interleaved, best of five. Waiting on a background worker is a condition with a generous deadline (`_wait_job` in `tests/test_web.py`), never a fixed number of sleeps.

### Consequences

* Good: a new test inherits the network guard, the warning policy, the timeout and random order without asking for them.
* Good: a failure has one place to be judged — a regression of code fails `pytest`; a regression of quality shows in a measurement with its pre-registered rule.
* Bad: the strict warning policy can break CI on a dependency upgrade that deprecates something we call; that is the intent, and the fix is a code change or a named, justified ignore.
* Neutral: the full suite still takes about five minutes on one core; the parallel fast loop is for editing, CI keeps the whole run.
* Known gap: `PageIndex._spans_other_page` searches the whole document per chunk, so page lookup is still quadratic in practice; its test keeps a wall-clock budget under `slow` until that is fixed.

## Alternatives

* **`pytest-socket`** for the network guard: blocks socket creation, which breaks asyncio's Windows self-pipe and the web `TestClient`; the conftest guard blocks `connect`/`getaddrinfo` outside loopback instead.
* **Recorded cassettes (VCR) for every LLM call**: heavier than per-phase fixed answers for the smoke test; the replay format of ADR-038 stays the place to record real trajectories.
* **Mutation testing in CI**: valuable as an audit (four hand-made mutants were run for #325), too slow for every push; left for periodic runs.

## References

* `pyproject.toml` — `[tool.pytest.ini_options]`, `[tool.coverage.*]`, dev dependency group
* `.github/workflows/tests.yml`
* `tests/conftest.py`, `tests/fakes.py`, `tests/web_support.py`, `tests/test_fakes_contract.py`
* `tests/test_pipeline_smoke.py`, `tests/test_managed_blocks_properties.py`, `tests/test_threshold_boundaries.py`, `tests/test_hermetic.py`
