# Recuperação: busca híbrida + GraphRAG leve

[← Voltar ao README](../README.md)

Como o sistema encontra as notas que alimentam o RAG do `connect`, as sugestões do `sync-manual` / `suggest-links`, o comando `ask` e o `article` — e por que existe um piso absoluto de relevância além do ranking.

Módulos: [`retrieval.py`](../zettel/retrieval.py), [`graph.py`](../zettel/graph.py), [`ask.py`](../zettel/ask.py), [`article.py`](../zettel/article.py) / [`article_graph/`](../zettel/article_graph/) (pacote — runtime, search, nodes, graph).

---

## Os três sinais

A recuperação de notas combina três sinais complementares ([ADR-003](adrs/generated/INFRA/ADR-003-hybrid-dense-bm25-retrieval.md)):

1. **Busca vetorial (densa)** — similaridade semântica no ChromaDB.
2. **Busca lexical BM25** — índice full-text **SQLite FTS5** no próprio `state.db` (tokenizer `unicode61` com `remove_diacritics`, então "conexao" casa "conexão"). Cobre o ponto fraco do embedding: termos técnicos exatos, siglas e nomes próprios. Palavras funcionais de altíssima frequência em PT-BR (artigos, preposições, conjunções — ex. "que", "de", "para") são filtradas da consulta antes do MATCH: sem isso, uma palavra como "que" aparece em quase toda nota do acervo e o "match" lexical deixa de significar qualquer coisa.
3. **Expansão por grafo (GraphRAG leve)** — as **conexões tipadas** já geradas pelo pipeline (tabela `note_connections`: `supports`, `contradicts`, `extends`, `depends_on`, `exemplifies`, `related`, `corroborates`) são percorridas a partir das notas recuperadas. Vizinhos entram no contexto ponderados por tipo de relação (`contradicts`/`extends` pesam mais — trazem informação que a similaridade vetorial **não** captura) e por decaimento por salto ([ADR-009](adrs/generated/RETRIEVAL/ADR-009-graph-based-note-discovery-weighted-bfs.md)). Uma aresta que o autor escreveu no corpo (`origin=manual`) usa o peso `manual` (0.95), não o de `related` (0.5). Já `corroborates` pesa **0.45**, abaixo de `related`, de propósito: o peso governa **travessia**, não importância — um clique de notas corroborantes é o que o pesquisador quer ler e a pior coisa em que gastar as vagas de `max_neighbors`, porque cada salto desses devolve uma paráfrase em vez de informação nova. A relação ganha destaque na renderização (`## Conexões` / `auto-backlinks`), não na expansão.

As listas densa e lexical são fundidas por **Reciprocal Rank Fusion (RRF)**, que usa apenas o *ranking* de cada id (não os scores brutos), dispensando calibração entre escalas incompatíveis (distância L2 vs. bm25). Os ids são compartilhados entre Chroma e SQLite, então a fusão é direta.

**Manter as duas abordagens**: `retrieval.mode: vector` restaura o comportamento histórico (só Chroma); `hybrid` (padrão) ativa a fusão. `graph_expansion.enabled: false` desliga o grafo. Se o SQLite não tiver FTS5, o sistema **degrada automaticamente** para busca vetorial pura (com aviso). Rode `zettel doctor` para conferir a disponibilidade de FTS5.

> **Nota de calibração**: a deduplicação semântica (`extract`) e a detecção de duplicatas do `harvest` **não** usam a busca híbrida — seus limiares (`dedupe_threshold`, `duplicate_chunk_threshold`) são calibrados sobre a distância vetorial crua e permanecem no vetor puro.

---

## `hits` vs `candidates`

`search_notes()` devolve um `NoteSearchResult` com duas listas ([ADR-010](adrs/generated/RETRIEVAL/ADR-010-retrieval-result-transparency-hits-vs-candidates.md)):

- **`hits`** — o que passou do piso de relevância (mais os vizinhos de grafo, `hop >= 1`). É isto que o chamador deve usar como evidência.
- **`candidates`** — o pool bruto ranqueado por RRF, *antes* do piso, sempre populado. Permite mostrar "o que chegou mais perto" mesmo quando `hits` está vazio.

Cada `RetrievedNote` carrega a proveniência: `vector_rank`, `bm25_rank`, `hop`, `via`, `passed_floor` e um `floor_reason` legível.

