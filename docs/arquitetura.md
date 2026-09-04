# Arquitetura

[← Voltar ao README](../README.md)

Mapa do repositório, papel de cada módulo, estrutura do vault, as camadas de proteção contra drift e como os custos de LLM/embedding são registrados.

Decisões de fundo estão documentadas nos [ADRs](adrs/ADR-INDEX.md) — 31 decisões formais em 12 módulos.

---

## Estrutura do repositório

```
zettel_app/
├── zettel/                  # Pacote principal
│   ├── cli.py               # Interface CLI (Typer + Rich)
│   ├── config.py            # Schema Pydantic + fallback; load_config le o YAML
│   ├── schemas.py           # Modelos Pydantic (dados + saidas estruturadas do LLM)
│   ├── hashing.py           # Hashing canonico em camadas
│   ├── pricing.py           # Estimativa de custo via mapa LiteLLM (so calculadora)
│   ├── usage.py             # CostTracker por run/fonte (contextvars)
│   ├── llm.py               # get_llm / call_llm (com usage + custo)
│   ├── state.py             # SQLite — estado incremental, grafo, FTS5, cache
│   ├── vault.py             # I/O do vault Obsidian (frontmatter, blocos gerenciados)
│   ├── index.py             # ChromaDB — indice vetorial (5 colecoes)
│   ├── bibliography.py      # Metadados bibliograficos ABNT (tipos, inferencia, formatacao)
│   ├── harvester/           # Fase 1 como PACOTE (ADR-027)
│   │   ├── __init__.py      # API publica: run_harvest / run_rechunk / run_set_paging
│   │   ├── pipeline.py      # Orquestracao: processamento de arquivo, chunking, persistencia
│   │   ├── extract.py       # Extracao de texto de PDF (Docling) e Markdown
│   │   ├── chunking.py      # Divisao em capitulos e persistencia de chunks
│   │   ├── citekey.py       # Geracao de citekey a partir dos metadados
│   │   ├── duplicates.py    # Deteccao de duplicatas em 3 camadas
│   │   ├── biblio_hitl.py   # HITL de metadados bibliograficos (UI Rich)
│   │   └── set_paging.py    # Correcao de paginacao em fontes ja harvestadas
│   ├── paging.py            # Inferencia de pagina arquivo/livro + inicio do conteudo
│   ├── chunk_dump.py        # Dump markdown opt-in dos chunks (inspecao de chunking)
│   ├── extraction_dump.py   # Dump markdown opt-in do texto extraido (headings Docling/MD)
│   ├── extractor.py         # Fase 2: Prompt 1 -> drafts LIT granulares
│   ├── review.py            # Fase 2b: aprovacao seletiva de LIT antes do vetorial
│   ├── connector.py         # Fase 3: geracao de notas permanentes
│   ├── gardener.py          # Fase 4: clusterizacao e MOCs taxonomicos
│   ├── gardener_assign.py   # Atribuicao por taxonomia, cluster por categoria, coesao de grafo
│   ├── gardener_hub.py      # Fase 4b: MOCs ancorados em notas-hub do grafo
│   ├── taxonomy.py          # Carga do YAML de topicos (pilar > categoria > topicos)
│   ├── moc_backrefs.py      # Bloco auto-moc-backrefs em notas permanentes
│   ├── retrieval.py         # Recuperacao hibrida (vetor + BM25) com fusao RRF
│   ├── graph.py             # Expansao por grafo sobre as conexoes tipadas (GraphRAG leve)
│   ├── ask.py               # Comando `ask`: QA sobre o vault com citacoes
│   ├── article.py           # Comando `article`: helpers de dominio (catalogo, draft, judge)
│   ├── article_graph/       # Orquestracao LangGraph do `article` (StateGraph + HITL, ADR-028/029)
│   ├── assets.py            # Extracao, adocao e descricao multimodal de imagens
│   ├── rebuild.py           # Reconstrucao do Chroma (reindex) e do vault (rebuild)
│   ├── sync.py              # Sincronizacao de notas manuais (SRC/LIT/ZTL/MOC) + grafo
│   ├── manual_lit.py        # Adocao de LIT manual e caminho LIT -> ZTL (ADR-030)
│   ├── new_note.py          # Scaffold de notas manuais (zettel new-note)
│   ├── purge_source.py      # Remocao completa de fonte (zettel delete-source)
│   ├── progress.py          # Protocolo ProgressObserver (CLI + web)
│   ├── markdown.py          # Render Markdown seguro (bleach) para a interface web
│   ├── web/                 # Interface web FastAPI (pacote, ADR-039)
│   ├── web_app.py           # Fila de jobs web e dispatch do pipeline
│   ├── templates/           # Jinja2 server-rendered (paginas + base.html e partials)
│   └── static/              # CSS (app.css, markdown.css, mobile.css, theme.css) + combobox.js
├── config/
│   ├── config.yaml          # Fonte operacional (todos os knobs do schema)
│   ├── moc_topics.yaml      # Taxonomia hierarquica de topicos para MOCs
│   └── personalities.yaml   # Perfis de reescrita do `zettel article`
├── prompts/                        # Templates de prompts para o LLM
│   ├── bibliographic_metadata.md   # Extracao de metadados bibliograficos (ABNT)
│   ├── literature_note.md          # Prompt 1: extracao de conceitos (c/ relevance_score)
│   ├── permanent_note.md           # Prompt 2: geracao de nota permanente (+ tipos de relacao)
│   ├── dedupe_decision.md          # Decisao de deduplicacao
│   ├── moc_generation.md           # Geracao de MOCs (c/ dominio e categorias)
│   ├── moc_incremental.md          # Classificacao incremental de notas em MOC existente
│   ├── moc_hub_generation.md       # Geracao de MOC ancorado em nota-hub
│   ├── moc_hub_incremental.md      # Atualizacao incremental de MOC hub
│   ├── ptbr_guard.md               # Guardrail de idioma PT-BR
│   ├── ask.md                      # Resposta a perguntas sobre o vault
│   ├── image_description.md        # Descricao multimodal de imagens (PT-BR)
│   ├── article_query_enrich.md     # Expansao do tema em queries semanticas
│   ├── article_outline.md          # Outline do artigo
│   ├── article_section_blog.md     # Draft de secao (estilo blog)
│   ├── article_section_academic.md # Draft de secao (estilo academico, ABNT)
│   ├── article_personality.md      # Reescrita estilistica
│   ├── article_judge.md            # Juiz de qualidade do artigo
│   └── article_anti_ai.md          # Bloco anti-prosa generica no article
├── data/
│   ├── inbox/               # Arquivos para processar (drop zone)
│   ├── cache/               # Cache intermediario (checkpointer article, dumps)
│   ├── chroma/              # ChromaDB (persistente)
│   └── state.db             # SQLite (estado do pipeline)
├── vault/                   # Vault do Obsidian (criado pelo init)
├── docs/                    # Esta documentacao + ADRs
├── tests/                   # Testes unitarios
├── pyproject.toml           # Dependencias e metadados (gerenciado por uv)
└── README.md
```

