# Pré-registro — #250: overlap e piso de chunk no gold set

Registrado em 2026-10-17, **antes** de qualquer `rechunk` ou `extract` destas condições. Este arquivo não contém resultado. O commit que o adiciona precede a execução; é isso que impede escolher a regra depois de ver o número.

Os defaults de produção permanecem (`chunk_overlap: 400`, `min_chunk_chars: 200`, `chunk_size: 2500`) até a regra abaixo ser atendida. Esta rodada **não** varia `chunk_size`.

## Pergunta

No gold set de extração de #175 (120 itens, rótulos humanos em `evals/gold/extracao-rotulos.json`, o mesmo conjunto do ADR-050), mudar o chunking muda o acordo com o humano o bastante para trocar o default?

Duas perguntas, em rodadas separadas, com o modelo e o prompt de `extract` que estiverem em produção no momento da execução:

1. **Overlap.** `chunk_overlap` 400 (produção) contra 0. `chunk_size` e `min_chunk_chars` ficam nos defaults.
2. **Piso em PDF.** `min_chunk_chars` 200 (produção) contra 400, só nos itens cujo origem é PDF. `chunk_overlap` fica em 400 e `chunk_size` no default.

## Por que os itens não são os mesmos chunks

Rechunkar muda a fronteira. O rótulo humano é de uma passagem já cortada. Cada item do gold é pareado com o chunk novo da mesma fonte cujo texto tem a maior cobertura de caracteres sobre o texto do item (interseção de caracteres do item contidos no chunk novo, dividida pelo tamanho do item). Empate: o menor `chunk_index`.

Um item com cobertura **< 0,5** fica **sem par**. Entra na métrica de rendimento e no custo, e **sai** do McNemar.

## Desfecho binário

Para cada item pareado, a condição acerta se:

- o humano marcou manter e o chunk pareado produz ao menos um candidato que passa o filtro determinístico do extract; ou
- o humano marcou descartar e o chunk pareado não produz candidato.

Não se conta nota aprovada na revisão humana desta rodada: isso exigiria um segundo rótulo. O filtro do extract é o que a condição controla.

## Métricas informativas

Reportadas ao lado, sem poder de decisão:

- candidatos aceitos pelo filtro por 1.000 caracteres lidos;
- rejeições por `rejection_reason`;
- conceitos `dedupe_pending` por fonte;
- custo de LLM em USD (o cache não é descontado: o número é teto).

## Regra de decisão

Trocar o default daquela rodada **se, e somente se**, no par produção × candidato, sobre os itens pareados julgados nas duas condições:

1. O candidato acerta mais itens que a produção, com McNemar exato **p < 0,05**.
2. Itens sem par são no máximo **20%** dos itens da rodada.
3. Falha de parse em no máximo **5%** dos itens de cada condição.

A rodada do piso usa só os itens PDF. A do overlap usa o gold inteiro.

## Desfechos possíveis

| situação | desfecho |
|---|---|
| regras 1, 2 e 3 atendidas | **adotar** aquele default; ADR-014 com os números |
| regra 1 falha | **manter** o default de produção |
| regra 2 ou 3 falha | **inconclusivo**; não mudar o default |

## O que esta sessão não faz

Não roda `rechunk` nem `extract`. Não lê o gabarito para ajustar a regra. Não muda `config/config.yaml`.
