# Prompts e taxonomia de MOCs

[← Voltar ao README](../README.md)

Como personalizar o que o LLM recebe: os templates em `prompts/` e a taxonomia de tópicos em `config/moc_topics.yaml`.

---

## Personalização dos prompts

Os prompts em [`prompts/`](../prompts) são templates Markdown com placeholders `{variavel}`. Você pode editá-los para ajustar:

- **Estilo das notas**: mais academico, mais informal, etc.
- **Idioma**: `{language}` ja e preenchido a partir de `language` no config em `ask.md`, `article_*.md`, `literature_note.md`, `permanent_note.md` e `image_description.md` — altere o config, nao o texto do prompt
- **Profundidade**: mais ou menos detalhes por nota
- **Tags**: criterios para sugestao de tags
- **Seletividade**: regras de relevancia e filtragem em `literature_note.md`
- **Imagens → candidatos/ZTL**: criterios de `relevant_image_ids` e extracao a partir de diagramas em `literature_note.md`; tom da descricao em `image_description.md`; uso de figuras no Prompt 2 em `permanent_note.md`
- **Taxonomia de MOCs**: edite `config/moc_topics.yaml` (pilares, categorias e topicos)
- **Dominio e categorias**: `{domain}` vem de `gardener.domain` e chega a `moc_generation.md`, `moc_hub_generation.md`, `literature_note.md` e `permanent_note.md`; `{allowed_topics_section}` em `moc_generation.md` vem do YAML da taxonomia
- **Classificacao incremental**: edite `moc_incremental.md` para ajustar como novas notas sao classificadas em MOCs existentes

O sistema detecta automaticamente quando um prompt muda (via `llm_call_checksum`) e reprocessa apenas os artefatos afetados.

> **Contrato prompt <-> codigo.** `tests/test_prompts.py` trava, sem chamar LLM, o que o
> editor de prompt nao pode quebrar: todo template tem o split `<!-- zettel:user -->`
> (menos o fragmento `article_anti_ai.md`), os placeholders usados sao exatamente as
> chaves que o caller passa (lidas do proprio `mapping` via `ast`), nenhum payload
> por chamada vive no lado system (quebraria o cache do provedor), e os exemplos JSON
> validam nos schemas Pydantic que os parsers usam. Rode
> `.venv/Scripts/python.exe -m pytest tests/test_prompts.py -v` depois de editar
> qualquer arquivo de `prompts/`.

### Quem usa qual prompt

| Arquivo | Consumidor |
|---|---|
| `bibliographic_metadata.md` | `harvest` — inferência de metadados ABNT |
| `literature_note.md` | `extract` — Prompt 1 (resumo, conceitos, candidatos, imagens relevantes) |
| `dedupe_decision.md` | `review` — decisão de deduplicação de conceitos |
| `permanent_note.md` | `connect` — Prompt 2 (nota permanente + tipos de relação) |
| `ptbr_guard.md` | `connect` — guardrail de idioma |
| `image_description.md` | `extract` — descrição multimodal de imagens (`llm.images`) |
| `moc_generation.md`, `moc_incremental.md` | `garden` (taxonômico) |
| `moc_hub_generation.md`, `moc_hub_incremental.md` | `garden --hubs` |
| `ask.md` | `ask` |
| `article_query_enrich.md`, `article_outline.md`, `article_section_blog.md`, `article_section_academic.md`, `article_personality.md`, `article_judge.md`, `article_anti_ai.md` | `article` |

O caminho da pasta é configurável em `prompts_path` ([configuracao.md](configuracao.md)).

### O marcador `<!-- zettel:user -->`

Os templates usam `<!-- zettel:user -->` para separar as instruções **estáveis** (que viram `SystemMessage`) do **payload por chamada** (que vira `HumanMessage`). Esse layout é o que viabiliza o prompt caching do provedor — se você editar um prompt, mantenha do lado do sistema apenas o que não muda entre chamadas. Veja [configuracao.md](configuracao.md#prompt-caching-do-provedor-vs-cache-sqlite).

---

## Taxonomia de tópicos para MOCs

O arquivo [`config/moc_topics.yaml`](../config/moc_topics.yaml) é a **fonte única** da taxonomia (pilar > categoria > tópicos). As **categorias** são a whitelist do campo `topic` do MOC; pilares agrupam; tópicos-folha orientam subseções no prompt.

Para personalizar:

1. Edite `config/moc_topics.yaml`
2. Ajuste `gardener.topics_path` em `config/config.yaml` se o arquivo estiver em outro caminho
3. Ajuste `gardener.domain` para refletir a área do seu acervo

Se `strict_topics: true` (padrão), MOCs com `topic` fora das categorias serão rejeitados. Use `strict_topics: false` para permitir tópicos fora da lista (com aviso no log).

A taxonomia também é usada **antes** do LLM: `gardener_assign.py` embedda o rótulo de cada categoria (`category_label_template`, ex. `"{domain}: {categoria}"`) e atribui cada nota ao bucket mais próximo, para então clusterizar dentro dele. Veja [pipeline.md](pipeline.md#fase-4--garden-jardim).

---

## Personalidades do `article`

[`config/personalities.yaml`](../config/personalities.yaml) define os perfis de reescrita estilística usados por `zettel article --personality`. O perfil `neutral` é um no-op: pula a chamada de LLM de reescrita. O caminho do arquivo vem de `retrieval.article.personalities_path`.

---

## Ver também

- [Configuração](configuracao.md) — `prompts_path`, `language`, identidade de LLM por fase
- [Pipeline](pipeline.md) — em que ponto cada prompt é chamado
- [Recuperação](recuperacao.md#gerar-artigo-a-partir-do-vault-zettel-article) — o grafo de nós do `article`
