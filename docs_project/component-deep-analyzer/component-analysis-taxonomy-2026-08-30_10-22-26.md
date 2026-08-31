# Component Deep Analysis Report — `taxonomy`

## 1. Executive Summary

`zettel/taxonomy.py` is a small, single-purpose data/validation component: it loads a hierarchical YAML taxonomy of knowledge domains (`taxonomia_conhecimento` -> pilar -> categoria -> topicos), validates its shape with Pydantic, and exposes derived views of it (a flat category-name whitelist and a markdown rendering) to the rest of the pipeline. It has no side effects, no I/O beyond reading one YAML file, and no direct dependency on the database, vector index, or LLM.

Its sole consumer is **Phase 4 (the Gardener, `gardener.py` + `gardener_assign.py` + `gardener_hub.py`)**, where the taxonomy plays two roles:

1. **Category-label embedding / bucket assignment** — `gardener_assign.load_category_names()` calls into taxonomy to get the flat list of category names, which `embed_category_labels()` then embeds and uses to bucket permanent notes before per-category clustering (`cluster_within_category`).
2. **MOC topic whitelist enforcement** — `resolve_allowed_topics()` supplies both the whitelist (`allowed_topics`) used by `_topic_matches_allowed()` / `_validate_moc_topic()` in `gardener.py`, and a full markdown rendering of the taxonomy (`taxonomy_detail`) injected into the `moc_generation.md` / `moc_hub_generation.md` LLM prompts as reference material.

The component is intentionally decoupled from its data source: `config/moc_topics.yaml` is the operational file (10 pilares, 32 categorias, 201 lines as shipped), but the component itself is data-agnostic — any conforming YAML works, and callers can bypass the file entirely via the `override` parameter (used pervasively in tests). It also has a well-defined "fail fast vs. degrade gracefully" contract governed by the caller-supplied `strict` flag, which is the component's central business rule.

