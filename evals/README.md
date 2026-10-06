# Avaliação do `zettel ask`

Infraestrutura **opcional** de pesquisa: separa *roteamento* (a nota-alvo chegou ao pool?) de *representação* (tendo chegado, sobreviveu ao piso e sustentou a resposta?). Decisão e alternativas: [ADR-038](../docs/adrs/generated/QA-WRITING/ADR-038-ask-trajectory-evals-offline-replay.md).

## Rodar

```bash
.venv/Scripts/python.exe -m zettel.evals.replay evals/configs/current-ask.yaml --out evals/results/current-ask.json
.venv/Scripts/python.exe -m pytest tests/evals/ -v
```

Nada aqui chama LLM nem abre rede — há testes que fazem `socket.connect` e `llm.call_llm` explodirem para garantir isso.

## Estrutura

| Caminho | O que é |
|---|---|
| `zettel/evals/manifest.py` | identidade do run (o envelope em que o número foi medido) |
| `zettel/evals/score.py` | veredictos determinísticos |
| `zettel/evals/replay.py` | runner offline + CLI |
| `evals/fixtures/` | só sintético ou domínio público |
| `evals/configs/` | YAML sem segredos |
| `evals/results/` | agregados pequenos, commitáveis |
| `.eval-work/` | **gitignored**: trajetórias cruas, vaults privados |

## Veredictos

| Veredicto | Significado |
|---|---|
| `routing_miss` | a nota-alvo nunca entrou no pool de candidatos |
| `floor_reject` | a nota-alvo **foi** recuperada; o piso a barrou |
| `answer_fail` | a nota-alvo foi usada; a resposta não bateu a rubrica declarada |
| `ok` | como esperado |
| `unknown` | o gold não nomeia alvo — medido, não chutado |

Pergunta marcada `expect_no_evidence` é julgada pelo comportamento que importa proteger: `hits` vazio tem que significar **LLM não chamado**. Se respondeu mesmo assim, é `answer_fail`.

A única checagem sobre a resposta é a rubrica de substring declarada no gold (`answer_must_contain`). Nada infere raciocínio oculto do texto. Sem rubrica, o julgamento é só de recuperação.

## Identidade do run

Dois runs só são comparáveis se o manifesto bater: mesmas perguntas, mesmo fixture, mesmos modelos, mesmos limiares — e **mesmo commit**. Uma comparação entre commits tem identidade diferente de propósito; tem que ser um ato deliberado, não um acidente.

O manifesto recusa campo obrigatório vazio e valor que pareça credencial (`sk-`, `api_key`, `Bearer `, `ghp_`). Ele é commitável; segredo fica no `.env`.

## Guardrail de afirmações

Um **null result é resultado válido**. Não publique comparação entre condições sem envelope idêntico (mesmas perguntas, mesmo modelo, mesmos limiares) dos dois lados, e não afirme que uma abordagem "vence" outra a partir de um fixture sintético — ele prova que as classificações funcionam, não que o vault recupera bem.

## Ablações

O campo `condition` do manifesto aceita qualquer nome (`current_ask`, `no_graph`, `vector_only`, `lit_only`, `topic_index_off`), **uma por run**. Nenhuma está implementada: o campo existe para que adicionar uma depois não exija redefinir a identidade do run.

Runner ao vivo (com orçamento declarado em `max_calls` / `max_input_tokens`, fail-closed) fica para um follow-up, e só se o replay estiver verde.

---

# Gold set de extração (#175)

Tudo o que o projeto media sobre qualidade de extração era auto-referente: recalculado a partir do veredito que o próprio `extract` gravou, ou treinado nele (ADR-049). Este gold set compara esse veredito com um julgamento humano independente.

## Rodar

```bash
# 1. exporta a planilha cega (texto + locator, sem veredito do LLM)
.venv/Scripts/python.exe scripts/export_extraction_gold.py
# 2. um humano preenche `veredito` (s | n | ?), `categoria` e `nota`
# 3. pontua
.venv/Scripts/python.exe -m zettel.evals.extraction evals/gold/extracao-planilha.csv evals/gold/extracao-GABARITO-NAO-ABRIR.json --labels-out evals/gold/extracao-rotulos.json --out evals/results/extraction-gold.json
```

## O que é commitável

| Arquivo | Commit? | Por quê |
|---|---|---|
| `*-planilha.csv`, `*-leitura.md` | **não** (gitignored) | carregam texto verbatim de obras sob direito autoral; o repositório é público |
| `*-GABARITO-NAO-ABRIR.json` | sim | só ids, veredito do LLM e população por estrato |
| `*-rotulos.json` | sim | o julgamento humano por `chunk_id`, sem texto — o artefato durável |
| `evals/results/extraction-gold.json` | sim | agregados e discordâncias, sem texto |

