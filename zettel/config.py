"""Schema Pydantic e loader da configuracao.

Fonte operacional: config/config.yaml. Este modulo define tipos/validators e
os Field defaults usados como fallback (YAML ausente, chave omitida, testes).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_DIR.parent
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "config.yaml"

_TOP_LEVEL_PATH_KEYS = (
    "vault_path",
    "inbox_path",
    "chroma_path",
    "state_db_path",
    "cache_path",
    "prompts_path",
)


def _project_root_for_config(config_path: Path) -> Path:
    """Directory that ``./vault``, ``./data/…`` in config.yaml are relative to."""
    resolved = config_path.resolve()
    if resolved.parent.name == "config":
        return resolved.parent.parent
    return resolved.parent


def _anchor_path_value(value: Any, base: Path) -> Any:
    if value is None:
        return value
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((base / path).resolve())


def _anchor_relative_paths(data: dict[str, Any], base: Path) -> dict[str, Any]:
    """Resolve config path strings against the repo root, not the process cwd."""
    out = dict(data)
    for key in _TOP_LEVEL_PATH_KEYS:
        if key in out:
            out[key] = _anchor_path_value(out[key], base)

    gardener = out.get("gardener")
    if isinstance(gardener, dict) and "topics_path" in gardener:
        gardener = dict(gardener)
        gardener["topics_path"] = _anchor_path_value(gardener["topics_path"], base)
        out["gardener"] = gardener

    domain = out.get("domain")
    if isinstance(domain, dict) and "examples_path" in domain:
        domain = dict(domain)
        domain["examples_path"] = _anchor_path_value(domain["examples_path"], base)
        out["domain"] = domain

    retrieval = out.get("retrieval")
    if isinstance(retrieval, dict):
        article = retrieval.get("article")
        if isinstance(article, dict) and "personalities_path" in article:
            retrieval = dict(retrieval)
            article = dict(article)
            article["personalities_path"] = _anchor_path_value(article["personalities_path"], base)
            retrieval["article"] = article
            out["retrieval"] = retrieval

    return out


def _default_app_config(project_root: Path) -> AppConfig:
    """Factory defaults anchored to ``project_root`` (YAML missing)."""
    return AppConfig(
        vault_path=project_root / "vault",
        inbox_path=project_root / "data" / "inbox",
        chroma_path=project_root / "data" / "chroma",
        state_db_path=project_root / "data" / "state.db",
        cache_path=project_root / "data" / "cache",
        prompts_path=project_root / "prompts",
    )


LLM_PHASES: tuple[str, ...] = (
    "harvest",
    "extract",
    "review",
    "connect",
    "garden",
    "ask",
    "article",
    "images",
    "summarize",
)


THINKING_LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high")

ThinkingValue = bool | Literal["minimal", "low", "medium", "high"] | int | None


class LLMPhaseConfig(BaseModel):
    """Identidade de um consumidor de LLM. Knobs de amostragem ficam em LLMConfig."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "openai"
    model: str = "gpt-4o-mini"
    base_url: str | None = None  # gateways OpenAI-compatible / Ollama; None = default do provider
    temperature: float | None = None  # None = herda llm.temperature; override so desta fase
    thinking: ThinkingValue = None  # None = default do vendor; false/0 = off; nivel ou budget

    @field_validator("thinking", mode="before")
    @classmethod
    def _thinking_value(cls, v: object) -> ThinkingValue:
        if v is None or isinstance(v, bool):
            return v
        if isinstance(v, int):
            if int(v) < 0:
                raise ValueError("llm.<fase>.thinking inteiro deve ser >= 0 (ou null)")
            return int(v)
        if isinstance(v, str) and v in THINKING_LEVELS:
            return v
        raise ValueError(
            "llm.<fase>.thinking deve ser null, true, false, "
            "minimal|low|medium|high, ou um inteiro >= 0"
        )