Key finding: the module has no dedicated test file — all of its behavior is exercised indirectly through `tests/test_gardener.py` (as a taxonomy/topic-validation section within the Gardener's test suite). Coverage is nonetheless thorough for the public API surface, including a smoke test against the real shipped `config/moc_topics.yaml`.

## 2. Data Flow Analysis

There are two distinct call chains that exercise the component, both originating in `gardener.py`'s `run_garden()` orchestration.

**A. Fail-fast validation at the start of a garden run:**
```
1. CLI/web triggers `zettel garden` -> gardener.run_garden(cfg, db, idx)
2. run_garden() calls taxonomy.resolve_allowed_topics(cfg.gardener.topics_path,
   cfg.gardener.allowed_topics, strict=cfg.gardener.strict_topics)
3. resolve_allowed_topics() calls taxonomy.load_moc_taxonomy(path)
4.   -> reads YAML file from disk (yaml.safe_load)
5.   -> validates top-level shape is a dict
6.   -> Pydantic-validates into MocTaxonomy (nested Pilar -> Categoria models)
7. On TaxonomyLoadError + strict=True: run_garden() logs, marks the run "failed"
   in StateDB, and re-raises — no clustering/LLM work happens.
8. On success: run continues into category-bucket clustering.
```

**B. Category-bucket assignment (per garden run, before clustering):**
```
1. run_garden() calls gardener_assign.load_category_names(gcfg.topics_path)
2. load_category_names() calls taxonomy.load_moc_taxonomy(path) (best-effort,
   catches all exceptions -> logs warning, returns [] on any failure)
3. On success: taxonomy.allowed_topic_names(tax) flattens pilar/categoria into
   a deduplicated, order-preserving list[str] of category names
4. gardener_assign.embed_category_labels(idx, categories, domain, template)
   formats each name into a label string and embeds it via VectorIndex
5. assign_notes_to_categories() buckets permanent notes by nearest category
   vector (cosine similarity) -> feeds cluster_notes_within_buckets()
```

**C. MOC topic generation + validation (per cluster, inside `_create_new_moc`):**
```
1. _create_new_moc() calls taxonomy.resolve_allowed_topics(topics_path,
   allowed_topics, strict=strict_topics)
2.   -> returns (allowed_topics: list[str], taxonomy_detail: markdown str)
3. taxonomy_detail is injected into the moc_generation.md prompt as full
   hierarchical reference; allowed_topics_section lists just the category names
4. LLM call produces MOCGenerationOutput.topic (freeform, LLM's chosen label)
5. gardener._topic_matches_allowed(suggested, allowed_topics) — if the
   pre-assigned bucket category substring-matches an allowed name, the LLM's
   topic is overridden with it (`moc_output.topic = suggested`)
6. gardener._validate_moc_topic(cfg, moc_output) re-resolves allowed_topics
   (a second, independent call to resolve_allowed_topics) and substring-matches
   the (possibly overridden) topic; rejects (returns False, MOC discarded) when
   strict_topics=True and no match, else logs and approves anyway.
7. On acceptance: MOC file is written to vault, indexed in Chroma/SQLite.
```

Note the taxonomy is **re-loaded from disk on every call** to `resolve_allowed_topics()` / `load_category_names()` — there is no caching layer inside `taxonomy.py`. For a garden run with many clusters, `_create_new_moc` and `_validate_moc_topic` each independently re-parse the YAML per MOC candidate (see Technical Debt, section 10).

## 3. Business Rules & Logic

### Overview of the business rules:

| Rule Type | Rule Description | Location |
|-----------|------------------|----------|
| Structural Validation | Taxonomy root must be a YAML mapping (dict), not a list/scalar | zettel/taxonomy.py:41-42 |
| Structural Validation | Taxonomy must conform to `pilar -> categorias[] -> {nome, topicos[]}` schema (Pydantic) | zettel/taxonomy.py:14-25, 43-46 |
| Fail-fast vs. Degrade | `path=None` always raises `TaxonomyLoadError` inside `load_moc_taxonomy` | zettel/taxonomy.py:34-35 |
| Fail-fast vs. Degrade | Missing/invalid file + `strict=True` + no override -> raise `TaxonomyLoadError` | zettel/taxonomy.py:95-98 |
| Fail-fast vs. Degrade | Missing/invalid file + (`strict=False` or override present) -> log warning, degrade | zettel/taxonomy.py:96-98 |
| Whitelist Derivation | Category name whitelist = `categoria.nome` values only (topicos are prompt guidance, not validated) | zettel/taxonomy.py:49-56 |
| Whitelist Derivation | Category names deduplicated, first-seen order preserved | zettel/taxonomy.py:52-55 |
| Whitelist Derivation | Empty/falsy category names are skipped | zettel/taxonomy.py:54 |
| Precedence | Non-empty `override` always wins over file-derived categories for the whitelist | zettel/taxonomy.py:100-101 |
| Precedence | `taxonomy_detail` markdown always sourced from the file when the file loads successfully, regardless of whether `override` supplies the whitelist | zettel/taxonomy.py:93-94, 100-101 |
| Precedence | Empty override + no usable taxonomy -> whitelist is `[]`, meaning "allow all" to downstream callers | zettel/taxonomy.py:104 |
| Formatting | Prompt rendering nests `## Pilar:` then `### Categoria:` then bullet list of `topicos`; empty taxonomy renders to `""` | zettel/taxonomy.py:59-70 |
| Consumer Rule (gardener.py) | `strict_topics=True` rejects an LLM-chosen MOC topic that doesn't case-insensitive substring-match any allowed category | zettel/gardener.py:440-452 |
| Consumer Rule (gardener.py) | `strict_topics=False` accepts any topic, logging a permissive-mode notice | zettel/gardener.py:453-458 |
| Consumer Rule (gardener.py) | Pre-assigned bucket category silently overrides the LLM's chosen topic when it matches the whitelist | zettel/gardener.py:350-351 |

### Detailed breakdown of the business rules:
---

### Business Rule: Fail-Fast vs. Graceful Degradation (the `strict` contract)

**Overview**:
The single most consequential rule in this component is the three-way branch inside `resolve_allowed_topics()` that decides whether a missing or malformed taxonomy file is a hard error or a soft warning. This is encoded via the `strict` keyword-only parameter, which callers thread through from `GardenerConfig.strict_topics`.

**Detailed description**:
When `topics_path` is provided and `load_moc_taxonomy()` raises `TaxonomyLoadError` (file absent, or present but fails YAML parsing / Pydantic validation), the function checks `strict and not override`. If both `strict` is true and no `override` list was supplied by the caller, the exception is re-raised immediately, aborting whatever pipeline stage invoked it. `run_garden()` in gardener.py relies on this at the very top of a garden run specifically so that a broken taxonomy file fails the entire `zettel garden` invocation before any embeddings, clustering, or LLM spend occurs — the intent documented in the code comment is "fail fast if taxonomy file is required but missing/invalid."

If instead `strict` is false, or an `override` list is present (even one supplied only for tests), the function swallows the `TaxonomyLoadError`, logs a warning (`Nao foi possivel carregar taxonomia de %s`), and falls through to return whatever whitelist is still derivable — either the `override` list verbatim, or an empty list if there is none. This means a caller that supplies both a non-empty `override` and a broken `topics_path` never raises regardless of `strict`, because the `not override` guard short-circuits: `override` presence unconditionally protects against a bad file. This is exercised directly by `test_resolve_allowed_topics_override`, which points `topics_path` at a valid file but the same protection logically extends to an invalid one (not separately tested, see section 11).

Two other call sites duplicate this exact resolve-and-branch pattern independently: `_create_new_moc()` (before generating a MOC) and `_validate_moc_topic()` (after generating one). Each wraps `resolve_allowed_topics()` in its own try/except `TaxonomyLoadError`, and on failure returns `None` / `False` respectively rather than raising further — i.e., the strict/raise behavior is only actually surfaced as a hard pipeline failure at the top-level `run_garden()` fail-fast check; the two per-cluster call sites treat a load failure as "reject this one MOC" rather than aborting the whole run. This layered design means a taxonomy file that becomes unreadable *mid-run* (e.g., deleted by a concurrent process) degrades to skipping individual MOCs rather than crashing, even under `strict_topics=True`, because those sites catch the exception locally.

**Rule workflow**:
```
resolve_allowed_topics(path, override, strict):
  if path is not None:
      try: tax = load_moc_taxonomy(path); detail = render(tax)
      except TaxonomyLoadError:
          if strict and not override: raise   <-- hard fail
          else: log warning, tax stays None    <-- soft degrade
  if override:            return override, detail
  elif tax is not None:   return allowed_topic_names(tax), detail
  else:                   return [], detail     <-- "allow all" semantics
```

---

### Business Rule: Category Name Extraction Is Whitelist-Only (topicos are not validated)

**Overview**:
`allowed_topic_names()` flattens the taxonomy into a list of `categoria.nome` strings — one level of the three-level hierarchy (`pilar` > `categoria` > `topicos`). The leaf-level `topicos` strings are never part of the validation whitelist; they exist purely as descriptive/prompt content.

**Detailed description**:
This is a deliberate architectural choice, reinforced by an explicit comment in `config/moc_topics.yaml` itself ("Nivel de validacao do MOC: categorias[].nome (pilares agrupam; topicos orientam subsecoes)" — translation: "MOC validation level is category names; pilares are grouping, topicos guide subsections"). The consequence is that an LLM-generated MOC topic is checked only against the 32 category names in the shipped taxonomy (e.g. "Matemática e Estatística", "Boas Práticas e Código"), never against the ~100+ granular topicos underneath them (e.g. "Álgebra Linear", "Clean Code"). A MOC titled anything substring-matching a category name passes; the topicos exist solely to be rendered into `format_taxonomy_for_prompt()`'s markdown so the LLM has richer context for choosing subsections and phrasing, and — per the gardener_assign module comment — topicos are not used for bucket-embedding either (only category names are embedded via `embed_category_labels`).

Deduplication (`cat.nome not in names`) and order preservation both matter for downstream consumers: `embed_category_labels()` in `gardener_assign.py` builds one embedding vector per unique name via `idx.embed_texts(labels)`, so a duplicate category name appearing under two different pilares would otherwise cause a wasted embedding call and an ambiguous bucket key in `assign_notes_to_categories()`'s `buckets: dict[str, list[str]]` (a dict keyed by category name — an actual duplicate would silently collapse into one bucket). The empty-name guard (`if cat.nome`) protects against a malformed YAML entry (`nome: ""`) from polluting the whitelist with an empty string that could then substring-match against arbitrary generated topics (since `"" in anything.lower()` is always true) — this is a subtle but important defensive check, as it prevents a `strict_topics=True` rejection gate from becoming a silent no-op.

The three-level structure exists for exactly one other purpose: `format_taxonomy_for_prompt()` renders the full hierarchy (pilar headers, category subheaders, bulleted topicos) as reference markdown injected into `moc_generation.md` / `moc_hub_generation.md`, giving the LLM the complete domain map even though only the middle tier is enforced.

**Rule workflow**:
```
allowed_topic_names(tax):
  names = []
  for pilar in tax.taxonomia_conhecimento:
      for cat in pilar.categorias:
          if cat.nome and cat.nome not in names:
              names.append(cat.nome)
  return names
  # topicos are never read here — only used by format_taxonomy_for_prompt()
```

---

### Business Rule: Override Precedence Splits Whitelist and Detail Sourcing

**Overview**:
When both a file-derived taxonomy and a caller-supplied `override` list are available, `resolve_allowed_topics()` does not simply pick one source wholesale — it splits: the **whitelist** comes from `override` (if non-empty), while the **detail markdown** always comes from the file (if it loaded successfully), independent of whether `override` was used for the whitelist.

**Detailed description**:
This split-sourcing behavior is easy to overlook but is explicitly tested (`test_resolve_allowed_topics_override`: `topics_path` points at a real mini-taxonomy, `override=["So Esta"]` is passed, and the assertion confirms `allowed == ["So Esta"]` while `detail` still contains `"## Pilar: Pilar A"` from the file). The rationale, inferred from the calling context in `gardener.py`, is that `override` exists primarily as a **test-only escape hatch** (per the `GardenerConfig.allowed_topics` field comment: "Override de testes; nao e knob do config.yaml") — production code paths always leave `allowed_topics` empty in `config.yaml`, so `override` is functionally never populated in a real run. Splitting the two sources means that even in a test that forces a narrow whitelist via `override`, the LLM prompt still receives the full taxonomy detail if a real file is also supplied, keeping prompt content realistic during testing.

In production, because `cfg.gardener.allowed_topics` defaults to an empty list and is never set in `config/config.yaml` (confirmed by grep — only `strict_topics` and `topics_path` appear there), the `override` branch of `resolve_allowed_topics()` is effectively dead in the live pipeline; the whitelist is always derived from `topics_path` via `tax is not None -> allowed_topic_names(tax)`. This means the taxonomy YAML file (`config/moc_topics.yaml`) is the single source of truth operationally, and the `override` parameter's only real consumers are the unit tests in `tests/test_gardener.py`.

**Rule workflow**:
```
if override:            whitelist = override        # detail already set from file (if loaded)
elif tax is not None:    whitelist = allowed_topic_names(tax)
else:                    whitelist = []               # allow-all
# detail is set once, earlier, purely based on whether the FILE loaded —
# never influenced by whether override was supplied
```

---

### Business Rule: Empty Whitelist Means "Allow All" Downstream

**Overview**:
An empty `allowed_topics` list — whether because no `topics_path` was configured, the file was missing under permissive mode, or the taxonomy legitimately has zero categories — is not treated as "nothing is allowed." Both `taxonomy.py`'s own contract and its consumer `gardener._validate_moc_topic()` / `_topic_matches_allowed()` interpret an empty list as "validation disabled, approve everything."

**Detailed description**:
This is implemented in two places that must stay in agreement: `resolve_allowed_topics()` returns `[]` for the "no override, no taxonomy" branch without ever raising (under permissive conditions), and separately, `gardener._topic_matches_allowed()` and `gardener._validate_moc_topic()` both explicitly check `if not allowed: return True` before attempting any substring match. This double implementation (taxonomy.py doesn't itself decide "allow all" — it just returns an empty list; the *interpretation* of empty-as-allow-all lives entirely in gardener.py's consumer functions) means `taxonomy.py` is agnostic to whether an empty whitelist is a real state (deliberately unconfigured system) or a transient degrade (file temporarily unavailable in permissive mode) — from the component's own perspective these are indistinguishable, which is a documented ambiguity accepted by design (see the docstring's four bullet cases in `resolve_allowed_topics`).

