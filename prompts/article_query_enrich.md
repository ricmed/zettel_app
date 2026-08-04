# Expansao de queries para pesquisa no vault

Voce recebe um tema de artigo e gera queries de busca semantica curtas
para recuperar notas permanentes relevantes no acervo Zettelkasten.

## Idioma

Queries em **{language}**.

## Tema

{topic}

## Estilo do artigo

`{style}`

## Quantidade alvo

Gere cerca de **{count}** queries distintas (facetas diferentes do tema).

## Queries extras do usuario (prioridade)

{extra_queries}

Se houver queries extras, **inclua-as primeiro** (normalizadas) e complete
com facetas complementares ate o total alvo. Nao duplique.

## Regras

1. Cada query deve ser curta (3-10 palavras), especifica e pesquisavel.
2. Cubra angulos diferentes (definicao, tecnicas, trade-offs, exemplos, limites).
3. Nao invente nomes de obras ou autores; foque em conceitos.
4. Responda **apenas** com JSON:

```json
{"queries": ["query 1", "query 2"]}
```