class LLMConfig(BaseModel):
    """Fallback de fabrica. Valores operacionais: config/config.yaml -> llm.

    Amostragem e retries sao globais. Cada fase declara provider + model + base_url
    e thinking (null = default do vendor).
    """

    model_config = ConfigDict(extra="forbid")

    temperature: float = 0
    top_p: float = 1  # nucleus sampling; encaminhado em get_llm
    max_retries: int = 2  # extras da mesma chamada; esgotadas => fail-fast da fase
    prompt_cache: bool = True  # prefix cache do provedor; ≠ llm_cache SQLite
    harvest: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)
    extract: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)
    review: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)
    connect: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)
    garden: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)
    ask: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)
    article: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)
    images: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)
    summarize: LLMPhaseConfig = Field(default_factory=LLMPhaseConfig)


class EmbeddingConfig(BaseModel):
    """Fallback de fabrica. Valores operacionais: config/config.yaml -> embedding."""

    # Registro em zettel/index.py (_EF_BUILDERS); tests/test_config.py pina os dois.
    provider: Literal["openai", "sentence-transformers", "ollama", "gemini"] = "openai"
    model: str = "text-embedding-3-small"
    # ollama: host nativo (http://localhost:11434); sufixo /v1 legado e removido.
    # gemini: ignorado (API do Google via GOOGLE_API_KEY / GEMINI_API_KEY).
    base_url: str | None = None
    allow_fallback: bool = False  # False = erro se faltar key (evita Chroma 384-d)
    # MRL: ollama (langchain_ollama), openai text-embedding-3-* (EF Chroma) e
    # gemini-embedding-001 (768/1536/3072; vetores re-normalizados no adaptador).
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
    chunk_size: int = 2500  # caracteres (nao tokens)
    chunk_overlap: int = 400
    min_section_chars: int = 200  # secoes menores sao fundidas com a seguinte
    min_chunk_chars: int = 200  # pedacos menores sao fundidos no anterior
    # Secao com fence cujo total cabe em chunk_size * slack fica inteira num
    # chunk, em vez de ser cortada nas bordas do fence (prosa e codigo juntos).
    fence_section_slack: float = 1.5


class LinkingConfig(BaseModel):
    topk: int = 5
    dedupe_threshold: float = 0.85
    # Alvo de saida por nota, usado APENAS na estimativa de pre-voo (nao e teto).
    preflight_output_tokens_per_note: int = 1200
    # Busca secundaria do connect: analogias fora do bucket taxonomico.
    # Piso LOCAL — nao altera retrieval.relevance_floor (compartilhado com ask).
    distant_analogy_topk: int = 5
    distant_analogy_min_similarity: float = 0.40
    # Corroboracao entre fontes: duas ZTL de source_id diferentes que dizem a
    # mesma coisa viram duas notas ligadas por `corroborates`, nao uma so.
    # Derivado por codigo a partir dos hits que o Retriever ja trouxe no connect
    # (zero embedding e zero chamada de LLM adicionais).
    corroborates_min_similarity: float = 0.85
    corroborates_max_edges: int = 3


class HarvestConfig(BaseModel):
    """Dedupe em 5 camadas (hash, DOI/ISBN, titulo+autor, similaridade) e ABNT."""

    # Camada 5 — similaridade semantica de chunks (ADR-011/ADR-046).
    #
    # DESLIGADA por default. Ela responde uma pergunta binaria por arquivo
    # ingerido ("este arquivo e a mesma obra que uma fonte que ja tenho?"), mas
    # o preco e embedar TODO chunk de TODA fonte para manter o indice-alvo: o
    # gasto cresce com o acervo, o uso cresce com os arquivos novos. As camadas
    # 3 e 4 (DOI/ISBN exato, titulo+autor) cobrem material catalogado de forma
    # deterministica e sem limiar para calibrar.
    #
    # Ligar custa um repovoamento: `zettel reindex --collection chunks`. Nao
    # desligue com a colecao ja parcialmente povoada esperando que a camada 5
    # continue correta — este flag governa **escrita e leitura ao mesmo tempo**
    # justamente porque consultar um indice incompleto produz falso negativo
    # silencioso, e nao um erro.
    semantic_duplicate_enabled: bool = False
    duplicate_chunk_threshold: float = 0.88
    duplicate_sample_size: int = 5
    non_interactive_duplicate_action: Literal["skip", "continue", "abort"] = "skip"
    biblio_confidence_threshold: float = 0.7
    biblio_llm_enabled: bool = True
    biblio_text_sample_chars: int = 5000


