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
LIT aprovadas em 20_Literature/{Citekey}/ (cofre + SQLite, sem embedding)
    ↓ connect
Notas Permanentes (ZTL) com links e backlinks
    ↓ garden
MOCs (Mapas de Conteúdo) por clusterização semântica
```

---

## Fase 1 — Harvest (Coleta)

Módulos: o pacote [`zettel/harvester/`](../zettel/harvester) ([ADR-027](adrs/generated/HARVEST/ADR-027-harvest-phase-as-python-package.md)), [`paging.py`](../zettel/paging.py) e [`bibliography.py`](../zettel/bibliography.py).

1. Varre `data/inbox/` por arquivos `.pdf` e `.md`
2. Calcula checksum SHA-256 de cada arquivo (pula inalterados) e aplica **detecção de duplicatas em 5 camadas** (hash de arquivo → hash do texto extraído → DOI/ISBN exato → título+autor → similaridade semântica de chunks)
3. Extrai texto usando **Docling** (PDF) ou parser nativo (Markdown). Antes do Docling, um *probe* de camada de texto (`pypdfium2`, dependência do próprio Docling) lê as 4 primeiras páginas: menos de 40 caracteres visíveis = PDF escaneado, recusado na hora com sugestão de OCR, sem pagar conversão. Logo depois, o texto passa por `text_sanitize` (zero-width, marcas bidi, bloco de tags Unicode) **antes** do checksum de extração ([ADR-033](adrs/generated/HARVEST/ADR-033-invisible-unicode-sanitization-and-text-layer-probe.md))
4. **Metadados bibliográficos (ABNT)**:
   - Infere o **tipo documental** (`livro`, `capitulo_livro`, `artigo_periodico`, `artigo_internet`, `material_curso`, `tese`, `anais_evento`, `relatorio`) e os campos tipados a partir de metadados do arquivo, heurísticas no texto e, se habilitado, LLM (`prompts/bibliographic_metadata.md`, com cache)
   - Campos obrigatórios variam por tipo (ex.: livro exige autores, título, cidade, editora, ano; artigo de internet exige título, URL e data de acesso)
   - Em modo interativo, **sempre** mostra preview (tabela + referência ABNT) e pede confirmação antes de gravar a SRC — mesmo quando os metadados já estão completos; dá para editar tipo/campos se recusar
   - Em modo não-interativo (`--yes`), bibliografia completa é aceita sem prompt **desde que algum campo obrigatório não tenha vindo só do modelo**; se autores, título e os demais obrigatórios são todos `llm`, o arquivo é pulado como incompleto. Incompleta **pula o arquivo**, salvo `--skip-biblio` (segue com parcial + aviso). O que está no frontmatter (`user`) não é sobrescrito pelo LLM. A origem de cada campo fica em `bibliography_json.provenance` (`user`, `file_metadata`, `llm`, `heuristic`) e aparece no preview do CLI e na revisão da web.
   - Persiste `document_type`, `bibliography_json` e `abnt_reference` no SQLite; a nota **SRC** recebe os campos separados no frontmatter **e** a string `abnt_reference` pronta para citar
5. Gera **citekey** determinístico: `@SobrenomeAnoTituloSlug` (a partir dos metadados já enriquecidos)
6. Grava nota **SRC** (`SRC - AuthorYear - slug.md`) em `10_Sources/` e o **índice LIT** (`LIT - AuthorYear - slug.md`) em `20_Literature/` **antes** do chunking/embeddings (que podem demorar minutos); `processing_status=in_progress`. Pastas e arquivos **não** usam `@` (o `@` fica só em `source_id` / CLI). O layout antigo (`@Citekey/`, `chunk_NNNN.md`, `*-index.md`) não é lido; reescreva notas `pipeline` com `zettel rebuild --force` e apague leftovers à mão.
7. Se `images.enabled`, registra imagens já extraídas em `90_Assets/`
8. Resolve **início da paginação** (ver a [seção seguinte](#paginacao-arquivo-vs-impressa)): página do **arquivo PDF** onde o conteúdo começa e o **número impresso** nessa página; páginas anteriores não geram chunks. Markdown nativo não tem página.
9. Divide o texto em **capítulos**/seções. Infere `page_in_file` pelo mapa Docling (marcadores de quebra de página no mesmo Markdown dos chunks) e grava `page_in_book = page_in_file - start_file + start_book` (chunk multi-página usa a **primeira** página). Indexa chunks no ChromaDB e no SQLite.
10. Atualiza a SRC com início de conteúdo/páginas/`total_chunks` e `processing_status=completed`
11. **Cobertura de capítulos**: harvest interrompido é completado no próximo `harvest` ou via `zettel rechunk`

Um arquivo que não passa na extração não derruba o lote: `run_harvest` devolve um `HarvestOutcome` com `source_ids` e `skipped` (motivo `empty_text_layer` ou `extraction_failed`); os demais arquivos do inbox seguem sendo processados, a CLI lista os recusados e sai com código 1, e a UI web mostra a mensagem real no job.

### Detecção de duplicatas em 5 camadas

Cada camada é mais barata e mais certeira que a seguinte, e roda antes de o arquivo virar uma fonte nova ([ADR-011](adrs/generated/HARVEST/ADR-011-three-layer-duplicate-detection.md)). As camadas 1, 2 e 5 vivem em `harvester/duplicates.py`; as camadas 3 e 4, em `harvester/biblio_dedupe.py`:

| # | Camada | Como funciona | Resultado |
|---|---|---|---|
| 1 | **Hash de arquivo** (`get_file_by_checksum`) | Bytes idênticos em outro caminho | Trata como cópia renomeada, reusa o `source_id`, não reprocessa |
| 2 | **Hash de extração** (`get_source_by_extraction_checksum`) | Bytes diferentes, texto extraído normalizado idêntico (ex.: mesmo paper em PDF re-exportado e em Markdown) | Reusa a fonte existente |
| 3 | **DOI / ISBN** (`find_exact_bibliographic_match`) | DOI no formato Crossref, pontuação final removida. ISBN com dígito verificador. | **DOI**: reusa sem perguntar. **ISBN**: só sem perguntar quando o título normalizado coincide e nenhum dos dois é capítulo ou anais; senão pergunta (sem TTY, segue como fonte nova) |
| 4 | **Título + autor** (`find_title_author_candidates`) | Exige título normalizado idêntico **e** sobrenome de autor em comum. `normalize_title` reusa `hashing.fold_for_match` e descarta o subtítulo após `:`; sobrenomes comparam o último token, então "D. Kahneman" casa com "Daniel Kahneman" | **Heurística**: sempre pergunta, nunca funde sozinha. Sem TTY o default é **`continue`** (cria fonte nova) |
| 5 | **Similaridade semântica** (`find_semantic_duplicate_candidates`) — **desligada por padrão**, ver abaixo | Amostra chunks do arquivo novo e consulta o Chroma por chunks quase idênticos (`harvest.duplicate_chunk_threshold`, default `0.76` cosseno, ADR-058) pertencentes a **outras** fontes | Se houver candidatos, `resolve_duplicate_decision` pergunta (Rich `Prompt`) ou aplica `harvest.non_interactive_duplicate_action` (`skip`/`continue`/`abort`) |

Um arquivo que **já está nesse caminho** e mudou de bytes não é nenhuma dessas camadas. `harvest.modified_file_action` decide: `skip` (padrão) deixa a fonte como está; `update` regrava a mesma fonte (citekey estável; capítulo inalterado conserva o `chunk_id`; capítulo reescrito volta com chunks `pending`); `new` cria outra fonte e o caminho passa a apontar para ela. No modo interativo o harvest pergunta. DOI/ISBN da própria fonte em atualização não interrompe a regravação. A nota permanente permanece; o arquivo LIT no vault não é apagado ([ADR-046](adrs/generated/HARVEST/ADR-046-bibliographic-duplicate-layers.md)).

#### A camada 5 vem desligada (`harvest.semantic_duplicate_enabled: false`)

Ela responde uma pergunta binária por arquivo ingerido — "este arquivo é a mesma obra que uma fonte que já tenho?" — mas o preço é **embedar todo chunk de toda fonte** para manter o índice-alvo. O gasto cresce com o acervo; o uso, com os arquivos novos. Na prática é o maior item isolado do tempo de harvest.

O flag governa **escrita e leitura ao mesmo tempo**, e isso não é opcional: consultar um índice que o pipeline parou de povoar não dá erro, dá **falso negativo silencioso** — a duplicata passa e você não fica sabendo. Por isso ele gateia os dois lados juntos:

| Ponto | Efeito com `false` |
|---|---|
| `chunking.chunk_and_persist` | não gera embedding de chunk (SQLite e FTS5 seguem completos) |
| `pipeline._process_file` | a camada 5 não é consultada |
| `rebuild.run_reindex` | a coleção `chunks` não é tocada — nem repovoada, nem resetada |

**Para ligar**: mude o flag e rode `zettel reindex --collection chunks` para povoar o índice-alvo com o acervo que já existe. Sem isso a camada roda contra uma coleção vazia e não detecta nada.

**Quando ligar vale a pena**: se o seu acervo tem muito material sem metadado utilizável (apostila, handout, PDF sem capa, título que o extrator chutou), a camada 5 é a única rede que sobra — as camadas 3 e 4 dependem de DOI/ISBN ou de título e autor confiáveis. Se você importa sobretudo material catalogado, elas já cobrem, de forma determinística e sem limiar para calibrar.

As camadas 3 e 4 são SQLite puro e rodam **antes de qualquer embedding**, então baratearam o caso comum. Elas **não** substituem a camada 5: a similaridade semântica é a única rede para material sem metadado utilizável (um handout, um PDF sem capa, um título que o extrator chutou).

Por que a camada 4 pergunta em vez de fundir: título e autor coincidem entre um livro e um capítulo dele, entre a 2ª e a 1ª edição, e entre uma tradução e o original. O ano é **reportado, nunca usado para descartar** — essa é exatamente a decisão do usuário. E fundir é a direção irreversível: o ADR-011 registra que não existe operação para separar dois documentos depois.

Caso do PDF escaneado: um scan nunca chega a essas camadas — o `assert_pdf_has_text_layer` ([ADR-033](adrs/generated/HARVEST/ADR-033-invisible-unicode-sanitization-and-text-layer-probe.md)) falha rápido sugerindo OCR. A duplicata aparece só **depois** do OCR, quando as camadas 1 e 2 erram (bytes e texto diferentes) e a 3/4 pegam.

As flags `--yes`, `--skip-duplicates` e `--force` controlam o comportamento não-interativo das camadas 1, 2 e 5. Toda decisão é registrada por `db.record_duplicate(run_id, layer)` — `file`, `content`, `biblio`, `semantic` — e aparece no `zettel status` e no resumo do `harvest`.

### Chunking

Chunking híbrido ([ADR-014](adrs/generated/HARVEST/ADR-014-hybrid-structural-chunking-strategy.md)): primeiro corta pela estrutura (headings H3–H6), fundindo seções menores que `chunking.min_section_chars` com a seguinte; depois aplica o splitter da LangChain com `chunk_size` / `chunk_overlap` (em **caracteres**, não tokens). Um terceiro piso roda depois do splitter: pedaço menor que `chunking.min_chunk_chars` (default `200`) é fundido no anterior (ou no seguinte, se for o primeiro da seção) — elimina caudas de corte e réguas horizontais isoladas (`---`) antes que virem uma chamada de LLM. Fontes já harvestadas só se beneficiam do piso novo depois de `zettel rechunk`.

**Fences são átomos.** Um bloco cercado CommonMark (```` ``` ```` ou `~~~`) nunca é cortado. Headings H1–H6 **dentro** do fence são ilustrativos — não viram capítulo nem entram no `section_path`; só headings fora do fence particionam. O splitter de tamanho age apenas na prosa entre fences. Se o fence for maior que `chunk_size`, sai um **chunk oversized** de propósito: fatiar um template ou um trecho de código em `\n\n` é pior que um chunk grande (isso não altera o `chunk_size` global). Fontes harvestadas antes dessa regra mantêm os chunks antigos até você rodar `zettel rechunk`. Uma tabela Markdown fora de fence é o mesmo tipo de átomo: cabe em `chunk_size * fence_section_slack` e sai inteira; acima disso é cortada por linha, e cada parte repete o cabeçalho e o separador. O padding de coluna que o Docling coloca nas células é colapsado na extração, antes do `extraction_checksum` — um re-harvest muda o checksum; `rechunk` sozinho não limpa um texto já gravado. Fora do escopo do scanner: código indentado por 4 espaços e HTML. Linhas `|` dentro de um fence continuam sendo conteúdo do fence.