---

## Estrutura do vault

```
vault/
├── 00_Inbox/                # Entrada; respostas do `ask` e artigos salvos
│   └── Review/              # Drafts de LIT granular (aguardando aprovacao)
├── 10_Sources/              # Notas bibliograficas (SRC)
├── 20_Literature/           # Indice LIT (raiz) + {Citekey}/ com as LIT granulares
├── 30_Permanent/            # Notas permanentes (ZTL)
├── 40_MOCs/                 # Mapas de Conteudo
└── 90_Assets/               # Imagens extraidas de PDFs/Markdown (nome por hash)
```

Convenções de nomes e IDs:

- Arquivos seguem `PREFIXO - IDENTIFICADOR - slug.md`. SRC e índice LIT usam `AuthorYear`; a LIT granular é `LIT - AuthorYear - pNNN - topico-NNNN.md`; ZTL e MOC usam ULID.
- O `@` pertence ao `source_id` e à CLI — **nunca** a caminhos do vault.
- IDs: fontes usam `@citekey`; chunks usam `source_id::chapter_id::short_hash`; notas e MOCs usam ULID.

---

## Infraestrutura compartilhada

| Módulo | Responsabilidade |
|---|---|
| [`config.py`](../zettel/config.py) | Schema Pydantic + fallback de fábrica. A fonte operacional é `config/config.yaml`; segredos ficam no `.env`. Identidade de LLM é **por fase**; knobs de amostragem são globais. Veja [configuracao.md](configuracao.md). |
| [`state.py`](../zettel/state.py) | SQLite em modo WAL ([ADR-001](adrs/generated/INFRA/ADR-001-sqlite-wal-fts5-primary-persistence.md)). Tabelas: `files`, `sources`, `chapters`, `chunks`, `concepts`, `notes`, `mocs`, `assets`, `llm_cache`, `note_connections`, `runs`, `web_jobs`, `web_job_events` + as virtuais FTS5 `fts_notes`/`fts_chunks`. `runs` e `sources` guardam custo e tokens estimados. |
| [`index.py`](../zettel/index.py) | Wrapper do ChromaDB ([ADR-002](adrs/generated/INFRA/ADR-002-chromadb-embedded-vector-store.md)) com 5 coleções: `sources`, `chunks`, `permanent_notes`, `mocs` e `literature_notes`. LITs só são embeddadas **após** a aprovação no review. Metadata do Chroma aceita apenas `str`/`int`/`float`/`bool` — listas são unidas com `", "` por `_sanitize_metadata()`. |
| [`vault.py`](../zettel/vault.py) | I/O do Obsidian: parse/render de frontmatter YAML, blocos gerenciados e escrita segura que nunca sobrescreve edição manual fora dos blocos. Builders de SRC, índice LIT e LIT granular. `sync_source_costs_to_vault` espelha os custos do SQLite no frontmatter da SRC. |
| [`llm.py`](../zettel/llm.py) | `get_llm` / `call_llm` / `load_prompt_parts` / `fill_template`. Instancia o client por fase, aplica o split System/Human dos prompts, lê `usage_metadata` e registra custo. Veja [ADR-024](adrs/generated/LLM/ADR-024-multi-provider-llm-strategy.md) e [ADR-025](adrs/generated/LLM/ADR-025-prompt-caching-system-human-split.md). |
| [`pricing.py`](../zettel/pricing.py) / [`usage.py`](../zettel/usage.py) | `cost_per_token` do LiteLLM como **calculadora de preço** (não como client de LLM); `CostTracker` agrega por run/fonte via contextvars. |
| [`hashing.py`](../zettel/hashing.py) | Normalização canônica (NFKC, colapso de espaços, de-hifenização de PDF) antes de qualquer hash. `dehyphenate_pdf_linebreaks` é reaproveitada também na extração de PDF (`harvester/extract.py`), antes da persistência — não só no hash. |
| [`schemas.py`](../zettel/schemas.py) | Modelos Pydantic v2 de todos os objetos de dados e das saídas estruturadas do LLM (`LiteratureChunkOutput`, `PermanentNoteLLMOutput`, `DedupeResult`, `MOCGenerationOutput`, `ArticleOutline`…). |

