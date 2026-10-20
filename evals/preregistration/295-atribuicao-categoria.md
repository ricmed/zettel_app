# Pré-registro — #295: como atribuir cada nota a uma categoria da taxonomia

Registrado em 2026-10-20, **antes** de qualquer rótulo humano. Neste commit o padrão continua `gardener.category_label_mode: label` (argmax sobre `"{pilar}: {categoria}"`) e `gardener.assign_min_similarity: null` (sem limiar). As outras condições existem no código, desligadas, para que a medição use exatamente o que seria adotado.

## O que já se sabe (descritivo, não decide)

- O argmax atual põe toda nota numa categoria. No acervo de desenvolvimento, as notas dos dois livros de *prompt engineering* caíram em "Processamento de Linguagem Natural" e "Modelos de Linguagem", embora "Prompt Engineering" seja folha de "Aplicações de LLMs"; o MOC "Sistemas Autônomos" reuniu notas sobre autonomia kantiana (`docs/artigo/05-garden.md`, §4.2).
- O shadow `moc_category` (Jev, ADR-055) concorda com o argmax em 9 de 22 clusters. Sem rótulo humano, isso não diz qual lado erra.

## Pergunta

Para cada nota permanente, qual categoria da taxonomia um leitor escolheria, e qual condição acerta mais vezes essa escolha?

## Condições

| | modo | texto embedado por categoria |
|---|---|---|
| A | `label` (atual) | `{pilar}: {categoria}` |
| B | `leaves_in_label` | `{pilar}: {categoria} — folha1, folha2, …` |
| C | `max_leaf` | um texto por folha, `{pilar}: {categoria} — folha`; vale a melhor folha |
| D | Jev (`sites.moc_category`) | resposta do shadow do cluster que lista a nota; só referência, cobertura parcial |

## Material

`scripts/export_category_gold.py --chroma-path <copia de data/chroma>` gera, em `.eval-work/categoria-notas/`:

- `categoria-notas-planilha.csv` e `categoria-notas-leitura.md`: título e conteúdo de cada nota (`note_content`), embaralhadas, e a taxonomia numerada com as folhas. Nenhuma resposta de condição aparece.
- `categoria-notas-GABARITO-NAO-ABRIR.json`: para cada item, a resposta de A, B e C (categoria, cosseno, segunda categoria, margem) e a de D quando houver; populações dos estratos.

Estratos: `agree` (A, B e C dão a mesma categoria) e `disagree`; censo até `--per-stratum` (75) por estrato, sorteio com semente 295 acima disso.

O rótulo é o número ou o nome de uma categoria, `nenhuma` quando nenhuma serve, ou `?` quando não dá para julgar (o item sai da conta).

## Métrica

Acerto = resposta da condição igual ao rótulo humano, `nenhuma` incluída (um argmax sem limiar sempre erra uma nota rotulada `nenhuma`). Comparações pareadas por McNemar exato sobre os itens que as duas condições respondem. O acerto é da amostra; o pontuador também reporta as populações para reponderar.

## Regras (aplicadas por `scripts/score_category_gold.py`)

1. Menos de 100 itens rotulados (sem contar `?`): `inconclusivo`, nada muda.
2. **Representação.** B ou C substitui A se o acerto dela menos o de A for ≥ 0,05 **e** McNemar p < 0,05. Se as duas cumprirem, vence a de maior acerto. Senão, A fica.
3. **Limiar.** Para a condição vencedora, varre-se `assign_min_similarity` sobre os cossenos observados (abaixo dele a resposta é `nenhuma`) e toma-se o de maior acerto (empate: o menor). Ele é adotado só se o acerto com limiar menos o acerto sem limiar for ≥ 0,02 **e** McNemar p < 0,05. Senão, o padrão fica sem limiar.
4. **Shadow `moc_category`** (custa a maior parte dos tokens de entrada da fase). Comparado com a condição vencedora nos itens que D cobre:
   - menos de 30 itens cobertos: continua em shadow;
   - D melhor por ≥ 0,05 com p < 0,05: abrir uma issue de portão (D não decide nada neste ciclo);
   - caso contrário: desligar o shadow do garden (`decision.sites.moc_category: off`).
5. A troca do padrão, do limiar ou do shadow sem cumprir a regra é decisão explícita posterior, registrada em adendo ao ADR-019 ou ao ADR-055.

## Fora desta rodada

- Pertencimento a duas categorias por margem pequena (soft assignment): a margem fica no gabarito, mas nenhuma regra decide sobre ela aqui.
- As analogias distantes do connect continuam no modo `label` sem limiar, qualquer que seja o resultado (isoladas em `connector.context.load_connect_taxonomy`).

## Comandos

```bash
.venv/Scripts/python.exe scripts/export_category_gold.py --chroma-path <copia de data/chroma>
.venv/Scripts/python.exe scripts/score_category_gold.py --key .eval-work/categoria-notas/categoria-notas-GABARITO-NAO-ABRIR.json --sheet .eval-work/categoria-notas/categoria-notas-planilha.csv --out evals/results/295-atribuicao-categoria.json
```