Consumidores migrados para o `Retriever`: RAG do connector (`.hits`), analogias distantes do `connect` (`search_distant_analogies`), sugestões do sync e do `suggest-links` (`.hits`), o comando `ask` (`.hits` + `.candidates`) e o `article` (`.hits` + catálogo).

---

## Piso de relevância absoluto

O score do RRF é **posicional**, não uma medida absoluta de relevância: a busca vetorial (kNN) sempre devolve os N vizinhos mais próximos disponíveis no corpus, mesmo que nenhum seja de fato relevante — então uma pergunta totalmente fora do acervo recebe um score no mesmo patamar de uma pergunta genuinamente respondível. `retrieval.relevance_floor` corrige isso aplicando três verificações, **nesta ordem**, por nota recuperada:

1. **Piso rígido** (`absolute_min_similarity`, padrão `0.15`): se a similaridade coseno (derivada da distância vetorial) estiver abaixo desse valor, a nota é **sempre** rejeitada — nem um match lexical (BM25) a salva. É uma proteção contra o caso patológico de uma nota semanticamente quase ortogonal que por acaso compartilha um termo com a pergunta. É deliberadamente bem mais baixo que o piso normal, para não atrapalhar o caso de uso principal do BM25 (siglas/termos técnicos que o embedding às vezes subestima, mas cuja similaridade raramente é próxima de zero).
2. **Bypass por match lexical forte** (`bm25_hit_bypasses_floor: true`): a nota passa direto, **independente** da similaridade vetorial, se satisfizer **duas** condições — posição ≤ `bm25_bypass_max_rank` (padrão `5`) **e** cobertura ≥ `bm25_bypass_min_coverage` (padrão `0.5`). Rank é um critério **relativo** ("foi bem entre os que casaram"); cobertura é o **absoluto**: quanto dos termos da pergunta a nota realmente contém. Falhar em qualquer um dos dois não rejeita a nota — ela apenas cai para a checagem normal de similaridade.
3. **Piso normal de similaridade** (`min_vector_similarity`, padrão `0.70`, calibrado empiricamente neste projeto — ajuste para seu corpus/modelo de embedding): critério padrão para notas sem match lexical forte.

Um hit apenas lexical e fraco, sem nenhum dado vetorial, também falha (evidência insuficiente dos dois lados).

Resultados abaixo do piso não alimentam a resposta, mas continuam visíveis em `--show-context` para fins de transparência/depuração — junto com o **motivo exato** da decisão (ex.: `similaridade 0.67 abaixo do piso (0.70)` ou `match lexical forte (bm25 rank 3 <= 5)`).

> O `bm25_bypass_max_rank` corrigiu um bug real em produção: antes dele, **qualquer** presença no BM25 dispensava o piso, então uma nota que casava só incidentalmente com um termo comum do domínio passava com similaridade baixa.

> **A cobertura fechou o resto desse buraco (2026-09-09, addendum da ADR-003).** O rank sozinho não bastava: o FTS junta os termos da pergunta com `OR`, então a nota entra no resultado por casar **um** deles, e quando o pool de match é menor que o corte, "top 5" quer dizer "todos". Medido neste acervo, `"como fazer risoto de cogumelos"` devolvia 4 notas, todas resgatadas pela palavra *fazer*, com similaridade 0.58–0.64 — abaixo do piso de 0.70. Com o gate de cobertura, o ruído fora do domínio caiu de 31 para 1 hit **sem perder nenhum hit dentro do domínio**.
>
> A cobertura foi escolhida no lugar de "casar pelo menos N termos" justamente para preservar o caso de uso do bypass: uma consulta de uma palavra só (`ARIMA`, `sazonalidade`) casa 1 termo, mas com cobertura `1.00` — um mínimo absoluto de 2 termos tornaria o bypass inalcançável para siglas, que é exatamente o que ele existe para resgatar.
>
> `bm25_bypass_min_coverage: 0.0` restaura o comportamento antigo.

---

## Catálogo: quais fontes tratam de um assunto (`zettel catalog`)

`ask` responde "que **ideia** responde a isso?". `catalog` responde "que
**material** trata disso?" — devolve a fonte, os capítulos relevantes e quantas
notas permanentes cada capítulo gerou. **Não chama LLM**: a resposta é uma tabela
ranqueada mais um agregado SQL (`COUNT(DISTINCT note_id)`), e pedir isso a um
modelo seria pedir que ele enumerasse uma contagem que pode alucinar.