class ExtractionConfig(BaseModel):
    min_relevance_score: int = 3  # candidatos abaixo sao descartados
    min_thesis_words: int = 5  # palavras minimas na tese
    require_anchor_quote: bool = True  # descartar se anchor_quote vazio
    min_definition_words: int = 10  # palavras minimas na definicao
    verify_anchor_quote: bool = True  # checa faixa de palavras e ancoragem no chunk
    anchor_quote_min_ratio: float = 0.85  # cobertura minima na checagem fuzzy
    anchor_quote_min_words: int = 10  # faixa que o prompt ja exige
    anchor_quote_max_words: int = 25
    # Margem sobre o teto antes de descartar o candidato inteiro. O modelo erra a
    # contagem por pouco e sempre para cima; `quote_is_grounded` e quem testa a
    # propriedade real. 1.0 = corte rigido (comportamento anterior a #153).
    anchor_quote_max_words_tolerance: float = 1.5
    # Alvo de saida por chunk, usado APENAS na estimativa de pre-voo (nao e teto).
    preflight_output_tokens_per_chunk: int = 800


class LiteratureReviewConfig(BaseModel):
    """Aprovacao seletiva de Notas de Literatura granulares (por chunk)."""

    # Placed just under 0.80 — the score a flawless chunk earns at the relevance
    # floor — so every defect-free chunk auto-approves and every chunk with a
    # detected defect does not. Kept in sync with config/config.yaml (issue #152).
    auto_approve_min_confidence: float = 0.75
    batch_sample_size: int = 20  # max drafts de baixa confianca a listar no review interativo
    drafts_subdir: str = "00_Inbox/Review"


class ImagesConfig(BaseModel):
    """Extracao de imagens no harvest (Docling/Markdown) e descricao multimodal."""

    enabled: bool = False  # extrai/descreve imagens de PDF (Docling) e Markdown
    scale: float = 2.0  # images_scale do Docling
    min_width: int = 64  # descarta imagens menores (icones/logos)
    min_height: int = 64
    context_chars: int = 600  # caracteres ao redor da imagem usados como contexto
    # Pacing + resiliencia a TPM (visao estoura tokens/min bem mais rapido que texto):
    min_interval_seconds: float = 0.4  # pausa minima entre chamadas LLM de imagem
    rate_limit_max_retries: int = 8  # tentativas por imagem em 429
    rate_limit_backoff_max: float = 60.0  # teto de espera (s) entre retries
    rate_limit_abort_after: int = 5  # 429 esgotados consecutivos => para o lote


# Formulas e codigo sao enriquecimentos do Docling no harvest de PDF. Os dois rodam o
# mesmo modelo local de visao (CodeFormulaV2, baixado do Hugging Face no primeiro uso) e
# deixam a conversao mais lenta. Markdown nativo nao passa pelo Docling: ja traz formula e
# codigo como texto. Ligar ou desligar muda o texto extraido -- logo os chunk_id das fontes
# recolhidas e os rotulos humanos presos a eles.
class FormulasConfig(BaseModel):
    """Decodifica regioes de formula do PDF em LaTeX (Docling ``do_formula_enrichment``)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class CodeConfig(BaseModel):
    """Decodifica blocos de codigo do PDF em texto (Docling ``do_code_enrichment``)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class DomainConfig(BaseModel):
    """Identidade do acervo e few-shots. Lido por extract, connect e garden."""

    model_config = ConfigDict(extra="forbid")

    name: str = "Geral"
    examples_path: Path = Path("config/domain_examples.yaml")

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        name = (v or "").strip()
        if not name:
            raise ValueError("domain.name nao pode ser vazio")
        return name

    @field_validator("examples_path", mode="before")
    @classmethod
    def resolve_examples_path(cls, v: Any) -> Path:
        if v is None or v == "":
            return Path("config/domain_examples.yaml").resolve()
        return Path(v).resolve()


