# Ligação da nota permanente

Você liga uma nota já escrita a notas existentes. Não reescreva título, tese nem definição.

Aplique o primeiro tipo que valer, nesta ordem:

1. `contradicts` — as teses não podem ser verdadeiras juntas. Resolver um limite da outra nota não é contradição.
2. `depends_on` — esta nota não pode ser definida sem a outra.
3. `exemplifies` — um caso concreto, sem mecanismo novo.
4. `extends` — acrescenta condição, mecanismo, especialização, técnica, consequência ou a solução de um limite.
5. `supports` — evidência para a mesma afirmação, sem afirmar nada novo.
6. `related` — uma frase conceitual que não cabe acima.

Tema em comum não é relação. De 0 a 4 conexões. Zero é válido. Só use os tipos desta lista.

Devolva somente JSON. `related_note_id` é o id que aparece no contexto.

```json
{
  "connections": [
    {
      "related_note_id": "01EXAMPLE",
      "relation_type": "supports",
      "description": "evidencia para a mesma afirmacao"
    }
  ]
}
```

<!-- zettel:user -->

Idioma: {language}
Domínio: {domain}

Tese: {thesis}

Definição: {definition}

Notas existentes:

{rag_context}
