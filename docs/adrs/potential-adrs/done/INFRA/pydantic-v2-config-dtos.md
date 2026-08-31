# Potential ADR: Pydantic v2 for Configuration Schema and LLM-Backed DTOs

**Module**: INFRA (Configuration & Data Validation)  
**Category**: Primary Framework / Data Validation  
**Priority**: Must Document (Score: 140)  
**Date Identified**: 2026-08-30

---

## What Was Identified

The project uses Pydantic v2 throughout for two critical purposes:
1. **Configuration schema** (`AppConfig` and nested config classes in `config.py`) — 25+ nested Pydantic models define all operational parameters, validated at startup via `load_config()`.
2. **LLM-backed structured outputs** (`schemas.py`) — Pydantic models define the contract for every LLM call that expects structured JSON output (e.g., `LiteratureChunkOutput`, `PermanentNoteLLMOutput`, `MOCGenerationOutput`, `DedupeResult`, `ArticleOutline`).

Pydantic is not optional; there is no alternative validation framework in the codebase. Every configuration option, every LLM structured output, and every pipeline phase's DTOs flow through Pydantic.

**Introduced**: Foundational; Pydantic v2 was explicitly adopted (v1 was likely prior); no indication of migration to v2 in recent history, suggesting v2 was chosen at project inception.

**Modified**: Stable; recent additions follow the pattern (e.g., new `HubMocsConfig` class for `garden --hubs` feature).

---

## Why This Might Deserve an ADR

- **Impact**: Every configuration load, every LLM-call validation, and every DTO instantiation depends on Pydantic. 30+ modules import from `schemas.py` or `config.py`. Switching frameworks would require rewriting all configuration and DTO validation.
- **Trade-offs Visible**:
  - **Validation**: Pydantic provides strong type checking, field validators, and error messages. Alternative (manual validation) would be verbose and error-prone.
  - **Serialization**: Pydantic's `.model_dump()` / `.model_validate_json()` make JSON serialization of LLM responses trivial; without it, would require custom JSON parsing or dataclass decorators.
  - **Performance**: Pydantic v2 is faster than v1; no performance complaints in codebase, suggesting adequate.
  - **Dependency**: Adds pydantic v2 as a hard dependency (in pyproject.toml). No conditional import or fallback.
- **Cost to Change**: Switching to dataclasses + marshmallow, or to a lighter validation library (e.g., jsonschema), would require rewriting:
  - All config classes (10+ nested models with validators like `_dimensions_positive`, `resolve_topics_path`, `resolve_path`)
  - All LLM-response schemas (5+ DTO classes)
  - All field validators (20+ @field_validator decorators)
- **Team Knowledge**: Anyone working on configuration or pipeline DTOs must understand Pydantic v2 field definitions, validators, Field defaults, and `.model_dump()` patterns.
- **Temporal Context**: Stable for project lifetime; no recent churn. Additions (e.g., new config fields for `garden --hubs`) follow the pattern consistently.

---

## Evidence Found in Codebase

### Key Files
- [`zettel/config.py`](../../../zettel/config.py) - Entire file (395 lines)
  - 15+ Pydantic config classes (LLMConfig, EmbeddingConfig, ChunkingConfig, etc.)
  - Field validators: `_dimensions_positive`, `resolve_topics_path`, `resolve_path`

- [`zettel/schemas.py`](../../../zettel/schemas.py) - Entire file (174 lines)
  - DTOs for LLM-structured outputs: `LiteratureChunkOutput`, `PermanentNoteLLMOutput`, `MOCGenerationOutput`, `DedupeResult`, `ArticleOutline`

### Code Evidence
```python
# From zettel/config.py (configuration schema):
from pydantic import BaseModel, Field, field_validator

class AppConfig(BaseModel):
    vault_path: Path = Path("./vault")
    inbox_path: Path = Path("./data/inbox")
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    # ... 10+ nested config classes

class EmbeddingConfig(BaseModel):
    provider: Literal["openai", "sentence-transformers", "ollama"] = "openai"
    dimensions: int | None = None

    @field_validator("dimensions")
    @classmethod
    def _dimensions_positive(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if int(v) < 1:
            raise ValueError("embedding.dimensions deve ser >= 1 (ou null)")
        return int(v)

# From zettel/schemas.py (LLM-response DTOs):
class LiteratureChunkOutput(BaseModel):
    """Output from LLM Prompt 1 (literature note generation)."""
    thesis: str
    anchor_quote: str
    definition: str
    # ...

class PermanentNoteLLMOutput(BaseModel):
    """Output from LLM Prompt 2 (permanent note generation)."""
    title: str
    body: str
    suggested_connections: list[str]

# Validation at config load time:
def load_config(path: Path | str | None = None) -> AppConfig:
    # YAML -> dict -> AppConfig (Pydantic validates here)
    raw_yaml = yaml.safe_load(config_file)
    return AppConfig(**raw_yaml)  # Pydantic validation happens here
```

### Impact Analysis
- **Introduced**: Foundational (Pydantic v2 present from project inception per pyproject.toml)
- **Modified**: Config classes evolve additively (new config groups for new features like `garden --hubs`, `images`, `hub_mocs`); DTOs added per LLM prompt (5 DTOs currently)
- **Last change**: Recent additions follow the pattern (no drift); stable usage
- **Files affected**: config.py (config validation), schemas.py (LLM-response validation), every phase that calls LLMs (extractor, connector, gardener, ask, article, assets for image description)
- **Scope**: Large (25+ modules import from config or schemas; foundational for type safety)

### Validators in Use
- `EmbeddingConfig._dimensions_positive` — ensures dimensions ≥ 1
- `GardenerConfig.resolve_topics_path` — converts string path to resolved Path object
- `AppConfig.resolve_path` — converts all path fields to absolute paths (6 path fields)

### Serialization Patterns
```python
# LLM call with Pydantic response:
llm_response_json = call_llm(prompt, ...)
validated_dto = LiteratureChunkOutput.model_validate_json(llm_response_json)
# Now validated_dto is guaranteed to have all required fields with correct types

# Config dump for logging:
config.model_dump(exclude_none=True)
```

---

## Questions to Address in ADR (if created)

- Why Pydantic v2 specifically? (v1 is older, v2 is faster; what was the migration driver?)
- Are there performance-critical code paths where Pydantic validation is a bottleneck? (No complaints visible, suggesting acceptable.)
- Should LLM-response validation be stricter (e.g., fail on extra fields) or more lenient (coerce types)? (Currently: default Pydantic behavior.)
- Could the configuration schema be generated from YAML JSON Schema instead of hand-written Pydantic classes? (Would reduce duplication but add tooling complexity.)

## Related Potential ADRs
- YAML-First Configuration with Pydantic Fallback (uses Pydantic for validation)
- Hybrid Dense+BM25 Retrieval (thresholds defined in RelevanceFloorConfig via Pydantic)

## Additional Notes
- Pydantic v2 includes built-in JSON serialization via `.model_dump()` / `.model_dump_json()`, used throughout for config logging and LLM-response validation.
- Field validators are minimal (3 custom validators across 15+ config classes), suggesting good design (most configs don't need custom validation).
- No visible use of Pydantic's discriminated unions or advanced features (e.g., computed fields); usage is straightforward.
- The `Field(default_factory=...)` pattern is used for nested config objects, enabling safe defaults and immutability.
