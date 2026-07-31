#!/usr/bin/env bash
# Publica o app no Hugging Face Spaces, que é de onde o "Discover apps" do robô
# tira a lista: o daemon consulta os Spaces com a tag `reachy_mini_python_app`
# (reachy_mini/apps/sources/hf_space.py). Não há PR, formulário nem aprovação —
# Space público com a tag certa aparece.
#
#   bash publicar.sh privado "mensagem"   # 1º: Space privado, para testar no robô
#   bash publicar.sh publico "mensagem"   # 2º: abre ao público, depois de testar
#   bash publicar.sh oficial              # 3º: PR opcional p/ a prateleira curada
#
# Requer login no Hugging Face antes (uma vez só):
#   hf auth login
#
# O Space privado já aparece no Discover DESTE robô, porque o daemon consulta a
# API autenticado com o token guardado nele. É o que permite instalar e testar
# de verdade antes de expor a qualquer pessoa.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$APP_DIR")"
ASSISTANT="$BASE_DIR/reachy_mini_env/bin/reachy-mini-app-assistant"
HF="$BASE_DIR/reachy_mini_env/bin/hf"
ETAPA="${1:-}"
MENSAGEM="${2:-Garra Reachy Mini $(grep -m1 '^version' "$APP_DIR/pyproject.toml" | cut -d'"' -f2)}"

# O `publish` deriva o nome do Space da PASTA, não do project.name.
ESPACO="$(basename "$APP_DIR")"

case "$ETAPA" in
  privado|publico|oficial) ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac

if ! "$HF" auth whoami >/dev/null 2>&1; then
  echo "Sem credencial do Hugging Face. Rode primeiro:" >&2
  echo "    hf auth login" >&2
  echo "com um token de ESCRITA de https://huggingface.co/settings/tokens" >&2
  exit 1
fi
DONO="$("$HF" auth whoami | head -1)"

case "$ETAPA" in
  privado)
    "$ASSISTANT" check "$APP_DIR"
    "$ASSISTANT" publish "$APP_DIR" "$MENSAGEM" --private
    cat <<FIM

Space privado: https://huggingface.co/spaces/$DONO/$ESPACO

Ele já aparece no Discover apps DESTE robô (o daemon consulta autenticado).
Antes de abrir ao público, instale e teste lá:

  curl -X POST http://reachy-mini.local:8000/api/apps/install \\
       -H 'Content-Type: application/json' \\
       -d '{"name":"$ESPACO","source_kind":"hf_space","url":"https://huggingface.co/spaces/$DONO/$ESPACO","extra":{"id":"$DONO/$ESPACO"}}'
  curl -X POST http://reachy-mini.local:8000/api/apps/start-app/$ESPACO
  curl -X POST http://reachy-mini.local:8000/api/apps/stop-current-app
FIM
    ;;

  publico)
    "$ASSISTANT" check "$APP_DIR"
    "$ASSISTANT" publish "$APP_DIR" "$MENSAGEM" --public
    echo
    echo "Confirme que entrou na listagem da comunidade (pode levar minutos):"
    echo "  curl -s 'https://huggingface.co/api/spaces?filter=reachy_mini_python_app&limit=1000' | grep -o '$DONO/$ESPACO'"
    echo "E no próprio robô, que é o que o usuário vê:"
    echo "  curl -s http://reachy-mini.local:8000/api/apps/list-available/hf_space | grep -o '$ESPACO'"
    ;;

  oficial)
    # Abre PR no dataset pollen-robotics/reachy-mini-official-app-store. É o
    # ÚNICO passo com aprovação humana — e serve só para destaque na prateleira
    # curada; não é requisito para instalar nem para aparecer no Discover.
    "$ASSISTANT" publish "$APP_DIR" "$MENSAGEM" --official
    ;;

  *)
    sed -n '2,16p' "$0"
    exit 2
    ;;
esac