## Leitura dos números

- **Positivo = "um humano guardaria".** Falso negativo é chunk que o LLM rejeitou e o humano guardaria — nota perdida em silêncio, sem estágio posterior que a veja.
- **A amostra é estratificada** (censo das rejeições contestadas, fatia de `structural`, fatia de aceitos). As estimativas de corpus são **ponderadas** por `população / amostra` de cada estrato; as contagens cruas vêm ao lado para o tamanho da amostra ficar visível. Censo não tem erro amostral; estrato amostrado leva IC95 de Wilson.
- **`?` fica fora da conta**, contado à parte — nunca como concordância nem como erro.
- **A planilha costuma voltar de uma planilha eletrônica** em `;` e cp850/cp1252. O leitor aceita ambos e reporta cada normalização (ex.: `y` lido como `s`).

# Camada de decisão tipada (#206)

O Jev (TypeSafe) responde perguntas tipadas com probabilidade ([ADR-055](../docs/adrs/generated/LLM/ADR-055-typed-decision-layer-shadow.md)). Duas medidas, com as regras pré-registradas **antes** da primeira chamada em [`preregistration/206-jev-camada-decisao.md`](preregistration/206-jev-camada-decisao.md).

## Sonda no gold set (ao vivo)

```bash
.venv/Scripts/python.exe scripts/probe_jev_gold.py --task extract --lang en          # estimativa, sem chamar
.venv/Scripts/python.exe scripts/probe_jev_gold.py --task extract --lang en --yes     --out evals/results/jev-gold-extract-en.json --out-key evals/gold/extracao-GABARITO-jev-en.json
.venv/Scripts/python.exe scripts/probe_jev_gold.py --task reader --lang en --yes --out evals/results/jev-gold-reader-en.json
```

- **Texto**: a passagem vem da planilha rotulada (`extracao-planilha.csv`, gitignored) — o texto que o humano julgou —, não do `state.db`, que o vault de desenvolvimento reinicia. As notas do leitor vêm da rodada gravada `gemini-t01-a` de #181 (`.eval-work/prompt1/`).
- **Sinal**: AUC do `noul` "um curador guardaria?" contra o rótulo humano, com IC95. O `--out-key` gera um gabarito para `scripts/compare_gold_runs.py`.
- **Replay**: respostas gravadas em `.eval-work/jev-gold/`, chaveadas por tarefa, idioma, modelo, permutações e perguntas. Rodada gravada não chama nada.
- **Medição, não porteira**: a ADR-049 continua valendo.

## Planilha cega de dedupe e categoria

Dedupe e categoria não têm gabarito humano: o m/d do revisor só cobre o que o LLM já marcou como repetição, e ninguém julga a categoria de um cluster. Depois de acumular decisões shadow (`zettel review`, `zettel garden`), exporte:

```bash
.venv/Scripts/python.exe scripts/export_decision_gold.py --site dedupe
.venv/Scripts/python.exe scripts/export_decision_gold.py --site moc_category
```

- **Mesma evidência que os modelos**: cada item vem do `state_json` da linha shadow — o candidato e as notas da mesma fonte que o dedupe viu, ou as notas e termos do cluster.
- **Cega**: nada de decisão do LLM, resposta do Jev, confiança ou rótulo do revisor na planilha ou na leitura; itens embaralhados. As notas existentes aparecem por **letra** (A, B, …); as categorias, por **número**.
- **Respostas**: dedupe — `decisao` = `nova` | `repete` | `desenvolve` | `?`, e `alvo` = letra da nota em `repete`/`desenvolve`; categoria — número ou nome da lista, `nenhuma` ou `?`.
- **Estratos**: dedupe pela decisão do LLM (`create_new` amostrado, `ignore`/`link` inteiros até o teto); categoria por concordância entre o Jev e o argmax (`agree`/`disagree`/`unassigned`). `--per-stratum` (padrão 30) e `--seed` controlam a amostra; o gabarito guarda a população de cada estrato.
- **Commitável**: só o `*-GABARITO-NAO-ABRIR.json` (ids, sem texto). Planilha e leitura são gitignored. Uma planilha existente nunca é sobrescrita sem `--force`.

## Relatório do shadow (offline)

```bash
.venv/Scripts/python.exe scripts/report_decision_shadow.py --out evals/results/decision-shadow.json
```

Lê `decision_shadow` do `state.db` e reporta, por site, a concordância com a decisão atual por faixa de confiança (≥ 0,9; 0,6–0,9; < 0,6), a concordância com o revisor no dedupe ao lado da do LLM, o desvio entre permutações e a latência. O rótulo humano do dedupe só existe para o que o LLM já marcou como redundante — leia o número com esse viés.
