# ADR-041: Dual Timezone — UTC in SQLite, Vault Timezone in Frontmatter

**Status:** Accepted  
**Date:** 2026-09-05  
**Related to:** [ADR-004: YAML-First Configuration](./ADR-004-yaml-first-configuration.md), [ADR-039: Web as Python Package](../WEB/ADR-039-web-as-python-package.md)

## Context and Problem Statement

Pipeline modules stamp `created_at` / `updated_at` on vault notes and operational rows in SQLite. After adopting Ruff `DTZ005`, timestamps were written as naive local time or explicit UTC (`+00:00`), which made Obsidian frontmatter show UTC wall clock while the operator works in Brazil (`America/Sao_Paulo`). The web UI sliced ISO strings (`[:10]`) without conversion, so dates could disagree with local expectations near midnight.

The vault is being reset; there is no requirement to parse legacy naive timestamps.

## Decision Drivers

* Operators read timestamps directly in Obsidian frontmatter.
* SQLite remains the sortable source of truth for jobs, runs, and graph state across deployments.
* A single configurable IANA timezone keeps YAML-first configuration consistent (ADR-004).
* Python 3.12 `zoneinfo` avoids extra dependencies.

## Decision Outcome

**Chosen:** dual-layer timestamps via `zettel/time.py` and `vault_timezone` in `config.yaml`.

| Layer | Format | API |
|-------|--------|-----|
| SQLite (`StateDB._now()`) | UTC ISO 8601 (`+00:00`) | `now_utc_iso()` |
| Vault frontmatter | ISO 8601 in `vault_timezone` (default `America/Sao_Paulo`) | `now_vault_iso(cfg.vault_timezone)` |
| Web display | Convert any aware ISO to `vault_timezone` | Jinja filter `local_dt` |
| Ask/ART filenames | Compact local timestamp | `now_filename_ts(cfg.vault_timezone)` |

Invalid IANA names fail at `load_config`. Naive timestamps passed to `format_local_datetime` raise `ValueError` (no legacy path).

## Consequences

* New notes show `-03:00` (or DST `-02:00`) in YAML; jobs in the UI show local date/time converted from UTC rows.
* Every vault writer and managed-block update receives `vault_timezone` from `AppConfig` (no ambient `load_config()` in `vault.py`).
* Changing `vault_timezone` affects **new** writes only; existing frontmatter is not migrated.

## References

* `zettel/time.py`, `zettel/config.py`, `config/config.yaml` (`vault_timezone`)
* `zettel/web/rendering.py` — `local_dt` filter
* GitHub issue #148
