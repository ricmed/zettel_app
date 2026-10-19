# Pré-registro #288 — escrita e ligação do Prompt 2

Data: 2026-10-19. Vale só depois deste arquivo estar no commit da execução.
Nenhuma chamada de modelo foi feita para escrever esta regra.

## O que está em jogo

`prompts/permanent_note.md` escreve a nota e escolhe as arestas na mesma chamada,
sem o trecho-fonte (`chunks.text`) e sem a `anchor_quote`. O review já aprovou o
candidato. A chamada ainda pode recusar a nota, e os critérios dessa recusa
quase não disparam.

A implementação atrás de `linking.prompt2_mode` já existe e o padrão é
`combined`: o connect de produção não muda até esta regra ser pontuada.

## Condições

As três usam o mesmo modelo, a mesma temperatura, o mesmo retriever e o mesmo
conjunto de itens. O que muda é o prompt.

| Condição | Chamadas | O que o modelo vê |
| --- | --- | --- |
| `atual` | 1, `permanent_note.md` | candidato + RAG, como hoje |
| `escrita` | 1, `note_write.md` | candidato + trecho (teto `write_source_chars`) + âncora |
| `dividida` | 2: escrita, depois `note_link.md` | a ligação vê a tese e a definição já escritas, mais o RAG |

`note_link.md` não oferece `corroborates`. Os tipos e a ordem continuam os da
#231. Esta rodada não escolhe modelo de NLI (#261).

## Itens

Os mesmos pares nota-alvo já rotulados na #231, relidos do gabarito daquela
rodada. Sem rótulo novo de relação. A deriva da tese e o apoio âncora→tese são
medidos em todo item, com ou sem aresta.

## Métricas

1. Relação exata, como na #231: o tipo rotulado no alvo rotulado.
2. Deriva da tese: `thesis_similarity` (razão de `SequenceMatcher` depois de
   `fold_for_match`) entre a tese aprovada e a tese escrita. Registrar média,
   mínimo e a fração abaixo de 0.90.
3. Apoio âncora→tese: a mesma razão entre a `anchor_quote` e a tese escrita.
   NLI fica para a #261. Registrar média e a fração abaixo de 0.25.
4. Custo: chamadas, tokens de entrada e de saída, USD, por condição.

## Regra

McNemar exato, bicaudal, α = 0.05, nas discordâncias de relação exata.

- `dividida` substitui `atual` no connect somente se a relação exata for melhor
  com p < 0.05 e a fração de teses abaixo de 0.90 não for maior que a de `atual`.
- Se `dividida` não ganhar a relação e `escrita` reduzir a fração abaixo de 0.90
  sem piorar a relação de `atual` (p de piora ≥ 0.05), fica registrado o número
  e o connect continua `combined`. Não se liga o modo `split` por uma melhora
  só de prosa.
- Se `dividida` ganhar, `linking.prompt2_mode` passa a `split` num commit
  posterior a esta pontuação. O limiar `thesis_drift_min_similarity` passa a
  0.90 só nesse commit, e o efeito é marcar `needs_thesis_review`. A nota não
  é descartada.
- Empate ou p ≥ 0.05 mantém `combined` e o limiar em 0.

A agregação de K chamadas da #286 continua desligada mesmo se `dividida` ganhar.
Ela espera este modo e uma rodada própria.

## O que invalida a rodada

Modelo diferente do `llm.connect` gravado no manifesto, itens fora do gabarito
da #231, ou a condição `dividida` ligando com a tese do candidato em vez da
tese escrita.
