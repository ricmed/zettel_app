# Instalação e primeiros passos

[← Voltar ao README](../README.md)

Este guia leva do clone ao primeiro `harvest`. São três caminhos:

- **script de setup** (recomendado);
- **passo a passo manual**;
- **Docker**.

Os três terminam no `zettel doctor`, que lista o que ainda falta.

---

## Pré-requisitos

| Requisito | Detalhe |
|---|---|
| **[uv](https://docs.astral.sh/uv/)** | Gerencia o ambiente, as dependências ([`uv.lock`](../uv.lock)) e o próprio Python. É o único requisito obrigatório. |
| **Python 3.12** | Não é preciso instalar à mão: o uv lê o [`.python-version`](../.python-version) e baixa o 3.12 se ele faltar. |
| **Chaves de API** | O [`config/config.yaml`](../config/config.yaml) padrão usa **quatro provedores**. Veja [Chaves de API](#chaves-de-api). |
| **GPU NVIDIA** (opcional) | Acelera o Docling (layout e tabelas de PDF) e os embeddings locais. Sem GPU tudo roda em CPU. |
| **Git** | Para clonar o repositório. |

Opcionais, só para certos usos:

| Ferramenta | Para quê |
|---|---|
| [Obsidian](https://obsidian.md/) | Ler e editar o vault. O `init` instala o plugin Templater; o gatilho de criação de nota precisa ser ligado **em cada dispositivo** ([notas manuais](notas-manuais.md#criar-notas-direto-no-obsidian-templates)). |
| [`ocrmypdf`](https://ocrmypdf.readthedocs.io/) | PDF escaneado: o `harvest` recusa um arquivo sem camada de texto e sugere `ocrmypdf entrada.pdf saida.pdf`. |
| [Ollama](https://ollama.com/) | LLM ou embedding locais, sem chave ([configuração](configuracao.md#provedores-de-llm-suportados)). |
| Node.js | Só para os testes que executam o script dos templates do Obsidian. Sem Node esses testes são pulados. |
| Docker | O [caminho com container](#docker). |

> O projeto é gerenciado exclusivamente por **uv**. Não use `pip install` na raiz: o ambiente e as versões vêm do `pyproject.toml` + `uv.lock`.

---

## Instalar o uv

```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Depois clone o repositório:

```bash
git clone https://github.com/ricmed/zettel_app.git
cd zettel_app
```

---

## Caminho 1: script de setup

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

```bash
# Linux / macOS
scripts/setup.sh
```

O script executa, em ordem:

1. **Instala as dependências.** Com `nvidia-smi` no sistema escolhe o torch CUDA; sem ele, o torch CPU. `-Cpu`/`-Cuda` (PowerShell) ou `--cpu`/`--cuda` (bash) forçam a escolha.
2. **Prepara o `.env`.** Copia o `.env.example` quando o `.env` não existe e gera um `SESSION_SECRET` quando ele está vazio.
3. **Roda `zettel init` e `zettel doctor`.**

Rodar de novo não estraga nada: o `.env` e o segredo já gravados ficam como estão. Depois:

1. Preencha no `.env` as chaves que o `doctor` marcou como **FALHA** (veja [Chaves de API](#chaves-de-api)).
2. Se a chave do embedding (`GOOGLE_API_KEY`) faltava, rode `init` de novo para criar o índice vetorial.
3. Siga para [Rodar o pipeline](#rodar-o-pipeline).

---

## Caminho 2: passo a passo

### 1. Dependências: escolha o torch

O torch entra como dependência do Docling. O extra só escolhe qual build instalar:

```bash
uv sync --extra cpu      # sem GPU NVIDIA (~200 MB de torch)
uv sync --extra cu126    # GPU NVIDIA com driver para CUDA 12.6 (~2,5 GB)
```

No macOS não há extra: `uv sync` instala o torch do PyPI.

Os dois extras são exclusivos, e o `pyproject.toml` declara o conflito. Sem extra, entra o torch do PyPI. Ele funciona, mas no Windows é só CPU e no Linux traz as bibliotecas CUDA 13.

> **Depois de um `uv sync --extra ...`, rode os comandos com `uv run --no-sync`** (ou ative o `.venv`). Um `uv run` simples sincroniza o ambiente **sem** o extra e troca o torch pelo do PyPI. O uv ainda não tem um "extra padrão". Para não repetir a flag, exporte `UV_NO_SYNC=1` no shell.

### 2. Chaves de API

```bash
cp .env.example .env
```

Preencha o `.env` (veja [Chaves de API](#chaves-de-api)) e gere o segredo da interface web:

```bash
uv run --no-sync python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Cole o resultado em `SESSION_SECRET=`. O sistema carrega o `.env` da raiz ao iniciar (via `python-dotenv`), então não é preciso exportar nada no shell.

### 3. Vault e bases

```bash
uv run --no-sync python -m zettel init
```

O `init` cria:

- as pastas do vault (`00_Inbox/` a `90_Assets/`, mais `99_Templates/`);
- o SQLite (`data/state.db`);
- o índice vetorial (`data/chroma/`).

Também configura o Templater no `.obsidian/`. Ele **nunca apaga** o vault: só cria o que falta. `zettel init --reset` recria SQLite, Chroma e cache e remove do vault apenas as notas do pipeline, preservando as suas e o `.obsidian/`.

Sem a chave do embedding, o `init` cria o vault e o SQLite, pula o índice vetorial e avisa qual variável definir. Depois de preencher a chave, rode `init` de novo.

### 4. Diagnóstico

```bash
uv run --no-sync python -m zettel doctor
```

O `doctor` mostra uma linha por fase LLM (`LLM extract`, `LLM connect`...), com o provedor e o modelo, e nomeia a variável que falta (`falta ANTHROPIC_API_KEY no .env`). Também verifica:

- a chave do embedding;
- Docling, torch e GPU;
- FTS5 no SQLite;
- os prompts e a taxonomia;
- o espaço de embedding ativo.

---

## Chaves de API

O `config/config.yaml` padrão escolhe um modelo por fase, medido contra os gold sets (ADR-050 e seguintes). Isso significa quatro provedores:

| Variável | Fases no config padrão | Sem ela |
|---|---|---|
| `OPENAI_API_KEY` | harvest (bibliografia), review (dedupe), garden, images, summarize | Essas fases param |
| `GOOGLE_API_KEY` (ou `GEMINI_API_KEY`) | extract e **embedding** | Sem extract; o `init` não cria o índice vetorial; busca, connect e garden param |
| `ANTHROPIC_API_KEY` | connect | Sem notas permanentes |
| `DEEPSEEK_API_KEY` | ask, article | Sem `ask` e `article` |
| `SESSION_SECRET` | interface web | Sem login. Use 32+ caracteres gerados; `doctor` e `/settings` avisam quando é curto |
| `TYPESAFE_API_KEY` | camada de decisão em shadow (opcional) | O pipeline segue igual |
| `OPENROUTER_API_KEY`, `CHROMA_OPENAI_API_KEY` | só se o `config.yaml` usar esses provedores | — |
| `ZETTEL_CONFIG` | opcional: YAML alternativo para a web | — |

**Para usar menos provedores**, troque `llm.<fase>.provider`/`model` e `embedding.*` no `config/config.yaml`; por exemplo, tudo em `openai`, ou embedding `ollama` local. A troca muda resultados que foram medidos, e mudar o embedding exige `zettel reindex --force` ([configuração](configuracao.md#provedores-de-llm-suportados)).

Segredos nunca vão no `config.yaml`.

---

## Docker

O container roda a interface web e a CLI sem instalar Python, uv ou torch no host.

```bash
cp .env.example .env            # preencha as chaves e o SESSION_SECRET
mkdir -p data vault             # no Linux, para as pastas ficarem com o seu usuario
docker compose build            # torch CPU; TORCH_EXTRA=cu126 docker compose build para GPU
docker compose run --rm zettel python -m zettel init
docker compose up -d            # web em http://localhost:5000
```

A CLI roda no mesmo container: `docker compose run --rm zettel python -m zettel run-all`.

| Montagem | Conteúdo |
|---|---|
| `./data` | `state.db`, Chroma, inbox, cache |
| `./vault` | o vault: abra esta pasta no Obsidian do host |
| `./config` (somente leitura) | `config.yaml` e taxonomia, editáveis sem rebuild |
| volume `model-cache` | cache do HuggingFace, onde o Docling guarda os modelos de PDF |

O container roda como o usuário `zettel` (uid 1000). No Linux, `data/` e `vault/` precisam ser graváveis por esse uid: crie as pastas antes do primeiro `run`, para que não nasçam como root. A imagem GPU (`TORCH_EXTRA=cu126`) precisa do [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/) e de `gpus: all` no serviço.

---

## Rodar o pipeline

```bash
cp meu_artigo.pdf data/inbox/
uv run --no-sync python -m zettel run-all
```

Depois abra a pasta `./vault` no Obsidian. A referência completa de comandos está em [cli.md](cli.md).

No **primeiro harvest de PDF**, o Docling baixa do HuggingFace os modelos de layout e de tabelas. Eles ficam no cache do HF (`HF_HOME`, por padrão `~/.cache/huggingface`) e não são baixados de novo. Numa máquina sem rede, rode esse primeiro harvest antes de desconectar.

---

## Executando os comandos

Todos os comandos passam pelo módulo `zettel`. Três formas equivalentes:

```bash
# Via uv, sem ressincronizar o ambiente (recomendado)
uv run --no-sync python -m zettel <comando>

# Com o ambiente ativado: .venv\Scripts\Activate.ps1 (Windows) ou source .venv/bin/activate
python -m zettel <comando>

# Pelo interpretador do venv
.venv/Scripts/python.exe -m zettel <comando>   # Windows
.venv/bin/python -m zettel <comando>           # Linux/macOS
```

Os exemplos da documentação abreviam para `python -m zettel <comando>`.

---

## Dependências

Tudo o que o pipeline usa já entra no `uv sync`:

| Pacote | Para quê |
|---|---|
| `docling` (pinado em `2.123.1`) | Única via de extração de PDF, sem fallback ([ADR-012](adrs/generated/HARVEST/ADR-012-docling-pdf-extraction-pymupdf-fallback.md)). Traz o torch |
| `chromadb`, `langchain-*` | Índice vetorial e clientes de LLM de todos os provedores suportados (OpenAI, Anthropic, Gemini, Ollama, DeepSeek) |
| `umap-learn`, `hdbscan`, `scikit-learn` | Clusterização dos MOCs (`zettel garden`) |
| `langgraph` | Orquestração do `zettel article` |
| `litellm` | Só o mapa de preços (`cost_per_token`); não é cliente de LLM |
| `fastapi`, `uvicorn`, `jinja2` | Interface web |

O grupo `dev` (`pytest`, `ruff`, `hypothesis` e plugins) também é instalado pelo `uv sync`. `uv sync --no-dev` o deixa de fora, como faz a imagem Docker.

Para usar o **Ollama** (LLM ou embeddings locais), o pacote Python já está instalado; o servidor precisa estar rodando e o modelo puxado:

```bash
ollama pull qwen3-embedding
```

---

## Testes

```bash
# Suíte completa, como o CI (testes lentos incluídos)
uv run --no-sync pytest

# Ciclo de edição: sem os testes lentos (UMAP, Docling, scikit-learn, subprocessos), em paralelo
uv run --no-sync pytest -m "not slow" -n auto

# Cobertura, como o CI roda (falha abaixo do piso em [tool.coverage.report])
uv run --no-sync pytest -n auto --cov

# Um arquivo / uma função
uv run --no-sync pytest tests/test_hashing.py -v
uv run --no-sync pytest tests/test_hashing.py::test_normalize_collapses_whitespace -v

# Interface web: seleção por nome (todo tests/test_web_*.py e os testes que citam a web)
uv run --no-sync pytest tests/ -k web

# Contrato entre as fases: run-all sobre um Markdown, LLM fixo, embedding local
uv run --no-sync pytest tests/test_pipeline_smoke.py -v

# Avaliação offline: replay do ask, estatística dos gold sets, mapa de medições (sem LLM, sem rede)
uv run --no-sync pytest tests/evals/ -v
uv run --no-sync python -m zettel.evals.replay evals/configs/current-ask.yaml

# Repetir a ordem aleatória de uma execução (a semente aparece no cabeçalho)
uv run --no-sync pytest --randomly-seed=N
```

A suíte não acessa a rede:

- `tests/conftest.py` recusa qualquer conexão fora de `127.0.0.1`/`::1`, e um teste que tente abrir conexão falha com `NetworkBlockedInTests`;
- o mapa de preços usado é o que vem com o LiteLLM instalado (`LITELLM_LOCAL_MODEL_COST_MAP`);
- a *fixture* `token_hash_embedding` oferece um embedding determinístico local para os testes que precisam do `VectorIndex` real.

A configuração do pytest fica em `pyproject.toml` (`[tool.pytest.ini_options]`):

- *markers* estritos;
- `DeprecationWarning` do próprio pacote vira erro;
- *timeout* de 300 s por teste;
- ordem aleatória a cada execução (`pytest-randomly`; a semente aparece no cabeçalho e `--randomly-seed=N` repete a ordem).

O CI instala com `--extra cpu`. A estratégia está no [ADR-065](adrs/generated/INFRA/ADR-065-test-strategy.md).

---

## Próximos passos

- [Configuração](configuracao.md): catálogo completo do `config.yaml`, provedores de LLM e embedding
- [Comandos (CLI)](cli.md): referência de todos os comandos e flags
- [Pipeline](pipeline.md): o que cada fase faz, em detalhe
- [Solução de problemas](troubleshooting.md): erros comuns e como sair deles
