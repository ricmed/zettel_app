# Pré-registro — #281: pisos de relevância e de analogia no Gemini

Registrado em 2026-10-19, **antes** de rodar o probe no índice atual. Neste commit não há tabela nova de cosseno, não há mudança em `config.yaml` e não há `evals/results/` desta rodada. A regra abaixo está em `scripts/probe_relevance_floor.py` (`preregistered_rule_281`, `preregistered_rule_281_analogy`). O commit deste arquivo precede a execução.

## Pergunta

Com `gemini-embedding-2@1536` no lado consulta (`embed_query`, que no Gemini é `RETRIEVAL_QUERY`), onde fica o piso de notas, o piso de capítulos e o piso local de analogia distante? A medição de 2026-09-06/07 foi feita com `ollama/qwen3-embedding@1024d` e só convertida de escala pelo ADR-058.

## O que já se sabe e não decide o número

- Piso de notas hoje: cosseno **0,40** (`retrieval.relevance_floor.min_vector_similarity`). Em 2026-09-06, na escala `(1+cos)/2`, o fora-do-domínio top-1 ficava perto de 0,67 e o dentro-do-domínio perto de 0,80; o piso foi 0,70 nessa escala.
- Piso de capítulos hoje: cosseno **0,40**, medido à parte em 2026-09-07 (71 capítulos; fora-do-domínio máximo 0,693; self-match mínimo 0,745; piso 0,70 na escala antiga, dentro de `(0,693, 0,72]`). Igualar ao piso de notas de novo só vale se as duas medições caírem no mesmo centésimo. Não se copia um para o outro.
- Analogia distante hoje: **−0,20**. Era 0,40 na escala antiga. O piso de relevância não entra nessa conta.
- A auditoria da #279 viu, no lado documento e entre pares quaisquer, p10 0,659, mediana 0,714, p90 0,781. Isso motiva a rodada. Não entra na regra: não é o lado consulta e não é o top-1 de uma pergunta.

Fora desta regra, e portanto intocados mesmo que o piso mude: `absolute_min_similarity`, `bm25_bypass_max_rank`, `bm25_bypass_min_coverage`, `corroborates_min_similarity`.

## Escopo válido

A regra devolve `invalid_setup` e o piso atual se qualquer item falhar:

- provedor `gemini`, modelo `gemini-embedding-2`, dimensões 1536;
- a consulta passa por `collection.query` (`query_side=embed_query`);
- a coleção declara distância cosseno e os vetores medidos têm norma 1 (tolerância do probe, 1e-3);
- as seis consultas fora-do-domínio e as doze dentro-do-domínio devolveram um top-1;
- self-match com pelo menos 12 títulos;
- corpus da coleção com pelo menos 30 itens.

`--off-domain-file` e uma lista dentro-do-domínio diferente das daqui invalidam a rodada. Não se escolhe título à mão.

## Consultas (fixadas agora)

Fora do domínio, as seis já embutidas em `OFF_DOMAIN_QUERIES`:

1. receita de bolo de cenoura com cobertura de chocolate
2. como trocar o oleo do motor de um carro flex
3. sintomas de deficiencia de vitamina D em idosos
4. escalacao do time para a final do campeonato brasileiro
5. previsao do tempo para o fim de semana no litoral norte
6. como podar uma roseira no inverno

Dentro do domínio, as doze de `IN_DOMAIN_QUERIES` (as mesmas do probe de BM25 deste acervo, inclusive quatro de um termo só):

1. o que e chain of thought prompting
2. few-shot e zero-shot na escolha de prompts
3. modelos ARIMA e series temporais nao estacionarias
4. raciocinio de senso comum em LLMs
5. componentes de uma serie historica: tendencia e sazonalidade
6. anti-padroes na escrita de prompts
7. regressao polinomial
8. estacionaridade de uma serie
9. sazonalidade
10. ARIMA
11. benchmarks
12. prompt injection

Self-match: os primeiros 20 títulos não vazios, `ORDER BY note_id` nas notas e `ORDER BY chapter_id` nos capítulos já resumidos. Se houver menos de 20 e pelo menos 12, usam-se todos. Sem amostragem aleatória.

Cada consulta contribui com a similaridade top-1 na coleção sondada. `off_hi` é o máximo fora do domínio. `in_lo` é o mínimo dentro do domínio. `self_lo` é o mínimo do self-match. A margem que decide é `in_lo − off_hi`.

