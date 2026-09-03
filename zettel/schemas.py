"""Pydantic schemas for all Zettelkasten data objects."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


# ── Enums ──────────────────────────────────────────────────────────────

ChunkStatus = Literal["accepted", "rejected"]
RejectionCategory = Literal[
    "", "structural", "narrative", "promotional", "trivial", "fragmented"
]
_REJECTION_CATEGORIES: frozenset[str] = frozenset(
    {"", "structural", "narrative", "promotional", "trivial", "fragmented"}
)


class DedupeDecision(str, Enum):
    CREATE_NEW = "create_new"
    IGNORE = "ignore"
    REFINE_EXISTING = "refine_existing"
    MERGE = "merge"


class RelationType(str, Enum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    EXTENDS = "extends"
    DEPENDS_ON = "depends_on"
    EXEMPLIFIES = "exemplifies"
    RELATED = "related"


# ── LLM Extraction Outputs ────────────────────────────────────────────


class PermanentNoteCandidate(BaseModel):
    """A single atomic concept extracted from a chunk."""
    chunk_status: str = Field(default="ok", description="Status do chunk")
    rejection_reason: str = Field(default="", description="Motivo da rejeição")
    rejection_category: str = Field(default="", description="Categoria da rejeição")
    thesis: str = Field(description="Tese principal (uma frase declarativa)")
    definition: str = Field(description="Definição/explicação autônoma")
    intuition: str = Field(default="", description="Intuição ou exemplo prático")
    limits: str = Field(default="", description="Limites ou ressalvas")
    anchor_quote: str = Field(
        default="",
        description="Citação-âncora de 10-25 palavras retirada do texto-fonte",
    )
    source_locator: str = Field(
        default="", description="Localizador na fonte (página, seção, timestamp)"
    )
    tags: list[str] = Field(default_factory=list)
    relevance_score: int = Field(
        default=3,
        ge=1, le=5,
        description="Relevancia do candidato (1=trivial, 5=fundamental)",
    )
    relevant_image_ids: list[str] = Field(
        default_factory=list,
        description="IDs de imagens (asset_id) essenciais para entender este conceito",
    )


_SUMMARY_MAX_CHARS = 280


class LiteratureChunkOutput(BaseModel):
    """Output from Prompt 1 for a single chunk — appended to LIT note."""
    chunk_status: ChunkStatus = Field(description="Status do chunk")
    rejection_reason: str = Field(description="Motivo da rejeição")
    rejection_category: RejectionCategory = Field(description="Categoria da rejeição")
    summary: str = Field(description="Resumo do chunk em PT-BR")
    key_concepts: list[str] = Field(
        default_factory=list, description="Conceitos-chave extraídos"
    )
    candidates: list[PermanentNoteCandidate] = Field(
        default_factory=list,
        description="Candidatos atômicos a notas permanentes",
    )

    @field_validator("rejection_category", mode="before")
    @classmethod
    def _normalize_unknown_category(cls, v: str) -> str:
        if v not in _REJECTION_CATEGORIES:
            logger.warning("rejection_category desconhecida do LLM: %r -- normalizada para ''", v)
            return ""
        return v

    @field_validator("summary", mode="after")
    @classmethod
    def _truncate_oversized_summary(cls, v: str) -> str:
        # summary is navigation material (filenames, review tables), not a semantic
        # contract -- truncate and warn instead of raising, so an overlong summary
        # never burns an LLM call on the generic retry.
        if len(v) > _SUMMARY_MAX_CHARS:
            logger.warning(
                "summary com %d chars, acima do teto de %d -- truncado", len(v), _SUMMARY_MAX_CHARS
            )
            return v[:_SUMMARY_MAX_CHARS].rstrip()
        return v


class DedupeResult(BaseModel):
    """Output from dedupe_decision prompt."""
    decision: DedupeDecision
    target_note_id: Optional[str] = None
    reason: str = ""


class RelationshipResult(BaseModel):
    """Typed relation between permanent notes (Prompt 2 / connect)."""
    related_note_id: str
    relation_type: RelationType
    description: str = ""


# ── LLM MOC Output ────────────────────────────────────────────────────


class MOCSubsection(BaseModel):
    title: str
    note_ids: list[str] = Field(default_factory=list)
    description: str = ""


class MOCGenerationOutput(BaseModel):
    topic: str = Field(description="Nome/tema do MOC")
    summary: str = Field(description="Resumo do tema em PT-BR")
    subsections: list[MOCSubsection] = Field(default_factory=list)
    topic_justification: str = Field(
        default="",
        description="Justificativa de porque este topico foi escolhido",
    )


# ── MOC Incremental Update ────────────────────────────────────────────


class MOCNotePlacement(BaseModel):
    note_id: str
    subsection: str = Field(description="Titulo da subsecao existente ou 'ignorar'")
    reason: str = ""


class MOCIncrementalOutput(BaseModel):
    placements: list[MOCNotePlacement] = Field(default_factory=list)
    new_subsections: list[MOCSubsection] = Field(default_factory=list)


# ── Hub MOC Output ────────────────────────────────────────────────────


class MOCHubGenerationOutput(BaseModel):
    topic: str = Field(description="Tema derivado do hub e vizinhanca")
    summary: str = Field(description="Resumo do tema em PT-BR")
    hub_role: str = Field(
        default="",
        description="Por que a nota-hub e a porta de entrada",
    )
    subsections: list[MOCSubsection] = Field(default_factory=list)


# ── Article generation ────────────────────────────────────────────────


class ArticleOutlineSection(BaseModel):
    heading: str
    goal: str
    note_ids: list[str] = Field(default_factory=list)
    figure_asset_ids: list[str] = Field(default_factory=list)


class ArticleOutline(BaseModel):
    title: str
    thesis: str = Field(description="Tese / ideia central em 1-2 frases")
    sections: list[ArticleOutlineSection] = Field(default_factory=list)
    style_notes: str = Field(
        default="",
        description="Dicas de tom para a redacao das secoes",
    )


# ── Permanent Note LLM Output ─────────────────────────────────────────


class PermanentNoteLLMOutput(BaseModel):
    """Structured output from Prompt 2 — the permanent note body.

    A rejected concept carries only ``status``/``reason``/``category``: the prompt
    does not ask for a note body that would be thrown away. The body fields are
    therefore optional here and validated by the connector when ``status`` is
    ``accepted``.
    """
    status: str = Field(description="Status da nota")
    reason: str = Field(default="", description="Motivo da rejeição")
    category: str = Field(default="", description="Categoria da rejeição (vazio se aceita)")
    title: str = Field(default="", description="Título declarativo da nota")
    thesis: str = Field(default="", description="Tese principal (blockquote)")
    definition: str = Field(default="", description="Definição/explicação autônoma")
    intuition: str = Field(default="", description="Intuição ou analogia")
    example: str = Field(default="", description="Exemplo prático")
    limits: str = Field(default="", description="Limites e ressalvas")
    connections: list[RelationshipResult] = Field(
        default_factory=list,
        description="Conexões tipadas com notas existentes",
    )
    tags: list[str] = Field(default_factory=list)
