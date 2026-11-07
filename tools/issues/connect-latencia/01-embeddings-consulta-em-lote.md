## Resumo

Calcular em lote, no início do connect, os embeddings da consulta de todos os conceitos da fila. As buscas de cada nota passam a usar vetores já prontos, e o mesmo texto deixa de ir ao Gemini três a cinco vezes por nota.

## Motivação

Run 12 (2026-11-07): `@Albada2025BuildingApplications`, 304 conceitos, uma fonte só, de modo que o paralelismo por fonte (#356) não entra. Ritmo medido de **27 s por nota**. O thinking já está desligado (#355): 1.164 tokens de saída por nota.

Distribuição do tempo, pelos timestamps do log e por medição local numa cópia do `state.db`:

| Etapa | Tempo por nota |
|---|---|
| Chamada ao Claude (Prompt 2) | ~18 s |
| Embeddings Gemini (4 a 5 chamadas de ~1 s) | ~5–6 s |
| Escrita, arestas, backlinks | ~1 s |
| BFS do grafo (5 saltos, 16 vizinhos), citação, `note_content` | < 0,1 s (medido: BFS 33 ms, citação 11 ms) |
| Shadow do Jev | desprezível (2 chamadas em 32 notas) |

O texto da consulta é `f"{cand.thesis} {cand.definition}"` (`connector/note.py`, `prepare_candidate`). Ele entra nestas chamadas:

| # | Log | Caminho | Lado |
|---|---|---|---|
| 1 | `busca densa (20 notas)` | `Retriever.search_notes` → `_vector_notes` → `VectorIndex.query_similar_notes(query_texts=[texto])` | consulta (`RETRIEVAL_QUERY`) |
| 2 | — | reforço do índice de tópicos: `_add_topic_index_seeds` → `query_notes_by_ids(texto, ids)`, só quando um termo casa | consulta |
| 3 | `analogia-categoria` | `context.search_distant_analogies` → `idx.embed_texts([texto])`, que compara com os rótulos da taxonomia | documento |
| 4 | `busca analogias (60 notas)` | `Retriever.search_distant_analogies` → `search_notes` → `query_similar_notes(texto)` | consulta |
| 5 | — | reforço do índice de tópicos de novo, dentro do item 4 | consulta |
| 6 | `index ZTL ...` | `upsert_permanent_note` da nota escrita | documento |

As chamadas 1, 2, 4 e 5 produzem **o mesmo vetor**. A 3 usa o mesmo texto no lado documento, que é o mesmo lado dos rótulos das categorias e por isso tem de continuar separada. A busca depende das notas já escritas, mas o vetor da consulta não depende: a tese e a definição de todos os conceitos estão prontas desde o review.

Além disso, `_LangChainChromaEF.embed_query` faz uma requisição por texto, e o lado consulta nunca é agrupado.

## Contrato / comportamento

- `VectorIndex.prime_query_embeddings(texts)` calcula em lote os vetores dos dois lados de cada texto distinto:
  - lado consulta: `embed_queries` do EF; o Gemini usa `embed_documents(texts, task_type="RETRIEVAL_QUERY")`, em lotes de até 100;
  - lado documento: `embedding_fn(texts)`.
  
  Os vetores ficam num cache por texto. O uso é registrado uma vez por texto e por lado no `CostTracker`.
- `query_similar_notes` e `query_notes_by_ids` usam `query_embeddings` quando o texto está no cache, e `query_texts` quando não está. `embed_texts` também consulta o cache, no lado documento. Um acerto de cache não registra uso de novo.
- `VectorIndex.clear_query_embeddings()` esvazia o cache.
- `run_connect`:
  - depois de montar a `ConnectSession`, chama `prime_query_embeddings` com os textos de consulta da fila;
  - no `finally`, chama `clear_query_embeddings`.
- O vetor é **idêntico** ao que a chamada avulsa daria (mesmo texto, mesmo modelo, mesmo `task_type`, mesma normalização). Buscas, contexto, arestas e notas não mudam.
- Falha: uma indisponibilidade no lote levanta `LLMUnavailableError` antes da primeira nota, como faria a primeira busca de hoje.
- `FakeVectorIndex` (`tests/fakes.py`) implementa os dois métodos novos com a assinatura real (`tests/test_fakes_contract.py`).

## Arquivos a tocar

- `zettel/index.py`: `_LangChainChromaEF.embed_queries`, `_GeminiChromaEF.embed_queries`, cache e os três métodos de busca/embedding.
- `zettel/connector/run.py`
- `tests/fakes.py`, `tests/test_index*.py`, `tests/test_connect*.py`
- CLAUDE.md (Phase 3 e `index.py`)

## Fora de escopo

- O embedding da ZTL escrita (6), que só existe depois do Claude.
- Paralelizar dentro de uma fonte (perderia arestas, ver #356).
- O dedupe do review e o `sync`, que fazem uma busca por item e ganhariam pouco.

## Critérios de aceite

- [ ] Num connect de N conceitos, as chamadas de embedding da consulta caem de ~3–5 N para ⌈N/100⌉ por lado.
- [ ] Os mesmos hits, distâncias e arestas que no caminho sem cache (teste com o embedding offline `token_hash_embedding`).
- [ ] O uso de embedding no `run_usage` conta cada texto uma vez por lado.
- [ ] Uma busca com um texto fora do cache continua funcionando.

## Testes

- Em `VectorIndex` real (`token_hash_embedding`): os resultados com o cache são iguais aos sem cache, e há uma chamada de embedding por lado no lote.
- `run_connect` com `FakeVectorIndex`: o prime é chamado com os textos da fila e o cache é esvaziado no fim, inclusive quando há falha.

## Dependências

Nenhuma.