The practical effect is a safety valve: an administrator can disable topic curation entirely simply by leaving `gardener.topics_path` unset in config (which the Pydantic `field_validator` normalizes empty string / `None` to `None`), or by setting `strict_topics: false` and letting the file go missing — both converge on the same "no validation" runtime state. Conversely, this means a typo'd or accidentally-emptied `moc_topics.yaml` (a dict with an empty `taxonomia_conhecimento` list) is valid YAML that parses successfully into `MocTaxonomy` with zero pilares, silently producing an empty whitelist — the Pydantic schema does not enforce a minimum of one category, so this failure mode does not raise `TaxonomyLoadError` at all and would not be caught by the `strict` gate (see Technical Debt, section 10).

**Rule workflow**:
```
# taxonomy.py: purely returns [] when nothing else applies
resolve_allowed_topics(...) -> ([], detail)   # no explicit "allow-all" flag

# gardener.py: interprets emptiness
_topic_matches_allowed(topic, allowed):
    if not allowed: return True    # allow-all interpretation happens HERE, not in taxonomy.py
    ...substring match...
```

---

### Business Rule: Bidirectional Substring Matching for Topic/Category Equivalence

**Overview**:
Although implemented in `gardener.py` rather than `taxonomy.py` itself, this rule is the direct and only consumer-side use of the whitelist that `taxonomy.py` produces, so it is documented here as part of the taxonomy component's functional contract. A generated MOC topic is considered "in" the taxonomy if either string contains the other, case-insensitively — not exact match, not prefix match.

