# Component Deep Analysis Report — `config`

## 1. Executive Summary

The `config` component (`zettel/config.py`) is the single Pydantic v2 schema and loader for the entire Zettelkasten pipeline's runtime configuration. It defines `AppConfig` — a nested tree of `BaseModel` subclasses covering paths, LLM/embedding provider settings, chunking, harvest deduplication, extraction filters, literature-review thresholds, image processing, the gardener/MOC pipeline (taxonomic and hub-based), and hybrid retrieval (RRF + relevance floor + graph expansion) — plus `load_config()`, which reads `config/config.yaml` and validates it into that schema. It also hosts two small operational helpers unrelated to schema validation: `setup_logging()` (Rich-based root logger configuration) and `detect_device()` / `get_gpu_info()` (CUDA/CPU device selection and diagnostics).

Architecturally, `config` is one of the two highest-afferent-coupling hubs in the codebase (25 of 38 internal modules import it directly, per `docs_project/architectural-analyzer/architectural-report-2026-08-30_10-22-26.md`), alongside `state.py`. Every CLI command and the web app's job worker instantiate it as the first step of `_load_deps()` / `WebApplication`. It has zero efferent dependency on any other pipeline-phase module (only `pyyaml`, `python-dotenv`, `pydantic`, and lazily `torch`/`rich`), making it the stable base of the dependency graph — the correct direction for shared infrastructure, but also the codebase's single largest blast-radius risk: any schema-loading regression or YAML misconfiguration prevents every command and the web app from starting.

Key findings:
- The component enforces a strict **"YAML is the operational source of truth, Python defaults are only a fallback"** contract, which is itself covered by an automated schema-vs-YAML parity test (`tests/test_config.py`).
- Secrets (API keys, `SESSION_SECRET`) are deliberately kept out of the schema and out of `config.yaml`; they are loaded from `.env` via `python-dotenv` inside `load_config()`, with `override=False` so pre-set process/host environment variables win.
- All filesystem paths in `AppConfig` are normalized to absolute, resolved paths at validation time via a shared `field_validator`, removing any ambiguity for downstream modules about relative-path resolution.
- The component carries almost no business logic beyond validation and defaulting — the "business rules" documented below are primarily validation/normalization contracts and factory-default policies that shape behavior throughout the rest of the pipeline (e.g., embedding dimension safety, duplicate-handling policy defaults, taxonomy strictness).

## 2. Data Flow Analysis

```
1. Process start (CLI command or WebApplication) calls a config entry point:
   - CLI: cli._load_deps(config_path) -> zettel.config.load_config(config_path)
   - Web: WebApplication(config_path) / WebWorker methods call zettel.config.load_config(self.config_path)
     (config_path defaults to os.environ.get("ZETTEL_CONFIG") when not explicitly passed — web.py:100)

2. load_config() side effect: loads .env (python-dotenv, override=False) BEFORE
   reading YAML, so API keys / SESSION_SECRET are in os.environ for any code
   that reads them later (llm.py provider clients, web.py session auth).

3. load_config() resolves the YAML path (explicit path arg, else
   Path("config/config.yaml")), reads it with yaml.safe_load if it exists,
   else logs a warning and proceeds with an empty dict (full factory defaults).

4. Raw dict is splatted into AppConfig(**data). Pydantic v2 validates every
   field:
   - Missing top-level or nested keys fall back to each model's Field default.
   - `field_validator("vault_path", ... , mode="before")` resolves every path
     field to an absolute Path via Path(v).resolve().
   - EmbeddingConfig.dimensions validated >= 1 or None.
   - GardenerConfig.topics_path: "" or None -> None, else resolved absolute Path.
   - Nested sub-configs (LLMConfig, EmbeddingConfig, ChunkingConfig, ...,
     RetrievalConfig with its own nested GraphExpansionConfig /
     RelevanceFloorConfig / AskConfig / ArticleConfig) validate recursively.

5. A fully validated, immutable-by-convention AppConfig instance is returned
   to the caller (cfg).

6. cfg is threaded, largely by explicit parameter passing (not a singleton
   or DI container), into every downstream module: StateDB(cfg.state_db_path),
   VectorIndex(**_idx_kwargs(cfg)) in index.py, get_llm(cfg, ...) in llm.py,
   and directly as cfg.<field> reads inside harvester, extractor, review,
   connector, gardener, gardener_hub, ask, article, sync, new_note,
   purge_source, assets, bibliography, retrieval, graph.

7. setup_logging(cfg.log_level) is called once per CLI invocation
   (cli._load_deps) to configure the root logger (RichHandler on stderr,
   noisy HTTP loggers silenced to WARNING).

8. detect_device(cfg.device) is called on-demand (not at load time) by
   index.py, harvester.py, and the `doctor`/`init` CLI commands whenever a
   Docling or sentence-transformers device needs to be resolved to a concrete
   "cpu"/"cuda" string.
```

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Contract | YAML overrides factory defaults; missing key/file = factory fallback | zettel/config.py:273-300 |
| Validation | Every AppConfig path field resolved to absolute Path | zettel/config.py:259-270 |
| Validation | `embedding.dimensions` must be `>= 1` or `null` | zettel/config.py:46-53 |
| Normalization | `gardener.topics_path` of `""`/`None` normalized to `None` (taxonomy unconfigured); otherwise resolved to absolute Path | zettel/config.py:128-133 |
| Security policy | Secrets never read from `config.yaml`; loaded from `.env` with `override=False` | zettel/config.py:280-286 |
| Contract (test-enforced) | Every leaf field path in the schema must exist in `config/config.yaml`, except an explicit Python-only allowlist | tests/test_config.py:55-67 |
| Safety default | `embedding.allow_fallback` defaults to `False` (fail loudly instead of silently mixing embedding spaces at 384-d) | zettel/config.py:41 |
| Policy default | `harvest.non_interactive_duplicate_action` restricted to `skip`\|`continue`\|`abort`, defaulting to `skip` | zettel/config.py:72 |
| Policy default | `gardener.strict_topics` defaults to `True` (reject MOC topics outside the taxonomy) | zettel/config.py:118 |
| Fallback data | `DEFAULT_RELATION_WEIGHTS` module constant supplies graph edge weights when `retrieval.graph_expansion.relation_weights` is not overridden | zettel/config.py:154-161 |
| Device selection logic | `detect_device()` maps `device: auto\|cpu\|cuda` to a concrete runtime device, with safe CPU fallback if CUDA is requested but unavailable | zettel/config.py:330-356 |
| Logging policy | `setup_logging()` silences `httpx`/`httpcore`/`openai`/`urllib3` to WARNING regardless of `log_level` | zettel/config.py:326-327 |
| Diagnostics | `get_gpu_info()` never raises if `torch` is absent; degrades to `"torch_version": "nao instalado"` | zettel/config.py:379-395 |

