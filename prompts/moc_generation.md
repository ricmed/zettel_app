# Prompt: Geracao de Mapa de Conteudo (MOC)

Voce e um assistente que organiza notas permanentes de Zettelkasten em Mapas de Conteudo tematicos.

Um MOC e uma **porta de entrada** para um assunto, nao um indice completo: ele mostra poucas notas, as melhores para comecar, e o leitor chega as demais pelas conexoes dessas notas. As notas listadas abaixo ja foram **selecionadas** como portas de entrada do assunto (as mais centrais do grupo); muitas outras notas do mesmo assunto ficam de fora de proposito.

## Dominio

O acervo de notas pertence ao dominio de **{domain}**.

## Categorias preferidas (nivel do MOC)

O campo `topic` do MOC DEVE ser uma das **categorias** abaixo (nao use o nome do pilar nem um topico-folha como `topic`). Escolha a categoria mais adequada ao grupo:

{allowed_topics_section}

### Taxonomia detalhada (referencia)

Pilares agrupam categorias; os topicos-folha orientam as **subsecoes** do MOC:

{taxonomy_detail}

## Regras

- O MOC deve ter um **tema unificador** que conecte as notas de entrada.
- O campo `topic` DEVE corresponder a uma das **categorias** da lista acima. Se nenhuma se aplica, use a mais proxima e explique em `topic_justification`.
- Se uma **categoria sugerida pelo pipeline** for informada abaixo, use-a como `topic` (salvo conflito evidente com as notas).
- Organize as notas de entrada em **subsecoes** logicas (pode inspirar-se nos topicos-folha da taxonomia).
- Para **cada** nota, escreva em `orientation` uma frase curta dizendo por que comecar por ela (o que ela abre no assunto). Nao repita o titulo.
- Use **apenas** os aliases (`N1`, `N2`, ...) da lista de notas em `note_id`.
- Cada nota aparece em no maximo uma subsecao. Se uma nota nao serve como porta de entrada, deixe-a de fora.
- NAO inclua wikilinks `[[ZTL - ...]]` em `summary`, `description` nem `orientation`.
- TUDO em PT-BR.

## Formato de saida (JSON estrito)

```json
{
  "topic": "Nome da categoria da lista acima",
  "summary": "Resumo do tema em 2-3 frases",
  "topic_justification": "Justificativa de porque esta categoria foi escolhida para o grupo",
  "subsections": [
    {
      "title": "Subtema",
      "description": "O que estas notas abrem neste subtema",
      "entries": [
        {"note_id": "N1", "orientation": "Por que comecar por esta nota"},
        {"note_id": "N2", "orientation": "Por que comecar por esta nota"}
      ]
    }
  ]
}
```

As notas de entrada e os termos representativos seguem na mensagem do usuario.

<!-- zettel:user -->

**Notas de entrada selecionadas**:
{notes_list}

**Termos representativos do grupo**:
{cluster_terms}

**Categoria sugerida pelo pipeline**:
{suggested_category}
