#!/usr/bin/env bash
# Sobe a ponte da Hera para o robô.
#
# É o MESMO código da ponte de voz do site (Painel-Local, voice_bridge/server.py),
# em segundo processo, com o perfil de canal do robô: soquete próprio, sessão do
# Hermes só do robô, e as ferramentas do nível que o dono escolheu. Roda com o
# Python do Hermes, como a do site, porque é ela quem resolve a chave da OpenAI —
# que não sai deste processo e não tem porta TCP.
#
# O perfil é gravado por `python -m companion.hera` (liberar, nivel, perfil).
set -euo pipefail
umask 077

CONF="${GARRA_REACHY_CONF:-$HOME/.config/garra-reachy}"
PERFIL="$CONF/hera-canal.json"
PONTE="${GARRA_PONTE_DO_PAINEL:-/mnt/NewVolume/Projetos/Painel-Local/voice_bridge/server.py}"
HERMES_ROOT="${HERMES_ROOT:-$HOME/.hermes/hermes-agent}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PYTHON="${HERMES_PYTHON:-$(ls -d "$HOME"/.hermes/tools/python-3.14*/bin/python3 2>/dev/null | head -n 1)}"
SOQUETE="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/garra-reachy/hera-bridge.sock"

[ -x "${PYTHON:-}" ] || { echo "garra-hera: Python do Hermes não encontrado" >&2; exit 1; }
[ -f "$PONTE" ] || { echo "garra-hera: ponte de voz do Painel-Local não encontrada em $PONTE" >&2; exit 1; }
grep -q "ChannelProfile" "$PONTE" || {
  echo "garra-hera: esta ponte do Painel-Local ainda não tem perfil de canal" >&2; exit 1; }
[ -f "$PERFIL" ] || {
  echo "garra-hera: perfil do canal ausente; rode: python -m companion.hera perfil" >&2; exit 1; }

# O Hermes carrega as dependências (ruamel, websockets) do ambiente gerenciado, e
# o systemd não herda o PYTHONPATH da sessão: acha-se o site-packages que as tem.
if [[ -z "${PYTHONPATH:-}" ]]; then
  for site in $(find "$HOME/.hermes/installs" -type d -path '*/venv/lib/python3.14/site-packages' 2>/dev/null); do
    if [[ -d "$site/ruamel" && -d "$site/websockets" ]]; then
      export PYTHONPATH="$site"
      break
    fi
  done
fi

mkdir -p -m 700 "$(dirname "$SOQUETE")"
export HERMES_HOME
exec "$PYTHON" "$PONTE" \
  --socket "$SOQUETE" \
  --profile "$PERFIL" \
  --hermes-root "$HERMES_ROOT" \
  --hermes-home "$HERMES_HOME" \
  --cwd "$HOME"
