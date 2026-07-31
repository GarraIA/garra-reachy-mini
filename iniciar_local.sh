#!/usr/bin/env bash
# Modo desktop/standalone: sobe o servidor de voz se preciso e roda o app
# no reachy_mini_env, controlando o robô pela rede.
#
# Sobe, no total, dois processos:
#   servidor_voz.py           :8123  Whisper (STT) + Chatterbox (TTS) na GPU
#   python -m garra_reachy_mini.main    loop de voz + controlador do robô + painel
#
# O gateway do Garra (:3888) NÃO é iniciado aqui: ele roda como serviço do
# usuário (`systemctl --user status garraia`) e é quem serve o console web.
set -e
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$APP_DIR")"                 # Reachy-Mini-Control
VOZ_URL="${GARRA_VOZ_URL:-http://127.0.0.1:8123}"
ROBO_API="${GARRA_ROBO_API:-http://reachy-mini.local:8000}"
GATEWAY_URL="${GARRA_GATEWAY_URL:-http://127.0.0.1:3888}"

az() { printf '\033[36m%s\033[0m' "$1"; }
verde() { printf '\033[32m%s\033[0m' "$1"; }
amarelo() { printf '\033[33m%s\033[0m' "$1"; }
vermelho() { printf '\033[31m%s\033[0m' "$1"; }

# Plugin WebRTC do GStreamer (mídia do robô) + libs CUDA do venv de voz
export GST_PLUGIN_PATH="/opt/gst-plugins-rs/lib/x86_64-linux-gnu:${GST_PLUGIN_PATH:-}"
NV_LIBS="$(find "$BASE_DIR/voz_env/lib/python3.12/site-packages/nvidia" \
  -maxdepth 2 -type d -name lib 2>/dev/null | paste -sd: || true)"
# Sem `:` sobrando: entrada vazia em LD_LIBRARY_PATH = diretório corrente no
# caminho de busca de bibliotecas.
if [ -n "$NV_LIBS" ]; then
  export LD_LIBRARY_PATH="${NV_LIBS}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

# ── dependências ─────────────────────────────────────────────────────────────
faltando=""
VOZ_PY="$BASE_DIR/servidor_voz.py"
[ -f "$VOZ_PY" ] || VOZ_PY="$APP_DIR/tools/servidor_voz.py"
for caminho in "$BASE_DIR/reachy_mini_env/bin/python" "$BASE_DIR/voz_env/bin/python" \
               "$VOZ_PY"; do
  [ -e "$caminho" ] || faltando="$faltando\n  - $caminho"
done
if [ -n "$faltando" ]; then
  printf "$(vermelho 'Faltam arquivos essenciais:')%b\n" "$faltando"
  echo "Confira o README: os ambientes virtuais precisam existir antes de iniciar."
  exit 1
fi
if [ ! -d /opt/gst-plugins-rs/lib/x86_64-linux-gnu ]; then
  echo "$(amarelo 'Aviso:') /opt/gst-plugins-rs não encontrado — a câmera do robô (WebRTC) não vai abrir."
  echo "       Rode o setup_sistema.sh para instalá-lo."
fi

# ── robô ─────────────────────────────────────────────────────────────────────
ESTADO_ROBO="desconectado"
MODO="simulado (sem robô)"
if STATUS=$(curl -sf -m 6 "$ROBO_API/api/daemon/status" 2>/dev/null); then
  ESTADO_ROBO="conectado"
  MODO="hardware real"
  IP_ROBO=$(printf '%s' "$STATUS" | grep -oP '"wlan_ip":"\K[^"]+' || true)
  VERSAO=$(printf '%s' "$STATUS" | grep -oP '"version":"\K[^"]+' || true)
else
  echo "$(amarelo 'Aviso:') o robô não respondeu em $ROBO_API."
  echo "       O painel e a API sobem assim mesmo, em modo simulado."
fi

