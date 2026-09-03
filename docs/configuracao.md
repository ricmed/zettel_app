# Configuração

[← Voltar ao README](../README.md)

Tudo que se ajusta sem tocar em código: o catálogo completo de `config/config.yaml`, os provedores de LLM e de embedding, os dois mecanismos de cache e o procedimento para trocar o modelo de embedding.

---

## Onde mora cada coisa

| Arquivo | Papel |
|---|---|
| [`config/config.yaml`](../config/config.yaml) | **Fonte operacional.** É o arquivo que o CLI e a web carregam. |
| [`zettel/config.py`](../zettel/config.py) | Schema Pydantic (tipos, validators) + **fallback de fábrica**. Só entra em ação quando o YAML falta, quando uma chave é omitida, ou nos testes que instanciam `AppConfig()`. |
| `.env` | Segredos (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `SESSION_SECRET`). **Nunca** no YAML. |
| [`config/moc_topics.yaml`](../config/moc_topics.yaml) | Taxonomia de tópicos dos MOCs (pilar > categoria > tópicos). Veja [prompts.md](prompts.md#taxonomia-de-topicos-para-mocs). |
| [`config/personalities.yaml`](../config/personalities.yaml) | Personalidades de reescrita do `zettel article`. |

`load_config` faz `AppConfig(**yaml)`: chave presente no YAML vence; chave ausente cai no `Field` default do schema. Um teste ([`tests/test_config.py`](../tests/test_config.py)) exige que **toda** chave do schema exista no YAML, com uma exceção — `gardener.allowed_topics`, que é override só de teste.

Para usar outro arquivo:

```bash
python -m zettel run-all --config ./minha_config.yaml
```

Na web, a variável de ambiente `ZETTEL_CONFIG` cumpre o mesmo papel.

> **Sobre os valores abaixo**: o bloco a seguir é o **catálogo de knobs com os defaults do schema** (`zettel/config.py`). O `config/config.yaml` deste repositório está calibrado para o vault local (Ollama, CUDA, limiares ajustados) e **diverge propositalmente** de vários desses defaults — ele é a fonte operacional; este catálogo é a referência do que existe e do que cada chave significa.

---

## Catálogo completo

```yaml
# ── Caminhos ────────────────────────────────────────────────────────────
vault_path: ./vault             # vault Obsidian (00_Inbox ... 90_Assets)
inbox_path: ./data/inbox        # drop zone de PDF/Markdown para o harvest
chroma_path: ./data/chroma      # indice vetorial persistente (ChromaDB)
state_db_path: ./data/state.db  # SQLite: estado, FTS5, grafo, cache de LLM
cache_path: ./data/cache        # cache intermediario (checkpointer do article, dumps)
prompts_path: ./prompts         # templates .md dos prompts

# ── LLM ─────────────────────────────────────────────────────────────────
# Knobs de amostragem sao GLOBAIS. Cada fase declara a propria identidade
# (provider + model + base_url) — nao ha heranca de um "modelo unico".
llm:
  temperature: 0             # 0 = deterministico (reduz drift)
  top_p: 1                   # nucleus sampling (encaminhado ao client LangChain)
  max_retries: 2             # retries do client em falha HTTP
  prompt_cache: true         # prefix cache do provedor (System + Human)
  harvest:                   # metadados bibliograficos ABNT
    provider: openai         # openai | anthropic | ollama | gemini | openrouter | opencode
    model: gpt-4o-mini
    base_url: null           # gateways OpenAI-compatible / Ollama
  extract:                   # Prompt 1 — notas de literatura
    provider: openai
    model: gpt-4o-mini
    base_url: null
  review:                    # dedupe de conceitos pos-aprovacao
    provider: openai
    model: gpt-4o-mini
    base_url: null
  connect:                   # Prompt 2 — notas permanentes
    provider: openai
    model: gpt-4o-mini
    base_url: null
  garden:                    # MOCs taxonomicos e hub
    provider: openai
    model: gpt-4o-mini
    base_url: null
  ask:                       # zettel ask
    provider: openai
    model: gpt-4o-mini
    base_url: null
  article:                   # zettel article (todos os nos do grafo)
    provider: openai
    model: gpt-4o-mini
    base_url: null
  images:                    # descricao multimodal — precisa ser um modelo vision
    provider: openai
    model: gpt-4o-mini
    base_url: null

# ── Embeddings ──────────────────────────────────────────────────────────
embedding:
  provider: openai                    # openai | sentence-transformers | ollama
  model: text-embedding-3-small
  base_url: null                      # ollama: default http://localhost:11434 (host nativo)
  allow_fallback: false               # false = erro se faltar API key
                                      # (evita cair no default 384-d do Chroma sem aviso)
  dimensions: null                    # truncagem MRL. null = dimensao nativa do modelo.
                                      # Vale para ollama (langchain_ollama) e para os
                                      # text-embedding-3-* da OpenAI.
                                      # Trocar exige `zettel reindex --force`.

# ── Chunking ────────────────────────────────────────────────────────────
chunking:
  chunk_size: 1000           # caracteres por chunk (nao tokens)
  chunk_overlap: 200         # sobreposicao entre chunks consecutivos
  min_section_chars: 200     # secoes (H3+) menores sao fundidas com a seguinte

# ── Imagens (extracao no harvest + descricao multimodal) ───────────────
images:
  enabled: false             # extrai imagens de PDF (Docling) e Markdown e as descreve
  scale: 2.0                 # images_scale do Docling
  min_width: 64              # descarta imagens menores (icones/logos)
  min_height: 64
  context_chars: 600         # caracteres ao redor da imagem usados como contexto
  min_interval_seconds: 0.4  # pacing entre descricoes (evita estourar o TPM)
  rate_limit_max_retries: 8  # retries por imagem em 429 (nao marca failed)
  rate_limit_backoff_max: 60 # teto de espera (s)
  rate_limit_abort_after: 5  # 429 esgotados consecutivos => pausa o lote (fica pending)

# ── Linkagem / Deduplicacao ────────────────────────────────────────────
linking:
  topk: 5                    # default do Retriever e do RAG de connect/sync
  dedupe_threshold: 0.90     # similaridade; L2 = 2 * (1 - threshold) no extract
  preflight_output_tokens_per_note: 1200  # alvo de saida por nota no pre-voo (nao e teto)

# ── Harvest (duplicatas + metadados bibliograficos ABNT) ───────────────
harvest:
  duplicate_chunk_threshold: 0.88   # similaridade minima p/ suspeita semantica (camada 3)
  duplicate_sample_size: 5          # chunks amostrados do arquivo novo
  non_interactive_duplicate_action: skip   # skip | continue | abort
  biblio_confidence_threshold: 0.7  # abaixo disso, pede confirmacao do tipo documental
  biblio_llm_enabled: true          # enriquece metadados via LLM apos as heuristicas
  biblio_text_sample_chars: 5000    # amostra inicial (capa/folha de rosto) enviada ao LLM

# ── Literature review (aprovacao seletiva de LIT por chunk) ────────────
# Limiares sao heuristicas iniciais (ADR-017), nao calibracao empirica.
# O corte "muito baixo" (0.4) vive em review.py, nao no YAML.
literature_review:
  auto_approve_min_confidence: 0.85
  batch_sample_size: 20              # max drafts listados no review interativo
  drafts_subdir: 00_Inbox/Review     # relativo ao vault

# ── Filtragem de candidatos a notas permanentes ────────────────────────
extraction:
  min_relevance_score: 3     # score minimo de relevancia (1-5)
  min_thesis_words: 5        # palavras minimas na tese
  require_anchor_quote: true # exigir citacao-ancora
  min_definition_words: 10   # palavras minimas na definicao
  preflight_output_tokens_per_chunk: 800  # alvo de saida por chunk no pre-voo (nao e teto)

# ── Recuperacao (busca hibrida + GraphRAG leve) ────────────────────────
retrieval:
  mode: hybrid               # hybrid (vetor + BM25) | vector (so Chroma, legado)
  rrf_k: 60                  # constante do Reciprocal Rank Fusion
  topic_index_boost: true    # termo do Topic Index vira semente extra (ainda passa pelo piso)
  topic_index_max_seeds: 5   # teto de notas trazidas por essa via em uma consulta
  relevance_floor:
    enabled: true            # piso ABSOLUTO de relevancia (alem do ranking RRF)
    min_vector_similarity: 0.70   # similaridade coseno minima (calibre por corpus)
    bm25_hit_bypasses_floor: true # match lexical real pode dispensar a similaridade
    bm25_bypass_max_rank: 5       # ... mas so quando o match lexical for forte (top-5)
    absolute_min_similarity: 0.15 # piso rigido que nem o bypass do BM25 derruba
  graph_expansion:
    enabled: true            # expande resultados pelas conexoes tipadas entre notas
    max_hops: 1              # saltos no grafo (1 ja traz o valor principal)
    decay: 0.5               # atenuacao do score por salto
    max_neighbors: 10        # teto de vizinhos trazidos ao contexto
    relation_weights:        # omitir = DEFAULT_RELATION_WEIGHTS em config.py
      contradicts: 1.0
      extends: 0.9
      depends_on: 0.9
      supports: 0.8
      exemplifies: 0.7
      related: 0.5
  ask:
    topk: 8                  # notas semente do comando `ask`
    max_context_notes: 8     # teto de notas no contexto do LLM
    max_chars_per_note: 1500 # truncagem do corpo de cada nota no contexto
  article:
    topk: 20                 # notas semente do comando `article`
    max_context_notes: 24
    max_chars_per_note: 1200
    max_hops: 2              # expansao de grafo mais ampla que a do ask
    max_sections: 8          # teto de secoes no outline
    max_figures: 6           # teto de figuras no catalogo
    chars_per_section_draft: 2500
    personalities_path: ./config/personalities.yaml
    default_personality: neutral   # neutral = nao reescreve via LLM
    enrich_query_count: 6          # queries extras geradas no enricher
    max_judge_iterations: 3        # ciclos draft <-> judge
    judge_min_score: 7.0           # media minima para APPROVED
    writer_temperature: null       # null = llm.temperature
    judge_temperature: 0.2
    enrich_temperature: 0.2

# ── Gardener (MOCs taxonomicos; zettel garden) ─────────────────────────
gardener:
  min_cluster_size: 5        # notas minimas por cluster
  min_notes_for_moc: 3       # cluster menor que isso nao vira MOC
  domain: ""                 # dominio do acervo (ex.: "Ciencia de Dados")
  strict_topics: true        # rejeitar MOCs fora das categorias da taxonomia
  topics_path: ./config/moc_topics.yaml  # pilar > categoria > topicos
  cluster_within_category: true          # clusteriza dentro de cada bucket da taxonomia
  category_label_template: "{domain}: {categoria}"  # texto embeddado por categoria
  overlap_threshold: 0.4     # fracao do cluster ja no MOC -> update incremental
  graph_cohesion_enabled: true
  graph_cohesion_min_ratio: 0.0  # 0 = so log; >0 rejeita cluster fraco
  umap_n_neighbors: null     # null = auto min(15, n-1)
  hdbscan_min_samples: null  # null = default do HDBSCAN

# ── MOCs hub (zettel garden --hubs) ────────────────────────────────────
hub_mocs:
  selection_mode: percentile # percentile (escala com o vault) | absolute (limiar fixo)
  hub_percentile: 0.90       # modo percentile: 0.90 = top 10% por grau ponderado
  top_n_hubs: 10             # teto de hubs processados por execucao
  min_weighted_degree: 8.0   # modo absolute: grau ponderado minimo
  max_hops: 2                # profundidade BFS a partir do hub
  max_neighbors: 15          # teto do cluster (hub + vizinhos de maior peso)
  min_neighbors: 8           # vizinhanca minima; abaixo disso o hub e ignorado
  decay: 0.5                 # atenuacao do peso por salto
  min_neighbor_weight: 0.3   # peso BFS minimo para o vizinho entrar
  dedup_subset_threshold: 0.8  # descarta hub cuja vizinhanca ja esta contida em outra

# ── Gerais ─────────────────────────────────────────────────────────────
language: pt-BR              # idioma dos prompts e do texto gerado
log_level: INFO              # raiz do pipeline (httpx/openai ficam em WARNING)
device: auto                 # auto | cpu | cuda (Docling + sentence-transformers)
```

Notas de uso:

- `chunking.*` entra no hash de paginação — mudar esses valores re-chunka as fontes no próximo `harvest`/`rechunk`. Veja [operacao.md](operacao.md#zettel-rechunk).
- `harvest.*`, `extraction.*` e `literature_review.*` são explicados em contexto no [pipeline](pipeline.md).
- `retrieval.*` tem uma seção própria em [recuperacao.md](recuperacao.md).
- `gardener.*` e `hub_mocs.*` estão detalhados em [pipeline.md](pipeline.md#fase-4--garden-jardim).
- `gardener.allowed_topics` existe no schema como override de testes e **não** deve aparecer no YAML: a whitelist real vem de `topics_path`.

---

## Provedores de LLM suportados

Cada fase (`harvest`, `extract`, `review`, `connect`, `garden`, `ask`, `article`, `images`) declara o próprio `provider` / `model` / `base_url`. Dá para misturar — por exemplo, `extract` na nuvem e `garden` no Ollama. A identidade do modelo de visão fica em `llm.images` (não existe mais `images.model`).

Os knobs de amostragem (`temperature`, `top_p`, `max_retries`, `prompt_cache`) são globais, em `llm.*`.

### OpenAI (padrão)

```yaml
llm:
  extract:
    provider: openai
    model: gpt-4o-mini    # ou gpt-4o, gpt-4-turbo
    base_url: null
  prompt_cache: true
```

Requer: `OPENAI_API_KEY`.

### Gateways OpenAI-compatible (OpenRouter, OpenCode, vLLM, LM Studio, Azure-compatible)

```yaml
llm:
  extract:
    provider: openrouter   # ou opencode | compatible | azure
    model: openai/gpt-4o-mini
    base_url: https://openrouter.ai/api/v1
  prompt_cache: true
```

Usa `ChatOpenAI` com o `base_url` da fase. A chave segue o que o gateway espera (normalmente `OPENAI_API_KEY`).

### Anthropic

```yaml
llm:
  extract:
    provider: anthropic
    model: claude-sonnet-4-20250514
    base_url: null
  prompt_cache: true
```

Requer: `ANTHROPIC_API_KEY` e `uv add langchain-anthropic`.

### Gemini

```yaml
llm:
  extract:
    provider: gemini
    model: gemini-2.0-flash
    base_url: null
  prompt_cache: true
```

Requer: `GOOGLE_API_KEY` (ou equivalente do SDK). Usa `ChatGoogleGenerativeAI`; o pacote `langchain-google-genai` já vem instalado.

### Ollama (local)

```yaml
llm:
  extract:
    provider: ollama
    model: llama3.1        # ou qualquer modelo local
    base_url: null         # default nativo do Ollama
```

Requer: Ollama rodando localmente. O pacote `langchain-ollama` já vem instalado.

---

## Prompt caching do provedor vs cache SQLite

Há **dois** mecanismos distintos, que costumam ser confundidos:

1. **`llm_cache` (SQLite)** — se a mesma chamada (prompt + inputs + modelo + temperatura) se repetir, a resposta completa é reutilizada: `$0`, sem HTTP. É o `cache_hits` que aparece nos logs e no `zettel status`. A chave é o `llm_call_checksum` (veja [arquitetura.md](arquitetura.md#estrategia-anti-drift)).
2. **Prompt caching do provedor** — atua entre chamadas *diferentes* que compartilham o mesmo prefixo de instruções (ex.: N chunks no `extract`). Os templates em `prompts/` usam o marcador `<!-- zettel:user -->`: o trecho **antes** vira `SystemMessage` (estável) e o **depois** vira `HumanMessage` (payload da chamada). OpenAI e Gemini aproveitam o prefixo implicitamente; Anthropic recebe `cache_control` explícito (`apply_prompt_cache_hints`); Ollama e gateways OpenAI-compatible ganham apenas o layout (reuso de KV, sem efeito em billing).

Contadores: `prompt_cache_read_tokens` / `prompt_cache_write_tokens` nos logs `COST` — observabilidade apenas; a estimativa em USD via LiteLLM ainda usa tokens de entrada/saída totais, e o desconto real aparece na fatura do provedor. Templates curtos podem ficar abaixo do mínimo do provedor (~1k–2k tokens) e não gerar hit.

Para comparar com e sem, desligue os hints e o layout com `llm.prompt_cache: false`:

```yaml
llm:
  prompt_cache: false
```

---

## Provedores de embedding suportados

### OpenAI (padrão)

```yaml
embedding:
  provider: openai
  model: text-embedding-3-small
  dimensions: null      # ou 512 / 1024 ... (MRL suportado nos text-embedding-3-*)
```

Requer: `OPENAI_API_KEY`.

### Sentence-Transformers (local via PyTorch)

```yaml
embedding:
  provider: sentence-transformers
  model: all-MiniLM-L6-v2
```

Usa o `device` da config (`auto` / `cpu` / `cuda`).

### Ollama (local)

```yaml
embedding:
  provider: ollama
  model: qwen3-embedding
  dimensions: 1024      # truncagem MRL; null = nativo (qwen3-embedding 8B = 4096)
  # base_url: http://localhost:11434   # opcional; host nativo do Ollama
```

Requer: Ollama rodando com o modelo de embedding puxado (`ollama pull qwen3-embedding`).

### `allow_fallback`

`allow_fallback: false` (padrão) faz o sistema **falhar** se a API key estiver ausente, em vez de cair silenciosamente na função de embedding default do Chroma (384 dimensões) — o que misturaria espaços vetoriais incompatíveis sem aviso.

---

## Trocar o modelo de embedding

O espaço vetorial é identificado pela tripla **`provider` / `model` / `dimensions`**, gravada na metadata das coleções do Chroma. Mudar **qualquer um dos três** torna os vetores antigos incompatíveis.

1. Edite `embedding.provider`, `embedding.model` e/ou `embedding.dimensions` (e opcionalmente `base_url`) em `config/config.yaml`.
2. No próximo comando que abre o índice (`harvest`, `extract`, `connect`, `ask`, `reindex`…), o CLI detecta o drift, avisa e pede confirmação.
3. Confirmando (ou passando `--yes`), roda automaticamente um `reindex --force`: regenera todos os embeddings a partir do SQLite, **sem** chamar o LLM e **sem** reescrever notas `.md`.

Forçando na mão:

```bash
python -m zettel reindex --force
python -m zettel reindex --force --yes   # sem prompt (scripts/CI)
```

Sem `--force` após uma troca de modelo, sources/chunks já indexados **não** seriam regenerados (só as notas permanentes), misturando espaços vetoriais. Por isso, ao detectar drift, o `reindex` aplica `--force` automaticamente.

Depois da troca, se a qualidade da busca degradar, recalibre:

- `retrieval.relevance_floor.min_vector_similarity` — o piso é dependente do modelo;
- `linking.dedupe_threshold` e `harvest.duplicate_chunk_threshold` — limiares de dedupe, calibrados sobre distância L2 crua.

O `zettel doctor` também reporta drift de embedding.

---

## Ver também

- [Instalação](instalacao.md) — chaves de API e `.env`
- [Recuperação](recuperacao.md) — o que cada knob de `retrieval.*` faz na prática
- [Prompts e taxonomia](prompts.md) — personalizar `prompts/` e `moc_topics.yaml`
- [Arquitetura](arquitetura.md#custos-de-llm-e-embeddings) — como os custos são estimados e registrados
