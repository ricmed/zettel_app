# Pré-registro — #285: sementes por fonte e grafo do connect

Registrado em 2026-10-19, **antes** de rodar `scripts/probe_connect_context.py` nas três condições. Neste commit a cota está desligada (`seed_same_source_max: 0`) e `linking.graph_expansion` copia `retrieval.graph_expansion` (5 saltos, 16 vizinhas, decay 0,2). O `ask` continua lendo só o bloco de `retrieval`. Não há `evals/results/` desta rodada.

A #281 ainda não remediu o piso. Esta regra usa o piso que estiver em `config.yaml` na hora da execução e não o altera.

## Pergunta

No conjunto de pares já rotulados do Prompt 2 (#212 / #231), diversificar as sementes por fonte, ou enxugar a expansão de grafo do connect, acerta mais relações exatas do que a configuração atual?

## Condições (fixadas agora)

As três usam o mesmo prompt (`prompts/permanent_note.md`), o mesmo modelo de connect e os mesmos pares. Só muda a recuperação.

| nome | `seed_same_source_max` | `max_hops` | `max_neighbors` |
|---|---|---|---|
| `atual` | 0 | 5 | 16 |
| `diversificado` | 2 | 5 | 16 |
| `grafo` | 0 | 1 | 4 |

`graph_hub_repeat_limit` fica 0 nas três. Não entra na decisão.

Métrica primária: relação exata contra o rótulo cego, como em #231. McNemar exato, p < 0,05.

Reportadas, sem decidir sozinhas: fração de arestas entre fontes diferentes; p90 do grau de entrada das notas que aparecem como vizinhas de grafo.

## Regra

Sobre os pares julgados que as três condições compartilham. Menos de 60 pares: `inconclusivo`, nada muda no YAML.

1. `diversificado` acerta mais que `atual` e p < 0,05 → `seed_same_source_max` passa a 2. Senão permanece 0.
2. `grafo` acerta mais que `atual` e p < 0,05 → `linking.graph_expansion` passa a `max_hops: 1` e `max_neighbors: 4`. Senão permanece a cópia do ask.
3. Os dois podem ser adotados juntos se cada um bater o `atual`. Não se compara `diversificado` com `grafo` para escolher um só.
4. Empate ou p ≥ 0,05 mantém o número atual. Adotar sem essa diferença é decisão explícita posterior, no mesmo espírito de #231, e tem de ser escrita no adendo.

O `[NEEDS INPUT]` do ADR-009 sobre connect e ask compartilharem um bloco fica respondido neste commit: são dois blocos. Os valores iguais são a condição `atual`, não a conclusão.

## Comandos (não rodar neste commit)

```bash
.venv/Scripts/python.exe scripts/probe_connect_context.py --prompt current=prompts/permanent_note.md
```

A execução que comparar as três condições grava `evals/results/connect-rag-285.json` e só então mexe no YAML.