**Detailed description**:
`_topic_matches_allowed()` and `_validate_moc_topic()` both lower-case both strings and check `allowed_lower in topic_lower or topic_lower in allowed_lower`. This bidirectional containment check is intentionally permissive in both directions: it allows an LLM to produce a *more specific* topic than the category name (e.g. category "Deep Learning e Modelos Neurais" matches generated topic "Deep Learning e Modelos Neurais Avancados" because the category string is a substring of the topic), and it also allows a *shorter* LLM-produced topic to match a longer category name (e.g. topic "Deep Learning" matches because it is a substring of the category). Both directions are explicitly covered by `test_validate_topic_substring`.

This design trades precision for flexibility: because `taxonomy.py` only exposes category-level names (not the granular topicos) as the whitelist, and LLMs do not reliably reproduce a category label verbatim, exact-match validation would reject a large fraction of otherwise-correct generations. The substring approach is a pragmatic middle ground, but it is also the source of the false-positive risk noted in section 3's "empty name" defensive check above (an empty category name would match everything) and more generally means any category name that is a common short word or a substring of an unrelated topic could cause an incorrect pass. No length minimum or word-boundary check guards against this.

**Rule workflow**:
```
_validate_moc_topic(cfg, moc_output):
    allowed, _ = resolve_allowed_topics(topics_path, allowed_topics, strict=strict_topics)
    if not allowed: return True
    for allowed_topic in allowed:
        if allowed_topic.lower() in moc_output.topic.lower()
           or moc_output.topic.lower() in allowed_topic.lower():
            return True
    return False if strict_topics else True   # permissive mode logs and approves anyway
```

