"""Schema Pydantic e loader da configuracao.

Fonte operacional: config/config.yaml. Este modulo define tipos/validators e
os Field defaults usados como fallback (YAML ausente, chave omitida, testes).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path("config/config.yaml")


LLM_PHASES: tuple[str, ...] = (
    "harvest",
    "extract",
    "review",
    "connect",
    "garden",
    "ask",
    "article",
    "images",
)


class LLMPhaseConfig(BaseModel):
    """Identidade de um consumidor de LLM. Knobs de amostragem ficam em LLMConfig."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "openai"
    model: str = "gpt-4o-mini"
    base_url: str | None = None       # gateways OpenAI-compatible / Ollama; None = default do provider


class LLMConfig(BaseModel):
    """Fallback de fabrica. Valores operacionais: config/config.yaml -> llm.

    Amostragem e retries sao globais. Cada fase declara provider + model + base_url.
    """

    model_config = ConfigDict(extra="forbid")

    temperature: float = 0
    top_p: float = 1                  # nucleus sampling; encaminhado em get_llm
    max_retries: int = 2
    prompt_cache: bool = True         # prefix cache do provedor; ≠ llm_cache SQLite
    harvest: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)
    extract: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)
    review: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)
    connect: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)
    garden: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)
    ask: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)
    article: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)
    images: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)