### Detailed breakdown of the business rules

---

### Business Rule: YAML-First Configuration Contract

**Overview**:
`load_config()` treats `config/config.yaml` (or an explicitly passed path) as the operational source of truth, and every `Field` default declared in the `AppConfig` model tree as a pure fallback — used only when the YAML file is absent, a specific key is omitted from it, or code constructs `AppConfig()` directly (as most unit tests do).

**Detailed description**:
The implementation is intentionally simple: `load_config()` calls `yaml.safe_load()` on the target file if it exists, keeps the result only if it parses to a `dict` (guarding against a YAML file that is empty or a scalar/list at the top level, which would otherwise crash `AppConfig(**data)`), and then splats that dict directly into `AppConfig(**data)`. Pydantic v2's normal construction semantics do the rest: any key present in the dict overrides the corresponding `Field` default, any key absent uses the default, and any key present but invalid (wrong type, out-of-range value, wrong Literal) raises a `ValidationError` at startup rather than silently coercing or ignoring it. If the file does not exist at all, `load_config()` logs a warning and proceeds with `data = {}`, meaning a fully factory-default `AppConfig` is returned rather than erroring — this makes `zettel doctor` and test suites resilient to a missing config file, but also means a typo'd `--config` path silently produces defaults instead of failing loudly (mitigated by `doctor`'s explicit "Config file" existence check).

This contract has a second half enforced outside `config.py` itself: `tests/test_config.py::test_config_yaml_covers_schema_keys` walks every leaf field path in the `AppConfig` model tree (via `model_fields` recursion) and asserts each one is present as a key in the checked-in `config/config.yaml`, except for a single allowlisted path (`gardener.allowed_topics`, which is a Python/test-only override field, not a knob meant to be curated in the YAML catalog). This means the schema and the operational YAML are kept in lockstep by CI-time verification: adding a new `Field` to any nested config model without also adding the corresponding YAML key (and comment) is a test failure, not a silent gap. This is the mechanism that prevents `config.yaml` from silently drifting out of sync with the schema as the pipeline grows new configurable behaviors.

The practical effect on the rest of the system is that every one of the 25+ consuming modules can rely on `cfg.<field>` always being present and correctly typed after `load_config()` succeeds — no downstream module needs `getattr(cfg, "x", default)` defensive code, and CLAUDE.md explicitly documents this as "each key from the schema must be in the YAML, except `gardener.allowed_topics`."

**Rule workflow**:
```
load_config(path?)
  -> resolve config_path (arg or "config/config.yaml")
  -> if config_path.exists():
         data = yaml.safe_load(file) if it parses to dict, else {}
     else:
         log warning; data = {}
  -> return AppConfig(**data)
       -> Pydantic validates every field: YAML value wins, else Field default
       -> invalid type/value/Literal -> ValidationError raised (startup fails loudly)
Separately, at test time:
  test_config_yaml_covers_schema_keys walks AppConfig.model_fields recursively
  -> asserts every leaf dotted-path exists in config/config.yaml
     (except allowlisted gardener.allowed_topics)
  -> fails the build if schema and YAML diverge
```

---

### Business Rule: Path Normalization to Absolute, Resolved Paths

**Overview**:
Every filesystem-path field on `AppConfig` (`vault_path`, `inbox_path`, `chroma_path`, `state_db_path`, `cache_path`, `prompts_path`) is passed through a shared `field_validator` in `mode="before"` that converts whatever value was supplied (string or `Path`, absolute or relative) into an absolute, symlink-resolved `Path` via `Path(v).resolve()`.

**Detailed description**:
Because `config.yaml` specifies all paths relative to the project root (e.g. `./vault`, `./data/chroma`), and because the CLI can be invoked from different working directories or the web app runs as a long-lived daemon process, relying on relative paths downstream would be fragile — any module receiving `cfg.vault_path` needs it to mean the same location regardless of the current working directory at the time of a particular file operation. By resolving eagerly at config-load time rather than at each point of use, the component guarantees that once `cfg` exists, every consumer (StateDB, VectorIndex, vault.py's I/O helpers, harvester's inbox scanner) sees a canonical absolute path with no further normalization needed.

A related, structurally identical rule governs `gardener.topics_path` (`ArticleConfig.personalities_path` is declared as a plain `Path` field without this validator, so it is **not** independently resolved by Pydantic — it inherits the YAML's `./config/personalities.yaml` relative form unless resolved by its consumer). `GardenerConfig.topics_path` has its own dedicated `field_validator("topics_path", mode="before")` (see the next rule) because it must additionally support a `None`/empty-string sentinel meaning "taxonomy not configured," which the generic path-resolver does not need to handle for the six always-required `AppConfig`-level paths.

This validator runs in `mode="before"`, meaning it operates on the raw YAML-supplied value (str) before Pydantic's own type coercion to `Path` — necessary because `Path(v).resolve()` needs to run on the string/Path input directly rather than on an already-instantiated but unresolved `Path`.

**Rule workflow**:
```
AppConfig(**data) validation, for each of:
  vault_path, inbox_path, chroma_path, state_db_path, cache_path, prompts_path
  -> resolve_path(v) [mode="before"]
       -> return Path(v).resolve()
  -> field stored as absolute, resolved Path
All downstream reads (cfg.vault_path, etc.) are guaranteed absolute.
```

---

### Business Rule: Embedding Dimension Safety Constraint

**Overview**:
`EmbeddingConfig.dimensions` (an optional Matryoshka-Representation-Learning truncation size) must be either `None` (use the model's native dimensionality) or an integer `>= 1`; zero or negative values are rejected at config-validation time.

**Detailed description**:
The `dimensions` field controls MRL truncation for embedding providers that support it (Ollama-native models and OpenAI `text-embedding-3-*` via Chroma's embedding function), and it is a highly consequential setting: CLAUDE.md and the config.yaml comments both note that changing `dimensions` (or the embedding provider/model entirely) invalidates every previously computed vector in the ChromaDB collections, requiring a full `zettel reindex --force`. Given that blast radius, the component defends against the most obviously invalid inputs — a `0` or negative dimension count, which would either be meaningless or would produce a cryptic downstream error from the embedding provider or ChromaDB itself — by validating eagerly at config-load time with a clear PT-BR error message ("`embedding.dimensions deve ser >= 1 (ou null)`").

This is a narrow, defensive validation rather than a semantic one: the validator does not know or check whether a given value is valid for the *specific* configured model (e.g., it would accept `dimensions: 999999` even though no real embedding model supports that), leaving that class of error to surface later, at actual embedding-call time, from the provider or from ChromaDB's dimension-mismatch detection (`peek_stored_embedding_identity` / the `doctor` command's "Embedding space" drift check, and `cli._idx_kwargs`'s `reset_mismatched` handling).

The active `config/config.yaml` sets `dimensions: 1024` against `embedding.provider: ollama` / `model: qwen3-embedding` (native 4096-d truncated to 1024), which the code comments describe as "good cost-benefit" — this is an operational choice documented in the YAML itself, not enforced by the schema.

**Rule workflow**:
```
EmbeddingConfig(dimensions=v) construction
  -> _dimensions_positive(v) [after validator, classmethod]
       -> if v is None: return None (native dimension, no truncation)
       -> if int(v) < 1: raise ValueError("embedding.dimensions deve ser >= 1 (ou null)")
       -> else: return int(v)
Downstream: cfg.embedding.dimensions flows into VectorIndex via
  cli._idx_kwargs / web_app._idx_kwargs -> Chroma collection embedding function
  configuration, and into the embedding-space-drift check in `zettel doctor`
  and cli.py's stored-vs-configured mismatch guard.
```

---

### Business Rule: MOC Taxonomy Path Sentinel (Configured vs. Unconfigured)

**Overview**:
`GardenerConfig.topics_path` distinguishes "taxonomy not configured" (`None`) from "taxonomy configured at a specific path" (a resolved absolute `Path`), treating an empty string in YAML (`topics_path: ""`) as equivalent to `null`, and never leaving it as an unresolved relative path.

**Detailed description**:
The gardener/MOC pipeline (Phase 4) can operate in two very different modes depending on whether a controlled taxonomy of category labels exists: with `topics_path` set, `gardener.py` loads `moc_topics.yaml`, embeds category labels, and (when `strict_topics: true`, the default) rejects any LLM-suggested MOC topic that doesn't substring-match an entry in that taxonomy — this is the codebase's main defense against topic drift/hallucination in auto-generated MOCs. Without a configured taxonomy, that whitelist mechanism has nothing to check against, so the component treats a missing/empty path as a distinct, explicit state (`None`) rather than letting an accidentally-empty string silently resolve to a bogus current-directory path via the generic `Path("").resolve()` behavior (which would resolve to the process's CWD, a footgun the dedicated validator avoids).

The comment on the field is explicit about why this distinction matters operationally: "None ≠ 'use the default'" — a caller who wants the shipped default taxonomy must specify `./config/moc_topics.yaml` (as `config.yaml` does), and a caller who wants to explicitly opt out of taxonomy validation must set it to `null`/omit it, understanding that doing so while `strict_topics: true` remains set will raise a `TaxonomyLoadError` downstream in `gardener.py`/`taxonomy.py` (not in `config.py` itself — this component only encodes the path-resolution/sentinel logic, not the taxonomy-loading failure mode). The `doctor` command surfaces this state directly: when `topics_path is None`, it reports the "MOC taxonomy" check as passing only if `strict_topics` is also `False` (an unconfigured taxonomy is only "healthy" if nothing requires it).

**Rule workflow**:
```
GardenerConfig(topics_path=v) construction
  -> resolve_topics_path(v) [before validator, classmethod]
       -> if v is None or v == "": return None
       -> else: return Path(v).resolve()
Consumers:
  - gardener.py: if topics_path is not None, load_moc_taxonomy(topics_path);
    strict_topics gates whether an out-of-taxonomy LLM topic is rejected.
  - cli.py `doctor`: topics_path is None -> healthy only if not strict_topics;
    topics_path set and exists -> loads taxonomy, reports category count;
    topics_path set but missing file -> reports failure with the path.
```

---

### Business Rule: Secrets Isolation from Versioned Configuration

**Overview**:
API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`) and the web UI's `SESSION_SECRET` are never modeled as `AppConfig` fields and are never read from `config/config.yaml`; `load_config()` loads them from a git-ignored `.env` file via `python-dotenv`, with `override=False` so any secret already present in the real process/host environment (e.g., a Replit secret) takes precedence over `.env`.

**Detailed description**:
This is a deliberate architectural boundary stated in both the module docstring and CLAUDE.md: `config.py` is described as "Segredos ficam no `.env`" (secrets live in `.env`). The mechanism is implemented as a side effect at the very top of `load_config()`, before the YAML is even read: `Path(".env")` is checked for existence, and if present, `load_dotenv(env_path, override=False)` populates `os.environ` from it. The `override=False` argument is significant — it means `.env` acts as a fallback for local development, not an override for deployed environments where secrets are injected directly into the process environment (the CLAUDE.md notes this explicitly for `SESSION_SECRET`: "read from the process environment at startup (`os.environ`), not from `config.yaml`; set it in `.env` or the host secrets (e.g. Replit)"). If neither `.env` exists nor the variable is set in the system environment, downstream code (llm.py's provider clients, web.py's `_session()`) will fail closed — for the web UI, an unset `SESSION_SECRET` means `_session()` always returns `None`, so no login session can ever be issued, rather than falling back to an insecure default.

Because `config.py` never models these values as typed fields, there is no validation, no default, and no schema visibility into whether a required key is present — that responsibility is pushed entirely to the consuming module at the point of use (e.g., `llm.py` raises when a provider's client construction fails due to a missing key; `web.py` reports "no session" rather than crashing). This is a clean separation of concerns (config schema = non-secret operational tuning; environment = secrets) but it does mean `zettel doctor`'s checklist does not currently verify presence of any specific API key for the configured `llm.provider`/`embedding.provider` — a gap noted under Technical Debt below.

**Rule workflow**:
```
load_config(path?)
  -> env_path = Path(".env")
  -> if env_path.exists(): load_dotenv(env_path, override=False)
       (fills os.environ only for keys not already set)
     else: log debug, rely on system env vars only
  -> proceed to read config.yaml as usual (secrets never touch this file)
Downstream:
  - llm.py get_llm(cfg): reads OPENAI_API_KEY / ANTHROPIC_API_KEY / GOOGLE_API_KEY
    from os.environ (via each LangChain provider client's own env lookup),
    not from cfg.
  - web.py: SESSION_SECRET read via os.environ; hmac.compare_digest used for
    timing-safe comparison; unset -> _session() always None (fail closed).
```

---

### Business Rule: Restrictive Enum Defaults for Ambiguous/Risky Operations

**Overview**:
Several fields use `Literal[...]` types with a conservative default chosen to fail safe rather than fail permissive: `embedding.provider` is restricted to three known providers with `allow_fallback: False` by default; `harvest.non_interactive_duplicate_action` defaults to `skip`; `gardener.strict_topics` defaults to `True`; `retrieval.mode` defaults to `hybrid` but is restricted to `Literal["vector", "hybrid"]`.

**Detailed description**:
`EmbeddingConfig.allow_fallback` defaulting to `False` is explained directly in the inline comment: "`False` = erro se faltar key (evita Chroma 384-d)" — i.e., if the configured embedding provider's API key or connection is unavailable, the system should raise an error rather than silently falling back to ChromaDB's built-in default embedding function (which produces 384-dimensional vectors). Silently mixing a 384-d fallback space with the project's actual configured embedding space (e.g. 1024-d Ollama vectors) would corrupt retrieval without any visible error, so the default trades convenience for correctness.

`harvest.non_interactive_duplicate_action` governs what happens when the three-layer duplicate detector (file hash / extraction hash / semantic similarity) flags a candidate source as a likely duplicate while running in a non-interactive context (`--yes`, `--skip-duplicates`, `--force`, or any scripted/CI invocation where a Rich `Prompt` cannot be shown). The `Literal["skip", "continue", "abort"]` restricts the field to exactly three well-understood behaviors, and the chosen default (`skip`) is documented as "mais seguro" (safer) — it avoids the two riskier alternatives of silently ingesting a duplicate (`continue`) or halting an entire multi-file harvest run over one suspected duplicate (`abort`).

`gardener.strict_topics` defaulting to `True` enforces that any MOC topic suggested by the LLM during Phase 4 clustering must substring-match an entry in the curated taxonomy (`moc_topics.yaml`) before being accepted; this is the primary technical control against topic-label hallucination/drift in an otherwise LLM-driven categorization step, and defaulting it "on" means a fresh/misconfigured deployment fails loudly (`TaxonomyLoadError` in the gardener) rather than silently accepting arbitrary LLM-invented categories.

**Rule workflow**:
```
Config validation (no custom validator needed — Literal type + default enforces this):
  embedding.provider in {"openai","sentence-transformers","ollama"}, else ValidationError
  embedding.allow_fallback default False
  harvest.non_interactive_duplicate_action in {"skip","continue","abort"}, default "skip"
  gardener.strict_topics default True
  retrieval.mode in {"vector","hybrid"}, default "hybrid"

Consumption:
  - index.py: allow_fallback=False + missing provider credentials -> raises
    instead of constructing Chroma's default 384-d embedding function.
  - harvester.py `_resolve_duplicate_decision`: non-interactive branch reads
    cfg.harvest.non_interactive_duplicate_action to choose skip/continue/abort.
  - gardener.py: strict_topics gates topic acceptance against the taxonomy.
  - retrieval.py `Retriever`: mode selects hybrid RRF fusion vs. legacy
    vector-only search; degrades to vector-only automatically when
    StateDB.fts_enabled is False regardless of configured mode.
```

---

### Business Rule: Graph Edge-Weight Fallback Table (`DEFAULT_RELATION_WEIGHTS`)

**Overview**:
A module-level constant, `DEFAULT_RELATION_WEIGHTS`, supplies the weight assigned to each typed relation (`contradicts`, `extends`, `depends_on`, `supports`, `exemplifies`, `related`) used when expanding retrieval seeds over the note graph (`graph.py::expand_notes`), and doubles as the `Field(default_factory=...)` for `GraphExpansionConfig.relation_weights` when `config.yaml` doesn't override it.

**Detailed description**:
This is a business rule about *domain semantics*, not just software defaults: the weight ordering encodes the project's belief about which typed relationship between two notes should count most when propagating relevance during graph-based retrieval expansion. `contradicts` is deliberately given the highest weight (`1.0`), with the comment "embedding nao distingue 'apoia' de 'contradiz'" — dense vector embeddings place semantically related-but-opposed notes close together in vector space, so the *typed graph edge* is the only signal available to distinguish "this note supports the topic" from "this note contradicts it," and the system chooses to weight that distinguishing signal maximally rather than treating it as equivalent to a generic topical relation. The other weights form a descending scale of relational strength: `extends`/`depends_on` (0.9, strong structural relations), `supports` (0.8), `exemplifies` (0.7), and finally `related` (0.5, the weakest/most generic "topically connected" edge).

Because this constant is duplicated as both a standalone importable value (used by `graph.py` as its own fallback when `expand_notes` is called without explicit weights) and as the `default_factory` for the nested `GraphExpansionConfig.relation_weights` field, any change to the domain ordering only needs to happen in one place in `config.py`, and both the config-driven path (via `cfg.retrieval.graph_expansion.relation_weights`) and any direct-call path bypassing config stay consistent. `config/config.yaml` currently mirrors these exact same default values under `retrieval.graph_expansion.relation_weights`, meaning the operational YAML does not currently diverge from the factory ordering — an operator could retune this table (e.g., after observing that `exemplifies` is systematically over- or under-weighted) purely via YAML without touching code.

**Rule workflow**:
```
Module load time:
  DEFAULT_RELATION_WEIGHTS = {contradicts:1.0, extends:0.9, depends_on:0.9,
                               supports:0.8, exemplifies:0.7, related:0.5}

GraphExpansionConfig.relation_weights:
  Field(default_factory=lambda: dict(DEFAULT_RELATION_WEIGHTS))
  -> config.yaml retrieval.graph_expansion.relation_weights overrides per-key

Consumption (graph.py expand_notes):
  each traversed edge's relation type -> weight lookup (config value, else
  DEFAULT_RELATION_WEIGHTS fallback) -> combined with hop decay
  (cfg.retrieval.graph_expansion.decay) to score graph-expanded neighbours
  before they re-enter the RRF-fused candidate pool.
```

---

### Business Rule: Device Selection Policy (`detect_device`)

**Overview**:
`detect_device(preference)` resolves the tri-state `device` config field (`"auto" | "cpu" | "cuda"`) to a concrete runtime device string (`"cpu"` or `"cuda"`), applying a safe-fallback policy when `"cuda"` is explicitly requested but unavailable.

**Detailed description**:
This is one of the few pieces of actual decision logic in the component (as opposed to pure schema validation). `"cpu"` is an unconditional force — the function returns `"cpu"` immediately without even checking CUDA availability, useful for explicitly constraining resource-constrained or shared environments. `"cuda"` is a request that is *verified*, not blindly trusted: `_cuda_available()` (a thin wrapper around `torch.cuda.is_available()` that catches `ImportError` and returns `False` if `torch` isn't installed at all) is checked, and only on success does the function return `"cuda"` (logging the detected GPU name via `_gpu_name()`); on failure it logs a warning ("CUDA solicitado mas nao disponivel. Usando CPU.") and falls back to `"cpu"` rather than raising — a deployment with `device: cuda` in `config.yaml` but no GPU driver present will run correctly on CPU, just slower, instead of crashing. `"auto"` (the schema's implied middle ground, though not the YAML's current operational value) performs the same availability probe as the `"cuda"` branch but without the "forced" framing in the log message, defaulting to CPU when no GPU is found.

This function is called lazily by consumers (`index.py`, `harvester.py`, and the `doctor`/`init` CLI paths) rather than being resolved once inside `load_config()` itself — `cfg.device` stays as the raw configured string, and each consumer calls `detect_device(cfg.device)` at the point where it actually needs to construct a Docling pipeline or sentence-transformers model, meaning device availability is (re-)checked per call site rather than cached on the config object. The architectural report flags the currently active `config.yaml` value (`device: cuda`) as an environment-specific hard dependency risk for portability, since `pyproject.toml` only declares `torch`/`torchvision` wheels for `win32`/`linux`.

**Rule workflow**:
```
detect_device(preference="auto")
  -> if preference == "cpu": return "cpu"  (no CUDA probe)
  -> if preference == "cuda":
       -> if _cuda_available(): return "cuda"  (log GPU name)
       -> else: log warning; return "cpu"      (safe fallback, no exception)
  -> else ("auto"):
       -> if _cuda_available(): return "cuda"
       -> else: return "cpu"

_cuda_available(): try import torch; return torch.cuda.is_available();
                    except ImportError: return False   (never raises)
```

---

## 4. Component Structure

`config` is a single-file component: `zettel/config.py`. It is not a package/directory — there is no `config/` Python subpackage; the identically-named `config/` directory at the project root holds the *operational* YAML artifacts consumed by this module, not additional Python source.

```
zettel/
└── config.py                      # Sole implementation file for this component
    ├── LLMConfig                  # LLM provider/model/sampling settings
    ├── EmbeddingConfig            # Embedding provider/model/dimensions + dimension validator
    ├── ChunkingConfig             # Chunk size/overlap/min section length
    ├── LinkingConfig              # RAG topk + dedupe threshold (connect/sync)
    ├── HarvestConfig              # 3-layer dedupe thresholds + ABNT biblio settings
    ├── ExtractionConfig           # Candidate filtering thresholds (Phase 2)
    ├── LiteratureReviewConfig     # Selective approval thresholds (Phase 2b)
    ├── ImagesConfig               # Image extraction/description + rate-limit pacing
    ├── GardenerConfig             # Taxonomic MOC pipeline (Phase 4) + topics_path validator
    ├── HubMocsConfig              # Hub-anchored MOC pipeline (Phase 4b)
    ├── DEFAULT_RELATION_WEIGHTS   # Module-level fallback graph edge-weight table
    ├── GraphExpansionConfig       # Graph BFS expansion (hops/decay/neighbors/weights)
    ├── AskConfig                  # `zettel ask` retrieval/context sizing
    ├── ArticleConfig              # `zettel article` retrieval/outline/judge sizing
    ├── RelevanceFloorConfig       # Absolute similarity floor gating (vector/bm25)
    ├── RetrievalConfig            # Top-level hybrid retrieval config (composes the above)
    ├── AppConfig                  # Root schema: paths + all sub-configs + top-level fields
    │                                 + resolve_path validator (all 6 path fields)
    ├── load_config()              # .env load -> YAML read -> AppConfig(**data)
    ├── setup_logging()            # RichHandler root logger setup, noisy-lib silencing
    ├── detect_device()            # auto|cpu|cuda -> concrete runtime device string
    ├── _cuda_available()          # torch.cuda.is_available() wrapper, ImportError-safe
    ├── _gpu_name()                # torch.cuda.get_device_name(0) wrapper, exception-safe
    └── get_gpu_info()             # Diagnostic dict for `zettel doctor` (torch/CUDA/VRAM)

config/                             # Operational artifacts consumed by this component
├── config.yaml                     # The catalog: every schema leaf key documented + set
├── moc_topics.yaml                 # Taxonomy referenced by gardener.topics_path
└── personalities.yaml              # Referenced by article.personalities_path
```

## 5. Dependency Analysis

```
Internal Dependencies (efferent — what config.py depends on):
  None from within the `zettel` package itself. config.py has Ce = 0 with
  respect to every other pipeline-phase module (harvester, extractor, review,
  connector, gardener*, retrieval, ask, article, sync, vault, state, index,
  llm, schemas, hashing, usage, pricing) — confirmed by the architectural
  report's coupling table (config: 25 afferent / 0 efferent).
  torch is imported lazily and defensively inside detect_device()/get_gpu_info()
  (try/except ImportError), not as a hard dependency.

Internal Dependencies (afferent — what depends on config.py):
  cli.py            -> AppConfig, load_config, setup_logging, detect_device,
                        get_gpu_info (every command; `doctor`/`init` use device
                        + gpu helpers directly)
  web.py            -> os.environ.get("ZETTEL_CONFIG") passed into WebApplication
  web_app.py        -> AppConfig (type hints), load_config (WebWorker._db,
                        WebApplication.cfg property, per-job dispatch)
  state.py, index.py, vault.py, hashing.py, schemas.py, usage.py, pricing.py,
  progress.py                       -> receive AppConfig instances as parameters
  harvester.py, paging.py, extractor.py, review.py, connector.py, gardener.py,
  gardener_assign.py, gardener_hub.py, moc_backrefs.py, sync.py, new_note.py,
  purge_source.py, rebuild.py, assets.py, bibliography.py, retrieval.py,
  graph.py, ask.py, article.py, article_graph.py, chunk_dump.py,
  extraction_dump.py, llm.py         -> read cfg.<field> for behavior control
  (25 internal modules total import zettel.config directly, per the
  architectural report; cfg is otherwise threaded as a plain parameter, not a
  global/singleton)

External Dependencies:
  - PyYAML (yaml.safe_load)          - Parses config/config.yaml
  - python-dotenv (load_dotenv)      - Loads .env into os.environ (secrets)
  - Pydantic v2 (BaseModel, Field,
    field_validator)                 - Schema definition, validation, coercion
  - Rich (RichHandler, Console)      - setup_logging()'s log handler (lazy import)
  - PyTorch (torch, optional)        - CUDA availability/diagnostics (lazy import,
                                        ImportError-tolerant; absence degrades
                                        gracefully, never crashes config loading)

Operational (non-code) dependency:
  - config/config.yaml                - The checked-in operational catalog this
                                         component's tests assert full coverage
                                         against (tests/test_config.py)
```

## 6. Afferent and Efferent Coupling

Coupling is measured at the class/model level within `config.py`, since this is Python/Pydantic (object-oriented) code. "Afferent" counts distinct other classes/modules in the codebase that reference the given model by name or via its field path; "Efferent" counts distinct other config models each class itself composes.

| Component (class) | Afferent Coupling | Efferent Coupling | Critical |
|--------------------|-------------------|--------------------|----------|
| `AppConfig` | 25 (nearly every internal module) | 11 (all top-level sub-configs) | Critical |
| `LLMConfig` | ~13 (llm.py, extractor, connector, gardener, gardener_hub, ask, article, article_graph, bibliography, assets, web.py, cli.py) | 0 | High |
| `EmbeddingConfig` | 3 (cli._idx_kwargs, web_app._idx_kwargs, index.py) | 0 | High |
| `RetrievalConfig` | 3 (retrieval.py, ask.py, article.py) | 4 (GraphExpansionConfig, RelevanceFloorConfig, AskConfig, ArticleConfig) | High |
| `GraphExpansionConfig` | 2 (retrieval.py, graph.py) | 0 (uses module-level `DEFAULT_RELATION_WEIGHTS` as default factory) | Medium |
| `RelevanceFloorConfig` | 1 (retrieval.py `_apply_relevance_floor`) | 0 | Medium |
| `GardenerConfig` | 3 (gardener.py, gardener_assign.py, cli.py `doctor`) | 0 | Medium |
| `HubMocsConfig` | 1 (gardener_hub.py) | 0 | Low |
| `HarvestConfig` | 2 (harvester.py, cli.py harvest flags) | 0 | Medium |
| `ImagesConfig` | 1 (assets.py) | 0 | Low |
| `ExtractionConfig` | 1 (extractor.py) | 0 | Low |
| `LiteratureReviewConfig` | 2 (extractor.py, review.py) | 0 | Medium |
| `ChunkingConfig` | 2 (harvester.py, paging.py) | 0 | Low |
| `LinkingConfig` | 2 (connector.py, sync.py) | 0 | Low |
| `AskConfig` | 1 (ask.py) | 0 | Low |
| `ArticleConfig` | 2 (article.py, article_graph.py) | 0 | Low |

Note: `AppConfig` itself is the dominant coupling point by a wide margin (matching the architectural report's headline finding); the nested sub-config classes exist primarily to give each pipeline phase a scoped, independently-testable slice of the tree rather than to spread coupling evenly.

## 7. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| `config/config.yaml` | Local file (filesystem) | Operational configuration source of truth | Direct file read | YAML | Missing file -> warning + factory defaults; malformed YAML/non-dict top level -> empty dict fallback (via `isinstance(raw, dict)` guard); invalid values -> Pydantic `ValidationError` propagates uncaught |
| `.env` | Local file (filesystem) | Secrets (API keys, `SESSION_SECRET`) injected into `os.environ` | Direct file read | dotenv KEY=VALUE | Missing file -> debug log, proceeds with system env vars only; `override=False` means pre-set env vars always win |
| `ZETTEL_CONFIG` env var | Environment variable | Lets the web app point at an alternate config path (tests, alternate deployments) | Process environment | String path | Absent -> falls back to default `config/config.yaml` resolution inside `load_config` |
| PyTorch / CUDA runtime | Optional local ML runtime | Device capability probing for `detect_device`/`get_gpu_info` | In-process import + API calls | N/A | `ImportError` caught explicitly everywhere `torch` is touched; broad `except Exception` around GPU name lookup in `_gpu_name()` |

`config` exposes no network endpoints of its own — it is a pure library component consumed in-process. (Per the report template, the Endpoints section is omitted because this component does not expose REST/GraphQL/gRPC endpoints; those live in `web.py`.)

## 8. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Schema/DTO via Pydantic models | `AppConfig` and 15 nested `BaseModel` subclasses | zettel/config.py:22-227 | Typed, self-validating configuration tree with IDE/type-checker support |
| Factory function | `load_config()` | zettel/config.py:273-300 | Single construction point that layers YAML over Field defaults, isolating file I/O and `.env` loading from the schema definition itself |
| Composite / nested value object | `RetrievalConfig` composing `GraphExpansionConfig`, `RelevanceFloorConfig`, `AskConfig`, `ArticleConfig`; `AppConfig` composing all top-level sub-configs | zettel/config.py:214-252 | Groups related settings into scoped, independently reusable/testable units mirroring the pipeline's phase boundaries |
| Before-validator normalization | `field_validator(..., mode="before")` on path fields and `dimensions`/`topics_path` | zettel/config.py:46-53, 128-133, 259-270 | Normalizes/validates raw YAML input (strings) before Pydantic's default type coercion, centralizing path-resolution and sentinel-value logic |
| Fallback constant / default factory | `DEFAULT_RELATION_WEIGHTS` + `Field(default_factory=lambda: dict(...))` | zettel/config.py:154-173 | Single source of truth for a domain-semantic default table, shared between the config schema and direct callers of `graph.expand_notes` |
| Strategy-selector enums | `Literal[...]` fields (`embedding.provider`, `harvest.non_interactive_duplicate_action`, `retrieval.mode`, `hub_mocs.selection_mode`) | zettel/config.py:37, 72, 139, 221 | Constrains configuration to a closed, statically-checkable set of supported behaviors, delegating the actual strategy dispatch to consumer modules (llm.py, index.py, retrieval.py) |
| Lazy/deferred import | `torch` inside `_cuda_available`/`_gpu_name`/`get_gpu_info`; `rich` inside `setup_logging` | zettel/config.py:309-310, 361-362, 371-372, 382-383 | Keeps `config.py` importable (and thus every dependent module importable) even when optional heavyweight dependencies (PyTorch) are not installed |
| Facade over environment + file config | `load_config()` unifying `.env`, YAML, and Field defaults behind one call | zettel/config.py:273-300 | Gives every consumer a single, simple entry point instead of scattering `os.environ`/YAML-parsing logic across the codebase |

## 9. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|------------------|-------|--------|
| Critical | `AppConfig` (whole component) | Highest afferent coupling in the codebase (25 dependents); no other module can start without a successful `load_config()` call | A schema regression, YAML syntax error, or an added required field without a default takes down the entire CLI and web app simultaneously |
| High | `_idx_kwargs` duplicated in `cli.py` and `web_app.py` | Both functions independently mirror the same `cfg.embedding.*`/`cfg.chroma_path`/`cfg.device` -> `VectorIndex` kwarg mapping (CLAUDE.md explicitly calls out that `web_app._idx_kwargs` "must mirror `cli._idx_kwargs`"), but there is no shared helper enforcing this in code — only documentation and human discipline | A future field added to one copy (e.g. a new embedding kwarg) but not the other silently causes the web UI and CLI to construct `VectorIndex` differently, risking embedding-space mismatches that only `doctor`'s drift check would catch, and only if run |
| Medium | Secrets presence not validated | `zettel doctor` checks config file/vault/prompts/dependencies/device/taxonomy/embedding-drift, but does not verify that the API key required by the *currently configured* `llm.provider`/`embedding.provider` is actually set in the environment | A misconfigured or missing API key surfaces only at the first real LLM/embedding call deep in a pipeline run, rather than at `doctor` time |
| Medium | No runtime re-validation / hot reload | `AppConfig` is loaded once per process invocation (or per `load_config()` call in the web worker, which re-reads on each job); there is no mechanism to detect or react to a `config.yaml` edit made while a long-running web app process is up, beyond the fact that `WebApplication`/`WebWorker` happen to call `load_config()` fresh per job | A config change intended to take effect immediately in the running web app requires understanding exactly which code paths re-read config the value of the change; some singletons cached elsewhere (e.g. an already-open `StateDB`/`VectorIndex`) will not reflect it |
| Low | `ArticleConfig.personalities_path` not covered by the shared path-resolution validator | Unlike the six `AppConfig`-level paths and `GardenerConfig.topics_path`, this field is declared as a plain `Path` with no `field_validator`, so it is only implicitly resolved relative to CWD by whatever consumes it | Inconsistent path-resolution guarantees across the schema; a future refactor could assume all `Path` fields on the config tree are pre-resolved and be surprised by this one exception |
| Low | Environment-specific operational default (`device: cuda`) documented but not schema-enforced | The schema accepts `"auto"|"cpu"|"cuda"` uniformly; the *portability* risk (torch/torchvision only packaged for win32/linux per `pyproject.toml`) lives entirely in the currently-committed YAML value, not in any schema-level guard or warning | A deployment to an unsupported OS (e.g. macOS) with the checked-in `config.yaml` unmodified will hit failure or degraded behavior only at `detect_device`/Docling-init time, not at config-load time |
| Low | `.env` loaded with a relative path (`Path(".env")`) | `load_config()` resolves `.env` relative to the process's current working directory, not relative to the repository root or to `config.py`'s own location | Invoking any `zettel` command from a directory other than the project root silently skips `.env` loading (falls through to "usando apenas variaveis de ambiente do sistema"), which could surprise a user running commands from a subdirectory |

## 10. Test Coverage Analysis

| Component | Unit Tests (direct) | Integration Tests (indirect, via `AppConfig()`/`load_config`) | Coverage | Test Quality |
|-----------|----------------------|------------------------------------------------------------------|----------|----------------|
| `load_config` / YAML-schema parity | `tests/test_config.py::test_load_config_yaml_smoke`, `::test_config_yaml_covers_schema_keys` | Indirectly exercised by every test file that calls `load_config` or constructs `AppConfig()` (46 occurrences across 23 test files, e.g. `tests/test_rebuild.py` x8, `tests/test_harvester_sections.py` x5, `tests/test_article.py`/`test_article_graph.py`/`test_ask.py` x3-4 each) | Good for the parity/coverage contract; the smoke test only spot-checks a handful of nested fields (`retrieval.mode`, `relevance_floor.min_vector_similarity`, `hub_mocs.selection_mode`, one `relation_weights` key) rather than every leaf value | Strong structural guarantee (schema-vs-YAML key parity is exhaustive and automated via `model_fields` recursion), but the smoke assertions are shallow spot-checks, not exhaustive value verification |
| `EmbeddingConfig.dimensions` validator | No dedicated unit test found | Exercised implicitly wherever `AppConfig()`/`load_config` runs with the default/YAML `dimensions: 1024` | Low (happy-path only) | No test found asserting the `ValueError` is actually raised for `dimensions: 0` or a negative value |
| `GardenerConfig.topics_path` sentinel (`""`/`None` -> `None`) | No dedicated unit test found | Indirect, via `tests/test_gardener.py` (2 `AppConfig()` uses) and `tests/test_gardener_hub.py` (2 uses), which likely rely on the resolved default path | Low-Medium | No test found directly asserting `topics_path=""` or `topics_path=None` both normalize to `None` |
| Path resolution validator (`resolve_path`) | No dedicated unit test found | Implicit in every test constructing `AppConfig` with path overrides (e.g. `tests/test_rebuild.py`, `tests/test_sync.py`, `tests/test_set_paging.py` use temp directories for `vault_path`/`state_db_path`/`chroma_path`) | Medium (behavior relied upon heavily, but not directly asserted) | Tests depend on the resolution behavior working correctly (temp-dir paths must resolve consistently) but don't explicitly assert `cfg.vault_path.is_absolute()` or similar |
| `detect_device` / `_cuda_available` / `_gpu_name` / `get_gpu_info` | No dedicated unit test found anywhere in `tests/` | None found | None | Untested; risk is mitigated by defensive `try/except ImportError`/`except Exception` inside the functions themselves, but the `"cpu"`/`"cuda"`/`"auto"` branch logic in `detect_device` has no automated coverage |
| `setup_logging` | No dedicated unit test found | None found | None | Untested; low risk given it only configures logging handlers with no business-logic branching beyond the noisy-logger list |
| Secrets loading (`.env` / `override=False`) | No dedicated unit test found | None found directly; `tests/test_web_state.py` (2 `AppConfig()` uses) tests web session/job state but does not appear to test the `.env`-loading side effect of `load_config` itself | None | Untested at the `config.py` level; `web.py`'s `SESSION_SECRET`-absent behavior may be covered elsewhere (not confirmed within this component's boundary) |

Overall assessment: the component's **structural** contract (schema completely covers/is covered by the operational YAML) is well-tested via an automated, exhaustive parity check — a notably strong practice. However, the component's **behavioral** edge cases (validator failure paths, the `topics_path` sentinel, `detect_device`'s three branches, and the `.env`/secrets-loading side effect) have no dedicated unit tests in `tests/test_config.py`; what confidence exists for those paths is incidental, arising from the fact that dozens of other test files happen to construct `AppConfig()` or call `load_config()` along their own happy paths.
