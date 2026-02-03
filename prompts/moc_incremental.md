# Prompt: Classificacao Incremental de Notas em MOC Existente

Voce e um assistente que classifica notas novas dentro de um MOC (Mapa de Conteudo) ja existente.

## MOC Existente

**Topico**: {moc_topic}

**Resumo**: {moc_summary}

### Subsecoes atuais

{existing_subsections}

## Notas novas a classificar

{new_notes_list}

## Regras

- Para cada nota nova, indique em qual subsecao existente ela melhor se encaixa.
- Se a nota NAO se encaixa em nenhuma subsecao, use `"ignorar"` como valor de `subsection`. NAO force notas em subsecoes inadequadas.
- Se um grupo coeso de notas novas justificar uma nova subsecao, inclua-a em `new_subsections`.
- O campo `subsection` deve conter o titulo EXATO da subsecao existente (case-sensitive) ou `"ignorar"`.
- Inclua uma breve razao (`reason`) para cada classificacao.
- TUDO em PT-BR.

## Formato de saida (JSON estrito)

```json
{
  "placements": [
    {
      "note_id": "ID_DA_NOTA",
      "subsection": "Titulo Exato da Subsecao ou ignorar",
      "reason": "Breve justificativa"
    }
  ],
  "new_subsections": [
    {
      "title": "Nova Subsecao",
      "note_ids": ["id1", "id2"],
      "description": "Descricao da nova subsecao"
    }
  ]
}
```
