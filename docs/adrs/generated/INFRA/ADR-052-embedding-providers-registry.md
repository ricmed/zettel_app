# ADR-052: Embedding Providers via Registry, Asymmetric Query Side, Unit-Norm Contract

**Status:** Accepted  
**Date:** 2026-09-21  
**Related to:** [ADR-002](./ADR-002-chromadb-embedded-vector-store.md), [ADR-003](./ADR-003-hybrid-dense-bm25-retrieval.md), [ADR-024](../LLM/ADR-024-multi-provider-llm-strategy.md)

## Context and Problem Statement

`embedding.provider` accepted `openai | sentence-transformers | ollama`, spelled out three times: the `EmbeddingConfig.provider` Literal, a `_SUPPORTED_PROVIDERS` tuple and an `if/elif` chain in `VectorIndex._build_embedding_fn`. Adding Google's `gemini-embedding-001` — the LLM side already runs on Gemini (ADR-050) and `langchain-google-genai` is installed — meant touching all three, the same debt ADR-024 records for chat providers.

Gemini also differs from the three existing providers in two ways that matter for retrieval:

1. **It is asymmetric.** The API takes a `task_type`; a stored passage should be embedded as `RETRIEVAL_DOCUMENT` and a question as `RETRIEVAL_QUERY`. The old Ollama adapter answered Chroma's `embed_query` hook with the document path, so the distinction had nowhere to go.
2. **It is not unit-normalized below 3072 dimensions.** With `output_dimensionality` reduced (MRL), vectors come back with norm != 1. Every similarity the system reasons about — the relevance floor's `1 - distance/2`, `dedupe_threshold`, `corroborates_min_similarity` — assumes unit vectors under Chroma's default squared L2 (see ADR-003; `scripts/probe_relevance_floor.py` checks it).

## Decision Outcome

**Chosen:** a provider registry plus one LangChain-backed adapter with explicit query and normalization contracts.

- `zettel/index.py` holds `_EF_BUILDERS: dict[str, builder]` (`openai`, `sentence-transformers`, `ollama`, `gemini`). `_build_embedding_fn` is a lookup; the unknown-provider error lists the registry. Adding a provider is one entry and one `_build_*_ef` method. `tests/test_index.py` pins the registry to the `EmbeddingConfig.provider` Literal so they cannot drift.
- `_LangChainChromaEF` adapts any LangChain `Embeddings` client to Chroma: `__call__` -> `embed_documents`, `embed_query` -> the client's `embed_query`. Chroma 1.5.9 calls `embed_query` from `collection.query`, so every retrieval question takes the query side with no change at call sites. `VectorIndex.embed_texts` stays on the document side: all its callers (gardener category labels, connect's candidate-to-category assignment, extract's intra-batch dedupe) compare document with document.
- `normalize = True` on an adapter L2-normalizes both sides. Gemini sets it; Ollama keeps it off (qwen3-embedding already returns unit vectors, and the adapter keeps Chroma's persisted name `ollama`, so the existing store opens without a reindex).
- The Gemini adapter persists under the name `zettel_gemini` (Chroma ships its own `google_*` EFs), reads `GOOGLE_API_KEY` or `GEMINI_API_KEY` and never writes the key into the config Chroma stores on disk. A missing key fails fast unless `embedding.allow_fallback`, like OpenAI.
- **Transient errors are retried in the adapter.** LangChain builds the `google-genai` client without `retry_options`, and the SDK then makes a single attempt: the first `429 RESOURCE_EXHAUSTED` aborted a `run-all` in the garden phase after every earlier phase had spent its quota. `_LangChainChromaEF._with_retry` retries (tenacity, exponential backoff 1s -> 60s with jitter, 6 attempts) only what `_is_transient` accepts; the Gemini adapter accepts `408/429/5xx` found anywhere in the exception chain, since LangChain wraps the SDK error in `GoogleGenerativeAIError`. A `400` fails at once. A per-minute limit resolves itself; an exhausted daily quota still fails, after about two minutes.
- Credential readiness moves to `zettel/credentials.py`, shared by the web settings page and `zettel doctor` ("Embedding credential").

## Consequences

* Good, because switching to Gemini is a config change (`provider: gemini`, `model: gemini-embedding-001`, `dimensions: 768|1536|3072`) followed by the existing drift flow (`reindex --force`).
* Good, because the unit-norm assumption behind every similarity threshold is now enforced by the adapter instead of hoped for.
* Neutral, because the vector-space identity is still `provider/model/dimensions`; `task_type` and normalization are fixed per provider, so they need no marker of their own.
* Bad, because thresholds do not transfer between models: `retrieval.relevance_floor`, `retrieval.chapter_floor`, `linking.dedupe_threshold` and `linking.corroborates_min_similarity` must be re-measured with `scripts/probe_relevance_floor.py` after a switch. The probe already measures through `collection.query`, i.e. the production query side.
* Bad, because with an asymmetric model every `collection.query` embeds its text as a question — including connect's RAG search, where the "question" is a candidate note. `corroborates_min_similarity` (0.85) and the dedupe thresholds were measured on a symmetric model and are likely too strict under Gemini until re-measured.

## References

- `zettel/index.py` — `_LangChainChromaEF`, `_OllamaChromaEF`, `_GeminiChromaEF`, `_EF_BUILDERS`
- `zettel/credentials.py` — `EMBEDDING_PROVIDER_ENV`, `embedding_ready`
- `tests/test_index.py` — registry/Literal pin, Gemini query routing and normalization, key never persisted
