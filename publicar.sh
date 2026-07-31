#!/usr/bin/env bash
# Publica o app no Hugging Face Spaces (requer `huggingface-cli login` antes).
# O publish valida a estrutura, cria o Space (sdk: static) e faz git init +
# push desta pasta — o que também a isola do repo git acidental do diretório pai.
set -e
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$APP_DIR")"
ASSISTANT="$BASE_DIR/reachy_mini_env/bin/reachy-mini-app-assistant"

"$ASSISTANT" check "$APP_DIR"
"$ASSISTANT" publish "$APP_DIR" "${1:-Atualização do garra_reachy_mini}"
