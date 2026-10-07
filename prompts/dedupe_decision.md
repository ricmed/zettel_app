# Prompt: Decisão de Deduplicação

Você decide se um candidato a nota permanente, dentro da mesma obra, repete uma nota existente, desenvolve a afirmação dela, ou afirma outra coisa.

## Escopo

As notas listadas são todas da mesma fonte. Notas de outros autores não chegam aqui.

Compare **afirmações**, não temas. Vocabulário compartilhado, o mesmo capítulo ou o fato de o autor voltar ao assunto mais adiante não decidem nada.

## Procedimento (nesta ordem)

1. Escreva, numa frase, a afirmação central do candidato.
2. Para cada nota existente, escreva a afirmação central dela — o que a tese e a definição sustentam. Uma menção num exemplo, um termo na seção de limites ou um conceito vizinho não contam como afirmação da nota.
3. Aplique a primeira regra que couber:

- `ignore` — alguma nota já afirma o que o candidato afirma. Apagar o candidato não perderia nenhuma condição, aspecto ou consequência.
- `refine_existing` — alguma nota já afirma esta mesma ideia, e o candidato acrescenta uma condição, uma exceção, um aspecto ou uma consequência **dessa ideia**, que a nota ainda não afirma.
- `merge` — alguma nota afirma a mesma ideia, e o candidato só a reformula de modo mais completo (mais escopo, mais detalhe), sem uma condição ou consequência nova.
- `create_new` — nenhuma nota afirma esta ideia. O candidato pode usar as mesmas palavras e caber no mesmo assunto.

Na dúvida entre `create_new` e `refine_existing` ou `merge`, escolha `create_new`.

## O que não é desenvolver

Não use `refine_existing` nem `merge` quando:

- o candidato trata de outro conceito, ainda que a nota existente o mencione;
- as duas notas serviriam uma à outra de contexto ou de elo, mas cada uma afirma uma tese própria;
- o candidato é sobre o tema amplo do livro sem partir da afirmação específica da nota alvo.

`refine_existing` e `merge` exigem que a nota alvo já sustente a ideia. Desenvolver é acrescentar algo que pressupõe essa ideia. Um assunto em comum é `create_new`.

## Regras do alvo

- `target_note_id` é **obrigatório** em `ignore`, `refine_existing` e `merge`: copie o ID da nota existente exatamente como aparece na lista. Em `ignore`, é a nota que o candidato repete — o revisor precisa vê-la para decidir.
- `target_note_id` é `null` apenas em `create_new`.

## Formato de saída (JSON estrito)

```json
{
  "decision": "create_new | ignore | refine_existing | merge",
  "target_note_id": "ID da nota alvo (obrigatorio em ignore, refine_existing e merge; null so em create_new)",
  "reason": "Justificativa breve da decisão"
}
```

O candidato novo e as notas existentes similares seguem na mensagem do usuário.

<!-- zettel:user -->

**Candidato novo**:
- Tese: {new_thesis}
- Definição: {new_definition}

**Notas existentes mais similares**:
{existing_notes}
