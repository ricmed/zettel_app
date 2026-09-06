# ADR-042: Domain as First-Class Config and Externalized Few-Shots

**Status:** Accepted  
**Date:** 2026-09-06  
**Related to:** [ADR-004](./ADR-004-yaml-first-configuration.md), [ADR-019](../GARDEN/ADR-019-taxonomy-first-moc-clustering.md), [ADR-025](../LLM/ADR-025-prompt-caching-system-human-split.md)

## Context and Problem Statement

`gardener.domain` filled `{domain}` in extract, connect and garden prompts, but extract and connect are not the gardener. A typo in that free-text string also prefixed every category-label embedding (`"{domain}: {categoria}"`), silently collapsing the argmax that assigns notes to buckets. Few-shots that define the 1–5 relevance scale lived hardcoded in `prompts/literature_note.md` as Machine Learning examples — a Kant argument had no ruler.

## Decision Outcome

**Chosen:** a top-level `DomainConfig` (`domain.name`, `domain.examples_path`) and a leaf loader `zettel/domain_examples.py` (YAML + Pydantic only). `gardener.domain` is removed. No alias and no fallback to the old path.

- `{domain}` in the four prompts comes from `cfg.domain.name` (non-blank).
- Few-shots live in `config/domain_examples.yaml` with both domains side by side so a "level 5" of ML next to a "level 5" of Philosophy teaches density, not field proximity.
- `render_for_prompt` always returns every placeholder key (empty string if the section is missing) because `fill_template` leaves unknown keys literal.
- Callers load examples **once per run** and list every key in the `mapping = {...}` literal so `tests/test_prompts.py` can see them via AST.
- `accepted_example` validates against `LiteratureChunkOutput`.
- Category labels no longer use `domain` as prefix (ADR-019 amendment): default template is `{pilar}: {categoria}`.

## Consequences

Changing `domain.name` no longer moves every category vector. Changing few-shots changes the relevance ruler for material extracted **from then on** (`run_extract` has no `--force`). `zettel doctor` checks the examples file the same way it checks the taxonomy.

## References

* GitHub issues #156, #157, #160
* `zettel/config.py` (`DomainConfig`), `zettel/domain_examples.py`, `config/domain_examples.yaml`
