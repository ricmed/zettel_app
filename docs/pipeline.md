# Como funciona cada fase do pipeline

[← Voltar ao README](../README.md)

O que acontece por dentro de `harvest → extract → review → connect → garden`, incluindo a resolução de paginação e os parâmetros de clusterização dos MOCs.

```
Arquivo (PDF/MD)
    ↓ harvest
Texto extraído → Chunks (com pagina) → SRC + indice LIT
    ↓ extract
Drafts de LIT granular (1 por chunk) em 00_Inbox/Review
    ↓ review
LIT aprovadas em 20_Literature/{Citekey}/ + literature_notes no Chroma
    ↓ connect
Notas Permanentes (ZTL) com links e backlinks
    ↓ garden
MOCs (Mapas de Conteúdo) por clusterização semântica
```

---

## Fase 1 — Harvest (Coleta)

Módulos: o pacote [`zettel/harvester/`](../zettel/harvester) ([ADR-027](adrs/generated/HARVEST/ADR-027-harvest-phase-as-python-package.md)), [`paging.py`](../zettel/paging.py) e [`bibliography.py`](../zettel/bibliography.py).

1. Varre `data/inbox/` por arquivos `.pdf` e `.md`
2. Calcula checksum SHA-256 de cada arquivo (pula inalterados) e aplica **detecção de duplicatas em 3 camadas** (hash de arquivo → hash do texto extraído → similaridade semântica de chunks)
3. Extrai texto usando **Docling** (PDF) ou parser nativo (Markdown)
4. **Metadados bibliográficos (ABNT)**:
   - Infere o **tipo documental** (`livro`, `capitulo_livro`, `artigo_periodico`, `artigo_internet`, `material_curso`, `tese`, `anais_evento`, `relatorio`) e os campos tipados a partir de metadados do arquivo, heurísticas no texto e, se habilitado, LLM (`prompts/bibliographic_metadata.md`, com cache)
   - Campos obrigatórios variam por tipo (ex.: livro exige autores, título, cidade, editora, ano; artigo de internet exige título, URL e data de acesso)
   - Em modo interativo, **sempre** mostra preview (tabela + referência ABNT) e pede confirmação antes de gravar a SRC — mesmo quando os metadados já estão completos; dá para editar tipo/campos se recusar
   - Em modo não-interativo (`--yes`), bibliografia completa é aceita sem prompt; incompleta **pula o arquivo**, salvo `--skip-biblio` (segue com parcial + aviso)
   - Persiste `document_type`, `bibliography_json` e `abnt_reference` no SQLite; a nota **SRC** recebe os campos separados no frontmatter **e** a string `abnt_reference` pronta para citar
