"""MOC topic taxonomy — load hierarchical YAML and derive prompt/validation lists."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class Categoria(BaseModel):
    nome: str
    topicos: list[str] = Field(default_factory=list)


class Pilar(BaseModel):
    pilar: str
    categorias: list[Categoria] = Field(default_factory=list)


class MocTaxonomy(BaseModel):
    taxonomia_conhecimento: list[Pilar] = Field(default_factory=list)


class TaxonomyLoadError(Exception):
    """Raised when the taxonomy YAML is missing or invalid under strict mode."""


def load_moc_taxonomy(path: Path | str | None) -> MocTaxonomy:
    """Load and validate ``config/moc_topics.yaml`` (or equivalent)."""
    if path is None:
        raise TaxonomyLoadError("gardener.topics_path nao configurado")
    p = Path(path)
    if not p.exists():
        raise TaxonomyLoadError(f"Arquivo de taxonomia nao encontrado: {p}")
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise TaxonomyLoadError(f"Taxonomia invalida (esperado mapping YAML): {p}")
    try:
        return MocTaxonomy.model_validate(raw)
    except Exception as e:
        raise TaxonomyLoadError(f"Taxonomia invalida em {p}: {e}") from e


def allowed_topic_names(tax: MocTaxonomy) -> list[str]:
    """Category names used as the MOC ``topic`` whitelist."""
    names: list[str] = []
    for pilar in tax.taxonomia_conhecimento:
        for cat in pilar.categorias:
            if cat.nome and cat.nome not in names:
                names.append(cat.nome)
    return names


def format_taxonomy_for_prompt(tax: MocTaxonomy) -> str:
    """Render the full hierarchy as markdown for the LLM reference section."""
    lines: list[str] = []
    for pilar in tax.taxonomia_conhecimento:
        lines.append(f"## Pilar: {pilar.pilar}")
        lines.append("")
        for cat in pilar.categorias:
            lines.append(f"### Categoria: {cat.nome}")
            for t in cat.topicos:
                lines.append(f"- {t}")
            lines.append("")
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def resolve_allowed_topics(
    topics_path: Path | None,
    override: list[str] | None = None,
    *,
    strict: bool = True,
) -> tuple[list[str], str]:
    """Return ``(allowed_category_names, taxonomy_detail_markdown)``.

    - Non-empty ``override`` (e.g. tests): used as the whitelist; detail still
      loaded from ``topics_path`` when the file exists.
    - Empty override + ``topics_path``: load categories from the YAML.
    - Empty override + no path: empty whitelist (validation treats as allow-all).
    - Path set but missing/invalid + ``strict``: raise ``TaxonomyLoadError``.
    """
    override = list(override or [])
    detail = "_(Taxonomia detalhada nao disponivel.)_"
    tax: MocTaxonomy | None = None

    if topics_path is not None:
        try:
            tax = load_moc_taxonomy(topics_path)
            detail = format_taxonomy_for_prompt(tax)
        except TaxonomyLoadError:
            if strict and not override:
                raise
            logger.warning("Nao foi possivel carregar taxonomia de %s", topics_path)

    if override:
        return override, detail
    if tax is not None:
        return allowed_topic_names(tax), detail
    return [], detail
