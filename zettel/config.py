"""Configuration loader and validator."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path("config/config.yaml")


class LLMConfig(BaseModel):
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    temperature: float = 0
    top_p: float = 1
    max_retries: int = 2


class EmbeddingConfig(BaseModel):
    provider: str = "openai"
    model: str = "text-embedding-3-small"
    # Se False (padrao), a ausencia de API key / provider invalido levanta erro em vez
    # de cair silenciosamente no embedding default do ChromaDB (384 dims), o que
    # misturaria espacos vetoriais incompativeis.
    allow_fallback: bool = False


class ChunkingConfig(BaseModel):
    chunk_size: int = 1000            # caracteres (nao tokens)
    chunk_overlap: int = 200
    min_section_chars: int = 200      # secoes menores sao fundidas com a seguinte


class LinkingConfig(BaseModel):
    topk: int = 5
    dedupe_threshold: float = 0.90


class HarvestConfig(BaseModel):
    """Controls the three-layer duplicate detection in the harvest phase."""

    duplicate_chunk_threshold: float = 0.88   # similaridade minima p/ suspeita semantica
    duplicate_sample_size: int = 5            # nro de chunks amostrados p/ checagem semantica
    # Comportamento padrao quando rodando sem interacao (--yes/--skip-duplicates/scripts):
    # "skip" (pular o arquivo suspeito, seguro por padrao), "continue" (tratar como nova
    # fonte) ou "abort" (interromper o harvest inteiro).
    non_interactive_duplicate_action: Literal["skip", "continue", "abort"] = "skip"


class ExtractionConfig(BaseModel):
    min_relevance_score: int = 3      # candidatos abaixo sao descartados
    min_thesis_words: int = 5         # palavras minimas na tese
    require_anchor_quote: bool = True # descartar se anchor_quote vazio
    min_definition_words: int = 10    # palavras minimas na definicao


class ImagesConfig(BaseModel):
    """Image extraction + multimodal description (Fase 3)."""

    enabled: bool = False             # extrai/descreve imagens de PDF (Docling) e Markdown
    scale: float = 2.0               # images_scale do Docling
    min_width: int = 64              # descarta imagens menores (icones/logos)
    min_height: int = 64
    context_chars: int = 600         # caracteres ao redor da imagem usados como contexto
    model: str = ""                  # vazio = usa llm.model (deve ser multimodal)


class GardenerConfig(BaseModel):
    min_cluster_size: int = 5
    min_notes_for_moc: int = 3
    domain: str = ""                               # ex: "Ciencia de Dados"
    allowed_topics: list[str] = Field(default_factory=list)
    strict_topics: bool = True                      # rejeitar MOCs fora da lista


# Peso de cada tipo de aresta na expansao por grafo. `contradicts` no topo porque
# e a informacao que a similaridade de embedding NAO captura (vetores proximos nao
# distinguem "apoia" de "contradiz"); `related` no fundo (relacao tematica fraca).
# Fonte unica de verdade — reutilizado por zettel/graph.py.
DEFAULT_RELATION_WEIGHTS: dict[str, float] = {
    "contradicts": 1.0,
    "extends": 0.9,
    "depends_on": 0.9,
    "supports": 0.8,
    "exemplifies": 0.7,
    "related": 0.5,
}


class GraphExpansionConfig(BaseModel):
    """Expansao 1-N saltos sobre note_connections apos a fusao hibrida."""

    enabled: bool = True
    max_hops: int = 1                 # 1 salto ja traz o valor do GraphRAG leve
    decay: float = 0.5                # atenuacao do score por salto adicional
    max_neighbors: int = 10           # teto de vizinhos trazidos para o contexto
    relation_weights: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_RELATION_WEIGHTS)
    )


class AskConfig(BaseModel):
    """Comando `zettel ask` — QA sobre o vault."""

    topk: int = 8
    max_context_notes: int = 8        # teto de notas montadas no contexto do LLM
    max_chars_per_note: int = 1500    # truncagem do corpo de cada nota no contexto


class RelevanceFloorConfig(BaseModel):
    """Piso de relevancia ABSOLUTO, aplicado alem da fusao RRF (que e apenas posicional).

    RRF so enxerga o RANKING de cada candidato, nao o quao bom ele e de fato — a
    busca vetorial (kNN) sempre devolve os N vizinhos mais proximos disponiveis no
    corpus, mesmo que nenhum seja de fato relevante. Sem um piso absoluto, uma
    pergunta totalmente fora do acervo (ex.: "o que e a chuva?") produz um score
    RRF no mesmo patamar de uma pergunta genuinamente respondivel.

    Calibrado empiricamente neste projeto (embedding text-embedding-3-small) com
    um par de perguntas reais: uma pergunta respondivel pelo acervo teve
    similaridade coseno 0.70-0.84 nos top-8; uma pergunta fora do tema teve
    0.63-0.65. `min_vector_similarity=0.70` separa os dois casos. Este limiar e
    dependente do modelo de embedding e do corpus — reajuste se notar falsos
    negativos/positivos.
    """

    enabled: bool = True
    min_vector_similarity: float = 0.70
    # Uma correspondencia lexical (BM25) exige overlap real de termos na nota —
    # ao contrario do kNN vetorial, que sempre devolve "o mais proximo disponivel"
    # mesmo sem relacao nenhuma. Por isso um hit achado via BM25 passa no piso
    # independente da similaridade vetorial (que pode nem existir para ele).
    bm25_hit_bypasses_floor: bool = True


class RetrievalConfig(BaseModel):
    """Recuperacao hibrida (vetor + BM25) com fusao RRF e expansao por grafo.

    `mode: vector` preserva o comportamento historico (Chroma puro). `hybrid`
    funde a busca densa do Chroma com o BM25 do FTS5 no state.db.
    """

    mode: Literal["vector", "hybrid"] = "hybrid"
    rrf_k: int = 60                   # constante do Reciprocal Rank Fusion (canonica)
    fts_min_token_len: int = 2        # tokens menores sao descartados no MATCH
    graph_expansion: GraphExpansionConfig = Field(default_factory=GraphExpansionConfig)
    relevance_floor: RelevanceFloorConfig = Field(default_factory=RelevanceFloorConfig)
    ask: AskConfig = Field(default_factory=AskConfig)


class AppConfig(BaseModel):
    vault_path: Path = Path("./vault")
    inbox_path: Path = Path("./data/inbox")
    chroma_path: Path = Path("./data/chroma")
    state_db_path: Path = Path("./data/state.db")
    cache_path: Path = Path("./data/cache")
    prompts_path: Path = Path("./prompts")

    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    linking: LinkingConfig = Field(default_factory=LinkingConfig)
    harvest: HarvestConfig = Field(default_factory=HarvestConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    images: ImagesConfig = Field(default_factory=ImagesConfig)
    gardener: GardenerConfig = Field(default_factory=GardenerConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)

    language: str = "pt-BR"
    log_level: str = "INFO"
    pdf_extractor: str = "docling"
    device: str = "auto"  # auto | cpu | cuda

    @field_validator(
        "vault_path",
        "inbox_path",
        "chroma_path",
        "state_db_path",
        "cache_path",
        "prompts_path",
        mode="before",
    )
    @classmethod
    def resolve_path(cls, v: Any) -> Path:
        return Path(v).resolve()


def load_config(path: Path | str | None = None) -> AppConfig:
    """Load configuration from a YAML file, with defaults.

    Also loads environment variables from a .env file (project root).
    """
    # Load .env before anything that reads env vars (LLM keys, etc.)
    env_path = Path(".env")
    if env_path.exists():
        load_dotenv(env_path, override=False)
        logger.info("Variaveis de ambiente carregadas de .env")
    else:
        logger.debug(".env nao encontrado, usando apenas variaveis de ambiente do sistema")

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

    return AppConfig(**data)


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger with RichHandler.

    Uses stderr so log messages flow correctly alongside Rich console.status() spinners.
    """
    from rich.logging import RichHandler
    from rich.console import Console

    handler = RichHandler(
        console=Console(stderr=True),
        show_time=True,
        show_path=False,
        markup=False,
    )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[handler],
    )