---

## 4. Component Structure

`taxonomy.py` is a flat, single-file module with no sub-package. Its structure by section:

```
zettel/
└── taxonomy.py                      # MOC topic taxonomy: load, validate, derive
    ├── Categoria(BaseModel)         # leaf: nome + topicos[] (Pydantic model)
    ├── Pilar(BaseModel)             # mid: pilar name + categorias[] (Pydantic model)
    ├── MocTaxonomy(BaseModel)       # root: taxonomia_conhecimento: list[Pilar]
    ├── TaxonomyLoadError(Exception) # raised on missing/invalid YAML
    ├── load_moc_taxonomy(path)      # read YAML -> validate -> MocTaxonomy
    ├── allowed_topic_names(tax)     # flatten -> deduped list[str] of categoria.nome
    ├── format_taxonomy_for_prompt(tax)  # render full hierarchy as markdown
    └── resolve_allowed_topics(...)  # single entry point combining the above +
                                      # override/strict precedence rules
```

Consumers (outside the component boundary, shown for context):
```
zettel/
├── gardener.py            # run_garden(), _create_new_moc(), _validate_moc_topic(),
│                           # _topic_matches_allowed() — imports TaxonomyLoadError,
│                           # resolve_allowed_topics
├── gardener_assign.py      # load_category_names(), embed_category_labels() —
│                           # imports allowed_topic_names, load_moc_taxonomy
├── gardener_hub.py         # hub MOC generation prompt — imports resolve_allowed_topics
├── cli.py                  # `zettel doctor` health check — imports allowed_topic_names,
│                           # load_moc_taxonomy directly (bypasses resolve_allowed_topics)
└── config.py                # GardenerConfig.topics_path / allowed_topics / strict_topics
                              # (the config surface that parameterizes every call)
```