Notas e capítulos são duas chamadas. A de capítulos usa `--collection chapter_summaries` e as mesmas doze perguntas dentro do domínio. O self-match de capítulo é o título do capítulo, como em 2026-09-07.

## Regra do piso (notas e capítulos, cada um com o seu `current`)

Implementada em `preregistered_rule_281`, nesta ordem:

| ordem | condição | desfecho |
|---|---|---|
| 0 | escopo inválido ou corpus com menos de 30 itens | `invalid_setup`: não muda o piso |
| 1 | margem ≤ 0 | `keep_floor_negative_margin`: o piso atual fica. Nenhum escalar separa as bandas. O adendo do ADR registra a sobreposição e aponta o ADR-044. Não se escolhe outro número depois de ver a tabela |
| 2 | o centésimo estritamente acima de `off_hi` é ≥ `min(in_lo, self_lo)` | `keep_floor_gap_below_resolution`: a folga não cabe num passo de 0,01 sem cortar o dentro-do-domínio ou o self-match. O piso atual fica |
| 3 | esse centésimo já é o piso atual | `keep_floor` |
| 4 | nenhum dos anteriores | `set_floor`: o piso passa a ser esse centésimo |

O centésimo estritamente acima é o que 2026-09-07 fez com 0,693 → 0,70. Um máximo que já cai num centésimo (0,70) produz 0,71, para o piso continuar acima de toda consulta fora do domínio. O ponto médio da faixa que o probe imprime continua sendo teto de leitura, não a regra.

`set_floor` escreve o valor em `config.yaml` e no default de `RelevanceFloorConfig` da coleção correspondente. Os dois objetos continuam separados no teste de config. O baseline `evals/results/current-ask.json` só é regenerado se o piso de notas mudar, e o commit da execução diz a diferença.

## Regra da analogia distante

Amostra: as primeiras 40 notas permanentes com tese ou definição e com bucket taxonômico, `ORDER BY note_id`. Bucket = o argmax que o connect já usa. Consulta = tese + definição, lado consulta. Fora do bucket = o resto de `permanent_notes`.

Por nota: as 5 maiores similaridades fora do bucket (`top_out`) e as 5 maiores dentro (`top_in`). No agregado, `out_p10` e `out_p90` sobre todos os `top_out`, `in_p10` sobre todos os `top_in`. p10 é o elemento de índice `ceil(0,10 · n) − 1` na lista ordenada.

`preregistered_rule_281_analogy`, com `current = −0,20`:

| ordem | condição | desfecho |
|---|---|---|
| 0 | menos de 30 notas com bucket | `keep_analogy_floor` |
| 1 | `out_p10` ≤ piso atual | `keep_analogy_floor`: o piso ainda corta a cauda que o connect vê |
| 2 | `out_p10` acima do piso atual, `out_p90` abaixo de `in_p10`, e o p10 arredondado ao centésimo continua abaixo de `in_p10` | `raise_analogy_floor`: o piso local passa a ser esse centésimo. É um piso real, no percentil 10 do que o connect enxerga fora do bucket |
| 3 | nenhum dos anteriores | `rank_only`: fora do bucket não é mais fraco que dentro. O critério fica bucket + top-k. `distant_analogy_min_similarity` vai para **−0,70** (o `absolute_min_similarity` já medido), só como backstop. O ADR-043 registra que o piso local deixou de ser corte de relevância |

O arredondamento do p10 é o centésimo mais próximo (`floor(x·100 + 0,5)/100`).

## Manifesto

O JSON da execução traz, por coleção: provedor, modelo, dimensões, `query_side`, espaço da distância, norma, `off_hi`, `in_lo`, `self_lo`, contagens, margem e o desfecho da regra. Sem isso a rodada é `invalid_setup`.

## Comandos (não rodar neste commit)

```bash
.venv/Scripts/python.exe scripts/probe_relevance_floor.py --samples 20 \
    --json evals/results/relevance-floor-281-notes.json
.venv/Scripts/python.exe scripts/probe_relevance_floor.py --samples 20 \
    --collection chapter_summaries \
    --json evals/results/relevance-floor-281-chapters.json
```

As doze consultas dentro do domínio e a amostra de analogia entram na execução que aplicar esta regra. Este pré-registro não as dispara.
