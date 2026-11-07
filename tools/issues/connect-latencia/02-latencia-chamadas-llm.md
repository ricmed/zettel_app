## Resumo

Medir e registrar a duração de cada chamada ao LLM: na linha `llm` do log e, por nota, em `notes.provenance_json`. Assim dá para separar a lentidão do provedor do custo do pipeline.

## Motivação

No run 12 (2026-11-07), cada nota do connect leva ~27 s, dos quais ~18 s são a chamada ao `claude-haiku-5-5`, para 1.164 tokens de saída (~65 tokens/s). Na rodada do #355, a mesma configuração (thinking desligado, effort `low`, prompt e contexto de produção) teve **4,4 s** de latência média por chamada para 971 tokens (~220 tokens/s).

Também no run anterior, com thinking ligado e ~2.300 tokens de saída, o ritmo foi o mesmo, 27 s por nota. Cortar a saída pela metade não mudou o tempo. Isso aponta para fila ou carga do lado do provedor, e não para geração de tokens. Mas o log não mostra quanto dura cada chamada, então hoje a hipótese não pode ser verificada. Só se vê o intervalo entre linhas de log, que mistura embeddings, escrita e o modelo.

## Contrato / comportamento

- `call_llm` (`zettel/llm.py`) mede com `time.perf_counter()` o tempo de `llm.invoke` e o passa ao `record_llm`.
- `UsageEvent.latency_ms`. A linha `llm` do log ganha a duração (por exemplo `11892 -> 1164  $0.001  17.8s`).
- `CostTracker` soma `llm_latency_ms`. A latência entra no `_usage_delta` do connect, e cada ZTL guarda `llm_latency_ms` em `notes.provenance_json`, ao lado de `llm_cost_usd` e dos tokens. Sem coluna nova: `provenance_json` é JSON.
- O connect paralelo (`CostTracker.absorb`) preserva a latência medida no worker.
- `vision` (`assets.py`) mede da mesma forma.

## Arquivos a tocar

- `zettel/llm.py`, `zettel/usage.py`, `zettel/logfmt.py` (se preciso), `zettel/assets.py`
- `zettel/connector/note.py` (`_usage_delta`)
- `tests/test_llm_usage.py`, `tests/test_usage.py`, `tests/test_connect_parallel.py`
- CLAUDE.md (LLM provider pattern)

## Fora de escopo

- Tempo até o primeiro token (TTFT), que exigiria streaming.
- Coluna de latência em `runs` / `run_usage`, que mudaria o schema.

## Critérios de aceite

- [ ] Toda linha `llm` do log mostra a duração da chamada.
- [ ] Cada ZTL nova tem `llm_latency_ms` em `provenance_json`.
- [ ] Um acerto do cache SQLite não conta latência.

## Dependências

Nenhuma.
