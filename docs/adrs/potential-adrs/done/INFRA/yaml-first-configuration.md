# Potential ADR: YAML-First Configuration with Pydantic Fallback

**Module**: INFRA (Configuration subsystem)  
**Category**: Configuration Architecture  
**Priority**: Must Document (Score: 125)  
**Date Identified**: 2026-08-30

---

## What Was Identified

The project implements a "YAML-first, code defaults as fallback" configuration strategy: `config/config.yaml` is the authoritative source of truth at runtime, with every key stored as YAML. The Pydantic schema (`AppConfig` in `config.py`) provides Field defaults only when YAML keys are missing or the file doesn't exist. This is a deliberate inversion of typical framework patterns (e.g., Flask defaults-in-code with optional config file override).

The contract is explicit in comments: "toda chave do schema deve estar no YAML" (every schema key must be in YAML). Environment variables are strictly separated: secrets (API keys, SESSION_SECRET) come from `.env` via `python-dotenv`, never from `config.yaml`.

**Introduced**: Formalized in `8ac6f32` ("feat(config): make config.yaml the operational source of truth"), suggesting prior iteration where code defaults were dominant. Earlier commits show config loading existed but the contract was ambiguous.

**Modified**: Stable since formalization. Additions (e.g., `hub_mocs` config for `garden --hubs`) are added to YAML directly, with Pydantic defaults only as scaffolding for tests.

---

## Why This Might Deserve an ADR

