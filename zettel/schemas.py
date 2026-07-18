"""Pydantic schemas for all Zettelkasten data objects."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────


class OriginType(str, Enum):
    PDF = "pdf"
    MARKDOWN = "md"
    AUDIO = "audio"


class ChunkStatus(str, Enum):
    PENDING = "pending"
    EXTRACTED = "extracted"
    FAILED = "failed"
    SKIPPED = "skipped"


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


# ── Source / File ──────────────────────────────────────────────────────


class FileRecord(BaseModel):
    path: str
    file_checksum: str
    origin_type: OriginType
    source_id: Optional[str] = None
    last_seen_at: datetime = Field(default_factory=datetime.now)


class SourceRecord(BaseModel):
    source_id: str
    citekey: str
    title: str
    authors: list[str] = Field(default_factory=list)
    year: Optional[int] = None
    file_checksum: str
    extraction_checksum: Optional[str] = None
    origin_path: str
    origin_type: OriginType
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


# ── Chapter / Chunk ────────────────────────────────────────────────────


class ChapterRecord(BaseModel):
    chapter_id: str
    source_id: str
    title: str = ""
    chapter_checksum: str
    locator: str = ""


class ChunkRecord(BaseModel):
    chunk_id: str
    source_id: str
    chapter_id: str
    text: str
    chunk_checksum: str
    locator: str = ""
    status: ChunkStatus = ChunkStatus.PENDING
    llm_prompt1_hash: Optional[str] = None
    llm_call_checksum: Optional[str] = None


# ── LLM Extraction Outputs ────────────────────────────────────────────


class LiteratureChunkOutput(BaseModel):
    """Output from Prompt 1 for a single chunk — appended to LIT note."""
    chunk_status: str = Field(description="Status do chunk")
    rejection_reason: str = Field(description="Motivo da rejeição")
    rejection_category: str = Field(description="Categoria da rejeição")
    summary: str = Field(description="Resumo do chunk em PT-BR")
    key_concepts: list[str] = Field(
        default_factory=list, description="Conceitos-chave extraídos"
    )
    candidates: list[PermanentNoteCandidate] = Field(
        default_factory=list,
        description="Candidatos atômicos a notas permanentes",
    )


class PermanentNoteCandidate(BaseModel):
    """A single atomic concept extracted from a chunk."""
    chunk_status: str = Field(description="Status do chunk")
    rejection_reason: str = Field(description="Motivo da rejeição")
    rejection_category: str = Field(description="Categoria da rejeição")
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


# Forward ref update
LiteratureChunkOutput.model_rebuild()


class DedupeResult(BaseModel):
    """Output from dedupe_decision prompt."""
    decision: DedupeDecision
    target_note_id: Optional[str] = None
    reason: str = ""


class RelationshipResult(BaseModel):
    """Output from relationship prompt."""
    related_note_id: str
    relation_type: RelationType
    description: str = ""


# ── Permanent Note ─────────────────────────────────────────────────────


class PermanentNoteRecord(BaseModel):
    note_id: str
    source_id: str
    literature_ref: str = ""
    title: str = ""
    thesis: str = ""
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    source_locator: str = ""
    connections: list[RelationshipResult] = Field(default_factory=list)
    note_semantic_checksum: Optional[str] = None
    auto_checksum: Optional[str] = None
    embedding_model: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


# ── Concept mapping ───────────────────────────────────────────────────


class ConceptRecord(BaseModel):
    concept_id: str
    source_id: str
    chunk_id: str
    anchor_hash: str = ""
    thesis_hash: str = ""
    note_id: Optional[str] = None


# ── MOC ────────────────────────────────────────────────────────────────


class MOCRecord(BaseModel):
    moc_id: str
    topic: str
    note_ids: list[str] = Field(default_factory=list)
    cluster_signature: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


# ── LLM MOC Output ────────────────────────────────────────────────────


class MOCGenerationOutput(BaseModel):
    topic: str = Field(description="Nome/tema do MOC")
    summary: str = Field(description="Resumo do tema em PT-BR")
    subsections: list[MOCSubsection] = Field(default_factory=list)
    topic_justification: str = Field(
        default="",
        description="Justificativa de porque este topico foi escolhido",
    )


class MOCSubsection(BaseModel):
    title: str
    note_ids: list[str] = Field(default_factory=list)
    description: str = ""


MOCGenerationOutput.model_rebuild()


# ── MOC Incremental Update ────────────────────────────────────────────


class MOCNotePlacement(BaseModel):
    note_id: str
    subsection: str = Field(description="Titulo da subsecao existente ou 'ignorar'")
    reason: str = ""


class MOCIncrementalOutput(BaseModel):
    placements: list[MOCNotePlacement] = Field(default_factory=list)
    new_subsections: list[MOCSubsection] = Field(default_factory=list)


MOCIncrementalOutput.model_rebuild()


# ── Permanent Note LLM Output ─────────────────────────────────────────


class PermanentNoteLLMOutput(BaseModel):
    """Structured output from Prompt 2 — the permanent note body."""
    status: str = Field(description="Status da nota")
    reason: str = Field(description="Motivo da rejeição")
    category: str = Field(description="Categoria da nota")
    title: str = Field(description="Título declarativo da nota")
    thesis: str = Field(description="Tese principal (blockquote)")
    definition: str = Field(description="Definição/explicação autônoma")
    intuition: str = Field(default="", description="Intuição ou analogia")
    example: str = Field(default="", description="Exemplo prático")
    limits: str = Field(default="", description="Limites e ressalvas")
    connections: list[RelationshipResult] = Field(
        default_factory=list,
        description="Conexões tipadas com notas existentes",
    )
    tags: list[str] = Field(default_factory=list)


PermanentNoteLLMOutput.model_rebuild()