# ── gateway do Garra ─────────────────────────────────────────────────────────
GARRA="inativo"
curl -sf -m 4 "$GATEWAY_URL/ping" >/dev/null 2>&1 && GARRA="ativo"
if [ "$GARRA" = "inativo" ]; then
  echo "$(amarelo 'Aviso:') o gateway do Garra não respondeu em $GATEWAY_URL."
  echo "       Sem ele o chat e a voz ficam sem cérebro: systemctl --user start garraia"
fi

# ── servidor de voz: reaproveita se já estiver de pé, senão sobe e espera ────
VOZ_PID=""
limpar() {
  [ -n "$VOZ_PID" ] && kill "$VOZ_PID" 2>/dev/null || true
  [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap limpar EXIT INT TERM

if ! curl -sf -m 4 "$VOZ_URL/saude" >/dev/null 2>&1; then
  echo "Subindo servidor de voz (carrega Whisper e Chatterbox na GPU, ~1 min)..."
  "$BASE_DIR/voz_env/bin/python" "$VOZ_PY" &
  VOZ_PID=$!
  until curl -sf -m 4 "$VOZ_URL/saude" >/dev/null 2>&1; do
    kill -0 $VOZ_PID 2>/dev/null || { echo "$(vermelho 'servidor_voz morreu')"; exit 1; }
    sleep 2
  done
fi

# Libera microfone/alto-falante do robô (fora do app, de propósito:
# o daemon só permite um app ativo e um app não deve parar a si mesmo)
curl -sf -m 10 -X POST "$ROBO_API/api/apps/stop-current-app" >/dev/null 2>&1 || true
sleep 3

# Binário do garra instalado pelo tools/install_garra.sh, se houver
[ -x "$APP_DIR/bin/garra" ] && export GARRA_BIN="${GARRA_BIN:-$APP_DIR/bin/garra}"

# Pacote importável no reachy_mini_env (instala editável na primeira vez)
"$BASE_DIR/reachy_mini_env/bin/python" -c "import garra_reachy_mini" 2>/dev/null \
  || "$BASE_DIR/reachy_mini_env/bin/pip" install -e "$APP_DIR"
# O servidor MCP `reachy` (as ferramentas de corpo do Garra) mora fora do
# pacote instalável; o gateway o encontra por PYTHONPATH.
export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"

# ── porta do painel ──────────────────────────────────────────────────────────
# A 8042 é a porta canônica de um app do Reachy, mas no desktop o
# reachy-mini-control da Pollen já a ocupa como proxy. O app escolhe a próxima
# livre sozinho; descobrimos aqui só para imprimir a URL certa.
PORTA_PAINEL=$("$BASE_DIR/reachy_mini_env/bin/python" - <<'PY'
from garra_reachy_mini.web.seguranca import resolver_politica
print(resolver_politica().porta)
PY
)

# ── banner ───────────────────────────────────────────────────────────────────
cat <<BANNER

$(verde 'Garra Reachy Mini iniciado com sucesso.')

  Web:        $(az "$GATEWAY_URL/")
  Reachy UI:  $(az "$GATEWAY_URL/#/reachy")   $(printf '\033[2m(direto: http://localhost:%s/reachy)\033[0m' "$PORTA_PAINEL")
  API:        $(az "http://localhost:$PORTA_PAINEL/api/robot")
  Status:     Reachy $ESTADO_ROBO${IP_ROBO:+ ($IP_ROBO, daemon $VERSAO)}
  Garra:      $GARRA
  Voz:        $VOZ_URL
  Mode:       $MODO

  Ctrl+C encerra tudo. Emergência: botão PARAR no painel, tecla Esc,
  ou   curl -X POST http://localhost:$PORTA_PAINEL/api/robot/stop

BANNER

# Em foreground SEM exec: exec substituiria o shell e o trap EXIT nunca
# dispararia, deixando o servidor_voz órfão com a GPU ocupada no Ctrl+C.
"$BASE_DIR/reachy_mini_env/bin/python" -u -m garra_reachy_mini.main &
APP_PID=$!
wait $APP_PID