**Heading no primeiro chunk.** O título ATX da seção (e o H1/H2 do capítulo na primeira peça) é prefixado **só no primeiro chunk** daquela seção, *depois* do split por fences — para um diagrama/template não virar dois chunks. As continuações da mesma seção seguem só com o corpo. O `section_path` continua metadado. Fontes antigas só mudam com `zettel rechunk`.

---

## Paginação: arquivo vs impressa

O número que o leitor vê no livro/revista (`page_in_book`) é o que vai para LIT/ZTL, para os nomes `pNNN` e para a citação ABNT. O índice do PDF (`page_in_file`) é só o deslocamento interno. Fórmula:

`page_in_book = page_in_file - content_start_file + content_start_book`

| Tipo de fonte | O que acontece |
|---|---|
| Livro com miolo (capa, sumario, prefacio) | Se o PDF tem `/PageLabels` (romanos e depois `1`), o início é a primeira página rotulada `1`. Senão detecta Capitulo 1 / Introduction (pula pagina de sumario). Paginas anteriores ao inicio nao viram chunk. |
| Artigo de revista (PDF p.1 = revista p.200) | Se os rótulos começam em `200`, esse é o número impresso. Senão, sem marcador de capitulo, usa o intervalo bibliografico `pages: 200-210`. |
| Apostila, tutorial, artigo que comeca em 1 | arquivo p.1 = impressa p.1 |
| Markdown (anotacoes de curso, etc.) | Sem pagina. Localizador = `section_path`. Digitos no texto nao viram pagina. |

