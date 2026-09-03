# Zettelkasten — Pipeline Automatizado de Geração de Notas

![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)
![uv](https://img.shields.io/badge/gerenciado%20com-uv-DE5FE9?logo=uv&logoColor=white)
![Obsidian](https://img.shields.io/badge/saída-Obsidian-7C3AED?logo=obsidian&logoColor=white)
![LangChain](https://img.shields.io/badge/LLM-LangChain%20%2B%20LangGraph-1C3C3C)
![Stores](https://img.shields.io/badge/stores-SQLite%20FTS5%20%2B%20ChromaDB-003B57?logo=sqlite&logoColor=white)
![ADRs](https://img.shields.io/badge/ADRs-31%20decisões-0A7EA4)
![Licença](https://img.shields.io/badge/licença-MIT-green)

Sistema em Python que lê arquivos (PDF, Markdown) e gera **Notas de Literatura** e **Notas Permanentes** seguindo rigorosamente o método Zettelkasten, com saída compatível com **Obsidian**.

```
Arquivo (PDF/MD)
    ↓ harvest
Texto extraído → Chunks (com pagina) → SRC + indice LIT
    ↓ extract
Drafts de LIT granular (1 por chunk) em 00_Inbox/Review
    ↓ review
LIT aprovadas em 20_Literature/{Citekey}/ + literature_notes no Chroma
    ↓ connect
Notas Permanentes (ZTL) com links e backlinks
    ↓ garden
MOCs (Mapas de Conteúdo) por clusterização semântica
```



### Princípios

- **Atomicidade real**: cada nota permanente = uma tese + explicação autônoma + limites
- **Rastreabilidade**: toda nota aponta para sua fonte (literatura + localizador)
- **Autonomia**: notas permanentes são compreensíveis sem consultar a fonte
- **Conectividade intencional**: links apenas quando há relação clara
- **Não-regressão**: atualizações automáticas não destroem edições manuais (blocos gerenciados)
- **Resistência a drift**: hashes em camadas + IDs estáveis + cache de LLM

---



## Documentação


| Guia                                            | O que você encontra                                                                      |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------- |
| [Instalação](docs/instalacao.md)                | Pré-requisitos, `uv sync`, `.env`, GPU, `init`, testes                                   |
| [Configuração](docs/configuracao.md)            | Catálogo completo do `config.yaml`, provedores de LLM/embedding, caches, troca de modelo |
| [Comandos (CLI)](docs/cli.md)                   | Referência de **todos** os comandos e flags                                              |
| [Arquitetura](docs/arquitetura.md)              | Mapa dos módulos, estrutura do vault, anti-drift, IDs estáveis, custos                   |
| [Pipeline](docs/pipeline.md)                    | O que cada fase faz por dentro, incluindo paginação arquivo vs. impressa                 |
| [Notas geradas](docs/notas.md)                  | Formato de SRC, índice LIT, LIT granular e ZTL; tipos documentais ABNT                   |
| [Recuperação](docs/recuperacao.md)              | Busca híbrida (vetor + BM25 + RRF), GraphRAG, piso de relevância, `ask` e `article`      |
| [Notas manuais](docs/notas-manuais.md)          | `new-note`, `sync-manual`, adoção de LIT e de imagens, caminho LIT → ZTL                 |
| [Interface web](docs/interface-web.md)          | Subir a UI, páginas, fila de jobs, o que é exclusivo da CLI                              |
| [Operação](docs/operacao.md)                    | Retenção, `reindex`/`rebuild`/`rechunk`, dumps, purga, remoção de fonte, backup          |
| [Solução de problemas](docs/troubleshooting.md) | Sintomas comuns e como sair deles                                                        |
| [Prompts e taxonomia](docs/prompts.md)          | Personalizar `prompts/`, `moc_topics.yaml` e as personalidades do `article`              |
| [Avaliação do `ask`](evals/README.md)         | Replay offline de trajetórias, veredictos e guardrail de afirmações       |
| [ADRs](docs/adrs/ADR-INDEX.md)                  | 38 decisões de arquitetura, com contexto e alternativas                                  |


Índice completo em [docs/INDICE.md](docs/INDICE.md).

---



## Início rápido

Requisitos: **Python 3.12+** e **[uv](https://docs.astral.sh/uv/)**.

```bash
# 1. Dependências (cria .venv, resolve uv.lock, instala torch com CUDA quando aplicável)
uv sync

# 2. Chave de API
cp .env.example .env        # edite: OPENAI_API_KEY=sk-...

# 3. Vault + bases
uv run python -m zettel init

# 4. Ingestão
cp meu_artigo.pdf data/inbox/
uv run python -m zettel run-all

# 5. Abra a pasta ./vault no Obsidian
```

Passo a passo completo (GPU, provedores locais, dependências opcionais): [docs/instalacao.md](docs/instalacao.md).

Os exemplos abaixo usam `python -m zettel ...`; prefixe com `uv run` se o ambiente não estiver ativado.

---



## Comandos principaismanu


| Comando                           | O que faz                                                                    |
| --------------------------------- | ---------------------------------------------------------------------------- |
| `zettel init`                     | Cria o vault e as bases (`--reset` apaga também SQLite/Chroma/cache)         |
| `zettel harvest`                  | Escaneia o inbox, extrai texto, cria SRC + índice LIT + chunks com página    |
| `zettel extract`                  | Prompt 1: gera drafts de LIT granular em `00_Inbox/Review`                   |
| `zettel review`                   | Portão humano: aprova/rejeita os drafts por faixa de confiança               |
| `zettel connect`                  | Prompt 2: gera notas permanentes (ZTL) com links e backlinks                 |
| `zettel garden`                   | Clusteriza notas e gera/atualiza MOCs (`--hubs` para MOCs ancorados em hubs) |
| `zettel ask "..."`                | QA sobre o vault com recuperação híbrida + grafo, sempre citando as notas    |
| `zettel article "..."`            | Artigo longo a partir do vault, com outline interativo                       |
| `zettel skill`                    | Exporta um recorte aprovado do vault como Agent Skill plana                  |
| `zettel new-note`                 | Scaffold de nota manual (`ztl`/`src`/`lit`/`moc`)                            |
| `zettel sync-manual`              | Adota notas escritas à mão no Obsidian (índice, grafo, backrefs)             |
| `zettel status` / `zettel doctor` | Estatísticas do pipeline / diagnóstico de config e dependências              |
| `zettel run-all`                  | Pipeline completo, do inbox aos MOCs                                         |


Fluxo típico do dia a dia:

```bash
python -m zettel harvest      # ingere o que está em data/inbox/
python -m zettel extract      # gera os drafts de literatura
python -m zettel review       # você aprova o que vale a pena
python -m zettel connect      # vira nota permanente
python -m zettel garden       # atualiza os mapas de conteúdo
```

Todas as flags de cada comando: [docs/cli.md](docs/cli.md).

> **Pré-voo de custo.** Antes de gastar LLM, `extract`, `connect` e `article` mostram um painel com o modelo da fase, a contagem de itens, os tokens estimados e o custo em USD, e pedem confirmação. `--yes` ou stdin sem TTY (scripts, CI) seguem direto; recusar aborta **antes** de qualquer chamada. É estimativa e limite de ordem de grandeza, não teto de orçamento: o cache de respostas do SQLite não é descontado, então o número é um limite superior. Detalhes em [ADR-037](docs/adrs/generated/CLI/ADR-037-llm-cost-preflight-estimate.md).

> **Índice de tópicos.** Cada índice LIT e cada MOC ganham um bloco `auto-topic-index` mapeando **termo → nota** (frameworks nomeados, depois tags, e a cabeça da tese como último recurso). É roteamento, não representação: quando a pergunta casa com um termo, a nota entra na busca do `ask` como semente extra **carregando distância vetorial real**, e passa pelo mesmo piso de relevância que qualquer outra — estar no índice não fura o piso. Desligue com `retrieval.topic_index_boost: false`. Detalhes em [ADR-036](docs/adrs/generated/RETRIEVAL/ADR-036-topic-index-routing-not-representation.md).

> **Exportar para um agente.** `zettel skill --source-id @Citekey` (ou `--moc-id`, ou `--topic`) projeta um recorte **já aprovado** do vault como [Agent Skill](https://code.claude.com/docs/en/skills) plana em `<vault>/.claude/skills/<slug>/`: um `SKILL.md` pequeno com Core + Topic Index + Note Index, mais `notes/`, `cheatsheet.md` e `glossary.md` que o agente abre sob demanda. É projeção determinística — **não** chama LLM e não grava nada nas bases. O trecho da fonte fica de fora por padrão (pack publicável); citekey, localizador e teses permanecem. Detalhes em [ADR-035](docs/adrs/generated/CLI/ADR-035-flat-agent-skill-export.md).

> **Julgamento do autor.** Quando o trecho *enuncia* como o autor decidiria, a extração registra `decision_rules` ("Quando X, faça Y, porque Z"), `anti_patterns` e `named_frameworks` (o nome exato do autor, sem tradução). São **opcionais**: um trecho que só define um conceito continua sendo um candidato válido, e nada no pipeline depende desses campos. A LIT ganha o bloco `auto-decision`; a ZTL carrega as listas no frontmatter. Detalhes em [ADR-034](docs/adrs/generated/EXTRACT/ADR-034-optional-author-judgement-fields.md).

> **Higiene da extração.** Antes de chamar o Docling, o `harvest` verifica a camada de texto das 3 primeiras páginas do PDF; um arquivo escaneado é recusado na hora, com a sugestão de rodar OCR (`ocrmypdf entrada.pdf saida.pdf`), sem gastar conversão. O texto extraído passa por uma limpeza de Unicode invisível (zero-width, marcas bidi, bloco de tags) **antes** do checksum de extração, para que nada invisível ao revisor chegue ao prompt, ao vault ou ao embedding. Um arquivo ruim não derruba o lote: os demais são processados e os recusados aparecem no fim com o motivo (a CLI sai com código 1). Fontes já colhidas mantêm o checksum antigo até um novo `harvest`. Detalhes em [ADR-033](docs/adrs/generated/HARVEST/ADR-033-invisible-unicode-sanitization-and-text-layer-probe.md).

> **Chunking e fences.** No `harvest`, um bloco cercado CommonMark (`````/`~~~`) é uma **unidade atômica**: nunca é cortado, e os headings dentro dele não viram `section_path`. Fence maior que `chunk_size` gera um chunk *oversized* de propósito. O heading ATX da seção entra **só no primeiro chunk** daquela seção (depois do split por fences); as continuações seguem só com o corpo. Fontes antigas só mudam com `zettel rechunk`. Detalhes em [docs/pipeline.md](docs/pipeline.md#chunking) e [ADR-014](docs/adrs/generated/HARVEST/ADR-014-hybrid-structural-chunking-strategy.md).

---



## Estrutura do vault

```
vault/
├── 00_Inbox/                # Entrada; respostas do `ask` e artigos salvos
│   └── Review/              # Drafts de LIT granular (aguardando aprovacao)
├── 10_Sources/              # Notas bibliograficas (SRC)
├── 20_Literature/           # Indice LIT (raiz) + {Citekey}/ com as LIT granulares
├── 30_Permanent/            # Notas permanentes (ZTL)
├── 40_MOCs/                 # Mapas de Conteudo
└── 90_Assets/               # Imagens extraidas de PDFs/Markdown (nome por hash)
```

Nomes seguem `PREFIXO - IDENTIFICADOR - slug.md` (SRC/LIT usam `AuthorYear`; ZTL/MOC usam ULID). O `@` vive em `source_id` e na CLI, nunca em caminhos.

Mapa dos módulos, blocos gerenciados e camadas anti-drift: [docs/arquitetura.md](docs/arquitetura.md). Exemplos completos de cada nota: [docs/notas.md](docs/notas.md).

---



## Interface web

```bash
uvicorn zettel.web:app --host 0.0.0.0 --port 5000
```

- App FastAPI server-rendered (Jinja2), sem Node nem bundler — não há subcomando `zettel web`.
- Login por `SESSION_SECRET` (variável de ambiente, não vai no `config.yaml`), cookie assinado por HMAC e CSRF em todo POST.
- Instância única com fila de jobs no SQLite: **um** trabalho mutante por vez; operações destrutivas continuam só na CLI.

Páginas, operações enfileiráveis e recuperação após reinício: [docs/interface-web.md](docs/interface-web.md).

---



## Configuração

A **fonte operacional** é `[config/config.yaml](config/config.yaml)` — é o arquivo que o CLI e a web carregam. `[zettel/config.py](zettel/config.py)` define o schema Pydantic e só aplica fallback quando o YAML falta ou omite uma chave. Segredos (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `SESSION_SECRET`) ficam no `.env`.


| Bloco                                                                                    | Controla                                                                                                                             |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `vault_path`, `inbox_path`, `chroma_path`, `state_db_path`, `cache_path`, `prompts_path` | Caminhos do projeto                                                                                                                  |
| `llm.*`                                                                                  | Identidade de LLM **por fase** (`harvest`, `extract`, `review`, `connect`, `garden`, `ask`, `article`, `images`) + amostragem global |
| `embedding.*`                                                                            | Provider, modelo, `dimensions` (MRL) e política de fallback                                                                          |
| `chunking.*`, `harvest.*`, `extraction.*`, `literature_review.*`                         | Ingestão, duplicatas, filtragem de candidatos e portão de aprovação                                                                  |
| `retrieval.*`                                                                            | Busca híbrida, piso de relevância, expansão por grafo, `ask` e `article`                                                             |
| `gardener.*`, `hub_mocs.*`                                                               | Clusterização e geração de MOCs                                                                                                      |
| `images.*`                                                                               | Extração e descrição multimodal de imagens                                                                                           |
| `language`, `log_level`, `device`                                                        | Idioma do conteúdo gerado, logging e dispositivo (CPU/CUDA)                                                                          |


Catálogo completo, provedores suportados e o procedimento de troca de embedding: [docs/configuracao.md](docs/configuracao.md).

---



## Testes

```bash
# Suite completa
uv run pytest tests/ -v

# Um arquivo / uma função
uv run pytest tests/test_hashing.py -v
uv run pytest tests/test_hashing.py::test_normalize_collapses_whitespace -v

# Interface web
uv run pytest tests/test_web.py tests/test_web_state.py -v

# Avaliacao do ask (offline: sem LLM, sem rede)
uv run pytest tests/evals/ -v
```

Problemas conhecidos e diagnósticos: [docs/troubleshooting.md](docs/troubleshooting.md).

---



## Licença

MIT