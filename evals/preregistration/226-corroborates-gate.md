# Pré-registro — #226: gate de `corroborates`, Jev contra limiar recalibrado

Registrado em 2026-10-08, **antes** de exportar a rodada 2. Neste commit só existe a rodada 1 (#208); nenhum par da rodada 2 foi visto, rotulado ou pontuado. O commit deste arquivo, junto com a regra em `scripts/score_decision_gold.py` (`preregistered_rule_226`), precede os resultados.

## Pergunta

Na aresta `corroborates` entre notas de obras diferentes, o Jev (`typesafe/jev-1.13.0`, nível 2 da média das duas ordens) acerta mais que um limiar de cosseno recalibrado? E esse limiar recalibrado acerta mais que o atual (0,85)?

## O que já se sabe (rodada 1, #208)

São 69 pares rotulados à mão, às cegas (`evals/gold/corroboracao-rotulos.json`, `evals/results/corroborates-gold-208.json`, ADR-055 *Results 2026-10-07*):

| condição | acertos |
|---|---|
| `jev` | 66/69 |
| `cos88` (cosseno ≥ 0,88, escolhido **nestes** rótulos) | 66/69 |
| `threshold` (aresta criada) | 52/69 |
| `cosine` (cosseno ≥ 0,85) | 48/69 |

O `jev` e o `cos88` discordam em 4 pares (2 a 2, p = 1,0). O `cos88` supera o `cosine` (20 a 2, p = 0,0001), mas o corte foi escolhido nos mesmos dados, então esse número é otimista. Todos os 9 pares `mesma-ideia` estão acima de 0,85.

## Condições (fixadas agora)

- **`jev`**: aresta se `corroborates_level(média(ab, ba)) == 2`. Sem nenhum ajuste.
- **`cos88`**: aresta se o cosseno é ≥ **0,88**. É o valor redondo entre o maior `diferente`/`mesmo-tema` abaixo dele (0,878) e o menor `mesma-ideia` acima (0,883).
- **`cosine`**: aresta se o cosseno é ≥ 0,85, que é o `linking.corroborates_min_similarity` de hoje.
- **`threshold`**: a aresta que o `connect` criou de fato. É só reportada, porque o limite `max_edges` e as sementes de hop > 0 também a impedem. A comparação entre limiares usa o cosseno, em que esses efeitos são iguais para os dois cortes.

"Mesma ideia" é só a resposta humana `mesma-ideia`. O `mesmo-tema` conta como "não". O `?` sai da conta.

## Dados (rodada 2)

Pares que nunca foram rotulados:

```bash
.venv/Scripts/python.exe scripts/export_decision_gold.py --site corroborates --round r2 --seed 1 \
    --per-stratum 40 --exclude-labels evals/gold/corroboracao-rotulos.json
```

Antes da exportação, havia 256 pares acima de 0,85 e 254 abaixo, sem rótulo. A `low_band` foi rotulada inteira na rodada 1. A rotulagem é manual, pelo sentido, sem cosseno, fontes ou modelo, e termina **antes** de qualquer pontuação. Os rótulos vão para `evals/gold/corroboracao-r2-rotulos.json` (`method: manual_blind`).

## Regra de decisão (rodada 2)

Implementada em `preregistered_rule_226`, aplicada nesta ordem:

| ordem | condição | desfecho |
|---|---|---|
| 0 | menos de **60** pares julgados ou menos de **8** `mesma-ideia` | `keep_accumulating`: exportar uma rodada 3 com os pares restantes, sem conclusão |
| 1 | `jev` acerta mais que `cos88` **e** McNemar exato p < 0,05 | `jev_decides_edge`: issue para o Jev decidir a aresta, mantendo as duas ordens e o fail-open (sem a API, volta para `cos88`) |
| 2 | `cos88` acerta mais que `cosine` **e** McNemar exato p < 0,05 | `raise_threshold_to_cut`: subir `linking.corroborates_min_similarity` para 0,88 (adendos à ADR-045 e à ADR-055); o site `decision.sites.corroborates` pode ser desligado |
| 3 | nenhum dos anteriores | `keep_threshold`: manter 0,85 e publicar o resultado nulo |

**Empate entre o Jev e o `cos88` favorece o limiar.** Ele não depende de serviço externo, não custa nada e não adiciona latência.

Reportados ao lado, sem decidir nada:
- os acertos de `threshold`;
- a matriz dos três níveis;
- a AUC do score do Jev e do cosseno;
- a divergência entre as ordens;
- os acertos por faixa.

## Poder estatístico (registrado de antemão)

Na rodada 1, o `jev` e o `cos88` discordaram em 4 de 69 pares. Se essa taxa se repetir, 80 pares dão cerca de 5 discordantes. Isso não basta para p < 0,05 nem com 5 a 0 (p = 0,0625). O desfecho 1 é, portanto, improvável por construção, e o desfecho esperado é o 2. Este pré-registro aceita isso: a pergunta que a rodada 2 de fato responde é se o corte de 0,88 se sustenta fora da amostra em que foi escolhido.

## Correção em relação ao texto da issue

O rascunho na #226 comparava `cos88` com `edge85` no desfecho 2. Aqui a comparação é com `cosine` (cosseno ≥ 0,85), pelo motivo descrito em **Condições**: o `edge85` mistura o corte com o limite de arestas.

## Validade

- **Falhas de API:** no máximo 5% das linhas do site. Acima disso, o resultado é inconclusivo.
- **Pares sem as duas ordens:** saem da conta. Se forem mais de 10%, o resultado é inconclusivo.
- **Escopo do corte:** 0,88 vale só para `ollama/qwen3-embedding@1024d`. Trocar o modelo de embedding invalida o corte.

## Comandos

```bash
# rodada 2: exportar, rotular à mão e só então pontuar
.venv/Scripts/python.exe scripts/score_decision_gold.py --site corroborates \
    --key evals/gold/corroboracao-r2-GABARITO-NAO-ABRIR.json \
    --sheet evals/gold/corroboracao-r2-planilha.csv \
    --labels-out evals/gold/corroboracao-r2-rotulos.json \
    --out evals/results/corroborates-gate-226-r2.json
```