Como o início é resolvido ([ADR-013](adrs/generated/HARVEST/ADR-013-three-layer-page-inference-strategy.md)):

- `/PageLabels` numéricos (pypdfium2): primeira página rotulada `1`, ou o primeiro rótulo arábico quando nenhum é `1`. O padrão `1..N` que o pdfium devolve sem rótulos não conta. Confiança `labels`, sem prompt
- Interativo: prompt HITL (sugestão como default) e a primeira linha do texto da página sugerida
- `--content-start-file` / `--content-start-book`: valor explícito (ganha de tudo)
- `--yes` / web / `run-all` sem flags: aplica rótulos ou, na falta deles, a heurística em silêncio
- `--skip-paging`: força arquivo p.1 = impressa p.1 (não detecta miolo nem revista)

O mapa de `page_in_file` vem do **Docling**, uma fatia por página do arquivo (`export_to_markdown(page_no=)`, o `prov.page_no`), inclusive páginas sem texto. O texto persistido leva um marcador numerado `<!-- zettel:page N -->` por página. O mapa só é usado quando cobre `1..total_pages_file`; se divergir, o harvest registra o erro e não rotula nenhuma página como exata. A página do chunk é o deslocamento do início do texto nesse mapa (`exact` se a sonda está numa só página, `fuzzy` se o cabeçalho se repete ou só há interseção de palavras, `inferred` por interpolação). `set-paging` preserva o rótulo. O marcador antigo `<!-- zettel:page-break -->` não é mapa — um re-harvest é necessário. O `prov.page_no` é o índice do arquivo, não o número impresso. Docling é obrigatório — não há fallback para outro extrator ([ADR-012](adrs/generated/HARVEST/ADR-012-docling-pdf-extraction-pymupdf-fallback.md), [ADR-013](adrs/generated/HARVEST/ADR-013-three-layer-page-inference-strategy.md)). Regex no corpo do chunk só roda quando não há mapa e a fonte não é Markdown.

