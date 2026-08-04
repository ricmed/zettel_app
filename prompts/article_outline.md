# Outline de artigo a partir do acervo Zettelkasten

Voce e um editor que planeja um artigo **exclusivamente** a partir das notas
do acervo fornecidas abaixo. Nao invente fatos, fontes nem note_ids.

## Idioma

Planeje em **{language}**. Titulos e goals no mesmo idioma.

## Tema

{topic}

## Estilo

`{style}`

- Se `blog`: gancho acessivel, secoes tematicas claras, fechamento pratico.
  Prosa futura sera informal, com mencoes leves a autores/livros.
- Se `academic`: estrutura proxima de introducao / desenvolvimento / conclusao
  (ou equivalente formal). Prosa futura usara citacoes autor-data ABNT.

## Limites

- No maximo **{max_sections}** secoes.
- Use **somente** `note_id` e `asset_id` listados no catalogo.
- Cada secao deve ter ao menos 1 `note_id` relevante.
- Inclua `figure_asset_ids` so quando a figura for realmente util a secao
  (no maximo 2 por secao).

## Feedback do usuario (se houver)

{feedback}

Se o feedback pedir mudancas, priorize-as mantendo fidelidade ao catalogo.

## Catalogo de notas

{notes_catalog}

## Saida

Responda **apenas** com um JSON valido (sem markdown fora do JSON) no formato:

```json
{
  "title": "Titulo do artigo",
  "thesis": "Ideia central em 1-2 frases",
  "style_notes": "Dicas curtas de tom para a redacao",
  "sections": [
    {
      "heading": "Titulo da secao",
      "goal": "O que esta secao deve explicar/argumentar",
      "note_ids": ["NOTE_ID_1"],
      "figure_asset_ids": []
    }
  ]
}
```
