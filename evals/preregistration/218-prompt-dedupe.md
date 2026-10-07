# Pré-registro — #218: prompt de dedupe sem excesso de "desenvolve"

Registrado em 2026-10-07, **antes** de qualquer medição de uma versão nova do prompt. Neste commit só existem as gravações do prompt `current` (#209). O commit deste arquivo precede os resultados; é isso que impede escolher a regra depois de ver o número.

## Pergunta

Uma reescrita de `prompts/dedupe_decision.md` (`new`) faz o LLM de dedupe da mesma fonte (`openai/gpt-4o-mini` @ 0,2) concordar mais com o julgamento humano do que o prompt atual (`current`), quando ambos veem o **texto completo** das notas existentes, como em produção desde #209?

## O que já se sabe

Rodada 1 (64 itens, rótulos manuais cegos, `evals/gold/dedupe-rotulos.json`, `evals/results/dedupe-context-209.json`), com o prompt `current`:

| contexto | acertos | "desenvolve" respondido |
|---|---|---|
| trecho de 200 caracteres | 40/64 | 26 |
| texto completo | 26/64 | 53 |
| humano | — | 23 |

O erro dominante com texto completo é transformar "nova" em "desenvolve": 29 dos 38 itens que o humano marcou como "nova".

## Dados

- **Desenvolvimento: rodada 1.** São os 64 itens acima. O prompt `new` pode ser ajustado livremente contra eles. Por isso, **nenhum número da rodada 1 decide nada**: ele só diz se vale a pena validar.
- **Validação: rodada 2.** Itens que **nunca** foram rotulados nem usados para escrever o prompt:

  ```bash
  .venv/Scripts/python.exe scripts/export_decision_gold.py --site dedupe --round r2 --seed 1 \
      --exclude-labels evals/gold/dedupe-rotulos.json
  ```

  Hoje isso dá 42 itens (os 12 `create_new` restantes e 30 de 114 `link`). A rotulagem é feita à mão, pelo sentido, sem score e sem modelo, **antes** de qualquer medição na rodada 2. Os rótulos vão para `evals/gold/dedupe-r2-rotulos.json` (`method: manual_blind`).

## Rodadas da sonda

`scripts/probe_dedupe_context.py --prompt current=<prompt de main> --prompt new=prompts/dedupe_decision.md`. Cada prompt roda nas duas condições de contexto (`trunc`, `full`), duas vezes (`-a`, `-b`). As regras usam as rodadas `-a`.

O `current` é o arquivo de `main` no momento do commit deste pré-registro (`git show main:prompts/dedupe_decision.md`). O `new` é a versão escolhida ao fim do desenvolvimento. Ela é congelada (com o sha registrado no resultado) antes de rotular a rodada 2.

## Regra de decisão (rodada 2)

Adotar o prompt `new` **se, e somente se**:

1. **Melhora sobre o atual.** `new:full-a` acerta mais itens que `current:full-a`, com McNemar exato **p < 0,05**.
2. **Não inferioridade em relação à melhor configuração medida.** `new:full-a` acerta pelo menos tantos itens quanto `current:trunc-a`.
3. **Validade.** No máximo 5% de respostas inválidas em cada rodada.

Reportado ao lado (informativo):
- respostas "desenvolve" do `new:full-a` contra as do humano;
- estabilidade `-a` × `-b`;
- acerto do alvo;
- placar do Jev nos mesmos itens;
- **quantas versões do prompt foram testadas na rodada 1** (cada tentativa extra aumenta a chance de ajuste ao acaso).

## Desfechos possíveis

| situação | desfecho |
|---|---|
| regras 1, 2 e 3 atendidas | **adotar** `new` com texto completo; merge de #217 com o prompt; ADR-016 com os números |
| regra 1 atendida, regra 2 falha | o prompt novo melhora o texto completo, mas não alcança o trecho: **adotar `new`** e reabrir a decisão de contexto da #209, medindo `new:trunc-a` contra `new:full-a` numa rodada 3 |
| regra 1 falha | **não adotar**; a decisão de manter o texto completo de #209 volta à mesa (o pré-registro de #209 mandava não adotar) |
| validade falha | **inconclusivo**: repetir as rodadas |

## Comandos

```bash
git show main:prompts/dedupe_decision.md > .eval-work/prompts/dedupe-current.md
# desenvolvimento (rodada 1, informativo)
.venv/Scripts/python.exe scripts/probe_dedupe_context.py --yes \
    --prompt current=.eval-work/prompts/dedupe-current.md --prompt new=prompts/dedupe_decision.md \
    --labels evals/gold/dedupe-rotulos.json --out evals/results/dedupe-prompt-218-r1.json
# validação (rodada 2, decisiva)
.venv/Scripts/python.exe scripts/score_decision_gold.py --site dedupe \
    --key evals/gold/dedupe-r2-GABARITO-NAO-ABRIR.json --sheet evals/gold/dedupe-r2-planilha.csv \
    --labels-out evals/gold/dedupe-r2-rotulos.json --out evals/results/dedupe-gold-r2.json
.venv/Scripts/python.exe scripts/probe_dedupe_context.py --yes \
    --key evals/gold/dedupe-r2-GABARITO-NAO-ABRIR.json --labels evals/gold/dedupe-r2-rotulos.json \
    --prompt current=.eval-work/prompts/dedupe-current.md --prompt new=prompts/dedupe_decision.md \
    --out evals/results/dedupe-prompt-218-r2.json
```