5. Gera **citekey** determinístico: `@SobrenomeAnoTituloSlug` (a partir dos metadados já enriquecidos)
6. Grava nota **SRC** (`SRC - AuthorYear - slug.md`) em `10_Sources/` e o **índice LIT** (`LIT - AuthorYear - slug.md`) em `20_Literature/` **antes** do chunking/embeddings (que podem demorar minutos); `processing_status=in_progress`. Pastas e arquivos **não** usam `@` (o `@` fica só em `source_id` / CLI). O layout antigo (`@Citekey/`, `chunk_NNNN.md`, `*-index.md`) não é lido; reescreva notas `pipeline` com `zettel rebuild --force` e apague leftovers à mão.
7. Se `images.enabled`, registra imagens já extraídas em `90_Assets/`
8. Resolve **início da paginação** (ver a [seção seguinte](#paginacao-arquivo-vs-impressa)): página do **arquivo PDF** onde o conteúdo começa e o **número impresso** nessa página; páginas anteriores não geram chunks. Markdown nativo não tem página.
9. Divide o texto em **capítulos**/seções. Infere `page_in_file` pelo mapa Docling (marcadores de quebra de página no mesmo Markdown dos chunks) e grava `page_in_book = page_in_file - start_file + start_book` (chunk multi-página usa a **primeira** página). Indexa chunks no ChromaDB e no SQLite.
10. Atualiza a SRC com início de conteúdo/páginas/`total_chunks` e `processing_status=completed`
11. **Cobertura de capítulos**: harvest interrompido é completado no próximo `harvest` ou via `zettel rechunk`

### Detecção de duplicatas em 3 camadas

Cada camada é mais barata e mais certeira que a seguinte, e roda antes de o arquivo virar uma fonte nova ([ADR-011](adrs/generated/HARVEST/ADR-011-three-layer-duplicate-detection.md)):

| # | Camada | Como funciona | Resultado |
|---|---|---|---|
| 1 | **Hash de arquivo** (`get_file_by_checksum`) | Bytes idênticos em outro caminho | Trata como cópia renomeada, reusa o `source_id`, não reprocessa |
| 2 | **Hash de extração** (`get_source_by_extraction_checksum`) | Bytes diferentes, texto extraído normalizado idêntico (ex.: mesmo paper em PDF re-exportado e em Markdown) | Reusa a fonte existente |
| 3 | **Similaridade semântica** (`_find_semantic_duplicate_candidates`) | Amostra chunks do arquivo novo e consulta o Chroma por chunks quase idênticos (`harvest.duplicate_chunk_threshold`, default `0.88`) pertencentes a **outras** fontes | Se houver candidatos, `_resolve_duplicate_decision` pergunta (Rich `Prompt`) ou aplica `harvest.non_interactive_duplicate_action` (`skip`/`continue`/`abort`) |

As flags `--yes`, `--skip-duplicates` e `--force` controlam o comportamento não-interativo. Toda decisão é registrada por `db.record_duplicate(run_id, layer)` e aparece no `zettel status` e no resumo do `harvest`.

### Chunking

Chunking híbrido ([ADR-014](adrs/generated/HARVEST/ADR-014-hybrid-structural-chunking-strategy.md)): primeiro corta pela estrutura (headings H3–H6), fundindo seções menores que `chunking.min_section_chars` com a seguinte; depois aplica o splitter da LangChain com `chunk_size` / `chunk_overlap` (em **caracteres**, não tokens). Um terceiro piso roda depois do splitter: pedaço menor que `chunking.min_chunk_chars` (default `200`) é fundido no anterior (ou no seguinte, se for o primeiro da seção) — elimina caudas de corte e réguas horizontais isoladas (`---`) antes que virem uma chamada de LLM. Fontes já harvestadas só se beneficiam do piso novo depois de `zettel rechunk`.

**Fences são átomos.** Um bloco cercado CommonMark (```` ``` ```` ou `~~~`) nunca é cortado. Headings H1–H6 **dentro** do fence são ilustrativos — não viram capítulo nem entram no `section_path`; só headings fora do fence particionam. O splitter de tamanho age apenas na prosa entre fences. Se o fence for maior que `chunk_size`, sai um **chunk oversized** de propósito: fatiar um template ou um trecho de código em `\n\n` é pior que um chunk grande (isso não altera o `chunk_size` global). Fontes harvestadas antes dessa regra mantêm os chunks antigos até você rodar `zettel rechunk`. Fora do escopo do scanner: código indentado por 4 espaços, tabelas fora de fence e HTML.

**Heading no primeiro chunk.** O título ATX da seção (e o H1/H2 do capítulo na primeira peça) é prefixado **só no primeiro chunk** daquela seção, *depois* do split por fences — para um diagrama/template não virar dois chunks. As continuações da mesma seção seguem só com o corpo. O `section_path` continua metadado. Fontes antigas só mudam com `zettel rechunk`.

---

## Paginação: arquivo vs impressa

O número que o leitor vê no livro/revista (`page_in_book`) é o que vai para LIT/ZTL, para os nomes `pNNN` e para a citação ABNT. O índice do PDF (`page_in_file`) é só o deslocamento interno. Fórmula:

`page_in_book = page_in_file - content_start_file + content_start_book`

| Tipo de fonte | O que acontece |
|---|---|
| Livro com miolo (capa, sumario, prefacio) | Detecta Capitulo 1 / Introduction (pula pagina de sumario). Paginas anteriores ao inicio nao viram chunk. Numero impresso nessa pagina: cabecalho/rodape, senao 1. |
| Artigo de revista (PDF p.1 = revista p.200) | Sem marcador de capitulo; usa o numero isolado no cabecalho/rodape da p.1, ou o intervalo bibliografico `pages: 200-210`. |
| Apostila, tutorial, artigo que comeca em 1 | arquivo p.1 = impressa p.1 |
| Markdown (anotacoes de curso, etc.) | Sem pagina. Localizador = `section_path`. Digitos no texto nao viram pagina. |

Como o início é resolvido ([ADR-013](adrs/generated/HARVEST/ADR-013-three-layer-page-inference-strategy.md)):

- Interativo: prompt HITL (sugestão da heurística como default)
- `--content-start-file` / `--content-start-book`: valor explícito (ganha de tudo)
- `--yes` / web / `run-all` sem flags: aplica a heurística em silêncio
- `--skip-paging`: força arquivo p.1 = impressa p.1 (não detecta miolo nem revista)

O mapa de `page_in_file` vem do **Docling** (`export_to_markdown` com `page_break_placeholder`, comentários `<!-- zettel:page-break -->` no texto extraído — o mesmo Markdown dos chunks). O `prov.page_no` do Docling é o índice do arquivo, não o número impresso. Docling é obrigatório — não há fallback para outro extrator ([ADR-012](adrs/generated/HARVEST/ADR-012-docling-pdf-extraction-pymupdf-fallback.md)). Regex no corpo do chunk só roda quando não há mapa e a fonte não é Markdown.

Para corrigir uma fonte já harvestada:

```bash
python -m zettel set-paging --source-id @Citekey --content-start-file 35 --content-start-book 10
```

Isso recalcula o número impresso sem chamar o LLM. Se `page_in_file` estiver errado ou vazio (harvest antigo, anterior ao mapa Docling), reprocesse o PDF (`zettel harvest`) ou rode `zettel rechunk` se o `extracted_text` já tiver os marcadores de quebra.

---

## Fase 2 — Extract (Extração → drafts granulares)

Módulo: [`extractor.py`](../zettel/extractor.py).

0. Descreve imagens pendentes com LLM multimodal (cache determinístico)
1. Para cada chunk `pending`, chama o LLM com o **Prompt 1**; localizador preferencial = `p.{page_in_book} / {section_path}`
2. Escreve um **draft** em `00_Inbox/Review/{Citekey}/LIT - AuthorYear - pNNN - topico-NNNN.md` (resumo, conceitos, candidatos, **trecho integral da fonte**, imagens; mesmo basename da nota aprovada)
3. Checkpoint no SQLite após **cada** chunk: `status=awaiting_review`, `summary_json`, `review_confidence`, `literature_note_path`
4. Concepts ficam em `awaiting_review` (não elegíveis ao `connect` ainda)
5. Filtragem estrutural de qualidade (`extraction.min_relevance_score`, `min_thesis_words`, `min_definition_words`, `require_anchor_quote`, `verify_anchor_quote` — checa faixa de 10-25 palavras e se a citação de fato existe no chunk, tolerando elipse editorial)
6. `--auto-approve` pode promover drafts com confiança ≥ `literature_review.auto_approve_min_confidence`

O nome legível do arquivo (com página e tópico) é uma decisão deliberada — [ADR-015](adrs/generated/EXTRACT/ADR-015-granular-literature-notes-readable-filenames.md).

---

## Fase 2b — Review (aprovação seletiva)

Módulo: [`review.py`](../zettel/review.py).

1. `zettel review` lista drafts `awaiting_review` e um **relatório de faixas** de `review_confidence` (baixíssima `<=0.4`, média até o limiar, alta `>= limiar`). Os cortes (`0.4` em `review.py`; limiar em `literature_review.auto_approve_min_confidence`) são **heurísticas tunáveis** ([ADR-017](adrs/generated/REVIEW/ADR-017-confidence-band-hitl-approval-gate.md)), não valores calibrados empiricamente — o YAML é a fonte operacional (pode divergir do default histórico `0.7` da ADR). Monitore o volume por faixa após harvest/extract e proponha ajuste via issue se a carga ficar desbalanceada; calibração formal só após evidência ou mudança significativa de modelo no extract (ver [RUNBOOK](adrs/RUNBOOK.md))
2. Modo interativo: `a` aprova lote `>= limiar` (abaixo do limiar permanecem pendentes); `d` abre submenu para rejeitar `t=todos` ou por faixa (`b`/`m`/`h`) após confirmação `s/n` (rejeição parcial volta ao menu); `r` revisa um a um com atalhos `a/r/p/q`; `q` sai
3. **Approve**: move para `20_Literature/{Citekey}/LIT - AuthorYear - pNNN - topico-NNNN.md`, indexa na coleção Chroma **`literature_notes`** (apenas a interpretação — o bloco de trecho da fonte é removido do texto embeddado), atualiza o índice LIT (wikilinks com rótulo `p. N — tópico`), promove concepts para dedupe → `approved`
4. **Reject**: apaga draft, `status=rejected`, concepts rejeitados — **nunca** entram em `literature_notes` (o chunk permanece no SQLite/Chroma `chunks` até `zettel purge-rejected`)
5. Deduplicação semântica contra permanentes roda **após** a aprovação, não no extract ([ADR-016](adrs/generated/REVIEW/ADR-016-post-approval-concept-deduplication-timing.md))
6. `zettel purge-rejected`: remove permanentemente chunks `rejected` (SQLite chunks+concepts+FTS, Chroma `chunks` e `literature_notes` se houver) e por padrão roda `VACUUM` em `state.db` e `chroma.sqlite3` (recupera disco; não altera dados restantes; `--no-compact` pula)

---

## Fase 3 — Connect (Conexão)

Módulo: [`connector.py`](../zettel/connector.py).

1. Para cada candidato aprovado, busca **top-k notas similares** (RAG híbrido) — apenas para conexões
2. Chama o LLM com o **Prompt 2** (`permanent_note.md`):
   - Conceito + contexto RAG + opcionalmente `{images_context}` das figuras do candidato → nota permanente completa
3. Valida idioma PT-BR (guardrail automático)
4. Resolve imagens do candidato (`relevant_image_ids`, com fallback por path no chunk se a lista estiver vazia) e cria o arquivo **ZTL** em `30_Permanent/` com:
   - Frontmatter YAML (type, note_id, source_id, tags, origin, etc.)
   - Corpo: Tese → Definição → Intuição → Exemplo → Limites → **Figuras** (embeds Obsidian + legendas) → Fonte → Conexões
5. Atualiza **backlinks** nas notas relacionadas via blocos gerenciados:
   ```
   <!-- zettel:auto-backlinks:start -->
   - [[ZTL - ID - titulo]]
   <!-- zettel:auto-backlinks:end -->
   ```
6. Indexa no ChromaDB e registra no SQLite, **persistindo o corpo e o frontmatter completos** (`notes.body`/`frontmatter_json`) — o que permite recriar o `.md` sem reprocessar o LLM. O re-embedding é pulado quando o conteúdo semântico e o modelo não mudaram (`embedding_input_hash`). A chamada do Prompt 2 também é cacheada.

O `literature_ref` aponta para a **LIT granular aprovada** daquele chunk (com fallback para o índice da fonte). Valores de `related_note_id` são canonicalizados (removendo `ZTL -` e wrappers de wikilink até sobrar o ULID) e **descartados** se o alvo não existir no SQLite ou o arquivo tiver sumido — nada de wikilinks fantasma `[[ZTL - ZTL - ULID]]`. O bloco `auto-backlinks` é **reconstruído** a partir das arestas de entrada em `note_connections` (stem do arquivo atual + relação inversa), nunca apenas concatenado.

Quando uma nota permanente entra ou sai de um MOC, o pipeline atualiza o bloco **`auto-moc-backrefs`** na ZTL (ver Fase 4 e [notas-manuais.md](notas-manuais.md)).

---

## Fase 4 — Garden (Jardim)

Módulos: [`gardener.py`](../zettel/gardener.py), [`gardener_assign.py`](../zettel/gardener_assign.py), [`moc_backrefs.py`](../zettel/moc_backrefs.py).

Pipeline **híbrido** (taxonomia → cluster por categoria → grafo → roteamento LLM) — [ADR-019](adrs/generated/GARDEN/ADR-019-taxonomy-first-moc-clustering.md) e [ADR-021](adrs/generated/GARDEN/ADR-021-single-llm-call-per-cluster-routing.md):

1. Carrega embeddings de todas as notas permanentes
2. **Atribuição taxonomia-first** (`gardener_assign.py`): embedda labels das categorias de `config/moc_topics.yaml` e agrupa cada nota no bucket de maior similaridade
3. **Clusterização por bucket**: UMAP + HDBSCAN (ou KMeans como fallback) **dentro de cada categoria**, alinhando clusters ao guarda-chuva da taxonomia
4. Extrai termos representativos via **TF-IDF**
5. **Roteamento inteligente** (`_process_cluster`) — no máximo **1 chamada LLM por cluster**:
   - Assinatura idêntica → skip (sem LLM)
   - Overlap de notas com MOC existente ≥ `overlap_threshold` → `moc_incremental` apenas
   - Categoria do bucket já tem MOC → `moc_incremental` apenas
   - Coesão de grafo abaixo de `graph_cohesion_min_ratio` (se > 0) → cluster rejeitado, sem MOC novo
   - Caso contrário → `moc_generation` uma vez, com **categoria sugerida** pelo pipeline no prompt
6. **Validação de tópico** pós-geração:
   - Substring match bidirecional contra os nomes das **categorias** do YAML
   - Se `strict_topics: true` e sem match: MOC rejeitado (com warning no log)
   - Se `strict_topics: false` e sem match: MOC aprovado (com info no log)
7. **Atualização incremental**: classifica notas novas nas subseções existentes (`moc_incremental.md`); pode criar subseções
8. Notas fora de MOC (ruído do HDBSCAN) permanecem no vault e são navegáveis via grafo/conexões — não existe fila de órfãs

Parâmetros híbridos em `config.yaml` (`gardener.*`):

| Parametro | Proposito |
|-----------|-----------|
| `cluster_within_category` | Ativa pipeline taxonomia-first (default `true`) |
| `category_label_template` | Texto embeddavel por categoria (ex. `"{domain}: {categoria}"`) |
| `overlap_threshold` | Fracao do cluster ja presente em um MOC → update incremental direto |
| `graph_cohesion_enabled` | Calcula score interno do cluster via `note_connections` |
| `graph_cohesion_min_ratio` | `0` = so log; `>0` rejeita cluster fraco antes de criar MOC novo |
| `umap_n_neighbors` | `null` = auto |
| `hdbscan_min_samples` | Opcional; ajuste fino do HDBSCAN |

`zettel garden --recreate` apaga os MOCs gerados pelo pipeline (`origin='pipeline'`) e regenera do zero, preservando MOCs manuais. Antes de apagar cada MOC, **`clear_moc_backrefs`** remove os links dele dos blocos `auto-moc-backrefs` das notas permanentes. Ao criar ou atualizar um MOC, **`sync_moc_backrefs`** adiciona/remove wikilinks do MOC nas ZTL listadas no corpo do mapa.

---

## Fase 4b — Garden Hub (porta de entrada temática)

Módulo: [`gardener_hub.py`](../zettel/gardener_hub.py) — [ADR-020](adrs/generated/GARDEN/ADR-020-hub-anchored-moc-pipeline.md).

Complementar ao pipeline taxonômico: MOCs ancorados em **notas-hub** (alto grau ponderado em `note_connections`). Uma nota pode aparecer em MOC de categoria **e** em MOC hub.

```bash
python -m zettel garden --hubs              # MOCs hub (complementar)
python -m zettel garden --hubs --recreate -y
```

1. Ranqueia notas permanentes por **grau ponderado** no grafo (`DEFAULT_RELATION_WEIGHTS`)
2. Expande a vizinhança via BFS (`expand_notes`, `max_hops` configurável)
3. Deduplica vizinhanças muito sobrepostas (`dedup_subset_threshold`)
4. Roteia cada hub:
   - MOC existente com o mesmo `hub_note_id` → `moc_hub_incremental`
   - Senão → `moc_hub_generation` (tópico livre, derivado pelo LLM)
5. Persiste com `origin='hub_pipeline'`, frontmatter `hub_note_id` e seção **Porta de entrada**

| Parametro (`hub_mocs.*`) | Proposito |
|--------------------------|-----------|
| `selection_mode` | `percentile` (top %) ou `absolute` (limiar fixo) |
| `hub_percentile` | Percentil minimo no modo percentile (default `0.90`) |
| `min_weighted_degree` | Limiar absoluto de grau ponderado |
| `top_n_hubs` | Teto de hubs processados por run |
| `max_hops` / `decay` | Expansao BFS a partir do hub |
| `max_neighbors` / `min_neighbors` | Tamanho da vizinhanca |
| `min_neighbor_weight` | Filtra vizinhos fracos pos-BFS |
| `dedup_subset_threshold` | Descarta hub menor se vizinhanca >= N% contida em outra |

`garden --hubs --recreate` apaga apenas MOCs `origin='hub_pipeline'`; MOCs taxonômicos (`pipeline`) e manuais permanecem intactos. A limpeza dos blocos `auto-moc-backrefs` segue a mesma lógica do `--recreate` taxonômico.

> **Pré-requisito**: o grafo precisa estar populado. Rode `connect` e, se você escreve notas à mão, `sync-manual --rebuild-graph`.

---

## Ver também

- [Comandos](cli.md) — flags de cada fase
- [Notas geradas](notas.md) — o formato de saída de cada fase
- [Recuperação](recuperacao.md) — o RAG que alimenta o `connect`
- [Notas manuais](notas-manuais.md) — o caminho paralelo, sem portão de aprovação
- [Solução de problemas](troubleshooting.md) — quando uma fase não produz o esperado
