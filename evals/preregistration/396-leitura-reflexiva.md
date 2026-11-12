# Pré-registro — #396: prompts reflexivos de LIT e ZTL para livros da Estante

## Pergunta

Em trechos de livros lidos em modo `reflexao` (romance, conto, quadrinho, ensaio), o prompt reflexivo (`prompts/literature_note_reflective.md` + `prompts/permanent_note_reflective.md`) produz mais trechos com ao menos uma nota **útil como analogia e fiel ao texto** do que o prompt conceitual (`prompts/literature_note.md` + `prompts/permanent_note.md`) sobre os mesmos trechos, segundo rótulo humano cego?

## O que já se sabe

Nada medido. A leitura do prompt conceitual prevê o resultado nos trechos narrativos: ele rejeita `narrative` e proíbe "segundo X". O que ainda não se sabe é se o prompt reflexivo extrai algo **útil**, e não só algo.

## Condições

- **A**: o prompt conceitual, no commit em que este pré-registro entra.
- **B**: o prompt reflexivo, no mesmo commit.
- Ambos com o mesmo `llm.extract` e `llm.connect`, a mesma temperatura e a mesma semente de amostragem.

## Material

- **Fontes**: dois livros em domínio público, harvestados como Markdown, ambos com LIV `reading_mode: reflexao`:
  - um romance (capítulos 1–3);
  - um ensaio ou livro de filosofia (capítulos 1–3).
- **Trechos**: todos os trechos desses capítulos, sem filtro.
- **Execução**: cada trecho passa por A e por B. As saídas são exportadas numa planilha **cega** que não diz qual condição gerou cada nota, em ordem aleatória (semente 0).
- **Rótulo** por nota, à mão, antes de qualquer contagem:
  - `util`: a ideia se entende fora da obra e serve de analogia;
  - `fiel`: a leitura é sustentada pelo trecho e a citação é literal;
  - `nenhum`.
- Um trecho conta como **acerto** da condição quando ela produz ao menos uma nota `util` **e** `fiel`.

## Tamanho da amostra e poder

- **Discordância esperada**: 0,4. A hipótese é que A quase nunca acerta em trechos narrativos.
- **Efeito mínimo de interesse**: 0,2.
- **Itens planejados**: o que `scripts/plan_gold_round.py --discordance 0.4 --effect 0.2` indicar, rodado antes de exportar a planilha. Se os seis capítulos não chegarem a esse número, a rodada é **exploratória** e o poder dela é declarado com `--items`.

## Métrica

- **Decide**: a acurácia por trecho de B contra A, pareada, com o p exato do McNemar.
- **Só reportado**:
  - a fração `fiel` de cada condição (Wilson IC95);
  - o número de notas por trecho;
  - o custo em tokens.

## Regra de decisão

B fica como prompt dos livros em modo `reflexao` se as três condições abaixo valerem:

- B acerta mais trechos que A;
- p exato < 0,05;
- a fração `fiel` de B não fica abaixo da de A por mais de 0,05.

## Desfechos possíveis

- **B melhor e fiel**: o roteamento por modo de leitura fica como está, e a issue fecha.
- **B melhor, mas infiel**: o prompt reflexivo é revisto (ancoragem, proibição de inventar leitura) antes de nova rodada.
- **Nulo ou exploratório**: B continua em uso por decisão explícita, registrada no ADR-069, e a rodada maior fica em aberto.

## Fora desta rodada

- Relações do Prompt 2 reflexivo contra rótulos (`exemplifies` e outras).
- Uso no `article` blog.

## Comandos

```bash
.venv/Scripts/python.exe -m zettel harvest --yes --dump-chunks
.venv/Scripts/python.exe -m zettel extract --yes
```

A troca entre A e B é feita pelo `reading_mode` da LIV (`conceito` em A, `reflexao` em B), com `zettel rechunk` entre as rodadas para voltar os trechos a `pending`.