class GardenerConfig(BaseModel):
    min_cluster_size: int = 5
    min_notes_for_moc: int = 3
    # Default ja aponta para o YAML da taxonomia. None = taxonomia nao configurada
    # (TaxonomyLoadError se strict_topics). Nao confundir None com "usar o default".
    topics_path: Path | None = Path("config/moc_topics.yaml")
    # Override de testes; nao e knob do config.yaml (whitelist vem de topics_path).
    allowed_topics: list[str] = Field(default_factory=list)
    strict_topics: bool = True  # rejeitar topic fora das categorias
    # Pipeline hibrido: taxonomia -> cluster por categoria -> grafo -> LLM.
    cluster_within_category: bool = True
    category_label_template: str = "{pilar}: {categoria}"
    overlap_threshold: float = 0.4  # overlap cluster/MOC -> incremental
    graph_cohesion_enabled: bool = True
    graph_cohesion_min_ratio: float = 0.0  # 0 = metrica apenas; >0 rejeita MOC novo
    umap_n_neighbors: int | None = None  # None = auto (min(15, n-1))
    hdbscan_min_samples: int | None = None  # None = default HDBSCAN

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
    # Convergencia de autoria (fontes diferentes, mesma ideia). Peso BAIXO de
    # proposito: o peso governa travessia, nao importancia. Um clique de N notas
    # quase identicas e valioso para quem escreve ("segundo A, corroborado por
    # B"), e toxico para uma fronteira de recuperacao que busca informacao nova —
    # com peso alto ele consome as vagas de max_neighbors com parafrases.
    # A relacao ganha destaque na renderizacao (Conexoes/backlinks), nao aqui.
    "corroborates": 0.45,
    # Aresta que o autor afirmou no corpo da nota (origin=manual). Nao e um
    # relation_type persistido — e o peso aplicado quando a origem e manual.
    "manual": 0.95,
}


class GraphExpansionConfig(BaseModel):
    """Expansao 1-N saltos sobre note_connections apos a fusao hibrida."""

    enabled: bool = True
    max_hops: int = 1  # 1 salto ja traz o valor do GraphRAG leve
    decay: float = 0.5  # atenuacao do score por salto adicional
    max_neighbors: int = 10  # teto de vizinhos trazidos para o contexto
    relation_weights: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_RELATION_WEIGHTS)
    )


class AskConfig(BaseModel):
    """Comando `zettel ask` — QA sobre o vault."""

    topk: int = 8
    max_context_notes: int = 8  # teto de notas montadas no contexto do LLM
    max_chars_per_note: int = 1500  # truncagem do corpo de cada nota no contexto


class ArticleConfig(BaseModel):
    """Comando `zettel article` — artigo estruturado a partir do vault."""

    topk: int = 20
    max_context_notes: int = 24
    max_chars_per_note: int = 1200
    max_hops: int = 2  # expansao de grafo mais ampla que o ask
    max_sections: int = 8
    max_figures: int = 6
    chars_per_section_draft: int = 2500
    personalities_path: Path = Path("./config/personalities.yaml")
    default_personality: str = "neutral"
    enrich_query_count: int = 6
    max_judge_iterations: int = 3
    judge_min_score: float = 7.0
    writer_temperature: float | None = None  # None = cfg.llm.temperature
    judge_temperature: float = 0.2
    enrich_temperature: float = 0.2


