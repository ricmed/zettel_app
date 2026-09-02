# Instalação e primeiros passos

[← Voltar ao README](../README.md)

Como sair do zero até o primeiro `harvest`: pré-requisitos, dependências via **uv**, chaves de API e inicialização do vault.

---

## Pré-requisitos

| Requisito | Detalhe |
|---|---|
| **Python 3.12+** | Declarado em [`pyproject.toml`](../pyproject.toml) (`requires-python = ">=3.12"`). Versões anteriores não são suportadas. |
| **[uv](https://docs.astral.sh/uv/)** | Gerenciador de dependências e ambiente do projeto. Todo o lockfile ([`uv.lock`](../uv.lock)) é mantido por ele. |
| **Chave de API de LLM** | OpenAI por padrão. Também dá para rodar 100% local com Ollama — veja [configuracao.md](configuracao.md#provedores-de-llm-suportados). |
| **GPU NVIDIA** (opcional) | Acelera Docling (OCR/layout de PDF) e embeddings locais. O projeto já fixa o índice CUDA 12.6 do PyTorch. |

> O projeto é gerenciado exclusivamente por **uv**. Não use `pip install` na raiz do projeto: o ambiente e as versões vêm do `pyproject.toml` + `uv.lock`.

---

## Passo a passo

### 1. Clone o repositório

```bash
git clone <repo-url>
cd zettel_app
```

### 2. Instale as dependências

```bash
uv sync
```

O `uv sync` cria o ambiente virtual em `.venv/`, resolve o `uv.lock` e instala tudo — inclusive `torch`/`torchvision` a partir do índice CUDA declarado no próprio `pyproject.toml`:

```toml
[[tool.uv.index]]
name = "pytorch-cu126"
url = "https://download.pytorch.org/whl/cu126"
explicit = true
```

Ou seja: **não é preciso instalar o PyTorch à mão**. Em Linux/Windows o build CUDA 12.6 é usado automaticamente; em outras plataformas o marcador de ambiente do `pyproject.toml` simplesmente não instala o torch.

Se você não tem GPU (ou não quer usá-la), deixe a instalação como está e ajuste apenas a configuração:

```yaml
# config/config.yaml
device: cpu     # auto | cpu | cuda
```

### 3. Configure as chaves de API

```bash
cp .env.example .env
```

Edite o `.env` na raiz do projeto:

```env
OPENAI_API_KEY=sk-sua-chave-aqui
```

Variáveis reconhecidas:

| Variável | Quando é necessária |
|---|---|
| `OPENAI_API_KEY` | `provider: openai` e gateways OpenAI-compatible (OpenRouter, OpenCode, vLLM, LM Studio…) |
| `ANTHROPIC_API_KEY` | `provider: anthropic` |
| `GOOGLE_API_KEY` | `provider: gemini` |
| `SESSION_SECRET` | Login da [interface web](interface-web.md). Lido do ambiente do processo (`os.environ`), **não** do `config.yaml` |
| `ZETTEL_CONFIG` | Opcional: aponta `create_app()` / `WebApplication` para um YAML alternativo (usado nos testes) |

O sistema carrega o `.env` automaticamente ao iniciar (via `python-dotenv`) — **não** é preciso exportar as variáveis no shell. Segredos nunca vão para o `config.yaml`.

### 4. Inicialize o sistema

```bash
uv run python -m zettel init
```

Cria a estrutura do vault (`00_Inbox/`, `10_Sources/`, `20_Literature/`, `30_Permanent/`, `40_MOCs/`, `90_Assets/`), o SQLite (`data/state.db`) e o diretório do ChromaDB (`data/chroma/`).

> `zettel init` **recria o vault vazio** e apaga `./vault`. State DB, Chroma e cache são preservados. Para zerar tudo (inclusive banco e índice), use `zettel init --reset`, que pede confirmação.

### 5. Verifique a instalação

```bash
uv run python -m zettel doctor
```

O `doctor` valida configuração, dependências (Docling, clustering, FTS5), cobertura de capítulos e o espaço de embedding ativo — é a forma mais rápida de descobrir que faltou uma chave de API ou que o SQLite foi compilado sem FTS5.

### 6. Rode o pipeline

```bash
cp meu_artigo.pdf data/inbox/
uv run python -m zettel run-all
```

Depois aponte o Obsidian para a pasta `./vault`. A referência completa de comandos está em [cli.md](cli.md).

---

## Executando os comandos

Todos os comandos passam pelo módulo `zettel`. Duas formas equivalentes:

```bash
# Via uv (recomendado — não exige ativar o venv)
uv run python -m zettel <comando>

# Via interpretador do venv (Windows)
.venv/Scripts/python.exe -m zettel <comando>

# Via interpretador do venv (Linux/macOS)
.venv/bin/python -m zettel <comando>
```

Os exemplos da documentação usam `python -m zettel <comando>` de forma abreviada; prefixe com `uv run` se o ambiente não estiver ativado.

---

## Dependências opcionais

A maior parte do que antes era opcional já entra no `uv sync`, porque é usada pelo caminho padrão do pipeline:

| Pacote | Situação | Para quê |
|---|---|---|
| `docling` | **Obrigatório** (pinado em `2.123.1`) | Única via de extração de PDF — sem fallback ([ADR-012](adrs/generated/HARVEST/ADR-012-docling-pdf-extraction-pymupdf-fallback.md)) |
| `umap-learn`, `hdbscan` | **Já instalados** | Clusterização densa dos MOCs (`zettel garden`) |
| `langchain-ollama` | **Já instalado** | LLM e embeddings locais via Ollama |
| `langchain-google-genai` | **Já instalado** | Provider `gemini` |
| `langgraph` | **Já instalado** | Orquestração do `zettel article` |
| `litellm` | **Já instalado** | Apenas mapa de preços (`cost_per_token`); não é cliente de LLM |
| `langchain-anthropic` | **Opcional** — adicione se for usar Claude | Provider `anthropic` |

Para adicionar o provider Anthropic:

```bash
uv add langchain-anthropic
```

Para usar **Ollama** (LLM ou embeddings locais), o pacote Python já está instalado, mas o servidor precisa estar rodando e o modelo puxado:

```bash
ollama pull qwen3-embedding
```

---

## Testes

```bash
# Suite completa
uv run pytest tests/ -v

# Um arquivo
uv run pytest tests/test_hashing.py -v

# Uma função
uv run pytest tests/test_hashing.py::test_normalize_collapses_whitespace -v

# Interface web
uv run pytest tests/test_web.py tests/test_web_state.py -v
```

---

## Próximos passos

- [Configuração](configuracao.md) — catálogo completo do `config.yaml`, provedores de LLM e embedding
- [Comandos (CLI)](cli.md) — referência de todos os comandos e flags
- [Pipeline](pipeline.md) — o que cada fase faz, em detalhe
- [Solução de problemas](troubleshooting.md) — erros comuns e como sair deles
