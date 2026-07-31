#!/usr/bin/env bash
# Instala as duas unidades de usuário do companion. Sem sudo: `systemctl --user`
# fala com o gerenciador do próprio usuário, e `loginctl enable-linger` (já
# ativo aqui) faz elas sobreviverem a logout e reboot.
#
#   bash companion/instalar.sh            # instala e liga o companion
#   bash companion/instalar.sh --auto-voz # e também liga a voz no arranque
set -euo pipefail

AQUI="$(cd "$(dirname "$0")" && pwd)"
DESTINO="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONF="${GARRA_REACHY_CONF:-$HOME/.config/garra-reachy}"

mkdir -p "$DESTINO" "$CONF"
chmod 700 "$CONF"

# Token da voz: criado uma vez, 0600. É o que impede que qualquer aparelho da
# LAN use a GPU depois que o servidor passa a escutar em 0.0.0.0.
if [ ! -s "$CONF/voz.token" ]; then
  umask 077
  head -c 24 /dev/urandom | base64 | tr -d '=+/' | cut -c1-32 > "$CONF/voz.token"
  chmod 600 "$CONF/voz.token"
  echo "token da voz criado em $CONF/voz.token (0600)"
else
  echo "token da voz já existia — mantido (o robô já pode ter uma cópia)"
fi

for unidade in garra-reachy-voice.service garra-reachy-companion.service; do
  cp "$AQUI/systemd/$unidade" "$DESTINO/$unidade"
  echo "instalada: $DESTINO/$unidade"
done
chmod +x "$AQUI/systemd/garra-voz.sh"

systemctl --user daemon-reload
systemctl --user enable --now garra-reachy-companion.service
echo "companion ligado em http://127.0.0.1:8125"

if [ "${1:-}" = "--auto-voz" ]; then
  systemctl --user enable --now garra-reachy-voice.service
  echo "voz ligada e marcada para subir no arranque"
else
  echo "voz NÃO foi ligada: use o painel em http://localhost:3888/#/reachy"
fi

if [ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null)" != "yes" ]; then
  echo
  echo "AVISO: linger desligado — as unidades não sobem antes do login."
  echo "       Ligue com: loginctl enable-linger $USER"
fi
