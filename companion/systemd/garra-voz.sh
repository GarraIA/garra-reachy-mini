#!/usr/bin/env bash
# Sobe o servidor de voz para o systemd.
#
# Existe porque duas coisas não cabem numa unidade systemd:
#
#  1. `LD_LIBRARY_PATH` das libs CUDA. Elas vêm dos wheels `nvidia-*` do venv,
#     em doze diretórios separados (cublas, cudnn, cufft, …). `Environment=`
#     não expande `*` — o systemd passa o asterisco literal —, então um glob
#     ali resultaria num caminho inválido e o faster-whisper cairia CALADO para
#     `int8` na CPU. Aqui é shell: o glob expande.
#
#  2. O token. Fica num arquivo 0600 lido na hora, e não numa linha de
#     `Environment=` que apareceria em `systemctl show`.
#
# Termina em `exec` para o processo do Python HERDAR o PID da unidade — sem
# isso o systemd vigiaria o shell, e um `stop` mataria o pai deixando o
# servidor de voz órfão segurando a GPU.
set -euo pipefail

BASE="${GARRA_BASE:-$HOME/Documents/Projetos/Reachy-Mini-Control}"
APP="$BASE/garra_reachy_mini"
PY="$BASE/voz_env/bin/python"
CONF="${GARRA_REACHY_CONF:-$HOME/.config/garra-reachy}"
HOST="${GARRA_VOZ_HOST:-0.0.0.0}"
PORT="${GARRA_VOZ_PORTA:-8123}"

[ -x "$PY" ] || { echo "voz_env não encontrado em $PY" >&2; exit 1; }
[ -f "$APP/tools/servidor_voz.py" ] || { echo "servidor_voz.py não encontrado" >&2; exit 1; }

# Doze diretórios, resolvidos agora. `printf %s:` + corte do último ':' evita
# uma entrada VAZIA no fim — entrada vazia em LD_LIBRARY_PATH significa
# "diretório corrente", que é exatamente o tipo de coisa que se descobre tarde.
libs=""
for d in "$BASE"/voz_env/lib/python3.*/site-packages/nvidia/*/lib; do
  [ -d "$d" ] && libs="${libs}${d}:"
done
if [ -n "$libs" ]; then
  export LD_LIBRARY_PATH="${libs%:}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

# Token da rede. O companion gera; se faltar, o servidor recusa escutar fora do
# loopback — falhar alto é melhor do que abrir a GPU para a LAN em silêncio.
if [ -r "$CONF/voz.token" ]; then
  GARRA_VOZ_TOKEN="$(cat "$CONF/voz.token")"
  export GARRA_VOZ_TOKEN
fi

exec "$PY" "$APP/tools/servidor_voz.py" --host "$HOST" --port "$PORT"
