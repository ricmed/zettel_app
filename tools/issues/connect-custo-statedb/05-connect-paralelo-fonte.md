## Resumo

Rodar o connect de fontes diferentes em paralelo, mantendo a ordem sequencial dentro de cada fonte. Só a chamada HTTP do Prompt 2 sai da thread principal.

## Motivação

O último connect (2 livros, 1.216 ZTL) levou **9h25**, ~27 s por nota, com ritmo estável. Não há custo O(N²): o tempo é a latência de uma chamada LLM por vez. A saída de cada nota precisa estar indexada para servir de contexto RAG à seguinte, e por isso o loop é sequencial.

Medição de localidade das 3.120 arestas LLM, por ordem de criação:

| Alvo da aresta | Fração |
|---|---|
| A nota imediatamente anterior | 17% |
| Uma das 2 anteriores | 27% |
| Uma das 4 anteriores | 38% |
| Uma das 8 anteriores | 48% |
| **Outra fonte** | **4,9%** (153) |

Paralelizar notas vizinhas da mesma fonte perderia até ~38% das ligações. Paralelizar **entre fontes** preserva todo o contexto intra-fonte e afeta no máximo as arestas entre fontes, que além disso passam a poder ir nos dois sentidos. O ganho é proporcional ao número de fontes no run: ~2x neste.

## Contrato / comportamento

- Novo knob `linking.connect_parallel_sources` (padrão 4; `1` = comportamento atual).
- `run_connect` agrupa a fila por `source_id`, mantendo a ordem atual (capítulo, `chunk_index`), com **no máximo uma chamada em voo por fonte**.
- Na thread principal ficam: SQLite (`StateDB` é uma conexão `check_same_thread`), Chroma, embeddings, vault e backlinks.
- No `ThreadPoolExecutor` fica só o HTTP do Prompt 2, rodado via `contextvars.copy_context().run` para o `CostTracker`, que ganha lock.
- Uma fonte prepara a nota seguinte só depois de gravar a anterior. A visibilidade dentro da fonte é idêntica à do modo sequencial.
- `generate_permanent_note` é dividido em preparar (checksum e consulta ao cache), chamar e finalizar (gravar cache, parse, guard PT-BR). Um acerto de cache pula a chamada. `cached_call_llm` ganha as duas metades reutilizáveis.
- O modo `split` continua sequencial.
- Progresso, contagens, `ConnectRejected`, `ConnectCandidateFailed` e `LLMUnavailableError` mantêm a semântica. `LLMUnavailableError` cancela as chamadas pendentes e propaga.

## Arquivos a tocar

- `zettel/connector/run.py`, `zettel/connector/note.py`, `zettel/connector/prompt.py`
- `zettel/llm.py`, `zettel/usage.py`
- `zettel/config.py`, `config/config.yaml`
- `tests/test_connector*.py`

## Fora de escopo

- Paralelismo dentro de uma fonte.
- Message Batches API, que barateia mas não acelera.

## Critérios de aceite

- [ ] Com 2 fontes, as notas e as arestas intra-fonte são as mesmas do modo sequencial.
- [ ] Os totais do tracker conferem.
- [ ] `connect_parallel_sources: 1` reproduz o comportamento atual.
- [ ] Uma falha de provedor interrompe as duas fontes sem nota pela metade.

## Dependências

- Lock no `CostTracker` (issue de custo por provedor).