class EmbeddingConfig(BaseModel):
    """Fallback de fabrica. Valores operacionais: config/config.yaml -> embedding."""

    provider: Literal["openai", "sentence-transformers", "ollama"] = "openai"
    model: str = "text-embedding-3-small"
    # ollama: host nativo (http://localhost:11434); sufixo /v1 legado e removido
    base_url: str | None = None
    allow_fallback: bool = False      # False = erro se faltar key (evita Chroma 384-d)
    # MRL: ollama (langchain_ollama) e openai text-embedding-3-* (EF Chroma).
    # null = dimensao nativa do modelo. Trocar exige reindex --force.
    dimensions: int | None = None

    @field_validator("dimensions")
    @classmethod
    def _dimensions_positive(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if int(v) < 1:
            raise ValueError("embedding.dimensions deve ser >= 1 (ou null)")
        return int(v)


class ChunkingConfig(BaseModel):
    chunk_size: int = 1000            # caracteres (nao tokens)
    chunk_overlap: int = 200
    min_section_chars: int = 200      # secoes menores sao fundidas com a seguinte
    min_chunk_chars: int = 200        # pedacos menores sao fundidos no anterior


class LinkingConfig(BaseModel):
    topk: int = 5
    dedupe_threshold: float = 0.90


class HarvestConfig(BaseModel):
    """Dedupe em 3 camadas (hash arquivo/texto + similaridade) e metadados ABNT."""

    duplicate_chunk_threshold: float = 0.88
    duplicate_sample_size: int = 5
    non_interactive_duplicate_action: Literal["skip", "continue", "abort"] = "skip"
    biblio_confidence_threshold: float = 0.7
    biblio_llm_enabled: bool = True
    biblio_text_sample_chars: int = 5000


class ExtractionConfig(BaseModel):
    min_relevance_score: int = 3      # candidatos abaixo sao descartados
    min_thesis_words: int = 5         # palavras minimas na tese
    require_anchor_quote: bool = True # descartar se anchor_quote vazio
    min_definition_words: int = 10    # palavras minimas na definicao


class LiteratureReviewConfig(BaseModel):
    """Aprovacao seletiva de Notas de Literatura granulares (por chunk)."""

    auto_approve_min_confidence: float = 0.85
    batch_sample_size: int = 20       # max drafts de baixa confianca a listar no review interativo
    drafts_subdir: str = "00_Inbox/Review"


class ImagesConfig(BaseModel):
    """Extracao de imagens no harvest (Docling/Markdown) e descricao multimodal."""

    enabled: bool = False             # extrai/descreve imagens de PDF (Docling) e Markdown
    scale: float = 2.0               # images_scale do Docling
    min_width: int = 64              # descarta imagens menores (icones/logos)
    min_height: int = 64
    context_chars: int = 600         # caracteres ao redor da imagem usados como contexto
    # Pacing + resiliencia a TPM (visao estoura tokens/min bem mais rapido que texto):
    min_interval_seconds: float = 0.4       # pausa minima entre chamadas LLM de imagem
    rate_limit_max_retries: int = 8         # tentativas por imagem em 429
    rate_limit_backoff_max: float = 60.0    # teto de espera (s) entre retries
    rate_limit_abort_after: int = 5         # 429 esgotados consecutivos => para o lote


class GardenerConfig(BaseModel):
    min_cluster_size: int = 5
    min_notes_for_moc: int = 3
    domain: str = ""                               # ex: "Ciencia de Dados"
    # Default ja aponta para o YAML da taxonomia. None = taxonomia nao configurada
    # (TaxonomyLoadError se strict_topics). Nao confundir None com "usar o default".
    topics_path: Path | None = Path("config/moc_topics.yaml")
    # Override de testes; nao e knob do config.yaml (whitelist vem de topics_path).
    allowed_topics: list[str] = Field(default_factory=list)
    strict_topics: bool = True                      # rejeitar topic fora das categorias
    # Pipeline hibrido: taxonomia -> cluster por categoria -> grafo -> LLM.
    cluster_within_category: bool = True
    category_label_template: str = "{domain}: {categoria}"
    overlap_threshold: float = 0.4                  # overlap cluster/MOC -> incremental
    graph_cohesion_enabled: bool = True
    graph_cohesion_min_ratio: float = 0.0           # 0 = metrica apenas; >0 rejeita MOC novo
    umap_n_neighbors: int | None = None             # None = auto (min(15, n-1))
    hdbscan_min_samples: int | None = None          # None = default HDBSCAN

    @field_validator("topics_path", mode="before")
    @classmethod
    def resolve_topics_path(cls, v: Any) -> Path | None:
        if v is None or v == "":
            return None
        return Path(v).resolve()


class HubMocsConfig(BaseModel):
    """Fallback de `zettel garden --hubs`. Catalogo operacional: config.yaml -> hub_mocs."""

    selection_mode: Literal["percentile", "absolute"] = "percentile"
    hub_percentile: float = 0.90
    top_n_hubs: int = 10
    min_weighted_degree: float = 8.0
    max_hops: int = 2
    max_neighbors: int = 15
    min_neighbors: int = 8
    decay: float = 0.5
    min_neighbor_weight: float = 0.3
    dedup_subset_threshold: float = 0.8


# Fallback dos pesos de aresta (grafo + hubs). Override operacional:
# retrieval.graph_expansion.relation_weights em config.yaml.
# contradicts no topo: embedding nao distingue "apoia" de "contradiz".
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


class ArticleConfig(BaseModel):
    """Comando `zettel article` — artigo estruturado a partir do vault."""

    topk: int = 20
    max_context_notes: int = 24
    max_chars_per_note: int = 1200
    max_hops: int = 2                 # expansao de grafo mais ampla que o ask
    max_sections: int = 8
    max_figures: int = 6
    chars_per_section_draft: int = 2500
    personalities_path: Path = Path("./config/personalities.yaml")
    default_personality: str = "neutral"
    enrich_query_count: int = 6
    max_judge_iterations: int = 3
    judge_min_score: float = 7.0
    writer_temperature: float | None = None   # None = cfg.llm.temperature
    judge_temperature: float = 0.2
    enrich_temperature: float = 0.2


class RelevanceFloorConfig(BaseModel):
    """Piso absoluto alem do RRF (que so ranqueia). Catalogo: config.yaml -> retrieval."""

    enabled: bool = True
    min_vector_similarity: float = 0.70
    bm25_hit_bypasses_floor: bool = True
    bm25_bypass_max_rank: int = 5
    absolute_min_similarity: float = 0.15


class RetrievalConfig(BaseModel):
    """Recuperacao hibrida (vetor + BM25) com fusao RRF e expansao por grafo.

    `mode: vector` preserva o comportamento historico (Chroma puro). `hybrid`
    funde a busca densa do Chroma com o BM25 do FTS5 no state.db.
    """

    mode: Literal["vector", "hybrid"] = "hybrid"
    rrf_k: int = 60                   # constante do Reciprocal Rank Fusion (canonica)
    graph_expansion: GraphExpansionConfig = Field(default_factory=GraphExpansionConfig)
    relevance_floor: RelevanceFloorConfig = Field(default_factory=RelevanceFloorConfig)
    ask: AskConfig = Field(default_factory=AskConfig)
    article: ArticleConfig = Field(default_factory=ArticleConfig)


class AppConfig(BaseModel):
    """Schema do pipeline. Fonte operacional: config/config.yaml (load_config).

    Field defaults sao fallback de fabrica (YAML ausente, chave omitida, testes).
    """

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
    literature_review: LiteratureReviewConfig = Field(default_factory=LiteratureReviewConfig)
    images: ImagesConfig = Field(default_factory=ImagesConfig)
    gardener: GardenerConfig = Field(default_factory=GardenerConfig)
    hub_mocs: HubMocsConfig = Field(default_factory=HubMocsConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)

    language: str = "pt-BR"
    log_level: str = "INFO"
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


def llm_phase(cfg: Any, phase: str) -> LLMPhaseConfig:
    """Return the LLM identity for a pipeline/QA/vision consumer.

    ``phase`` must be one of ``LLM_PHASES``. Unknown names raise ``ValueError``
    rather than falling back to another phase.
    """
    if phase not in LLM_PHASES:
        raise ValueError(
            f"Fase LLM desconhecida: {phase!r}. "
            f"Valores validos: {', '.join(LLM_PHASES)}"
        )
    spec = getattr(cfg.llm, phase)
    if not isinstance(spec, LLMPhaseConfig):
        raise TypeError(
            f"llm.{phase} deve ser LLMPhaseConfig, obtido {type(spec).__name__}"
        )
    return spec


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger with RichHandler.

    Uses stderr so log messages flow correctly alongside Rich console.status() spinners.
    Quiets noisy HTTP client loggers so pipeline progress (X/Y) stays readable.
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
    # httpx/OpenAI emit one INFO line per request ("HTTP Request: POST ... 200 OK"),
    # which drowns the harvest progress when embedding hundreds of chunks.
    for noisy in ("httpx", "httpcore", "openai", "openai._base_client", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


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