Data file consumed (not part of the component's code, but its sole runtime input):
```
config/
└── moc_topics.yaml          # 10 pilares, 32 categorias, ~100 topicos (201 lines)
                              # single source of truth referenced by gardener.topics_path
```

## 5. Dependency Analysis

```
Internal Dependencies:
(none within zettel/ — taxonomy.py imports no other zettel module)

Internal Dependents (reverse — who depends on taxonomy.py):
gardener.py            -> taxonomy.TaxonomyLoadError, resolve_allowed_topics
gardener_assign.py      -> taxonomy.allowed_topic_names, load_moc_taxonomy
gardener_hub.py         -> taxonomy.resolve_allowed_topics
cli.py (doctor command) -> taxonomy.allowed_topic_names, load_moc_taxonomy
config.py                -> no import, but GardenerConfig fields parameterize every
                            taxonomy.py call (topics_path, allowed_topics, strict_topics)

External Dependencies:
- PyYAML (`yaml.safe_load`)         - YAML parsing, no version pin observed in module
- Pydantic (`BaseModel`, `Field`)   - schema validation (v2 API: model_validate)
- Python stdlib `pathlib.Path`      - path handling
- Python stdlib `logging`           - warning logs on degrade path
```

The component has **zero internal (`zettel.*`) dependencies** — it is a leaf node in the project's dependency graph, depending only on third-party libraries already used project-wide (PyYAML, Pydantic). This gives it very low efferent coupling and makes it trivially reusable/testable in isolation, which is reflected in the coupling table below.

## 6. Afferent and Efferent Coupling

Coupling is measured at the level of the module's public symbols (functions/classes), since `taxonomy.py` is procedural/data-model style rather than object-oriented with multiple interacting classes.

```
| Component (symbol)          | Afferent Coupling | Efferent Coupling | Critical |
|------------------------------|-------------------|--------------------|----------|
| resolve_allowed_topics       | 3 (gardener.py x2 call sites, gardener_hub.py) | 2 (load_moc_taxonomy, format_taxonomy_for_prompt) | High |
| load_moc_taxonomy            | 3 (resolve_allowed_topics, gardener_assign.load_category_names, cli.py doctor) | 1 (yaml.safe_load + Pydantic MocTaxonomy) | High |
| allowed_topic_names          | 2 (resolve_allowed_topics, gardener_assign.load_category_names, cli.py doctor — 3 call sites) | 0 (pure function over MocTaxonomy) | Medium |
| format_taxonomy_for_prompt   | 1 (resolve_allowed_topics) | 0 (pure function over MocTaxonomy) | Low |
| TaxonomyLoadError            | 4 (load_moc_taxonomy raises; gardener.py x2, resolve_allowed_topics catch) | 0 | Medium |
| MocTaxonomy / Pilar / Categoria (models) | 1 (load_moc_taxonomy validates into these) | 0 | Low |
```

`resolve_allowed_topics` and `load_moc_taxonomy` are the module's high-traffic, high-criticality surface: three independent call sites re-invoke the full load-and-validate cycle per garden run (once in `run_garden` fail-fast, once in `_create_new_moc`, once in `_validate_moc_topic` — plus `gardener_assign.load_category_names` for bucket assignment), meaning the module's afferent coupling is concentrated but its efferent coupling stays minimal (it does not reach back into any zettel module).

## 7. Endpoints

Not applicable — `taxonomy.py` exposes no REST/GraphQL/gRPC/CLI-command surface of its own. It is a pure library module invoked in-process by `gardener.py`, `gardener_assign.py`, `gardener_hub.py`, and the `zettel doctor` CLI command's health-check logic. (The `zettel doctor` command itself is a CLI entry point, but it belongs to `cli.py`'s component boundary, not this one.)

## 8. Integration Points

```
| Integration              | Type          | Purpose                                   | Protocol       | Data Format | Error Handling                          |
|---------------------------|---------------|--------------------------------------------|----------------|-------------|------------------------------------------|
| config/moc_topics.yaml    | Local File    | Source of the MOC category taxonomy        | Filesystem I/O | YAML        | TaxonomyLoadError on missing/invalid file; caller decides raise-vs-degrade via `strict` |
| Gardener pipeline (gardener.py) | Internal Module | Consumes whitelist + detail markdown for MOC topic validation and LLM prompt context | In-process function call | Python list[str] / str | Caller wraps every call in try/except TaxonomyLoadError |
| Gardener assignment (gardener_assign.py) | Internal Module | Consumes category names for embedding + note-to-category bucket assignment | In-process function call | Python list[str] | Best-effort: catches all exceptions broadly, returns [] on failure (does not propagate TaxonomyLoadError) |
| CLI doctor (cli.py)       | Internal Module | Health-check: verifies taxonomy file loads and reports category count | In-process function call | Python int / str | Catches Exception broadly, reports as a failed check row rather than raising |
```

No network calls, no database access, no message queues — the component's only I/O is a single local file read.

## 9. Design Patterns & Architecture