class RelevanceFloorConfig(BaseModel):
    """Piso absoluto alem do RRF (que so ranqueia). Catalogo: config.yaml -> retrieval."""

    enabled: bool = True
    min_vector_similarity: float = 0.70
    bm25_hit_bypasses_floor: bool = True
    bm25_bypass_max_rank: int = 5
    absolute_min_similarity: float = 0.15
    # Fracao dos termos da pergunta que a nota precisa conter para o bypass
    # lexical valer. `bm25_bypass_max_rank` sozinho e um criterio RELATIVO:
    # num pool de 4 notas que casam, "top 5" quer dizer "todas". Cobertura e
    # ABSOLUTA -- mede quanto do que foi perguntado esta de fato na nota.
    # 0.5 medido em 2026-09-09 (ADR-003 addendum); 0.0 restaura o
    # comportamento anterior (so rank).
    bm25_bypass_min_coverage: float = 0.5


class CatalogConfig(BaseModel):
    """Busca de catalogo: quais fontes tratam de um assunto (ADR-047)."""

    model_config = ConfigDict(extra="forbid")

    # Sementes de nota (sinal A) e de resumo (sinal B). Os dois sinais sao
    # fundidos por RRF no nivel do capitulo.
    note_topk: int = 20
    summary_topk: int = 20
    max_sources: int = 10
    max_chapters_per_source: int = 8


class SummarizeConfig(BaseModel):
    """Resumo de capitulo e de fonte (ADR-047). Catalogo: config.yaml -> summarize."""

    model_config = ConfigDict(extra="forbid")

    # Acima deste orcamento o capitulo e resumido por map-reduce (grupos de
    # chunks -> resumos parciais -> um resumo so), em vez de uma chamada unica.
    max_input_chars: int = 60000
    # Alvo de saida por resumo. Alvo do estimador, nao teto do modelo.
    preflight_output_tokens_per_chapter: int = 600
    max_topics: int = 8
    # Quantos capitulos o mapa lista com wikilinks de notas antes de truncar a
    # lista de links (o resumo do capitulo em si nunca e truncado).
    max_links_per_chapter: int = 12


