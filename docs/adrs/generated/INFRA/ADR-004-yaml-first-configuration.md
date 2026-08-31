# ADR-XXX: YAML-First Configuration with Pydantic Fallback

**Status:** Accepted
**Date:** 2025-02-01
**Used by:** [ADR-XXX: Pluggable Multi-Provider LLM Strategy](../LLM/ADR-XXX-pluggable-multi-provider-llm-strategy.md)
**Related to:** [ADR-XXX: Pydantic v2 for Configuration Schema and LLM-Backed DTOs](./ADR-006-pydantic-v2-config-dtos.md)

## Context and Problem Statement

The project needs a single, consistent strategy for supplying configuration to 25+ modules that depend on `config.py`, covering everything from vault paths to LLM providers, embedding settings, retrieval thresholds, and gardener/hub parameters. Two common approaches exist: define defaults in code and let an optional file override them (the typical framework pattern, e.g. Flask), or treat an external file as the authoritative source and use code only as a fallback for missing entries.

The project chose the second approach: `config/config.yaml` is the operational source of truth at runtime, and every key is expected to live there. The Pydantic schema (`AppConfig` and its nested classes in `config.py`) supplies Field defaults only for keys absent from the YAML file, or when the file itself does not exist — primarily so tests can run without a full configuration file. Secrets (API keys, `SESSION_SECRET`) are deliberately excluded from this contract and are read from `.env` via `python-dotenv` instead.

This inversion was formalized in a dedicated commit that made `config.yaml` the explicit operational source of truth, after an earlier period where code defaults were dominant and the contract was ambiguous. [NEEDS INPUT: Was YAML-first adopted to satisfy an explicit audit or deployment requirement, or did it emerge as an internal convention that was later formalized?]

## Decision Drivers

* Operational values must be explicit and diffable in version control for auditability.
* Deployments and CI/CD need to swap environments by replacing a file rather than rebuilding code.
* Tests need to run in isolation without requiring a full, valid `config.yaml`.
* Secrets must stay out of version control, separated from operational configuration.
* A single, teachable contract is needed for a codebase with 25+ modules depending on configuration.
* New configuration groups (e.g. `hub_mocs` for `garden --hubs`) need one established pattern to follow rather than an ad hoc choice each time.

## Considered Options

* YAML-first configuration with Pydantic Field defaults as fallback only
* Code-defaults-first configuration with optional YAML override (conventional framework pattern)

## Decision Outcome

Chosen option: "YAML-first configuration with Pydantic Field defaults as fallback only", because it makes `config.yaml` the single, auditable record of what a given deployment actually runs, while Pydantic defaults exist purely as scaffolding so unit tests and new environments don't require a complete file. Secrets remain environment-only via `.env`, kept outside this contract entirely.

[NEEDS INPUT: Should a config-validation tool be introduced to check that every Pydantic Field has a corresponding YAML entry, or is enforcement through code review considered sufficient going forward?]

## Pros and Cons of the Options

### YAML-first configuration with Pydantic fallback

* Good, because every operational value is explicit and reviewable as a version-control diff.
* Good, because environments can be swapped via an alternate file (`ZETTEL_CONFIG`) with no code changes.
* Good, because tests can run without a full `config.yaml`, using Field defaults as scaffolding.
* Bad, because a missing YAML key silently falls back to its code default, which can mask a real production misconfiguration (e.g. an unset `llm.provider` silently becomes `"openai"`).
* Bad, because no automated tool verifies the "every key must be in YAML" contract; it depends entirely on code review.

### Code-defaults-first with optional YAML override

* Good, because it matches conventional framework behavior, lowering onboarding friction for developers familiar with that pattern.
* Good, because options that rarely change require no YAML boilerplate.
* Bad, because operational values would live partly in code, reducing auditability of what is actually running in a given deployment.
* Bad, because adopting it now would require auditing every existing YAML key against Pydantic fields and confirming no test relies on current fallback behavior.

## Consequences

Config changes remain visible as version-control diffs, and deployments can switch environments by pointing at a different YAML file without touching code, which the `ZETTEL_CONFIG` test-override mechanism already exercises. Tests stay fast and isolated because they don't need a complete configuration file to exercise pipeline modules.

Without an enforcement tool, the "every Field must be in YAML" contract can erode over time as new options are added in code without a matching YAML entry, and a missing production key currently produces no error — only a silent fallback to the code default. [NEEDS INPUT: What is the intended behavior when `config.yaml` is missing or corrupted in production — continue using defaults silently (current behavior), or fail fast at startup?] Any future change to this strategy would require auditing every YAML key against the Pydantic schema across all 25+ dependent modules before it could be adopted safely.

## References

* zettel/config.py:300 — `load_config()`, the YAML-first loading logic
* zettel/config.py:256 — `AppConfig` schema definition
* zettel/config.py:42 — `LLMConfig`, an example nested config class with Field defaults
* config/config.yaml — the operational source of truth
