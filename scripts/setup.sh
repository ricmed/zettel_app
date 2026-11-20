#!/usr/bin/env bash
# Prepara o Zettel num clone novo: uv, dependencias, .env, vault e diagnostico.
#
#   scripts/setup.sh            # escolhe torch CUDA se houver nvidia-smi, senao CPU
#   scripts/setup.sh --cpu      # forca torch CPU
#   scripts/setup.sh --cuda     # forca torch CUDA 12.6
#
# Idempotente: rodar de novo nao sobrescreve o .env nem o SESSION_SECRET.
# Se o uv nao estiver no PATH, o script instala o binario oficial em ~/.local/bin.
# Um hash mismatch de torch/torchvision atualiza o lock desses pacotes, grava o
# hash da roda baixada quando o indice nao publica um, e tenta o sync outra vez.
set -euo pipefail
cd "$(dirname "$0")/.."

variant=""
case "${1:-}" in
  --cpu) variant=cpu ;;
  --cuda) variant=cu126 ;;
  "") ;;
  *) echo "uso: scripts/setup.sh [--cpu | --cuda]" >&2; exit 2 ;;
esac

if ! command -v uv >/dev/null 2>&1 && [ -x "${HOME}/.local/bin/uv" ]; then
  export PATH="${HOME}/.local/bin:${PATH}"
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "uv nao encontrado. Instalando..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "A instalacao do uv nao o deixou disponivel nesta sessao." >&2
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

# O indice CUDA publica algumas rodas sem sha256 do arquivo. O uv entao recusa o
# download. Gravamos o hash que ele acabou de calcular, na roda deste Python.
repair_torch_wheel_hash() {
  local log="$1"
  local name version hash py tag plat wheel ver_enc
  name="$(sed -n 's/.*Failed to download `\([^`]*\)==\([^`]*\)`.*/\1/p' "$log" | head -n 1)"
  version="$(sed -n 's/.*Failed to download `\([^`]*\)==\([^`]*\)`.*/\2/p' "$log" | head -n 1)"
  hash="$(awk 'f && /sha256:/{print; exit} /Computed:/{f=1}' "$log" | sed -n 's/.*sha256:\([0-9a-fA-F]\{64\}\).*/\1/p')"
  if [ "$name" != torch ] && [ "$name" != torchvision ]; then
    return 0
  fi
  if [ -z "$version" ] || [ -z "$hash" ]; then
    return 0
  fi
  py="$(tr -d '[:space:]' < .python-version)"
  tag="cp${py//./}"
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) plat=manylinux_2_28_x86_64 ;;
    Linux-aarch64|Linux-arm64) plat=manylinux_2_28_aarch64 ;;
    Darwin-arm64) plat=macosx_14_0_arm64 ;;
    Darwin-x86_64) plat=macosx_10_12_x86_64 ;;
    *) return 0 ;;
  esac
  ver_enc="${version//+/%2B}"
  wheel="${name}-${ver_enc}-${tag}-${tag}-${plat}.whl"
  if ! grep -F -q "$wheel" uv.lock; then
    echo "Nao achei $wheel no uv.lock; o novo sync segue com o lock atual."
    return 0
  fi
  awk -v wheel="$wheel" -v hash="$(printf '%s' "$hash" | tr '[:upper:]' '[:lower:]')" '
    index($0, wheel) {
      if ($0 ~ /hash = "sha256:[0-9a-fA-F]{64}"/) {
        sub(/hash = "sha256:[0-9a-fA-F]{64}"/, "hash = \"sha256:" hash "\"")
      } else {
        sub(/url = "[^"]+"/, "&, hash = \"sha256:" hash "\"")
      }
    }
    { print }
  ' uv.lock > uv.lock.tmp
  mv uv.lock.tmp uv.lock
  echo "Hash gravado para $wheel."
}

sync_deps() {
  local log code
  log="$(mktemp)"
  set +e
  # shellcheck disable=SC2086  # $extra is empty or two words on purpose
  uv sync $extra >"$log" 2>&1
  code=$?
  set -e
  cat "$log"
  if [ "$code" -eq 0 ]; then
    rm -f "$log"
    return 0
  fi
  if grep -q 'Hash mismatch' "$log" && grep -q 'torch' "$log"; then
    echo "Hash do torch/torchvision desatualizado. Atualizando o lock e tentando de novo."
    uv lock --refresh-package torch --refresh-package torchvision
    repair_torch_wheel_hash "$log"
    rm -f "$log"
    # shellcheck disable=SC2086
    uv sync $extra
    return 0
  fi
  rm -f "$log"
  exit "$code"
}
sync_deps

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