class RetrievalConfig(BaseModel):
    """Recuperacao hibrida (vetor + BM25) com fusao RRF e expansao por grafo.

    `mode: vector` preserva o comportamento historico (Chroma puro). `hybrid`
    funde a busca densa do Chroma com o BM25 do FTS5 no state.db.
    """

    mode: Literal["vector", "hybrid"] = "hybrid"
    rrf_k: int = 60  # constante do Reciprocal Rank Fusion (canonica)
    # Termo da pergunta que casa com o Topic Index vira semente EXTRA -- e passa
    # pelo mesmo piso de relevancia. Nunca fura o piso (ver ADR-036).
    topic_index_boost: bool = True
    topic_index_max_seeds: int = 5
    graph_expansion: GraphExpansionConfig = Field(default_factory=GraphExpansionConfig)
    relevance_floor: RelevanceFloorConfig = Field(default_factory=RelevanceFloorConfig)
    # Piso PROPRIO para resumos de capitulo (ADR-047). Objeto separado de
    # `relevance_floor` porque mede outra distribuicao de texto: um resumo de
    # capitulo e mais longo e difuso que uma nota. Os dois valem 0.70 hoje por
    # coincidencia de duas medicoes independentes, nao por heranca -- mexer em
    # um nao deve mexer no outro.
    #
    # Medido em 2026-09-07 sobre 71 capitulos resumidos
    # (ollama/qwen3-embedding@1024d): consultas fora do dominio marcam no maximo
    # 0.693, self-match no minimo 0.745. O 0.60 anterior deixava as seis
    # consultas fora do dominio passarem. Re-medir com
    # `scripts/probe_relevance_floor.py --collection chapter_summaries` apos
    # qualquer troca de embedding -- o numero nao transfere entre modelos.
    chapter_floor: RelevanceFloorConfig = Field(
        default_factory=lambda: RelevanceFloorConfig(min_vector_similarity=0.70)
    )
    ask: AskConfig = Field(default_factory=AskConfig)
    article: ArticleConfig = Field(default_factory=ArticleConfig)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)


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
    formulas: FormulasConfig = Field(default_factory=FormulasConfig)
    code: CodeConfig = Field(default_factory=CodeConfig)
    domain: DomainConfig = Field(default_factory=DomainConfig)
    gardener: GardenerConfig = Field(default_factory=GardenerConfig)
    hub_mocs: HubMocsConfig = Field(default_factory=HubMocsConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    summarize: SummarizeConfig = Field(default_factory=SummarizeConfig)

    language: str = "pt-BR"
    vault_timezone: str = "America/Sao_Paulo"
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

    @field_validator("vault_timezone")
    @classmethod
    def validate_vault_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:
            msg = f"vault_timezone IANA invalido: {v!r}"
            raise ValueError(msg) from exc
        return v


def load_config(path: Path | str | None = None) -> AppConfig:
    """Carrega config/config.yaml (ou ``path``) e valida em AppConfig.

    Contrato YAML-primeiro: cada chave do YAML substitui o Field default;
    chave ausente (ou arquivo faltando) usa o fallback de fabrica. Segredos
    (API keys) vêm de ``.env``, nao do YAML.

    Caminhos relativos no YAML são resolvidos a partir da raiz do repositório
    (pai de ``config/``), não do cwd do processo — o uvicorn/CLI enxergam o
    mesmo ``state.db`` e vault independentemente de onde foram iniciados.
    """
    config_path = Path(path).resolve() if path else _DEFAULT_CONFIG_PATH
    project_root = _project_root_for_config(config_path)

    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
        logger.info("Variaveis de ambiente carregadas de .env")
    else:
        logger.debug(".env nao encontrado, usando apenas variaveis de ambiente do sistema")

    data: dict[str, Any] = {}

    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
            if isinstance(raw, dict):
                data = raw
        logger.info("Configuração carregada de %s", config_path)
    else:
        logger.warning("Arquivo de config não encontrado: %s — usando defaults", config_path)

    if not data:
        return _default_app_config(project_root)

    return AppConfig(**_anchor_relative_paths(data, project_root))


def llm_phase(cfg: Any, phase: str) -> LLMPhaseConfig:
    """Return the LLM identity for a pipeline/QA/vision consumer.

    ``phase`` must be one of ``LLM_PHASES``. Unknown names raise ``ValueError``
    rather than falling back to another phase.
    """
    if phase not in LLM_PHASES:
        raise ValueError(
            f"Fase LLM desconhecida: {phase!r}. Valores validos: {', '.join(LLM_PHASES)}"
        )
    spec = getattr(cfg.llm, phase)
    if not isinstance(spec, LLMPhaseConfig):
        raise TypeError(f"llm.{phase} deve ser LLMPhaseConfig, obtido {type(spec).__name__}")
    return spec


def effective_temperature(cfg: Any, spec: LLMPhaseConfig) -> float:
    """Resolve the sampling temperature for a phase: its own override, else the global default."""
    return cfg.llm.temperature if spec.temperature is None else spec.temperature


def thinking_checksum_token(thinking: ThinkingValue) -> str:
    """Canonical cache-key fragment for ``llm.<phase>.thinking``.

    ``None`` is empty so callers that omit the checksum field stay aligned
    with a phase that left thinking at the vendor default.
    """
    if thinking is None:
        return ""
    if thinking is True:
        return "true"
    if thinking is False:
        return "false"
    return str(thinking)


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger with RichHandler.

    Uses stderr so log messages flow correctly alongside Rich console.status() spinners.
    Quiets noisy HTTP client loggers so pipeline progress (X/Y) stays readable.
    """
    from rich.console import Console
    from rich.logging import RichHandler

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
    for noisy in (
        "httpx",
        "httpcore",
        "openai",
        "openai._base_client",
        "urllib3",
        "google_genai",
        "google.generativeai",
        "google.ai.generativelanguage",
        "langchain_google_genai",
        "grpc",
        "grpc._cython",
        "grpc._cython.cygrpc",
    ):
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
            info["vram_gb"] = round(mem / (1024**3), 1)
            info["cuda_version"] = torch.version.cuda or "N/A"
    except ImportError:
        info["torch_version"] = "nao instalado"
    return info
