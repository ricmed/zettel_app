# ADR-XXX: Pydantic v2 for Configuration Schema and LLM-Backed DTOs
**Status:** Accepted
**Date:** 2024-08-30
**Related to:** [ADR-XXX: YAML-First Configuration with Pydantic Fallback](./ADR-004-yaml-first-configuration.md)

## Context and Problem Statement

The system needs a single validation mechanism for two distinct but related concerns: validating the operational configuration loaded from YAML at startup, and validating structured JSON output returned by LLM calls across every pipeline phase. Configuration spans 15+ nested classes (LLM, embedding, chunking, retrieval, gardener, and related settings), and LLM-backed data transfer objects cover the structured outputs for literature notes, permanent notes, MOC generation, deduplication, and article outlines.

Both concerns share the same requirements: strict type checking, clear validation errors, default values for optional fields, and straightforward serialization back to JSON or dict form for logging and downstream calls. Handling them with two different mechanisms (e.g., dataclasses for config, a separate schema library for LLM output) would duplicate validation logic and increase the surface area new contributors need to learn.

Pydantic v2 was already present at project inception, with no evidence in the codebase history of a prior version or a migration from v1. There is no conditional import or fallback path; Pydantic is a hard dependency for both configuration loading and every LLM call that expects structured output.

## Decision Drivers

* Every configuration load and every LLM structured-output validation must go through one consistent mechanism used across 25+ modules.
* Nested configuration (15+ classes) needs type coercion, default values via `Field(default_factory=...)`, and a small number of custom validators (e.g., dimension bounds, path resolution).
* LLM responses must be parsed and validated as structured JSON without hand-written parsing code for each of the five DTOs in `schemas.py`.
* Serialization back to dict/JSON is needed for config logging (`model_dump`) and is exercised on every LLM call boundary.
* Switching cost is high: 15+ config classes, 5+ DTO classes, and 20+ field validators would all need to be rewritten.

## Considered Options

* Pydantic v2 (chosen)
* Standard library dataclasses with hand-written manual validation
* A lighter schema-validation library (e.g., jsonschema) layered over plain Python objects

## Decision Outcome

Chosen option: Pydantic v2, because it already underpins both configuration and LLM structured-output validation across the entire codebase, its `model_validate_json` / `model_dump` pair directly matches the deserialize-validate-serialize cycle needed at the LLM boundary, and no performance or correctness issue with the current usage is visible in the codebase. Rewriting to an alternative would touch every config class, every LLM DTO, and every module that imports from `config.py` or `schemas.py`, with no documented technical driver for doing so.

[NEEDS INPUT: The specific reason v2 was chosen over v1 at project inception (e.g., performance, stricter validation, ecosystem alignment) is not recoverable from the codebase — no migration commit or comment exists.]

## Pros and Cons of the Options

### Pydantic v2

* Good, because it provides strong type checking and field validators with minimal boilerplate (only 3 custom validators across 15+ config classes).
* Good, because `.model_dump()` / `.model_validate_json()` give built-in JSON (de)serialization, used for both config logging and LLM-response parsing.
* Good, because v2 is faster than v1 with no performance complaints observed in this codebase.
* Bad, because it is a hard dependency with no fallback or conditional import if it becomes unavailable or needs replacing.

### Dataclasses with manual validation

* Good, because it removes an external dependency and gives full control over validation logic.
* Bad, because hand-written validation for 15+ config classes and 20+ current validation rules would be verbose and error-prone.
* Bad, because there is no built-in JSON-schema validation for LLM structured output, requiring custom parsing per DTO.

### Lightweight schema library (e.g., jsonschema)

* Good, because JSON Schema is a portable, language-agnostic format.
* Bad, because it is less ergonomic with Python type hints and IDE support than Pydantic models.
* Bad, because it would still require a separate mapping layer from validated JSON into Python objects, duplicating what Pydantic already provides.

## Consequences

Configuration and LLM-response validation follow one consistent pattern project-wide: nested `BaseModel` classes with `Field` defaults and targeted `@field_validator` decorators only where default coercion is insufficient (e.g., path resolution, positive-integer checks). This keeps custom validation code small relative to the number of config classes.

The trade-off is a hard, uncontested dependency on Pydantic v2: any breaking change in the library requires coordinated updates across `config.py`, `schemas.py`, and every one of the 25+ modules that import from them. New configuration groups and LLM DTOs are expected to follow the established pattern (nested `BaseModel`, `Field(default_factory=...)`, minimal validators), as already demonstrated by additive changes such as the `garden --hubs` configuration.

[NEEDS INPUT: Whether LLM-response validation should be tightened (e.g., reject extra fields) or kept at Pydantic's default lenient behavior has not been decided; current behavior is unexamined default coercion.]

## References

* zettel/config.py:256 (AppConfig, root configuration schema)
* zettel/config.py:62 (EmbeddingConfig, with the `_dimensions_positive` validator)
* zettel/schemas.py:61 (LiteratureChunkOutput, LLM Prompt 1 structured output)
* zettel/schemas.py:159 (PermanentNoteLLMOutput, LLM Prompt 2 structured output)
