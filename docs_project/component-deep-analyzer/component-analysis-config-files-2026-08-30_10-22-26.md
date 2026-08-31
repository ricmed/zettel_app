# Component Deep Analysis Report — Config Files

## 1. Executive Summary

The "config files" component is not a code module but a set of three **operational YAML data contracts** that parameterize the entire Zettelkasten pipeline:

| File | Role | Validated by | Consumed by |
|------|------|---------------|-------------|
| `config/config.yaml` | Single operational source of truth for every runtime knob (paths, LLM, embeddings, chunking, dedupe, harvest, extraction, review, images, gardener, hub MOCs, hybrid retrieval, language, logging, PDF/device) | `zettel/config.py` — `AppConfig` (Pydantic v2), via `load_config()` | Every pipeline module (`harvester.py`, `extractor.py`, `review.py`, `connector.py`, `gardener.py`, `gardener_hub.py`, `retrieval.py`, `ask.py`, `article.py`, `sync.py`, `cli.py`, `web_app.py`, `web.py`, …) |
| `config/moc_topics.yaml` | Hierarchical taxonomy (pilar → categoria → topicos) used as the MOC category whitelist and as LLM prompt reference material | `zettel/taxonomy.py` — `MocTaxonomy` / `Pilar` / `Categoria` (Pydantic v2), via `load_moc_taxonomy()` | `zettel/gardener.py` (taxonomic clustering, `strict_topics` validation), `zettel/gardener_assign.py` (category embedding/assignment), referenced by path from `config.yaml` → `gardener.topics_path` |
| `config/personalities.yaml` | Named style/tone presets (temperature + prompt fragment) for the article-writing personality rewrite step | **No Pydantic schema** — loaded as a raw `dict` by `zettel/article.py::load_personalities()` | `zettel/article.py::apply_personality_rewrite()` (article pipeline's personality node), referenced by path from `config.yaml` → `retrieval.article.personalities_path` |

**Key findings:**

1. `config/config.yaml` and `zettel/config.py::AppConfig` are **contractually coupled by an automated test** (`tests/test_config.py::test_config_yaml_covers_schema_keys`), which asserts every schema leaf path exists in the YAML except the explicit code-only allowlist (`gardener.allowed_topics`). This test currently **passes** — the two files are in sync as of this analysis.
2. `config/moc_topics.yaml` structurally matches `taxonomy.py`'s `MocTaxonomy` model and is exercised by a dedicated smoke test (`test_load_project_moc_topics_yaml`) that skips gracefully if the file is absent — it currently **passes**.
3. `config/personalities.yaml` is the only one of the three files with **no schema/validation layer**: it is read as an untyped dict, defended only by `.get()` fallbacks and a hardcoded `neutral` default inside `load_personalities()`. A malformed or missing key (e.g. absent `style_prompt`) degrades silently rather than failing loudly.
4. `zettel doctor` (the CLI's config/dependency health check) verifies the existence of `config/config.yaml`, the vault, the inbox, and all prompt template files — but does **not** check `moc_topics.yaml` or `personalities.yaml`, even though both are referenced by path from `config.yaml` and their absence produces different failure modes at very different times (`TaxonomyLoadError` raised eagerly inside `garden` when `strict_topics: true`, vs. a silent single-profile fallback inside `article`).
5. `config.yaml` intentionally carries many values that differ from `AppConfig`'s Pydantic field defaults (e.g. `llm.temperature: 0.15` vs. schema default `0`, `embedding.provider: ollama` vs. schema default `openai`, `gardener.min_notes_for_moc: 10` vs. schema default `3`). This is the designed operating model per `CLAUDE.md`/the file's own header comment ("FONTE OPERACIONAL UNICA … zettel/config.py … so preenche chave ausente") — not a defect, but worth surfacing explicitly since a reader diffing schema vs. YAML would otherwise mistake every such delta for drift.

---

## 2. Data Flow Analysis

Configuration data is **read-only, load-once-per-process** data — it does not flow through a request pipeline the way business data does. Its "flow" is: file → parse → validate/structure → in-memory config object → passed by reference into every pipeline stage.

```
config/config.yaml
  1. Read from disk by zettel.config.load_config(path) [zettel/config.py:273]
     - .env loaded first (python-dotenv) so LLM/API-key env vars are available
     - yaml.safe_load() -> dict (or {} if file missing, with a logged warning)
  2. dict unpacked into AppConfig(**data) [zettel/config.py:300]
     - Pydantic validates types, applies field_validators (path resolution,
       embedding.dimensions >= 1, gardener.topics_path resolution)
     - Any YAML key absent from the schema is silently ignored by Pydantic
       (not a validation error) unless a stricter model config is set
       (none is; AppConfig uses default "ignore-or-allow" behavior for extras)
  3. cli.py::_load_deps() / web_app.py holds the resulting AppConfig instance
     and threads it through every command:
       - harvest -> HarvestConfig, ChunkingConfig, ImagesConfig, paths
       - extract -> ExtractionConfig, LLMConfig
       - review  -> LiteratureReviewConfig
       - connect -> LinkingConfig, RetrievalConfig (RAG)
       - garden  -> GardenerConfig (+ config/moc_topics.yaml via topics_path)
       - garden --hubs -> HubMocsConfig
       - ask/article -> RetrievalConfig.ask / RetrievalConfig.article
                          (+ config/personalities.yaml via personalities_path)
       - sync    -> RetrievalConfig (suggestion RAG)
  4. VectorIndex / StateDB construction reads embedding.* and *_path fields
     to open Chroma/SQLite with the correct embedding space and file location

config/moc_topics.yaml (indirect, via gardener.topics_path)
  1. gardener.py / gardener_assign.py call
     taxonomy.resolve_allowed_topics(cfg.gardener.topics_path, cfg.gardener.allowed_topics,
                                      strict=cfg.gardener.strict_topics)
  2. taxonomy.load_moc_taxonomy(path) reads + yaml.safe_load()s the file
     -> MocTaxonomy.model_validate(raw) (Pydantic)
  3. allowed_topic_names(tax) -> flat list of categoria names (MOC whitelist)
     format_taxonomy_for_prompt(tax) -> markdown block injected into the
       moc_generation / moc_incremental LLM prompts
  4. Category label embeddings (category_label_template.format(domain=..., categoria=...))
     are computed once per garden run and used to assign notes to buckets
     before clustering (gardener_assign.py)

config/personalities.yaml (indirect, via retrieval.article.personalities_path)
  1. article.py::apply_personality_rewrite() is called at the end of the
     article-writing graph (article_graph.py), after assembly, before the
     judge loop
  2. If personality_id == "neutral" and no custom_style_notes -> short-circuit,
     no file read, no LLM call (documented no-op)
  3. Otherwise: load_personalities(Path(art_cfg.personalities_path)) parses
     the YAML into {id: {name, temperature, style_prompt}}
  4. profile = profiles.get(pid) or profiles.get("neutral") or <inline fallback dict>
  5. style_prompt / temperature feed prompts/article_personality.md via
     fill_template(), then _cached_llm() executes the rewrite
```

---

## 3. Business Rules & Logic

### Overview of the business rules

| Rule Type | Rule Description | Location |
|-----------|-------------------|----------|
| Contract enforcement | Every `AppConfig` schema leaf must have a corresponding key in `config/config.yaml`, except the explicit code-only allowlist | `zettel/config.py` (schema) + `tests/test_config.py:15,59-67` |
| Fallback semantics | `config/config.yaml` is the sole operational source; a missing file or missing key falls back to the Pydantic `Field` default, never to a second config file | `zettel/config.py:273-300` |
| Secret exclusion | API keys/secrets must never live in `config.yaml`; they come from `.env` / process environment (`SESSION_SECRET`, `OPENAI_API_KEY`, etc.) | `zettel/config.py:280-286`; `config/config.yaml:7-10` header comment |
| Path normalization | All path-typed config fields (`vault_path`, `inbox_path`, `chroma_path`, `state_db_path`, `cache_path`, `prompts_path`, `gardener.topics_path`) are resolved to absolute paths at validation time | `zettel/config.py:259-270`, `128-133` |
| Embedding dimension guard | `embedding.dimensions` must be `null` or a positive integer | `zettel/config.py:46-53` |
| Embedding-space immutability | Changing `embedding.provider`/`embedding.model`/`embedding.dimensions` invalidates existing Chroma vectors; requires `zettel reindex --force` | `config/config.yaml:33-37` (comment); enforced operationally in `cli.py`/`index.py` (embedding-mismatch detection), not by a config-file validator |
| Taxonomy whitelist enforcement | A MOC's `topic` must match a `categoria.nome` in `moc_topics.yaml` when `gardener.strict_topics: true`; otherwise the MOC is rejected | `zettel/taxonomy.py:73-104`; `config/config.yaml:109` |
| Taxonomy load failure mode | Missing/invalid taxonomy file + `strict_topics: true` raises `TaxonomyLoadError` (hard stop); + `strict_topics: false` logs a warning and falls back to an "allow-all" empty whitelist | `zettel/taxonomy.py:91-104` |
| Personality fallback chain | Requested personality id → exact match → `neutral` → inline hardcoded default dict, always resolves to *something* even for a corrupt/missing file | `zettel/article.py:1055-1094` |
| Personality no-op rule | `personality == "neutral"` with no custom style notes skips the file read and the LLM call entirely (cost/latency optimization + deterministic default) | `zettel/article.py:1084-1087` |
| Default personality reference integrity (implicit) | `retrieval.article.default_personality` (config.yaml value: `neutral`) must exist as a key in `personalities.yaml`; nothing enforces this at load time | `config/config.yaml:176`; `config/personalities.yaml:2` |
| MOC category label composition | Category text embedded for note-to-bucket assignment is built from a template combining `gardener.domain` and each taxonomy `categoria.nome` | `config/config.yaml:108,112`; `zettel/gardener.py:113-130` |
| Relevance-floor calibration note | `retrieval.relevance_floor.min_vector_similarity` (0.70) is explicitly documented as empirically calibrated per corpus/embedding model and must be retuned on embedding-model change | `config/config.yaml:139,145` |
| Doctor check scope | `zettel doctor` validates config file existence, vault/inbox paths, all prompt template files, FTS5 availability, and dependency imports — but does not validate `moc_topics.yaml` or `personalities.yaml` | `zettel/cli.py:1750-1826` |

### Detailed breakdown of the business rules

---

### Business Rule: Schema-YAML Parity Contract

**Overview**:
`config/config.yaml` is declared the single operational source of truth for the pipeline, while `zettel/config.py`'s `AppConfig` Pydantic model exists purely to supply types, validators, and factory-default fallbacks for missing keys or a missing file (e.g., in unit tests that instantiate `AppConfig()` directly). To prevent these two artifacts from drifting apart — a new config field added to the schema but forgotten in the YAML, or vice versa — the project enforces the relationship with an automated test rather than a code-level runtime check.

**Detailed description**:
`tests/test_config.py::test_config_yaml_covers_schema_keys` walks the `AppConfig` model tree recursively (`schema_leaf_paths`), producing every dotted leaf path (e.g. `retrieval.relevance_floor.min_vector_similarity`), unwrapping `Optional`/`Union` annotations to find nested `BaseModel`s. It then parses the live `config/config.yaml` with `yaml.safe_load` and checks, for every leaf path, whether that dotted path exists as nested keys in the YAML (`yaml_has_path`). Any leaf path missing from the YAML fails the test, with one explicit, intentional exception: `gardener.allowed_topics`, which is documented in both the schema (`zettel/config.py:116-117`, "Override de testes; nao e knob do config.yaml") and the test module (`_PYTHON_ONLY_PATHS`) as a field that exists purely for test injection and has no meaning as an operational YAML knob (the real whitelist source is `gardener.topics_path`).

This rule matters because `AppConfig`'s field defaults and the YAML's actual values are frequently and deliberately different — the defaults exist as safe fallbacks for `AppConfig()` bare instantiation in unit tests, not as the values the running pipeline should use. Without the parity test, a developer adding a new tunable (e.g. a new `HubMocsConfig` field) could ship code that silently uses the Pydantic default in production because nobody remembered to add the corresponding line to `config/config.yaml`. Because `load_config()` uses `AppConfig(**data)` (whole-model construction from the parsed dict) rather than merging per-field, a key omitted from the YAML does not raise at load time — it is only caught by this repository-level test, not by any runtime guard.

A companion smoke test, `test_load_config_yaml_smoke`, loads the real `config/config.yaml` through `load_config()` and asserts a handful of representative values resolve correctly (`retrieval.mode == "hybrid"`, `relevance_floor.min_vector_similarity == 0.70`, `hub_mocs.selection_mode` is a valid literal, `"contradicts"` is present in `relation_weights`). This is a narrower, values-level regression guard distinct from the exhaustive keys-level guard above.

**Rule workflow**:
```
CI/local test run
  -> AppConfig model tree walked recursively (schema_leaf_paths)
  -> config/config.yaml parsed with yaml.safe_load
  -> for each schema leaf path not in the allowlist:
       assert path exists as nested dict keys in the parsed YAML
  -> failure lists every missing dotted path in the assertion message,
     telling the developer to either add it to config.yaml or add it
     to the code-only allowlist
```

---

### Business Rule: Config File as Sole Operational Source / Field Defaults as Fallback Only

**Overview**:
The header comment of `config/config.yaml` states explicitly: "FONTE OPERACIONAL UNICA: edite este arquivo" (single operational source: edit this file). `zettel/config.py`'s docstring reiterates that Field defaults are a "fallback de fabrica" used only for a missing file or a missing individual key, and for bare `AppConfig()` construction in tests.

**Detailed description**:
`load_config()` implements this rule mechanically: it attempts to read `config/config.yaml` (or a caller-supplied path, or the `ZETTEL_CONFIG` env var indirectly via `web.py`'s `WebApplication(config_path)`); if the file does not exist, it logs a warning and proceeds with an empty dict, meaning `AppConfig(**{})` resolves every field to its schema default. If the file exists but omits a key, `AppConfig(**data)` again falls through to that field's default for the missing key only — Pydantic does per-field, not per-file, defaulting. This means partial configs are valid and behave predictably: a stripped-down test fixture YAML containing only `{llm: {provider: "openai"}}` still produces a fully-populated `AppConfig` with every other field at its schema default.

This design choice has a direct implication for the "config files" component's boundary: there is exactly one operational YAML per concern (config.yaml for pipeline knobs), and the project explicitly rejects the alternative pattern of environment-specific config files (e.g. `config.dev.yaml`, `config.prod.yaml`) in favor of a single file plus `--config` CLI override and `ZETTEL_CONFIG` env var for tests/alternate deployments (this is the mechanism `WebApplication` and `tests/test_web*.py` use to point at throwaway fixture configs without touching the real `config/config.yaml`).

Secrets are explicitly out of scope for this file: the top of `config.yaml` documents that `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` live in `.env`, and `SESSION_SECRET` (web login) is read directly from `os.environ` in `web.py`, never from `config.yaml` (confirmed in the CLAUDE.md operational notes and not present anywhere in the schema). `load_config()` calls `load_dotenv(env_path, override=False)` before any other logic, ensuring `.env` values are available to downstream LLM client constructors without ever passing through the YAML/Pydantic layer.

**Rule workflow**:
```
load_config(path=None)
  -> load .env (override=False; does not clobber real env vars)
  -> config_path = path or Path("config/config.yaml")
  -> if config_path exists: yaml.safe_load() -> dict; else: warn, dict = {}
  -> return AppConfig(**dict)
       -> each field individually: YAML value if present & valid, else Field default
  -> secrets (API keys, SESSION_SECRET) are never read from this dict;
     downstream code (llm.py get_llm, web.py auth) reads os.environ directly
```

---

### Business Rule: MOC Taxonomy Whitelist and Strict-Mode Validation

**Overview**:
`config/moc_topics.yaml` is the single source of truth for the hierarchical knowledge taxonomy (pilar → categoria → topicos) that governs which `topic` values a generated MOC (Map of Content) is allowed to declare. The whitelist is derived from the flattened category names, and enforcement strictness is itself a config knob (`gardener.strict_topics`).

**Detailed description**:
`zettel/taxonomy.py::resolve_allowed_topics()` is the single entry point both `gardener.py` and `gardener_assign.py` use to obtain `(allowed_category_names, taxonomy_detail_markdown)`. Its behavior branches on two independent inputs: whether a non-empty `override` list was supplied (this is exclusively a test-injection mechanism via `gardener.allowed_topics`, which per the schema-parity rule above is intentionally excluded from `config.yaml`) and whether `gardener.topics_path` points at a loadable file. In the normal production path, `override` is empty and `topics_path` resolves to `config/moc_topics.yaml`, so the function loads and parses the real taxonomy, extracts every `categoria.nome` across all `pilar` entries (deduplicated, order-preserving) as the whitelist, and renders the full hierarchy as a markdown block (`format_taxonomy_for_prompt`) that is injected verbatim into the `moc_generation`/`moc_incremental` LLM prompts as reference material so the model knows the full menu of valid pillars/categories/topics, not just the flat category-name whitelist used for post-hoc validation.

The `strict_topics` flag changes the failure semantics of a missing or malformed taxonomy file dramatically. With `strict_topics: true` (the shipped default in `config.yaml`), a `TaxonomyLoadError` (file not found, not a YAML mapping, or fails `MocTaxonomy.model_validate`) propagates as a hard exception out of `resolve_allowed_topics`, which will abort the `garden` command entirely — this is a deliberate fail-closed design: if the taxonomy that gates every MOC's topic can't be loaded, the pipeline should not silently generate MOCs with an unconstrained topic space. With `strict_topics: false`, the same load failure is caught, logged as a warning, and the function falls through to an empty-whitelist ("allow-all") mode, meaning downstream validation treats any topic string as acceptable — a fail-open design intended for exploratory/non-production use. Separately from load failure, once a MOC candidate topic is generated by the LLM, `gardener.py:447` performs the actual whitelist check only when `cfg.gardener.strict_topics` is true, rejecting/re-routing any candidate whose topic isn't a substring/exact match (per the CLAUDE.md architecture note: "New MOC topics validated against categorias — substring match; rejected if strict_topics: true").

Because `moc_topics.yaml` also feeds `category_label_template` (a string template combining `gardener.domain` and each `categoria.nome`, e.g. `"Ciencia de Dados: Matemática e Estatística"`) which is embedded once per `garden` run to assign notes to taxonomy buckets before clustering, a change to the taxonomy's category names has a downstream effect beyond validation: it changes the embedding space used for note-to-category assignment, meaning edits to `moc_topics.yaml` should be treated similarly to embedding-model changes in terms of needing a fresh `garden` run to re-assign notes consistently (this is implicit; no automated re-assignment trigger exists in the codebase for a taxonomy edit alone).

**Rule workflow**:
```
gardener.py / gardener_assign.py
  -> resolve_allowed_topics(cfg.gardener.topics_path, cfg.gardener.allowed_topics,
                             strict=cfg.gardener.strict_topics)
       -> override non-empty? (test-only path) -> use override as whitelist,
          still try to load topics_path for the prompt detail markdown
       -> topics_path set?
            -> try load_moc_taxonomy(path) -> yaml.safe_load -> MocTaxonomy.model_validate
                 -> success: detail = format_taxonomy_for_prompt(tax)
                 -> failure (TaxonomyLoadError):
                      strict=true  -> re-raise (abort garden run)
                      strict=false -> warn, continue with tax=None
       -> return (allowed_topic_names(tax) if tax else [], detail)
  -> category_label_template.format(domain=cfg.gardener.domain, categoria=cat.nome)
     embedded per category -> notes assigned to nearest category bucket
  -> LLM proposes a MOC topic (moc_generation / moc_incremental prompt, given
     the full taxonomy detail markdown as reference)
  -> gardener.py: if strict_topics and topic not matched among allowed names
     -> candidate rejected/handled per gardener's rejection logic
```

---

### Business Rule: Article Personality Resolution Fallback Chain

**Overview**:
`config/personalities.yaml` supplies named style presets (temperature + a natural-language style instruction) for the `zettel article` command's personality-rewrite step. Unlike the other two config files, its resolution is defensive rather than validated — it is designed to never hard-fail regardless of file content.

**Detailed description**:
`zettel/article.py::load_personalities(path)` first checks file existence; if the file is absent, it returns a single hardcoded in-memory profile (`neutral`, temperature 0.5, `"Sem reescrita."`) rather than raising — meaning a deployment that deletes or never ships `personalities.yaml` degrades to a working (if uncustomizable) article pipeline rather than crashing `zettel article`. If the file exists, it is parsed with `yaml.safe_load`, and the function accepts two possible top-level shapes: a dict with a `personalities` key wrapping the profile map (the shape actually used in `config/personalities.yaml`), or — as a compatibility fallback — the raw dict of profiles directly at the top level (`data.get("personalities") or data`). Every value under each profile id is coerced with `dict(v)`, so a YAML author could technically supply a non-dict value here and cause a `TypeError` at load time; this is the one point in the three-file component with a plausible unguarded runtime error path.

`apply_personality_rewrite()` layers a second fallback on top: even after a successful file load, if the requested `personality_id` (typically supplied by a CLI/web caller, defaulting to `cfg.retrieval.article.default_personality`, which is `"neutral"` in `config.yaml`) is not a key in the loaded profile dict, it falls back to the `neutral` key if present, and if even that is absent (a `personalities.yaml` that doesn't define `neutral` at all — a valid-YAML-but-broken-contract scenario the schema-parity mechanism used for `config.yaml`/`moc_topics.yaml` has no equivalent for here), it synthesizes a profile inline from the caller's free-text `custom_style_notes` or a generic `"Reescreva com clareza."` instruction. This three-tier fallback (exact id → `neutral` → synthesized) guarantees `apply_personality_rewrite` always produces *some* style prompt and temperature, at the cost of being unable to distinguish "the user asked for a personality that doesn't exist" from "everything is fine" anywhere in this function — no warning is logged on any of the fallback branches, unlike the equivalent `TaxonomyLoadError` warning path in `taxonomy.py`.

A distinct, earlier-exit rule governs cost/latency: if `personality_id == "neutral"` and no `custom_style_notes` were supplied, the function returns the input body unchanged with `called=False` *before* even attempting to read `personalities.yaml` — the file is not touched at all on the most common path (default personality, no custom notes), which is both a performance optimization (no LLM call, no file I/O) and a documented determinism guarantee (the neutral personality is defined in code as a true no-op, not merely "a profile with an empty style prompt" that would still trigger an LLM round trip).

**Rule workflow**:
```
apply_personality_rewrite(cfg, db, body, personality_id, custom_style_notes="")
  -> pid = personality_id or cfg.retrieval.article.default_personality or "neutral"
  -> notes = custom_style_notes.strip()
  -> if pid == "neutral" and not notes:
       return body unchanged, called=False   # no file read, no LLM call
  -> profiles = load_personalities(Path(cfg.retrieval.article.personalities_path))
       -> file missing -> {"neutral": {...hardcoded default...}}
       -> file present -> yaml.safe_load -> profiles dict (supports
          {personalities: {...}} or flat {...} shape)
  -> profile = profiles.get(pid)
              or profiles.get("neutral")
              or {"name": pid, "temperature": 0.7, "style_prompt": notes or "Reescreva com clareza."}
  -> build LLM prompt from profile["style_prompt"] + profile["name"] + custom notes
  -> call LLM at temperature = profile.get("temperature", 0.7)
  -> return rewritten body, called=True
```

---

### Business Rule: Provider/Model Toggle-by-Comment Pattern (LLM and Embedding Sections)

**Overview**:
`config/config.yaml`'s `llm` and `embedding` sections each ship with one active provider block and one (or more) alternative provider block(s) present in the file but commented out, as a manual toggle mechanism for switching providers.

**Detailed description**:
In the `llm` section, `provider: openai` / `model: gpt-4o-mini` are active, immediately followed by commented-out `#provider: ollama` / `#model: qwen3.5:4b` lines documenting the alternative. In the `embedding` section, the pattern is inverted: `provider: openai` / `model: text-embedding-3-small` are commented out, while `provider: ollama` / `model: qwen3-embedding` are active. This is a documentation-as-code convention — a maintainer switches providers by commenting/uncommenting matched pairs of lines rather than by maintaining two separate config files or a provider-keyed nested structure.

This pattern is not enforced or validated by any schema — `AppConfig` has no cross-field validator ensuring the active `llm.provider` matches the "shape" of `llm.model` (e.g. nothing stops `provider: openai` with `model: qwen3.5:4b` from loading successfully; the mismatch would only surface at runtime when `llm.py::get_llm()` attempts to instantiate the wrong client or the model name is rejected by the actual OpenAI API). Because the embedding provider/model/dimensions triple additionally determines the Chroma vector space, an accidental or incomplete toggle (e.g. changing `provider` but forgetting to also change `dimensions`) is caught downstream by the embedding-mismatch detection in `index.py`/`cli.py` (which compares the configured embedding identity against what's stored in Chroma and prompts for `--force` reindex), not by the config file itself. This is documented behavior (`config.yaml:33-37` comments) but represents a manual-discipline dependency rather than a structurally enforced one.

**Rule workflow**:
```
Maintainer wants to switch LLM or embedding provider:
  -> comment out the active provider/model lines
  -> uncomment the alternative provider/model lines
  -> (embedding only) also update `dimensions` to match the new model's
     native/truncated dimensionality
  -> run zettel reindex --force if the embedding space changed
     (index.py detects the mismatch on next VectorIndex open and blocks
      normal operation until --force or explicit confirmation)
  -> recalibrate retrieval.relevance_floor thresholds if search quality
     degrades under the new embedding space (documented, not automated)
```

---

## 4. Component Structure

```
config/
├── config.yaml            # Operational source of truth for AppConfig (zettel/config.py)
│                           #   - paths, llm, embedding, chunking, linking, harvest,
│                           #     extraction, literature_review, images, gardener,
│                           #     hub_mocs, retrieval (+ nested ask/article/graph_expansion/
│                           #     relevance_floor), language, log_level, pdf_extractor, device
├── moc_topics.yaml         # MOC category taxonomy (zettel/taxonomy.py -> MocTaxonomy)
│                           #   - taxonomia_conhecimento: [ { pilar, categorias: [ { nome, topicos } ] } ]
│                           #   - referenced by config.yaml:gardener.topics_path
└── personalities.yaml      # Article personality/style presets (zettel/article.py, untyped dict)
                            #   - personalities: { id: { name, temperature, style_prompt } }
                            #   - referenced by config.yaml:retrieval.article.personalities_path
```

No subfolders, no environment-specific variants (`config.dev.yaml`, etc.), no `.example`/`.sample` template files were found under `config/`. The only override mechanisms are: CLI `--config` flag (`cli.py` root callback), `ZETTEL_CONFIG` env var (`web.py:100`, consumed by `WebApplication`), and ad-hoc fixture paths passed directly to `load_config()` in tests.

---

## 5. Dependency Analysis

```
Internal Dependencies (config data -> validating/consuming Python modules):

config/config.yaml
  -> zettel/config.py (AppConfig, load_config)          [schema + loader]
  -> zettel/cli.py (_load_deps, _idx_kwargs, all commands)
  -> zettel/web_app.py (WebApplication._idx_kwargs — must mirror cli.py's)
  -> zettel/web.py (WebApplication instantiation, ZETTEL_CONFIG env var)
  -> zettel/harvester.py, paging.py (chunking.*, harvest.*, images.*, pdf_extractor)
  -> zettel/extractor.py (extraction.*, literature_review.*)
  -> zettel/review.py (literature_review.*)
  -> zettel/connector.py, sync.py, retrieval.py (linking.*, retrieval.*)
  -> zettel/gardener.py, gardener_assign.py, gardener_hub.py (gardener.*, hub_mocs.*)
  -> zettel/ask.py, article.py, article_graph.py (retrieval.ask.*, retrieval.article.*)
  -> zettel/assets.py, bibliography.py (images.*, harvest.*)
  -> zettel/llm.py (llm.*, via get_llm/call_llm)
  -> zettel/vault.py, new_note.py, purge_source.py, rebuild.py (vault_path, prompts_path)

config/moc_topics.yaml
  -> zettel/taxonomy.py (MocTaxonomy, Pilar, Categoria — schema + loader)
  -> zettel/gardener.py (resolve_allowed_topics, strict-mode validation)
  -> zettel/gardener_assign.py (category label embedding for note assignment)
  (reached only via config.yaml:gardener.topics_path — no module hardcodes the filename
   except as the schema's own Field default and the CLI's --help text)

config/personalities.yaml
  -> zettel/article.py (load_personalities, apply_personality_rewrite — no schema)
  (reached only via config.yaml:retrieval.article.personalities_path)

External Dependencies:
- PyYAML (yaml.safe_load) — parser for all three files; no external schema validator
  (e.g. no JSON Schema / Cerberus) is used — validation is entirely via Pydantic v2
  models for config.yaml and moc_topics.yaml, and via plain dict access for personalities.yaml
- Pydantic v2 (BaseModel, Field, field_validator) — AppConfig (config.py) and
  MocTaxonomy/Pilar/Categoria (taxonomy.py)
- python-dotenv (load_dotenv) — loads .env before config.yaml parsing, for secrets
  that deliberately live outside these YAML files
```

---

## 6. Afferent and Efferent Coupling

Coupling here is measured between the three YAML data contracts and the Python types/functions that define and consume their schema (the natural "component" unit for data-contract files, since they have no classes of their own).

| Component | Afferent Coupling (consumers depending on it) | Efferent Coupling (things it depends on) | Critical |
|-----------|-----------------------------------------------|-------------------------------------------|----------|
| `config/config.yaml` (validated by `AppConfig`) | ~25 modules read at least one top-level section (see Dependency Analysis); virtually every pipeline command depends on it directly or transitively | 1 (Pydantic `AppConfig` schema in `zettel/config.py`) | High — sole operational config; a structural break here halts the entire CLI/web app |
| `config/moc_topics.yaml` (validated by `MocTaxonomy`) | 2 modules directly (`gardener.py`, `gardener_assign.py`), reached indirectly by `zettel garden` / `zettel garden --hubs` CLI paths | 2 (Pydantic `MocTaxonomy`/`Pilar`/`Categoria` in `zettel/taxonomy.py`; `gardener.topics_path` value from `config.yaml`) | Medium-High — only affects Phase 4 (gardener); failure mode is loud (`TaxonomyLoadError`) under the shipped `strict_topics: true` |
| `config/personalities.yaml` (untyped) | 1 module (`zettel/article.py`), reached only by `zettel article` with a non-neutral personality or custom style notes | 0 formal (no schema); implicitly depends on `retrieval.article.personalities_path` / `default_personality` values from `config.yaml` | Low-Medium — narrowest blast radius (one optional feature of one CLI-only command not exposed in the web UI), but its lack of schema validation makes its actual failure behavior the least predictable of the three |

---

## 8. Integration Points

| Integration | Type | Purpose | Protocol | Data Format | Error Handling |
|-------------|------|---------|----------|-------------|-----------------|
| `config/config.yaml` <-> `zettel.config.AppConfig` | Internal data contract | Single source of runtime configuration for the whole pipeline | Filesystem read (`yaml.safe_load`) | YAML -> Pydantic model | Missing file: warning + factory defaults. Invalid type/value: Pydantic `ValidationError` propagates uncaught from `load_config()`. Schema/YAML drift: caught only by `tests/test_config.py`, not at runtime. |
| `config/moc_topics.yaml` <-> `zettel.taxonomy.MocTaxonomy` | Internal data contract | Category whitelist + LLM prompt reference for MOC generation | Filesystem read (`yaml.safe_load`) | YAML -> Pydantic model | Missing/invalid file + `strict_topics: true`: `TaxonomyLoadError` raised, aborts `garden`. + `strict_topics: false`: warning logged, allow-all fallback. |
| `config/personalities.yaml` <-> `zettel.article.load_personalities` | Internal data contract | Style/temperature presets for the article personality-rewrite LLM step | Filesystem read (`yaml.safe_load`) | YAML -> raw `dict` (no schema) | Missing file: hardcoded single-profile fallback, no error. Malformed profile value (non-mapping): unguarded `TypeError` possible in `dict(v)`. Unknown personality id: silent fallback to `neutral`, then to a synthesized profile — no warning ever logged. |
| `.env` file / process environment | External secret source | Supplies API keys and `SESSION_SECRET`, deliberately kept out of all three YAML files | Filesystem read (`python-dotenv`) + `os.environ` | key=value | `.env` absent: debug-level log only, falls through to system env vars |

---

## 9. Design Patterns & Architecture

| Pattern | Implementation | Location | Purpose |
|---------|-----------------|----------|---------|
| Single Source of Truth | `config/config.yaml` as the only operational config file, with Pydantic defaults as fallback only | `config/config.yaml` header comment; `zettel/config.py` docstrings | Prevents config sprawl across environments/files |
| Schema-as-Contract via Pydantic | `AppConfig` / `MocTaxonomy` model trees with `field_validator`s for path resolution and value constraints | `zettel/config.py:22-270`, `zettel/taxonomy.py:14-25` | Type safety + fail-fast validation for the two schema-backed files |
| Contract Test (schema-YAML parity) | Recursive schema-leaf-path walk compared against parsed YAML keys, with an explicit code-only allowlist | `tests/test_config.py:26-67` | Prevents schema/YAML drift without a runtime enforcement cost |
| Graceful Degradation / Fallback Chain | `load_personalities()` + `apply_personality_rewrite()` multi-tier fallback (exact id -> neutral -> synthesized) | `zettel/article.py:1055-1094` | Article pipeline never hard-fails on a missing/incomplete personality file |
| Fail-Closed vs. Fail-Open Toggle | `gardener.strict_topics` switches taxonomy load failure between a hard `TaxonomyLoadError` and a soft allow-all fallback | `zettel/taxonomy.py:91-104` | Lets operators choose safety (production) vs. permissiveness (exploration) per deployment |
| Toggle-by-Comment | Paired commented/active provider blocks for `llm` and `embedding` sections | `config/config.yaml:21-48` | Low-ceremony provider switching without maintaining multiple files |
| Indirection via Path Reference | `config.yaml` stores *paths* to the other two YAML files (`gardener.topics_path`, `retrieval.article.personalities_path`) rather than inlining their content | `config/config.yaml:110,175` | Keeps concerns separated (pipeline knobs vs. taxonomy vs. writing style) while allowing all three to be swapped independently (e.g. `--config` pointing at an alternate `config.yaml` that in turn points at a different taxonomy file) |

---

## 10. Technical Debt & Risks

| Risk Level | Component Area | Issue | Impact |
|------------|-----------------|-------|--------|
| Medium | `config/personalities.yaml` | No Pydantic schema (unlike `config.yaml` and `moc_topics.yaml`); loaded as a raw dict with `dict(v)` coercion that can raise `TypeError` on a malformed profile value | Runtime crash risk on a hand-edit typo is less discoverable than the other two files' Pydantic `ValidationError`s; no test currently guards the shipped file's structural validity the way `test_config.py`/`test_load_project_moc_topics_yaml` do |
| Medium | `zettel doctor` coverage gap | The `doctor` command checks `config.yaml` existence, vault/inbox paths, and every prompt template file, but does not check `config/moc_topics.yaml` or `config/personalities.yaml` existence/validity even though both are referenced by path from `config.yaml` | An operator who deletes/misconfigures `moc_topics.yaml` or `personalities.yaml` gets no early warning from the health-check command; the failure surfaces later, mid-`garden` (hard abort under `strict_topics: true`) or silently inside `article` |
| Low-Medium | Cross-field consistency (`llm.provider`/`llm.model`, `embedding.provider`/`embedding.model`/`embedding.dimensions`) | No validator enforces that the active provider and model/dimensions values are a coherent triple; the toggle-by-comment convention relies entirely on manual discipline | A partial or mismatched toggle (e.g. provider changed, model left stale) is only caught downstream (LLM client construction failure, or Chroma embedding-mismatch detection), not at config-load time |
| Low | Referential integrity: `retrieval.article.default_personality` vs. `personalities.yaml` keys | Nothing validates at load time that `config.yaml`'s `default_personality: neutral` actually exists as a key in `personalities.yaml` | Currently harmless because `article.py`'s fallback chain always resolves to *something*, but a rename of the `neutral` key in `personalities.yaml` without updating `config.yaml` (or vice versa) would silently change which profile is used as the default, with no warning logged |
| Low | Referential integrity: `gardener.domain` used inside `category_label_template` | `domain: "Ciencia de Dados"` is a free-text string with no validation against any controlled vocabulary; a typo silently changes every category-assignment embedding | Would degrade note-to-category assignment quality without raising any error; only discoverable via manual inspection of `garden` output |
| Low | No environment-specific config variants | Only one operational `config.yaml` exists; switching between local/dev and any other target environment requires either editing this file in place or passing `--config`/`ZETTEL_CONFIG` to point at a hand-maintained alternate copy (not templated/generated) | Not a bug per project convention (explicitly documented as the intended model), but increases the chance of accidentally committing/losing environment-specific edits to the single tracked file |

---

## 11. Test Coverage Analysis

| Config File / Concern | Unit Tests | Integration Tests | Coverage | Test Quality |
|------------------------|------------|---------------------|----------|----------------|
| `config/config.yaml` schema parity | `tests/test_config.py::test_config_yaml_covers_schema_keys` (1) | — | High for structural (key-presence) coverage across the entire schema tree | Strong: recursively walks every nested Pydantic model, unwraps Optional/Union types, and asserts against the real shipped file, not a fixture copy — catches drift at the source. Does not validate *values* (only key presence), and does not fail on YAML keys that are extraneous to the schema (Pydantic silently ignores unknown keys by default; no test asserts the reverse direction, i.e. that the YAML has no stray/obsolete keys). |
| `config/config.yaml` value smoke test | `tests/test_config.py::test_load_config_yaml_smoke` (1) | — | Low-moderate — spot-checks 4 representative values (`retrieval.mode`, `relevance_floor.min_vector_similarity`, `hub_mocs.selection_mode`, presence of `contradicts` in `relation_weights`) | Adequate as a smoke test but far from exhaustive; most of the ~90 leaf values in `config.yaml` have no assertion anywhere confirming they parse to the expected value |
| `config/moc_topics.yaml` structure/loader | `tests/test_gardener.py::test_load_moc_taxonomy`, `test_format_taxonomy_for_prompt`, `test_load_project_moc_topics_yaml`, `test_validate_from_taxonomy_file`, `test_resolve_allowed_topics_from_file`, `test_resolve_allowed_topics_override`, plus missing-file/strict-mode tests (all in `tests/test_gardener.py`) | None found (no test drives a full `garden` run against the real shipped `moc_topics.yaml` end-to-end; the smoke test only confirms it parses and has categories) | Good for the loader/validator layer; the "does the real file parse" smoke test (`test_load_project_moc_topics_yaml`) is present and correctly designed to `pytest.skip` rather than fail if the file is absent | Strong coverage of `taxonomy.py`'s functions using synthetic fixture taxonomies (`mini_taxonomy_path`) for most assertions, plus one direct smoke test against the real file. Missing: no test asserts the real file's category names are non-empty/unique, or that `gardener.domain` + `category_label_template` combine without formatting errors against the real categories. |
| `config/personalities.yaml` structure/loader | None found — no test loads or validates the real shipped file; existing personality tests (`tests/test_article.py::test_personality_neutral_noop`, plus several in `tests/test_article_graph.py`) only exercise the `neutral` no-op path, which never reads the file | None found | Low — the only behavior actually tested is the short-circuit path that never touches the file at all | The neutral no-op is well covered, but the file's real content (`geek_philosopher`, `serious_academic`, `clear_teacher` profiles) has zero test coverage: nothing asserts these three profiles parse correctly, have a `style_prompt` key, or that `load_personalities()` correctly unwraps the `personalities:` wrapper key against the actual shipped file (as opposed to a synthetic one, since no test constructs one). This is the weakest-tested of the three config artifacts. |
| Doctor/health-check coverage of config files | Not directly unit-tested (no test file for `cli.py::doctor` was found under `tests/`) | — | None for this specific command | `zettel doctor`'s config-related checks (`config.yaml` existence, prompt file existence) appear untested by the automated suite; combined with the coverage gap noted in Section 10 (doctor doesn't check `moc_topics.yaml`/`personalities.yaml` at all), this is an area with no safety net in either the command's own logic or its test coverage |

**Test file locations** (absolute paths):
- `D:\projetos\zettel_app\tests\test_config.py`
- `D:\projetos\zettel_app\tests\test_gardener.py` (taxonomy-related tests)
- `D:\projetos\zettel_app\tests\test_gardener_hub.py` (no direct taxonomy/personalities assertions found, despite consuming `HubMocsConfig` indirectly)
- `D:\projetos\zettel_app\tests\test_article.py` (personality no-op test)
- `D:\projetos\zettel_app\tests\test_article_graph.py` (personality no-op tests within the LangGraph flow)
- `D:\projetos\zettel_app\tests\test_web.py` (references `ZETTEL_CONFIG`-style alternate config loading for the web app, not specific YAML content)

All tests referenced above were executed during this analysis (`pytest tests/test_config.py -v` and `pytest tests/test_gardener.py -k "taxonomy or moc_topics" -v`); all passed against the current state of the three config files.

---

*Note on scope*: Sections 3 (Endpoints) is intentionally omitted — this data-only component exposes no REST/GraphQL/gRPC endpoints of its own; it is consumed in-process by the modules listed in Sections 5 and 8.