Para corrigir uma fonte já harvestada:

```bash
python -m zettel set-paging --source-id @Citekey --content-start-file 35 --content-start-book 10
```

Isso recalcula o número impresso sem chamar o LLM. Se `page_in_file` estiver errado ou vazio (harvest antigo, ou texto ainda com `<!-- zettel:page-break -->`), reprocesse o PDF (`zettel harvest`). `zettel rechunk` só reconstrói o mapa se o `extracted_text` já tiver os marcadores numerados `<!-- zettel:page N -->`.

Um harvest interrompido fica com `processing_status=in_progress`. A passada seguinte retoma: o `chapter_checksum` só vale quando os chunks daquele capítulo foram commitados na mesma transação. O índice Chroma de chunks é refeito a partir do SQLite.

---

## Fase 2 — Extract (Extração → drafts granulares)

Módulo: [`extractor.py`](../zettel/extractor.py).

0. Descreve imagens pendentes com LLM multimodal (cache determinístico)
1. Para cada chunk `pending`, chama o LLM com o **Prompt 1**; localizador preferencial = `p.{page_in_book} / {section_path}`
2. Escreve um **draft** em `00_Inbox/Review/{Citekey}/LIT - AuthorYear - pNNN - topico-NNNN.md` (resumo, conceitos, candidatos, **trecho integral da fonte**, imagens; mesmo basename da nota aprovada)
3. Checkpoint no SQLite após **cada** chunk: `status=awaiting_review`, `summary_json`, `review_confidence`, `literature_note_path`
4. Concepts ficam em `awaiting_review` (não elegíveis ao `connect` ainda)
5. Filtragem estrutural de qualidade (`extraction.min_relevance_score`, `min_thesis_words`, `min_definition_words`, `require_anchor_quote`, `verify_anchor_quote` — piso de 10 palavras; acima do teto tolerado, uma citação literal é recortada para 25 palavras em vez de descartar o candidato; citação que não está no chunk continua descartada)
6. `--auto-approve` pode promover drafts com confiança ≥ `literature_review.auto_approve_min_confidence`
7. **Julgamento do autor** (opcional): quando o trecho *enuncia* a regra, o candidato traz `decision_rules`, `anti_patterns` e `named_frameworks`. São opcionais por construção — default `[]`, não entram na filtragem de qualidade, e o validador trunca em 3 itens em vez de falhar o chunk. O draft ganha o bloco `auto-decision` sob `## Julgamento do autor` (só quando houver conteúdo) e a ZTL carrega as listas no frontmatter ([ADR-034](adrs/generated/EXTRACT/ADR-034-optional-author-judgement-fields.md))

O nome legível do arquivo (com página e tópico) é uma decisão deliberada — [ADR-015](adrs/generated/EXTRACT/ADR-015-granular-literature-notes-readable-filenames.md).

---

## Fase 2b — Review (aprovação seletiva)

Módulo: [`review.py`](../zettel/review.py).

