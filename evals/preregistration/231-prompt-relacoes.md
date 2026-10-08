# Pré-registro — #231: definições de relação e teto de conexões do Prompt 2

Registrado em 2026-10-08, **antes** de qualquer chamada com a versão nova do prompt. Neste commit existem:
- as gravações do prompt `current` (rodadas da #212);
- os 80 rótulos revisados sob as novas definições (`evals/gold/relacoes-rotulos-v2.json`).

O commit deste arquivo precede qualquer resposta do prompt `new`.

## Pergunta

Uma reescrita de `prompts/permanent_note.md` (`new`) faz o Prompt 2 (`gemini/gemini-3.1-flash-lite` @ 0,15) escolher relações que concordam mais com o julgamento humano do que o prompt atual (`current`)? A reescrita traz:
- seis definições de relação mutuamente exclusivas, aplicadas em ordem;
- a regra "tema em comum não é relação";
- o teto de 0 a 4 conexões, sem forçar uma conexão fraca.

O contexto das vizinhas é o de produção, 150 caracteres (`trunc`). A #212 mostrou que ele não é o gargalo.

## Decisões do usuário (fixadas na #231)

- **Tema em comum é `nenhuma`.** O `related` exige uma relação descrita em uma frase que não caiba nos outros tipos.
- **De 0 a 4 conexões por nota.** Zero é válido, e uma relação fraca não deve ser forçada.
- **Definições, em ordem** (a primeira que vale decide), lendo a frase como "a nota nova ___ a relacionada":
  1. `contradicts`: as teses não podem ser verdadeiras juntas. Resolver uma limitação da outra **não** conta.
  2. `depends_on`: a nota nova não pode ser definida nem entendida sem a outra.
  3. `exemplifies`: é um caso concreto da outra, sem acrescentar mecanismo, condição ou técnica.
  4. `extends`: acrescenta condição, mecanismo, especialização, técnica, consequência ou a solução de uma limitação.
  5. `supports`: traz evidência para a mesma afirmação, sem afirmar nada novo.
  6. `related`: relação conceitual que não cabe acima.

As mesmas definições valem para o prompt e para quem rotula.

## Dados

### Desenvolvimento: os 80 pares da #212, revisados

- **Rótulos:** os 80 pares foram rotulados de novo sob as definições acima (`relacoes-rotulos-v2.json`). Houve 26 mudanças, principalmente 9 de `depends_on` para `extends` e 5 de `related` para `nenhuma`.
- **Base:** snapshot `n40-s0`, gabarito `relacoes-GABARITO-NAO-ABRIR.json`.
- **Como entra na decisão:** o prompt `new` pode ser ajustado contra esses pares, então **nenhum número do desenvolvimento decide nada**.
- **Linha de base:** com esses rótulos, o `current` acerta 24/80 na rodada `trunc-a`.

### Validação: conceitos e pares que nunca foram vistos

```bash
.venv/Scripts/python.exe scripts/probe_connect_context.py --sample 40 --seed 1 \
    --exclude-snapshot .eval-work/connect-context/snapshot-n40-s0.json \
    --runs trunc-a,trunc-b --prompt current=.eval-work/prompts/permanent-current.md \
    --prompt new=prompts/permanent_note.md --yes
.venv/Scripts/python.exe scripts/probe_connect_context.py --sample 40 --seed 1 \
    --exclude-snapshot .eval-work/connect-context/snapshot-n40-s0.json \
    --runs trunc-a,trunc-b --prompt current=.eval-work/prompts/permanent-current.md \
    --prompt new=prompts/permanent_note.md \
    --export --compare current:trunc-a,new:trunc-a --sheet-prefix relacoes-r2 --seed 1
```

- **Amostra:** 40 conceitos novos, sem os do snapshot `s0`, alternando entre as obras.
- **Planilha:** pares estratificados por qual prompt propôs aresta (`both`, `current_only`, `new_only`, `neither`), até 20 por estrato.
- **Congelamento:** antes da exportação, o prompt `new` é **congelado**, e o sha dele vai para o resultado.
- **Rotulagem:** manual, cega e pelo sentido, sob as definições acima, **antes** de qualquer pontuação. Os rótulos vão para `evals/gold/relacoes-r2-rotulos.json`.

## Regra de decisão (validação)

Um acerto é a **relação exata**, contando `nenhuma`. Adotar o prompt `new` **se, e somente se**:

1. **Melhora:** `new:trunc-a` acerta mais pares que `current:trunc-a`, com McNemar exato **p < 0,05**.
2. **Teto:** as arestas do `new:trunc-a` ficam em no máximo 4 por nota, na média e em pelo menos 95% das notas.
3. **Validade:** no máximo 5% de respostas inválidas ou ausentes por rodada, e no máximo 10% dos conceitos excluídos.
4. **Amostra:** pelo menos 60 pares julgados (sem `?`).

| situação | desfecho |
|---|---|
| 1 a 4 atendidas | adotar `new`, com um adendo na ADR das relações tipadas e os números |
| 1 falha | não adotar; publicar o resultado. Uma decisão explícita de manter, como na #218, precisa ser registrada como tal |
| 2 ou 3 falha | inconclusivo; corrigir o prompt ou repetir as rodadas |
| 4 falha | exportar mais pares do mesmo snapshot, sem mudar a regra |

## Reportado ao lado (informativo)

- a presença de aresta e o tipo nas arestas compartilhadas;
- as arestas propostas por cada prompt contra as arestas do humano;
- a matriz de confusão por tipo;
- a estabilidade entre as rodadas `-a` e `-b`;
- **quantas versões do prompt foram testadas no desenvolvimento**.

## Fora de escopo

- o tamanho do contexto (já medido na #212);
- o `example`, o trecho-fonte e o `anchor_quote` no payload;
- a reconstrução das arestas que já existem no vault. Adotar o prompt muda só as notas geradas daqui em diante, ou as que forem reprocessadas.