```
| Pattern                     | Implementation                                   | Location                     | Purpose                                                         |
|-------------------------------|---------------------------------------------------|-------------------------------|------------------------------------------------------------------|
| Schema Validation (DTO)       | Categoria / Pilar / MocTaxonomy Pydantic models   | zettel/taxonomy.py:14-25       | Enforce structural correctness of untrusted YAML input           |
| Facade / Single Entry Point   | resolve_allowed_topics() wraps load+derive+precedence | zettel/taxonomy.py:73-104   | Give callers one function instead of composing load/allowed/format themselves |
| Fail-Fast vs. Graceful Degradation (Strategy via flag) | `strict: bool` parameter branches raise-vs-log | zettel/taxonomy.py:95-98      | Let different callers (top-level run vs. per-cluster) choose their own failure tolerance |
| Custom Domain Exception       | TaxonomyLoadError(Exception)                      | zettel/taxonomy.py:28-29       | Distinguish taxonomy-specific failures from generic I/O/validation errors for targeted except clauses |
| Pure Function / No Shared State | allowed_topic_names(), format_taxonomy_for_prompt() take `tax` as an argument, no globals/caching | zettel/taxonomy.py:49-70 | Testability — every function is deterministic given its inputs |
```

Architecturally, the module follows a "load raw -> validate into typed model -> derive views" pipeline shape, entirely stateless between calls (no caching of the parsed `MocTaxonomy`, no module-level singleton). This is consistent with the project's general convention (per CLAUDE.md) of avoiding hidden state and keeping config-driven modules re-readable.

## 10. Technical Debt & Risks

```
| Risk Level | Component Area                              | Issue                                                                                   | Impact                                                                                 |
|------------|-----------------------------------------------|-------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| Medium     | load_moc_taxonomy / resolve_allowed_topics     | No caching — the YAML is parsed from disk and re-validated on every call. In `_create_new_moc` and `_validate_moc_topic`, this happens twice per MOC candidate, and `run_garden` adds a third read at start. For a garden run with many clusters this is repeated, unnecessary disk I/O + Pydantic validation work per cluster. | Performance overhead scales with cluster count; not correctness-affecting but wasteful, especially since the file cannot change mid-run in any supported workflow. |
| Medium     | MocTaxonomy schema                             | No minimum-length constraint on `taxonomia_conhecimento`, `categorias`, or `nome`. A YAML file with `taxonomia_conhecimento: []` (empty list) or a category with `nome: ""` parses successfully — the empty-name case is defended against downstream in `allowed_topic_names` (`if cat.nome`), but an entirely empty taxonomy silently produces an empty whitelist rather than raising `TaxonomyLoadError`, which means `strict_topics=True` provides no protection against an accidentally emptied file (only a missing/malformed one). | A silently-emptied taxonomy file degrades to "allow all" MOC topics even under strict mode, which contradicts the apparent intent of `strict_topics: true` in config.yaml. |
| Low        | gardener_assign.load_category_names             | Catches bare `Exception` (not just `TaxonomyLoadError`) and logs+returns `[]` — this masks unrelated bugs (e.g. a TypeError from a future taxonomy.py refactor) as "taxonomy unavailable," making debugging harder. | Reduced diagnosability; a genuine programming error could be misattributed to "taxonomy file problem." |
| Low        | Duplicated substring-match / whitelist-resolution logic in gardener.py | `_topic_matches_allowed()` and `_validate_moc_topic()` each independently call `resolve_allowed_topics()` and re-implement the same lower-case substring check; this logic conceptually belongs to the taxonomy/validation domain but lives entirely in gardener.py, split across two near-duplicate functions. | Two near-duplicate substring-matching implementations to keep in sync if the matching rule ever changes; not a taxonomy.py-internal issue but affects the component's effective contract, since taxonomy.py's whitelist is meaningless without this consumer-side logic. |
| Low        | Bidirectional substring matching (see Business Rules) | No word-boundary or minimum-length guard on the substring check consuming `allowed_topic_names()`'s output — a short or generic category name could false-positive-match unrelated topics. | Reduced validation precision; is a known accepted trade-off per the code's design, but not documented as a formal risk anywhere in-repo. |
| Low        | No caching / no file-watch                     | If `config/moc_topics.yaml` is edited mid-process (e.g., via the web UI or an external editor) while a long garden run is in progress, different clusters in the same run could be validated against different taxonomy content, since each call re-reads the file fresh. | Non-deterministic validation behavior within a single run under concurrent file edits — an edge case, but possible given the web UI's job-queue architecture allows the vault/config to be touched externally. |
```

## 11. Test Coverage Analysis

There is **no dedicated `tests/test_taxonomy.py` file**. All coverage of `zettel/taxonomy.py` lives inside `tests/test_gardener.py`, in a clearly demarcated `# ── Taxonomy YAML ──` section (lines 69–157) plus a `# ── Topic Validation Tests ──` section (lines 160–220) that exercises the gardener-side consumer logic built on top of the taxonomy whitelist.

