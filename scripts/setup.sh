#!/usr/bin/env bash
# Prepara o Zettel num clone novo: dependencias, .env, vault e diagnostico.
#
#   scripts/setup.sh            # escolhe torch CUDA se houver nvidia-smi, senao CPU
#   scripts/setup.sh --cpu      # forca torch CPU
#   scripts/setup.sh --cuda     # forca torch CUDA 12.6
#
# Idempotente: rodar de novo nao sobrescreve o .env nem o SESSION_SECRET.
set -euo pipefail
cd "$(dirname "$0")/.."

variant=""
case "${1:-}" in
  --cpu) variant=cpu ;;
  --cuda) variant=cu126 ;;
  "") ;;
  *) echo "uso: scripts/setup.sh [--cpu | --cuda]" >&2; exit 2 ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  echo "uv nao encontrado. Instale com:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  echo "e rode este script de novo (o uv tambem baixa o Python 3.12)." >&2
  exit 1
fi

# 1. Dependencias. O torch vem pelo Docling; o extra so escolhe o build.
extra=""
if [ "$(uname -s)" = "Darwin" ]; then
  [ -n "$variant" ] && echo "macOS: o torch vem do PyPI; --cpu/--cuda ignorado."
else
  if [ -z "$variant" ]; then
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
      variant=cu126
    else
      variant=cpu
    fi
  fi
  extra="--extra $variant"
  echo "torch: $variant"
fi
# shellcheck disable=SC2086  # $extra is empty or two words on purpose
uv sync $extra

# 2. Segredos. As chaves de API ficam para voce; o segredo da web e gerado.
if [ ! -f .env ]; then
  cp .env.example .env
  echo ".env criado a partir de .env.example"
fi
if ! grep -Eq '^SESSION_SECRET=.+' .env; then
  secret="$(uv run --no-sync python -c 'import secrets; print(secrets.token_urlsafe(32))')"
  if grep -q '^SESSION_SECRET=' .env; then
    sed -i.bak "s|^SESSION_SECRET=.*|SESSION_SECRET=${secret}|" .env && rm -f .env.bak
  else
    printf '\nSESSION_SECRET=%s\n' "$secret" >> .env
  fi
  echo "SESSION_SECRET gerado no .env"
fi

# 3. Vault, bases e diagnostico. --no-sync: um `uv run` simples sincronizaria sem o
# extra e trocaria o torch pelo do PyPI.
uv run --no-sync python -m zettel init
uv run --no-sync python -m zettel doctor || true

cat <<'EOF'

Pronto. Proximos passos:
  1. Preencha no .env as chaves que o doctor marcou como FALHA.
  2. Rode os comandos com:  uv run --no-sync python -m zettel <comando>
     (ou ative o ambiente: source .venv/bin/activate)
  3. Coloque um PDF ou Markdown em data/inbox/ e rode: run-all
EOF
