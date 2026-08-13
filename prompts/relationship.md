# Prompt: Classificação de Relacionamento entre Notas

<!-- NOTA: Os tipos de relação abaixo estão integrados diretamente no prompt
     permanent_note.md. Este arquivo serve como referência e documentação. -->

Analise a relação entre a nota atual e cada nota candidata a conexão.

## Tipos de relação

- `supports`: a nota candidata reforça ou valida a tese da nota atual
- `contradicts`: a nota candidata contradiz ou tensiona a tese
- `extends`: a nota candidata amplia ou aprofunda o conceito
- `depends_on`: a nota atual depende conceitualmente da candidata
- `exemplifies`: uma serve como exemplo prático da outra
- `related`: relação temática clara mas não se encaixa nas anteriores

## Formato de saída (JSON — lista)

```json
[
  {
    "related_note_id": "ID da nota",
    "relation_type": "supports | contradicts | extends | depends_on | exemplifies | related",
    "description": "Breve descrição da relação"
  }
]
```

A nota atual e as candidatas seguem na mensagem do usuario.

<!-- zettel:user -->

**Nota atual**:
- Título: {current_title}
- Tese: {current_thesis}

**Notas candidatas**:
{candidate_notes}