- **Impact**: Every module (25+ dependents on `config.py` per mapping) reads configuration through this pattern. Changing the strategy would require migrating all config sources.
- **Trade-offs Visible**:
  - YAML-first means all configuration is externalized and version-controlled (good for auditability, bad if secrets leak).
  - Pydantic fallback provides safety (tests can run without a `config.yaml` file), but can mask missing production keys (e.g., missing `llm.provider` in YAML falls silently to "openai" default, hiding config omission).
  - The contract "every key must be in YAML" is aspirational; no enforcement tool exists (a config validator could check this, but doesn't).
  - Environment-variable secrets are NOT part of this contract (SESSION_SECRET reads from process env at startup, not config.yaml).
- **Cost to Change**: Switching to code-defaults-first would require audit of all YAML files, identification of keys with defaults, and verification that no tests rely on fallback behavior.
- **Team Knowledge**: Anyone adding a new configuration option must understand: add the key to `config.yaml`, update the Pydantic Field default (even if unused), and document whether the option is operational (lives in YAML) or test-only (can fall back).
- **Temporal Context**: Formalized ~18 months ago (8ac6f32); recent additions (hub_mocs config in `garden --hubs` feature) follow the pattern consistently.

---

## Evidence Found in Codebase

### Key Files
- [`zettel/config.py`](../../../zettel/config.py) - Entire file (395 lines)
  - Lines 273-300: `load_config()` function showing YAML-first logic
  - Lines 22-258: `AppConfig` + nested config classes with Field defaults
  - Comment line 275: "Contrato YAML-primeiro: cada chave do YAML substitui o Field default"

- [`config/config.yaml`](../../../config/config.yaml) - Operational source of truth (not visible in codebase, but referenced)

### Code Evidence
```python
# From zettel/config.py (load_config function):
def load_config(path: Path | str | None = None) -> AppConfig:
    """Carrega config/config.yaml (ou ``path``) e valida em AppConfig.

    Contrato YAML-primeiro: cada chave do YAML substitui o Field default;
    chave ausente (ou arquivo faltando) usa o fallback de fabrica. Segredos
    (API keys) vêm de ``.env``, nao do YAML.
    """
    # Load .env before anything that reads env vars (LLM keys, etc.)
    env_path = Path(".env")
    if env_path.exists():
        load_dotenv(env_path, override=False)
        logger.info("Variaveis de ambiente carregadas de .env")

    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}

    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
            if isinstance(raw, dict):
                data = raw
        logger.info("Configuração carregada de %s", config_path)
    else:
        logger.warning("Arquivo de config não encontrado: %s — usando defaults", config_path)

    # YAML values override Field defaults
    return AppConfig(**data)

# Pydantic schema with Field defaults (fallback only)
class AppConfig(BaseModel):
    vault_path: Path = Path("./vault")  # Default if missing from YAML
    inbox_path: Path = Path("./data/inbox")
    chroma_path: Path = Path("./data/chroma")
    state_db_path: Path = Path("./data/state.db")
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    # ... 10+ nested config classes, each with defaults

# LLMConfig example:
class LLMConfig(BaseModel):
    provider: str = "openai"  # Default if llm.provider missing from YAML
    model: str = "gpt-4o-mini"
    temperature: float = 0
    # ...

# Secrets from .env, NOT config.yaml:
# SESSION_SECRET is read at app startup:
# os.environ.get("SESSION_SECRET", "") <- from .env, never yaml
```

### Impact Analysis
- **Introduced**: Formalized in `8ac6f32` (feat: config YAML-first, ~18 months ago)
- **Modified**: Incremental additions (e.g., `hub_mocs` config group added when `garden --hubs` was introduced)
- **Last change**: Recent additions to gardener/retrieval config groups; stable pattern
- **Files affected**: config.py (loader), every module that depends on config (25+ per mapping); tests often override via `ZETTEL_CONFIG` env var pointing to alternate YAML
- **Scope**: Large (25+ afferent dependencies; first-class configuration system, not a detail)

### Contract Violations / Gaps
- **No validation tool**: No script exists to verify "every Pydantic Field has a corresponding YAML entry" — the contract is enforced by code review, not automation.
- **Fallback mask**: If a required key is missing from YAML, Pydantic silently uses the default, potentially hiding a misconfiguration (e.g., `llm.provider: null` in YAML is not rejected; code defaults to "openai").
- **Secrets not in YAML**: `SESSION_SECRET` (web login) and potentially other secrets read from process environment, not YAML. This is correct (secrets should not be version-controlled), but the contract should be explicit.
- **Test-only defaults**: Some Field defaults (e.g., `images.enabled: False`) are test-friendly but not production-relevant; the contract doesn't distinguish.

---

## Questions to Address in ADR (if created)

- Why YAML-first instead of code-defaults-first (the more common pattern in Python frameworks)?
  - Answer likely: clarity (config is a distinct artifact, auditable in version control), auditability (all operational decisions are explicit in YAML), and deployment pipeline (config files are easier to swap in CI/CD than rebuilding Python).
- How do we prevent configuration drift between `config.yaml` and the Pydantic schema?
  - Currently: code review only; a linter/validator could check `AppConfig` fields against YAML entries.
- Should secrets (API keys, SESSION_SECRET) be part of the config schema, or remain environment-only?
  - Currently: environment-only (correct), but `LLMConfig` has `provider` / `base_url` which are semi-secrets (API endpoint URLs); should these be environment-derived too?
- What happens if `config.yaml` is deleted or corrupted? (Currently: uses all defaults; should this be an error instead?)
- Should the Pydantic schema be the single source of truth for allowed config keys, or should YAML keys be validated against a whitelist?

## Related Potential ADRs
- Pydantic v2 for Configuration Schema & DTOs
- Environment-Based Secrets Management (SESSION_SECRET, LLM API keys)

## Additional Notes
- The `ZETTEL_CONFIG` environment variable allows tests/deployments to point at alternate YAML files without code changes (good for test isolation).
- Recent additions (hub_mocs, personalities.yaml) follow the YAML-first pattern consistently.
- The separation of secrets (.env via python-dotenv) from config (YAML) is a security best practice, well-enforced in code.
- No documented "config migration" or "config upgrade" process exists; if schema changes, users must manually update their YAML files.