Cada comando da CLI monta a tripla `(AppConfig, StateDB, VectorIndex)` via `_load_deps()`, `_get_db()` e `_get_idx()` em `cli.py`.

---

## Fluxo de dados entre as fases

```
harvest  → chunks `pending` no SQLite + Chroma `chunks`
extract  → drafts de LIT + concepts `awaiting_review`
review   → LIT aprovadas em `literature_notes`; concepts deduplicados → `approved`
connect  → le `get_concepts_by_status("approved", without_notes=True)` do SQLite
garden   → le embeddings de notas permanentes + `note_connections`
```

Toda comunicação entre fases passa por **StateDB** e **ChromaDB** — nenhuma fase depende do estado em memória da anterior ([ADR-005](adrs/generated/INFRA/ADR-005-dual-store-persistence.md)).

---

## Estratégia anti-drift

O sistema usa múltiplas camadas de proteção contra drift (mudanças indesejadas) — [ADR-007](adrs/generated/INFRA/ADR-007-layered-hashing-strategy.md):

| Camada | Hash | Finalidade |
|---|---|---|
| Arquivo | `file_checksum` | Detectar alteração binária |
| Extração | `extraction_checksum` | Separar mudança binária de textual |
| Capítulo | `chapter_checksum` | Reprocessar só capítulos alterados |
| Chunk | `chunk_checksum` | Reprocessar só chunks alterados |
| LLM | `llm_call_checksum` | Cache de chamadas ao LLM (Prompt 1 e Prompt 2) |
| Nota | `note_semantic_checksum` | Detectar mudança de conteúdo da nota |
| Embedding | `embedding_input_hash` | Pular re-embed quando conteúdo + modelo não mudam |

