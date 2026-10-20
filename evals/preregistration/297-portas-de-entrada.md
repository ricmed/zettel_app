# Pré-registro — #297: qual sinal escolhe as portas de entrada dos MOCs

Registrado em 2026-10-19, **antes** de qualquer rótulo humano. Neste commit o padrão provisório é `gardener.entry_selection: internal_degree` com `max_entries_per_moc: 10` (ADR-060), escolhido pelo autor antes desta rodada.

## O que já foi medido (descritivo, não decide)

`scripts/probe_moc_entry_points.py` rodou sobre o acervo de desenvolvimento (781 notas, 2.305 arestas, 11 MOCs usados como assuntos), sem LLM e sem embedding. Resultado em `evals/results/297-portas-de-entrada.json`. Alcançabilidade a 2 saltos (R2) com teto 10:

| sinal | R2 |
|---|---|
| `internal_degree` | 93,2% |
| `relevance` | 93,3% |
| `corroboration` | 91,2% |
| `medoid` | 90,5% |
| `first_in_source` | 90,3% |

O banco medido é anterior à coluna `status` de `note_connections`, então "todas" e "percorríveis" coincidem. A alcançabilidade separa pouco os sinais (3 pontos); por isso ela não decide sozinha.

## Pergunta

Num julgamento humano cego, as notas que cada sinal escolhe são boas portas de entrada para o assunto?

## Material

`scripts/probe_moc_entry_points.py --export-sheet .eval-work/portas --sheet-cap 10` gera `portas-planilha.csv` (para cada assunto, a união dos 10 primeiros de cada sinal, embaralhada, com título e conteúdo; nenhuma pista do sinal) e `portas-GABARITO-NAO-ABRIR.json` (quais sinais escolheram cada item). O rótulo é `s` (boa porta: o leitor começaria por ela para entender o assunto) ou `n`.

## Métrica

Para cada sinal, **precisão** = itens `s` / itens que o sinal escolheu. Intervalo de 95% por *bootstrap* reamostrando **assuntos** (2.000 reamostras, semente 297), porque os itens de um assunto não são independentes.

## Regra

1. Menos de 100 itens rotulados ou menos de 8 assuntos: `inconclusivo`, nada muda.
2. Um sinal X substitui `internal_degree` como padrão só se:
   - a precisão de X menos a de `internal_degree` for ≥ 0,10, com o intervalo de 95% da diferença acima de 0; **e**
   - o R2 percorrível de X, com teto 10, não ficar mais de 2 pontos abaixo do de `internal_degree` no mesmo banco.
3. Se mais de um sinal cumprir a regra, vence o de maior precisão.
4. O teto (10) não é decidido nesta rodada; os tetos 5 e 15 são reportados.
5. Adotar outro sinal sem cumprir a regra é decisão explícita posterior, registrada em adendo ao ADR-060.

## Comandos

```bash
.venv/Scripts/python.exe scripts/probe_moc_entry_points.py --chroma-path <copia de data/chroma> --json evals/results/297-portas-de-entrada.json
.venv/Scripts/python.exe scripts/probe_moc_entry_points.py --chroma-path <copia de data/chroma> --export-sheet .eval-work/portas
```

A pontuação da planilha rotulada é um passo seguinte (não implementado neste commit) e grava `evals/results/297-portas-julgamento.json`.
