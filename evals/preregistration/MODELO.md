# Pré-registro — #NNN: título da pergunta

Modelo para os pré-registros desta pasta. Copie para `NNN-assunto.md`, preencha e **commite antes** de rotular ou medir: o commit precede o resultado, e é isso que impede escolher a regra depois de ver o número.

## Pergunta

Uma frase: qual condição, contra qual, sobre quais itens e com que rótulo.

## O que já se sabe

Números de rodadas anteriores (descritivos, não decidem), cada proporção com o IC95 de Wilson (`zettel.evals.stats.wilson`).

## Condições

O que é comparado (prompt, modelo, limiar, contexto), com o commit ou o sha que congela cada um.

## Material

De onde vêm os itens, a semente, os estratos e a população de cada um, e como a rotulagem é feita (cega, à mão, antes de qualquer medição).

## Tamanho da amostra e poder

Declarado **antes** de exportar a planilha (#332):

- **Discordância esperada**: fração dos itens em que só uma das condições acerta. Da rodada anterior da mesma decisão, se houver (`só A + só B` sobre `n`).
- **Efeito mínimo de interesse**: diferença de acurácia que mudaria a decisão. Não é o efeito observado antes, a não ser que seja esse o que se quer confirmar.
- **Itens planejados** para poder 0,8 com α = 0,05, pelo teste que a regra usa:

  ```bash
  .venv/Scripts/python.exe scripts/plan_gold_round.py --discordance 0.3 --effect 0.1
  .venv/Scripts/python.exe scripts/plan_gold_round.py --from-round N:SO_A:SO_B --items N
  ```

- Se a população não comporta esse tamanho, diga o poder que a rodada tem (`--items`) e trate o resultado como **exploratório**: um p acima de 0,05 nessa rodada não é evidência de ausência de efeito.

## Métrica

O número que decide, e o que é só reportado. Toda proporção sai com o IC95 de Wilson; toda comparação pareada sai com o p exato e o *mid-p* do McNemar.

## Regra de decisão

A regra, com o teste em que ela se apoia (`p exato < 0,05` ou `mid-p < 0,05`) — escolhido aqui, não depois. Os outros números são reportados, não decidem.

## Desfechos possíveis

O que acontece em cada caso, inclusive o nulo.

## Fora desta rodada

## Comandos