Dois sinais, fundidos no nível do capítulo, cada linha declarando qual a
encontrou:

| Sinal | De onde vem | Cobre |
|---|---|---|
| **notas** | os hits de `search_notes` subidos por `note → conceito → chunk → capítulo` | o capítulo que virou nota permanente — o sinal mais forte, já passado pelo portão humano |
| **resumo** | `search_chapter_summaries` sobre os resumos de capítulo | o capítulo que **não** virou nota nenhuma (rendimento zero, ou fonte ainda sem `connect`) |

Duas assimetrias deliberadas nesse caminho:

- **Sem expansão por grafo.** Em `search_notes` um vizinho entra em `hits` pela
  relevância da semente que o puxou, não pela sua própria — certo para RAG,
  errado aqui: o vizinho trata de algo *relacionado* à pergunta, o que não diz
  nada sobre o capítulo dele. Só sementes `hop == 0` contam.
- **Um capítulo achado pelas notas não enfrenta o piso de capítulo.** A evidência
  dele é uma nota que já passou pelo piso de notas.

### O piso de capítulo é outro, e ainda não está calibrado

`retrieval.chapter_floor` é um `RelevanceFloorConfig` **separado** de
`retrieval.relevance_floor`. Herdar o `0.70` seria erro de categoria: aquele
número foi medido sobre cosenos *nota × pergunta*, e um resumo de capítulo é um
texto mais longo e mais difuso — pontua mais baixo para a mesma pergunta, então o
piso de nota rejeitaria demais.

Medido em 2026-09-07 sobre 71 capítulos resumidos
(`ollama/qwen3-embedding@1024d`):

| banda | n | min | mediana | max |
|---|---|---|---|---|
| fora-domínio | 6 | 0.676 | 0.685 | **0.693** |
| self-match | 20 | **0.745** | 0.821 | 0.883 |

O piso ficou em **`0.70`** — acima de tudo que foi medido fora do domínio, e
0.045 abaixo da recuperação mais fácil que existe. O `0.60` inicial deixava as
seis consultas fora do domínio passarem; com `0.70` elas devolvem zero capítulos
pelo sinal de resumo.

Que esse `0.70` coincida com o piso de notas é **duas medições independentes
concordando, não herança** — são objetos de config separados, e mexer num não
mexe no outro.

> A margem é estreita (0.052). Alguns capítulos fora do assunto ficam logo acima
> da linha (observado: 0.707–0.723 para capítulos de uma fonte não relacionada).
> Subir até o teto de `0.72` começaria a custar recall real — o próprio probe
> avisa. Re-meça com
> `scripts/probe_relevance_floor.py --collection chapter_summaries` depois de
> qualquer troca de modelo de embedding: o número não transfere.

Um capítulo barrado continua visível em `--show-context` com o motivo, então um
piso alto demais aparece em vez de sumir calado — o mesmo contrato de
`hits`/`candidates` do `ask`.

### O `catalog` não tem triagem de LLM para compensar o piso de notas

O sinal de **notas** herda o `search_notes` inteiro, inclusive as fraquezas dele.
O `ask` absorve ruído do piso porque o `prompts/ask.md` manda o modelo triar o
contexto e descartar o irrelevante ("estar no contexto não é motivo para usar").
O `catalog`, que por decisão não chama LLM nenhum, imprime o que o piso entregou.

Isso fez do `catalog` o canário do piso compartilhado: foi ele que expôs o bypass
do BM25 aceitando notas fora do domínio por uma única palavra em comum
(`"como fazer risoto de cogumelos"` devolvia quatro notas resgatadas por *fazer*,
com similaridade 0.58–0.64). **Corrigido em 2026-09-09** no próprio piso, com o
gate de cobertura descrito acima — 31 hits fora do domínio viraram 1, sem perder
nada dentro do domínio.

Na prática, ao avaliar um resultado estranho: olhe a coluna "Achado por". Linhas
só de **notas**, sem similaridade, numa fonte obviamente alheia ao assunto, são o
sintoma de que o piso deixou passar algo.

---

## Índice de tópicos (roteamento)

`sync_topic_index` ([`topic_index.py`](../zettel/topic_index.py)) mantém um mapa **termo → nota** em duas superfícies: um bloco gerenciado `auto-topic-index` no índice LIT de cada fonte e em cada MOC (para você e para um agente ler), espelhado numa tabela `topic_index_terms` no SQLite (para o `ask` consultar sem parsear Markdown). Os termos saem de `named_frameworks` (vocabulário do autor), depois tags, e só caem na cabeça da tese quando a nota não tem nenhum dos dois — a mesma regra que o `zettel skill` usa, para os dois índices não divergirem.

