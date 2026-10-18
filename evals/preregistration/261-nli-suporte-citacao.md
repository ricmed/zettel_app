# Pré-registro — #261: suporte citação→tese por NLI

Registrado em 2026-10-18, **antes** de qualquer execução. Este branch não adiciona modelo de NLI e não calcula o score.

Protocolo de #180. A amostra é a dos 38 chunks aceitos do gold set de #175. Nada é ajustado contra esses 38 rótulos.

## Pergunta

P(entailment | âncora → tese), e a variante P(entailment | âncora + definição → tese), separam o que um humano guardaria do que descartaria melhor do que uma moeda?

## Sinais

Calculados offline, sem chamada de LLM do pipeline:

- por candidato aceito: entailment âncora → tese
- variante: entailment (âncora + definição) → tese
- por chunk: mínimo e média dos candidatos

O modelo de NLI, quando a medição rodar, fica fixado neste arquivo **antes** da primeira pontuação. Ainda não está escolhido.

## Regra de decisão

A barra é a de #180: o **limite inferior do IC95 da AUC** (Hanley–McNeil, como `scripts/calibrate_review_confidence.py`) **> 0,5**.

- Passar a barra **não** liga um gate. Abre a confirmação em amostra nova e disjunta (item 5 de #180).
- Não passar **encerra** o sinal como candidato a gate.
- Mostrar o score no cartão do review, sem usá-lo como gate, é uma decisão separada e só se discute depois do número.

## Desfechos

| situação | desfecho |
|---|---|
| IC95 inferior > 0,5 | **candidato** — plano de confirmação disjunta em #180; sem gate neste resultado |
| IC95 cruza 0,5 ou fica abaixo | **descartar** como sinal de auto-aprovação |
| o modelo não foi fixado antes da rodada | **inválido** — repetir |

## Comandos

Ainda não há probe. A medição futura escreve scores por item em `evals/results/` sem o texto das notas. Não rodar neste branch.
