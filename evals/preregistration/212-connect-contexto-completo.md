# Pré-registro — #212: contexto completo das vizinhas no Prompt 2 do connect

Registrado em 2026-10-08, **antes** de qualquer chamada ao modelo. Neste commit existem só o código (`scripts/probe_connect_context.py`, o site `relations` de `scripts/score_decision_gold.py`) e o snapshot da recuperação, que fica em `.eval-work/` (gitignored) e não usa LLM. Nenhuma resposta do Prompt 2 foi vista.

## Pergunta

O Prompt 2 (`prompts/permanent_note.md`, `gemini/gemini-3.1-flash-lite` @ 0,15) escolhe relações tipadas (`supports`, `contradicts`, `extends`, `depends_on`, `exemplifies`, `related`) vendo cada nota vizinha como um trecho de **150 caracteres** (`linking.rag_note_chars`). Se ele vir o conteúdo inteiro de cada vizinha, as relações que escolhe concordam mais com o julgamento humano?

## Por que medir antes de trocar

A #209 deu o texto completo ao dedupe e o resultado contra rótulos manuais foi uma **regressão**: o LLM passou a responder "desenvolve" para quase tudo. O risco análogo aqui é o **excesso de arestas**. Ver mais texto pode fazer o modelo achar relação onde só há tema em comum.

## Condições

As duas condições diferem **só** no número de caracteres por vizinha:

| condição | `note_chars` | o que o modelo vê de cada vizinha |
|---|---|---|
| `trunc` | 150 | os primeiros 150 caracteres do `note_content`, o recorte histórico |
| `full` | 6000 | o `note_content` inteiro; a maior nota da amostra tem 2.187 caracteres |

O formato é o mesmo nas duas: uma linha de cabeçalho (id, wikilink, tags ou relação de grafo) e o conteúdo indentado abaixo. A recuperação também é a mesma, congelada num snapshot por conceito. O candidato, os exemplos, a imagem e a referência de literatura são idênticos nas duas condições.

Cada condição roda duas vezes (`-a` e `-b`). A regra usa só as rodadas `-a`. As `-b` medem o ruído.

## Amostra

- **40 conceitos** já conectados, sorteados com `--seed 0` e alternando entre as obras. Isso dá 6 obras, 6 ou 7 conceitos cada, e 803 pares (conceito, vizinha recuperada).
- Ficam de fora os conceitos que alguma rodada `-a` recusou ou respondeu de forma inválida, e esse número é reportado.
- **Planilha cega de pares**, estratificada pelas rodadas `-a`:
  - `both`: as duas propõem aresta;
  - `trunc_only`: só o `trunc` propõe;
  - `full_only`: só o `full` propõe;
  - `neither`: nenhuma propõe.

  Até **20 pares por estrato**, com `--seed 0`. Nem as analogias distantes nem as sugestões entram, porque nunca viram aresta (ADR-043).
- **Rótulo humano:** a relação (`supports`, `contradicts`, `extends`, `depends_on`, `exemplifies`, `related`, `nenhuma`), ou `?`. A rotulagem é manual, pelo sentido, sem modelo. A planilha não mostra condição, rodada nem motivo da recuperação. O candidato aparece com tese, definição, intuição e limites; a vizinha, com o conteúdo inteiro.

```bash
.venv/Scripts/python.exe scripts/probe_connect_context.py --sample 40 --seed 0 --yes \
    --out evals/results/connect-context-212.json
.venv/Scripts/python.exe scripts/probe_connect_context.py --sample 40 --seed 0 --export
# rotular evals/gold/relacoes-planilha.csv, e só então:
.venv/Scripts/python.exe scripts/score_decision_gold.py --site relations \
    --labels-out evals/gold/relacoes-rotulos.json --out evals/results/relacoes-gold-212.json
```

## Regra de decisão

Um par é um **acerto** quando a resposta da rodada é **exatamente** o rótulo humano, contando `nenhuma`. Uma aresta onde o humano diz `nenhuma` é erro, e uma relação de tipo errado também.

Adotar `linking.rag_note_chars: 6000` **se, e somente se**:

1. **Não inferioridade:** `full-a` acerta pelo menos tantos pares quanto `trunc-a` (`preregistered_rule_212.full_not_worse`).
2. **Validade:** no máximo 5% de respostas inválidas ou ausentes em cada rodada, e no máximo 10% dos conceitos excluídos.
3. **Amostra:** pelo menos 40 pares julgados (sem `?`).

| situação | desfecho |
|---|---|
| 1, 2 e 3 atendidas | trocar o padrão para 6000 (schema e YAML), com adendo na ADR-003 e os números |
| 1 falha | manter 150; registrar o resultado e o tipo de erro dominante, para orientar uma mudança de prompt, como na #218 |
| 2 falha | inconclusivo; repetir as rodadas |
| 3 falha | exportar mais pares do mesmo snapshot, com `--per-stratum` maior, sem mudar a regra |

A não inferioridade, e não a superioridade, é o critério porque o conteúdo inteiro é o desenho correto (#210). O ônus da prova cabe à piora.

## Reportado ao lado, sem decidir nada

- McNemar exato entre `trunc-a` e `full-a`;
- acurácia ponderada pela população de cada estrato;
- presença de aresta (binária) e tipo nas arestas que humano e modelo compartilham;
- **arestas propostas pelo modelo contra arestas do humano**, que é o sinal de excesso de arestas visto na #209;
- distribuição dos tipos de relação por rodada;
- arestas por nota;
- estabilidade entre `-a` e `-b`.

## Fora de escopo

O Prompt 2 também não recebe o `example` do candidato, o trecho-fonte nem o `anchor_quote`. Incluí-los muda o template e é outra pergunta, com outra medição, depois desta.