**Roteamento não é representação.** O índice é uma *dica de onde olhar*, não um veredito de relevância:

- Só alvos que são **notas permanentes** roteiam. Um alvo LIT aparece no bloco (leva o leitor à nota granular certa) mas é gravado sem `note_id` e nunca vira semente — o Retriever pontua ZTL, não LIT.
- Quando a pergunta contém um termo indexado, a nota roteada é buscada no Chroma com o espaço de busca **restrito ao id dela** (`query_notes_by_ids`), então chega com **distância real** e enfrenta o mesmo `_apply_relevance_floor` que qualquer outro candidato. Estar no índice **não** fura o piso — seria o mesmo bug do bypass incondicional do BM25, com outro nome.
- `retrieval.topic_index_boost: false` restaura o comportamento anterior. `topic_index_max_seeds` limita quantas notas uma pergunta pode puxar por essa via.
- Uma nota que chegou assim aparece com origem `topic index` no `--show-context`.

Custo: **uma** chamada extra de embedding da pergunta, e só quando algum termo casa.

O índice se materializa no `review` (fonte) e no `garden`/`sync-manual` (MOC). Um vault já maduro pega tudo de uma vez com `zettel reindex`, que refaz também esse índice (como já faz com o FTS5). Decisão e alternativas recusadas: [ADR-036](adrs/generated/RETRIEVAL/ADR-036-topic-index-routing-not-representation.md).

---

## Expansão por grafo

`expand_notes` ([`graph.py`](../zettel/graph.py)) faz BFS em Python (não CTE recursiva em SQL) sobre `note_connections`, de forma não-direcionada, com peso por tipo de relação (`DEFAULT_RELATION_WEIGHTS` em `config.py`; `contradicts` no topo — é o sinal que embeddings não capturam) e decaimento por salto. Arestas com `origin=manual` usam o peso `manual` (0.95). As sementes entram com o próprio score RRF (`seed_weights`), e cada fronteira faz **uma** consulta em lote (`StateDB.get_connections_for_notes`).

Só sementes que **passaram do piso** alimentam a expansão — evita ampliar ruído.

---

## FTS5 no `state.py`

As tabelas virtuais `fts_notes` / `fts_chunks` (`unicode61 remove_diacritics`) são mantidas em sincronia dentro de `upsert_note` / `upsert_chunk` — populadas explicitamente, não por triggers.

`_fts_match_expr` cita cada token para neutralizar operadores do FTS5 (**nunca** interpole texto do usuário direto num MATCH) e remove os stopwords PT-BR de alta frequência (`_PT_STOPWORDS`) antes de montar a expressão OR. `rebuild_fts()` é acionado pelo `zettel reindex`.

---

## Perguntar ao acervo (`zettel ask`)

```bash
python -m zettel ask "Como heurísticas geram vieses?" --show-context
```

O comando recupera as notas relevantes (híbrido + grafo + piso de relevância), monta um contexto com citações e pede ao LLM uma resposta em PT-BR **baseada apenas no acervo** (se não houver evidência, ele diz isso, em vez de alucinar). Cada afirmação cita o `[[wikilink]]` exato da nota-fonte. Quando nenhuma nota recuperada passa do piso de relevância, o LLM **nem chega a ser chamado** — a resposta padrão de "não encontrei evidência" é determinística.

Com `--show-context`, o comando mostra dois relatórios extras:

- **Parâmetros de recuperação**: todos os valores configurados usados naquela consulta (modo, top-k, `rrf_k`, os três limiares do piso de relevância, e os parâmetros de expansão por grafo) — para você conferir exatamente sob quais regras a recuperação rodou, sem precisar abrir o `config.yaml`.
- **Notas recuperadas**: o top-k bruto, com Score RRF, Similaridade, Rank BM25 (posição no ranking lexical, ou "-" se não casou), Salto (0 = achada na busca, ≥1 = vizinha de grafo), se foi **usada** e o **motivo** exato da decisão (ex.: `match lexical forte (bm25 rank 3 <= 5)` ou `similaridade 0.67 abaixo do piso (0.70)`) — para auditar a recuperação mesmo quando a resposta é negativa.

