# Pré-registro — #208: `corroborates` julgado pelo Jev

Registrado em 2026-10-06, **antes** de qualquer decisão shadow deste site existir. O commit deste arquivo precede qualquer resultado; é isso que impede escolher a regra depois de ver o número.

## Pergunta

Duas notas permanentes de **obras diferentes** afirmam a mesma ideia? Hoje o `connect` decide por um limiar de cosseno (`linking.corroborates_min_similarity` = 0,85, sem medição registrada para esta pergunta). O Jev (`typesafe/jev-1.13.0`, instruções em `en` — desfecho de #206), perguntado em três níveis (ideias diferentes / mesmo tema, teses diferentes / mesma ideia), acerta mais que o limiar contra o julgamento humano?

Nenhuma aresta muda nesta issue, qualquer que seja o resultado.

## Medidas

- **Shadow (sem rótulo).** Cada par de outra fonte com cosseno ≥ `decision.corroborates_band_min` (0,75) é perguntado nas duas ordens (`ab`: nota nova primeiro; `ba`: invertido). Nível do Jev para o par = `corroborates_level(média dos dois scores)`, isto é, o nível mais próximo (corte em 0,5 e 1,5). `scripts/report_decision_shadow.py` reporta a matriz aresta × nível, por faixa de cosseno (`low_band` 0,75–0,80, `near_threshold` 0,80–0,85, `above_threshold` ≥ 0,85), e a divergência média |ab − ba|.
- **Rótulo humano.** Planilha cega `scripts/export_decision_gold.py --site corroborates`, estratificada pelas mesmas faixas, resposta `diferente` | `mesmo-tema` | `mesma-ideia` | `?`. A planilha não mostra cosseno, fontes, aresta nem resposta do Jev.

## Regra de decisão

Para cada par rotulado (sem `?`), "mesma ideia" segundo o humano é a resposta `mesma-ideia`.

- O **limiar acerta** quando `aresta criada` ⇔ humano `mesma-ideia`.
- O **Jev acerta** quando `nível 2` ⇔ humano `mesma-ideia`.

Abrir uma issue de gate para `corroborates` **se, e somente se**:

1. **Amostra.** ≥ 30 pares rotulados, com ≥ 5 em cada uma das três faixas de cosseno.
2. **Acerto.** O Jev acerta mais pares que o limiar **e** o McNemar exato sobre os pares discordantes dá **p < 0,05**.
3. **Ordem.** A divergência média |ab − ba| ≤ 0,3 nível. Acima disso a regra 2 ainda vale, mas qualquer gate futuro precisa manter as duas ordens.

O nível intermediário (`mesmo-tema`) é reportado — quantos pares o humano e o Jev põem ali —, mas não entra na regra: a aresta em jogo é binária.

## Condições de validade

- **Falhas de API** ≤ 5% das linhas do site. Acima disso, inconclusivo.
- **Par incompleto.** Par sem uma das duas ordens sai da conta e é reportado; mais de 10% incompletos torna o resultado inconclusivo.

## Desfechos possíveis

| situação | desfecho |
|---|---|
| regras 1 e 2 atendidas | **abrir issue de gate**, com pré-registro próprio (limiar de confiança medido nos rótulos, validado em lote novo) |
| regra 1 falha | **continuar acumulando** — sem conclusão |
| regra 2 falha (Jev não supera o limiar) | **manter o limiar**; o site pode ser desligado |
| Jev acerta muito mais só em `low_band`/`near_threshold` | registrar como evidência de **falso negativo do limiar**, mesmo sem gate — candidato a re-medir `corroborates_min_similarity` |

Resultado nulo é válido e será publicado como tal.

## Comandos

```bash
.venv/Scripts/python.exe scripts/report_decision_shadow.py
.venv/Scripts/python.exe scripts/export_decision_gold.py --site corroborates
```