```
| Component / Function                  | Unit Tests | Integration Tests | Coverage (qualitative) | Test Quality |
|-----------------------------------------|------------|--------------------|--------------------------|-----------------------------------------------------------------------------|
| load_moc_taxonomy()                     | 3 (test_load_moc_taxonomy, test_resolve_missing_file_strict via nested call, test_load_project_moc_topics_yaml) | 1 (smoke test against real config/moc_topics.yaml) | Good — happy path, missing-file, and real-file smoke all covered | Solid; missing: no test for a malformed-but-existing YAML (e.g. non-dict root, or Pydantic ValidationError path) |
| allowed_topic_names()                   | 1 (test_allowed_topic_names_are_categories) | 0 | Adequate for the happy path | Missing: no test for duplicate category names across pilares, nor for an empty/falsy `nome` being skipped |
| format_taxonomy_for_prompt()            | 1 (test_format_taxonomy_for_prompt) | 0 | Adequate for the happy path | Missing: no test for the empty-taxonomy case (`lines` empty -> `""` return path) |
| resolve_allowed_topics() — file path    | 1 (test_resolve_allowed_topics_from_file) | 0 | Covers "empty override + topics_path -> load from YAML" | Good |
| resolve_allowed_topics() — override precedence | 1 (test_resolve_allowed_topics_override) | 0 | Covers "override wins for whitelist, detail still from file" | Good, directly validates the split-sourcing business rule |
| resolve_allowed_topics() — strict missing-file | 1 (test_resolve_missing_file_strict) | 0 | Covers strict=True + no override + missing file -> raises | Good |
| resolve_allowed_topics() — permissive missing-file | 1 (test_resolve_missing_file_permissive) | 0 | Covers strict=False + missing file -> degrades to [] | Good |
| resolve_allowed_topics() — strict + override + missing file | 0 | 0 | **Not covered**: the `strict=True, override=[...], topics_path=<missing>` combination (verifies `not override` short-circuit) is untested | Gap |
| resolve_allowed_topics() — invalid (not missing) file, both strict modes | 0 | 0 | **Not covered**: only the missing-file branch of TaxonomyLoadError is tested; a present-but-malformed YAML (non-dict root, or schema violation) going through resolve_allowed_topics is untested (load_moc_taxonomy's own malformed-file paths are also untested directly) | Gap |
| Empty-taxonomy edge case (`taxonomia_conhecimento: []`) | 0 | 0 | **Not covered** — the "silently produces empty whitelist even under strict_topics" risk noted in section 10 has no regression test | Gap |
| gardener._topic_matches_allowed / _validate_moc_topic (consumer logic) | 6 (test_validate_topic_in_list, test_validate_topic_substring [2 assertions], test_validate_topic_strict_reject, test_validate_topic_permissive, test_validate_empty_list, test_validate_from_taxonomy_file) | 0 | Strong — covers exact match, substring both directions, strict reject, permissive accept, empty-whitelist allow-all, and file-backed validation | Good; missing: no negative test for the bidirectional-substring false-positive risk (e.g. a short/generic category name incorrectly matching an unrelated topic) |
```

**Test file locations** (relative to repo root):
- `tests/test_gardener.py` — lines 1–38 (imports/fixtures), 69–157 (taxonomy loading), 160–220 (topic validation consumer logic). This is the sole test file touching `zettel/taxonomy.py`; it is co-located with Gardener tests rather than isolated, reflecting the component's status as an internal helper to the Gardener rather than a standalone feature.
- No references to `taxonomy` found in `tests/test_config.py` beyond the unrelated `_PYTHON_ONLY_PATHS` constant (which documents that `gardener.allowed_topics` is a Python-only config override, not part of the YAML schema round-trip tests).
- No references to `taxonomy` in `tests/test_gardener_hub.py` beyond an unrelated test name (`test_purge_hub_pipeline_mocs_keeps_taxonomy`) that does not exercise the taxonomy module directly.

**Overall assessment**: functional/happy-path coverage of the public API is good, and the core precedence/strict-vs-permissive business rules are each backed by a dedicated test. The main gaps are all on the *malformed-input* and *empty-but-valid* edge cases — no test drives a present-but-schema-invalid YAML file through either `load_moc_taxonomy()` directly or `resolve_allowed_topics()`, and no test exists for the empty-`taxonomia_conhecimento` risk identified in section 10.

---

**Component analyzed:** `taxonomy` (`zettel/taxonomy.py`)
**Report saved to:** `D:\projetos\zettel_app\docs_project\component-deep-analyzer\component-analysis-taxonomy-2026-08-30_10-22-26.md`