1. `zettel review` lista drafts `awaiting_review` e um **relatório de faixas** de `review_confidence` (baixíssima `<=0.4`, média até o limiar, alta `>= limiar`). O limiar operacional é `literature_review.auto_approve_min_confidence` **0.75** ([ADR-017](adrs/generated/REVIEW/ADR-017-confidence-band-hitl-approval-gate.md)). O corte 0.4 continua uma heurística; um draft comum não cai nessa faixa (o adendo de 2026-10-19 do ADR-017 registra quando ela é alcançável). Monitore o volume por faixa após harvest/extract e proponha ajuste via issue se a carga ficar desbalanceada; calibração formal só após evidência ou mudança significativa de modelo no extract (ver [RUNBOOK](adrs/RUNBOOK.md))
2. Modo interativo: toda ação volta ao menu, que só fecha com `q` ou quando não resta nada. O Enter no menu é `r` (revisar um a um); o Enter no cartão é `p` (pular), qualquer que seja a confiança. `a` pede confirmação (`s/N`) com a contagem e o aviso de que o limiar não foi validado, e só então aprova o lote `>= limiar` (os de baixo continuam listados). A fila e a tabela começam pelos drafts com defeito (candidato descartado pelo filtro, ou campo em falta) e, no resto, pela confiança crescente — a amostra de `batch_sample_size` é o começo dessa fila, não os 20 primeiros da consulta. `d` abre submenu para rejeitar `t=todos` ou por faixa (`b`/`m`/`h`) após confirmação `s/n`; `r` revisa um a um o que resta, com atalhos `a/r/p/q` (`q` volta ao menu); `x` lista os chunks **rejeitados pelo extract** (título, afiliação, referências…: nunca tiveram draft, por isso ficam fora das faixas) com categoria e motivo, e devolve os escolhidos para `pending` apagando o veredito em cache — rode `zettel extract` depois; `q` sai. O resumo da sessão separa aprovações manuais das aprovações por limiar. `--low-confidence-only` junto com `--yes` ou `--auto-approve` é recusado. No modo `r` e na web a decisão é por candidato (tese, definição, intuição, limites, relevância e âncora destacada no trecho). Aprovar um e rejeitar outro persiste o chunk só com o aprovado. Uma tese editada no rascunho entra no `candidate_json`; uma âncora que não está no trecho não é aprovada.
3. **Approve**: move para `20_Literature/{Citekey}/LIT - AuthorYear - pNNN - topico-NNNN.md`. O arquivo aprovado é gravado, o SQLite é atualizado e só então o draft sai do inbox. Um lote (`a`, web, `--auto-approve`) reescreve o **mapa de capítulos** do índice LIT uma vez por fonte no fim; uma aprovação avulsa reescreve a fonte na hora. O mapa é o único lugar onde as LIT aprovadas aparecem, uma por linha, com rótulo `p. N — tópico`. Promove concepts para dedupe **escopada à própria fonte** → `approved`. A nota **não é embeddada**. Um draft rejeitado que permanece no disco entra no resumo da sessão e no `zettel status`.
4. **Dedupe propõe, você decide**: quando a dedupe acha que um conceito repete outra nota da mesma fonte (decisão `ignore` do LLM, ou absorvido por outro candidato do mesmo lote), ele **não é descartado**: vira `dedupe_pending`, fora do `connect`. O review interativo e a fila web `/review?queue=duplicates` mostram a tese nova ao lado da nota/candidato concorrente e do motivo. Manter aprova o conceito e marca `override`, então a dedupe nunca mais o sinaliza. Descartar marca `duplicate`. No CLI, `--yes` e o `run-all` sem decisão interativa deixam os pendentes esperando; a contagem aparece no resumo e no `zettel status`. O `run-all` só aprova drafts das fontes que o harvest daquela execução escreveu.
5. **Refinamento**: quando a dedupe decide `refine_existing` ou `merge`, o conceito é aprovado e a nota-alvo fica gravada em `concepts.dedupe_json`. No `connect`, a nova nota ganha uma aresta `extends` para essa nota, a menos que o modelo já a tenha ligado ([adendo da ADR-045](adrs/generated/REVIEW/ADR-045-cross-source-overlap-is-corroboration.md#amendment-2026-09-24-refine_existing-now-reaches-connect))
6. **Reject**: apaga draft, `status=rejected`, concepts rejeitados (o chunk permanece no SQLite/Chroma `chunks` até `zettel purge-rejected`)
7. Deduplicação semântica contra permanentes roda **após** a aprovação, não no extract ([ADR-016](adrs/generated/REVIEW/ADR-016-post-approval-concept-deduplication-timing.md))
8. `zettel purge-rejected`: remove permanentemente chunks `rejected` (SQLite chunks+concepts+FTS e Chroma `chunks`) e por padrão roda `VACUUM` em `state.db` e `chroma.sqlite3` (recupera disco; não altera dados restantes; `--no-compact` pula)

---

## Fase 3 — Connect (Conexão)

Pacote: [`connector/`](../zettel/connector/) ([ADR-053](adrs/generated/CONNECT/ADR-053-connect-phase-as-python-package.md)): `run` (entrada e loop), `note` (um candidato -> uma ZTL), `prompt` (Prompt 2 + cache + guard PT-BR), `context` (RAG, analogias, imagens), `links` (relações e backlinks).

1. Para cada candidato aprovado, busca **top-k notas similares** (RAG híbrido) — apenas para conexões — e uma busca secundária de **analogias distantes** (fora do bucket taxonômico, piso local `linking.distant_analogy_min_similarity`; o piso global de `ask`/`article` não muda)
2. Chama o LLM com o **Prompt 2** (`permanent_note.md`):
   - Conceito + contexto RAG (embedding / grafo / analogias distantes) + opcionalmente `{images_context}` das figuras do candidato → nota permanente completa
3. Valida idioma PT-BR (guardrail automático)
4. Resolve imagens do candidato (`relevant_image_ids`, com fallback por path no chunk se a lista estiver vazia) e cria o arquivo **ZTL** em `30_Permanent/` com:
   - Frontmatter YAML enxuto (type, note_id, title, source_id, chunk_id, tags, origin, page, datas e as listas de julgamento do autor). `literature_ref`, `source_locator`, `citation`, `anchor_quote` e o custo LLM da nota ficam em `notes.provenance_json` no SQLite — os quatro primeiros já aparecem no corpo
   - Corpo: Tese → Definição → Intuição → Exemplo → Limites → **Figuras** (embeds Obsidian + legendas) → Fonte → Conexões
5. Atualiza **backlinks** nas notas relacionadas via blocos gerenciados:
   ```
   <!-- zettel:auto-backlinks:start -->
   - [[ZTL - ID - titulo]]
   <!-- zettel:auto-backlinks:end -->
   ```
6. Indexa no ChromaDB e registra no SQLite, **persistindo o corpo e o frontmatter completos** (`notes.body`/`frontmatter_json`) — o que permite recriar o `.md` sem reprocessar o LLM. O re-embedding é pulado quando o conteúdo semântico e o modelo não mudaram (`embedding_input_hash`). A chamada do Prompt 2 também é cacheada. O conceito fica `connecting` até o vetor e as arestas existirem; só então vira `noted`. Uma queda do embedding na busca ou no upsert interrompe o connect antes de pagar o Prompt 2 (na busca) ou sem marcar o conceito `noted` (no upsert). O `ask` continua podendo responder só com BM25.

`zettel reconnect` aplica o Prompt 2 atual às notas permanentes que já existem. Troca as arestas `origin='llm'` cuja origem é a nota, preserva `manual` e `derived`, e grava em cada aresta nova o hash do template, o modelo e o `run_id`. O corpo da nota não é reescrito. A busca vê o acervo inteiro, então uma nota antiga pode ligar uma nota mais nova.

Uma recusa do Prompt 2 marca o conceito `connect_rejected` (motivo e categoria em `dedupe_json`) e a execução seguinte não pergunta de novo. JSON inválido vai para `llm_failures` e sai do cache. Cada conexão é validada sozinha: tipo inválido, repetida, fora do bloco RAG ou além de 4 é descartada, e a nota permanece.

As sementes do connect podem ser limitadas por fonte (`linking.seed_same_source_max`, hoje 0) e a expansão no grafo do connect é `linking.graph_expansion`, separada da do `ask`. Os números atuais copiam o ask. A regra que decide se mudam está em `evals/preregistration/285-connect-rag-diversidade.md` e ainda não foi executada. A ordem dentro de cada grupo do contexto continua a do retriever até a regra em `evals/preregistration/286-ordem-contexto-prompt2.md`. `linking.prompt2_mode` fica `combined`: o Prompt 2 atual escreve e liga na mesma chamada. `split` (`note_write.md` com o trecho e a âncora, depois `note_link.md`) espera a regra em `evals/preregistration/288-escrita-ligacao.md`. Com `linking.llm_edges: propose`, a aresta do modelo fica na fila (`zettel links` ou `/review?queue=links`) e não é percorrida até ser aceita. `corroborates` e o `extends` da dedupe entram direto.

A fila do connect é estável: `source_id`, capítulo, `chunk_index`, `rowid`. O `run-all` (CLI e web) só conecta conceitos das fontes daquele harvest; o `zettel connect` isolado vê o acervo inteiro. O CLI pergunta o pré-voo antes do connect no `run-all` (`--yes` e stdin sem TTY passam). O pré-voo conta `topk + vizinhos de grafo + analogias distantes`. O guard PT-BR usa o cache SQLite e uma queda do provider interrompe a fase; se ele reescreveu o corpo, `provenance_json.ptbr_guard` fica verdadeiro. O resumo do CLI lista arestas por tipo, intra × entre fontes, `corroborates`, sugestões em `auto-connections`, recusas e custo.

O `literature_ref` aponta para a **LIT granular aprovada** daquele chunk (com fallback para o índice da fonte), com alias `p. N — tópico`. A seção `## Fonte` e o frontmatter carregam também `page`, a **página impressa estrutural** lida de `chunks.page_in_book` — o `source_locator` continua ali como campo descritivo, mas é prosa escrita pelo LLM no Prompt 1, então não é a página de referência. Markdown nativo não tem página: o campo é omitido, nunca nulo.

### Corroboração entre fontes

Quando um hit que o `Retriever` **já trouxe** para o contexto RAG é semente de busca (`hop == 0`), tem similaridade de cosseno acima de `linking.corroborates_min_similarity` (0.76, ADR-058) e pertence a um **`source_id` diferente**, o connect injeta uma aresta `corroborates` (teto: `linking.corroborates_max_edges`, default 3). Dois autores convergindo sobre a mesma ideia é o produto da pesquisa, não uma duplicata a colapsar — e é por isso que a dedupe da Fase 2b é escopada à própria fonte.

Isso custa **zero embedding e zero chamada de LLM** adicionais: a busca já rodou. A linha é gravada com **`origin='derived'`**, não `'llm'` — a aresta não foi proposta por modelo nenhum, e é isso que autoriza gravá-la como aresta real em vez de sugestão. Grava-se **uma linha**, não o par simétrico — a travessia do grafo é não-direcionada e o `auto-backlinks` renderiza o inverso (`corroborado por`) na outra nota, exatamente como `extends` já funciona. A ordem de processamento se resolve sozinha: se A foi conectada antes de B existir, B grava B→A na própria execução.

`corroborates` **não aparece** no menu de relações do `permanent_note.md`, e um teste fixa essa ausência. Dois `source_id` diferentes são um fato sobre autoria. "Mesma ideia" é um julgamento determinístico pela similaridade — o #226 mediu o erro dele — então a aresta pode ser escrita direto enquanto for recalculada quando o corte muda. Oferecida ao modelo, ela seria emitida retoricamente ("esta nota também concorda") e o sinal se perderia dentro de `supports`. Um `corroborates` emitido pelo LLM é rebaixado para `supports` na fronteira de confiança, antes da injeção — as arestas que o código afirma sobrevivem. Mesmo padrão do [ADR-034](adrs/generated/EXTRACT/ADR-034-optional-author-judgement-fields.md) e um carve-out deliberado do [ADR-043](adrs/generated/RETRIEVAL/ADR-043-distant-analogies-as-suggestions.md), que barra o *julgamento* do LLM.

O corte 0,76 foi medido com `gemini-embedding-2@1536` no lado consulta. `scripts/rebuild_corroborates.py` refaz as arestas `corroborates` já gravadas com a mesma função do connect (consulta nova de tese + definição; o padrão é dry-run, `--apply` grava). Uma nota sem tese e sem definição fica de fora.

Seu peso no grafo é **0.45**, abaixo de `related` — ver [recuperacao.md](recuperacao.md).

Valores de `related_note_id` são canonicalizados (removendo `ZTL -` e wrappers de wikilink até sobrar o ULID) e **descartados** se o alvo não existir no SQLite ou o arquivo tiver sumido — nada de wikilinks fantasma `[[ZTL - ZTL - ULID]]`. O bloco `auto-backlinks` é **reconstruído** a partir das arestas de entrada em `note_connections` (stem do arquivo atual + relação inversa), nunca apenas concatenado. Conexões cujo alvo veio do grupo de analogias distantes vão para `auto-connections`, não para o grafo.

Quando uma nota permanente entra ou sai de um MOC, o pipeline atualiza o bloco **`auto-moc-backrefs`** na ZTL (ver Fase 4 e [notas-manuais.md](notas-manuais.md)).

---

## Fase 4 — Garden (Jardim)

Módulos: [`gardener.py`](../zettel/gardener.py), [`gardener_assign.py`](../zettel/gardener_assign.py), [`moc_backrefs.py`](../zettel/moc_backrefs.py).

Pipeline **híbrido** (taxonomia → cluster por categoria → grafo → roteamento LLM) — [ADR-019](adrs/generated/GARDEN/ADR-019-taxonomy-first-moc-clustering.md) e [ADR-021](adrs/generated/GARDEN/ADR-021-single-llm-call-per-cluster-routing.md).

**MOCs e hubs são portas de entrada, não índices completos** ([ADR-060](adrs/generated/GARDEN/ADR-060-mocs-and-hubs-are-entry-points.md)): cada MOC lista poucas notas, as melhores para começar, com uma linha de orientação para cada uma, e o leitor chega às demais pelo grafo. Notas fora de qualquer MOC são esperadas; o que indica problema é um assunto sem porta ou uma alcançabilidade baixa (relatório da execução).

1. Carrega embeddings de todas as notas permanentes
2. **Atribuição taxonomia-first** (`gardener_assign.py`): embedda labels `{pilar}: {categoria}` de `config/moc_topics.yaml` e agrupa cada nota no bucket de maior similaridade. Nomes de categoria precisam ser **globalmente únicos**.
3. **Clusterização por bucket**: UMAP + HDBSCAN (ou KMeans como fallback) **dentro de cada categoria**, alinhando clusters ao guarda-chuva da taxonomia. A semente `random_state` torna a partição reprodutível, e o HDBSCAN usa `max(min_cluster_size, min_notes_for_moc)` como tamanho mínimo, para não aceitar um cluster que não poderia virar MOC. O log informa o backend usado (`umap+hdbscan`, `kmeans` ou `single`), o ruído e os buckets pequenos
4. **Seleção das notas de entrada** (`garden_entries.select_entry_notes`): de cada cluster, as `max_entries_per_moc` (10) notas mais bem colocadas pelo sinal `entry_selection` (`internal_degree`, padrão provisório; ou `medoid`, `first_in_source`, `relevance`, `corroboration`). O roteamento usa o cluster inteiro; o MOC recebe só as entradas
5. Extrai termos representativos via **TF-IDF**
6. **Roteamento por assunto** (`plan_subject` / `_process_subject`, #296). Cada categoria é um **assunto**, e todo assunto com notas suficientes ganha pelo menos uma porta. Primeiro, cada cluster da categoria:
   - Assinatura idêntica → skip (sem LLM)
   - Overlap com um MOC do pipeline (a fração das entradas de **uma subseção** do MOC que cai no cluster ≥ `overlap_threshold`) → `moc_incremental` apenas
   - Coesão de grafo abaixo de `graph_cohesion_min_ratio` (se > 0) → cluster rejeitado, sem MOC novo
   - Caso contrário o cluster é **novo**. O nome da categoria não é mais uma rota por cluster: ele mandava todo sub-cluster novo para o primeiro MOC da categoria.

   Depois, os clusters novos da categoria, juntos:
   - A categoria ainda não tem MOC e `k * min_entries_per_subtopic <= max_entries_per_moc` → **um MOC** com uma subseção por cluster (`max_entries_per_moc // k` entradas de cada), numa chamada de `moc_generation`
   - A categoria ainda não tem MOC e os clusters não cabem → **MOC de MOCs**: um sub-MOC "Categoria — subtema" por cluster (uma chamada cada) e o MOC da categoria, que lista só os sub-MOCs na seção "Subtemas", com a primeira frase do resumo de cada um como orientação (mais uma chamada, para o resumo)
   - A categoria já tem MOC → cada cluster novo entra nele como entradas enquanto o MOC só lista notas e tem espaço para `min_entries_per_subtopic`; senão, ou se o LLM não posicionar nenhuma nota do cluster, o cluster vira um sub-MOC listado no MOC da categoria. É o único caso de duas chamadas para um cluster
7. **Validação de tópico** pós-geração:
   - Substring match bidirecional contra os nomes das **categorias** do YAML
   - Se `strict_topics: true` e sem match: MOC rejeitado (com warning no log)
   - Se `strict_topics: false` e sem match: MOC aprovado (com info no log)
8. **Atualização incremental**: oferece ao MOC só as entradas que cabem no teto (um MOC cheio não recebe nada e não gasta chamada); o LLM encaixa cada uma numa subseção existente ou nova, com a orientação, ou responde `ignorar`. O `ignorar` fica gravado em `moc_discards` (nota, motivo, checksum semântico da nota, checksum do prompt, modelo): a nota não é oferecida de novo àquele MOC enquanto não mudar, ou até `zettel garden --reoffer-discarded`. O arquivo e a assinatura do MOC só mudam quando as entradas mudam. Uma entrada que o LLM não posiciona fica registrada como omitida, não é listada (não existe mais a seção "Outras notas do cluster"). Cada subseção tem no máximo `max_entries_per_subsection` (5) entradas. Quando as entradas crescem `summary_refresh_growth` (50%), o resumo é reescrito (`moc_summary_refresh.md`)
9. Notas fora de MOC (ruído do HDBSCAN ou fora da seleção) permanecem no vault e são navegáveis via grafo/conexões — não existe fila de órfãs

Parâmetros híbridos em `config.yaml` (`gardener.*`):

| Parametro | Proposito |
|-----------|-----------|
| `cluster_within_category` | Ativa pipeline taxonomia-first (default `true`) |
| `category_label_template` | Texto embeddavel por categoria (default `"{pilar}: {categoria}"`; `domain` continua disponível num template customizado) |
| `overlap_threshold` | Fração das entradas de uma subseção do MOC que cai no cluster → update incremental direto |
| `graph_cohesion_enabled` | Calcula score interno do cluster via `note_connections` |
| `graph_cohesion_min_ratio` | `0` = so log; `>0` rejeita cluster fraco antes de criar MOC novo |
| `umap_n_neighbors` | `null` = auto |
| `hdbscan_min_samples` | Opcional; ajuste fino do HDBSCAN |
| `entry_selection` | Sinal que escolhe as notas de entrada (`internal_degree` provisório; medição pré-registrada em `evals/preregistration/297-portas-de-entrada.md`) |
| `max_entries_per_moc` / `max_entries_per_subsection` | Teto de entradas por MOC (10, contando o hub nos MOCs hub) e por subseção (5) |
| `min_entries_per_subtopic` | Entradas mínimas de cada sub-cluster num MOC compartilhado (3): `k` sub-clusters cabem num MOC só enquanto `k * 3 <= max_entries_per_moc`; acima disso, MOC de MOCs |
| `summary_refresh_growth` | Fração de crescimento das entradas que dispara a reescrita do resumo (0,5) |
| `random_state` | Semente do UMAP (e do KMeans de fallback). Fixa (`42`) = mesma partição para os mesmos vetores, o que permite reaproveitar MOCs por assinatura; `null` = mais rápido e não determinístico |

`zettel garden --recreate` apaga os MOCs gerados pelo pipeline (`origin='pipeline'`) e regenera do zero, preservando MOCs manuais. Um arquivo cujo frontmatter diga outra origem nunca é apagado. A taxonomia, o cliente de LLM e o embedding dos rótulos são conferidos **antes** da purga: se o provedor estiver fora, o comando para com código 1 sem apagar nada (ADR-059). É **obrigatório** depois de mudar o template de rótulo ou a taxonomia — os MOCs existentes ficam desalinhados. Antes de apagar cada MOC, **`clear_moc_backrefs`** remove os links dele dos blocos `auto-moc-backrefs` das notas permanentes. Ao criar ou atualizar um MOC, **`sync_moc_backrefs`** adiciona/remove wikilinks do MOC nas ZTL listadas no corpo do mapa.

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
