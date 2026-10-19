# Pré-registro — #286: ordem do contexto e teto de conexões do Prompt 2

Registrado em 2026-10-19, **antes** de qualquer chamada com ordem embaralhada ou permutação. A ordem que o connect grava continua a do retriever (score). `build_rag_context` só embaralha se receber `order_key`, e o connect não envia essa chave.

## O que já se sabe e não decide

Nas 781 respostas em cache, a 1ª nota similar foi escolhida em 60% e a 5ª em 18,5%. As analogias, também ordenadas por score, ficaram quase planas. 727 de 776 respostas (93,7%) tinham exatamente 3 conexões, o teto da época. Esse teto está registrado na #231. Não muda as definições de tipo.

O efeito de posição está confundido com relevância. Esta rodada separa os dois.

## Condições

Mesmos conceitos, mesmo retrieval congelado, mesmo prompt e mesmo modelo. Só a ordem dentro de cada grupo (similares, vizinhas, analogias) muda. Os títulos dos grupos ficam.

| nome | ordem |
|---|---|
| `atual` | a que o retriever devolve |
| `embaralhada` | `Random` com semente `sha256("{seed}:{concept_id}:{grupo}")`, seed fixa da rodada |
| `agregada` | K=3 permutações. A aresta fica se aparece em pelo menos 2. O tipo é a maioria; empate desempata pelo nome do tipo |

K=3 é o mínimo em que ⌈K/2⌉ é maioria estrita de 2. Não se aumenta K depois de ver o resultado.

## Métricas

Primária: relação exata contra os rótulos cegos de #231, McNemar exato.

Também, sem decidir sozinhas: taxa de escolha por posição dentro de cada grupo, nas ordens `atual` e `embaralhada`; número de conexões por nota.

Menos de 60 pares julgados nas três condições: `inconclusivo`.

## Regra

1. `embaralhada` acerta menos que `atual` com p < 0,05 → `keep_score_order`. A ordem aleatória piora e não entra no connect.
2. `embaralhada` não piora (acerto maior, igual, ou p ≥ 0,05) → `shuffle_by_concept`. O connect passa a enviar `order_key=concept_id`. A chave do cache muda com a ordem, de propósito.
3. `agregada` acerta mais que `atual` com p < 0,05 → `consider_aggregation`. Não se liga no connect neste commit: são 3 chamadas por nota. A decisão de pagar isso fica para uma issue que separe ligação e escrita (#288). O adendo registra o número.
4. Caso contrário a agregação não entra.

`shuffle_by_concept` e `consider_aggregation` podem coexistir. Adotar a ordem embaralhada sem a regra 2, ou ligar a agregação no connect, é decisão explícita posterior.

## Comandos (não rodar neste commit)

```bash
.venv/Scripts/python.exe scripts/probe_connect_context.py --shuffle-context 0 --yes
.venv/Scripts/python.exe scripts/probe_connect_context.py --permutations 3 --yes
```