A resposta pode ser salva como nota `.md` em `00_Inbox/` (`--save` ou `--save-to`), com frontmatter e uma seção **Fontes consultadas** que registra, para cada nota efetivamente usada, o wikilink, a origem na recuperação (busca vs. conexão de grafo, com o tipo da relação), o score e a fonte bibliográfica — rastreabilidade completa de onde veio cada informação.

O `ask` usa o mesmo cache determinístico de LLM do connector (`compute_llm_call_checksum`).

Flags completas em [cli.md](cli.md#ask).

---

## Gerar artigo a partir do vault (`zettel article`)

```bash
python -m zettel article "Tecnicas de Prompt Engineering" --style blog
python -m zettel article "Grafos de conhecimento e LLMs" --style academic --personality serious_academic --save
python -m zettel article "RAG" --outline-only
```

Orquestrado por **LangGraph** ([`article_graph/`](../zettel/article_graph/), pacote per [ADR-029](adrs/generated/QA-WRITING/ADR-029-article-graph-as-python-package.md)), diferente do `ask` (QA curto) — [ADR-028](adrs/generated/QA-WRITING/ADR-028-langgraph-stategraph-article-orchestration.md). Fluxo:

1. **Query enricher** — expande o tema em várias queries semânticas
2. **Busca acumulativa** — Retriever híbrido + merge por `note_id` (queries extras do usuário somam, não substituem)
3. **HITL de contexto** — aprovar o pool, pedir queries extras (`e`) ou abortar
4. **Outline** — LLM + aprovação interativa (`a` / `r`+feedback / `q`)
5. **Draft por seção** — blog (menções leves) ou acadêmico (ABNT autor-data) + anti-padrões de prosa
6. **Assemble** — figuras, "Para saber mais" ou "Referências" ABNT, "Origem no vault"
7. **Personality** — reescrita estilística via `config/personalities.yaml` (`neutral` = no-op)
8. **Judge** — avalia fidelidade/cobertura/refs/naturalidade; rejeição re-drafta até `max_judge_iterations`

Flags: `--personality`, `--style-notes`, `--skip-context-review`, `--skip-judge`, `--max-judge-iterations`, `--outline-only`, `--save` / `--save-to`. Saída em `00_Inbox/ART - ....md` (não indexa no Chroma).

O HITL usa `interrupt()` do LangGraph com prompts Rich; o checkpointer é um `MemorySaver` por execução. Reusa o `Retriever`, o `format_abnt_in_text` e o cache de LLM.

---

## Analogias distantes (outro domínio)

O `connect` (e o `suggest-links`) faz uma busca secundária pequena, com piso **local** (`linking.distant_analogy_min_similarity`, default 0.40), restrita a notas **fora** do bucket taxonômico do candidato. `retrieval.relevance_floor` não muda — o remédio do `connect` não pode virar doença no `ask`.

Essas notas entram no Prompt 2 como o terceiro grupo do RAG (`### Analogias distantes`). O critério é **mecanismo que transfere**, não vocabulário compartilhado. O resultado vai para `auto-connections`, não para `note_connections`: uma analogia especulativa só vira aresta quando o autor move o wikilink para a prosa.

---

## Fechando o ciclo do grafo (notas manuais)

Notas escritas à mão no Obsidian também alimentam o grafo: no `sync-manual`, os `[[wikilinks]]` presentes **no corpo** de uma nota permanente (fora dos blocos gerenciados `auto-connections`, `auto-backlinks` e `auto-moc-backrefs`, que são gerados automaticamente) são persistidos como arestas `related` com `origin=manual`. Uma aresta já tipada nunca é rebaixada. Use `zettel sync-manual --rebuild-graph` para re-derivar essas arestas de todo o vault a partir dos corpos já persistidos no SQLite.

MOCs manuais ou editados no Obsidian também disparam **`sync_moc_backrefs`**: notas permanentes linkadas no corpo do MOC ganham (ou perdem) entradas no bloco `auto-moc-backrefs`.

---

## Ver também

- [Configuração](configuracao.md) — o bloco `retrieval.*` completo
- [Comandos](cli.md#ask) — flags de `ask` e `article`
- [Notas manuais](notas-manuais.md) — como notas escritas à mão entram na recuperação (`suggest-links` sem reescrita)
- [Pipeline](pipeline.md#fase-3--connect-conexao) — onde o RAG é usado na geração de notas