### IDs estáveis

- `source_id`: derivado do citekey (`@AutorAnoSlug`)
- `chunk_id`: `source_id::chapter_id::short_hash(chunk_checksum)` — estável se o texto não muda
- `concept_id`: baseado em âncora do texto-fonte (não no output do LLM)
- `note_id`: ULID, mapeado via `concept_id → note_id` — sem duplicação

### Blocos gerenciados

Atualizações automáticas ficam dentro de marcadores HTML:

```
<!-- zettel:auto-backlinks:start -->
...conteúdo auto-gerenciado...
<!-- zettel:auto-backlinks:end -->
```

Tudo fora desses blocos é preservado — edições manuais nunca são sobrescritas.

Blocos usados pelo pipeline:

| Bloco | Onde | Atualizado por |
|-------|------|----------------|
| `auto-backlinks` | ZTL alvo de conexoes | `connect` |
| `auto-connections` | ZTL (sugestoes) | `sync-manual` |
| `auto-lit-index` | indice LIT | `review` |
| `auto-source-excerpt` | LIT granular | `extract` |
| `auto-moc-backrefs` | ZTL listada em MOCs | `garden`, `garden --hubs`, `sync-manual`; removido em `garden --recreate` / `garden --hubs --recreate` |

O bloco **`auto-moc-backrefs`** lista os MOCs (taxonômicos, hub ou manuais) que referenciam a nota permanente no corpo do mapa. Quando um MOC é editado, links obsoletos saem e novos entram; ao purgar MOCs com `--recreate`, o bloco é limpo **antes** de o arquivo ser apagado. Esses blocos são ignorados ao extrair wikilinks manuais para o grafo (`sync-manual`).

---

## Custos de LLM e embeddings

Cada chamada de LLM (via `call_llm`) e cada upsert/query de embedding registra tokens e **USD estimado** usando o mapa público do pacote **LiteLLM** (`cost_per_token` — só calculadora; o runtime continua LangChain). Atualize preços/modelos com `uv sync --upgrade-package litellm`.

- **Por comando**: totais em `runs` (também no fim do log: `Custo do run: ...`). `zettel status` mostra a tabela do último run.
- **Por fonte**: colunas acumulativas em `sources` e no frontmatter da SRC (`cost_usd_*`, `tokens_*`).
- **Por ZTL**: `llm_cost_usd` / tokens / `llm_cache_hit` no frontmatter + log na geração.
- **Cache SQLite** (`cache_hits`) = `$0`. **Prompt cache do provedor** = contadores `prompt_cache_read` / `prompt_cache_write` nos logs (não confundir com `cache_hits`).
- Ollama / modelos locais = `$0` (tokens ainda são contados). Os valores estimados são *list price*, não a fatura do provedor.

---

## Ver também

- [Pipeline](pipeline.md) — o que cada fase executa
- [Notas geradas](notas.md) — o formato exato de SRC, LIT e ZTL
- [Recuperação](recuperacao.md) — busca híbrida, grafo e piso de relevância
- [Operação](operacao.md) — o que é reconstruível e o que é fonte de verdade
- [ADRs](adrs/ADR-INDEX.md) — decisões de arquitetura, com contexto e alternativas