def detect_device(preference: str = "auto") -> str:
    """Detect the best available compute device.

    Args:
        preference: "auto" (detect), "cpu" (force CPU), "cuda" (force GPU).

    Returns:
        "cuda" if a GPU is available and selected, otherwise "cpu".
    """
    if preference == "cpu":
        logger.info("Dispositivo: CPU (forcado via config)")
        return "cpu"

    if preference == "cuda":
        if _cuda_available():
            logger.info("Dispositivo: CUDA (forcado via config) — %s", _gpu_name())
            return "cuda"
        logger.warning("CUDA solicitado mas nao disponivel. Usando CPU.")
        return "cpu"

    # auto
    if _cuda_available():
        logger.info("GPU detectada: %s — usando CUDA", _gpu_name())
        return "cuda"

    logger.info("Nenhuma GPU detectada. Usando CPU.")
    return "cpu"


def _cuda_available() -> bool:
    """Check if CUDA-capable GPU is available via PyTorch."""
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


def _gpu_name() -> str:
    """Return the name of the current CUDA device."""
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except Exception:
        pass
    return "desconhecida"


def get_gpu_info() -> dict[str, Any]:
    """Return detailed GPU information for diagnostics."""
    info: dict[str, Any] = {"available": False}
    try:
        import torch
        info["torch_version"] = torch.__version__
        info["cuda_built"] = torch.cuda.is_available()
        if torch.cuda.is_available():
            info["available"] = True
            info["device_name"] = torch.cuda.get_device_name(0)
            info["device_count"] = torch.cuda.device_count()
            mem = torch.cuda.get_device_properties(0).total_memory
            info["vram_gb"] = round(mem / (1024 ** 3), 1)
            info["cuda_version"] = torch.version.cuda or "N/A"
    except ImportError:
        info["torch_version"] = "nao instalado"
    return info
